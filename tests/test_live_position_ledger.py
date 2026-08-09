import json
import sys
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

KST = timezone(timedelta(hours=9), "Asia/Seoul")

from stockbot.live_position_ledger import (
    InMemoryManagedLivePositionLedger,
    JsonManagedLivePositionLedger,
    ManagedProfitAggregate,
)


class JsonManagedLivePositionLedgerTest(unittest.TestCase):
    def test_schema_four_migration_restores_confirmation_for_consumed_fill_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "scope": "account-scope",
                        "positions": {},
                        "consumed_fills": {
                            "2026-07-10:123:005930:SELL": 2,
                        },
                        "consumed_notional_by_fill": {
                            "2026-07-10:123:005930:SELL": "140000",
                        },
                        "realized_pnl_by_date": {},
                        "profit_history_by_hour": {},
                        "position_lifecycle_by_symbol": {},
                        "entry_counts_by_date": {},
                        "entry_count_unknown_dates": [],
                        "position_lifecycle_unknown_symbols": [],
                    }
                ),
                encoding="utf-8",
            )

            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            ledger.ensure_ready()
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            self.assertEqual(0, restored.quantity_for("005930"))
            self.assertEqual(
                0,
                restored.account_quantity_confirmation_for("005930"),
            )

    def test_schema_four_migration_rejects_unscopable_consumed_fill_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 4,
                        "scope": "account-scope",
                        "positions": {},
                        "consumed_fills": {"legacy-fill": 1},
                        "consumed_notional_by_fill": {},
                        "realized_pnl_by_date": {},
                        "profit_history_by_hour": {},
                        "position_lifecycle_by_symbol": {},
                        "entry_counts_by_date": {},
                        "entry_count_unknown_dates": [],
                        "position_lifecycle_unknown_symbols": [],
                    }
                ),
                encoding="utf-8",
            )

            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            with self.assertRaisesRegex(ValueError, "invalid managed live position ledger"):
                ledger.ensure_ready()

    def test_fill_transaction_persists_account_quantity_confirmation_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            timestamp = datetime(2026, 7, 10, 9, 5)

            ledger.record_fill_transaction(
                fill_key="buy-order",
                symbol="005930",
                side="BUY",
                quantity_delta=3,
                cumulative_filled=3,
                timestamp=timestamp,
                price=Decimal("70000"),
            )
            ledger.record_fill_transaction(
                fill_key="sell-order",
                symbol="005930",
                side="SELL",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=timestamp,
                price=Decimal("70100"),
            )

            restored = JsonManagedLivePositionLedger(
                path,
                scope="account-scope",
            )
            self.assertEqual(2, restored.quantity_for("005930"))
            self.assertEqual(
                2,
                restored.account_quantity_confirmation_for("005930"),
            )
            self.assertFalse(
                restored.reconcile_account_quantity_confirmation("005930", 3)
            )
            self.assertEqual(
                2,
                restored.account_quantity_confirmation_for("005930"),
            )
            self.assertTrue(
                restored.reconcile_account_quantity_confirmation("005930", 2)
            )
            self.assertIsNone(
                restored.account_quantity_confirmation_for("005930")
            )

    def test_profit_history_aggregates_new_partial_fills_once_in_kst_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "managed-live-positions.json"
            ledger_factories = (
                ("memory", InMemoryManagedLivePositionLedger),
                (
                    "json",
                    lambda: JsonManagedLivePositionLedger(
                        json_path,
                        scope="account-scope",
                    ),
                ),
            )
            for label, factory in ledger_factories:
                with self.subTest(ledger=label):
                    ledger = factory()
                    first_fill_at = datetime(2026, 7, 10, 9, 5)
                    ledger.record_fill_transaction(
                        fill_key="buy-order",
                        symbol="005930",
                        side="BUY",
                        quantity_delta=1,
                        cumulative_filled=1,
                        timestamp=first_fill_at,
                        price=Decimal("70000"),
                    )
                    ledger.record_fill_transaction(
                        fill_key="buy-order",
                        symbol="005930",
                        side="BUY",
                        quantity_delta=1,
                        cumulative_filled=1,
                        timestamp=first_fill_at,
                        price=Decimal("70000"),
                    )
                    ledger.record_fill_transaction(
                        fill_key="buy-order",
                        symbol="005930",
                        side="BUY",
                        quantity_delta=2,
                        cumulative_filled=3,
                        timestamp=datetime(2026, 7, 10, 9, 20),
                        price=Decimal("71000"),
                    )
                    ledger.record_fill_transaction(
                        fill_key="sell-order",
                        symbol="005930",
                        side="SELL",
                        quantity_delta=1,
                        cumulative_filled=1,
                        timestamp=datetime(2026, 7, 10, 0, 40, tzinfo=timezone.utc),
                        price=Decimal("72000"),
                        realized_pnl=Decimal("2000"),
                    )
                    ledger.record_fill_transaction(
                        fill_key="sell-order",
                        symbol="005930",
                        side="SELL",
                        quantity_delta=1,
                        cumulative_filled=1,
                        timestamp=datetime(2026, 7, 10, 0, 40, tzinfo=timezone.utc),
                        price=Decimal("72000"),
                        realized_pnl=Decimal("2000"),
                    )
                    if label == "json":
                        payload = json.loads(json_path.read_text(encoding="utf-8"))
                        profit_payload = payload["profit_history_by_hour"]
                        self.assertEqual(
                            ["2026-07-10T09:00:00+09:00"],
                            list(profit_payload),
                        )
                        self.assertNotIn("buy-order", json.dumps(profit_payload))
                        self.assertNotIn("sell-order", json.dumps(profit_payload))
                        ledger = JsonManagedLivePositionLedger(
                            json_path,
                            scope="account-scope",
                        )

                    history = ledger.profit_history(
                        date(2026, 7, 10),
                        date(2026, 7, 10),
                    )

                    self.assertEqual(
                        (
                            ManagedProfitAggregate(
                                period_start=datetime(
                                    2026,
                                    7,
                                    10,
                                    9,
                                    tzinfo=KST,
                                ),
                                period_end=datetime(
                                    2026,
                                    7,
                                    10,
                                    10,
                                    tzinfo=KST,
                                ),
                                realized_pnl=Decimal("2000"),
                                fill_count=3,
                            ),
                        ),
                        history,
                    )
                    self.assertEqual(
                        {date(2026, 7, 10): Decimal("2000")},
                        ledger.daily_realized_pnl(
                            date(2026, 7, 10),
                            date(2026, 7, 10),
                        ),
                    )

    def test_profit_history_converts_aware_timestamp_across_kst_date_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonManagedLivePositionLedger(
                Path(tmp) / "managed-live-positions.json",
                scope="account-scope",
            )
            timestamp = datetime(2026, 7, 9, 15, 30, tzinfo=timezone.utc)

            ledger.record_fill_transaction(
                fill_key="buy-order",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=timestamp,
                price=Decimal("70000"),
            )
            ledger.record_fill_transaction(
                fill_key="sell-order",
                symbol="005930",
                side="SELL",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=timestamp,
                price=Decimal("70100"),
                realized_pnl=Decimal("100"),
            )

            self.assertEqual((), ledger.profit_history(date(2026, 7, 9), date(2026, 7, 9)))
            history = ledger.profit_history(date(2026, 7, 10), date(2026, 7, 10))
            self.assertEqual(1, len(history))
            self.assertEqual(
                datetime(2026, 7, 10, 0, tzinfo=KST),
                history[0].period_start,
            )
            self.assertEqual(2, history[0].fill_count)
            self.assertEqual(Decimal("100"), history[0].realized_pnl)
            self.assertEqual(
                {date(2026, 7, 10): Decimal("100")},
                ledger.daily_realized_pnl(date(2026, 7, 9), date(2026, 7, 10)),
            )

    def test_replayed_multi_step_partial_sell_records_each_realized_delta_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonManagedLivePositionLedger(
                Path(tmp) / "managed-live-positions.json",
                scope="account-scope",
            )
            timestamp = datetime(2026, 7, 10, 10, 5, tzinfo=KST)
            ledger.record_fill_transaction(
                fill_key="buy-order",
                symbol="005930",
                side="BUY",
                quantity_delta=5,
                cumulative_filled=5,
                timestamp=timestamp,
                price=Decimal("70000"),
            )
            for quantity_delta, cumulative_filled, realized_pnl in (
                (2, 2, Decimal("200")),
                (2, 2, Decimal("200")),
                (3, 5, Decimal("300")),
                (3, 5, Decimal("300")),
            ):
                ledger.record_fill_transaction(
                    fill_key="sell-order",
                    symbol="005930",
                    side="SELL",
                    quantity_delta=quantity_delta,
                    cumulative_filled=cumulative_filled,
                    timestamp=timestamp,
                    price=Decimal("70100"),
                    realized_pnl=realized_pnl,
                )

            history = ledger.profit_history(date(2026, 7, 10), date(2026, 7, 10))

            self.assertEqual(1, len(history))
            self.assertEqual(Decimal("500"), history[0].realized_pnl)
            self.assertEqual(3, history[0].fill_count)
            self.assertEqual(
                {date(2026, 7, 10): Decimal("500")},
                ledger.daily_realized_pnl(date(2026, 7, 10), date(2026, 7, 10)),
            )
            self.assertEqual({}, ledger.all())

    def test_schema_three_migrates_daily_profit_without_fabricating_hourly_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "scope": "account-scope",
                        "positions": {},
                        "consumed_fills": {},
                        "consumed_notional_by_fill": {},
                        "realized_pnl_by_date": {
                            "2026-07-09": "-250",
                            "2026-07-10": "1250",
                        },
                        "position_lifecycle_by_symbol": {},
                        "entry_counts_by_date": {},
                        "entry_count_unknown_dates": [],
                        "position_lifecycle_unknown_symbols": [],
                    }
                ),
                encoding="utf-8",
            )

            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            ledger.ensure_ready()
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            self.assertEqual(
                {
                    date(2026, 7, 9): Decimal("-250"),
                    date(2026, 7, 10): Decimal("1250"),
                },
                restored.daily_realized_pnl(date(2026, 7, 1), date(2026, 7, 31)),
            )
            self.assertEqual(
                (),
                restored.profit_history(date(2026, 7, 1), date(2026, 7, 31)),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertGreater(payload["schema_version"], 3)
            self.assertEqual(
                {"2026-07-09": "-250", "2026-07-10": "1250"},
                payload["realized_pnl_by_date"],
            )
            self.assertEqual({}, payload["profit_history_by_hour"])
            self.assertEqual({}, payload["account_quantity_confirmations"])

    def test_profit_history_rejects_reversed_date_range(self):
        for ledger in (
            InMemoryManagedLivePositionLedger(),
            JsonManagedLivePositionLedger("unused-managed-live-positions.json"),
        ):
            with self.subTest(ledger=type(ledger).__name__):
                with self.assertRaisesRegex(ValueError, "start date"):
                    ledger.profit_history(date(2026, 7, 11), date(2026, 7, 10))
                with self.assertRaisesRegex(ValueError, "start date"):
                    ledger.daily_realized_pnl(date(2026, 7, 11), date(2026, 7, 10))

    def test_record_consumed_fill_is_idempotent_by_fill_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_consumed_fill(
                fill_key="2026-07-03:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )
            ledger.record_consumed_fill(
                fill_key="2026-07-03:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )

            self.assertEqual(1, ledger.quantity_for("005930"))
            self.assertEqual(1, ledger.consumed_quantity_for("2026-07-03:123:005930:BUY"))

    def test_record_consumed_fill_applies_only_new_cumulative_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_consumed_fill(
                fill_key="2026-07-03:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )
            ledger.record_consumed_fill(
                fill_key="2026-07-03:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=2,
                cumulative_filled=3,
            )

            self.assertEqual(3, ledger.quantity_for("005930"))
            self.assertEqual(3, ledger.consumed_quantity_for("2026-07-03:123:005930:BUY"))

    def test_scoped_ledger_persists_consumed_fill_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_consumed_fill(
                fill_key="2026-07-03:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            self.assertEqual(1, restored.quantity_for("005930"))
            self.assertEqual(1, restored.consumed_quantity_for("2026-07-03:123:005930:BUY"))

    def test_scoped_ledger_persists_realized_pnl_by_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_realized_pnl(date(2026, 7, 3), Decimal("1010"))
            ledger.record_realized_pnl(date(2026, 7, 3), Decimal("-120"))
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            self.assertEqual(Decimal("890"), restored.realized_pnl_today(date(2026, 7, 3)))
            self.assertEqual(Decimal("0"), restored.realized_pnl_today(date(2026, 7, 4)))

    def test_position_lifecycle_reopens_from_json_as_an_immutable_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            opened_at = datetime(2026, 7, 10, 9, 1, 2)

            ledger.add("005930", 1)
            ledger.initialize_lifecycle("005930", opened_at, Decimal("70100"))
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            lifecycle = restored.lifecycle_for("005930")
            self.assertIsNotNone(lifecycle)
            self.assertEqual(opened_at, lifecycle.opened_at)
            self.assertEqual(Decimal("70100"), lifecycle.highest_price)
            self.assertEqual(Decimal("70100"), lifecycle.lowest_price)
            with self.assertRaises(FrozenInstanceError):
                lifecycle.highest_price = Decimal("70200")

    def test_lifecycle_is_explicit_and_scale_in_preserves_opened_at_and_extrema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            first_opened_at = datetime(2026, 7, 10, 9, 5)

            ledger.add("005930", 1)
            self.assertIsNone(ledger.lifecycle_for("005930"))

            ledger.initialize_lifecycle("005930", first_opened_at, Decimal("70000"))
            ledger.add("005930", 2)
            ledger.initialize_lifecycle("005930", datetime(2026, 7, 10, 9, 10), Decimal("71000"))
            ledger.update_lifecycle_price("005930", datetime(2026, 7, 10, 9, 15), Decimal("69000"))

            lifecycle = ledger.lifecycle_for("005930")
            self.assertEqual(first_opened_at, lifecycle.opened_at)
            self.assertEqual(Decimal("71000"), lifecycle.highest_price)
            self.assertEqual(Decimal("69000"), lifecycle.lowest_price)

    def test_lifecycle_is_removed_when_quantity_reaches_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            ledger.add("005930", 2)
            ledger.initialize_lifecycle("005930", datetime(2026, 7, 10, 9, 5), Decimal("70000"))

            ledger.subtract("005930", 1)
            self.assertIsNotNone(ledger.lifecycle_for("005930"))
            ledger.subtract("005930", 1)

            self.assertEqual(0, ledger.quantity_for("005930"))
            self.assertIsNone(ledger.lifecycle_for("005930"))

    def test_reconciled_sell_removes_lifecycle_when_quantity_reaches_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            ledger.add("005930", 1)
            ledger.initialize_lifecycle("005930", datetime(2026, 7, 10, 9, 5), Decimal("70000"))

            ledger.record_consumed_fill(
                fill_key="2026-07-10:124:005930:SELL",
                symbol="005930",
                side="SELL",
                quantity_delta=1,
                cumulative_filled=1,
            )

            self.assertEqual(0, ledger.quantity_for("005930"))
            self.assertIsNone(ledger.lifecycle_for("005930"))

    def test_entry_counts_reopen_and_are_independent_by_symbol_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_entry("005930", date(2026, 7, 10))
            ledger.record_entry("005930", date(2026, 7, 10), count=2)
            ledger.record_entry("005930", date(2026, 7, 11))
            ledger.record_entry("000660", date(2026, 7, 10))
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")

            self.assertEqual(
                {
                    ("005930", date(2026, 7, 10)): 3,
                    ("005930", date(2026, 7, 11)): 1,
                    ("000660", date(2026, 7, 10)): 1,
                },
                restored.entry_counts(),
            )

    def test_fill_transaction_records_partial_buy_once_per_order_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            first_fill_at = datetime(2026, 7, 10, 9, 5)

            first = ledger.record_fill_transaction(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=first_fill_at,
                price=Decimal("70000"),
            )
            duplicate = ledger.record_fill_transaction(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=first_fill_at,
                price=Decimal("70000"),
            )
            second_partial = ledger.record_fill_transaction(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=2,
                cumulative_filled=3,
                timestamp=datetime(2026, 7, 10, 9, 6),
                price=Decimal("71500"),
            )

            lifecycle = ledger.lifecycle_for("005930")
            self.assertEqual(1, first.applied_quantity)
            self.assertTrue(first.entry_recorded)
            self.assertEqual(0, duplicate.applied_quantity)
            self.assertFalse(duplicate.entry_recorded)
            self.assertEqual(2, second_partial.applied_quantity)
            self.assertFalse(second_partial.entry_recorded)
            self.assertEqual(3, ledger.quantity_for("005930"))
            self.assertEqual(3, ledger.consumed_quantity_for("2026-07-10:123:005930:BUY"))
            self.assertEqual(
                Decimal("213000"),
                ledger.consumed_notional_for("2026-07-10:123:005930:BUY"),
            )
            self.assertEqual({("005930", date(2026, 7, 10)): 1}, ledger.entry_counts())
            self.assertEqual(first_fill_at, lifecycle.opened_at)
            self.assertEqual(Decimal("71500"), lifecycle.highest_price)
            self.assertEqual(Decimal("70000"), lifecycle.lowest_price)

    def test_fill_transaction_counts_distinct_buy_orders_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            for order_no in ("123", "124"):
                ledger.record_fill_transaction(
                    fill_key=f"2026-07-10:{order_no}:005930:BUY",
                    symbol="005930",
                    side="BUY",
                    quantity_delta=1,
                    cumulative_filled=1,
                    timestamp=datetime(2026, 7, 10, 9, 5),
                    price=Decimal("70000"),
                )

            self.assertEqual({("005930", date(2026, 7, 10)): 2}, ledger.entry_counts())

    def test_fill_transaction_rejects_mismatched_delta_without_advancing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            with self.assertRaisesRegex(ValueError, "delta does not match"):
                ledger.record_fill_transaction(
                    fill_key="2026-07-10:123:005930:BUY",
                    symbol="005930",
                    side="BUY",
                    quantity_delta=1,
                    cumulative_filled=3,
                    timestamp=datetime(2026, 7, 10, 9, 5),
                    price=Decimal("70000"),
                )

            self.assertEqual(0, ledger.quantity_for("005930"))
            self.assertEqual(0, ledger.consumed_quantity_for("2026-07-10:123:005930:BUY"))
            self.assertEqual({}, ledger.entry_counts())

    def test_fill_transaction_uses_one_document_write_and_preserves_file_on_failure(self):
        class CountingLedger(JsonManagedLivePositionLedger):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.write_count = 0
                self.fail_writes = False

            def _write_document(self, document):
                self.write_count += 1
                if self.fail_writes:
                    raise OSError("replace failed")
                return super()._write_document(document)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = CountingLedger(path, scope="account-scope")
            ledger.ensure_ready()
            ledger.write_count = 0

            ledger.record_fill_transaction(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=datetime(2026, 7, 10, 9, 5),
                price=Decimal("70000"),
            )
            self.assertEqual(1, ledger.write_count)
            before_failure = path.read_text(encoding="utf-8")

            ledger.fail_writes = True
            with self.assertRaisesRegex(OSError, "replace failed"):
                ledger.record_fill_transaction(
                    fill_key="2026-07-10:124:000660:BUY",
                    symbol="000660",
                    side="BUY",
                    quantity_delta=1,
                    cumulative_filled=1,
                    timestamp=datetime(2026, 7, 10, 9, 6),
                    price=Decimal("120000"),
                )

            self.assertEqual(before_failure, path.read_text(encoding="utf-8"))
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")
            self.assertEqual({"005930": 1}, restored.all())
            self.assertEqual({("005930", date(2026, 7, 10)): 1}, restored.entry_counts())

    def test_fill_transaction_replace_failure_keeps_previous_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            ledger.ensure_ready()
            before_failure = path.read_text(encoding="utf-8")

            with patch.object(Path, "replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    ledger.record_fill_transaction(
                        fill_key="2026-07-10:123:005930:BUY",
                        symbol="005930",
                        side="BUY",
                        quantity_delta=1,
                        cumulative_filled=1,
                        timestamp=datetime(2026, 7, 10, 9, 5),
                        price=Decimal("70000"),
                    )

            self.assertEqual(before_failure, path.read_text(encoding="utf-8"))
            restored = JsonManagedLivePositionLedger(path, scope="account-scope")
            self.assertEqual({}, restored.all())
            self.assertEqual({}, restored.entry_counts())
            self.assertEqual({}, restored.account_quantity_confirmations())

    def test_consumed_fill_does_not_implicitly_record_an_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

            ledger.record_consumed_fill(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )

            self.assertEqual({}, ledger.entry_counts())

    def test_legacy_structured_document_migrates_missing_fields_without_losing_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "scope": "account-scope",
                        "positions": {"005930": 2},
                        "consumed_fills": {"fill-1": 2},
                        "realized_pnl_by_date": {"2026-07-10": "1250"},
                    }
                ),
                encoding="utf-8",
            )

            migration_day = date(2026, 7, 10)
            ledger = JsonManagedLivePositionLedger(
                path,
                scope="account-scope",
                trading_day_provider=lambda: migration_day,
            )
            ledger.ensure_ready()
            migrated = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual("account-scope", migrated["scope"])
            self.assertEqual({"005930": 2}, migrated["positions"])
            self.assertEqual({"fill-1": 2}, migrated["consumed_fills"])
            self.assertEqual({}, migrated["consumed_notional_by_fill"])
            self.assertEqual({"2026-07-10": "1250"}, migrated["realized_pnl_by_date"])
            self.assertEqual({}, migrated["position_lifecycle_by_symbol"])
            self.assertEqual({}, migrated["entry_counts_by_date"])
            self.assertGreaterEqual(migrated["schema_version"], 1)
            self.assertEqual(["2026-07-10"], migrated["entry_count_unknown_dates"])
            self.assertEqual(["005930"], migrated["position_lifecycle_unknown_symbols"])
            self.assertFalse(ledger.entry_counts_are_known(migration_day))
            self.assertTrue(ledger.entry_counts_are_known(date(2026, 7, 11)))
            self.assertFalse(ledger.position_lifecycle_is_known("005930"))
            self.assertIsNone(ledger.consumed_notional_for("fill-1"))

            restored = JsonManagedLivePositionLedger(
                path,
                scope="account-scope",
                trading_day_provider=lambda: date(2026, 7, 11),
            )
            self.assertTrue(restored.entry_counts_are_known(date(2026, 7, 11)))

    def test_replace_entry_counts_for_date_atomically_marks_migrated_day_known(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "scope": "account-scope",
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                        "entry_counts_by_date": {
                            "2026-07-10": {"005930": 9},
                            "2026-07-11": {"000660": 2},
                        },
                    }
                ),
                encoding="utf-8",
            )
            ledger = JsonManagedLivePositionLedger(
                path,
                scope="account-scope",
                trading_day_provider=lambda: date(2026, 7, 10),
            )
            ledger.ensure_ready()

            ledger.replace_entry_counts_for_date(
                date(2026, 7, 10),
                {"005930": 1, "035420": 2, "000660": 0},
            )

            self.assertTrue(ledger.entry_counts_are_known(date(2026, 7, 10)))
            self.assertEqual(
                {
                    ("005930", date(2026, 7, 10)): 1,
                    ("035420", date(2026, 7, 10)): 2,
                    ("000660", date(2026, 7, 11)): 2,
                },
                ledger.entry_counts(),
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual([], payload["entry_count_unknown_dates"])

    def test_invalid_entry_count_reconciliation_keeps_migrated_day_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            trading_day = date(2026, 7, 10)
            ledger = JsonManagedLivePositionLedger(
                path,
                trading_day_provider=lambda: trading_day,
            )
            ledger.ensure_ready()

            with self.assertRaisesRegex(ValueError, "nonnegative integer"):
                ledger.replace_entry_counts_for_date(trading_day, {"005930": -1})

            self.assertFalse(ledger.entry_counts_are_known(trading_day))
            self.assertEqual({}, ledger.entry_counts())

    def test_writes_preserve_scope_and_every_structured_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(path, scope="account-scope")
            opened_at = datetime(2026, 7, 10, 9, 5)
            ledger.add("005930", 1)
            ledger.initialize_lifecycle("005930", opened_at, Decimal("70000"))
            ledger.record_entry("005930", date(2026, 7, 10), count=2)
            ledger.record_consumed_fill(
                fill_key="2026-07-10:123:005930:BUY",
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )
            ledger.record_realized_pnl(date(2026, 7, 10), Decimal("100"))

            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual("account-scope", payload["scope"])
            self.assertEqual({"005930": 2}, payload["positions"])
            self.assertEqual({"2026-07-10:123:005930:BUY": 1}, payload["consumed_fills"])
            self.assertEqual({}, payload["consumed_notional_by_fill"])
            self.assertEqual({"2026-07-10": "100"}, payload["realized_pnl_by_date"])
            self.assertEqual(
                {
                    "005930": {
                        "opened_at": opened_at.isoformat(),
                        "highest_price": "70000",
                        "lowest_price": "70000",
                    }
                },
                payload["position_lifecycle_by_symbol"],
            )
            self.assertEqual({"2026-07-10": {"005930": 2}}, payload["entry_counts_by_date"])

    def test_malformed_scoped_lifecycle_and_entry_count_fields_fail_closed(self):
        malformed_fields = (
            ("position_lifecycle_by_symbol", []),
            (
                "position_lifecycle_by_symbol",
                {
                    "005930": {
                        "opened_at": "not-a-datetime",
                        "highest_price": "70000",
                        "lowest_price": "69000",
                    }
                },
            ),
            ("entry_counts_by_date", []),
            ("entry_counts_by_date", {"not-a-date": {"005930": 1}}),
            ("entry_counts_by_date", {"2026-07-10": {"005930": -1}}),
        )
        for field, value in malformed_fields:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "managed-live-positions.json"
                path.write_text(
                    json.dumps(
                        {
                            "scope": "account-scope",
                            "positions": {},
                            "consumed_fills": {},
                            "realized_pnl_by_date": {},
                            field: value,
                        }
                    ),
                    encoding="utf-8",
                )

                ledger = JsonManagedLivePositionLedger(path, scope="account-scope")

                with self.assertRaisesRegex(ValueError, "invalid managed live position ledger"):
                    ledger.ensure_ready()


if __name__ == "__main__":
    unittest.main()
