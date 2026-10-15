"""Sliding-window token limiter backed by Redis. Knows nothing about LiteLLM.

Each tenant has two Redis keys:
  aegis:tpm:{tenant}:log     sorted set  member = request id, score = when it started (ms)
  aegis:tpm:{tenant}:tokens  hash        field  = request id, value = its tokens
"Tokens used in the last minute" = the sum of tokens of requests that started
less than a window ago. Older entries are dropped as the window slides.

Every check-and-update runs as one Lua script. Redis runs a script start to
finish without running any other command in between, so two requests can never
both read "there's room for one more" and both get in.
"""

import math
from dataclasses import dataclass

from redis.asyncio import Redis

# KEYS: log, tokens.  ARGV: window ms, limit, cost of this request, request id
RESERVE = """
local log, tok = KEYS[1], KEYS[2]
local window, limit, cost, id = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), ARGV[4]

-- Redis's own clock, so every gateway instance agrees on "now"
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- 1. Slide the window: forget requests that started more than a window ago
for _, old in ipairs(redis.call('ZRANGEBYSCORE', log, '-inf', now - window)) do
  redis.call('HDEL', tok, old)
end
redis.call('ZREMRANGEBYSCORE', log, '-inf', now - window)

-- 2. Add up the tokens still inside the window
local used = 0
for _, v in ipairs(redis.call('HVALS', tok)) do used = used + tonumber(v) end

-- 3. Over the limit? Reject, and work out when enough old tokens slide out
if used + cost > limit then
  local need, freed, retry = used + cost - limit, 0, window
  local entries = redis.call('ZRANGE', log, 0, -1, 'WITHSCORES')  -- oldest first
  for i = 1, #entries, 2 do
    freed = freed + tonumber(redis.call('HGET', tok, entries[i]) or 0)
    if freed >= need then
      retry = tonumber(entries[i + 1]) + window - now
      break
    end
  end
  return {0, used, retry}
end

-- 4. Fits: record this request; idle tenants' keys vanish after one window
redis.call('ZADD', log, now, id)
redis.call('HSET', tok, id, cost)
redis.call('PEXPIRE', log, window)
redis.call('PEXPIRE', tok, window)
return {1, used + cost, 0}
"""

# KEYS: tokens.  ARGV: request id, actual tokens. Only updates a request still in the window.
RECORD = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
  return 1
end
return 0
"""

# KEYS: log, tokens.  ARGV: request id
RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
return redis.call('HDEL', KEYS[2], ARGV[1])
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    used: int            # tokens in the window; includes this request if it was allowed
    limit: int
    retry_after_ms: int  # 0 when allowed

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def retry_after_seconds(self) -> int:
        """For the Retry-After header: whole seconds, at least 1."""
        return max(1, math.ceil(self.retry_after_ms / 1000))


class SlidingWindowLimiter:
    def __init__(self, redis: Redis, window_seconds: float = 60, prefix: str = "aegis:tpm"):
        self.window_ms = int(window_seconds * 1000)
        self.prefix = prefix
        self._reserve = redis.register_script(RESERVE)
        self._record = redis.register_script(RECORD)
        self._release = redis.register_script(RELEASE)

    def _keys(self, tenant: str) -> list[str]:
        # {tenant} braces keep both keys on the same node if this ever runs on Redis Cluster
        return [f"{self.prefix}:{{{tenant}}}:log", f"{self.prefix}:{{{tenant}}}:tokens"]

    async def reserve(self, tenant: str, request_id: str, tokens: int, limit: int) -> Decision:
        """Atomically check the window and, if the request fits, count its tokens."""
        allowed, used, retry_ms = await self._reserve(
            keys=self._keys(tenant), args=[self.window_ms, limit, tokens, request_id]
        )
        return Decision(bool(allowed), int(used), limit, int(retry_ms))

    async def record(self, tenant: str, request_id: str, tokens: int) -> bool:
        """Replace a reservation's estimate with the real count. False if it already slid out."""
        return bool(await self._record(keys=self._keys(tenant)[1:], args=[request_id, tokens]))

    async def release(self, tenant: str, request_id: str) -> None:
        """Give a reservation's tokens back (the model call failed, nothing was used)."""
        await self._release(keys=self._keys(tenant), args=[request_id])
