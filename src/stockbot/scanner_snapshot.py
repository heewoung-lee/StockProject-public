from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence


_ACCOUNT_LIKE_PATTERN = re.compile(r"\d{8,}")
_SYMBOL_ALIASES = ("symbol", "code", "stock_code", "ticker")
_PRICE_ALIASES = (
    "price",
    "current_price",
    "currentPrice",
    "close",
    "last_price",
    "lastPrice",
    "trade_price",
)
_VOLUME_ALIASES = ("volume", "trade_volume", "accumulated_volume", "acml_vol")
_PRIORITY_ALIASES = ("priority", "rank_score", "score", "trading_value", "trade_value", "amount", "volume")
_MAX_COMPLETED_HISTORY_ROWS = 60


@dataclass(frozen=True)
class SnapshotWriteOptions:
    provider: str = "external-json"
    max_candidates: int | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    min_volume: int | None = None
    pretty: bool = True


def load_external_snapshot_payload(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_scanner_snapshot(
    input_path: str | Path,
    output_path: str | Path,
    options: SnapshotWriteOptions | None = None,
) -> int:
    return write_scanner_snapshot_payload(
        load_external_snapshot_payload(input_path),
        output_path,
        options,
    )


def write_scanner_snapshot_payload(
    external_payload: Any,
    output_path: str | Path,
    options: SnapshotWriteOptions | None = None,
) -> int:
    resolved_options = options or SnapshotWriteOptions()
    payload = build_scanner_snapshot_payload(external_payload, resolved_options)
    _atomic_write_json(Path(output_path), payload, pretty=resolved_options.pretty)
    return len(payload["candidates"])


def build_scanner_snapshot_payload(
    external_payload: Any,
    options: SnapshotWriteOptions | None = None,
) -> dict[str, Any]:
    resolved_options = options or SnapshotWriteOptions()
    provider = _provider_from_payload(external_payload, resolved_options.provider)
    generated_at = datetime.now(timezone.utc).isoformat()
    candidates: list[dict[str, Any]] = []

    for index, record in enumerate(_records_from_payload(external_payload)):
        candidate = _candidate_from_record(record, generated_at=generated_at, fallback_priority=index)
        if candidate is None:
            continue
        if not _passes_filters(candidate, resolved_options):
            continue
        candidates.append(candidate)

    candidates = sorted(
        candidates,
        key=lambda candidate: -_float_value(candidate.get("priority")),
    )
    if resolved_options.max_candidates is not None:
        candidates = candidates[: max(0, int(resolved_options.max_candidates))]

    return {
        "provider": provider,
        "generated_at": generated_at,
        "candidates": candidates,
    }


def _records_from_payload(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    for key in ("candidates", "items", "stocks", "symbols", "results", "data", "positions"):
        raw_records = payload.get(key)
        if isinstance(raw_records, list):
            return [record for record in raw_records if isinstance(record, Mapping)]
    return []


def _candidate_from_record(
    record: Mapping[str, Any],
    *,
    generated_at: str,
    fallback_priority: int,
) -> dict[str, Any] | None:
    symbol = _symbol_from_record(record)
    price = _decimal_from_aliases(record, _PRICE_ALIASES, absolute=True)
    if not symbol or price is None:
        return None

    volume = _int_from_aliases(record, _VOLUME_ALIASES)
    priority = _priority_from_record(record, fallback_priority=fallback_priority)
    candidate: dict[str, Any] = {
        "symbol": symbol,
        "price": _decimal_text(price),
        "volume": volume,
        "priority": priority,
        "reason": _safe_metadata_text(record.get("reason"), "external_rank"),
        "timestamp": _timestamp_from_record(record, generated_at),
    }

    name = record.get("name") or record.get("company_name") or record.get("companyName") or record.get("stock_name")
    if name:
        candidate["name"] = _safe_metadata_text(name, "")
    market = _safe_metadata_text(record.get("market"), "").strip().upper()
    if market:
        candidate["market"] = market[:32]

    for source_key, target_key in (
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("vwap", "vwap"),
        ("bid", "bid"),
        ("ask", "ask"),
    ):
        value = _decimal_from_aliases(record, (source_key,), absolute=True)
        if value is not None:
            candidate[target_key] = _decimal_text(value)

    for source_key, target_key in (
        ("change_rate", "change_rate"),
        ("change_pct", "change_rate"),
        ("trading_value", "trading_value"),
        ("trade_value", "trading_value"),
        ("amount", "trading_value"),
    ):
        value = _decimal_from_aliases(record, (source_key,))
        if value is not None and target_key not in candidate:
            candidate[target_key] = _decimal_text(value)

    history = _history_from_record(record, symbol)
    if history:
        candidate["history"] = history

    return candidate


def _history_from_record(record: Mapping[str, Any], symbol: str) -> list[dict[str, Any]]:
    raw_history = record.get("history")
    if not isinstance(raw_history, list):
        return []

    rows_by_timestamp: dict[str, dict[str, Any]] = {}
    for raw_row in raw_history:
        if not isinstance(raw_row, Mapping):
            continue
        row_symbol = _symbol_from_record(raw_row)
        if row_symbol != symbol:
            continue
        timestamp = _strict_timestamp(raw_row.get("timestamp"))
        if timestamp is None:
            continue

        prices: dict[str, Decimal] = {}
        malformed = False
        for key in ("open", "high", "low", "close", "vwap"):
            value = _decimal_value(raw_row.get(key))
            if value is None or not value.is_finite() or value <= 0:
                malformed = True
                break
            prices[key] = value
        if malformed:
            continue
        if prices["high"] < max(prices["open"], prices["close"]):
            continue
        if prices["low"] > min(prices["open"], prices["close"]):
            continue

        volume_value = _decimal_value(raw_row.get("volume"))
        if (
            volume_value is None
            or not volume_value.is_finite()
            or volume_value < 0
            or volume_value != volume_value.to_integral_value()
        ):
            continue
        row: dict[str, Any] = {
            "symbol": symbol,
            "timestamp": timestamp,
            "open": _decimal_text(prices["open"]),
            "high": _decimal_text(prices["high"]),
            "low": _decimal_text(prices["low"]),
            "close": _decimal_text(prices["close"]),
            "volume": int(volume_value),
            "vwap": _decimal_text(prices["vwap"]),
        }
        market = _safe_metadata_text(raw_row.get("market"), "").strip().upper()
        if market:
            row["market"] = market[:32]
        for key in ("bid", "ask"):
            value = _decimal_value(raw_row.get(key))
            if value is not None and value.is_finite() and value > 0:
                row[key] = _decimal_text(value)
        rows_by_timestamp[timestamp] = row
    return [
        rows_by_timestamp[timestamp]
        for timestamp in sorted(rows_by_timestamp)[-_MAX_COMPLETED_HISTORY_ROWS:]
    ]


def _strict_timestamp(value: object) -> str | None:
    raw_value = str(value or "").strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.isoformat()


def _passes_filters(candidate: Mapping[str, Any], options: SnapshotWriteOptions) -> bool:
    price = _decimal_value(candidate.get("price"))
    if price is None:
        return False
    if options.min_price is not None and price < options.min_price:
        return False
    if options.max_price is not None and price > options.max_price:
        return False
    if options.min_volume is not None and int(candidate.get("volume", 0)) < options.min_volume:
        return False
    return True


def _provider_from_payload(payload: Any, fallback: str) -> str:
    safe_fallback = _safe_metadata_text(fallback, "external-json")
    if isinstance(payload, Mapping):
        return _safe_metadata_text(payload.get("provider"), safe_fallback)
    return safe_fallback


def _symbol_from_record(record: Mapping[str, Any]) -> str:
    raw_symbol = _first_value(record, _SYMBOL_ALIASES)
    if raw_symbol is None:
        return ""
    symbol = str(raw_symbol).strip().upper()
    if "." in symbol:
        symbol = symbol.split(".", 1)[0]
    if symbol.startswith("A") and symbol[1:].isdigit():
        symbol = symbol[1:]
    return "".join(char for char in symbol if char.isalnum())


def _priority_from_record(record: Mapping[str, Any], *, fallback_priority: int) -> float:
    for key in _PRIORITY_ALIASES:
        if key not in record:
            continue
        value = _decimal_value(record.get(key), absolute=True)
        if value is not None:
            return float(value)
    return float(max(0, 1_000_000 - fallback_priority))


def _timestamp_from_record(record: Mapping[str, Any], default: str) -> str:
    raw_value = str(record.get("timestamp") or record.get("datetime") or record.get("time") or "").strip()
    if not raw_value:
        return default
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return default


def _first_value(record: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for alias in aliases:
        if alias in record:
            return record[alias]
    return None


def _decimal_from_aliases(
    record: Mapping[str, Any],
    aliases: Sequence[str],
    *,
    absolute: bool = False,
) -> Decimal | None:
    return _decimal_value(_first_value(record, aliases), absolute=absolute)


def _decimal_value(value: Any, *, absolute: bool = False) -> Decimal | None:
    if value is None:
        return None
    text = _decimal_source_text(value)
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return abs(parsed) if absolute else parsed


def _int_from_aliases(record: Mapping[str, Any], aliases: Sequence[str]) -> int:
    value = _decimal_from_aliases(record, aliases, absolute=True)
    if value is None:
        return 0
    return max(0, int(value))


def _float_value(value: Any) -> float:
    parsed = _decimal_value(value, absolute=True)
    return 0.0 if parsed is None else float(parsed)


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f")


def _decimal_source_text(value: Any) -> str:
    return (
        str(value)
        .strip()
        .replace(",", "")
        .replace("원", "")
        .replace("₩", "")
        .replace("%", "")
        .strip()
    )


def _safe_metadata_text(value: object, default: str) -> str:
    text = str(value or "").strip()
    if not text:
        return default
    lowered = text.lower()
    if any(token in lowered for token in ("authorization", "bearer", "token", "secret", "account", "acct", "계좌")):
        return default
    if _ACCOUNT_LIKE_PATTERN.search(text):
        return default
    if "\\" in text or "/" in text:
        return default
    if len(text) > 60:
        return default
    safe = "".join(char for char in text if char.isalnum() or char in {" ", "-", "_", ".", "&", "(", ")", "+"}).strip()
    return safe if safe and safe == text else default


def _atomic_write_json(output_path: Path, payload: Mapping[str, Any], *, pretty: bool) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.name}.tmp")
    rendered = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=True,
    )
    temp_path.write_text(f"{rendered}\n", encoding="utf-8")
    temp_path.replace(output_path)
