import unittest
from datetime import datetime
from decimal import Decimal

from stockbot.metrics import PaperMetricsTracker, account_unrealized_pnl
from stockbot.models import AccountSnapshot, Fill, Order, Position


def _position(symbol, *, side, avg, last, quantity=2):
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_price=Decimal(avg),
        last_price=Decimal(last),
        opened_at=datetime(2026, 6, 11, 9, 0),
        highest_price=Decimal(max(avg, last)),
        lowest_price=Decimal(min(avg, last)),
        side=side,
    )


class AccountUnrealizedPnlTest(unittest.TestCase):
    def test_sums_long_and_short_open_profit(self):
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": _position("005930", side="LONG", avg="100", last="110", quantity=3),
                "035720": _position("035720", side="SHORT", avg="200", last="180", quantity=2),
            },
            realized_pnl_today=Decimal("500"),
        )

        self.assertEqual(Decimal("70"), account_unrealized_pnl(account))


class PaperMetricsTrackerTest(unittest.TestCase):
    def test_snapshot_reports_free_cash_when_short_positions_are_open(self):
        tracker = PaperMetricsTracker()
        account = AccountSnapshot(
            cash=Decimal("600000"),
            positions={
                "005930": _position("005930", side="LONG", avg="10000", last="10000", quantity=50),
                "000660": _position("000660", side="SHORT", avg="20000", last="20000", quantity=5),
            },
            realized_pnl_today=Decimal("0"),
        )

        metrics = tracker.snapshot(account)

        self.assertEqual(Decimal("400000"), metrics.cash)
        self.assertEqual(Decimal("1000000"), metrics.equity)

    def test_snapshot_combines_account_and_trade_statistics(self):
        tracker = PaperMetricsTracker()
        tracker.record_fill(
            Fill(
                order=Order.sell("005930", 1, "take_profit"),
                accepted=True,
                timestamp=datetime(2026, 6, 11, 9, 1),
                price=Decimal("110"),
                quantity=1,
                realized_pnl=Decimal("10"),
            )
        )
        tracker.record_rejection()
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={"035720": _position("035720", side="SHORT", avg="200", last="190", quantity=1)},
            realized_pnl_today=Decimal("10"),
        )

        metrics = tracker.snapshot(account)

        self.assertEqual(Decimal("10"), metrics.realized_pnl)
        self.assertEqual(Decimal("10"), metrics.unrealized_pnl)
        self.assertEqual(Decimal("20"), metrics.total_pnl)
        self.assertEqual(1, metrics.open_positions)
        self.assertEqual(0, metrics.long_positions)
        self.assertEqual(1, metrics.short_positions)
        self.assertEqual(1, metrics.filled_trades)
        self.assertEqual(1, metrics.rejected_trades)
        self.assertEqual(Decimal("100"), metrics.win_rate_pct)

    def test_losses_count_against_win_rate(self):
        tracker = PaperMetricsTracker()
        tracker.record_fill(
            Fill(
                order=Order.cover("035720", 1, "stop_loss"),
                accepted=True,
                timestamp=datetime(2026, 6, 11, 9, 1),
                price=Decimal("210"),
                quantity=1,
                realized_pnl=Decimal("-10"),
            )
        )

        metrics = tracker.snapshot(AccountSnapshot(cash=Decimal("1000000")))

        self.assertEqual(0, metrics.winning_exits)
        self.assertEqual(1, metrics.losing_exits)
        self.assertEqual(Decimal("0"), metrics.win_rate_pct)

    def test_flat_exits_count_in_win_rate_denominator(self):
        tracker = PaperMetricsTracker()
        tracker.record_fill(
            Fill(
                order=Order.sell("005930", 1, "take_profit"),
                accepted=True,
                timestamp=datetime(2026, 6, 11, 9, 1),
                price=Decimal("110"),
                quantity=1,
                realized_pnl=Decimal("10"),
            )
        )
        tracker.record_fill(
            Fill(
                order=Order.sell("005930", 1, "breakeven"),
                accepted=True,
                timestamp=datetime(2026, 6, 11, 9, 2),
                price=Decimal("100"),
                quantity=1,
                realized_pnl=Decimal("0"),
            )
        )

        metrics = tracker.snapshot(AccountSnapshot(cash=Decimal("1000000")))

        self.assertEqual(1, metrics.winning_exits)
        self.assertEqual(0, metrics.losing_exits)
        self.assertEqual(1, metrics.flat_exits)
        self.assertEqual(Decimal("50.0"), metrics.win_rate_pct)


if __name__ == "__main__":
    unittest.main()
