from __future__ import annotations

import re
from collections.abc import Sequence


SENSITIVE_KEY_PARTS = (
    "secret",
    "token",
    "authorization",
    "appkey",
    "app_key",
    "account",
    "account_no",
    "acct",
    "cano",
)

_ASSIGNMENT_PATTERN = re.compile(
    r"\b("
    r"KIS_(?:LIVE|VTS)_[A-Z_]*(?:SECRET|KEY|ACCOUNT_NO)"
    r"|app(?:secret|key)"
    r"|app_(?:secret|key)"
    r"|access_token"
    r"|refresh_token"
    r"|token"
    r"|authorization"
    r"|account(?:_no)?"
    r"|acct"
    r"|cano"
    r")(\s*[:=]\s*)[^\s,;]+",
    flags=re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"Bearer\s+[^\s,;]+", flags=re.IGNORECASE)
_LONG_NUMBER_PATTERN = re.compile(r"\b\d{8,}(?:-\d{1,2})?\b")
MIN_EXTRA_REDACTION_VALUE_LENGTH = 8


def redact_sensitive_text(text: str, *, extra_values: Sequence[str] = ()) -> str:
    redacted = str(text)
    for secret in redactable_extra_values(extra_values):
        redacted = redacted.replace(secret, "[REDACTED]")

    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
    redacted = _LONG_NUMBER_PATTERN.sub("[REDACTED]", redacted)
    return redacted


def redactable_extra_values(extra_values: Sequence[str]) -> tuple[str, ...]:
    values = {
        str(value).strip()
        for value in extra_values
        if len(str(value).strip()) >= MIN_EXTRA_REDACTION_VALUE_LENGTH
    }
    return tuple(sorted(values, key=len, reverse=True))


def is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)
