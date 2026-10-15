"""Settings and logging shared by the Aegis hooks."""

import logging
import os

# What a hook does when a dependency it needs (PII detector, Redis) is broken:
#   open   = let the request through and log a warning
#   closed = reject the request (503) and log an error
FAIL_OPEN = "open"
FAIL_CLOSED = "closed"


def fail_mode(env_var: str, default: str) -> str:
    """Read a fail mode from the environment. A typo stops the gateway at startup."""
    value = os.environ.get(env_var, default).strip().lower()
    if value not in (FAIL_OPEN, FAIL_CLOSED):
        raise ValueError(f"{env_var} must be '{FAIL_OPEN}' or '{FAIL_CLOSED}', got '{value}'")
    return value


def get_logger(name: str) -> logging.Logger:
    """A logger that prints to the container output. LiteLLM doesn't show INFO logs from our code otherwise."""
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
