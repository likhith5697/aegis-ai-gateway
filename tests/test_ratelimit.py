"""Tests for gateway/ratelimit.py and gateway/budget_hook.py against a real Redis (no mocks).

Runs inside Docker: the compose "tests" service starts the redis container first.
Every test gets an empty Redis database (DB 15, never the gateway's DB 0).
"""

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException
from redis.asyncio import Redis

from budget_hook import RESULT_KEY, BudgetHook, estimate_tokens
from ratelimit import SlidingWindowLimiter
from settings import FAIL_CLOSED, FAIL_OPEN
from tenants import DEFAULT_TENANTS_FILE

pytestmark = pytest.mark.anyio
REDIS_URL = os.environ.get("AEGIS_TEST_REDIS_URL", "redis://redis:6379/15")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def redis():
    client = Redis.from_url(REDIS_URL)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def limiter(redis):
    return SlidingWindowLimiter(redis, window_seconds=60)


# --- the limiter itself ---

async def test_under_limit_is_allowed(limiter):
    decision = await limiter.reserve("support", "req-1", tokens=400, limit=1000)
    assert decision.allowed
    assert decision.used == 400
    assert decision.remaining == 600


async def test_over_limit_is_rejected_with_retry_after(limiter):
    await limiter.reserve("support", "req-1", tokens=800, limit=1000)
    decision = await limiter.reserve("support", "req-2", tokens=300, limit=1000)
    assert not decision.allowed
    assert decision.used == 800  # the rejected request was not counted
    assert 0 < decision.retry_after_ms <= 60_000
    assert 1 <= decision.retry_after_seconds <= 60


async def test_window_slides_and_allows_again(redis):
    limiter = SlidingWindowLimiter(redis, window_seconds=1)
    assert (await limiter.reserve("support", "req-1", tokens=1000, limit=1000)).allowed
    blocked = await limiter.reserve("support", "req-2", tokens=1, limit=1000)
    assert not blocked.allowed
    assert blocked.retry_after_ms <= 1000

    await asyncio.sleep(1.1)  # req-1 slides out of the 1-second window

    again = await limiter.reserve("support", "req-3", tokens=1000, limit=1000)
    assert again.allowed
    assert again.used == 1000  # only req-3 is in the window now


async def test_one_tenant_at_its_limit_does_not_block_another(limiter):
    assert (await limiter.reserve("marketing", "m-1", tokens=1000, limit=1000)).allowed
    assert not (await limiter.reserve("marketing", "m-2", tokens=1, limit=1000)).allowed
    other = await limiter.reserve("engineering", "e-1", tokens=1000, limit=1000)
    assert other.allowed
    assert other.used == 1000


async def test_concurrent_requests_never_exceed_the_quota(limiter, redis):
    # 50 requests of 100 tokens race for a 1000-token budget: exactly 10 may win.
    decisions = await asyncio.gather(
        *(limiter.reserve("support", f"req-{i}", tokens=100, limit=1000) for i in range(50))
    )
    assert sum(d.allowed for d in decisions) == 10
    stored = await redis.hvals("aegis:tpm:{support}:tokens")
    assert sum(int(v) for v in stored) == 1000


async def test_record_replaces_the_estimate_with_actual_usage(limiter):
    await limiter.reserve("support", "req-1", tokens=900, limit=1000)
    assert await limiter.record("support", "req-1", tokens=100)  # the reply was shorter than max_tokens
    decision = await limiter.reserve("support", "req-2", tokens=800, limit=1000)
    assert decision.allowed
    assert decision.used == 900


async def test_release_gives_the_tokens_back(limiter):
    await limiter.reserve("support", "req-1", tokens=1000, limit=1000)
    await limiter.release("support", "req-1")
    assert (await limiter.reserve("support", "req-2", tokens=1000, limit=1000)).allowed


async def test_record_after_the_request_slid_out_changes_nothing(redis):
    limiter = SlidingWindowLimiter(redis, window_seconds=1)
    await limiter.reserve("support", "req-1", tokens=500, limit=1000)
    await asyncio.sleep(1.1)
    await limiter.reserve("support", "req-2", tokens=1, limit=1000)  # slides req-1 out
    assert not await limiter.record("support", "req-1", tokens=500)
    assert await redis.hget("aegis:tpm:{support}:tokens", "req-1") is None


async def test_idle_tenant_keys_expire(limiter, redis):
    await limiter.reserve("support", "req-1", tokens=10, limit=1000)
    assert 0 < await redis.pttl("aegis:tpm:{support}:log") <= 60_000
    assert 0 < await redis.pttl("aegis:tpm:{support}:tokens") <= 60_000


# --- the LiteLLM hook ---

TEST_TENANTS = """
tenants:
  support:
    allowed_models: [claude-sonnet-5-5]
    tpm_quota: 1000
  engineering:
    allowed_models: [claude-sonnet-5-5]
    tpm_quota: 100000
"""


@pytest.fixture
def tenants_file(tmp_path):
    path = tmp_path / "tenants.yaml"
    path.write_text(TEST_TENANTS)
    return path


@pytest.fixture
def hook(limiter, tenants_file):
    return BudgetHook(limiter=limiter, tenants_file=tenants_file)


