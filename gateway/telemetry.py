"""Usage telemetry delivery: buffer events in memory, publish them in the background.

Knows nothing about LiteLLM. EventSink is the interface: today RedisStreamSink,
later e.g. a KinesisSink, without touching the hooks.

emit() is what the request path calls. It only appends to an in-memory buffer:
no network, no waiting, no exceptions. A background task drains the buffer into
the sink. If the sink is down, events wait in the buffer (bounded) and the task
retries with backoff; when the buffer is full the oldest events are dropped.
"""

import asyncio
import json
from abc import ABC, abstractmethod
from collections import deque

from redis.asyncio import Redis

from settings import get_logger

logger = get_logger("aegis.telemetry")


class EventSink(ABC):
    """Somewhere usage events go. publish() raises if they didn't get there."""

    @abstractmethod
    async def publish(self, events: list[dict]) -> None: ...


class RedisStreamSink(EventSink):
    """Appends each event to a Redis Stream as one JSON field."""

    def __init__(self, redis: Redis, stream: str = "aegis:usage", max_len: int = 100_000):
        self.redis = redis
        self.stream = stream
        self.max_len = max_len  # cap the stream so a stopped consumer can't fill Redis

    async def publish(self, events: list[dict]) -> None:
        pipe = self.redis.pipeline(transaction=False)  # one round trip for the whole batch
        for event in events:
            pipe.xadd(self.stream, {"event": json.dumps(event)}, maxlen=self.max_len, approximate=True)
        await pipe.execute()


class TelemetryEmitter:
    def __init__(self, sink: EventSink, buffer_size: int = 10_000, batch_size: int = 100, max_backoff: float = 30.0):
        self.sink = sink
        self.buffer_size = buffer_size
        self.batch_size = batch_size
        self.max_backoff = max_backoff
        self.buffer: deque[dict] = deque()
        self.dropped = 0
        self._wakeup = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._failing = False
        self._in_flight = 0  # events taken from the buffer but not yet confirmed by the sink

    def emit(self, event: dict) -> None:
        """Hand an event over. Never blocks, never raises. Must run inside the event loop."""
        if len(self.buffer) >= self.buffer_size:
            self.buffer.popleft()
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 1000 == 0:
                logger.warning("telemetry_buffer_full dropped_total=%d buffer_size=%d", self.dropped, self.buffer_size)
        self.buffer.append(event)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())
        self._wakeup.set()

    async def _run(self) -> None:
        backoff = 1.0
        while True:
            if not self.buffer:
                self._wakeup.clear()
                await self._wakeup.wait()
                continue
            batch = [self.buffer.popleft() for _ in range(min(self.batch_size, len(self.buffer)))]
            self._in_flight = len(batch)
            try:
                await self.sink.publish(batch)
            except Exception as e:  # any sink failure: keep the events, retry later
                self._requeue(batch)
                self._in_flight = 0
                if not self._failing:
                    logger.warning("telemetry_sink_unavailable error=%s buffered=%d", type(e).__name__, len(self.buffer))
                    self._failing = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.max_backoff)
                continue
            self._in_flight = 0
            if self._failing:
                logger.warning("telemetry_sink_recovered buffered=%d", len(self.buffer))
                self._failing = False
            backoff = 1.0

    def _requeue(self, batch: list[dict]) -> None:
        """Put a failed batch back at the front, still respecting the buffer limit."""
        for event in reversed(batch):
            if len(self.buffer) >= self.buffer_size:
                self.dropped += 1
                continue
            self.buffer.appendleft(event)

    async def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every event reached the sink (tests, shutdown). True if it did in time."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self.buffer or self._in_flight:
            if asyncio.get_running_loop().time() > deadline:
                return False
            await asyncio.sleep(0.05)
        return True
