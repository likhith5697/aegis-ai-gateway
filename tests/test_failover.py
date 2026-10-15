"""Tests for gateway/failover.py and gateway/failover_hook.py.

The router tests use LiteLLM's real Router, built from config.sandbox.yaml, against
the real chaos container (scripts/chaos_llm.py): claude-opus-5-5 is the breakable
chaos-primary, claude-sonnet-5-5 the always-healthy chaos-backup. No real model, no cost.
"""

import logging
import os
import time
from types import SimpleNamespace

import httpx
import litellm
import pytest
import yaml
from litellm import Router
from litellm.types.router import RouterRateLimitError

from failover import install_failover_policy, should_fail_over
from failover_hook import FailoverHook
from tenants import DEFAULT_TENANTS_FILE

pytestmark = pytest.mark.anyio
CHAOS_URL = os.environ.get("AEGIS_TEST_CHAOS_URL", "http://chaos:8080")
GATEWAY_DIR = DEFAULT_TENANTS_FILE.parent
PRIMARY, FALLBACK = "claude-opus-5-5", "claude-sonnet-5-5"
MESSAGES = [{"role": "user", "content": "Say hi."}]

install_failover_policy()


@pytest.fixture
def anyio_backend():
    return "asyncio"


def load_config(name: str) -> dict:
    return yaml.safe_load((GATEWAY_DIR / name).read_text())


# --- 1. which errors may fail over (no network) ---

def error(cls, **extra):
    return cls(message="boom", model="m", llm_provider="openai", **extra)


FORBIDDEN = httpx.Response(403, request=httpx.Request("POST", "http://x"))


@pytest.mark.parametrize(
    "exc",
    [
        error(litellm.RateLimitError),                 # 429
        error(litellm.ServiceUnavailableError),        # 503
        error(litellm.InternalServerError),            # 500
        error(litellm.Timeout),                        # 408 / timed out
        error(litellm.APIConnectionError),             # never reached the model
        RouterRateLimitError(model="m", cooldown_time=10, enable_pre_call_checks=False, cooldown_list=[]),
    ],
    ids=lambda e: type(e).__name__,
)
def test_transient_errors_fail_over(exc):
    assert should_fail_over(exc)


@pytest.mark.parametrize(
    "exc",
    [
        error(litellm.BadRequestError),                # 400
        error(litellm.AuthenticationError),            # 401
        error(litellm.PermissionDeniedError, response=FORBIDDEN),  # 403
        error(litellm.NotFoundError),                  # 404
        error(litellm.ContentPolicyViolationError),
        error(litellm.ContextWindowExceededError),
    ],
    ids=lambda e: type(e).__name__,
)
def test_client_errors_do_not_fail_over(exc):
    assert not should_fail_over(exc)


# --- 2. the real router against the chaos container ---

def set_chaos(mode: str) -> None:
    httpx.post(f"{CHAOS_URL}/chaos", params={"mode": mode}).raise_for_status()


def primary_calls() -> int:
    return httpx.get(f"{CHAOS_URL}/chaos").json()["primary_requests"]


def sandbox_router(timeout: float = 1) -> Router:
    """A fresh router (fresh cooldowns) with the sandbox's failover settings."""
    config = load_config("config.sandbox.yaml")
    return Router(model_list=config["model_list"], **{**config["router_settings"], "timeout": timeout})


def answered_by(response) -> tuple[str, int]:
    headers = response._hidden_params["additional_headers"]
    return headers["x-litellm-model-group"], int(headers["x-litellm-attempted-fallbacks"])


async def ask(router, **kwargs):
    return await router.acompletion(model=PRIMARY, messages=MESSAGES, **kwargs)


async def test_healthy_primary_answers_without_fallback():
    set_chaos("ok")
    response = await ask(sandbox_router())
    assert answered_by(response) == (PRIMARY, 0)
    assert primary_calls() == 1


async def test_primary_429_fails_over_without_retrying():
    set_chaos("429")
    response = await ask(sandbox_router())
    assert answered_by(response) == (FALLBACK, 1)
    assert primary_calls() == 1  # RateLimitErrorRetries: 0


async def test_primary_503_is_retried_once_then_fails_over():
    set_chaos("503")
    response = await ask(sandbox_router())
    assert answered_by(response) == (FALLBACK, 1)
    assert primary_calls() == 2  # ServiceUnavailableErrorRetries: 1


async def test_primary_timeout_fails_over():
    set_chaos("hang")
    started = time.monotonic()
    response = await ask(sandbox_router(timeout=1))
    assert answered_by(response) == (FALLBACK, 1)
    assert 1 <= time.monotonic() - started < 5  # waited for the timeout, once (TimeoutErrorRetries: 0)
    assert primary_calls() == 1


@pytest.mark.parametrize(
    "mode, expected", [("401", litellm.AuthenticationError), ("400", litellm.BadRequestError)]
)
async def test_client_errors_are_returned_without_fallback(mode, expected):
    set_chaos(mode)
    with pytest.raises(expected):  # had it failed over, the backup would have answered
        await ask(sandbox_router())
    assert primary_calls() == 1  # no retry either


