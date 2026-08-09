import unittest
from datetime import datetime
from decimal import Decimal

from stockbot.candidate_selection import CandidateSelector
from stockbot.models import MarketBar


def make_bar(symbol="005930", bid="50000", ask="50001"):
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 17, 9, 0),
        open=Decimal(ask),
        high=Decimal(ask),
        low=Decimal(bid),
        close=Decimal(ask),
        volume=1000,
        vwap=Decimal(ask),
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


class CandidateSelectorTest(unittest.TestCase):
    def test_long_only_scan_affordability_uses_buy_price(self):
        selector = CandidateSelector(
            latest_entry_prices={},
            order_cash_amount=Decimal("50000"),
            priority_provider=lambda _symbol: 0.0,
        )

        issue = selector.entry_issue_for_scan(make_bar())

        self.assertIsNotNone(issue)
        self.assertIn("entry_unaffordable", issue.message())
        self.assertIn("entry_budget=", issue.message())
        self.assertNotIn("order_cash=", issue.message())

    def test_short_enabled_scan_can_use_sell_price_when_buy_price_is_unaffordable(self):
        selector = CandidateSelector(
            latest_entry_prices={},
            order_cash_amount=Decimal("50000"),
            priority_provider=lambda _symbol: 0.0,
            allow_short_entries=True,
        )

        self.assertIsNone(selector.entry_issue_for_scan(make_bar()))

    def test_short_enabled_known_unaffordable_symbol_requires_both_entry_sides_to_be_too_expensive(self):
        selector = CandidateSelector(
            latest_entry_prices={"HIGH01": Decimal("200000")},
            latest_short_entry_prices={"HIGH01": Decimal("200000")},
            order_cash_amount=Decimal("50000"),
            priority_provider=lambda _symbol: 0.0,
            allow_short_entries=True,
        )

        self.assertTrue(selector.known_entry_unaffordable("HIGH01"))

    def test_known_affordable_symbols_sort_before_unknown_and_unaffordable(self):
        selector = CandidateSelector(
            latest_entry_prices={
                "HIGH01": Decimal("200000"),
                "BUY002": Decimal("10000"),
            },
            order_cash_amount=Decimal("50000"),
            priority_provider=lambda symbol: {"HIGH01": 100.0, "BUY002": 10.0, "UNK003": 50.0}[symbol],
        )

        self.assertEqual(["BUY002", "UNK003", "HIGH01"], selector.order_symbols(["HIGH01", "UNK003", "BUY002"]))


if __name__ == "__main__":
    unittest.main()
