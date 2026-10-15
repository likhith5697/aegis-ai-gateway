"""Tests for gateway/tenant_hook.py. They call the hook directly: no server, no API calls."""

import asyncio
from types import MappingProxyType

import pytest
from fastapi import HTTPException

from tenant_hook import TenantHook
from tenants import DEFAULT_TENANTS_FILE, load_tenants

TEST_TENANTS = """
tenants:
  support:
    allowed_models: [cheap-model]
    tpm_quota: 50000
  engineering:
    allowed_models: [cheap-model, big-model]
    tpm_quota: 100000
"""


CONFIGURED_FALLBACKS = {"big-model": ["cheap-model"], "cheap-model": ["big-model"]}


@pytest.fixture
def hook(tmp_path):
    tenants_file = tmp_path / "tenants.yaml"
    tenants_file.write_text(TEST_TENANTS)
    return TenantHook(tenants_file, fallbacks_for=lambda model: CONFIGURED_FALLBACKS.get(model, []))


def make_request(headers, model="cheap-model"):
    """Shape a request the way LiteLLM hands it to the hook."""
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hi"}],
        "proxy_server_request": {"headers": headers},
    }


def run_hook(hook, data):
    return asyncio.run(
        hook.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type="completion")
    )


# --- allowed requests ---

def test_allowed_request_passes_through_unchanged(hook):
    data = make_request({"tenant-id": "support"})
    assert run_hook(hook, data) is data


def test_header_name_is_case_insensitive(hook):
    data = make_request({"Tenant-ID": "engineering"}, model="big-model")
    assert run_hook(hook, data) is data


def test_accepts_read_only_headers_like_litellm_sends(hook):
    data = make_request(MappingProxyType({"tenant-id": "support"}))
    assert run_hook(hook, data) is data


# --- rejections ---

@pytest.mark.parametrize("headers", [{}, {"tenant-id": ""}, {"tenant-id": "   "}])
def test_missing_tenant_is_400(hook, headers):
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, make_request(headers))
    assert exc.value.status_code == 400
    assert "Tenant-ID" in exc.value.detail["error"]


def test_missing_request_info_is_400(hook):
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, {"model": "cheap-model"})
    assert exc.value.status_code == 400


def test_unknown_tenant_is_403(hook):
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, make_request({"tenant-id": "sales"}))
    assert exc.value.status_code == 403
    assert "Unknown tenant" in exc.value.detail["error"]


def test_tenant_ids_are_case_sensitive(hook):
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, make_request({"tenant-id": "Support"}))
    assert exc.value.status_code == 403


def test_disallowed_model_is_403(hook):
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, make_request({"tenant-id": "support"}, model="big-model"))
    assert exc.value.status_code == 403
    assert "not allowed to use model 'big-model'" in exc.value.detail["error"]


# --- fallbacks: only to models the tenant may use ---

def test_fallback_allowed_for_tenant_is_kept(hook):
    data = make_request({"tenant-id": "engineering"}, model="big-model")
    run_hook(hook, data)
    assert data["fallbacks"] == [{"big-model": ["cheap-model"]}]


def test_fallback_the_tenant_may_not_use_is_dropped(hook):
    # support may only use cheap-model, so its configured fallback (big-model) is off-limits.
    data = make_request({"tenant-id": "support"}, model="cheap-model")
    run_hook(hook, data)
    assert data["fallbacks"] == []


def test_client_cannot_choose_its_own_fallbacks(hook):
    data = make_request({"tenant-id": "support"}, model="cheap-model")
    data["fallbacks"] = ["big-model"]                        # would dodge allowed_models
    data["content_policy_fallbacks"] = [{"cheap-model": ["big-model"]}]
    run_hook(hook, data)
    assert data["fallbacks"] == []
    assert "content_policy_fallbacks" not in data


# --- loading tenants.yaml ---

def test_real_tenants_file_matches_spec():
    tenants = load_tenants(DEFAULT_TENANTS_FILE)
    quotas = {name: t["tpm_quota"] for name, t in tenants.items()}
    assert quotas == {"support": 50000, "engineering": 100000, "marketing": 20000}


@pytest.mark.parametrize(
    "content",
    [
        "",                                                     # empty file
        "tenants: {}",                                          # no tenants
        "tenants:\n  support:\n    tpm_quota: 1",               # no allowed_models
        "tenants:\n  support:\n    allowed_models: [a]",        # no tpm_quota
        "tenants:\n  support:\n    allowed_models: [a]\n    tpm_quota: 1\n    pii_scrubbing: maybe",
    ],
)
def test_malformed_tenants_file_fails_at_startup(tmp_path, content):
    bad = tmp_path / "tenants.yaml"
    bad.write_text(content)
    with pytest.raises(ValueError):
        load_tenants(bad)
