from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .broker import PaperBroker
from .journal import CsvTradeJournal
from .models import MarketBar, Order, Signal
from .risk import RiskManager


@dataclass(frozen=True)
class ExecutionSettings:
    order_cash_amount: Decimal = Decimal("50000")


class ExecutionEngine:
    def __init__(
        self,
        broker: PaperBroker,
        strategy,
        risk_manager: RiskManager,
        journal: CsvTradeJournal,
        settings: ExecutionSettings,
    ):
        self.broker = broker
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.journal = journal
        self.settings = settings

    def run(self, bars: Iterable[MarketBar]) -> None:
        for bar in bars:
            self.broker.update_market(bar)
            account = self.broker.snapshot()
            for signal in self.strategy.on_bar(bar, account):
                order = self._order_from_signal(signal, bar, account)
                estimated_price = self._estimated_price(order, bar)
                decision = self.risk_manager.check(
                    order,
                    self.broker.snapshot(),
                    estimated_price,
                    as_of=bar.timestamp.date(),
                )
                if not decision.approved:
                    self.journal.record_reject(order, bar, self.broker.snapshot(), decision.reason, estimated_price)
                    continue

                fill = self.broker.place_order(order, bar)
                self.risk_manager.record_order_result(fill.accepted)
                if fill.accepted:
                    if fill.order.side in {"BUY", "SHORT_ENTRY"}:
                        self.risk_manager.record_entry(fill.order.symbol, fill.timestamp.date())
                    self.journal.record_fill(fill, self.broker.snapshot())
                else:
                    self.journal.record_reject(order, bar, self.broker.snapshot(), fill.reject_reason, estimated_price)

    def _order_from_signal(self, signal: Signal, bar: MarketBar, account) -> Order:
        return order_from_signal(signal, bar, account, self.settings)

    @staticmethod
    def _estimated_price(order: Order, bar: MarketBar) -> Decimal:
        return estimated_order_price(order, bar)


def order_from_signal(signal: Signal, bar: MarketBar, account, settings: ExecutionSettings) -> Order:
    if signal.side == "SELL":
        position = account.positions.get(signal.symbol)
        quantity = 0 if position is None else _exit_quantity(position)
        return Order.sell(signal.symbol, quantity, signal.reason)

    if signal.side == "SHORT_EXIT":
        position = account.positions.get(signal.symbol)
        quantity = 0 if position is None else _exit_quantity(position)
        return Order.cover(signal.symbol, quantity, signal.reason)

    if signal.side == "SHORT_ENTRY":
        price = bar.sell_price
        quantity = int(settings.order_cash_amount / price) if price > 0 else 0
        return Order.short(signal.symbol, quantity, signal.reason)

    price = bar.buy_price
    quantity = int(settings.order_cash_amount / price) if price > 0 else 0
    return Order.buy(signal.symbol, quantity, signal.reason)


def _exit_quantity(position) -> int:
    managed_quantity = getattr(position, "managed_quantity", None)
    if managed_quantity is not None:
        return max(0, min(position.quantity, int(managed_quantity)))
    return position.quantity


def estimated_order_price(order: Order, bar: MarketBar) -> Decimal:
    if order.side in {"BUY", "SHORT_EXIT"}:
        return bar.buy_price
    return bar.sell_price
