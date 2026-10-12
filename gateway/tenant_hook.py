"""Aegis tenant check, as a LiteLLM pre-call hook.

LiteLLM runs async_pre_call_hook on every LLM request, after the master key is
checked and before anything is sent to the model provider. We read the
Tenant-ID header, look the tenant up in tenants.yaml, and reject the request if
the tenant is missing, unknown, or asking for a model it may not use.
"""

from pathlib import Path

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from tenants import DEFAULT_TENANTS_FILE, get_tenant_id, load_tenants


class TenantHook(CustomLogger):
    def __init__(self, tenants_file: Path = DEFAULT_TENANTS_FILE):
        super().__init__()
        self.tenants = load_tenants(tenants_file)

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

        return data


# config.yaml points LiteLLM at this object: callbacks: tenant_hook.proxy_handler_instance
proxy_handler_instance = TenantHook()
