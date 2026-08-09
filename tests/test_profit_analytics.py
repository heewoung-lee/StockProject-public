from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from stockbot.profit_analytics import (
    KST,
    ProfitAnalyticsService,
    SqliteAccountProfitStore,
)
from stockbot.kis_models import parse_kis_realized_profit_row_today


@dataclass(frozen=True)
class _KisProfitRow:
    trading_date: date
    realized_pnl: Decimal
    fee: Decimal
    tax: Decimal
    loan_interest: Decimal
    has_activity: bool | None = True


@dataclass(frozen=True)
class _ManagedAggregate:
    period_start: datetime
    period_end: datetime
    realized_pnl: Decimal
    fill_count: int


class _ManagedLedger:
    def __init__(
        self,
        *,
        hourly: tuple[_ManagedAggregate, ...] = (),
        daily: dict[date, Decimal] | None = None,
    ) -> None:
        self.hourly = hourly
        self.daily = daily or {}

    def profit_history(self, start_date: date, end_date: date) -> tuple[_ManagedAggregate, ...]:
        return tuple(
            item
            for item in self.hourly
            if start_date <= item.period_start.astimezone(KST).date() <= end_date
        )

    def daily_realized_pnl(self, start_date: date, end_date: date) -> dict[date, Decimal]:
        return {
            day: amount
            for day, amount in self.daily.items()
            if start_date <= day <= end_date
        }


