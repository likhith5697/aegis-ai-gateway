"""Telemetry consumer: reads usage events from the Redis Stream and writes them to InfluxDB.

At-least-once delivery, without double counting:
  - Consumer group: Redis remembers which entries were handed to which consumer.
  - An entry is XACKed only after InfluxDB confirmed the write. Until then it is
    "pending" (delivered, not done).
  - After a crash or an InfluxDB outage, this consumer first re-reads its own
    pending entries (read id "0") and writes them again; entries left pending by a
    consumer that died are taken over with XAUTOCLAIM once idle long enough.
  - Writing an event twice is harmless: InfluxDB treats the same measurement +
    tags + timestamp as the same point and overwrites it.
  - Entries that can't be parsed go to a dead-letter stream and are acked, so
    one bad entry can't block the rest forever.
"""

import json
import logging
import os
import time

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from redis import Redis
from redis.exceptions import ResponseError

MEASUREMENT = "llm_usage"
# Tags: few distinct values, used to filter and group (indexed).
TAGS = ("gateway", "tenant", "requested_model", "model_used", "fallback", "status_code", "rate_limited")
# Fields: the numbers to aggregate, plus request_id (unique per request, so never a tag).
FIELDS = ("prompt_tokens", "completion_tokens", "cost_usd", "latency_ms", "pii_redactions", "request_id")

logging.basicConfig(level=logging.INFO, format="%(asctime)s telemetry-consumer %(levelname)s %(message)s")
log = logging.getLogger("consumer")


def to_point(event: dict) -> Point:
    """One usage event → one InfluxDB point. Raises KeyError/ValueError on a malformed event."""
    point = Point(MEASUREMENT)
    for tag in TAGS:
        value = event[tag]
        point = point.tag(tag, str(value).lower() if isinstance(value, bool) else str(value))
    for field in FIELDS:
        value = event.get(field)
        if value is None:  # e.g. cost unknown for an unpriced model: leave the field out
            continue
        if field in ("cost_usd", "latency_ms"):
            value = float(value)  # always float: InfluxDB rejects int/float mixes in one field
        point = point.field(field, value)
    point = point.field("total_tokens", int(event["prompt_tokens"]) + int(event["completion_tokens"]))
    return point.time(int(event["timestamp_ns"]), WritePrecision.NS)


class Consumer:
    def __init__(self, redis: Redis, influx_write, stream="aegis:usage", group="influx-writer",
                 name="writer-1", batch_size=100, claim_idle_ms=60_000):
        self.redis = redis
        self.write = influx_write  # callable(list[Point]); raises if InfluxDB didn't accept them
        self.stream, self.group, self.name = stream, group, name
        self.dead_letter = f"{stream}:dead"
        self.batch_size = batch_size
        self.claim_idle_ms = claim_idle_ms
        self.write_failed = False  # last write to InfluxDB failed: the main loop backs off

    def ensure_group(self) -> None:
        """Create the group once. id="0": also deliver events published before the group existed."""
        try:
            self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):  # already exists: fine
                raise

    def run_once(self, block_ms: int = 5000) -> int:
        """One round: retry own pending, take over stale pending, then read new. Returns events written."""
        written = 0
        mine = self.redis.xreadgroup(self.group, self.name, {self.stream: "0"}, count=self.batch_size)
        written += self.handle(self._entries(mine))
        if self.write_failed:  # InfluxDB is down: don't pull even more entries this round
            return written
        _, stale, _ = self.redis.xautoclaim(self.stream, self.group, self.name, self.claim_idle_ms, count=self.batch_size)
        written += self.handle(stale)
        if self.write_failed:
            return written
        new = self.redis.xreadgroup(self.group, self.name, {self.stream: ">"}, count=self.batch_size, block=block_ms)
        written += self.handle(self._entries(new))
        return written

    def handle(self, entries: list, ack: bool = True) -> int:
        """Write a batch, then ack it. If the write fails, nothing is acked: it stays pending for a retry."""
        if not entries:
            return 0
        points, done, broken = [], [], []
        for entry_id, fields in entries:
            if not fields:  # trimmed from the stream (MAXLEN) while pending: nothing to write, just ack
                done.append(entry_id)
                continue
            try:
                points.append(to_point(json.loads(fields[b"event"])))
                done.append(entry_id)
            except (KeyError, ValueError, TypeError) as e:
                broken.append((entry_id, fields, type(e).__name__))
        try:
            if points:
                self.write(points)
        except Exception as e:
            log.warning("influx_write_failed error=%s events=%d (left pending, will retry)", type(e).__name__, len(points))
            self.write_failed = True
            return 0
        self.write_failed = False
        for entry_id, fields, error in broken:
            self.redis.xadd(self.dead_letter, {**fields, b"error": error.encode()})
            log.warning("dead_lettered id=%s error=%s", entry_id.decode(), error)
        if ack and (done or broken):
            self.redis.xack(self.stream, self.group, *done, *(b[0] for b in broken))
        return len(points)

    @staticmethod
    def _entries(response) -> list:
        """XREADGROUP returns [[stream, [(id, fields), ...]]]; keep just the entries."""
        return [entry for _, entries in (response or []) for entry in entries]


def main() -> None:
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    influx = InfluxDBClient(url=os.environ["INFLUX_URL"], token=os.environ["INFLUX_TOKEN"], org=os.environ["INFLUX_ORG"])
    write_api = influx.write_api(write_options=SYNCHRONOUS)
    bucket = os.environ["INFLUX_BUCKET"]

    consumer = Consumer(
        redis,
        influx_write=lambda points: write_api.write(bucket=bucket, record=points),
        stream=os.environ.get("STREAM", "aegis:usage"),
        group=os.environ.get("GROUP", "influx-writer"),
        name=os.environ.get("CONSUMER_NAME", "writer-1"),  # stable name: after a restart it finds its own pending
    )
    log.info("starting stream=%s group=%s consumer=%s bucket=%s", consumer.stream, consumer.group, consumer.name, bucket)

    backoff = 1.0
    while True:
        try:
            consumer.ensure_group()
            written = consumer.run_once()
            if written:
                log.info("wrote events=%d", written)
            if not consumer.write_failed:
                backoff = 1.0
                continue
        except Exception as e:  # Redis down etc.: wait and try again, never exit
            log.warning("consumer_error error=%s retry_in=%.0fs", type(e).__name__, backoff)
        time.sleep(backoff)  # Redis or InfluxDB unavailable: back off before retrying
        backoff = min(backoff * 2, 30.0)


if __name__ == "__main__":
    main()
