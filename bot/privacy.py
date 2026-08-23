"""Small, shared redaction helpers for persisted or transferable support data."""

from __future__ import annotations

import re
from typing import Any


_PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)")
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def redact_text(value: Any, *, limit: int = 4000) -> str:
    text = str(value or "")[: max(0, int(limit))]
    return _PHONE_RE.sub("[phone]", _EMAIL_RE.sub("[email]", text))


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key)[:80]: redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(value)
