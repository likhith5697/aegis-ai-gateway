"""When to fail over to a fallback model. LiteLLM's router does the actual failover.

LiteLLM's router falls back on *every* error. That's wrong for client errors:
a bad request, a bad key or a content-policy block would fail the same way on
the backup, so trying it only doubles the cost and hides the real error.
install_failover_policy() puts should_fail_over() in front of the router's one
"should I fall back?" decision point.
"""

import functools
import inspect

import litellm
from litellm.exceptions import MidStreamFallbackError
from litellm.router import Router
from litellm.types.router import RouterRateLimitError

# Never fail over on these: the request itself is the problem, so the backup fails the same way.
NO_FAILOVER = (litellm.ContentPolicyViolationError, litellm.ContextWindowExceededError)
# Always fail over on these: the model is slow, throttled, or skipped by the circuit breaker.
ALWAYS_FAILOVER = (litellm.Timeout, RouterRateLimitError)  # RouterRateLimitError = model in cooldown


def should_fail_over(error: Exception) -> bool:
    """True for 408/429/5xx, timeouts, network failures and cooldowns; False for other 4xx."""
    # A stream that failed before its first token arrives wrapped; judge the provider's error inside.
    if isinstance(error, MidStreamFallbackError) and error.original_exception is not None:
        error = error.original_exception
    if isinstance(error, NO_FAILOVER):
        return False
    if isinstance(error, ALWAYS_FAILOVER):
        return True
    status = getattr(error, "status_code", None)
    if status is None:  # never got an HTTP answer (connection refused, reset): the model is unreachable
        return True
    status = int(status)
    return status in (408, 429) or status >= 500


def install_failover_policy() -> None:
    """Make LiteLLM's router ask should_fail_over() before it falls back. Safe to call twice."""
    original = Router.async_function_with_fallbacks_common_utils
    if getattr(original, "_aegis_policy", False):
        return
    # This is LiteLLM internals: if an upgrade renames or reshapes it, refuse to start
    # instead of silently failing over on every error again.
    if "e" not in inspect.signature(original).parameters:
        raise RuntimeError("LiteLLM's fallback internals changed; review gateway/failover.py before upgrading")

    @functools.wraps(original)
    async def with_policy(self, *args, **kwargs):
        error = kwargs["e"] if "e" in kwargs else args[0]
        if not should_fail_over(error):
            raise error  # the caller gets the original error, e.g. the 401, unchanged
        return await original(self, *args, **kwargs)

    with_policy._aegis_policy = True
    Router.async_function_with_fallbacks_common_utils = with_policy


def configured_fallbacks(model: str) -> list[str]:
    """The fallbacks config.yaml's router_settings define for a model, [] if none."""
    from litellm.proxy import proxy_server  # imported here: the running proxy's router only exists in the proxy

    router = getattr(proxy_server, "llm_router", None)
    for entry in getattr(router, "fallbacks", None) or []:
        if isinstance(entry, dict) and model in entry:
            return list(entry[model])
    return []
