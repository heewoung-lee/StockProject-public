from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

from .models import AccountSnapshot, Fill, MarketBar, Order, Position, money

MAX_POSITION_PRICE_HISTORY = 80


class PaperBroker:
    def __init__(self, initial_cash: Decimal | int | str, allow_short: bool = False):
        self._cash = money(initial_cash)
        self._allow_short = allow_short
        self._positions: dict[str, Position] = {}
        self._realized_pnl_today = Decimal("0")

    def snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            cash=self._cash,
            positions=deepcopy(self._positions),
            realized_pnl_today=self._realized_pnl_today,
        )

    def set_allow_short(self, allow_short: bool) -> None:
        if not isinstance(allow_short, bool):
            raise ValueError("allow_short must be boolean")
        self._allow_short = allow_short

    def update_market(self, bar: MarketBar) -> None:
        position = self._positions.get(bar.symbol)
        if position is None:
            return
        position.last_price = bar.close
        if bar.close > position.highest_price:
            position.highest_price = bar.close
        if position.lowest_price is None or bar.close < position.lowest_price:
            position.lowest_price = bar.close
        position.price_history = _append_price_history(position.price_history, bar.timestamp, bar.close)

    def place_order(self, order: Order, bar: MarketBar) -> Fill:
        if order.quantity <= 0:
            return self._reject(order, bar, "invalid_quantity")
        if order.side == "BUY":
            return self._buy(order, bar)
        if order.side == "SELL":
            return self._sell(order, bar)
        if order.side == "SHORT_ENTRY":
            return self._short_entry(order, bar)
        if order.side == "SHORT_EXIT":
            return self._short_exit(order, bar)
        return self._reject(order, bar, "invalid_side")

    def _buy(self, order: Order, bar: MarketBar) -> Fill:
        price = bar.buy_price
        if price <= 0:
            return self._reject(order, bar, "invalid_price")
        cost = price * order.quantity
        if cost > self.snapshot().buying_power:
            return self._reject(order, bar, "insufficient_cash")

        existing = self._positions.get(order.symbol)
        if existing is not None and existing.side != "LONG":
            return self._reject(order, bar, "position_side_conflict")

        if existing is None:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=order.quantity,
                avg_price=price,
                last_price=price,
                opened_at=bar.timestamp,
                highest_price=price,
                lowest_price=price,
                price_history=((bar.timestamp, price),),
            )
        else:
            total_quantity = existing.quantity + order.quantity
            total_cost = existing.avg_price * existing.quantity + cost
            existing.quantity = total_quantity
            existing.avg_price = total_cost / total_quantity
            existing.last_price = price
            if price > existing.highest_price:
                existing.highest_price = price
            if existing.lowest_price is None or price < existing.lowest_price:
                existing.lowest_price = price
            existing.price_history = _append_price_history(existing.price_history, bar.timestamp, price)

        self._cash -= cost
        return Fill(order=order, accepted=True, timestamp=bar.timestamp, price=price, quantity=order.quantity)

    def _sell(self, order: Order, bar: MarketBar) -> Fill:
        position = self._positions.get(order.symbol)
        if position is None or position.quantity < order.quantity:
            return self._reject(order, bar, "insufficient_position")
        if position.side != "LONG":
            return self._reject(order, bar, "position_side_conflict")

        price = bar.sell_price
        if price <= 0:
            return self._reject(order, bar, "invalid_price")

        proceeds = price * order.quantity
        realized = (price - position.avg_price) * order.quantity
        self._cash += proceeds
        self._realized_pnl_today += realized

        position.quantity -= order.quantity
        position.last_price = price
        if position.quantity == 0:
            del self._positions[order.symbol]
        else:
            position.price_history = _append_price_history(position.price_history, bar.timestamp, price)

        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=price,
            quantity=order.quantity,
            realized_pnl=realized,
        )

    def _short_entry(self, order: Order, bar: MarketBar) -> Fill:
        if not self._allow_short:
            return self._reject(order, bar, "paper_short_disabled")

        price = bar.sell_price
        if price <= 0:
            return self._reject(order, bar, "invalid_price")

        existing = self._positions.get(order.symbol)
        if existing is not None and existing.side != "SHORT":
            return self._reject(order, bar, "position_side_conflict")

        proceeds = price * order.quantity
        if proceeds > self.snapshot().buying_power:
            return self._reject(order, bar, "insufficient_cash")

        if existing is None:
            self._positions[order.symbol] = Position(
                symbol=order.symbol,
                quantity=order.quantity,
                avg_price=price,
                last_price=price,
                opened_at=bar.timestamp,
                highest_price=price,
                side="SHORT",
                lowest_price=price,
                price_history=((bar.timestamp, price),),
            )
        else:
            total_quantity = existing.quantity + order.quantity
            total_proceeds = existing.avg_price * existing.quantity + proceeds
            existing.quantity = total_quantity
            existing.avg_price = total_proceeds / total_quantity
            existing.last_price = price
            if price > existing.highest_price:
                existing.highest_price = price
            if existing.lowest_price is None or price < existing.lowest_price:
                existing.lowest_price = price
            existing.price_history = _append_price_history(existing.price_history, bar.timestamp, price)

        self._cash += proceeds
        return Fill(order=order, accepted=True, timestamp=bar.timestamp, price=price, quantity=order.quantity)

    def _short_exit(self, order: Order, bar: MarketBar) -> Fill:
        position = self._positions.get(order.symbol)
        if position is None:
            return self._reject(order, bar, "insufficient_position")
        if position.side != "SHORT":
            return self._reject(order, bar, "position_side_conflict")
        if position.quantity < order.quantity:
            return self._reject(order, bar, "insufficient_position")

        price = bar.buy_price
        if price <= 0:
            return self._reject(order, bar, "invalid_price")

        cost = price * order.quantity
        realized = (position.avg_price - price) * order.quantity
        self._cash -= cost
        self._realized_pnl_today += realized

        position.quantity -= order.quantity
        position.last_price = price
        if position.lowest_price is None or price < position.lowest_price:
            position.lowest_price = price
        if position.quantity == 0:
            del self._positions[order.symbol]
        else:
            position.price_history = _append_price_history(position.price_history, bar.timestamp, price)

        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=price,
            quantity=order.quantity,
            realized_pnl=realized,
        )

    @staticmethod
    def _reject(order: Order, bar: MarketBar, reason: str) -> Fill:
        return Fill(order=order, accepted=False, timestamp=bar.timestamp, reject_reason=reason)


def _append_price_history(
    history: tuple[tuple[object, Decimal], ...],
    timestamp,
    price: Decimal,
) -> tuple[tuple[object, Decimal], ...]:
    updated = tuple(history)
    if updated and updated[-1][0] == timestamp:
        updated = (*updated[:-1], (timestamp, price))
    else:
        updated = (*updated, (timestamp, price))
    return updated[-MAX_POSITION_PRICE_HISTORY:]
