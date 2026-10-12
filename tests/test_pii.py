"""Tests for gateway/pii.py and gateway/pii_hook.py. They call the code directly: no server, no API calls."""

import asyncio
import copy
import logging

import pytest
from fastapi import HTTPException

from pii import PIIDetector, PIIMatch, RegexDetector, luhn_valid, redact
from pii_hook import DISABLED, RESULT_KEY, PIIHook


def scrub(text):
    return asyncio.run(redact(text, RegexDetector()))


# --- the detector, one PII type at a time ---

@pytest.mark.parametrize(
    "value, placeholder",
    [
        ("123-45-6789", "[REDACTED_SSN]"),
        ("4111111111111111", "[REDACTED_CREDIT_CARD]"),       # Visa test number
        ("4111 1111 1111 1111", "[REDACTED_CREDIT_CARD]"),    # grouped with spaces
        ("5555-5555-5555-4444", "[REDACTED_CREDIT_CARD]"),    # Mastercard test number, dashes
        ("378282246310005", "[REDACTED_CREDIT_CARD]"),        # Amex, 15 digits
        ("jane.doe+work@example.co.uk", "[REDACTED_EMAIL]"),
        ("555-123-4567", "[REDACTED_PHONE]"),
        ("(555) 123-4567", "[REDACTED_PHONE]"),
        ("+1 555.123.4567", "[REDACTED_PHONE]"),
        ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789", "[REDACTED_API_KEY]"),
        ("AKIAIOSFODNN7EXAMPLE", "[REDACTED_API_KEY]"),       # AWS's documented example key
        ("ghp_1234567890abcdefghijklmnopqrstuvwxyz", "[REDACTED_API_KEY]"),
    ],
)
def test_each_pii_type_is_redacted(value, placeholder):
    text, counts = scrub(f"My value is {value}, thanks.")
    assert text == f"My value is {placeholder}, thanks."
    assert sum(counts.values()) == 1  # e.g. a card is not *also* counted as a phone number


def test_multiple_types_in_one_prompt():
    prompt = (
        "I'm Jane, SSN 123-45-6789, card 4111 1111 1111 1111, email jane@example.com, "
        "call 555-123-4567. My AWS key is AKIAIOSFODNN7EXAMPLE. Old SSN: 234-56-7890."
    )
    text, counts = scrub(prompt)
    assert counts == {"SSN": 2, "CREDIT_CARD": 1, "EMAIL": 1, "PHONE": 1, "API_KEY": 1}
    assert text == (
        "I'm Jane, SSN [REDACTED_SSN], card [REDACTED_CREDIT_CARD], email [REDACTED_EMAIL], "
        "call [REDACTED_PHONE]. My AWS key is [REDACTED_API_KEY]. Old SSN: [REDACTED_SSN]."
    )


@pytest.mark.parametrize("number", ["4111111111111112", "4111 1111 1111 1112", "1234567812345678"])
def test_luhn_invalid_number_is_not_redacted(number):
    prompt = f"Tracking number {number} shipped today."
    text, counts = scrub(prompt)
    assert text == prompt
    assert not counts


def test_luhn_check():
    assert luhn_valid("4111111111111111")
    assert luhn_valid("79927398713")  # the textbook example
    assert not luhn_valid("4111111111111112")


@pytest.mark.parametrize(
    "prompt",
    [
        "Summarize the Q3 report in three bullet points.",
        "Release v1.105.0 shipped on 2026-10-12 at 14:30.",
        "Order #12345 has 3 items totalling $1,299.99.",
        "Use the task-runner and a risk-based approach.",
        "000-12-3456 is not a valid SSN.",
        "",
    ],
)
def test_prompt_without_pii_is_unchanged(prompt):
    text, counts = scrub(prompt)
    assert text == prompt
    assert not counts


def test_redacting_twice_changes_nothing():
    once, _ = scrub("SSN 123-45-6789, mail a@b.com")
    twice, counts = scrub(once)
    assert twice == once
    assert not counts


# --- the LiteLLM hook ---

TEST_TENANTS = """
tenants:
  support:
    allowed_models: [m]
    tpm_quota: 1
  research:
    allowed_models: [m]
    tpm_quota: 1
    pii_scrubbing: false
"""


@pytest.fixture
def tenants_file(tmp_path):
    path = tmp_path / "tenants.yaml"
    path.write_text(TEST_TENANTS)
    return path


@pytest.fixture
def hook(tenants_file):
    return PIIHook(tenants_file=tenants_file)


@pytest.fixture
def pii_logs():
    """Capture what the hook logs, as plain strings."""
    records = []

    class ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = ListHandler()
    logger = logging.getLogger("aegis.pii")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


def make_request(messages, tenant="support"):
    """Shape a request the way LiteLLM hands it to the hook."""
    return {"model": "m", "messages": messages, "proxy_server_request": {"headers": {"tenant-id": tenant}}}


def run_hook(hook, data):
    return asyncio.run(hook.async_pre_call_hook(user_api_key_dict=None, cache=None, data=data, call_type="completion"))


def response_headers(hook, data, response=object()):
    return asyncio.run(
        hook.async_post_call_response_headers_hook(data=data, user_api_key_dict=None, response=response)
    )


