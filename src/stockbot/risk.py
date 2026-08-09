from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from .models import AccountSnapshot, Order


@dataclass(frozen=True)
class RiskConfig:
    max_order_amount: Decimal = Decimal("0")
    max_position_amount: Decimal = Decimal("300000")
    max_positions: int = 0
    max_daily_loss: Decimal = Decimal("100000")
    max_daily_entries_per_symbol: int = 1
    max_consecutive_order_failures: int = 3
    kill_switch: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_positions", int(self.max_positions))
        if self.max_positions < 0:
            raise ValueError("max_positions must be 0 or greater")


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str = ""


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.config = config
        self._entry_counts: dict[tuple[str, date], int] = {}
        self._consecutive_order_failures = 0

    def check(
        self,
        order: Order,
        account: AccountSnapshot,
        estimated_price: Decimal,
        as_of: date | None = None,
    ) -> RiskDecision:
        if order.quantity <= 0:
            return RiskDecision(False, "invalid_quantity")
        if estimated_price <= 0:
            return RiskDecision(False, "invalid_price")

        position = account.positions.get(order.symbol)

        if order.side in {"SELL", "SHORT_EXIT"}:
            if position is None or position.quantity < order.quantity:
                return RiskDecision(False, "insufficient_position")
            if order.side == "SELL" and position.side != "LONG":
                return RiskDecision(False, "position_side_conflict")
            if order.side == "SHORT_EXIT" and position.side != "SHORT":
                return RiskDecision(False, "position_side_conflict")
            return RiskDecision(True)

        if order.side not in {"BUY", "SHORT_ENTRY"}:
            return RiskDecision(False, "invalid_side")

        if self.config.kill_switch:
            return RiskDecision(False, "cleanup_mode_active")

        if self._consecutive_order_failures >= self.config.max_consecutive_order_failures:
            return RiskDecision(False, "order_failure_limit_reached")

        if not account.realized_pnl_today_known:
            return RiskDecision(False, "daily_realized_pnl_unknown")

        if _account_day_pnl(account) <= -self.config.max_daily_loss:
            return RiskDecision(False, "daily_loss_limit_reached")

        trading_date = as_of or date.today()
        if self.entry_limit_reached(order.symbol, trading_date):
            return RiskDecision(False, "max_daily_entries_reached")

        order_amount = estimated_price * order.quantity
        if order.side in {"BUY", "SHORT_ENTRY"} and order_amount > account.buying_power:
            return RiskDecision(False, "insufficient_cash")

        existing_amount = Decimal("0") if position is None else position.quantity * estimated_price
        if position is not None:
            if order.side == "BUY" and position.side != "LONG":
                return RiskDecision(False, "position_side_conflict")
            if order.side == "SHORT_ENTRY" and position.side != "SHORT":
                return RiskDecision(False, "position_side_conflict")
        if existing_amount + order_amount > self.config.max_position_amount:
            return RiskDecision(False, "max_position_amount_exceeded")

        if self.config.max_positions > 0 and position is None and len(account.positions) >= self.config.max_positions:
            return RiskDecision(False, "max_positions_reached")

        return RiskDecision(True)

    def record_entry(self, symbol: str, as_of: date | None = None) -> None:
        trading_date = as_of or date.today()
        key = (symbol, trading_date)
        self._entry_counts[key] = self._entry_counts.get(key, 0) + 1

    def restore_entry_counts(self, mapping: Mapping[object, object]) -> None:
        restored: dict[tuple[str, date], int] = {}
        if not isinstance(mapping, Mapping):
            self._entry_counts = restored
            return
        for raw_key, raw_count in mapping.items():
            if not isinstance(raw_key, tuple) or len(raw_key) != 2:
                continue
            symbol = _restored_symbol(raw_key[0])
            trading_date = _restored_date(raw_key[1])
            count = _restored_count(raw_count)
            if not symbol or trading_date is None or count is None or count == 0:
                continue
            key = (symbol, trading_date)
            restored[key] = max(restored.get(key, 0), count)
        self._entry_counts = restored

    def entry_limit_reached(self, symbol: str, as_of: date | None = None) -> bool:
        trading_date = as_of or date.today()
        return self._entry_counts.get((symbol, trading_date), 0) >= self.config.max_daily_entries_per_symbol

    def record_order_result(self, accepted: bool) -> None:
        if accepted:
            self._consecutive_order_failures = 0
        else:
            self._consecutive_order_failures += 1

    def reset_order_failures(self) -> None:
        self._consecutive_order_failures = 0


def _account_day_pnl(account: AccountSnapshot) -> Decimal:
    unrealized = sum(position.unrealized_pnl for position in account.positions.values())
    return account.realized_pnl_today + unrealized


def _restored_symbol(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _restored_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _restored_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed >= 0 else None
