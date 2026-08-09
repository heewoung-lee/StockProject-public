from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import Lock, RLock
from typing import Callable, Mapping, Protocol


MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION = 5
_ENTRY_COUNT_STATE_SCHEMA_VERSION = 2
_ACCOUNT_QUANTITY_CONFIRMATION_SCHEMA_VERSION = 5
_KST = timezone(timedelta(hours=9), "Asia/Seoul")
_LEDGER_PATH_LOCKS: dict[str, RLock] = {}
_LEDGER_PATH_LOCKS_GUARD = Lock()


@dataclass(frozen=True)
class PositionLifecycle:
    opened_at: datetime
    highest_price: Decimal
    lowest_price: Decimal


@dataclass(frozen=True)
class ManagedFillLedgerResult:
    applied_quantity: int
    entry_recorded: bool


@dataclass(frozen=True)
class ManagedProfitAggregate:
    period_start: datetime
    period_end: datetime
    realized_pnl: Decimal
    fill_count: int


@dataclass
class _LedgerDocument:
    positions: dict[str, int]
    account_quantity_confirmations: dict[str, int]
    consumed_fills: dict[str, int]
    consumed_notional_by_fill: dict[str, Decimal]
    realized_pnl_by_date: dict[str, Decimal]
    profit_history_by_hour: dict[datetime, ManagedProfitAggregate]
    position_lifecycle_by_symbol: dict[str, PositionLifecycle]
    entry_counts_by_date: dict[tuple[str, date], int]
    entry_count_unknown_dates: set[date]
    position_lifecycle_unknown_symbols: set[str]
    schema_version: int = MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION
    scope: str = ""


class ManagedLivePositionLedger(Protocol):
    is_durable: bool

    def ensure_ready(self) -> None:
        ...

    def quantity_for(self, symbol: str) -> int:
        ...

    def add(self, symbol: str, quantity: int) -> None:
        ...

    def subtract(self, symbol: str, quantity: int) -> None:
        ...

    def account_quantity_confirmation_for(self, symbol: str) -> int | None:
        ...

    def account_quantity_confirmations(self) -> dict[str, int]:
        ...

    def reconcile_account_quantity_confirmation(
        self,
        symbol: str,
        observed_quantity: int,
    ) -> bool:
        ...

    def lifecycle_for(self, symbol: str) -> PositionLifecycle | None:
        ...

    def initialize_lifecycle(
        self,
        symbol: str,
        opened_at: datetime | str,
        price: Decimal,
        *,
        preserve_unknown: bool = False,
    ) -> None:
        ...

    def update_lifecycle_price(self, symbol: str, timestamp: datetime | str, price: Decimal) -> None:
        ...

    def consumed_quantity_for(self, fill_key: str) -> int:
        ...

    def consumed_notional_for(self, fill_key: str) -> Decimal | None:
        ...

    def record_consumed_fill(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
    ) -> None:
        ...

    def all(self) -> dict[str, int]:
        ...

    def record_realized_pnl(self, trading_day: date | str, amount: Decimal) -> None:
        ...

    def realized_pnl_today(self, trading_day: date | str) -> Decimal:
        ...

    def profit_history(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[ManagedProfitAggregate, ...]:
        ...

    def daily_realized_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, Decimal]:
        ...

    def entry_counts(self) -> dict[tuple[str, date], int]:
        ...

    def entry_counts_are_known(self, trading_day: date | str) -> bool:
        ...

    def replace_entry_counts_for_date(
        self,
        trading_day: date | str,
        counts_by_symbol: Mapping[object, object],
    ) -> None:
        ...

    def position_lifecycle_is_known(self, symbol: str) -> bool:
        ...

    def record_entry(self, symbol: str, trading_day: date | str, count: int = 1) -> None:
        ...

    def record_fill_transaction(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
        timestamp: datetime | str,
        price: Decimal,
        realized_pnl: Decimal = Decimal("0"),
    ) -> ManagedFillLedgerResult:
        ...


def managed_live_position_ledger_scope(account_no: str, product_code: str) -> str:
    account_no = str(account_no or "").strip()
    product_code = str(product_code or "").strip()
    if not account_no or not product_code:
        return ""
    digest = hashlib.sha256(f"{account_no}:{product_code}".encode("utf-8")).hexdigest()
    return digest[:12]


def _ledger_path_lock(path: Path) -> RLock:
    key = str(path.resolve())
    with _LEDGER_PATH_LOCKS_GUARD:
        lock = _LEDGER_PATH_LOCKS.get(key)
        if lock is None:
            lock = RLock()
            _LEDGER_PATH_LOCKS[key] = lock
        return lock