def test_every_role_is_scrubbed(hook):
    data = make_request([
        {"role": "system", "content": "Escalate to ops@example.com."},
        {"role": "user", "content": "My SSN is 123-45-6789."},
        {"role": "assistant", "content": "Thanks, I'll call 555-123-4567."},
        {"role": "tool", "content": "key=AKIAIOSFODNN7EXAMPLE"},
    ])
    run_hook(hook, data)
    assert [m["content"] for m in data["messages"]] == [
        "Escalate to [REDACTED_EMAIL].",
        "My SSN is [REDACTED_SSN].",
        "Thanks, I'll call [REDACTED_PHONE].",
        "key=[REDACTED_API_KEY]",
    ]


def test_multi_part_content_scrubs_text_parts_only(hook):
    image = {"type": "image_url", "image_url": {"url": "https://example.com/receipt.png"}}
    data = make_request([{
        "role": "user",
        "content": [
            {"type": "text", "text": "Card 4111 1111 1111 1111 on this receipt:"},
            image,
            {"type": "text", "text": "Reply to jane@example.com"},
        ],
    }])
    run_hook(hook, data)
    parts = data["messages"][0]["content"]
    assert parts[0]["text"] == "Card [REDACTED_CREDIT_CARD] on this receipt:"
    assert parts[1] == {"type": "image_url", "image_url": {"url": "https://example.com/receipt.png"}}
    assert parts[2]["text"] == "Reply to [REDACTED_EMAIL]"


def test_counts_reach_the_response_headers(hook):
    data = make_request([{"role": "user", "content": "SSN 123-45-6789 and 234-56-7890, mail a@b.com"}])
    run_hook(hook, data)
    assert data["metadata"][RESULT_KEY] == {"SSN": 2, "EMAIL": 1}
    assert response_headers(hook, data) == {
        "x-aegis-pii-redactions": "3",
        "x-aegis-pii-types": "EMAIL=1,SSN=2",
    }


def test_no_pii_request_is_unchanged(hook, pii_logs):
    messages = [{"role": "user", "content": "What is the capital of France?"}]
    data = make_request(copy.deepcopy(messages))
    run_hook(hook, data)
    assert data["messages"] == messages
    assert response_headers(hook, data) == {"x-aegis-pii-redactions": "0", "x-aegis-pii-types": "none"}
    assert pii_logs == []  # nothing found, nothing logged


def test_no_headers_on_failed_calls(hook):
    data = make_request([{"role": "user", "content": "hi"}])
    data["metadata"] = {RESULT_KEY: DISABLED}  # e.g. sent by the client, request then rejected
    assert response_headers(hook, data, response=None) is None


def test_client_cannot_fake_the_result(hook):
    data = make_request([{"role": "user", "content": "SSN 123-45-6789"}])
    data["metadata"] = {RESULT_KEY: DISABLED}  # client tries to claim scrubbing was off
    run_hook(hook, data)
    assert data["metadata"][RESULT_KEY] == {"SSN": 1}
    assert data["messages"][0]["content"] == "SSN [REDACTED_SSN]"


def test_tenant_can_turn_scrubbing_off(hook):
    data = make_request([{"role": "user", "content": "SSN 123-45-6789"}], tenant="research")
    run_hook(hook, data)
    assert data["messages"][0]["content"] == "SSN 123-45-6789"
    assert response_headers(hook, data) == {"x-aegis-pii-redactions": DISABLED}


def test_logs_have_tenant_and_counts_but_never_pii(hook, pii_logs):
    data = make_request([{"role": "user", "content": "SSN 123-45-6789, card 4111111111111111"}])
    run_hook(hook, data)
    assert pii_logs == ["pii_redacted tenant=support total=2 types=CREDIT_CARD:1,SSN:1"]


def test_litellm_logging_copy_is_scrubbed_too(hook):
    data = make_request([{"role": "user", "content": "SSN 123-45-6789"}])
    # LiteLLM keeps a shallow copy of the body for logging; it shares the message objects.
    data["proxy_server_request"]["body"] = {k: v for k, v in data.items() if k != "proxy_server_request"}
    run_hook(hook, data)
    assert data["proxy_server_request"]["body"]["messages"][0]["content"] == "SSN [REDACTED_SSN]"


def test_broken_detector_fails_closed(tenants_file, pii_logs):
    class BrokenDetector(PIIDetector):
        async def detect(self, text):
            raise RuntimeError(f"could not scan: {text}")

    hook = PIIHook(detector=BrokenDetector(), tenants_file=tenants_file)
    with pytest.raises(HTTPException) as exc:
        run_hook(hook, make_request([{"role": "user", "content": "SSN 123-45-6789"}]))
    assert exc.value.status_code == 503
    assert pii_logs == ["pii_scrub_failed tenant=support error=RuntimeError"]  # no text in the log


def test_any_detector_plugs_in_without_changing_the_hook(tenants_file):
    class NameDetector(PIIDetector):  # stands in for a future ComprehendDetector
        async def detect(self, text):
            start = text.find("Alice")
            return [PIIMatch("NAME", start, start + 5)] if start >= 0 else []

    hook = PIIHook(detector=NameDetector(), tenants_file=tenants_file)
    data = make_request([{"role": "user", "content": "Hi, I'm Alice."}])
    run_hook(hook, data)
    assert data["messages"][0]["content"] == "Hi, I'm [REDACTED_NAME]."
