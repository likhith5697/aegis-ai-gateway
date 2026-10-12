"""Tenant config shared by every Aegis hook: load tenants.yaml and read the Tenant-ID header."""

from collections.abc import Mapping
from pathlib import Path

import yaml

# HTTP header names are case-insensitive, so we always compare in lowercase.
TENANT_HEADER = "tenant-id"
DEFAULT_TENANTS_FILE = Path(__file__).parent / "tenants.yaml"


def load_tenants(path: Path) -> dict[str, dict]:
    """Read tenants.yaml and fail loudly at startup if it is malformed."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    tenants = raw.get("tenants") if isinstance(raw, dict) else None
    if not isinstance(tenants, dict) or not tenants:
        raise ValueError(f"{path}: expected a non-empty 'tenants' mapping")

    for name, settings in tenants.items():
        if not isinstance(settings, dict) or not isinstance(settings.get("allowed_models"), list):
            raise ValueError(f"{path}: tenant '{name}' needs an 'allowed_models' list")
        if not isinstance(settings.get("tpm_quota"), int):
            raise ValueError(f"{path}: tenant '{name}' needs an integer 'tpm_quota'")
        if not isinstance(settings.get("pii_scrubbing", True), bool):
            raise ValueError(f"{path}: tenant '{name}' has a non-boolean 'pii_scrubbing'")

    return tenants


def pii_scrubbing_enabled(tenant: dict) -> bool:
    """PII scrubbing is on unless the tenant explicitly sets pii_scrubbing: false."""
    return tenant.get("pii_scrubbing", True)


def get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup. LiteLLM hands us a read-only mapping, not a dict."""
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def get_tenant_id(data: dict) -> str:
    """The Tenant-ID header of a LiteLLM request, stripped; "" if missing."""
    headers = data.get("proxy_server_request", {}).get("headers") or {}
    return (get_header(headers, TENANT_HEADER) or "").strip()
