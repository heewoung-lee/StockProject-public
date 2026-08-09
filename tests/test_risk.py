import sys
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.models import AccountSnapshot, Order, Position
from stockbot.risk import RiskConfig, RiskManager


class RiskManagerTest(unittest.TestCase):
    def test_risk_config_rejects_negative_max_positions(self):
        with self.assertRaisesRegex(ValueError, "max_positions must be 0 or greater"):
            RiskConfig(max_positions=-1)

    def test_rejects_buy_that_exceeds_cash(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("2000000")))
        account = AccountSnapshot(cash=Decimal("10000"), positions={}, realized_pnl_today=Decimal("0"))

        decision = risk.check(Order.buy("005930", 2, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("insufficient_cash", decision.reason)

    def test_rejects_buy_that_only_fits_short_sale_proceeds(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("2000000")))
        account = AccountSnapshot(
            cash=Decimal("1140000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    avg_price=Decimal("70000"),
                    last_price=Decimal("70000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("70000"),
                    side="SHORT",
                    lowest_price=Decimal("70000"),
                )
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("000660", 105, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("insufficient_cash", decision.reason)

    def test_rejects_mixed_account_buy_that_only_fits_short_sale_proceeds(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("1000000"), max_position_amount=Decimal("1000000")))
        account = AccountSnapshot(
            cash=Decimal("600000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=50,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                ),
                "000660": Position(
                    symbol="000660",
                    quantity=5,
                    avg_price=Decimal("20000"),
                    last_price=Decimal("20000"),
                    opened_at=datetime(2026, 6, 8, 9, 1),
                    highest_price=Decimal("20000"),
                    side="SHORT",
                    lowest_price=Decimal("20000"),
                ),
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("035420", 55, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("insufficient_cash", decision.reason)

    def test_legacy_max_order_amount_does_not_reject_buy(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("50000")))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        decision = risk.check(Order.buy("005930", 6, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertTrue(decision.approved)

    def test_unknown_account_wide_realized_pnl_blocks_entry_but_allows_exit(self):
        risk = RiskManager(RiskConfig())
        position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 7, 10, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={"005930": position},
            realized_pnl_today_known=False,
        )

        entry = risk.check(Order.buy("000660", 1, "entry"), account, Decimal("10000"))
        exit_decision = risk.check(Order.sell("005930", 1, "exit"), account, Decimal("10000"))

        self.assertFalse(entry.approved)
        self.assertEqual("daily_realized_pnl_unknown", entry.reason)
        self.assertTrue(exit_decision.approved)

    def test_rejects_buy_above_symbol_position_limit(self):
        risk = RiskManager(RiskConfig(max_position_amount=Decimal("100000")))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=9,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                )
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("005930", 2, reason="add"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("max_position_amount_exceeded", decision.reason)

    def test_rejects_new_buy_when_max_positions_is_reached(self):
        risk = RiskManager(RiskConfig(max_positions=1))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                )
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("000660", 1, reason="entry"), account, estimated_price=Decimal("100000"))

        self.assertFalse(decision.approved)
        self.assertEqual("max_positions_reached", decision.reason)

    def test_zero_max_positions_allows_new_buy_until_other_limits_apply(self):
        risk = RiskManager(RiskConfig(max_positions=0, max_order_amount=Decimal("500000")))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                ),
                "000660": Position(
                    symbol="000660",
                    quantity=1,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                ),
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("035420", 1, reason="entry"), account, estimated_price=Decimal("100000"))

        self.assertTrue(decision.approved)

    def test_rejects_new_buy_after_daily_loss_limit(self):
        risk = RiskManager(RiskConfig(max_daily_loss=Decimal("50000")))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("-50000"))

        decision = risk.check(Order.buy("005930", 1, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("daily_loss_limit_reached", decision.reason)

    def test_rejects_new_buy_when_unrealized_account_loss_reaches_daily_limit(self):
        risk = RiskManager(RiskConfig(max_daily_loss=Decimal("1000")))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "000660": Position(
                    symbol="000660",
                    quantity=1,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("9000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                )
            },
            realized_pnl_today=Decimal("0"),
        )

        decision = risk.check(Order.buy("005930", 1, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("daily_loss_limit_reached", decision.reason)

    def test_daily_loss_limit_still_allows_exit_sell(self):
        risk = RiskManager(RiskConfig(max_daily_loss=Decimal("50000")))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("9000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("10000"),
                )
            },
            realized_pnl_today=Decimal("-50000"),
        )

        decision = risk.check(Order.sell("005930", 2, reason="exit"), account, estimated_price=Decimal("9000"))

        self.assertTrue(decision.approved)

    def test_daily_loss_limit_still_allows_short_cover_exit(self):
        risk = RiskManager(RiskConfig(max_daily_loss=Decimal("50000")))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("11000"),
                    opened_at=datetime(2026, 6, 8, 9, 0),
                    highest_price=Decimal("11000"),
                    side="SHORT",
                )
            },
            realized_pnl_today=Decimal("-50000"),
        )

        decision = risk.check(Order.cover("005930", 2, reason="exit"), account, estimated_price=Decimal("11000"))

        self.assertTrue(decision.approved)

    def test_short_entry_ignores_legacy_max_order_amount(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("50000")))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        decision = risk.check(Order.short("005930", 6, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertTrue(decision.approved)

    def test_rejects_short_entry_that_exceeds_available_cash(self):
        risk = RiskManager(RiskConfig(max_order_amount=Decimal("2000000"), max_position_amount=Decimal("2000000")))
        account = AccountSnapshot(cash=Decimal("10000"), positions={}, realized_pnl_today=Decimal("0"))

        decision = risk.check(Order.short("005930", 2, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("insufficient_cash", decision.reason)

    def test_cleanup_mode_rejects_new_entries_but_allows_exits(self):
        risk = RiskManager(RiskConfig(kill_switch=True))
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    "005930",
                    1,
                    Decimal("10000"),
                    Decimal("10000"),
                    datetime(2026, 6, 15, 9, 0),
                    Decimal("10000"),
                )
            },
            realized_pnl_today=Decimal("0"),
        )

        buy_decision = risk.check(Order.buy("000660", 1, reason="entry"), account, estimated_price=Decimal("10000"))
        sell_decision = risk.check(Order.sell("005930", 1, reason="exit"), account, estimated_price=Decimal("10000"))

        self.assertFalse(buy_decision.approved)
        self.assertEqual("cleanup_mode_active", buy_decision.reason)
        self.assertTrue(sell_decision.approved)

    def test_rejects_buy_after_symbol_reentry_limit(self):
        risk = RiskManager(RiskConfig(max_daily_entries_per_symbol=1))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        risk.record_entry("005930")

        decision = risk.check(Order.buy("005930", 1, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("max_daily_entries_reached", decision.reason)

    def test_symbol_reentry_limit_resets_by_trading_date(self):
        risk = RiskManager(RiskConfig(max_daily_entries_per_symbol=1))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        risk.record_entry("005930", date(2026, 6, 8))

        decision = risk.check(
            Order.buy("005930", 1, reason="entry"),
            account,
            estimated_price=Decimal("10000"),
            as_of=date(2026, 6, 9),
        )

        self.assertTrue(decision.approved)

    def test_reports_symbol_reentry_limit_without_order_check(self):
        risk = RiskManager(RiskConfig(max_daily_entries_per_symbol=1))
        risk.record_entry("005930", date(2026, 6, 8))

        self.assertTrue(risk.entry_limit_reached("005930", date(2026, 6, 8)))
        self.assertFalse(risk.entry_limit_reached("005930", date(2026, 6, 9)))

    def test_restore_entry_counts_replaces_state_and_sanitizes_persisted_values(self):
        risk = RiskManager(RiskConfig(max_daily_entries_per_symbol=2))
        risk.record_entry("OLD", date(2026, 7, 10))

        risk.restore_entry_counts(
            {
                (" 005930 ", date(2026, 7, 10)): "2",
                ("005930", "2026-07-11"): 1,
                ("000660", date(2026, 7, 10)): 1,
                ("", date(2026, 7, 10)): 99,
                ("BAD-DATE", "not-a-date"): 99,
                ("NEGATIVE", date(2026, 7, 10)): -1,
                ("NOT-AN-INT", date(2026, 7, 10)): "1.5",
                "not-a-pair": 99,
            }
        )

        self.assertTrue(risk.entry_limit_reached("005930", date(2026, 7, 10)))
        self.assertFalse(risk.entry_limit_reached("005930", date(2026, 7, 11)))
        self.assertFalse(risk.entry_limit_reached("000660", date(2026, 7, 10)))
        self.assertFalse(risk.entry_limit_reached("OLD", date(2026, 7, 10)))
        self.assertFalse(risk.entry_limit_reached("NEGATIVE", date(2026, 7, 10)))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        decision = risk.check(
            Order.buy("005930", 1, reason="entry"),
            account,
            estimated_price=Decimal("10000"),
            as_of=date(2026, 7, 10),
        )
        self.assertFalse(decision.approved)
        self.assertEqual("max_daily_entries_reached", decision.reason)

    def test_rejects_new_buy_after_consecutive_order_failures(self):
        risk = RiskManager(RiskConfig(max_consecutive_order_failures=2))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        risk.record_order_result(False)
        risk.record_order_result(False)

        decision = risk.check(Order.buy("005930", 1, reason="entry"), account, estimated_price=Decimal("10000"))

        self.assertFalse(decision.approved)
        self.assertEqual("order_failure_limit_reached", decision.reason)


if __name__ == "__main__":
    unittest.main()