class InMemoryManagedLivePositionLedger:
    is_durable = False

    def __init__(self, positions: Mapping[str, int] | None = None):
        self._lock = RLock()
        self._positions = {
            str(symbol): max(0, int(quantity))
            for symbol, quantity in (positions or {}).items()
            if int(quantity) > 0
        }
        self._account_quantity_confirmations: dict[str, int] = {}
        self._consumed_fills: dict[str, int] = {}
        self._consumed_notional_by_fill: dict[str, Decimal] = {}
        self._realized_pnl_by_date: dict[str, Decimal] = {}
        self._profit_history_by_hour: dict[datetime, ManagedProfitAggregate] = {}
        self._position_lifecycle_by_symbol: dict[str, PositionLifecycle] = {}
        self._entry_counts_by_date: dict[tuple[str, date], int] = {}
        self._entry_count_unknown_dates: set[date] = set()
        self._position_lifecycle_unknown_symbols: set[str] = set(self._positions)

    def ensure_ready(self) -> None:
        return None

    def quantity_for(self, symbol: str) -> int:
        return self._positions.get(symbol, 0)

    def add(self, symbol: str, quantity: int) -> None:
        parsed_quantity = max(0, int(quantity))
        if parsed_quantity <= 0:
            return
        with self._lock:
            key = _symbol_key(symbol)
            if self._positions.get(key, 0) <= 0 and key not in self._position_lifecycle_by_symbol:
                self._position_lifecycle_unknown_symbols.add(key)
            self._positions[key] = self._positions.get(key, 0) + parsed_quantity

    def subtract(self, symbol: str, quantity: int) -> None:
        parsed_quantity = max(0, int(quantity))
        if parsed_quantity <= 0:
            return
        with self._lock:
            key = _symbol_key(symbol)
            remaining = self._positions.get(key, 0) - parsed_quantity
            if remaining <= 0:
                self._positions.pop(key, None)
                self._position_lifecycle_by_symbol.pop(key, None)
                self._position_lifecycle_unknown_symbols.discard(key)
                return
            self._positions[key] = remaining

    def account_quantity_confirmation_for(self, symbol: str) -> int | None:
        return self._account_quantity_confirmations.get(_symbol_key(symbol))

    def account_quantity_confirmations(self) -> dict[str, int]:
        return dict(self._account_quantity_confirmations)

    def reconcile_account_quantity_confirmation(
        self,
        symbol: str,
        observed_quantity: int,
    ) -> bool:
        key = _symbol_key(symbol)
        observed = max(0, int(observed_quantity))
        with self._lock:
            expected = self._account_quantity_confirmations.get(key)
            if expected is None:
                return True
            if observed != expected:
                return False
            self._account_quantity_confirmations.pop(key, None)
            return True

    def lifecycle_for(self, symbol: str) -> PositionLifecycle | None:
        return self._position_lifecycle_by_symbol.get(str(symbol))

    def initialize_lifecycle(
        self,
        symbol: str,
        opened_at: datetime | str,
        price: Decimal,
        *,
        preserve_unknown: bool = False,
    ) -> None:
        with self._lock:
            key = _symbol_key(symbol)
            self._position_lifecycle_by_symbol[key] = _initialize_lifecycle(
                self._position_lifecycle_by_symbol.get(key),
                opened_at,
                price,
            )
            if not preserve_unknown:
                self._position_lifecycle_unknown_symbols.discard(key)

    def update_lifecycle_price(self, symbol: str, timestamp: datetime | str, price: Decimal) -> None:
        key = _symbol_key(symbol)
        self._position_lifecycle_by_symbol[key] = _update_lifecycle(
            self._position_lifecycle_by_symbol.get(key),
            timestamp,
            price,
        )

    def all(self) -> dict[str, int]:
        return dict(self._positions)

    def record_realized_pnl(self, trading_day: date | str, amount: Decimal) -> None:
        parsed_amount = Decimal(str(amount or "0"))
        if parsed_amount == 0:
            return
        key = _day_key(trading_day)
        self._realized_pnl_by_date[key] = self.realized_pnl_today(key) + parsed_amount

    def realized_pnl_today(self, trading_day: date | str) -> Decimal:
        return self._realized_pnl_by_date.get(_day_key(trading_day), Decimal("0"))

    def profit_history(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[ManagedProfitAggregate, ...]:
        with self._lock:
            return _profit_history_range(
                self._profit_history_by_hour,
                start_date,
                end_date,
            )

    def daily_realized_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, Decimal]:
        with self._lock:
            return _daily_realized_pnl_range(
                self._realized_pnl_by_date,
                start_date,
                end_date,
            )

    def entry_counts(self) -> dict[tuple[str, date], int]:
        return dict(self._entry_counts_by_date)

    def entry_counts_are_known(self, trading_day: date | str) -> bool:
        return _trading_date(trading_day) not in self._entry_count_unknown_dates

    def replace_entry_counts_for_date(
        self,
        trading_day: date | str,
        counts_by_symbol: Mapping[object, object],
    ) -> None:
        parsed_day = _trading_date(trading_day)
        replacement = _replacement_entry_counts(parsed_day, counts_by_symbol)
        with self._lock:
            self._entry_counts_by_date = {
                key: count
                for key, count in self._entry_counts_by_date.items()
                if key[1] != parsed_day
            }
            self._entry_counts_by_date.update(replacement)
            self._entry_count_unknown_dates.discard(parsed_day)

    def position_lifecycle_is_known(self, symbol: str) -> bool:
        return _symbol_key(symbol) not in self._position_lifecycle_unknown_symbols

    def record_entry(self, symbol: str, trading_day: date | str, count: int = 1) -> None:
        parsed_count = max(0, int(count))
        if parsed_count <= 0:
            return
        key = (_symbol_key(symbol), _trading_date(trading_day))
        self._entry_counts_by_date[key] = self._entry_counts_by_date.get(key, 0) + parsed_count

    def consumed_quantity_for(self, fill_key: str) -> int:
        return self._consumed_fills.get(str(fill_key), 0)

    def consumed_notional_for(self, fill_key: str) -> Decimal | None:
        key = str(fill_key)
        if self._consumed_fills.get(key, 0) <= 0:
            return Decimal("0")
        return self._consumed_notional_by_fill.get(key)

    def record_consumed_fill(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
    ) -> None:
        parsed_delta = max(0, int(quantity_delta))
        parsed_cumulative = max(0, int(cumulative_filled))
        if parsed_delta <= 0:
            key = str(fill_key)
            prior_quantity = self.consumed_quantity_for(key)
            self._consumed_fills[key] = max(prior_quantity, parsed_cumulative)
            if parsed_cumulative > prior_quantity:
                self._consumed_notional_by_fill.pop(key, None)
            return
        if side == "BUY":
            self.add(symbol, parsed_delta)
        elif side == "SELL":
            self.subtract(symbol, parsed_delta)
        else:
            raise ValueError("invalid managed live fill side")
        key = str(fill_key)
        self._consumed_fills[key] = max(self.consumed_quantity_for(key), parsed_cumulative)
        self._consumed_notional_by_fill.pop(key, None)
        self._account_quantity_confirmations[_symbol_key(symbol)] = self.quantity_for(symbol)

    def record_fill_transaction(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
        timestamp: datetime | str,
        price: Decimal,
        realized_pnl: Decimal = Decimal("0"),
    ) -> ManagedFillLedgerResult:
        with self._lock:
            key, symbol_key, normalized_side, parsed_delta, parsed_cumulative = _fill_values(
                fill_key,
                symbol,
                side,
                quantity_delta,
                cumulative_filled,
            )
            fill_timestamp = _datetime_value(timestamp)
            profit_timestamp = _kst_datetime(fill_timestamp)
            fill_price = _price_value(price)
            parsed_pnl = _realized_pnl_value(realized_pnl)
            already_consumed = self._consumed_fills.get(key, 0)
            target_consumed, effective_delta = _fill_consumption_delta(
                already_consumed,
                parsed_delta,
                parsed_cumulative,
            )
            prior_notional = self._consumed_notional_by_fill.get(key)
            if already_consumed <= 0:
                prior_notional = Decimal("0")
            if effective_delta > 0 and prior_notional is None:
                raise ValueError("managed live fill notional is unavailable")
            entry_recorded = False
            if effective_delta > 0 and normalized_side == "BUY":
                existing_quantity = self._positions.get(symbol_key, 0)
                self._positions[symbol_key] = existing_quantity + effective_delta
                self._position_lifecycle_by_symbol[symbol_key] = _initialize_lifecycle(
                    self._position_lifecycle_by_symbol.get(symbol_key),
                    fill_timestamp,
                    fill_price,
                )
                if existing_quantity <= 0:
                    self._position_lifecycle_unknown_symbols.discard(symbol_key)
                entry_recorded = already_consumed == 0
                if entry_recorded:
                    entry_key = (symbol_key, profit_timestamp.date())
                    self._entry_counts_by_date[entry_key] = self._entry_counts_by_date.get(entry_key, 0) + 1
            elif effective_delta > 0 and normalized_side == "SELL":
                remaining = self._positions.get(symbol_key, 0) - effective_delta
                if remaining <= 0:
                    self._positions.pop(symbol_key, None)
                    self._position_lifecycle_by_symbol.pop(symbol_key, None)
                    self._position_lifecycle_unknown_symbols.discard(symbol_key)
                else:
                    self._positions[symbol_key] = remaining
                if parsed_pnl != 0:
                    day_key = profit_timestamp.date().isoformat()
                    self._realized_pnl_by_date[day_key] = (
                        self._realized_pnl_by_date.get(day_key, Decimal("0")) + parsed_pnl
                    )
            if effective_delta > 0:
                self._account_quantity_confirmations[symbol_key] = self._positions.get(
                    symbol_key,
                    0,
                )
            if effective_delta > 0:
                _record_profit_fill(
                    self._profit_history_by_hour,
                    profit_timestamp,
                    parsed_pnl if normalized_side == "SELL" else Decimal("0"),
                )
            self._consumed_fills[key] = target_consumed
            if effective_delta > 0 and prior_notional is not None:
                self._consumed_notional_by_fill[key] = (
                    prior_notional + (fill_price * Decimal(effective_delta))
                )
            return ManagedFillLedgerResult(effective_delta, entry_recorded)


