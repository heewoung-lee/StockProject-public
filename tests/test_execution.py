import csv
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.broker import PaperBroker
from stockbot.execution import ExecutionEngine, ExecutionSettings, order_from_signal
from stockbot.journal import CsvTradeJournal
from stockbot.models import AccountSnapshot, MarketBar, Order, Signal
from stockbot.risk import RiskConfig, RiskManager


class BuyOnceStrategy:
    def __init__(self):
        self.seen = False

    def on_bar(self, bar, account):
        if self.seen:
            return []
        self.seen = True
        return [Signal.buy(bar.symbol, "test_entry")]


class AlwaysBuyStrategy:
    def on_bar(self, bar, account):
        return [Signal.buy(bar.symbol, "test_entry")]


class ShortOnceStrategy:
    def __init__(self):
        self.seen = False

    def on_bar(self, bar, account):
        if self.seen:
            return []
        self.seen = True
        return [Signal.short(bar.symbol, "short_entry")]


def make_bar(symbol="005930", offset=0, close="10000"):
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        vwap=price,
        bid=price,
        ask=price,
    )


class ExecutionEngineTest(unittest.TestCase):
    def test_signal_places_order_and_writes_fill_to_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "trades.csv"
            engine = ExecutionEngine(
                broker=PaperBroker(initial_cash=Decimal("1000000")),
                strategy=BuyOnceStrategy(),
                risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
                journal=CsvTradeJournal(journal_path),
                settings=ExecutionSettings(order_cash_amount=Decimal("50000")),
            )

            engine.run([make_bar()])

            with journal_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("FILL", rows[0]["event"])
            self.assertEqual("BUY", rows[0]["side"])
            self.assertEqual("5", rows[0]["order_quantity"])
            self.assertEqual("5", rows[0]["fill_quantity"])
            self.assertEqual("10000", rows[0]["fill_price"])
            self.assertEqual("0", rows[0]["trade_pnl"])
            self.assertEqual("test_entry", rows[0]["reason"])

    def test_risk_rejection_is_logged_without_placing_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "trades.csv"
            broker = PaperBroker(initial_cash=Decimal("1000000"))
            engine = ExecutionEngine(
                broker=broker,
                strategy=AlwaysBuyStrategy(),
                risk_manager=RiskManager(RiskConfig(max_position_amount=Decimal("1000"))),
                journal=CsvTradeJournal(journal_path),
                settings=ExecutionSettings(order_cash_amount=Decimal("50000")),
            )

            engine.run([make_bar()])

            with journal_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("REJECT", rows[0]["event"])
            self.assertEqual("max_position_amount_exceeded", rows[0]["reject_reason"])
            self.assertEqual("5", rows[0]["order_quantity"])
            self.assertEqual("10000", rows[0]["order_price"])
            self.assertEqual({}, broker.snapshot().positions)

    def test_risk_rejections_do_not_lock_out_later_valid_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "trades.csv"
            broker = PaperBroker(initial_cash=Decimal("1000000"))
            engine = ExecutionEngine(
                broker=broker,
                strategy=AlwaysBuyStrategy(),
                risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_consecutive_order_failures=2)),
                journal=CsvTradeJournal(journal_path),
                settings=ExecutionSettings(order_cash_amount=Decimal("50000")),
            )

            engine.run(
                [
                    make_bar(offset=0, close="100000"),
                    make_bar(offset=1, close="100000"),
                    make_bar(offset=2, close="10000"),
                ]
            )

            with journal_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(["REJECT", "REJECT", "FILL"], [row["event"] for row in rows])
            self.assertEqual("invalid_quantity", rows[0]["reject_reason"])
            self.assertEqual("invalid_quantity", rows[1]["reject_reason"])
            self.assertIn("005930", broker.snapshot().positions)

    def test_order_from_signal_maps_short_and_cover_sides(self):
        account = AccountSnapshot(cash=Decimal("1000000"))
        short_order = order_from_signal(
            Signal.short("005930", "downtrend_short"),
            make_bar(),
            account,
            ExecutionSettings(order_cash_amount=Decimal("50000")),
        )

        self.assertEqual("SHORT_ENTRY", short_order.side)
        self.assertEqual(5, short_order.quantity)

        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(short_order, make_bar())
        cover_order = order_from_signal(
            Signal.cover("005930", "take_profit"),
            make_bar(close="9900"),
            broker.snapshot(),
            ExecutionSettings(order_cash_amount=Decimal("50000")),
        )

        self.assertEqual("SHORT_EXIT", cover_order.side)
        self.assertEqual(5, cover_order.quantity)

    def test_execution_engine_records_short_entry_for_reentry_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "trades.csv"
            risk = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_order_amount=Decimal("100000")))
            engine = ExecutionEngine(
                broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
                strategy=ShortOnceStrategy(),
                risk_manager=risk,
                journal=CsvTradeJournal(journal_path),
                settings=ExecutionSettings(order_cash_amount=Decimal("50000")),
            )

            engine.run([make_bar()])
            decision = risk.check(
                Order.short("005930", 1, "another_short"),
                AccountSnapshot(cash=Decimal("1000000")),
                estimated_price=Decimal("10000"),
                as_of=datetime(2026, 6, 8).date(),
            )

        self.assertFalse(decision.approved)
        self.assertEqual("max_daily_entries_reached", decision.reason)


if __name__ == "__main__":
    unittest.main()
