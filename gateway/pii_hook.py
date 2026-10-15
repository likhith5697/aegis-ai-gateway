"""Aegis PII scrubbing, as a LiteLLM hook. Runs after tenant_hook (see config.yaml).

Before the request leaves the gateway, every message's text is scanned and PII
is replaced with [REDACTED_<TYPE>] placeholders. Only the tenant and the counts
per PII type are logged, never the text. The counts come back to the client as
x-aegis-pii-* response headers.
"""

from collections import Counter
from pathlib import Path

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from pii import PIIDetector, RegexDetector, redact
from settings import FAIL_CLOSED, fail_mode, get_logger
from tenants import DEFAULT_TENANTS_FILE, get_tenant_id, load_tenants, pii_scrubbing_enabled

# Where the hook leaves its result for the response-headers hook.
# Always overwritten, so a value sent by the client is never trusted.
RESULT_KEY = "aegis_pii_redactions"
DISABLED = "disabled"
UNAVAILABLE = "unavailable"

logger = get_logger("aegis.pii")


async def scrub_messages(messages: list | None, detector: PIIDetector) -> Counter:
    """Redact every message's text, all roles. Edits the messages *in place*.

    In place matters: LiteLLM keeps a shallow copy of the request body for
    logging that shares these same message objects, so that copy gets
    scrubbed too.
    """
    counts = Counter()
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"], found = await redact(content, detector)
            counts += found
        elif isinstance(content, list):  # multi-part: [{"type": "text", "text": ...}, {"type": "image_url", ...}]
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"], found = await redact(part["text"], detector)
                    counts += found
    return counts


class PIIHook(CustomLogger):
    def __init__(
        self,
        detector: PIIDetector | None = None,
        tenants_file: Path = DEFAULT_TENANTS_FILE,
        on_detector_error: str | None = None,
    ):
        super().__init__()
        self.detector = detector or RegexDetector()
        self.tenants = load_tenants(tenants_file)
        self.on_detector_error = on_detector_error or fail_mode("AEGIS_PII_FAIL_MODE", default=FAIL_CLOSED)

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        tenant_id = get_tenant_id(data)
        tenant = self.tenants.get(tenant_id)
        metadata = data.setdefault("metadata", {})

        # An unknown tenant can only get here if the hook order is wrong; scrub anyway to be safe.
        if tenant is not None and not pii_scrubbing_enabled(tenant):
            metadata[RESULT_KEY] = DISABLED
            return data

        try:
            counts = await scrub_messages(data.get("messages"), self.detector)
        except Exception as e:
            # Log the error type only; the message could contain the text being scanned.
            if self.on_detector_error == FAIL_CLOSED:  # default: never send a prompt we couldn't scan
                logger.error("pii_scrub_failed tenant=%s error=%s action=reject", tenant_id, type(e).__name__)
                raise HTTPException(status_code=503, detail={"error": "PII scrubbing unavailable"})
            logger.warning("pii_scrub_failed tenant=%s error=%s action=allow_unscrubbed", tenant_id, type(e).__name__)
            metadata[RESULT_KEY] = UNAVAILABLE
            return data

        metadata[RESULT_KEY] = dict(counts)
        if counts:
            types = ",".join(f"{t}:{n}" for t, n in sorted(counts.items()))
            logger.info("pii_redacted tenant=%s total=%d types=%s", tenant_id, counts.total(), types)
        return data

    async def async_post_call_response_headers_hook(
        self, data, user_api_key_dict, response, request_headers=None, litellm_call_info=None
    ):
        # Only on success: then our pre-call hook definitely ran and overwrote RESULT_KEY.
        # On a failure (e.g. tenant rejected) the metadata could still hold a client-sent value.
        if response is None:
            return None
        result = (data.get("metadata") or {}).get(RESULT_KEY)
        if result is None:
            return None
        if result in (DISABLED, UNAVAILABLE):
            return {"x-aegis-pii-redactions": result}
        return {
            "x-aegis-pii-redactions": str(sum(result.values())),
            "x-aegis-pii-types": ",".join(f"{t}={n}" for t, n in sorted(result.items())) or "none",
        }


# config.yaml points LiteLLM at this object: callbacks: [..., pii_hook.proxy_handler_instance]
proxy_handler_instance = PIIHook()
