"""Aegis tenant check, as a LiteLLM pre-call hook.

LiteLLM runs async_pre_call_hook on every LLM request, after the master key is
checked and before anything is sent to the model provider. We read the
Tenant-ID header, look the tenant up in tenants.yaml, and reject the request if
the tenant is missing, unknown, or asking for a model it may not use.
"""

from collections.abc import Callable
from pathlib import Path

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from failover import configured_fallbacks
from tenants import DEFAULT_TENANTS_FILE, get_tenant_id, load_tenants

# Fallback settings a client could send to steer failover; LiteLLM honors them if present.
CLIENT_FALLBACK_KEYS = ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks")


class TenantHook(CustomLogger):
    def __init__(
        self,
        tenants_file: Path = DEFAULT_TENANTS_FILE,
        fallbacks_for: Callable[[str], list[str]] = configured_fallbacks,
    ):
        super().__init__()
        self.tenants = load_tenants(tenants_file)
        self.fallbacks_for = fallbacks_for

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        tenant_id = get_tenant_id(data)

        if not tenant_id:
            raise HTTPException(
                status_code=400,
                detail={"error": "Missing required header: Tenant-ID"},
            )

        tenant = self.tenants.get(tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=403,
                detail={"error": f"Unknown tenant: '{tenant_id}'"},
            )

        model = data.get("model")
        if model not in tenant["allowed_models"]:
            raise HTTPException(
                status_code=403,
                detail={"error": f"Tenant '{tenant_id}' is not allowed to use model '{model}'"},
            )

        # Fallbacks: only configured ones this tenant may also use. Always overwrite what the
        # client sent; otherwise a client could name its own fallback and dodge allowed_models.
        for key in CLIENT_FALLBACK_KEYS:
            data.pop(key, None)
        allowed_fallbacks = [m for m in self.fallbacks_for(model) if m in tenant["allowed_models"]]
        data["fallbacks"] = [{model: allowed_fallbacks}] if allowed_fallbacks else []

        return data


# config.yaml points LiteLLM at this object: callbacks: tenant_hook.proxy_handler_instance
proxy_handler_instance = TenantHook()