@pytest.fixture
def ratelimit_logs():
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append((record.levelname, record.getMessage()))

    handler = ListHandler()
    logger = logging.getLogger("aegis.ratelimit")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def make_request(tenant="support", max_tokens=100, content="hi"):
    """Shape a request the way LiteLLM hands it to the hook."""
    return {
        "model": "claude-sonnet-5-5",
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": content}],
        "proxy_server_request": {"headers": {"tenant-id": tenant}},
    }


async def run_hook(hook, data):
    return await hook.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type="completion")


async def response_headers(hook, data, response=object()):
    return await hook.async_post_call_response_headers_hook(data=data, user_api_key_dict=None, response=response)


async def report_success(hook, data, total_tokens):
    """What LiteLLM calls after a successful model call."""
    kwargs = {"litellm_params": {"metadata": data["metadata"]}}
    response = SimpleNamespace(usage=SimpleNamespace(total_tokens=total_tokens))
    await hook.async_log_success_event(kwargs, response, None, None)


def test_estimate_is_prompt_tokens_plus_max_tokens():
    small = estimate_tokens(make_request(max_tokens=100, content="hi"))
    assert 100 < small < 150
    assert estimate_tokens(make_request(max_tokens=500, content="hi")) == small + 400


async def test_allowed_request_gets_ratelimit_headers(hook):
    data = make_request(max_tokens=100)
    assert await run_hook(hook, data) is data
    remaining = 1000 - estimate_tokens(data)
    assert await response_headers(hook, data) == {
        "x-aegis-ratelimit-limit": "1000",
        "x-aegis-ratelimit-remaining": str(remaining),
    }


async def test_over_budget_is_429_with_retry_after_and_details(hook):
    await run_hook(hook, make_request(max_tokens=700))
    with pytest.raises(HTTPException) as exc:
        await run_hook(hook, make_request(max_tokens=400))
    err = exc.value
    assert err.status_code == 429
    assert int(err.headers["Retry-After"]) >= 1
    assert err.headers["x-aegis-ratelimit-limit"] == "1000"
    assert err.detail["tenant"] == "support"
    assert err.detail["limit"] == 1000
    assert err.detail["used"] > 700
    assert "over its token budget" in err.detail["error"]


async def test_request_bigger_than_the_whole_budget_is_413(hook):
    with pytest.raises(HTTPException) as exc:
        await run_hook(hook, make_request(max_tokens=5000))
    assert exc.value.status_code == 413


async def test_success_records_actual_tokens_not_the_estimate(hook):
    first = make_request(max_tokens=800)
    await run_hook(hook, first)
    await report_success(hook, first, total_tokens=50)  # the model replied briefly
    # 800+ would not fit next to another 800 request, but 50 does.
    assert await run_hook(hook, make_request(max_tokens=800))


async def test_failed_call_releases_its_reservation(hook):
    first = make_request(max_tokens=800)
    await run_hook(hook, first)
    await hook.async_post_call_failure_hook(first, RuntimeError("provider down"), None)
    assert await run_hook(hook, make_request(max_tokens=800))


async def test_one_tenant_over_budget_does_not_block_another(hook):
    await run_hook(hook, make_request(tenant="support", max_tokens=800))
    with pytest.raises(HTTPException):
        await run_hook(hook, make_request(tenant="support", max_tokens=800))
    assert await run_hook(hook, make_request(tenant="engineering", max_tokens=800))


@pytest.fixture
def dead_redis_limiter():
    return SlidingWindowLimiter(Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5))


async def test_redis_down_fail_open_allows_and_warns(dead_redis_limiter, tenants_file, ratelimit_logs):
    hook = BudgetHook(limiter=dead_redis_limiter, tenants_file=tenants_file, on_redis_error=FAIL_OPEN)
    data = make_request()
    assert await run_hook(hook, data) is data
    assert ratelimit_logs == [("WARNING", "ratelimit_unavailable tenant=support error=ConnectionError action=allow")]
    assert await response_headers(hook, data) == {
        "x-aegis-ratelimit-limit": "1000",
        "x-aegis-ratelimit-remaining": "unknown",
    }


async def test_redis_down_fail_closed_rejects(dead_redis_limiter, tenants_file, ratelimit_logs):
    hook = BudgetHook(limiter=dead_redis_limiter, tenants_file=tenants_file, on_redis_error=FAIL_CLOSED)
    with pytest.raises(HTTPException) as exc:
        await run_hook(hook, make_request())
    assert exc.value.status_code == 503
    assert ratelimit_logs == [("ERROR", "ratelimit_unavailable tenant=support error=ConnectionError action=reject")]


def test_hooks_run_in_the_right_order():
    config = yaml.safe_load((DEFAULT_TENANTS_FILE.parent / "config.yaml").read_text())
    assert config["litellm_settings"]["callbacks"] == [
        "tenant_hook.proxy_handler_instance",
        "pii_hook.proxy_handler_instance",
        "budget_hook.proxy_handler_instance",
        "failover_hook.proxy_handler_instance",
        "telemetry_hook.proxy_handler_instance",
    ]
