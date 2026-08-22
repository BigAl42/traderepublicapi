"""Redact secrets from logs and error strings."""

from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(?i)\b(sessionToken|refreshToken|access_token|tr_session|authorization)"
            r"([\"']?\s*[:=]\s*[\"']?)([^\s\"',}\]]+)"
        ),
        r"\1\2***",
    ),
    (re.compile(r"(?i)\b(TR_TOKEN|TR_PIN|TR_PHONE)(\s*[=:]\s*)(\S+)"), r"\1\2***"),
    (re.compile(r"(?i)\b(pin|password|phoneNumber)([\"']?\s*[:=]\s*[\"']?)([^\s\"',}\]]+)"), r"\1\2***"),
    (re.compile(r"(\+49)\d{6,}"), r"\1***"),
    (re.compile(r"(Bearer\s+)\S+", re.I), r"\1***"),
]


def redact_secrets(text: str) -> str:
    """Mask credential-like substrings before logging or returning raw errors."""
    if not text:
        return text
    redacted = text
    for pattern, repl in _PATTERNS:
        redacted = pattern.sub(repl, redacted)
    return redacted
