"""Tests for gateway/telemetry.py and gateway/telemetry_hook.py (the gateway side of usage telemetry).

Uses the real Redis container (DB 15) where a stream is involved.
"""

import asyncio
import logging
import os
import time
import uuid
from datetime import datetime
from types import SimpleNamespace

import litellm
import pytest
from fastapi import HTTPException
from redis.asyncio import Redis

from telemetry import EventSink, RedisStreamSink, TelemetryEmitter
from telemetry_hook import REQUESTED_MODEL_KEY, TelemetryHook
from tenants import DEFAULT_TENANTS_FILE

pytestmark = pytest.mark.anyio
REDIS_URL = os.environ.get("AEGIS_TEST_REDIS_URL", "redis://redis:6379/15")
PROMPT_SECRET = "SECRET-PROMPT-7f3a my SSN is 123-45-6789"
REPLY_SECRET = "SECRET-REPLY-91bc"
EVENT_FIELDS = {
    "schema", "timestamp_ns", "request_id", "gateway", "tenant", "requested_model", "model_used", "fallback",
    "prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "status_code", "pii_redactions", "rate_limited",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class ListSink(EventSink):
    def __init__(self):
        self.events = []

    async def publish(self, events):
        self.events.extend(events)


@pytest.fixture
def sink():
    return ListSink()


@pytest.fixture
def hook(sink):
    return TelemetryHook(emitter=TelemetryEmitter(sink), tenants_file=DEFAULT_TENANTS_FILE, gateway="test")


@pytest.fixture
def telemetry_logs():
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = ListHandler()
    logger = logging.getLogger("aegis.telemetry")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def success_kwargs(tenant="engineering", requested="claude-opus-5-5", used="claude-opus-5-5", cost=0.0012):
    """Shaped like what LiteLLM passes to async_log_success_event, prompt and reply text included."""
    messages = [{"role": "user", "content": PROMPT_SECRET}]
    return {
        "litellm_call_id": "call-123",
        "messages": messages,
        "proxy_server_request": {
            "headers": {"tenant-id": tenant},
            "arrival_time": time.time() - 0.25,
            "body": {"messages": messages},
        },
        "litellm_params": {"metadata": {REQUESTED_MODEL_KEY: requested, "aegis_pii_redactions": {"SSN": 1}}},
        "standard_logging_object": {
            "id": "call-123",
            "model_group": used,
            "response_cost": cost,
            "prompt_tokens": 100,
            "completion_tokens": 40,
            "messages": messages,
            "response": {"choices": [{"message": {"content": REPLY_SECRET}}]},
        },
    }


def failure_data(tenant="marketing", model="claude-sonnet-5-5"):
    """Shaped like the request_data LiteLLM passes to async_post_call_failure_hook."""
    headers = {"tenant-id": tenant} if tenant else {}
    return {
        "litellm_call_id": "call-456",
        "model": model,
        "messages": [{"role": "user", "content": PROMPT_SECRET}],
        "proxy_server_request": {"headers": headers, "arrival_time": time.time() - 0.01},
        "metadata": {"aegis_pii_redactions": {"EMAIL": 2}},
    }


REPLY = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=REPLY_SECRET))])


async def emit_success(hook, **kwargs):
    await hook.async_log_success_event(success_kwargs(**kwargs), REPLY, datetime.now(), datetime.now())
    assert await hook.emitter.flush()


async def emit_failure(hook, error, **kwargs):
    await hook.async_post_call_failure_hook(failure_data(**kwargs), error, None)
    assert await hook.emitter.flush()


# --- the event ---

async def test_success_event_has_the_expected_fields(hook, sink):
    await emit_success(hook)
    [event] = sink.events
    assert set(event) == EVENT_FIELDS
    assert {k: event[k] for k in EVENT_FIELDS - {"timestamp_ns", "latency_ms"}} == {
        "schema": 1,
        "request_id": "call-123",
        "gateway": "test",
        "tenant": "engineering",
        "requested_model": "claude-opus-5-5",
        "model_used": "claude-opus-5-5",
        "fallback": False,
        "prompt_tokens": 100,
        "completion_tokens": 40,
        "cost_usd": 0.0012,
        "status_code": 200,
        "pii_redactions": 1,
        "rate_limited": False,
    }
    assert 200 <= event["latency_ms"] < 2000
    assert abs(event["timestamp_ns"] - time.time_ns()) < 5e9


async def test_fallback_is_detected_from_the_model_that_answered(hook, sink):
    await emit_success(hook, requested="claude-opus-5-5", used="claude-sonnet-5-5")
    assert sink.events[0]["fallback"] is True
    assert sink.events[0]["model_used"] == "claude-sonnet-5-5"


