"""Aegis usage telemetry, as a LiteLLM hook: exactly one usage event per request.

  success → async_log_success_event   (LiteLLM runs it in the background, after the response)
  failure → async_post_call_failure_hook (fires once for any failed request: our own
            400/403/413/429 rejections and provider errors)

Deliberately NOT async_log_failure_event: it also fires for the primary model's
failure inside a request that then succeeds on its fallback, so that request
would be counted twice.

Events are built field by field from a fixed list. Nothing is copied from the
request or response wholesale, so prompt and response text can never end up in one.
"""

import os
import time
from datetime import datetime

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger
from redis.asyncio import Redis

from pii_hook import RESULT_KEY as PII_RESULT_KEY
from settings import get_logger
from telemetry import RedisStreamSink, TelemetryEmitter
from tenants import DEFAULT_TENANTS_FILE, get_tenant_id, load_tenants

SCHEMA_VERSION = 1
REQUESTED_MODEL_KEY = "aegis_requested_model"  # stamped before the call; the answering model may differ
UNKNOWN_TENANT = "unknown"  # missing or not in tenants.yaml
OTHER_MODEL = "other"       # not in any tenant's allowed_models
NO_MODEL = "none"           # the request failed, no model answered

logger = get_logger("aegis.telemetry")


def default_emitter() -> TelemetryEmitter:
    redis = Redis.from_url(
        os.environ.get("AEGIS_TELEMETRY_REDIS_URL", "redis://redis:6379/0"),
        socket_connect_timeout=0.5,
        socket_timeout=0.5,
    )
    return TelemetryEmitter(RedisStreamSink(redis, stream=os.environ.get("AEGIS_TELEMETRY_STREAM", "aegis:usage")))


def proxy_request(source: dict) -> dict:
    """LiteLLM's record of the incoming HTTP request (headers, arrival time)."""
    return source.get("proxy_server_request") or (source.get("litellm_params") or {}).get("proxy_server_request") or {}


def pii_redactions(metadata: dict) -> int:
    result = metadata.get(PII_RESULT_KEY)
    return sum(result.values()) if isinstance(result, dict) else 0  # "disabled"/"unavailable"/missing → 0


def latency_ms(arrival: object, end_seconds: float) -> float | None:
    return round((end_seconds - arrival) * 1000, 1) if isinstance(arrival, (int, float)) else None


class TelemetryHook(CustomLogger):
    def __init__(self, emitter: TelemetryEmitter | None = None, tenants_file=DEFAULT_TENANTS_FILE, gateway: str | None = None):
        super().__init__()
        tenants = load_tenants(tenants_file)
        self.known_tenants = set(tenants)
        self.known_models = {m for t in tenants.values() for m in t["allowed_models"]}
        self.gateway = gateway or os.environ.get("AEGIS_GATEWAY_NAME", "main")
        self.emitter = emitter or default_emitter()
        self._warned_unpriced: set[str] = set()

    # --- hooks ---

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Never rejects. Remembers which model the client asked for, before any fallback."""
        data.setdefault("metadata", {})[REQUESTED_MODEL_KEY] = data.get("model")
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        try:
            self.emitter.emit(self.success_event(kwargs, end_time))
        except Exception as e:  # telemetry must never break a request
            logger.warning("telemetry_event_failed outcome=success error=%s", type(e).__name__)

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        try:
            self.emitter.emit(self.failure_event(request_data, original_exception))
        except Exception as e:
            logger.warning("telemetry_event_failed outcome=failure error=%s", type(e).__name__)

    # --- building events (pure: no I/O) ---

    def success_event(self, kwargs: dict, end_time: datetime | None = None) -> dict:
        slo = kwargs.get("standard_logging_object") or {}  # LiteLLM's own per-call summary
        metadata = (kwargs.get("litellm_params") or {}).get("metadata") or {}
        model_used = slo.get("model_group")
        requested = metadata.get(REQUESTED_MODEL_KEY) or model_used
        cost = slo.get("response_cost")
        if cost is None and model_used not in self._warned_unpriced:
            self._warned_unpriced.add(model_used)
            logger.warning("telemetry_cost_unknown model=%s (not in LiteLLM's price list; set custom prices)", model_used)
        end = end_time.timestamp() if isinstance(end_time, datetime) else time.time()
        return self._event(
            request_id=kwargs.get("litellm_call_id") or slo.get("id"),
            tenant=get_tenant_id({"proxy_server_request": proxy_request(kwargs)}),
            requested_model=requested,
            model_used=model_used,
            prompt_tokens=slo.get("prompt_tokens") or 0,
            completion_tokens=slo.get("completion_tokens") or 0,
            cost_usd=cost,
            latency_ms=latency_ms(proxy_request(kwargs).get("arrival_time"), end),
            status_code=200,
            pii_redactions=pii_redactions(metadata),
            rate_limited=False,
        )

    def failure_event(self, request_data: dict, error: Exception) -> dict:
        status = int(getattr(error, "status_code", None) or 500)
        return self._event(
            request_id=request_data.get("litellm_call_id"),
            tenant=get_tenant_id(request_data),
            requested_model=request_data.get("model"),
            model_used=None,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,  # failed calls aren't billed
            latency_ms=latency_ms(proxy_request(request_data).get("arrival_time"), time.time()),
            status_code=status,
            pii_redactions=pii_redactions(request_data.get("metadata") or {}),
            # Our own budget rejection, not a provider 429 (that would be a litellm.RateLimitError)
            rate_limited=isinstance(error, HTTPException) and status == 429,
        )

    def _event(self, *, request_id, tenant, requested_model, model_used, prompt_tokens, completion_tokens,
               cost_usd, latency_ms, status_code, pii_redactions, rate_limited) -> dict:
        requested = requested_model if requested_model in self.known_models else OTHER_MODEL
        used = NO_MODEL if model_used is None else (model_used if model_used in self.known_models else OTHER_MODEL)
        return {
            "schema": SCHEMA_VERSION,
            "timestamp_ns": time.time_ns(),
            "request_id": str(request_id or ""),
            "gateway": self.gateway,
            "tenant": tenant if tenant in self.known_tenants else UNKNOWN_TENANT,
            "requested_model": requested,
            "model_used": used,
            "fallback": used not in (NO_MODEL, requested),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cost_usd": None if cost_usd is None else float(cost_usd),
            "latency_ms": latency_ms,
            "status_code": status_code,
            "pii_redactions": int(pii_redactions),
            "rate_limited": bool(rate_limited),
        }


# config.yaml points LiteLLM at this object: callbacks: [..., telemetry_hook.proxy_handler_instance]
proxy_handler_instance = TelemetryHook()
