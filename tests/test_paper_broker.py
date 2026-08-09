import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.broker import PaperBroker
from stockbot.models import MarketBar, Order


def bar(symbol="005930", close="10000", bid="9990", ask="10010", minute=1):
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, minute),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100_000,
        vwap=Decimal(close),
        bid=Decimal(bid),
        ask=Decimal(ask),
    )


class PaperBrokerTest(unittest.TestCase):
    def test_buy_creates_position_and_reduces_cash(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        fill = broker.place_order(Order.buy("005930", 10, reason="entry"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("899900"), broker.snapshot().cash)
        position = broker.snapshot().positions["005930"]
        self.assertEqual(10, position.quantity)
        self.assertEqual(Decimal("10010"), position.avg_price)

    def test_additional_buy_updates_weighted_average_price(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        broker.place_order(Order.buy("005930", 10, reason="entry"), bar(ask="10000"))
        broker.place_order(Order.buy("005930", 10, reason="add"), bar(close="11000", bid="10990", ask="11000"))

        position = broker.snapshot().positions["005930"]
        self.assertEqual(20, position.quantity)
        self.assertEqual(Decimal("10500"), position.avg_price)

    def test_market_updates_append_recent_price_history_for_open_position(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        broker.place_order(Order.buy("005930", 10, reason="entry"), bar(ask="10000", minute=1))
        broker.update_market(bar(close="10100", bid="10090", ask="10110", minute=2))
        broker.update_market(bar(close="10200", bid="10190", ask="10210", minute=3))

        position = broker.snapshot().positions["005930"]
        self.assertEqual(
            (
                (datetime(2026, 6, 8, 9, 1), Decimal("10000")),
                (datetime(2026, 6, 8, 9, 2), Decimal("10100")),
                (datetime(2026, 6, 8, 9, 3), Decimal("10200")),
            ),
            position.price_history,
        )

    def test_sell_records_realized_profit_and_removes_closed_position(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        broker.place_order(Order.buy("005930", 10, reason="entry"), bar(ask="10000"))
        fill = broker.place_order(Order.sell("005930", 10, reason="exit"), bar(close="11000", bid="11000", ask="11010"))

        self.assertTrue(fill.accepted)
        snapshot = broker.snapshot()
        self.assertNotIn("005930", snapshot.positions)
        self.assertEqual(Decimal("1010000"), snapshot.cash)
        self.assertEqual(Decimal("10000"), snapshot.realized_pnl_today)

    def test_rejects_buy_when_cash_is_insufficient(self):
        broker = PaperBroker(initial_cash=Decimal("10000"))

        fill = broker.place_order(Order.buy("005930", 2, reason="entry"), bar(ask="10000"))

        self.assertFalse(fill.accepted)
        self.assertEqual("insufficient_cash", fill.reject_reason)
        self.assertEqual(Decimal("10000"), broker.snapshot().cash)

    def test_rejects_sell_when_position_is_insufficient(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        fill = broker.place_order(Order.sell("005930", 1, reason="exit"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("insufficient_position", fill.reject_reason)

    def test_partial_sell_keeps_average_cost_and_records_realized_profit(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        broker.place_order(Order.buy("005930", 10, reason="entry"), bar(ask="10000"))
        fill = broker.place_order(Order.sell("005930", 4, reason="trim"), bar(close="11000", bid="11000", ask="11010"))

        self.assertTrue(fill.accepted)
        snapshot = broker.snapshot()
        position = snapshot.positions["005930"]
        self.assertEqual(6, position.quantity)
        self.assertEqual(Decimal("10000"), position.avg_price)
        self.assertEqual(Decimal("4000"), snapshot.realized_pnl_today)

    def test_rejects_zero_or_negative_quantity(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))

        zero = broker.place_order(Order.buy("005930", 0, reason="bad"), bar())
        negative = broker.place_order(Order.buy("005930", -1, reason="bad"), bar())

        self.assertFalse(zero.accepted)
        self.assertFalse(negative.accepted)
        self.assertEqual("invalid_quantity", zero.reject_reason)
        self.assertEqual("invalid_quantity", negative.reject_reason)


class PaperBrokerShortTest(unittest.TestCase):
    def test_short_order_builders_use_paper_short_sides(self):
        short = Order.short("005930", 2, "downtrend")
        cover = Order.cover("005930", 2, "take_profit")

        self.assertEqual("SHORT_ENTRY", short.side)
        self.assertEqual("SHORT_EXIT", cover.side)

    def test_short_is_rejected_when_not_enabled(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=False)

        fill = broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="70000", bid="70000", ask="70010"))

        self.assertFalse(fill.accepted)
        self.assertEqual("paper_short_disabled", fill.reject_reason)

    def test_short_cover_realizes_profit_when_price_falls(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        short_fill = broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="70000", bid="70000", ask="70010"))
        cover_fill = broker.place_order(Order.cover("005930", 2, "take_profit"), bar(close="69000", bid="68990", ask="69000"))

        self.assertTrue(short_fill.accepted)
        self.assertTrue(cover_fill.accepted)
        self.assertEqual(Decimal("2000"), cover_fill.realized_pnl)
        self.assertEqual(Decimal("1002000"), broker.snapshot().cash)
        self.assertNotIn("005930", broker.snapshot().positions)

    def test_short_entry_reserves_cash_from_buying_power(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="70000", bid="70000", ask="70010"))
        snapshot = broker.snapshot()

        self.assertEqual(Decimal("1140000"), snapshot.cash)
        self.assertEqual(Decimal("1000000"), snapshot.equity)
        self.assertEqual(Decimal("860000"), snapshot.buying_power)

    def test_short_sale_proceeds_are_subtracted_from_mixed_account_buying_power(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        broker.place_order(Order.buy("005930", 50, "long_entry"), bar(close="10000", bid="9990", ask="10000"))
        before_short = broker.snapshot()
        broker.place_order(Order.short("000660", 5, "short_entry"), bar(symbol="000660", close="20000", bid="20000", ask="20010"))
        after_short = broker.snapshot()

        self.assertEqual(Decimal("500000"), before_short.cash)
        self.assertEqual(Decimal("500000"), before_short.buying_power)
        self.assertEqual(Decimal("600000"), after_short.cash)
        self.assertEqual(Decimal("100000"), after_short.short_proceeds)
        self.assertEqual(Decimal("400000"), after_short.buying_power)

    def test_buy_cannot_spend_short_sale_proceeds(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        broker.place_order(Order.buy("005930", 50, "long_entry"), bar(close="10000", bid="9990", ask="10000"))
        broker.place_order(Order.short("000660", 5, "short_entry"), bar(symbol="000660", close="20000", bid="20000", ask="20010"))
        fill = broker.place_order(Order.buy("035420", 45, "new_long"), bar(symbol="035420", close="10000", bid="9990", ask="10000"))

        self.assertFalse(fill.accepted)
        self.assertEqual("insufficient_cash", fill.reject_reason)
        snapshot = broker.snapshot()
        self.assertEqual({"005930", "000660"}, set(snapshot.positions))
        self.assertEqual(Decimal("600000"), snapshot.cash)
        self.assertEqual(Decimal("400000"), snapshot.buying_power)

    def test_short_entry_rejects_when_margin_cash_is_insufficient(self):
        broker = PaperBroker(initial_cash=Decimal("10000"), allow_short=True)

        fill = broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="10000", bid="10000", ask="10010"))

        self.assertFalse(fill.accepted)
        self.assertEqual("insufficient_cash", fill.reject_reason)
        self.assertEqual(Decimal("10000"), broker.snapshot().cash)
        self.assertEqual({}, broker.snapshot().positions)

    def test_short_cover_realizes_loss_when_price_rises(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="70000", bid="70000", ask="70010"))
        fill = broker.place_order(Order.cover("005930", 2, "stop_loss"), bar(close="71000", bid="70990", ask="71000"))

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("-2000"), fill.realized_pnl)
        self.assertEqual(Decimal("998000"), broker.snapshot().cash)

    def test_short_cover_is_allowed_even_when_loss_exceeds_cash(self):
        broker = PaperBroker(initial_cash=Decimal("100"), allow_short=True)

        broker.place_order(Order.short("005930", 1, "downtrend"), bar(close="100", bid="100", ask="101"))
        fill = broker.place_order(Order.cover("005930", 1, "stop_loss"), bar(close="250", bid="249", ask="250"))

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("-150"), fill.realized_pnl)
        self.assertEqual(Decimal("-50"), broker.snapshot().cash)
        self.assertNotIn("005930", broker.snapshot().positions)

    def test_partial_short_cover_keeps_remaining_short_position(self):
        broker = PaperBroker(initial_cash=Decimal("1000"), allow_short=True)

        broker.place_order(Order.short("005930", 4, "downtrend"), bar(close="100", bid="100", ask="101"))
        fill = broker.place_order(Order.cover("005930", 2, "partial_take_profit"), bar(close="90", bid="89", ask="90"))

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("20"), fill.realized_pnl)
        snapshot = broker.snapshot()
        position = snapshot.positions["005930"]
        self.assertEqual("SHORT", position.side)
        self.assertEqual(2, position.quantity)
        self.assertEqual(Decimal("100"), position.avg_price)
        self.assertEqual(Decimal("90"), position.last_price)
        self.assertEqual(Decimal("20"), position.unrealized_pnl)
        self.assertEqual(Decimal("1040"), snapshot.equity)
        self.assertEqual(Decimal("90"), position.lowest_price)

    def test_additional_short_updates_low_water_mark(self):
        broker = PaperBroker(initial_cash=Decimal("1000"), allow_short=True)

        broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="100", bid="100", ask="101"))
        broker.place_order(Order.short("005930", 2, "add_downtrend"), bar(close="90", bid="90", ask="91"))

        position = broker.snapshot().positions["005930"]
        self.assertEqual(4, position.quantity)
        self.assertEqual(Decimal("95"), position.avg_price)
        self.assertEqual(Decimal("90"), position.last_price)
        self.assertEqual(Decimal("90"), position.lowest_price)

    def test_cover_long_position_reports_side_conflict(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(Order.buy("005930", 1, "entry"), bar(ask="10000"))

        fill = broker.place_order(Order.cover("005930", 1, "wrong_exit_side"), bar(ask="10000"))

        self.assertFalse(fill.accepted)
        self.assertEqual("position_side_conflict", fill.reject_reason)

    def test_short_position_requires_cover_order_to_close(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(Order.short("005930", 2, "downtrend"), bar(close="70000", bid="70000", ask="70010"))

        fill = broker.place_order(Order.sell("005930", 2, "wrong_exit_side"), bar(close="69000", bid="69000", ask="69010"))

        self.assertFalse(fill.accepted)
        self.assertEqual("position_side_conflict", fill.reject_reason)
        self.assertEqual(2, broker.snapshot().positions["005930"].quantity)

    def test_long_realized_profit_remains_unchanged_with_short_support(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)

        broker.place_order(Order.buy("005930", 10, reason="entry"), bar(ask="10000"))
        fill = broker.place_order(Order.sell("005930", 10, reason="exit"), bar(close="11000", bid="11000", ask="11010"))

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("10000"), fill.realized_pnl)
        self.assertEqual(Decimal("1010000"), broker.snapshot().cash)


if __name__ == "__main__":
    unittest.main()