async def test_budget_rejection_is_an_event_marked_rate_limited(hook, sink):
    await emit_failure(hook, HTTPException(status_code=429, detail={"error": "over budget"}))
    [event] = sink.events
    assert set(event) == EVENT_FIELDS
    assert (event["status_code"], event["rate_limited"], event["model_used"]) == (429, True, "none")
    assert (event["prompt_tokens"], event["completion_tokens"], event["cost_usd"]) == (0, 0, 0.0)
    assert (event["tenant"], event["pii_redactions"], event["request_id"]) == ("marketing", 2, "call-456")


async def test_provider_429_is_not_our_rate_limit(hook, sink):
    await emit_failure(hook, litellm.RateLimitError(message="slow down", llm_provider="anthropic", model="m"))
    assert (sink.events[0]["status_code"], sink.events[0]["rate_limited"]) == (429, False)


async def test_unknown_tenants_and_models_are_bucketed(hook, sink):
    await emit_failure(hook, HTTPException(status_code=403), tenant="made-up-tenant-8812", model="gpt-random")
    await emit_failure(hook, HTTPException(status_code=400), tenant=None)
    assert [(e["tenant"], e["requested_model"]) for e in sink.events] == [
        ("unknown", "other"),
        ("unknown", "claude-sonnet-5-5"),
    ]


async def test_unpriced_model_has_no_cost_and_warns_once(hook, sink, telemetry_logs):
    await emit_success(hook, cost=None)
    await emit_success(hook, cost=None)
    assert [e["cost_usd"] for e in sink.events] == [None, None]
    assert [line for line in telemetry_logs if "cost_unknown" in line] == [
        "telemetry_cost_unknown model=claude-opus-5-5 (not in LiteLLM's price list; set custom prices)"
    ]


# --- never any text ---

@pytest.fixture
async def redis():
    client = Redis.from_url(REDIS_URL)
    yield client
    await client.aclose()


async def test_no_prompt_or_response_text_reaches_the_stream(redis):
    stream = f"test:usage:{uuid.uuid4().hex}"
    hook = TelemetryHook(emitter=TelemetryEmitter(RedisStreamSink(redis, stream=stream)), gateway="test")
    await hook.async_log_success_event(success_kwargs(), REPLY, datetime.now(), datetime.now())
    await hook.async_post_call_failure_hook(failure_data(), HTTPException(status_code=429), None)
    assert await hook.emitter.flush()

    raw = b"".join(value for _, fields in await redis.xrange(stream) for value in fields.values())
    await redis.delete(stream)
    assert raw.count(b'"request_id"') == 2  # both events made it
    for secret in (PROMPT_SECRET, REPLY_SECRET, "SECRET", "123-45-6789"):
        assert secret.encode() not in raw


# --- a telemetry outage never hurts the request ---

async def test_telemetry_outage_does_not_fail_or_slow_the_request(telemetry_logs):
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    hook = TelemetryHook(emitter=TelemetryEmitter(RedisStreamSink(dead)), gateway="test")

    started = time.monotonic()
    await hook.async_log_success_event(success_kwargs(), REPLY, datetime.now(), datetime.now())  # no exception
    await hook.async_post_call_failure_hook(failure_data(), HTTPException(status_code=403), None)
    assert time.monotonic() - started < 0.05  # handing the event over doesn't wait for Redis

    await asyncio.sleep(0.5)  # let the background task try and fail
    assert len(hook.emitter.buffer) == 2  # kept, not lost
    assert any(line.startswith("telemetry_sink_unavailable error=ConnectionError") for line in telemetry_logs)
    hook.emitter._task.cancel()


async def test_buffered_events_are_delivered_once_the_sink_recovers(telemetry_logs):
    class FlakySink(ListSink):
        failures_left = 2

        async def publish(self, events):
            if self.failures_left:
                self.failures_left -= 1
                raise ConnectionError("down")
            await super().publish(events)

    sink = FlakySink()
    emitter = TelemetryEmitter(sink, max_backoff=0.1)
    for i in range(3):
        emitter.emit({"n": i})
    assert await emitter.flush(timeout=10)
    assert sink.events == [{"n": 0}, {"n": 1}, {"n": 2}]  # all of them, in order
    assert [line.split()[0] for line in telemetry_logs] == ["telemetry_sink_unavailable", "telemetry_sink_recovered"]


async def test_full_buffer_drops_the_oldest_events(telemetry_logs):
    emitter = TelemetryEmitter(ListSink(), buffer_size=3)
    for i in range(5):  # no await in between, so the background task hasn't run yet
        emitter.emit({"n": i})
    assert [e["n"] for e in emitter.buffer] == [2, 3, 4]
    assert emitter.dropped == 2
    assert telemetry_logs == ["telemetry_buffer_full dropped_total=1 buffer_size=3"]
    emitter._task.cancel()


async def test_a_bug_in_event_building_never_breaks_the_request(hook, sink, telemetry_logs):
    await hook.async_log_success_event({"standard_logging_object": "not a dict"}, None, None, None)
    assert sink.events == []
    assert telemetry_logs == ["telemetry_event_failed outcome=success error=AttributeError"]
