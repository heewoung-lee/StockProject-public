from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol, Sequence

from .models import MarketBar


_ACCOUNT_LIKE_PATTERN = re.compile(r"\d{8,}")
_SENSITIVE_METADATA_TOKENS = (
    "authorization",
    "bearer",
    "token",
    "secret",
    "account",
    "acct",
    "\uacc4\uc88c",
)


@dataclass(frozen=True)
class ScannerCandidate:
    symbol: str
    priority: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class ScannerDiagnostics:
    provider: str = "unknown"
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScannerSnapshot:
    bars: Mapping[str, MarketBar] = field(default_factory=dict)
    candidates: Sequence[ScannerCandidate] = field(default_factory=tuple)
    diagnostics: ScannerDiagnostics = field(default_factory=ScannerDiagnostics)
    histories: Mapping[str, Sequence[MarketBar]] = field(default_factory=dict)

    def ordered_candidates(self) -> list[ScannerCandidate]:
        return sorted(
            list(self.candidates),
            key=lambda candidate: -float(candidate.priority),
        )

    def ordered_symbols(self, fallback_symbols: Sequence[str]) -> list[str]:
        ordered: list[str] = []

        for candidate in self.ordered_candidates():
            if candidate.symbol not in ordered:
                ordered.append(candidate.symbol)

        for symbol in fallback_symbols:
            if symbol not in ordered:
                ordered.append(symbol)
        return ordered

    def priority(self, symbol: str) -> float:
        priority = self.candidate_priority(symbol)
        return 0.0 if priority is None else priority

    def candidate_priority(self, symbol: str) -> float | None:
        for candidate in self.candidates:
            if candidate.symbol == symbol:
                return float(candidate.priority)
        return None


