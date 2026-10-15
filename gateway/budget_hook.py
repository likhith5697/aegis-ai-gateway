"""Aegis per-tenant token budgets, as a LiteLLM hook. Runs last: tenant check → PII scrub → budget.

Before the call: estimate the request's tokens and reserve them in the tenant's
sliding window (tpm_quota in tenants.yaml), or reject with 429 + Retry-After.
After the call: replace the estimate with the real count from the response, or
give the reservation back if the call failed.
"""

import os
import uuid
from pathlib import Path

import litellm
from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from ratelimit import SlidingWindowLimiter
from settings import FAIL_CLOSED, FAIL_OPEN, fail_mode, get_logger
from tenants import DEFAULT_TENANTS_FILE, get_tenant_id, load_tenants

# Where the pre-call hook leaves its result for the later hooks. Always overwritten.
RESULT_KEY = "aegis_ratelimit"
# Output tokens we assume when a request doesn't set max_tokens. Corrected after the call.
DEFAULT_OUTPUT_ESTIMATE = 1024
# What a broken or unreachable Redis raises (connection refused, timeout, ...).
REDIS_ERRORS = (RedisError, OSError)

logger = get_logger("aegis.ratelimit")


def estimate_tokens(data: dict) -> int:
    """Prompt tokens (counted locally, no network) + the most the reply may use."""
    input_tokens = litellm.token_counter(
        model=data.get("model") or "",
        messages=data.get("messages") or [],
        use_default_image_token_count=True,  # never download images just to count them
    )
    output_tokens = data.get("max_tokens") or data.get("max_completion_tokens") or DEFAULT_OUTPUT_ESTIMATE
    return input_tokens + int(output_tokens)


def default_limiter() -> SlidingWindowLimiter:
    redis = Redis.from_url(
        os.environ.get("AEGIS_REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=0.5,  # a dead Redis must not stall every request
        socket_timeout=0.5,
    )
    return SlidingWindowLimiter(redis)


class BudgetHook(CustomLogger):
    def __init__(
        self,
        limiter: SlidingWindowLimiter | None = None,
        tenants_file: Path = DEFAULT_TENANTS_FILE,
        on_redis_error: str | None = None,
    ):
        super().__init__()
        self.tenants = load_tenants(tenants_file)
        self.limiter = limiter or default_limiter()
        self.on_redis_error = on_redis_error or fail_mode("AEGIS_RATELIMIT_FAIL_MODE", default=FAIL_OPEN)

    # --- before the model call ---

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        tenant_id = get_tenant_id(data)
        tenant = self.tenants.get(tenant_id)
        if tenant is None:  # only possible if the hook order is wrong
            raise HTTPException(status_code=403, detail={"error": f"Unknown tenant: '{tenant_id}'"})

        limit = tenant["tpm_quota"]
        estimate = estimate_tokens(data)
        metadata = data.setdefault("metadata", {})

        if estimate > limit:  # could never fit, so "retry later" would be a lie
            raise HTTPException(
                status_code=413,
                detail={
                    "error": f"Request needs ~{estimate} tokens, more than tenant '{tenant_id}' "
                    f"may use per minute ({limit}). Lower max_tokens or shorten the prompt.",
                },
            )

        request_id = uuid.uuid4().hex
        try:
            decision = await self.limiter.reserve(tenant_id, request_id, estimate, limit)
        except REDIS_ERRORS as e:
            if self.on_redis_error == FAIL_CLOSED:
                logger.error("ratelimit_unavailable tenant=%s error=%s action=reject", tenant_id, type(e).__name__)
                raise HTTPException(status_code=503, detail={"error": "Rate limiter unavailable"})
            logger.warning("ratelimit_unavailable tenant=%s error=%s action=allow", tenant_id, type(e).__name__)
            metadata[RESULT_KEY] = {"limit": limit, "remaining": "unknown"}
            return data

        if not decision.allowed:
            seconds = decision.retry_after_seconds
            raise HTTPException(
                status_code=429,
                detail={
                    "error": f"Tenant '{tenant_id}' is over its token budget: {decision.used} of {limit} "
                    f"tokens used in the last minute, this request needs ~{estimate}. Retry in {seconds}s.",
                    "tenant": tenant_id,
                    "limit": limit,
                    "used": decision.used,
                    "requested": estimate,
                    "retry_after_seconds": seconds,
                },
                headers={
                    "Retry-After": str(seconds),
                    "x-aegis-ratelimit-limit": str(limit),
                    "x-aegis-ratelimit-remaining": str(decision.remaining),
                },
            )

        metadata[RESULT_KEY] = {
            "id": request_id,
            "tenant": tenant_id,
            "limit": limit,
            "remaining": decision.remaining,
        }
        return data

    # --- after the model call ---

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Fires once per successful call, streaming or not. Swap the estimate for real usage."""
        state = self._reservation(kwargs.get("litellm_params", {}).get("metadata"))
        usage = getattr(response_obj, "usage", None)
        if state is None or usage is None:
            return
        try:
            await self.limiter.record(state["tenant"], state["id"], int(usage.total_tokens))
        except REDIS_ERRORS as e:  # the estimate stays in the window; it expires on its own
            logger.warning("ratelimit_record_failed tenant=%s error=%s", state["tenant"], type(e).__name__)

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        """The call failed, so no tokens were used: give the reservation back."""
        state = self._reservation(request_data.get("metadata"))
        if state is None:  # rejected before we reserved anything
            return
        try:
            await self.limiter.release(state["tenant"], state["id"])
        except REDIS_ERRORS as e:
            logger.warning("ratelimit_release_failed tenant=%s error=%s", state["tenant"], type(e).__name__)

    async def async_post_call_response_headers_hook(
        self, data, user_api_key_dict, response, request_headers=None, litellm_call_info=None
    ):
        if response is None:  # failures: the 429 carries its own headers
            return None
        state = (data.get("metadata") or {}).get(RESULT_KEY)
        if not isinstance(state, dict):
            return None
        return {
            "x-aegis-ratelimit-limit": str(state["limit"]),
            "x-aegis-ratelimit-remaining": str(state["remaining"]),
        }

    @staticmethod
    def _reservation(metadata) -> dict | None:
        state = (metadata or {}).get(RESULT_KEY)
        if isinstance(state, dict) and "id" in state and "tenant" in state:
            return state
        return None


# config.yaml points LiteLLM at this object: callbacks: [..., budget_hook.proxy_handler_instance]
proxy_handler_instance = BudgetHook()