class JsonManagedLivePositionLedger:
    is_durable = True

    def __init__(
        self,
        path: str | Path,
        *,
        scope: str = "",
        trading_day_provider: Callable[[], date] = date.today,
    ):
        self.path = Path(path)
        self.scope = str(scope or "").strip()
        self._trading_day_provider = trading_day_provider
        self._lock = _ledger_path_lock(self.path)

    def ensure_ready(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                self._write_document(self._load_document())
                return
            self._write_document(_empty_document(self.scope))

    def quantity_for(self, symbol: str) -> int:
        return self._load_document().positions.get(symbol, 0)

    def add(self, symbol: str, quantity: int) -> None:
        parsed_quantity = max(0, int(quantity))
        if parsed_quantity <= 0:
            return
        with self._lock:
            document = self._load_document()
            key = _symbol_key(symbol)
            if document.positions.get(key, 0) <= 0 and key not in document.position_lifecycle_by_symbol:
                document.position_lifecycle_unknown_symbols.add(key)
            document.positions[key] = document.positions.get(key, 0) + parsed_quantity
            self._write_document(document)

    def subtract(self, symbol: str, quantity: int) -> None:
        parsed_quantity = max(0, int(quantity))
        if parsed_quantity <= 0:
            return
        with self._lock:
            document = self._load_document()
            key = _symbol_key(symbol)
            remaining = document.positions.get(key, 0) - parsed_quantity
            if remaining <= 0:
                document.positions.pop(key, None)
                document.position_lifecycle_by_symbol.pop(key, None)
                document.position_lifecycle_unknown_symbols.discard(key)
            else:
                document.positions[key] = remaining
            self._write_document(document)

    def account_quantity_confirmation_for(self, symbol: str) -> int | None:
        return self._load_document().account_quantity_confirmations.get(
            _symbol_key(symbol)
        )

    def account_quantity_confirmations(self) -> dict[str, int]:
        return dict(self._load_document().account_quantity_confirmations)

    def reconcile_account_quantity_confirmation(
        self,
        symbol: str,
        observed_quantity: int,
    ) -> bool:
        key = _symbol_key(symbol)
        observed = max(0, int(observed_quantity))
        with self._lock:
            document = self._load_document_unlocked()
            expected = document.account_quantity_confirmations.get(key)
            if expected is None:
                return True
            if observed != expected:
                return False
            document.account_quantity_confirmations.pop(key, None)
            self._write_document_unlocked(document)
            return True

    def lifecycle_for(self, symbol: str) -> PositionLifecycle | None:
        return self._load_document().position_lifecycle_by_symbol.get(str(symbol))

    def initialize_lifecycle(
        self,
        symbol: str,
        opened_at: datetime | str,
        price: Decimal,
        *,
        preserve_unknown: bool = False,
    ) -> None:
        with self._lock:
            document = self._load_document()
            key = _symbol_key(symbol)
            document.position_lifecycle_by_symbol[key] = _initialize_lifecycle(
                document.position_lifecycle_by_symbol.get(key),
                opened_at,
                price,
            )
            if not preserve_unknown:
                document.position_lifecycle_unknown_symbols.discard(key)
            self._write_document(document)

    def update_lifecycle_price(self, symbol: str, timestamp: datetime | str, price: Decimal) -> None:
        with self._lock:
            document = self._load_document()
            key = _symbol_key(symbol)
            document.position_lifecycle_by_symbol[key] = _update_lifecycle(
                document.position_lifecycle_by_symbol.get(key),
                timestamp,
                price,
            )
            self._write_document(document)

    def all(self) -> dict[str, int]:
        return self._load()

    def consumed_quantity_for(self, fill_key: str) -> int:
        return self._load_document().consumed_fills.get(str(fill_key), 0)

    def consumed_notional_for(self, fill_key: str) -> Decimal | None:
        document = self._load_document()
        key = str(fill_key)
        if document.consumed_fills.get(key, 0) <= 0:
            return Decimal("0")
        return document.consumed_notional_by_fill.get(key)

    def record_realized_pnl(self, trading_day: date | str, amount: Decimal) -> None:
        parsed_amount = Decimal(str(amount or "0"))
        if parsed_amount == 0:
            return
        with self._lock:
            document = self._load_document()
            key = _day_key(trading_day)
            document.realized_pnl_by_date[key] = (
                document.realized_pnl_by_date.get(key, Decimal("0")) + parsed_amount
            )
            self._write_document(document)

    def realized_pnl_today(self, trading_day: date | str) -> Decimal:
        return self._load_document().realized_pnl_by_date.get(_day_key(trading_day), Decimal("0"))

    def profit_history(
        self,
        start_date: date,
        end_date: date,
    ) -> tuple[ManagedProfitAggregate, ...]:
        _date_range(start_date, end_date)
        return _profit_history_range(
            self._load_document().profit_history_by_hour,
            start_date,
            end_date,
        )

    def daily_realized_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[date, Decimal]:
        _date_range(start_date, end_date)
        return _daily_realized_pnl_range(
            self._load_document().realized_pnl_by_date,
            start_date,
            end_date,
        )

    def entry_counts(self) -> dict[tuple[str, date], int]:
        return dict(self._load_document().entry_counts_by_date)

    def entry_counts_are_known(self, trading_day: date | str) -> bool:
        return _trading_date(trading_day) not in self._load_document().entry_count_unknown_dates

    def replace_entry_counts_for_date(
        self,
        trading_day: date | str,
        counts_by_symbol: Mapping[object, object],
    ) -> None:
        parsed_day = _trading_date(trading_day)
        replacement = _replacement_entry_counts(parsed_day, counts_by_symbol)
        with self._lock:
            document = self._load_document_unlocked()
            document.entry_counts_by_date = {
                key: count
                for key, count in document.entry_counts_by_date.items()
                if key[1] != parsed_day
            }
            document.entry_counts_by_date.update(replacement)
            document.entry_count_unknown_dates.discard(parsed_day)
            self._write_document_unlocked(document)

    def position_lifecycle_is_known(self, symbol: str) -> bool:
        return _symbol_key(symbol) not in self._load_document().position_lifecycle_unknown_symbols

    def record_entry(self, symbol: str, trading_day: date | str, count: int = 1) -> None:
        parsed_count = max(0, int(count))
        if parsed_count <= 0:
            return
        with self._lock:
            document = self._load_document()
            key = (_symbol_key(symbol), _trading_date(trading_day))
            document.entry_counts_by_date[key] = document.entry_counts_by_date.get(key, 0) + parsed_count
            self._write_document(document)

    def record_consumed_fill(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
    ) -> None:
        with self._lock:
            document = self._load_document()
            parsed_delta = max(0, int(quantity_delta))
            parsed_cumulative = max(0, int(cumulative_filled))
            key = str(fill_key)
            already_consumed = document.consumed_fills.get(key, 0)
            target_consumed = max(already_consumed, parsed_cumulative)
            effective_delta = min(parsed_delta, max(0, target_consumed - already_consumed))
            if effective_delta > 0:
                symbol_key = _symbol_key(symbol)
                if side == "BUY":
                    if (
                        document.positions.get(symbol_key, 0) <= 0
                        and symbol_key not in document.position_lifecycle_by_symbol
                    ):
                        document.position_lifecycle_unknown_symbols.add(symbol_key)
                    document.positions[symbol_key] = document.positions.get(symbol_key, 0) + effective_delta
                elif side == "SELL":
                    remaining = document.positions.get(symbol_key, 0) - effective_delta
                    if remaining <= 0:
                        document.positions.pop(symbol_key, None)
                        document.position_lifecycle_by_symbol.pop(symbol_key, None)
                        document.position_lifecycle_unknown_symbols.discard(symbol_key)
                    else:
                        document.positions[symbol_key] = remaining
                else:
                    raise ValueError("invalid managed live fill side")
                document.account_quantity_confirmations[symbol_key] = (
                    document.positions.get(symbol_key, 0)
                )
            document.consumed_fills[key] = target_consumed
            document.consumed_notional_by_fill.pop(key, None)
            self._write_document(document)

    def record_fill_transaction(
        self,
        *,
        fill_key: str,
        symbol: str,
        side: str,
        quantity_delta: int,
        cumulative_filled: int,
        timestamp: datetime | str,
        price: Decimal,
        realized_pnl: Decimal = Decimal("0"),
    ) -> ManagedFillLedgerResult:
        with self._lock:
            key, symbol_key, normalized_side, parsed_delta, parsed_cumulative = _fill_values(
                fill_key,
                symbol,
                side,
                quantity_delta,
                cumulative_filled,
            )
            fill_timestamp = _datetime_value(timestamp)
            profit_timestamp = _kst_datetime(fill_timestamp)
            fill_price = _price_value(price)
            parsed_pnl = _realized_pnl_value(realized_pnl)

            document = self._load_document()
            already_consumed = document.consumed_fills.get(key, 0)
            target_consumed, effective_delta = _fill_consumption_delta(
                already_consumed,
                parsed_delta,
                parsed_cumulative,
            )
            entry_recorded = False
            prior_notional = document.consumed_notional_by_fill.get(key)
            if already_consumed <= 0:
                prior_notional = Decimal("0")
            if effective_delta > 0 and prior_notional is None:
                raise ValueError("managed live fill notional is unavailable")

            if effective_delta > 0 and normalized_side == "BUY":
                existing_quantity = document.positions.get(symbol_key, 0)
                document.positions[symbol_key] = existing_quantity + effective_delta
                document.position_lifecycle_by_symbol[symbol_key] = _initialize_lifecycle(
                    document.position_lifecycle_by_symbol.get(symbol_key),
                    fill_timestamp,
                    fill_price,
                )
                if existing_quantity <= 0:
                    document.position_lifecycle_unknown_symbols.discard(symbol_key)
                entry_recorded = already_consumed == 0
                if entry_recorded:
                    entry_key = (symbol_key, profit_timestamp.date())
                    document.entry_counts_by_date[entry_key] = (
                        document.entry_counts_by_date.get(entry_key, 0) + 1
                    )
            elif effective_delta > 0 and normalized_side == "SELL":
                remaining = document.positions.get(symbol_key, 0) - effective_delta
                if remaining <= 0:
                    document.positions.pop(symbol_key, None)
                    document.position_lifecycle_by_symbol.pop(symbol_key, None)
                    document.position_lifecycle_unknown_symbols.discard(symbol_key)
                else:
                    document.positions[symbol_key] = remaining
                if parsed_pnl != 0:
                    day_key = profit_timestamp.date().isoformat()
                    document.realized_pnl_by_date[day_key] = (
                        document.realized_pnl_by_date.get(day_key, Decimal("0")) + parsed_pnl
                    )
            if effective_delta > 0:
                document.account_quantity_confirmations[symbol_key] = (
                    document.positions.get(symbol_key, 0)
                )
            if effective_delta > 0:
                _record_profit_fill(
                    document.profit_history_by_hour,
                    profit_timestamp,
                    parsed_pnl if normalized_side == "SELL" else Decimal("0"),
                )

            document.consumed_fills[key] = target_consumed
            if effective_delta > 0 and prior_notional is not None:
                document.consumed_notional_by_fill[key] = (
                    prior_notional + (fill_price * Decimal(effective_delta))
                )
            self._write_document(document)
            return ManagedFillLedgerResult(effective_delta, entry_recorded)

    def _load(self) -> dict[str, int]:
        return dict(self._load_document().positions)

    def _load_document(self) -> _LedgerDocument:
        with self._lock:
            return self._load_document_unlocked()

    def _load_document_unlocked(self) -> _LedgerDocument:
        if not self.path.exists():
            return _empty_document(self.scope)
        try:
            root_payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid managed live position ledger: {self.path}") from exc
        if not isinstance(root_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")

        saved_scope = ""
        consumed_payload: object = {}
        account_quantity_confirmation_payload: object = {}
        consumed_notional_payload: object = {}
        realized_payload: object = {}
        profit_history_payload: object = {}
        lifecycle_payload: object = {}
        entry_counts_payload: object = {}
        unknown_dates_payload: object = []
        unknown_lifecycle_payload: object = []
        schema_version = 0
        structured_payload = "positions" in root_payload
        if structured_payload:
            raw_scope = root_payload.get("scope", "")
            if not isinstance(raw_scope, str):
                raise ValueError(f"invalid managed live position ledger: {self.path}")
            saved_scope = raw_scope.strip()
            if self.scope and saved_scope != self.scope:
                raise ValueError(f"managed live position ledger scope mismatch: {self.path}")
            raw_schema_version = root_payload.get("schema_version", 0)
            if isinstance(raw_schema_version, bool) or not isinstance(raw_schema_version, int):
                raise ValueError(f"invalid managed live position ledger: {self.path}")
            schema_version = raw_schema_version
            if schema_version < 0 or schema_version > MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION:
                raise ValueError(f"unsupported managed live position ledger schema: {self.path}")
            consumed_payload = root_payload.get("consumed_fills", {})
            account_quantity_confirmation_payload = root_payload.get(
                "account_quantity_confirmations",
                {},
            )
            consumed_notional_payload = root_payload.get("consumed_notional_by_fill", {})
            realized_payload = root_payload.get("realized_pnl_by_date", {})
            profit_history_payload = root_payload.get("profit_history_by_hour", {})
            lifecycle_payload = root_payload.get("position_lifecycle_by_symbol", {})
            entry_counts_payload = root_payload.get("entry_counts_by_date", {})
            unknown_dates_payload = root_payload.get("entry_count_unknown_dates", [])
            unknown_lifecycle_payload = root_payload.get("position_lifecycle_unknown_symbols", [])
            positions_payload = root_payload.get("positions")
            if not isinstance(positions_payload, dict):
                raise ValueError(f"invalid managed live position ledger: {self.path}")
            required_current_fields = {
                "consumed_fills",
                "account_quantity_confirmations",
                "consumed_notional_by_fill",
                "realized_pnl_by_date",
                "profit_history_by_hour",
                "position_lifecycle_by_symbol",
                "entry_counts_by_date",
                "entry_count_unknown_dates",
                "position_lifecycle_unknown_symbols",
            }
            if (
                schema_version == MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION
                and not required_current_fields.issubset(root_payload)
            ):
                raise ValueError(f"invalid managed live position ledger: {self.path}")
        else:
            positions_payload = root_payload
        if not structured_payload and self.scope and positions_payload:
            raise ValueError(f"legacy managed live position ledger requires manual reconciliation: {self.path}")
        if not isinstance(consumed_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(account_quantity_confirmation_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(consumed_notional_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(realized_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(profit_history_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(lifecycle_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(entry_counts_payload, dict):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(unknown_dates_payload, list):
            raise ValueError(f"invalid managed live position ledger: {self.path}")
        if not isinstance(unknown_lifecycle_payload, list):
            raise ValueError(f"invalid managed live position ledger: {self.path}")

        try:
            positions: dict[str, int] = {}
            for symbol, quantity in positions_payload.items():
                parsed_quantity = int(quantity)
                if parsed_quantity > 0:
                    positions[_symbol_key(symbol)] = parsed_quantity
            consumed_fills: dict[str, int] = {}
            for order_no, quantity in consumed_payload.items():
                parsed_quantity = int(quantity)
                if parsed_quantity > 0:
                    consumed_fills[str(order_no)] = parsed_quantity
            account_quantity_confirmations: dict[str, int] = {}
            for symbol, quantity in account_quantity_confirmation_payload.items():
                if isinstance(quantity, bool):
                    raise ValueError("invalid account quantity confirmation")
                parsed_quantity = int(quantity)
                if parsed_quantity < 0:
                    raise ValueError("invalid account quantity confirmation")
                account_quantity_confirmations[_symbol_key(symbol)] = parsed_quantity
            if schema_version == _ACCOUNT_QUANTITY_CONFIRMATION_SCHEMA_VERSION - 1:
                for fill_key in consumed_fills:
                    symbol = _symbol_from_managed_fill_key(fill_key)
                    if not symbol:
                        raise ValueError(
                            "managed fill key cannot be migrated to account confirmation"
                        )
                    account_quantity_confirmations.setdefault(
                        symbol,
                        positions.get(symbol, 0),
                    )
            consumed_notional_by_fill: dict[str, Decimal] = {}
            for fill_key, amount in consumed_notional_payload.items():
                key = str(fill_key)
                parsed_amount = Decimal(str(amount))
                if not parsed_amount.is_finite() or parsed_amount < 0:
                    raise ValueError("invalid consumed fill notional")
                if key in consumed_fills:
                    consumed_notional_by_fill[key] = parsed_amount
            realized_pnl_by_date: dict[str, Decimal] = {}
            for day, amount in realized_payload.items():
                parsed_amount = Decimal(str(amount or "0"))
                if not parsed_amount.is_finite():
                    raise ValueError("invalid realized pnl")
                if parsed_amount != 0:
                    realized_pnl_by_date[_day_key(str(day))] = parsed_amount
            profit_history_by_hour = _profit_history_map(profit_history_payload)
            position_lifecycle_by_symbol = _lifecycle_map(lifecycle_payload)
            entry_counts_by_date = _entry_count_map(entry_counts_payload)
            entry_count_unknown_dates = {
                _trading_date(item)
                for item in unknown_dates_payload
            }
            position_lifecycle_unknown_symbols = {
                _symbol_key(item)
                for item in unknown_lifecycle_payload
            }
            if schema_version < _ENTRY_COUNT_STATE_SCHEMA_VERSION:
                entry_count_unknown_dates.add(_trading_date(self._trading_day_provider()))
            position_lifecycle_unknown_symbols.update(
                set(positions) - set(position_lifecycle_by_symbol)
            )
            position_lifecycle_unknown_symbols.intersection_update(positions)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"invalid managed live position ledger: {self.path}") from exc
        return _LedgerDocument(
            positions=positions,
            account_quantity_confirmations=account_quantity_confirmations,
            consumed_fills=consumed_fills,
            consumed_notional_by_fill=consumed_notional_by_fill,
            realized_pnl_by_date=realized_pnl_by_date,
            profit_history_by_hour=profit_history_by_hour,
            position_lifecycle_by_symbol=position_lifecycle_by_symbol,
            entry_counts_by_date=entry_counts_by_date,
            entry_count_unknown_dates=entry_count_unknown_dates,
            position_lifecycle_unknown_symbols=position_lifecycle_unknown_symbols,
            schema_version=MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION,
            scope=saved_scope or self.scope,
        )

    def _write(self, positions: Mapping[str, int]) -> None:
        with self._lock:
            document = self._load_document()
            document.positions = dict(positions)
            document.position_lifecycle_by_symbol = {
                symbol: lifecycle
                for symbol, lifecycle in document.position_lifecycle_by_symbol.items()
                if int(document.positions.get(symbol, 0)) > 0
            }
            document.position_lifecycle_unknown_symbols.intersection_update(document.positions)
            self._write_document(document)

    def _write_document(self, document: _LedgerDocument) -> None:
        with self._lock:
            self._write_document_unlocked(document)

    def _write_document_unlocked(self, document: _LedgerDocument) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        position_payload = {
            str(symbol): max(0, int(quantity))
            for symbol, quantity in document.positions.items()
            if int(quantity) > 0
        }
        entry_counts_payload: dict[str, dict[str, int]] = {}
        for (symbol, trading_day), count in document.entry_counts_by_date.items():
            parsed_count = max(0, int(count))
            if parsed_count <= 0:
                continue
            day_key = _trading_date(trading_day).isoformat()
            entry_counts_payload.setdefault(day_key, {})[_symbol_key(symbol)] = parsed_count
        payload = {
            "schema_version": MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION,
            "positions": position_payload,
            "account_quantity_confirmations": {
                _symbol_key(symbol): max(0, int(quantity))
                for symbol, quantity in document.account_quantity_confirmations.items()
            },
            "consumed_fills": {
                str(order_no): max(0, int(quantity))
                for order_no, quantity in document.consumed_fills.items()
                if int(quantity) > 0
            },
            "consumed_notional_by_fill": {
                str(fill_key): str(Decimal(str(amount)))
                for fill_key, amount in document.consumed_notional_by_fill.items()
                if fill_key in document.consumed_fills
            },
            "realized_pnl_by_date": {
                _day_key(day): str(Decimal(str(amount)))
                for day, amount in document.realized_pnl_by_date.items()
                if Decimal(str(amount)) != 0
            },
            "profit_history_by_hour": {
                period_start.isoformat(): {
                    "realized_pnl": str(aggregate.realized_pnl),
                    "fill_count": aggregate.fill_count,
                }
                for period_start, aggregate in sorted(document.profit_history_by_hour.items())
                if aggregate.fill_count > 0
            },
            "position_lifecycle_by_symbol": {
                _symbol_key(symbol): {
                    "opened_at": lifecycle.opened_at.isoformat(),
                    "highest_price": str(lifecycle.highest_price),
                    "lowest_price": str(lifecycle.lowest_price),
                }
                for symbol, lifecycle in document.position_lifecycle_by_symbol.items()
            },
            "entry_counts_by_date": entry_counts_payload,
            "entry_count_unknown_dates": sorted(
                _trading_date(trading_day).isoformat()
                for trading_day in document.entry_count_unknown_dates
            ),
            "position_lifecycle_unknown_symbols": sorted(
                _symbol_key(symbol)
                for symbol in document.position_lifecycle_unknown_symbols
                if symbol in position_payload
            ),
        }
        saved_scope = self.scope or document.scope
        if saved_scope:
            payload["scope"] = saved_scope
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)


def _empty_document(scope: str = "") -> _LedgerDocument:
    return _LedgerDocument(
        positions={},
        account_quantity_confirmations={},
        consumed_fills={},
        consumed_notional_by_fill={},
        realized_pnl_by_date={},
        profit_history_by_hour={},
        position_lifecycle_by_symbol={},
        entry_counts_by_date={},
        entry_count_unknown_dates=set(),
        position_lifecycle_unknown_symbols=set(),
        schema_version=MANAGED_LIVE_POSITION_LEDGER_SCHEMA_VERSION,
        scope=scope,
    )


def _lifecycle_map(payload: Mapping[object, object]) -> dict[str, PositionLifecycle]:
    lifecycles: dict[str, PositionLifecycle] = {}
    for symbol, item in payload.items():
        if not isinstance(item, dict):
            raise ValueError("invalid position lifecycle")
        key = _symbol_key(symbol)
        opened_at = _datetime_value(item.get("opened_at"))
        highest_price = _price_value(item.get("highest_price"))
        lowest_price = _price_value(item.get("lowest_price"))
        if highest_price < lowest_price:
            raise ValueError("invalid position lifecycle extrema")
        lifecycles[key] = PositionLifecycle(opened_at, highest_price, lowest_price)
    return lifecycles


def _entry_count_map(payload: Mapping[object, object]) -> dict[tuple[str, date], int]:
    counts: dict[tuple[str, date], int] = {}
    for trading_day, symbol_counts in payload.items():
        parsed_day = _trading_date(trading_day)
        if not isinstance(symbol_counts, dict):
            raise ValueError("invalid entry count record")
        for symbol, count in symbol_counts.items():
            parsed_count = _nonnegative_int(count)
            if parsed_count > 0:
                counts[(_symbol_key(symbol), parsed_day)] = parsed_count
    return counts


def _profit_history_map(
    payload: Mapping[object, object],
) -> dict[datetime, ManagedProfitAggregate]:
    history: dict[datetime, ManagedProfitAggregate] = {}
    for period_start_value, item in payload.items():
        if not isinstance(item, dict):
            raise ValueError("invalid managed profit aggregate")
        period_start = _hour_start(period_start_value)
        if period_start in history:
            raise ValueError("duplicate managed profit aggregate")
        realized_pnl = _realized_pnl_value(item.get("realized_pnl", "0"))
        fill_count = _nonnegative_int(item.get("fill_count", 0))
        if fill_count <= 0:
            raise ValueError("managed profit fill count must be positive")
        history[period_start] = ManagedProfitAggregate(
            period_start=period_start,
            period_end=period_start + timedelta(hours=1),
            realized_pnl=realized_pnl,
            fill_count=fill_count,
        )
    return history


def _record_profit_fill(
    history: dict[datetime, ManagedProfitAggregate],
    timestamp: datetime | str,
    realized_pnl: Decimal,
) -> None:
    period_start = _hour_start(timestamp)
    existing = history.get(period_start)
    history[period_start] = ManagedProfitAggregate(
        period_start=period_start,
        period_end=period_start + timedelta(hours=1),
        realized_pnl=(existing.realized_pnl if existing else Decimal("0")) + realized_pnl,
        fill_count=(existing.fill_count if existing else 0) + 1,
    )


def _profit_history_range(
    history: Mapping[datetime, ManagedProfitAggregate],
    start_date: date,
    end_date: date,
) -> tuple[ManagedProfitAggregate, ...]:
    parsed_start, parsed_end = _date_range(start_date, end_date)
    return tuple(
        history[period_start]
        for period_start in sorted(history)
        if parsed_start <= period_start.date() <= parsed_end
    )


def _daily_realized_pnl_range(
    realized_pnl_by_date: Mapping[str, Decimal],
    start_date: date,
    end_date: date,
) -> dict[date, Decimal]:
    parsed_start, parsed_end = _date_range(start_date, end_date)
    return {
        trading_day: realized_pnl_by_date[day_key]
        for day_key in sorted(realized_pnl_by_date)
        if parsed_start <= (trading_day := _trading_date(day_key)) <= parsed_end
    }


def _date_range(start_date: date, end_date: date) -> tuple[date, date]:
    parsed_start = _trading_date(start_date)
    parsed_end = _trading_date(end_date)
    if parsed_start > parsed_end:
        raise ValueError("profit history start date must be on or before end date")
    return parsed_start, parsed_end


def _replacement_entry_counts(
    trading_day: date,
    counts_by_symbol: Mapping[object, object],
) -> dict[tuple[str, date], int]:
    if not isinstance(counts_by_symbol, Mapping):
        raise ValueError("entry count reconciliation must be a mapping")
    replacement: dict[tuple[str, date], int] = {}
    for symbol, count in counts_by_symbol.items():
        parsed_count = _nonnegative_int(count)
        if parsed_count > 0:
            replacement[(_symbol_key(symbol), trading_day)] = parsed_count
    return replacement


def _initialize_lifecycle(
    existing: PositionLifecycle | None,
    opened_at: datetime | str,
    price: Decimal,
) -> PositionLifecycle:
    parsed_opened_at = _datetime_value(opened_at)
    parsed_price = _price_value(price)
    if existing is None:
        return PositionLifecycle(parsed_opened_at, parsed_price, parsed_price)
    try:
        earliest_opened_at = min(existing.opened_at, parsed_opened_at)
    except TypeError as exc:
        raise ValueError("incompatible position lifecycle timestamps") from exc
    return PositionLifecycle(
        opened_at=earliest_opened_at,
        highest_price=max(existing.highest_price, parsed_price),
        lowest_price=min(existing.lowest_price, parsed_price),
    )


def _update_lifecycle(
    existing: PositionLifecycle | None,
    timestamp: datetime | str,
    price: Decimal,
) -> PositionLifecycle:
    parsed_timestamp = _datetime_value(timestamp)
    parsed_price = _price_value(price)
    if existing is None:
        return PositionLifecycle(parsed_timestamp, parsed_price, parsed_price)
    return PositionLifecycle(
        opened_at=existing.opened_at,
        highest_price=max(existing.highest_price, parsed_price),
        lowest_price=min(existing.lowest_price, parsed_price),
    )


def _fill_values(
    fill_key: object,
    symbol: object,
    side: object,
    quantity_delta: object,
    cumulative_filled: object,
) -> tuple[str, str, str, int, int]:
    key = str(fill_key or "").strip()
    if not key:
        raise ValueError("managed live fill key is required")
    symbol_key = _symbol_key(symbol)
    normalized_side = str(side or "").strip().upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("invalid managed live fill side")
    parsed_delta = max(0, int(quantity_delta))
    parsed_cumulative = max(0, int(cumulative_filled))
    return key, symbol_key, normalized_side, parsed_delta, parsed_cumulative


def _fill_consumption_delta(
    already_consumed: int,
    quantity_delta: int,
    cumulative_filled: int,
) -> tuple[int, int]:
    target_consumed = max(already_consumed, cumulative_filled)
    effective_delta = max(0, target_consumed - already_consumed)
    if effective_delta > 0 and quantity_delta != effective_delta:
        raise ValueError("managed live fill delta does not match cumulative quantity")
    return target_consumed, effective_delta


def _symbol_key(symbol: object) -> str:
    value = str(symbol or "")
    if not value.strip():
        raise ValueError("position symbol is required")
    return value


def _symbol_from_managed_fill_key(fill_key: object) -> str:
    parts = str(fill_key or "").rsplit(":", 2)
    if len(parts) != 3 or parts[2].upper() not in {"BUY", "SELL"}:
        return ""
    return parts[1].strip()


def _datetime_value(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("position lifecycle timestamp is required")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _kst_datetime(value: datetime | str) -> datetime:
    parsed = _datetime_value(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=_KST)
    return parsed.astimezone(_KST)


def _hour_start(value: datetime | str | object) -> datetime:
    return _kst_datetime(value).replace(minute=0, second=0, microsecond=0)


def _realized_pnl_value(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value or "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("managed live fill realized pnl is invalid") from exc
    if not parsed.is_finite():
        raise ValueError("managed live fill realized pnl is invalid")
    return parsed


def _price_value(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("position lifecycle price must be positive") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("position lifecycle price must be positive")
    return parsed


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("entry count must be a nonnegative integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("entry count must be a nonnegative integer")
        parsed = int(text)
    else:
        raise ValueError("entry count must be a nonnegative integer")
    if parsed < 0:
        raise ValueError("entry count must be a nonnegative integer")
    return parsed


def _trading_date(trading_day: date | str | object) -> date:
    if isinstance(trading_day, datetime):
        return trading_day.date()
    if isinstance(trading_day, date):
        return trading_day
    value = str(trading_day or "").strip()
    if not value:
        raise ValueError("trading day is required")
    return date.fromisoformat(value)


def _day_key(trading_day: date | str) -> str:
    if isinstance(trading_day, date):
        return trading_day.isoformat()
    value = str(trading_day or "").strip()
    if not value:
        return date.today().isoformat()
    return value[:10]
