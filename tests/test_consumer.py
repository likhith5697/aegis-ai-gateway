"""Tests for telemetry/consumer.py against the real Redis and InfluxDB containers.

Each test gets its own stream key and its own temporary InfluxDB bucket, so the
real "usage" bucket stays clean.
"""

import asyncio
import json
import os
import time
import uuid

import pytest
from influxdb_client import InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
from redis import Redis
from redis.asyncio import Redis as AsyncRedis

from consumer import Consumer, to_point
from telemetry import RedisStreamSink

REDIS_URL = os.environ.get("AEGIS_TEST_REDIS_URL", "redis://redis:6379/15")
INFLUX_ORG = os.environ.get("INFLUX_ORG", "aegis")


@pytest.fixture(scope="module")
def influx():
    client = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=INFLUX_ORG)
    yield client
    client.close()


@pytest.fixture
def bucket(influx):
    created = influx.buckets_api().create_bucket(bucket_name=f"aegis_test_{uuid.uuid4().hex[:8]}", org=INFLUX_ORG)
    yield created.name
    influx.buckets_api().delete_bucket(created)


@pytest.fixture
def redis():
    client = Redis.from_url(REDIS_URL)
    yield client
    client.close()


@pytest.fixture
def stream(redis):
    key = f"test:usage:{uuid.uuid4().hex}"
    yield key
    redis.delete(key, f"{key}:dead")


def make_consumer(redis, influx, bucket, stream, name="writer-1", **kwargs):
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    consumer = Consumer(redis, lambda points: write_api.write(bucket=bucket, record=points), stream=stream, name=name, **kwargs)
    consumer.ensure_group()
    return consumer


def event(tenant="support", n=0, **overrides):
    """A usage event as the gateway's telemetry hook produces it."""
    return {
        "schema": 1, "timestamp_ns": time.time_ns() + n, "request_id": f"req-{uuid.uuid4().hex[:8]}",
        "gateway": "test", "tenant": tenant, "requested_model": "claude-opus-5-5", "model_used": "claude-sonnet-5-5",
        "fallback": True, "prompt_tokens": 100, "completion_tokens": 40, "cost_usd": 0.0006, "latency_ms": 812.5,
        "status_code": 200, "pii_redactions": 1, "rate_limited": False, **overrides,
    }


def publish(redis, stream, events):
    for e in events:
        redis.xadd(stream, {"event": json.dumps(e)})


def points_in(influx, bucket) -> list[dict]:
    """One row per stored point: its tags plus the cost and total_tokens fields."""
    query = f'''
        from(bucket: "{bucket}") |> range(start: -1h)
          |> filter(fn: (r) => r._measurement == "llm_usage")
          |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''
    tables = influx.query_api().query(query, org=INFLUX_ORG)
    return [record.values for table in tables for record in table.records]


def pending(redis, stream) -> int:
    return redis.xpending(stream, "influx-writer")["pending"]


# --- writing ---

def test_consumer_writes_events_to_influxdb(redis, influx, bucket, stream):
    publish(redis, stream, [event("support", 0), event("engineering", 1), event("marketing", 2)])
    consumer = make_consumer(redis, influx, bucket, stream)

    assert consumer.run_once(block_ms=100) == 3
    rows = points_in(influx, bucket)
    assert sorted(r["tenant"] for r in rows) == ["engineering", "marketing", "support"]
    row = rows[0]
    assert (row["requested_model"], row["model_used"], row["fallback"], row["status_code"], row["rate_limited"]) == (
        "claude-opus-5-5", "claude-sonnet-5-5", "true", "200", "false")
    assert (row["cost_usd"], row["total_tokens"], row["latency_ms"]) == (0.0006, 140, 812.5)
    assert pending(redis, stream) == 0  # all acked


def test_events_from_the_gateway_sink_are_consumed(redis, influx, bucket, stream):
    async def publish_like_the_gateway():
        client = AsyncRedis.from_url(REDIS_URL)
        await RedisStreamSink(client, stream=stream).publish([event("support")])
        await client.aclose()

    asyncio.run(publish_like_the_gateway())
    assert make_consumer(redis, influx, bucket, stream).run_once(block_ms=100) == 1
    assert len(points_in(influx, bucket)) == 1


# --- restarts: nothing lost, nothing counted twice ---

def test_crash_before_ack_is_redelivered_without_double_counting(redis, influx, bucket, stream):
    publish(redis, stream, [event("support", 0), event("marketing", 1)])
    first = make_consumer(redis, influx, bucket, stream)
    entries = first._entries(redis.xreadgroup("influx-writer", "writer-1", {stream: ">"}, count=10))
    first.handle(entries, ack=False)  # wrote to InfluxDB, then "crashed" before XACK
    assert len(points_in(influx, bucket)) == 2
    assert pending(redis, stream) == 2  # Redis still considers them unfinished

    restarted = make_consumer(redis, influx, bucket, stream)  # same consumer name after a restart
    assert restarted.run_once(block_ms=100) == 2  # re-read its own pending entries and wrote them again
    assert pending(redis, stream) == 0
    assert len(points_in(influx, bucket)) == 2  # same tags + timestamp: overwritten, not doubled


def test_pending_entries_of_a_dead_consumer_are_taken_over(redis, influx, bucket, stream):
    publish(redis, stream, [event("support")])
    survivor = make_consumer(redis, influx, bucket, stream, name="writer-new", claim_idle_ms=0)  # creates the group
    redis.xreadgroup("influx-writer", "writer-old", {stream: ">"}, count=10)  # another consumer read it, then died
    assert pending(redis, stream) == 1

    assert survivor.run_once(block_ms=100) == 1  # XAUTOCLAIM took the entry over
    assert pending(redis, stream) == 0
    assert len(points_in(influx, bucket)) == 1


def test_influxdb_outage_leaves_events_pending_until_it_is_back(redis, influx, bucket, stream):
    publish(redis, stream, [event("support", 0), event("support", 1)])

    def influx_down(points):
        raise ConnectionError("influxdb unreachable")

    broken = Consumer(redis, influx_down, stream=stream)
    broken.ensure_group()
    assert broken.run_once(block_ms=100) == 0
    assert broken.write_failed
    assert pending(redis, stream) == 2  # not acked, so not lost

    assert make_consumer(redis, influx, bucket, stream).run_once(block_ms=100) == 2
    assert pending(redis, stream) == 0
    assert len(points_in(influx, bucket)) == 2


def test_unreadable_event_is_dead_lettered_not_retried_forever(redis, influx, bucket, stream):
    redis.xadd(stream, {"event": "this is not json"})
    publish(redis, stream, [event("support")])

    assert make_consumer(redis, influx, bucket, stream).run_once(block_ms=100) == 1  # the good one still lands
    assert pending(redis, stream) == 0
    assert redis.xlen(f"{stream}:dead") == 1


# --- layout ---

def test_tags_are_labels_fields_are_numbers_and_request_id():
    line = to_point(event("support", request_id="req-abc")).to_line_protocol()
    tags, fields, _ = line.split(" ")
    assert tags.startswith("llm_usage,")
    for tag in ("gateway=test", "tenant=support", "model_used=claude-sonnet-5-5", "fallback=true", "status_code=200"):
        assert tag in tags
    assert "request_id" not in tags            # unique per request: a tag would create a series per request
    assert 'request_id="req-abc"' in fields
    assert "cost_usd=0.0006" in fields and "total_tokens=140i" in fields