def _normalized_completed_history(
    symbol: str,
    bars: Sequence[MarketBar],
    current_bar: MarketBar,
) -> tuple[MarketBar, ...]:
    try:
        current_minute = _timestamp_minute_key(current_bar.timestamp)
    except (OverflowError, TypeError, ValueError):
        return ()
    bars_by_timestamp: dict[float, MarketBar] = {}
    for bar in bars:
        if not _history_bar_is_well_formed(bar, symbol):
            continue
        try:
            timestamp_key = _timestamp_sort_key(bar.timestamp)
        except (OverflowError, TypeError, ValueError):
            continue
        if int(timestamp_key // 60) >= current_minute:
            continue
        bars_by_timestamp[timestamp_key] = bar
    return tuple(bars_by_timestamp[key] for key in sorted(bars_by_timestamp))


def _history_bar_is_well_formed(bar: object, symbol: str) -> bool:
    if not isinstance(bar, MarketBar) or bar.symbol != symbol:
        return False
    if not isinstance(bar.timestamp, datetime):
        return False
    try:
        open_price = Decimal(str(bar.open))
        high = Decimal(str(bar.high))
        low = Decimal(str(bar.low))
        close = Decimal(str(bar.close))
        vwap = Decimal(str(bar.vwap))
        volume = int(bar.volume)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return False
    prices = (open_price, high, low, close, vwap)
    if not all(price.is_finite() and price > 0 for price in prices):
        return False
    if high < max(open_price, close) or low > min(open_price, close) or high < low:
        return False
    if volume < 0 or isinstance(bar.volume, bool):
        return False

    optional_prices: list[Decimal] = []
    for value in (bar.bid, bar.ask):
        if value is None:
            continue
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return False
        if not parsed.is_finite() or parsed <= 0:
            return False
        optional_prices.append(parsed)
    if bar.bid is not None and bar.ask is not None and optional_prices[0] > optional_prices[1]:
        return False
    return True


def _timestamp_sort_key(value: datetime) -> float:
    normalized = value
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    normalized = normalized.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (normalized - epoch).total_seconds()


def _timestamp_minute_key(value: datetime) -> int:
    return int(_timestamp_sort_key(value) // 60)


class ScannerProvider(Protocol):
    label: str
    kind: str

    def rank_symbols(self, symbols: Sequence[str]) -> list[str]:
        ...

    def snapshot(self, symbols: Sequence[str]) -> ScannerSnapshot:
        ...


class StaticScannerProvider:
    label = "static scanner"
    kind = "static"

    def __init__(
        self,
        *,
        bars: Mapping[str, MarketBar],
        histories: Mapping[str, Sequence[MarketBar]] | None = None,
        priorities: Mapping[str, float] | None = None,
        label: str | None = None,
        kind: str | None = None,
    ) -> None:
        self.bars = dict(bars)
        self.histories = {
            symbol: tuple(history)
            for symbol, history in (histories or {}).items()
        }
        self.priorities = dict(priorities or {})
        if label is not None:
            self.label = label
        if kind is not None:
            self.kind = kind

    def snapshot(self, symbols: Sequence[str]) -> ScannerSnapshot:
        requested = list(dict.fromkeys(symbols))
        requested_set = set(requested)
        bars = {symbol: bar for symbol, bar in self.bars.items() if symbol in requested_set}
        histories = {
            symbol: normalized
            for symbol, bar in bars.items()
            if (
                normalized := _normalized_completed_history(
                    symbol,
                    self.histories.get(symbol, ()),
                    bar,
                )
            )
        }
        candidates = tuple(
            ScannerCandidate(symbol=symbol, priority=self.priorities.get(symbol, 0.0))
            for symbol in bars
        )
        return ScannerSnapshot(
            bars=bars,
            candidates=candidates,
            diagnostics=ScannerDiagnostics(provider=self.kind),
            histories=histories,
        )

    def rank_symbols(self, symbols: Sequence[str]) -> list[str]:
        requested = list(symbols)
        if not requested:
            requested = list(self.bars)
        return self.snapshot(requested).ordered_symbols(requested)


class BarProviderScanner:
    def __init__(
        self,
        bar_provider: Callable[[str], MarketBar | None],
        *,
        priority_provider: Callable[[str], float] | None = None,
        label: str = "bar provider scanner",
        kind: str = "bar-provider",
    ) -> None:
        self.bar_provider = bar_provider
        self.priority_provider = priority_provider
        self.label = label
        self.kind = kind

    def rank_symbols(self, symbols: Sequence[str]) -> list[str]:
        requested = list(dict.fromkeys(symbols))
        if self.priority_provider is None:
            return requested
        return sorted(requested, key=lambda symbol: -self._priority(symbol))

    def snapshot(self, symbols: Sequence[str]) -> ScannerSnapshot:
        requested = list(dict.fromkeys(symbols))
        bars: dict[str, MarketBar] = {}
        candidates: list[ScannerCandidate] = []
        messages: list[str] = []

        for symbol in requested:
            try:
                bar = self.bar_provider(symbol)
            except Exception as exc:
                messages.append(f"{symbol}: {exc.__class__.__name__}")
                continue
            if bar is None:
                continue
            bars[symbol] = bar
            candidates.append(
                ScannerCandidate(
                    symbol=symbol,
                    priority=self._priority(symbol),
                    reason=self.kind,
                )
            )

        return ScannerSnapshot(
            bars=bars,
            candidates=tuple(candidates),
            diagnostics=ScannerDiagnostics(provider=self.kind, messages=tuple(messages)),
        )

    def _priority(self, symbol: str) -> float:
        if self.priority_provider is None:
            return 0.0
        try:
            return float(self.priority_provider(symbol))
        except Exception:
            return 0.0


class JsonScannerProvider:
    label = "JSON scanner"
    kind = "json"

    def __init__(
        self,
        path: str | Path,
        *,
        label: str | None = None,
        kind: str = "json",
        max_snapshot_age_seconds: int | None = None,
        now_provider: Callable[[], datetime] | None = None,
        refresh_callback: Callable[[], None] | None = None,
        require_current_minute: bool = False,
        refresh_failure_retry_seconds: float = 0.0,
    ) -> None:
        self.path = Path(path)
        self.kind = kind
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self.refresh_callback = refresh_callback
        self.require_current_minute = bool(require_current_minute)
        self.refresh_failure_retry_seconds = max(0.0, float(refresh_failure_retry_seconds))
        self._last_refresh_failure_at: datetime | None = None
        if label is not None:
            self.label = label

    def rank_symbols(self, symbols: Sequence[str]) -> list[str]:
        requested = list(dict.fromkeys(symbols))
        requested_set = set(requested) if requested else None
        entries, provider, messages, generated_at = self._load_entries()
        if messages:
            raise RuntimeError("; ".join(messages))
        entries, filtered_count = self._current_minute_entries(entries, generated_at)
        if filtered_count and not entries:
            raise RuntimeError(
                f"{self.kind}: no current-minute scanner candidates "
                f"filtered_count={filtered_count}"
            )

        candidates = [
            self._candidate_from_entry(entry)
            for entry in entries
            if self._symbol_from_entry(entry)
            and (requested_set is None or self._symbol_from_entry(entry) in requested_set)
        ]
        return ScannerSnapshot(
            candidates=tuple(candidates),
            diagnostics=ScannerDiagnostics(provider=provider),
        ).ordered_symbols(requested)

    def snapshot(self, symbols: Sequence[str]) -> ScannerSnapshot:
        requested = list(dict.fromkeys(symbols))
        requested_set = set(requested)
        entries, provider, messages, generated_at = self._load_entries()
        entries, filtered_count = self._current_minute_entries(entries, generated_at)
        bars: dict[str, MarketBar] = {}
        histories: dict[str, tuple[MarketBar, ...]] = {}
        candidates: list[ScannerCandidate] = []
        diagnostic_messages = list(messages)
        if filtered_count:
            diagnostic_messages.append(
                f"{self.kind}: stale_candidate_minute_filtered count={filtered_count}"
            )

        for entry in entries:
            symbol = self._symbol_from_entry(entry)
            if not symbol or symbol not in requested_set:
                continue
            bar = self._bar_from_entry(entry, symbol, generated_at=generated_at)
            if bar is None:
                diagnostic_messages.append(f"{symbol}: invalid scanner bar")
                continue
            bars[symbol] = bar
            history, filtered_history_count = self._history_from_entry(entry, symbol, bar)
            if history:
                histories[symbol] = history
            if filtered_history_count:
                diagnostic_messages.append(
                    f"{symbol}: scanner_history_records_filtered count={filtered_history_count}"
                )
            candidates.append(self._candidate_from_entry(entry))

        return ScannerSnapshot(
            bars=bars,
            candidates=tuple(candidates),
            diagnostics=ScannerDiagnostics(
                provider=provider,
                messages=tuple(diagnostic_messages),
            ),
            histories=histories,
        )

    def _load_entries(
        self,
        *,
        allow_refresh: bool = True,
    ) -> tuple[list[Mapping[str, Any]], str, tuple[str, ...], datetime | None]:
        result = self._read_entries()
        entries, provider, messages, generated_at = result
        if (
            allow_refresh
            and self.refresh_callback is not None
            and self._should_refresh(messages)
            and self._refresh_failure_retry_ready()
        ):
            try:
                self.refresh_callback()
            except Exception as exc:
                self._last_refresh_failure_at = self._now_utc()
                return (
                    entries,
                    provider,
                    (f"{self.kind}: refresh failed {exc.__class__.__name__}", *messages),
                    generated_at,
                )
            refreshed = self._load_entries(allow_refresh=False)
            if self._should_refresh(refreshed[2]):
                self._last_refresh_failure_at = self._now_utc()
            else:
                self._last_refresh_failure_at = None
            return refreshed
        return result

    def _read_entries(self) -> tuple[list[Mapping[str, Any]], str, tuple[str, ...], datetime | None]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            return [], self.kind, (f"{self.kind}: {exc.__class__.__name__}",), None

        if isinstance(payload, list):
            generated_at = self._file_timestamp()
            stale_message = self._snapshot_stale_message(generated_at)
            if stale_message is not None:
                return [], self.kind, (stale_message,), generated_at
            return [entry for entry in payload if isinstance(entry, Mapping)], self.kind, (), generated_at
        if not isinstance(payload, Mapping):
            return [], self.kind, (f"{self.kind}: invalid payload",), None

        provider = self._safe_metadata_text(payload.get("provider"), self.kind)
        generated_at = self._metadata_timestamp(payload.get("generated_at")) or self._file_timestamp()
        stale_message = self._snapshot_stale_message(generated_at)
        if stale_message is not None:
            return [], provider, (stale_message,), generated_at
        raw_candidates = payload.get("candidates", [])
        if not isinstance(raw_candidates, list):
            return [], provider, (f"{self.kind}: invalid candidates",), generated_at
        return [entry for entry in raw_candidates if isinstance(entry, Mapping)], provider, (), generated_at

    def _should_refresh(self, messages: tuple[str, ...]) -> bool:
        if not messages:
            return False
        refreshable_fragments = (
            "stale scanner snapshot",
            "FileNotFoundError",
            "JSONDecodeError",
            "invalid payload",
            "invalid candidates",
        )
        return any(
            any(fragment in message for fragment in refreshable_fragments)
            for message in messages
        )

    def _candidate_from_entry(self, entry: Mapping[str, Any]) -> ScannerCandidate:
        symbol = self._symbol_from_entry(entry)
        return ScannerCandidate(
            symbol=symbol,
            priority=self._priority_from_entry(entry),
            reason=self._safe_metadata_text(entry.get("reason"), self.kind),
        )

    def _bar_from_entry(
        self,
        entry: Mapping[str, Any],
        symbol: str,
        *,
        generated_at: datetime | None = None,
    ) -> MarketBar | None:
        close = self._decimal_from_entry(entry, ("price", "close", "current_price"))
        if close is None:
            return None

        derived_open = self._open_from_change_rate(entry, close)
        open_price = self._decimal_from_entry(entry, ("open",), derived_open or close)
        high = self._decimal_from_entry(entry, ("high",), max(open_price, close))
        low = self._decimal_from_entry(entry, ("low",), min(open_price, close))
        derived_vwap = ((open_price + close) / Decimal("2")) if open_price != close else close
        vwap = self._decimal_from_entry(entry, ("vwap",), derived_vwap)
        bid = self._decimal_from_entry(entry, ("bid",), None)
        ask = self._decimal_from_entry(entry, ("ask",), None)
        return MarketBar(
            symbol=symbol,
            timestamp=self._timestamp_from_entry(entry, generated_at=generated_at),
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=self._volume_from_entry(entry, close),
            vwap=vwap,
            bid=bid,
            ask=ask,
            market=self._safe_metadata_text(entry.get("market"), "").strip().upper()[:32],
        )

    def _history_from_entry(
        self,
        entry: Mapping[str, Any],
        symbol: str,
        current_bar: MarketBar,
    ) -> tuple[tuple[MarketBar, ...], int]:
        if "history" not in entry:
            return (), 0
        raw_history = entry.get("history")
        if not isinstance(raw_history, list):
            return (), 1

        parsed = [
            bar
            for record in raw_history
            if isinstance(record, Mapping)
            and (bar := self._history_bar_from_entry(record, symbol)) is not None
        ]
        normalized = _normalized_completed_history(symbol, parsed, current_bar)
        return normalized, max(0, len(raw_history) - len(normalized))

    def _history_bar_from_entry(
        self,
        entry: Mapping[str, Any],
        symbol: str,
    ) -> MarketBar | None:
        if self._symbol_from_entry(entry) != symbol:
            return None
        timestamp = self._metadata_timestamp(entry.get("timestamp"))
        if timestamp is None:
            return None

        required_prices: dict[str, Decimal] = {}
        for key in ("open", "high", "low", "close", "vwap"):
            if key not in entry:
                return None
            value = self._decimal_from_entry(entry, (key,), None)
            if value is None or not value.is_finite():
                return None
            required_prices[key] = value

        if "volume" not in entry or isinstance(entry.get("volume"), bool):
            return None
        try:
            volume = int(str(entry["volume"]).replace(",", ""))
        except (TypeError, ValueError, OverflowError):
            return None

        optional_prices: dict[str, Decimal | None] = {"bid": None, "ask": None}
        for key in optional_prices:
            if key not in entry or entry.get(key) is None:
                continue
            value = self._decimal_from_entry(entry, (key,), None)
            if value is None or not value.is_finite():
                return None
            optional_prices[key] = value

        bar = MarketBar(
            symbol=symbol,
            timestamp=timestamp,
            open=required_prices["open"],
            high=required_prices["high"],
            low=required_prices["low"],
            close=required_prices["close"],
            volume=volume,
            vwap=required_prices["vwap"],
            bid=optional_prices["bid"],
            ask=optional_prices["ask"],
            market=self._safe_metadata_text(entry.get("market"), "").strip().upper()[:32],
        )
        return bar if _history_bar_is_well_formed(bar, symbol) else None

    def _symbol_from_entry(self, entry: Mapping[str, Any]) -> str:
        return str(entry.get("symbol") or "").strip()

    def _priority_from_entry(self, entry: Mapping[str, Any]) -> float:
        raw_value = entry.get("priority", entry.get("rank_score", entry.get("volume", 0)))
        try:
            return float(raw_value)
        except (TypeError, ValueError):
            return 0.0

    def _volume_from_entry(self, entry: Mapping[str, Any], close: Decimal) -> int:
        try:
            volume = max(0, int(str(entry.get("volume", 0)).replace(",", "")))
        except (TypeError, ValueError):
            volume = 0
        if volume > 0:
            return volume

        trading_value = self._decimal_from_entry(
            entry,
            ("trading_value", "trade_value", "amount"),
            None,
        )
        if trading_value is None or trading_value <= 0 or close <= 0:
            return 0
        try:
            return max(0, int(trading_value / close))
        except (InvalidOperation, ValueError, ZeroDivisionError):
            return 0

    def _open_from_change_rate(self, entry: Mapping[str, Any], close: Decimal) -> Decimal | None:
        percent_change = self._decimal_from_entry(entry, ("change_rate", "change_pct"), None)
        decimal_change = self._decimal_from_entry(entry, ("change_ratio", "change_decimal"), None)
        if percent_change is None and decimal_change is None:
            return None
        if close <= 0:
            return None

        rate = percent_change / Decimal("100") if percent_change is not None else decimal_change
        if rate is None:
            return None
        denominator = Decimal("1") + rate
        if denominator <= 0:
            return None
        return close / denominator

    def _timestamp_from_entry(self, entry: Mapping[str, Any], *, generated_at: datetime | None = None) -> datetime:
        raw_value = str(entry.get("timestamp") or "").strip()
        if raw_value:
            try:
                return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            except ValueError:
                pass
        return generated_at or self.now_provider()

    def _metadata_timestamp(self, value: object) -> datetime | None:
        raw_value = str(value or "").strip()
        if not raw_value:
            return None
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _file_timestamp(self) -> datetime | None:
        try:
            return datetime.fromtimestamp(self.path.stat().st_mtime, timezone.utc)
        except OSError:
            return None

    def _now_utc(self) -> datetime:
        now = self.now_provider()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    @staticmethod
    def _minute_key(value: datetime) -> tuple[int, int, int, int, int]:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        return value.year, value.month, value.day, value.hour, value.minute

    def _current_minute_entries(
        self,
        entries: list[Mapping[str, Any]],
        generated_at: datetime | None,
    ) -> tuple[list[Mapping[str, Any]], int]:
        if not self.require_current_minute:
            return entries, 0
        current_minute = self._minute_key(self._now_utc())
        current_entries = [
            entry
            for entry in entries
            if self._minute_key(
                self._timestamp_from_entry(entry, generated_at=generated_at)
            )
            == current_minute
        ]
        return current_entries, len(entries) - len(current_entries)

    def _refresh_failure_retry_ready(self) -> bool:
        if self._last_refresh_failure_at is None or self.refresh_failure_retry_seconds <= 0:
            return True
        elapsed_seconds = (self._now_utc() - self._last_refresh_failure_at).total_seconds()
        return elapsed_seconds >= self.refresh_failure_retry_seconds

    def _snapshot_stale_message(self, generated_at: datetime | None) -> str | None:
        if generated_at is None:
            return None
        now_utc = self._now_utc()
        generated_utc = generated_at.astimezone(timezone.utc)
        if self.require_current_minute and (
            now_utc.year,
            now_utc.month,
            now_utc.day,
            now_utc.hour,
            now_utc.minute,
        ) != (
            generated_utc.year,
            generated_utc.month,
            generated_utc.day,
            generated_utc.hour,
            generated_utc.minute,
        ):
            return f"{self.kind}: stale scanner snapshot minute_mismatch=true"
        if self.max_snapshot_age_seconds is None or self.max_snapshot_age_seconds <= 0:
            return None
        age_seconds = (now_utc - generated_utc).total_seconds()
        if age_seconds <= float(self.max_snapshot_age_seconds):
            return None
        return f"{self.kind}: stale scanner snapshot age_seconds={age_seconds:.0f}"

    def _decimal_from_entry(
        self,
        entry: Mapping[str, Any],
        keys: Sequence[str],
        default: Decimal | None = None,
    ) -> Decimal | None:
        for key in keys:
            if key not in entry:
                continue
            try:
                return Decimal(str(entry[key]).replace(",", ""))
            except (InvalidOperation, TypeError, ValueError):
                return default
        return default

    def _safe_metadata_text(self, value: object, default: str) -> str:
        text = str(value or "").strip()
        if not text:
            return default
        lowered = text.lower()
        if any(token in lowered for token in _SENSITIVE_METADATA_TOKENS):
            return default
        if _ACCOUNT_LIKE_PATTERN.search(text):
            return default
        if "\\" in text or "/" in text:
            return default
        if len(text) > 60:
            return default
        safe = "".join(char for char in text if char.isalnum() or char in {" ", "-", "_", "."}).strip()
        return safe if safe and safe == text else default
