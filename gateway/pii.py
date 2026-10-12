"""PII detection and redaction. Knows nothing about LiteLLM.

A PIIDetector only *finds* PII and reports where it is. redact() does the
replacing. To swap regex for Amazon Comprehend later, write a new PIIDetector
subclass and pass it to the hook; nothing else changes.
"""

import re
from abc import ABC, abstractmethod
from collections import Counter
from dataclasses import dataclass

SSN = "SSN"
CREDIT_CARD = "CREDIT_CARD"
EMAIL = "EMAIL"
PHONE = "PHONE"
API_KEY = "API_KEY"


@dataclass(frozen=True)
class PIIMatch:
    pii_type: str  # one of the constants above
    start: int     # text[start:end] is the sensitive value
    end: int


class PIIDetector(ABC):
    """Finds PII in text. async so detectors that call a network service don't block the gateway."""

    @abstractmethod
    async def detect(self, text: str) -> list[PIIMatch]: ...


def luhn_valid(digits: str) -> bool:
    """The checksum every real card number passes. Catches ~90% of random digit strings."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:      # double every second digit from the right
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class RegexDetector(PIIDetector):
    # (?<!\d) / (?!\d) stop a match from starting or ending inside a longer number.
    PATTERNS: list[tuple[str, re.Pattern]] = [
        # US SSN, dashed: 123-45-6789. Skips numbers the SSA never issues (000, 666, 9xx, ...).
        (SSN, re.compile(r"(?<!\d)(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?!\d)")),
        # 13-19 digits, optionally grouped by single spaces or dashes. Luhn-checked below.
        (CREDIT_CARD, re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")),
        (EMAIL, re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")),
        # US numbers: 555-123-4567, (555) 123-4567, +1 555.123.4567, 5551234567
        (PHONE, re.compile(r"(?<![\w+])(?:\+?1[ .-]?)?(?:\(\d{3}\)|\d{3})[ .-]?\d{3}[ .-]?\d{4}(?!\w)")),
        (API_KEY, re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),             # OpenAI, Anthropic (sk-ant-...)
        (API_KEY, re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),        # AWS access key IDs
        (API_KEY, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),       # GitHub tokens
        (API_KEY, re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),       # GitHub fine-grained tokens
        (API_KEY, re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),       # Slack tokens
        (API_KEY, re.compile(r"\bAIza[0-9A-Za-z_-]{35}")),              # Google API keys
    ]

    async def detect(self, text: str) -> list[PIIMatch]:
        matches = []
        for pii_type, pattern in self.PATTERNS:
            for m in pattern.finditer(text):
                if pii_type == CREDIT_CARD and not luhn_valid(re.sub(r"\D", "", m.group())):
                    continue
                matches.append(PIIMatch(pii_type, m.start(), m.end()))
        return matches


async def redact(text: str, detector: PIIDetector) -> tuple[str, Counter]:
    """Replace each PII match with [REDACTED_<TYPE>]. Returns the new text and a count per type."""
    matches = await detector.detect(text)
    # Earliest first; when two matches start together, the longer one wins.
    matches.sort(key=lambda m: (m.start, -(m.end - m.start)))

    parts, counts, pos = [], Counter(), 0
    for m in matches:
        if m.start < pos:  # overlaps a match we already replaced
            continue
        parts.append(text[pos:m.start])
        parts.append(f"[REDACTED_{m.pii_type}]")
        counts[m.pii_type] += 1
        pos = m.end
    parts.append(text[pos:])
    return "".join(parts), counts
