from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable, Mapping

from .models import MarketBar


@dataclass(frozen=True)
class EntryAffordabilityIssue:
    code: str
    price: Decimal
    order_cash_amount: Decimal

    def message(self) -> str:
        if self.code == "entry_price_invalid":
            return self.code
        return (
            "entry_unaffordable: "
            f"one_share_price={format_krw(self.price)}, "
            f"entry_budget={format_krw(self.order_cash_amount)}"
        )


class CandidateSelector:
    def __init__(
        self,
        *,
        latest_entry_prices: Mapping[str, Decimal],
        latest_short_entry_prices: Mapping[str, Decimal] | None = None,
        order_cash_amount: Decimal,
        priority_provider: Callable[[str], float],
        allow_short_entries: bool = False,
    ) -> None:
        self.latest_entry_prices = latest_entry_prices
        self.latest_short_entry_prices = latest_short_entry_prices or {}
        self.order_cash_amount = Decimal(str(order_cash_amount))
        self.priority_provider = priority_provider
        self.allow_short_entries = bool(allow_short_entries)

    def known_entry_unaffordable(self, symbol: str) -> bool:
        price = self.latest_entry_prices.get(symbol)
        if not self._known_price_unaffordable(price):
            return False
        if not self.allow_short_entries:
            return True
        short_price = self.latest_short_entry_prices.get(symbol)
        return self._known_price_unaffordable(short_price)

    def order_symbols(self, symbols: Iterable[str]) -> list[str]:
        listed = list(symbols)
        original_indexes = {symbol: index for index, symbol in enumerate(listed)}
        return sorted(
            listed,
            key=lambda symbol: self._scan_sort_key(symbol, original_indexes[symbol]),
        )

    def entry_issue_for_signal(self, side: str, bar: MarketBar) -> EntryAffordabilityIssue | None:
        price = entry_reference_price(bar, side)
        return entry_affordability_issue(price, self.order_cash_amount)

    def entry_issue_for_scan(self, bar: MarketBar) -> EntryAffordabilityIssue | None:
        buy_issue = entry_affordability_issue(bar.buy_price, self.order_cash_amount)
        if buy_issue is None:
            return None
        if self.allow_short_entries:
            short_issue = entry_affordability_issue(bar.sell_price, self.order_cash_amount)
            if short_issue is None:
                return None
        return buy_issue

    def _scan_sort_key(self, symbol: str, original_index: int):
        price = self.latest_entry_prices.get(symbol)
        priority = -self._priority(symbol)
        return (self._scan_price_status(symbol), priority, original_index, price or Decimal("0"))

    def _scan_price_status(self, symbol: str) -> int:
        price = self.latest_entry_prices.get(symbol)
        if self._known_price_affordable(price):
            return 0
        if price is None or price <= 0:
            return 1
        if self.allow_short_entries:
            short_price = self.latest_short_entry_prices.get(symbol)
            if self._known_price_affordable(short_price):
                return 0
            if short_price is None or short_price <= 0:
                return 1
        return 2

    def _known_price_affordable(self, price: Decimal | None) -> bool:
        return price is not None and price > 0 and price <= self.order_cash_amount

    def _known_price_unaffordable(self, price: Decimal | None) -> bool:
        return price is not None and price > self.order_cash_amount

    def _priority(self, symbol: str) -> float:
        try:
            return float(self.priority_provider(symbol))
        except Exception:
            return 0.0


def entry_reference_price(bar: MarketBar, side: str = "BUY") -> Decimal | None:
    price = bar.sell_price if side == "SHORT_ENTRY" else bar.buy_price
    if price <= 0:
        return None
    return price


def entry_affordability_issue(
    price: Decimal | None,
    order_cash_amount: Decimal,
) -> EntryAffordabilityIssue | None:
    order_cash = Decimal(str(order_cash_amount))
    if price is None or price <= 0:
        return EntryAffordabilityIssue("entry_price_invalid", Decimal("0"), order_cash)
    if int(order_cash / price) <= 0:
        return EntryAffordabilityIssue("entry_unaffordable", price, order_cash)
    return None


def format_krw(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1')):,}원"