async def test_cooldown_skips_the_bad_model_on_the_next_request():
    set_chaos("429")
    router = sandbox_router()
    assert answered_by(await ask(router)) == (FALLBACK, 1)
    assert primary_calls() == 1
    # The primary is now in cooldown: the next request goes straight to the fallback.
    assert answered_by(await ask(router)) == (FALLBACK, 1)
    assert primary_calls() == 1  # not called again


async def test_401s_never_trip_the_circuit_breaker():
    set_chaos("401")
    router = sandbox_router()
    for _ in range(5):
        with pytest.raises(litellm.AuthenticationError):
            await ask(router)
    set_chaos("ok")
    assert answered_by(await ask(router)) == (PRIMARY, 0)  # still in rotation


async def test_401s_do_not_lower_the_bar_for_a_later_503():
    # Fail counters are per error type (allowed_fails_policy in model_info), so earlier
    # 401s don't count toward the 503 limit: the 503 still gets its retry.
    router = sandbox_router()
    set_chaos("401")
    for _ in range(3):
        with pytest.raises(litellm.AuthenticationError):
            await ask(router)
    set_chaos("503")
    assert answered_by(await ask(router)) == (FALLBACK, 1)
    assert primary_calls() == 2


async def collect_stream(router) -> tuple[str, Exception | None]:
    text, failure = "", None
    try:
        stream = await ask(router, stream=True)
        async for chunk in stream:
            text += chunk.choices[0].delta.content or ""
    except Exception as e:
        failure = e
    return text, failure


async def test_stream_failing_before_the_first_token_fails_over():
    set_chaos("503")
    text, failure = await collect_stream(sandbox_router())
    assert failure is None
    assert text.strip() == "Hello from chaos-backup. You said: Say hi."  # one clean answer, from the backup


async def test_stream_failing_mid_answer_is_not_failed_over():
    set_chaos("cut")
    text, failure = await collect_stream(sandbox_router())
    assert text.strip() == "Hello from"   # what the primary had already sent
    assert failure is not None            # then the stream ends with an error
    assert "backup" not in text           # no second answer glued onto the first


# --- 3. the hook and the config ---

@pytest.fixture
def failover_logs():
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = ListHandler()
    logger = logging.getLogger("aegis.failover")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def fake_response(model_group, attempted_fallbacks):
    headers = {"x-litellm-model-group": model_group, "x-litellm-attempted-fallbacks": attempted_fallbacks}
    return SimpleNamespace(_hidden_params={"additional_headers": headers})


async def test_headers_name_the_model_that_answered():
    hook = FailoverHook()
    data = {"model": PRIMARY}
    assert await hook.async_post_call_response_headers_hook(data, None, fake_response(FALLBACK, 1)) == {
        "x-aegis-model-used": FALLBACK,
        "x-aegis-fallback": "true",
    }
    assert await hook.async_post_call_response_headers_hook(data, None, fake_response(PRIMARY, 0)) == {
        "x-aegis-model-used": PRIMARY,
        "x-aegis-fallback": "false",
    }
    assert await hook.async_post_call_response_headers_hook(data, None, None) is None


async def test_failover_is_logged_without_prompt_content(failover_logs):
    hook = FailoverHook()
    data = {
        "model": PRIMARY,
        "messages": [{"role": "user", "content": "my secret plan"}],
        "proxy_server_request": {"headers": {"tenant-id": "engineering"}},
    }
    await hook.async_pre_call_hook(None, None, data, "completion")
    kwargs = {"model": FALLBACK, "metadata": dict(data["metadata"]), "messages": data["messages"]}
    await hook.log_success_fallback_event(PRIMARY, kwargs, error(litellm.ServiceUnavailableError))
    await hook.log_failure_fallback_event(
        PRIMARY, kwargs, RouterRateLimitError(model=PRIMARY, cooldown_time=10, enable_pre_call_checks=False, cooldown_list=[])
    )

    assert len(failover_logs) == 2
    assert failover_logs[0].startswith(
        "failover tenant=engineering primary=claude-opus-5-5 error=ServiceUnavailableError "
        "fallback=claude-sonnet-5-5 outcome=answered latency_ms="
    )
    assert "error=cooldown" in failover_logs[1] and "outcome=failed" in failover_logs[1]
    assert not any("secret" in line for line in failover_logs)


def test_sandbox_uses_the_same_failover_policy_as_production():
    real, sandbox = load_config("config.yaml"), load_config("config.sandbox.yaml")
    for key in ("fallbacks", "num_retries", "retry_policy"):
        assert real["router_settings"][key] == sandbox["router_settings"][key], key
    assert [m["model_name"] for m in real["model_list"]] == [m["model_name"] for m in sandbox["model_list"]]
    assert [m["model_info"] for m in real["model_list"]] == [m["model_info"] for m in sandbox["model_list"]]
    assert real["litellm_settings"]["callbacks"] == sandbox["litellm_settings"]["callbacks"]
