from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Optional


def money(value: Decimal | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    vwap: Decimal
    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None
    upper_limit: Optional[Decimal] = None
    lower_limit: Optional[Decimal] = None
    market: str = ""
    temporary_stop: Optional[bool] = None
    vi_code: str = ""
    security_status_code: str = ""
    trading_state_source: str = ""

    @property
    def buy_price(self) -> Decimal:
        return self.ask if self.ask is not None else self.close

    @property
    def sell_price(self) -> Decimal:
        return self.bid if self.bid is not None else self.close

    @property
    def spread_bps(self) -> Decimal:
        if self.bid is None or self.ask is None:
            return Decimal("0")
        mid = (self.bid + self.ask) / Decimal("2")
        if mid <= 0:
            return Decimal("0")
        return ((self.ask - self.bid) / mid) * Decimal("10000")


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    reason: str

    @classmethod
    def buy(cls, symbol: str, reason: str) -> "Signal":
        return cls(symbol=symbol, side="BUY", reason=reason)

    @classmethod
    def sell(cls, symbol: str, reason: str) -> "Signal":
        return cls(symbol=symbol, side="SELL", reason=reason)

    @classmethod
    def short(cls, symbol: str, reason: str) -> "Signal":
        return cls(symbol=symbol, side="SHORT_ENTRY", reason=reason)

    @classmethod
    def cover(cls, symbol: str, reason: str) -> "Signal":
        return cls(symbol=symbol, side="SHORT_EXIT", reason=reason)


@dataclass(frozen=True)
class Order:
    symbol: str
    side: str
    quantity: int
    reason: str
    order_type: str = "MARKET"

    @classmethod
    def buy(cls, symbol: str, quantity: int, reason: str) -> "Order":
        return cls(symbol=symbol, side="BUY", quantity=quantity, reason=reason)

    @classmethod
    def sell(cls, symbol: str, quantity: int, reason: str) -> "Order":
        return cls(symbol=symbol, side="SELL", quantity=quantity, reason=reason)

    @classmethod
    def short(cls, symbol: str, quantity: int, reason: str) -> "Order":
        return cls(symbol=symbol, side="SHORT_ENTRY", quantity=quantity, reason=reason)

    @classmethod
    def cover(cls, symbol: str, quantity: int, reason: str) -> "Order":
        return cls(symbol=symbol, side="SHORT_EXIT", quantity=quantity, reason=reason)


@dataclass(frozen=True)
class Fill:
    order: Order
    accepted: bool
    timestamp: datetime
    price: Decimal = Decimal("0")
    quantity: int = 0
    reject_reason: str = ""
    realized_pnl: Decimal = Decimal("0")
    pending_order_tracked: bool = False
    requires_cycle_pause: bool = False


@dataclass
class Position:
    symbol: str
    quantity: int
    avg_price: Decimal
    last_price: Decimal
    opened_at: datetime
    highest_price: Decimal
    side: str = "LONG"
    lowest_price: Decimal | None = None
    price_history: tuple[tuple[datetime, Decimal], ...] = field(default_factory=tuple)
    sellable_quantity: int | None = None
    managed_quantity: int | None = None

    def __post_init__(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("position side must be LONG or SHORT")
        if self.sellable_quantity is not None:
            self.sellable_quantity = max(0, int(self.sellable_quantity))
        if self.managed_quantity is not None:
            self.managed_quantity = max(0, int(self.managed_quantity))
        if self.lowest_price is None:
            self.lowest_price = self.last_price
        if not self.price_history:
            self.price_history = ((self.opened_at, self.last_price),)
        else:
            self.price_history = tuple((timestamp, money(price)) for timestamp, price in self.price_history)

    @property
    def market_value(self) -> Decimal:
        if self.side == "SHORT":
            return -(self.last_price * self.quantity)
        return self.last_price * self.quantity

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.side == "SHORT":
            return (self.avg_price - self.last_price) * self.quantity
        return (self.last_price - self.avg_price) * self.quantity


@dataclass(frozen=True)
class AccountSnapshot:
    cash: Decimal
    positions: dict[str, Position] = field(default_factory=dict)
    realized_pnl_today: Decimal = Decimal("0")
    equity_override: Decimal | None = None
    buying_power_override: Decimal | None = None
    realized_pnl_today_known: bool = True

    @property
    def equity(self) -> Decimal:
        if self.equity_override is not None:
            return max(Decimal("0"), money(self.equity_override))
        return self.cash + sum(position.market_value for position in self.positions.values())

    @property
    def short_proceeds(self) -> Decimal:
        return sum(
            position.avg_price * position.quantity
            for position in self.positions.values()
            if position.side == "SHORT"
        )

    @property
    def free_cash(self) -> Decimal:
        return self.cash - (self.short_proceeds * Decimal("2"))

    @property
    def buying_power(self) -> Decimal:
        if self.buying_power_override is not None:
            orderable = max(Decimal("0"), money(self.buying_power_override))
            return min(orderable, max(Decimal("0"), self.equity))
        capped = min(self.free_cash, self.equity)
        return max(Decimal("0"), capped)