class SqliteAccountProfitStoreTests(unittest.TestCase):
    def test_reading_a_missing_store_does_not_create_or_write_a_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profit.sqlite3"
            store = SqliteAccountProfitStore(path, scope="scope")

            self.assertEqual({}, store.daily_records(date(2026, 7, 1), date(2026, 7, 31)))
            self.assertEqual((), store.snapshots(date(2026, 7, 29)))
            self.assertEqual(set(), store.covered_dates(date(2026, 7, 1), date(2026, 7, 31)))
            self.assertEqual(
                set(),
                store.exact_covered_dates(date(2026, 7, 1), date(2026, 7, 31)),
            )
            self.assertIsNone(store.latest_observed_at())
            self.assertFalse(path.exists())

    def test_schema_one_reads_without_writing_then_migrates_on_explicit_initialization(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profit.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    CREATE TABLE daily_account_profit (
                        trading_date TEXT PRIMARY KEY,
                        reported_realized_pnl TEXT NOT NULL,
                        fee TEXT NOT NULL,
                        tax TEXT NOT NULL,
                        loan_interest TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    );
                    INSERT INTO metadata(key, value) VALUES('account_scope', 'scope');
                    INSERT INTO metadata(key, value) VALUES('schema_version', '1');
                    INSERT INTO daily_account_profit VALUES(
                        '2026-07-29', '1200', '10', '20', '0',
                        '2026-07-29T15:31:00+09:00'
                    );
                    """
                )
            connection.close()
            store = SqliteAccountProfitStore(path, scope="scope")

            daily = store.daily_records(date(2026, 7, 29), date(2026, 7, 29))

            self.assertEqual("unknown", daily[date(2026, 7, 29)].activity_status)
            with sqlite3.connect(path) as connection:
                columns_before = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(daily_account_profit)"
                    ).fetchall()
                }
            connection.close()
            self.assertNotIn("activity_status", columns_before)

            store.ensure_ready()

            with sqlite3.connect(path) as connection:
                columns_after = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(daily_account_profit)"
                    ).fetchall()
                }
                schema_version = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()[0]
            connection.close()
            self.assertIn("activity_status", columns_after)
            self.assertEqual("2", schema_version)

    def test_persists_daily_rows_coverage_and_current_day_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profit.sqlite3"
            store = SqliteAccountProfitStore(path, scope="account-scope")
            observed_at = datetime(2026, 7, 29, 10, 15, tzinfo=KST)
            rows = (
                _KisProfitRow(
                    trading_date=date(2026, 7, 28),
                    realized_pnl=Decimal("-1200"),
                    fee=Decimal("100"),
                    tax=Decimal("200"),
                    loan_interest=Decimal("0"),
                ),
                _KisProfitRow(
                    trading_date=date(2026, 7, 29),
                    realized_pnl=Decimal("3400"),
                    fee=Decimal("120"),
                    tax=Decimal("240"),
                    loan_interest=Decimal("10"),
                ),
            )

            store.record_kis_period(
                rows,
                observed_at=observed_at,
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 29),
            )

            restored = SqliteAccountProfitStore(path, scope="account-scope")
            daily = restored.daily_records(date(2026, 7, 1), date(2026, 7, 31))
            snapshots = restored.snapshots(date(2026, 7, 29))
            self.assertEqual(Decimal("-1200"), daily[date(2026, 7, 28)].reported_realized_pnl)
            self.assertEqual(Decimal("370"), daily[date(2026, 7, 29)].trading_cost)
            self.assertEqual(Decimal("3400"), snapshots[-1].reported_realized_pnl)
            self.assertTrue(restored.is_date_covered(date(2026, 7, 15)))
            self.assertFalse(restored.is_date_covered(date(2026, 6, 30)))

    def test_successful_empty_current_day_records_zero_snapshot_without_faking_daily_trade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (),
                observed_at=datetime(2026, 7, 29, 9, 2, tzinfo=KST),
                start_date=date(2026, 7, 29),
                end_date=date(2026, 7, 29),
            )

            self.assertEqual({}, store.daily_records(date(2026, 7, 29), date(2026, 7, 29)))
            snapshots = store.snapshots(date(2026, 7, 29))
            self.assertEqual(1, len(snapshots))
            self.assertEqual(Decimal("0"), snapshots[0].reported_realized_pnl)
            self.assertTrue(store.is_date_covered(date(2026, 7, 29)))

    def test_exact_empty_kis_response_is_preserved_as_no_trade_activity(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            trading_day = date(2026, 7, 29)
            row = parse_kis_realized_profit_row_today(
                {"output1": [], "output2": {}},
                trading_date=trading_day,
            )
            store.record_kis_period(
                (row,),
                observed_at=datetime(2026, 7, 29, 15, 31, tzinfo=KST),
                start_date=trading_day,
                end_date=trading_day,
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 30, 9, 0, tzinfo=KST),
            )

            report = service.query(
                granularity="day",
                scope="account",
                anchor=trading_day.isoformat(),
            )
            bucket = next(item for item in report["buckets"] if item["key"] == trading_day.isoformat())

            self.assertEqual("no_trade", bucket["status"])
            self.assertEqual("no_trade", bucket["activityStatus"])
            self.assertEqual(0, bucket["reportedRealizedPnlKrw"])
            self.assertEqual(0, bucket["fillCount"])

    def test_current_day_keeps_trade_activity_separate_from_provisional_status(self):
        trading_day = date(2026, 7, 29)
        now = datetime(2026, 7, 29, 12, 0, tzinfo=KST)
        with tempfile.TemporaryDirectory() as temp_dir:
            no_trade_store = SqliteAccountProfitStore(
                Path(temp_dir) / "no-trade.sqlite3",
                scope="no-trade",
            )
            no_trade_store.record_kis_period(
                (
                    parse_kis_realized_profit_row_today(
                        {"output1": [], "output2": {}},
                        trading_date=trading_day,
                    ),
                ),
                observed_at=now,
                start_date=trading_day,
                end_date=trading_day,
            )
            trade_store = SqliteAccountProfitStore(
                Path(temp_dir) / "trade.sqlite3",
                scope="trade",
            )
            trade_store.record_kis_period(
                (
                    _KisProfitRow(
                        trading_date=trading_day,
                        realized_pnl=Decimal("0"),
                        fee=Decimal("0"),
                        tax=Decimal("0"),
                        loan_interest=Decimal("0"),
                        has_activity=True,
                    ),
                ),
                observed_at=now,
                start_date=trading_day,
                end_date=trading_day,
            )

            no_trade_report = ProfitAnalyticsService(
                account_store=no_trade_store,
                managed_ledger=None,
                now_provider=lambda: now,
            ).query(granularity="day", scope="account", anchor=trading_day.isoformat())
            trade_report = ProfitAnalyticsService(
                account_store=trade_store,
                managed_ledger=None,
                now_provider=lambda: now,
            ).query(granularity="day", scope="account", anchor=trading_day.isoformat())
            no_trade_bucket = no_trade_report["buckets"][-1]
            trade_bucket = trade_report["buckets"][-1]

            self.assertEqual("provisional", no_trade_bucket["status"])
            self.assertEqual("provisional", trade_bucket["status"])
            self.assertEqual("no_trade", no_trade_bucket["activityStatus"])
            self.assertEqual("trade", trade_bucket["activityStatus"])

    def test_month_coverage_without_today_row_does_not_create_a_false_zero_snapshot(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")

            store.record_kis_period(
                (
                    _KisProfitRow(
                        trading_date=date(2026, 7, 28),
                        realized_pnl=Decimal("100"),
                        fee=Decimal("1"),
                        tax=Decimal("2"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                observed_at=datetime(2026, 7, 29, 9, 2, tzinfo=KST),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 29),
            )

            self.assertEqual((), store.snapshots(date(2026, 7, 29)))
            self.assertTrue(store.is_date_covered(date(2026, 7, 28)))
            self.assertFalse(store.is_date_covered(date(2026, 7, 29)))
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 9, 3, tzinfo=KST),
            )
            report = service.query(
                granularity="day",
                scope="account",
                anchor="2026-07-29",
            )
            today = next(bucket for bucket in report["buckets"] if bucket["key"] == "2026-07-29")
            self.assertEqual("unavailable", today["status"])
            self.assertIsNone(today["reportedRealizedPnlKrw"])

    def test_older_observation_cannot_overwrite_a_newer_daily_account_value(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            trading_day = date(2026, 7, 29)
            for observed_at, realized_pnl in (
                (datetime(2026, 7, 29, 15, 0, tzinfo=KST), Decimal("2500")),
                (datetime(2026, 7, 29, 14, 0, tzinfo=KST), Decimal("1000")),
            ):
                store.record_kis_period(
                    (
                        _KisProfitRow(
                            trading_date=trading_day,
                            realized_pnl=realized_pnl,
                            fee=Decimal("10"),
                            tax=Decimal("20"),
                            loan_interest=Decimal("0"),
                        ),
                    ),
                    observed_at=observed_at,
                    start_date=trading_day,
                    end_date=trading_day,
                )

            daily = store.daily_records(trading_day, trading_day)

            self.assertEqual(Decimal("2500"), daily[trading_day].reported_realized_pnl)
            self.assertEqual(
                datetime(2026, 7, 29, 15, 0, tzinfo=KST),
                daily[trading_day].observed_at,
            )

    def test_rejects_an_existing_database_for_another_account_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profit.sqlite3"
            SqliteAccountProfitStore(path, scope="first").ensure_ready()

            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                SqliteAccountProfitStore(path, scope="second").ensure_ready()


class ProfitAnalyticsServiceTests(unittest.TestCase):
    def test_current_month_daily_report_stops_at_today_without_an_extra_future_bucket(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (),
                observed_at=datetime(2026, 7, 29, 12, 0, tzinfo=KST),
                start_date=date(2026, 7, 29),
                end_date=date(2026, 7, 29),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 12, 1, tzinfo=KST),
            )

            report = service.query(granularity="day", scope="account", anchor="2026-07-29")

            self.assertEqual(29, len(report["buckets"]))
            self.assertEqual("2026-07-01", report["buckets"][0]["key"])
            self.assertEqual("2026-07-29", report["buckets"][-1]["key"])
            self.assertEqual("2026-07-30T00:00:00+09:00", report["range"]["endAt"])

    def test_confirmed_zero_profit_is_not_mislabeled_as_no_trades(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (
                    _KisProfitRow(
                        trading_date=date(2026, 7, 29),
                        realized_pnl=Decimal("0"),
                        fee=Decimal("50"),
                        tax=Decimal("25"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                observed_at=datetime(2026, 8, 1, 9, 0, tzinfo=KST),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 31),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 8, 1, 12, 0, tzinfo=KST),
            )

            report = service.query(granularity="day", scope="account", anchor="2026-07-29")

            self.assertEqual("complete", report["status"])
            bucket = next(item for item in report["buckets"] if item["key"] == "2026-07-29")
            self.assertEqual("confirmed", bucket["status"])
            self.assertEqual("trade", bucket["activityStatus"])
            self.assertEqual(0, bucket["reportedRealizedPnlKrw"])
            self.assertEqual(75, report["summary"]["tradingCostKrw"])

    def test_daily_report_distinguishes_confirmed_no_trade_closed_and_unavailable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (
                    _KisProfitRow(
                        trading_date=date(2026, 7, 10),
                        realized_pnl=Decimal("1200"),
                        fee=Decimal("30"),
                        tax=Decimal("50"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                observed_at=datetime(2026, 7, 10, 15, 31, tzinfo=KST),
                start_date=date(2026, 7, 9),
                end_date=date(2026, 7, 12),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            )

            report = service.query(granularity="day", scope="account", anchor="2026-07-15")
            buckets = {bucket["key"]: bucket for bucket in report["buckets"]}

            self.assertEqual("no_trade", buckets["2026-07-09"]["status"])
            self.assertEqual("no_trade", buckets["2026-07-09"]["activityStatus"])
            self.assertEqual(0, buckets["2026-07-09"]["reportedRealizedPnlKrw"])
            self.assertEqual("confirmed", buckets["2026-07-10"]["status"])
            self.assertEqual("trade", buckets["2026-07-10"]["activityStatus"])
            self.assertEqual(1200, buckets["2026-07-10"]["reportedRealizedPnlKrw"])
            self.assertEqual("market_closed", buckets["2026-07-11"]["status"])
            self.assertEqual("unavailable", buckets["2026-07-13"]["status"])
            self.assertIsNone(buckets["2026-07-13"]["reportedRealizedPnlKrw"])
            self.assertEqual("unknown", report["costInclusion"])

    def test_month_report_aggregates_available_daily_values_without_turning_gaps_into_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (
                    _KisProfitRow(
                        trading_date=date(2026, 1, 5),
                        realized_pnl=Decimal("3000"),
                        fee=Decimal("30"),
                        tax=Decimal("70"),
                        loan_interest=Decimal("0"),
                    ),
                    _KisProfitRow(
                        trading_date=date(2026, 1, 6),
                        realized_pnl=Decimal("-1000"),
                        fee=Decimal("20"),
                        tax=Decimal("40"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                observed_at=datetime(2026, 1, 6, 16, 0, tzinfo=KST),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            )

            report = service.query(granularity="month", scope="account", anchor="2026-07-29")
            january = report["buckets"][0]

            self.assertEqual("2026-01", january["key"])
            self.assertEqual(2000, january["reportedRealizedPnlKrw"])
            self.assertEqual(160, january["feeKrw"] + january["taxKrw"] + january["interestKrw"])
            self.assertEqual("confirmed", january["status"])
            self.assertEqual("trade", january["activityStatus"])
            self.assertIsNone(january["fillCount"])
            self.assertIsNone(report["buckets"][1]["reportedRealizedPnlKrw"])
            self.assertEqual("partial", report["status"])

    def test_month_report_preserves_no_trade_activity_for_a_fully_covered_period(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (),
                observed_at=datetime(2026, 2, 1, 9, 0, tzinfo=KST),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 31),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            )

            report = service.query(granularity="month", scope="account", anchor="2026-07-29")
            january = report["buckets"][0]

            self.assertEqual("confirmed", january["status"])
            self.assertEqual("no_trade", january["activityStatus"])
            self.assertEqual(0, january["reportedRealizedPnlKrw"])
            self.assertEqual(0, january["fillCount"])

    def test_partial_month_does_not_confirm_zero_fills_from_covered_no_trade_days(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            store.record_kis_period(
                (),
                observed_at=datetime(2026, 1, 6, 9, 0, tzinfo=KST),
                start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 5),
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            )

            report = service.query(granularity="month", scope="account", anchor="2026-07-29")
            january = report["buckets"][0]

            self.assertEqual("partial", january["status"])
            self.assertEqual("unknown", january["activityStatus"])
            self.assertEqual(0, january["reportedRealizedPnlKrw"])
            self.assertIsNone(january["fillCount"])

    def test_year_report_marks_fully_covered_no_trade_history_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            today = date(2026, 7, 29)
            store.record_kis_period(
                (),
                observed_at=datetime(2026, 7, 29, 16, 0, tzinfo=KST),
                start_date=date(2020, 1, 1),
                end_date=today,
            )
            store.record_kis_period(
                (
                    parse_kis_realized_profit_row_today(
                        {"output1": [], "output2": {}},
                        trading_date=today,
                    ),
                ),
                observed_at=datetime(2026, 7, 29, 16, 1, tzinfo=KST),
                start_date=today,
                end_date=today,
            )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 16, 0, tzinfo=KST),
            )

            report = service.query(granularity="year", scope="account", anchor=today.isoformat())

            self.assertEqual("empty", report["status"])
            self.assertTrue(report["buckets"])
            self.assertTrue(
                all(bucket["activityStatus"] == "no_trade" for bucket in report["buckets"])
            )
            self.assertTrue(all(bucket["fillCount"] == 0 for bucket in report["buckets"]))

    def test_account_hour_report_uses_cumulative_snapshot_deltas_and_marks_late_baseline_partial(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SqliteAccountProfitStore(Path(temp_dir) / "profit.sqlite3", scope="scope")
            first = datetime(2026, 7, 29, 10, 5, tzinfo=KST)
            second = datetime(2026, 7, 29, 10, 55, tzinfo=KST)
            for observed_at, pnl, fee in (
                (first, "1000", "20"),
                (second, "2400", "50"),
            ):
                store.record_kis_period(
                    (
                        _KisProfitRow(
                            trading_date=date(2026, 7, 29),
                            realized_pnl=Decimal(pnl),
                            fee=Decimal(fee),
                            tax=Decimal("0"),
                            loan_interest=Decimal("0"),
                        ),
                    ),
                    observed_at=observed_at,
                    start_date=date(2026, 7, 29),
                    end_date=date(2026, 7, 29),
                )
            service = ProfitAnalyticsService(
                account_store=store,
                managed_ledger=None,
                now_provider=lambda: datetime(2026, 7, 29, 11, 0, tzinfo=KST),
            )

            report = service.query(granularity="hour", scope="account", anchor="2026-07-29")
            ten_oclock = next(bucket for bucket in report["buckets"] if bucket["key"] == "2026-07-29T10")

            self.assertEqual("partial", ten_oclock["status"])
            self.assertEqual(1400, ten_oclock["reportedRealizedPnlKrw"])
            self.assertEqual(30, ten_oclock["feeKrw"])
            self.assertIn("baseline", " ".join(ten_oclock["issues"]))

    def test_stockbot_scope_reads_only_managed_ledger_and_exposes_gross_cost_unknown(self):
        start = datetime(2026, 7, 29, 9, 0, tzinfo=KST)
        ledger = _ManagedLedger(
            hourly=(
                _ManagedAggregate(
                    period_start=start,
                    period_end=start + timedelta(hours=1),
                    realized_pnl=Decimal("700"),
                    fill_count=3,
                ),
            ),
            daily={date(2026, 7, 29): Decimal("700")},
        )
        service = ProfitAnalyticsService(
            account_store=None,
            managed_ledger=ledger,
            now_provider=lambda: datetime(2026, 7, 29, 12, 0, tzinfo=KST),
        )

        report = service.query(granularity="hour", scope="stockbot", anchor="2026-07-29")
        nine_oclock = next(bucket for bucket in report["buckets"] if bucket["key"] == "2026-07-29T09")

        self.assertEqual(700, nine_oclock["reportedRealizedPnlKrw"])
        self.assertEqual(3, nine_oclock["fillCount"])
        self.assertIsNone(nine_oclock["feeKrw"])
        self.assertEqual("unknown", nine_oclock["costInclusion"])
        self.assertNotIn("account", repr(report).lower())

    def test_rejects_invalid_query_values_and_future_anchor_navigation(self):
        service = ProfitAnalyticsService(
            account_store=None,
            managed_ledger=None,
            now_provider=lambda: datetime(2026, 7, 29, 12, 0, tzinfo=KST),
        )

        with self.assertRaisesRegex(ValueError, "granularity"):
            service.query(granularity="minute", scope="account", anchor="2026-07-29")
        with self.assertRaisesRegex(ValueError, "scope"):
            service.query(granularity="day", scope="all", anchor="2026-07-29")
        with self.assertRaisesRegex(ValueError, "anchor"):
            service.query(granularity="day", scope="account", anchor="07/29/2026")

        report = service.query(granularity="day", scope="account", anchor="2026-07-29")
        self.assertIsNone(report["range"]["nextAnchor"])


if __name__ == "__main__":
    unittest.main()
