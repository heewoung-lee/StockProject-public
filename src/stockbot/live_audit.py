from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .redaction import is_sensitive_key, redact_sensitive_text


SAFE_STRUCTURED_VALUE_KEYS = frozenset(
    {
        "average_fill_price",
        "estimated_price",
        "filled_quantity",
        "quantity",
        "reference_price",
        "remaining_quantity",
        "requested_quantity",
        "slippage_pct",
        "submitted_price",
        "unfilled_quantity",
    }
)


class JsonlLiveAuditLog:
    def __init__(self, path: str | Path, *, redact_values: Sequence[str] = ()):
        self.path = Path(path)
        self.redact_values = tuple(str(value) for value in redact_values if str(value))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: str, payload: Mapping[str, Any]) -> None:
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": str(event),
            "payload": self._redact(payload),
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()

    def _redact(self, value: Any, *, key: str = "") -> Any:
        if is_sensitive_key(key):
            return "[REDACTED]"
        if isinstance(value, Mapping):
            return {str(child_key): self._redact(child_value, key=str(child_key)) for child_key, child_value in value.items()}
        if isinstance(value, list):
            return [self._redact(item, key=key) for item in value]
        if isinstance(value, tuple):
            return tuple(self._redact(item, key=key) for item in value)
        if isinstance(value, str):
            if key in SAFE_STRUCTURED_VALUE_KEYS:
                return value
            return redact_sensitive_text(value, extra_values=self.redact_values)
        return value
