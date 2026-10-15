"""Aegis failover visibility, as a LiteLLM hook. Runs last: tenant → PII → budget → failover.

LiteLLM's router does the failover itself (fallbacks, retries, timeouts and
cooldowns from config.yaml's router_settings). This hook only:
  - installs our "which errors may fail over" policy (gateway/failover.py)
  - logs every failover: tenant, primary, error type, fallback, latency; never the prompt
  - adds x-aegis-model-used / x-aegis-fallback response headers
"""

import time

from litellm.integrations.custom_logger import CustomLogger

from failover import install_failover_policy
from settings import get_logger
from tenants import get_tenant_id

# Stamped by our pre-call hook; LiteLLM copies metadata onto each fallback attempt.
STARTED_KEY = "aegis_started_at"
TENANT_KEY = "aegis_tenant"

logger = get_logger("aegis.failover")
install_failover_policy()  # before the first request, so no request can skip it


class FailoverHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Never rejects. Remembers when the request started and which (already checked) tenant sent it."""
        metadata = data.setdefault("metadata", {})
        metadata[STARTED_KEY] = time.monotonic()
        metadata[TENANT_KEY] = get_tenant_id(data)
        return data

    async def log_success_fallback_event(self, original_model_group, kwargs, original_exception):
        self._log_failover(original_model_group, kwargs, original_exception, outcome="answered")

    async def log_failure_fallback_event(self, original_model_group, kwargs, original_exception):
        self._log_failover(original_model_group, kwargs, original_exception, outcome="failed")

    def _log_failover(self, primary, kwargs, error, outcome):
        metadata = kwargs.get("metadata") or kwargs.get("litellm_metadata") or {}
        started = metadata.get(STARTED_KEY)
        latency = f"{(time.monotonic() - started) * 1000:.0f}" if isinstance(started, (int, float)) else "unknown"
        error_type = "cooldown" if type(error).__name__ == "RouterRateLimitError" else type(error).__name__
        logger.warning(
            "failover tenant=%s primary=%s error=%s fallback=%s outcome=%s latency_ms=%s",
            metadata.get(TENANT_KEY, "unknown"), primary, error_type, kwargs.get("model"), outcome, latency,
        )

    async def async_post_call_response_headers_hook(
        self, data, user_api_key_dict, response, request_headers=None, litellm_call_info=None
    ):
        if response is None:
            return None
        # LiteLLM already records which model group answered and how many fallbacks it took.
        litellm_headers = (getattr(response, "_hidden_params", None) or {}).get("additional_headers") or {}
        model_used = litellm_headers.get("x-litellm-model-group") or data.get("model")
        fell_back = int(litellm_headers.get("x-litellm-attempted-fallbacks") or 0) > 0
        return {
            "x-aegis-model-used": str(model_used),
            "x-aegis-fallback": "true" if fell_back else "false",
        }


# config.yaml points LiteLLM at this object: callbacks: [..., failover_hook.proxy_handler_instance]
proxy_handler_instance = FailoverHook()
