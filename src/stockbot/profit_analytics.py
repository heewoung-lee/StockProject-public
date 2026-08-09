from __future__ import annotations

import calendar
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence


KST = timezone(timedelta(hours=9), name="KST")
PROFIT_REPORT_SCHEMA_VERSION = 1
_STORE_SCHEMA_VERSION = "2"
_SUPPORTED_STORE_SCHEMA_VERSIONS = frozenset({"1", _STORE_SCHEMA_VERSION})
_GRANULARITIES = frozenset({"hour", "day", "month", "year"})
_SCOPES = frozenset({"account", "stockbot"})


@dataclass(frozen=True)
class AccountProfitRecord:
    trading_date: date
    reported_realized_pnl: Decimal
    fee: Decimal
    tax: Decimal
    loan_interest: Decimal
    activity_status: str
    observed_at: datetime

    @property
    def trading_cost(self) -> Decimal:
        return self.fee + self.tax + self.loan_interest


@dataclass(frozen=True)
class AccountProfitSnapshot:
    observed_at: datetime
    trading_date: date
    reported_realized_pnl: Decimal
    fee: Decimal
    tax: Decimal
    loan_interest: Decimal


class ManagedProfitAggregateLike(Protocol):
    period_start: datetime
    period_end: datetime
    realized_pnl: Decimal
    fill_count: int


class ManagedProfitLedgerLike(Protocol):
    def profit_history(
        self,
        start_date: date,
        end_date: date,
    ) -> Sequence[ManagedProfitAggregateLike]:
        ...

    def daily_realized_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> Mapping[date, Decimal]:
        ...


class SqliteAccountProfitStore:
    """Account-scoped local profit history without storing account identifiers."""

    def __init__(self, path: str | Path, *, scope: str) -> None:
        self.path = Path(path)
        self.scope = str(scope or "").strip()
        if not self.scope:
            raise ValueError("profit analytics account scope is required")

    def ensure_ready(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_account_profit (
                    trading_date TEXT PRIMARY KEY,
                    reported_realized_pnl TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    tax TEXT NOT NULL,
                    loan_interest TEXT NOT NULL,
                    activity_status TEXT NOT NULL DEFAULT 'unknown',
                    observed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_profit_snapshots (
                    observed_at TEXT PRIMARY KEY,
                    trading_date TEXT NOT NULL,
                    reported_realized_pnl TEXT NOT NULL,
                    fee TEXT NOT NULL,
                    tax TEXT NOT NULL,
                    loan_interest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_profit_snapshots_date
                    ON account_profit_snapshots(trading_date, observed_at);
                CREATE TABLE IF NOT EXISTS query_coverage (
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    PRIMARY KEY(start_date, end_date)
                );
                CREATE TABLE IF NOT EXISTS exact_date_coverage (
                    trading_date TEXT PRIMARY KEY,
                    observed_at TEXT NOT NULL
                );
                """
            )
            saved_scope = self._metadata(connection, "account_scope")
            if saved_scope and saved_scope != self.scope:
                raise ValueError("profit analytics account scope mismatch")
            saved_schema = self._metadata(connection, "schema_version")
            if saved_schema and saved_schema not in _SUPPORTED_STORE_SCHEMA_VERSIONS:
                raise ValueError("unsupported profit analytics schema")
            daily_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(daily_account_profit)").fetchall()
            }
            if "activity_status" not in daily_columns:
                connection.execute(
                    "ALTER TABLE daily_account_profit "
                    "ADD COLUMN activity_status TEXT NOT NULL DEFAULT 'unknown'"
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('account_scope', ?)",
                (self.scope,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (_STORE_SCHEMA_VERSION,),
            )

    def record_kis_period(
        self,
        rows: Iterable[object],
        observed_at: datetime,
        start_date: date,
        end_date: date,
    ) -> None:
        start = _date_value(start_date, "start_date")
        end = _date_value(end_date, "end_date")
        if end < start:
            raise ValueError("profit analytics end_date precedes start_date")
        observed = _as_kst(observed_at)
        parsed_rows = tuple(_account_record_from_kis_row(row, observed) for row in rows)
        for row in parsed_rows:
            if not start <= row.trading_date <= end:
                raise ValueError("KIS profit row is outside the observed date range")
        observed_row = next(
            (row for row in parsed_rows if row.trading_date == observed.date()),
            None,
        )
        coverage_end = end
        if start < end == observed.date() and observed_row is None:
            coverage_end = end - timedelta(days=1)

        self.ensure_ready()
        with self._connection() as connection:
            for row in parsed_rows:
                connection.execute(
                    """
                    INSERT INTO daily_account_profit(
                        trading_date,
                        reported_realized_pnl,
                        fee,
                        tax,
                        loan_interest,
                        activity_status,
                        observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(trading_date) DO UPDATE SET
                        reported_realized_pnl=excluded.reported_realized_pnl,
                        fee=excluded.fee,
                        tax=excluded.tax,
                        loan_interest=excluded.loan_interest,
                        activity_status=excluded.activity_status,
                        observed_at=excluded.observed_at
                    WHERE excluded.observed_at >= daily_account_profit.observed_at
                    """,
                    (
                        row.trading_date.isoformat(),
                        str(row.reported_realized_pnl),
                        str(row.fee),
                        str(row.tax),
                        str(row.loan_interest),
                        row.activity_status,
                        observed.isoformat(),
                    ),
                )
            if coverage_end >= start:
                connection.execute(
                    """
                    INSERT INTO query_coverage(start_date, end_date, observed_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(start_date, end_date) DO UPDATE SET
                        observed_at=excluded.observed_at
                    """,
                    (start.isoformat(), coverage_end.isoformat(), observed.isoformat()),
                )
            if start == end:
                connection.execute(
                    """
                    INSERT INTO exact_date_coverage(trading_date, observed_at)
                    VALUES(?, ?)
                    ON CONFLICT(trading_date) DO UPDATE SET
                        observed_at=excluded.observed_at
                    """,
                    (start.isoformat(), observed.isoformat()),
                )
            if start <= observed.date() <= end:
                current = observed_row
                if current is None and not (start == end == observed.date()):
                    return
                snapshot_values = (
                    (
                        current.reported_realized_pnl,
                        current.fee,
                        current.tax,
                        current.loan_interest,
                    )
                    if current is not None
                    else (Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"))
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO account_profit_snapshots(
                        observed_at,
                        trading_date,
                        reported_realized_pnl,
                        fee,
                        tax,
                        loan_interest
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observed.isoformat(),
                        observed.date().isoformat(),
                        *(str(value) for value in snapshot_values),
                    ),
                )

    def daily_records(self, start_date: date, end_date: date) -> dict[date, AccountProfitRecord]:
        start = _date_value(start_date, "start_date")
        end = _date_value(end_date, "end_date")
        if not self.path.is_file():
            return {}
        with self._read_connection() as connection:
            daily_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(daily_account_profit)").fetchall()
            }
            activity_expression = (
                "activity_status"
                if "activity_status" in daily_columns
                else "'unknown' AS activity_status"
            )
            rows = connection.execute(
                f"""
                SELECT
                    trading_date,
                    reported_realized_pnl,
                    fee,
                    tax,
                    loan_interest,
                    {activity_expression},
                    observed_at
                FROM daily_account_profit
                WHERE trading_date BETWEEN ? AND ?
                ORDER BY trading_date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return {
            date.fromisoformat(row[0]): AccountProfitRecord(
                trading_date=date.fromisoformat(row[0]),
                reported_realized_pnl=_decimal_value(row[1], "reported_realized_pnl"),
                fee=_nonnegative_decimal(row[2], "fee"),
                tax=_nonnegative_decimal(row[3], "tax"),
                loan_interest=_nonnegative_decimal(row[4], "loan_interest"),
                activity_status=_stored_activity_status(row[5]),
                observed_at=_as_kst(datetime.fromisoformat(row[6])),
            )
            for row in rows
        }

    def snapshots(self, trading_date: date) -> tuple[AccountProfitSnapshot, ...]:
        day = _date_value(trading_date, "trading_date")
        if not self.path.is_file():
            return ()
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    observed_at,
                    reported_realized_pnl,
                    fee,
                    tax,
                    loan_interest
                FROM account_profit_snapshots
                WHERE trading_date = ?
                ORDER BY observed_at
                """,
                (day.isoformat(),),
            ).fetchall()
        return tuple(
            AccountProfitSnapshot(
                observed_at=_as_kst(datetime.fromisoformat(row[0])),
                trading_date=day,
                reported_realized_pnl=_decimal_value(row[1], "reported_realized_pnl"),
                fee=_nonnegative_decimal(row[2], "fee"),
                tax=_nonnegative_decimal(row[3], "tax"),
                loan_interest=_nonnegative_decimal(row[4], "loan_interest"),
            )
            for row in rows
        )

    def is_date_covered(self, trading_date: date) -> bool:
        day = _date_value(trading_date, "trading_date")
        return day in self.covered_dates(day, day)

    def covered_dates(self, start_date: date, end_date: date) -> set[date]:
        start = _date_value(start_date, "start_date")
        end = _date_value(end_date, "end_date")
        if end < start:
            raise ValueError("profit analytics end_date precedes start_date")
        if not self.path.is_file():
            return set()
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT start_date, end_date
                FROM query_coverage
                WHERE end_date >= ? AND start_date <= ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        covered: set[date] = set()
        for raw_start, raw_end in rows:
            interval_start = max(start, date.fromisoformat(raw_start))
            interval_end = min(end, date.fromisoformat(raw_end))
            covered.update(_date_range(interval_start, interval_end))
        return covered

    def exact_covered_dates(self, start_date: date, end_date: date) -> set[date]:
        start = _date_value(start_date, "start_date")
        end = _date_value(end_date, "end_date")
        if end < start:
            raise ValueError("profit analytics end_date precedes start_date")
        if not self.path.is_file():
            return set()
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT trading_date
                FROM exact_date_coverage
                WHERE trading_date BETWEEN ? AND ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return {date.fromisoformat(row[0]) for row in rows}

    def latest_observed_at(self) -> datetime | None:
        if not self.path.is_file():
            return None
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT MAX(observed_at)
                FROM (
                    SELECT observed_at FROM daily_account_profit
                    UNION ALL
                    SELECT observed_at FROM account_profit_snapshots
                )
                """
            ).fetchone()
        if not row or not row[0]:
            return None
        return _as_kst(datetime.fromisoformat(row[0]))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _read_connection(self):
        connection = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro",
            timeout=1.0,
            uri=True,
        )
        try:
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA query_only=ON")
            saved_scope = self._metadata(connection, "account_scope")
            if saved_scope and saved_scope != self.scope:
                raise ValueError("profit analytics account scope mismatch")
            saved_schema = self._metadata(connection, "schema_version")
            if saved_schema and saved_schema not in _SUPPORTED_STORE_SCHEMA_VERSIONS:
                raise ValueError("unsupported profit analytics schema")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _metadata(connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else ""


class ProfitAnalyticsService:
    def __init__(
        self,
        *,
        account_store: SqliteAccountProfitStore | None,
        managed_ledger: ManagedProfitLedgerLike | None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.account_store = account_store
        self.managed_ledger = managed_ledger
        self._now_provider = now_provider or (lambda: datetime.now(KST))

    def query(
        self,
        *,
        granularity: str,
        scope: str,
        anchor: str | date | None = None,
    ) -> dict[str, object]:
        unit = str(granularity or "").strip().lower()
        selected_scope = str(scope or "").strip().lower()
        if unit not in _GRANULARITIES:
            raise ValueError("profit report granularity is invalid")
        if selected_scope not in _SCOPES:
            raise ValueError("profit report scope is invalid")
        now = _as_kst(self._now_provider())
        anchor_date = _anchor_date(anchor, now.date())
        period = _report_period(unit, anchor_date, now.date())

        if selected_scope == "account":
            buckets = self._account_buckets(unit, period, now)
            source = "KIS_PERIOD_PROFIT_LOCAL"
            latest = self.account_store.latest_observed_at() if self.account_store is not None else None
        else:
            buckets = self._stockbot_buckets(unit, period, now)
            source = "STOCKBOT_MANAGED_LEDGER"
            latest = None

        report_status = _report_status(buckets)
        values = [
            Decimal(str(bucket["reportedRealizedPnlKrw"]))
            for bucket in buckets
            if bucket["reportedRealizedPnlKrw"] is not None
        ]
        positive = sum((value for value in values if value > 0), Decimal("0"))
        negative = sum((value for value in values if value < 0), Decimal("0"))
        cost_values = [
            Decimal(str(bucket["feeKrw"]))
            + Decimal(str(bucket["taxKrw"]))
            + Decimal(str(bucket["interestKrw"]))
            for bucket in buckets
            if bucket["feeKrw"] is not None
            and bucket["taxKrw"] is not None
            and bucket["interestKrw"] is not None
        ]
        issues = sorted(
            {
                str(issue)
                for bucket in buckets
                for issue in bucket["issues"]
                if str(issue).strip()
            }
        )
        return {
            "schemaVersion": PROFIT_REPORT_SCHEMA_VERSION,
            "generatedAt": now.isoformat(),
            "status": report_status,
            "query": {
                "granularity": unit,
                "scope": selected_scope,
                "anchor": anchor_date.isoformat(),
                "timezone": "Asia/Seoul",
            },
            "range": {
                "label": period.label,
                "startAt": period.start_at.isoformat(),
                "endAt": period.end_at.isoformat(),
                "anchor": anchor_date.isoformat(),
                "previousAnchor": period.previous_anchor.isoformat(),
                "nextAnchor": period.next_anchor.isoformat() if period.next_anchor else None,
            },
            "summary": {
                "reportedRealizedPnlKrw": _number(sum(values, Decimal("0"))) if values else None,
                "profitableBucketsTotalKrw": _number(positive) if values else None,
                "losingBucketsTotalKrw": _number(negative) if values else None,
                "tradingCostKrw": _number(sum(cost_values, Decimal("0"))) if cost_values else None,
                "profitableBucketCount": sum(value > 0 for value in values),
                "losingBucketCount": sum(value < 0 for value in values),
                "availableBucketCount": len(values),
            },
            "buckets": buckets,
            "issues": issues,
            "costInclusion": "unknown",
            "dataSource": source,
            "updatedAt": latest.isoformat() if latest else None,
        }

    def _account_buckets(
        self,
        unit: str,
        period: "_ReportPeriod",
        now: datetime,
    ) -> list[dict[str, object]]:
        if self.account_store is None:
            return [
                _empty_bucket(bucket, status="unavailable", issue="account profit store is unavailable")
                for bucket in _bucket_periods(unit, period, now)
            ]
        if unit == "hour":
            return self._account_hour_buckets(period, now)

        final_day = (period.end_at - timedelta(days=1)).date()
        records = self.account_store.daily_records(period.start_at.date(), final_day)
        covered_dates = self.account_store.covered_dates(
            period.start_at.date(),
            final_day,
        )
        exact_covered_dates = self.account_store.exact_covered_dates(
            period.start_at.date(),
            final_day,
        )
        daily = {
            day: _account_day_value(
                day,
                records.get(day),
                covered=(
                    day in covered_dates
                    and (day != now.date() or day in exact_covered_dates)
                ),
                now=now,
            )
            for day in _date_range(period.start_at.date(), final_day)
        }
        if unit == "day":
            return [
                _bucket_from_daily_value(_day_bucket(day), daily[day])
                for day in _date_range(period.start_at.date(), final_day)
            ]
        return [
            _aggregate_daily_bucket(bucket, daily, now)
            for bucket in _bucket_periods(unit, period, now)
        ]

    def _account_hour_buckets(
        self,
        period: "_ReportPeriod",
        now: datetime,
    ) -> list[dict[str, object]]:
        assert self.account_store is not None
        snapshots = self.account_store.snapshots(period.start_at.date())
        daily_record = self.account_store.daily_records(
            period.start_at.date(),
            period.start_at.date(),
        ).get(period.start_at.date())
        result: list[dict[str, object]] = []
        for bucket in _bucket_periods("hour", period, now):
            before = [item for item in snapshots if item.observed_at < bucket.start_at]
            inside = [
                item
                for item in snapshots
                if bucket.start_at <= item.observed_at < bucket.end_at
            ]
            baseline = before[-1] if before else (inside[0] if inside else None)
            terminal = inside[-1] if inside else None
            if baseline is None or terminal is None:
                result.append(
                    _empty_bucket(
                        bucket,
                        status="unavailable",
                        issue="hourly account snapshots are unavailable",
                    )
                )
                continue
            baseline_missing = not before
            if baseline is terminal:
                if baseline_missing and baseline.reported_realized_pnl != 0:
                    result.append(
                        _empty_bucket(
                            bucket,
                            status="partial",
                            issue="hourly baseline was captured after the bucket started",
                        )
                    )
                    continue
                reported = Decimal("0")
                fee = tax = interest = Decimal("0")
            else:
                reported = terminal.reported_realized_pnl - baseline.reported_realized_pnl
                fee = terminal.fee - baseline.fee
                tax = terminal.tax - baseline.tax
                interest = terminal.loan_interest - baseline.loan_interest
            status = "partial" if baseline_missing else ("provisional" if bucket.end_at > now else "confirmed")
            issues = ["hourly baseline was captured after the bucket started"] if baseline_missing else []
            if any(value != 0 for value in (reported, fee, tax, interest)):
                activity_status = "trade"
            elif daily_record is not None and daily_record.activity_status == "no_trade":
                activity_status = "no_trade"
            else:
                activity_status = "unknown"
            result.append(
                _value_bucket(
                    bucket,
                    reported=reported,
                    fee=max(Decimal("0"), fee),
                    tax=max(Decimal("0"), tax),
                    interest=max(Decimal("0"), interest),
                    fill_count=None,
                    status=status,
                    activity_status=activity_status,
                    issues=issues,
                )
            )
        return result

    def _stockbot_buckets(
        self,
        unit: str,
        period: "_ReportPeriod",
        now: datetime,
    ) -> list[dict[str, object]]:
        if self.managed_ledger is None:
            return [
                _empty_bucket(bucket, status="unavailable", issue="managed profit ledger is unavailable")
                for bucket in _bucket_periods(unit, period, now)
            ]
        start_day = period.start_at.date()
        end_day = (period.end_at - timedelta(days=1)).date()
        hourly = tuple(self.managed_ledger.profit_history(start_day, end_day))
        if unit == "hour":
            by_key = {
                _hour_key(_as_kst(item.period_start)): item
                for item in hourly
            }
            result = []
            for bucket in _bucket_periods("hour", period, now):
                item = by_key.get(bucket.key)
                if item is None:
                    result.append(
                        _empty_bucket(
                            bucket,
                            status="unavailable",
                            issue="managed hourly history was not recorded",
                            costs_known=False,
                        )
                    )
                    continue
                result.append(
                    _value_bucket(
                        bucket,
                        reported=_decimal_value(item.realized_pnl, "managed realized_pnl"),
                        fee=None,
                        tax=None,
                        interest=None,
                        fill_count=max(0, int(item.fill_count)),
                        status="provisional" if bucket.end_at > now else "confirmed",
                        activity_status="trade" if int(item.fill_count) > 0 else "no_trade",
                        issues=["StockBot realized P&L excludes unallocated broker costs"],
                    )
                )
            return result

        daily_values = {
            _date_value(day, "managed trading_date"): _decimal_value(amount, "managed realized_pnl")
            for day, amount in self.managed_ledger.daily_realized_pnl(start_day, end_day).items()
        }
        fills_by_day: dict[date, int] = {}
        for item in hourly:
            day = _as_kst(item.period_start).date()
            fills_by_day[day] = fills_by_day.get(day, 0) + max(0, int(item.fill_count))
        daily = {
            day: _stockbot_day_value(day, daily_values, fills_by_day, now)
            for day in _date_range(start_day, end_day)
        }
        if unit == "day":
            return [
                _bucket_from_daily_value(_day_bucket(day), daily[day])
                for day in _date_range(start_day, end_day)
            ]
        return [
            _aggregate_daily_bucket(bucket, daily, now, costs_known=False)
            for bucket in _bucket_periods(unit, period, now)
        ]


@dataclass(frozen=True)
class _ReportPeriod:
    label: str
    start_at: datetime
    end_at: datetime
    previous_anchor: date
    next_anchor: date | None


@dataclass(frozen=True)
class _BucketPeriod:
    key: str
    label: str
    start_at: datetime
    end_at: datetime


@dataclass(frozen=True)
class _DailyValue:
    reported: Decimal | None
    fee: Decimal | None
    tax: Decimal | None
    interest: Decimal | None
    fill_count: int | None
    status: str
    activity_status: str
    issues: tuple[str, ...] = ()


def _report_period(unit: str, anchor: date, today: date) -> _ReportPeriod:
    if unit == "hour":
        start_day = end_day = anchor
        previous = anchor - timedelta(days=1)
        next_value = anchor + timedelta(days=1)
        next_anchor = next_value if next_value <= today else None
        label = anchor.isoformat()
    elif unit == "day":
        start_day = anchor.replace(day=1)
        end_day = _month_end(anchor)
        previous = (start_day - timedelta(days=1)).replace(day=1)
        next_value = end_day + timedelta(days=1)
        next_anchor = next_value if next_value <= today else None
        label = f"{start_day:%Y.%m.%d} - {min(end_day, today) if start_day <= today <= end_day else end_day:%Y.%m.%d}"
    elif unit == "month":
        start_day = date(anchor.year, 1, 1)
        end_day = date(anchor.year, 12, 31)
        previous = date(anchor.year - 1, 1, 1)
        next_value = date(anchor.year + 1, 1, 1)
        next_anchor = next_value if next_value <= today else None
        label = str(anchor.year)
    else:
        decade_start = (anchor.year // 10) * 10
        start_day = date(decade_start, 1, 1)
        end_day = date(decade_start + 9, 12, 31)
        previous = date(decade_start - 10, 1, 1)
        next_value = date(decade_start + 10, 1, 1)
        next_anchor = next_value if next_value <= today else None
        label = f"{decade_start} - {min(decade_start + 9, today.year)}"

    if start_day <= today <= end_day:
        end_day = today
    return _ReportPeriod(
        label=label,
        start_at=datetime.combine(start_day, time.min, tzinfo=KST),
        end_at=datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=KST),
        previous_anchor=previous,
        next_anchor=next_anchor,
    )


def _bucket_periods(
    unit: str,
    period: _ReportPeriod,
    now: datetime,
) -> list[_BucketPeriod]:
    if unit == "hour":
        day = period.start_at.date()
        market_start = datetime.combine(day, time(9, 0), tzinfo=KST)
        market_end = datetime.combine(day, time(16, 0), tzinfo=KST)
        if day == now.date():
            market_end = min(market_end, now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        return [
            _BucketPeriod(
                key=_hour_key(cursor),
                label=cursor.strftime("%H:00"),
                start_at=cursor,
                end_at=cursor + timedelta(hours=1),
            )
            for cursor in _datetime_range(market_start, market_end, timedelta(hours=1))
        ]
    if unit == "day":
        return [_day_bucket(day) for day in _date_range(period.start_at.date(), (period.end_at - timedelta(days=1)).date())]
    if unit == "month":
        buckets: list[_BucketPeriod] = []
        cursor = period.start_at.date().replace(day=1)
        final_day = (period.end_at - timedelta(days=1)).date()
        while cursor <= final_day:
            next_month = _month_end(cursor) + timedelta(days=1)
            buckets.append(
                _BucketPeriod(
                    key=cursor.strftime("%Y-%m"),
                    label=cursor.strftime("%m월"),
                    start_at=datetime.combine(cursor, time.min, tzinfo=KST),
                    end_at=datetime.combine(min(next_month, final_day + timedelta(days=1)), time.min, tzinfo=KST),
                )
            )
            cursor = next_month
        return buckets
    buckets = []
    final_day = (period.end_at - timedelta(days=1)).date()
    for year in range(period.start_at.year, final_day.year + 1):
        start = date(year, 1, 1)
        end = min(date(year + 1, 1, 1), final_day + timedelta(days=1))
        buckets.append(
            _BucketPeriod(
                key=str(year),
                label=f"{year}년",
                start_at=datetime.combine(start, time.min, tzinfo=KST),
                end_at=datetime.combine(end, time.min, tzinfo=KST),
            )
        )
    return buckets


def _account_day_value(
    day: date,
    record: AccountProfitRecord | None,
    *,
    covered: bool,
    now: datetime,
) -> _DailyValue:
    if day.weekday() >= 5:
        return _DailyValue(None, None, None, None, None, "market_closed", "unknown")
    if record is not None:
        if record.activity_status == "no_trade":
            return _DailyValue(
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                Decimal("0"),
                0,
                "provisional" if day == now.date() else "no_trade",
                "no_trade",
            )
        return _DailyValue(
            record.reported_realized_pnl,
            record.fee,
            record.tax,
            record.loan_interest,
            None,
            "provisional" if day == now.date() else "confirmed",
            _account_record_activity_status(record),
        )
    if covered:
        return _DailyValue(
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            Decimal("0"),
            0,
            "provisional" if day == now.date() else "no_trade",
            "no_trade",
        )
    return _DailyValue(
        None,
        None,
        None,
        None,
        None,
        "unavailable",
        "unknown",
        ("daily KIS history was not collected",),
    )


def _stockbot_day_value(
    day: date,
    daily_values: Mapping[date, Decimal],
    fills_by_day: Mapping[date, int],
    now: datetime,
) -> _DailyValue:
    if day.weekday() >= 5:
        return _DailyValue(None, None, None, None, None, "market_closed", "unknown")
    if day not in daily_values and day not in fills_by_day:
        return _DailyValue(
            None,
            None,
            None,
            None,
            None,
            "unavailable",
            "unknown",
            ("managed daily history was not recorded",),
        )
    return _DailyValue(
        daily_values.get(day, Decimal("0")),
        None,
        None,
        None,
        fills_by_day.get(day),
        "provisional" if day == now.date() else "confirmed",
        "trade",
        ("StockBot realized P&L excludes unallocated broker costs",),
    )


def _aggregate_daily_bucket(
    bucket: _BucketPeriod,
    daily: Mapping[date, _DailyValue],
    now: datetime,
    *,
    costs_known: bool = True,
) -> dict[str, object]:
    values = [
        value
        for day, value in daily.items()
        if bucket.start_at.date() <= day < bucket.end_at.date()
    ]
    available = [value for value in values if value.reported is not None]
    relevant = [value for value in values if value.status != "market_closed"]
    if not available:
        issue = next((issue for value in values for issue in value.issues), "profit history is unavailable")
        return _empty_bucket(bucket, status="unavailable", issue=issue, costs_known=costs_known)
    has_unavailable = any(value.status in {"unavailable", "partial"} for value in relevant)
    is_current = bucket.start_at <= now < bucket.end_at
    status = "partial" if has_unavailable else ("provisional" if is_current else "confirmed")
    activity_status = _aggregate_activity_status(relevant)
    reported = sum((value.reported or Decimal("0") for value in available), Decimal("0"))
    if costs_known:
        fee = sum((value.fee or Decimal("0") for value in available), Decimal("0"))
        tax = sum((value.tax or Decimal("0") for value in available), Decimal("0"))
        interest = sum((value.interest or Decimal("0") for value in available), Decimal("0"))
    else:
        fee = tax = interest = None
    fill_count = (
        sum((value.fill_count or 0 for value in relevant), 0)
        if relevant and all(value.fill_count is not None for value in relevant)
        else None
    )
    issues = sorted({issue for value in values for issue in value.issues})
    return _value_bucket(
        bucket,
        reported=reported,
        fee=fee,
        tax=tax,
        interest=interest,
        fill_count=fill_count,
        status=status,
        activity_status=activity_status,
        issues=issues,
    )


def _bucket_from_daily_value(bucket: _BucketPeriod, value: _DailyValue) -> dict[str, object]:
    if value.reported is None:
        return _empty_bucket(
            bucket,
            status=value.status,
            issue=value.issues[0] if value.issues else "",
            costs_known=value.fee is not None,
        )
    return _value_bucket(
        bucket,
        reported=value.reported,
        fee=value.fee,
        tax=value.tax,
        interest=value.interest,
        fill_count=value.fill_count,
        status=value.status,
        activity_status=value.activity_status,
        issues=value.issues,
    )


def _value_bucket(
    bucket: _BucketPeriod,
    *,
    reported: Decimal,
    fee: Decimal | None,
    tax: Decimal | None,
    interest: Decimal | None,
    fill_count: int | None,
    status: str,
    activity_status: str,
    issues: Iterable[str],
) -> dict[str, object]:
    return {
        "key": bucket.key,
        "label": bucket.label,
        "startAt": bucket.start_at.isoformat(),
        "endAt": bucket.end_at.isoformat(),
        "reportedRealizedPnlKrw": _number(reported),
        "feeKrw": _number(fee) if fee is not None else None,
        "taxKrw": _number(tax) if tax is not None else None,
        "interestKrw": _number(interest) if interest is not None else None,
        "fillCount": fill_count,
        "status": status,
        "activityStatus": activity_status,
        "costInclusion": "unknown",
        "issues": [str(issue) for issue in issues if str(issue).strip()],
    }


def _empty_bucket(
    bucket: _BucketPeriod,
    *,
    status: str,
    issue: str,
    costs_known: bool = False,
    activity_status: str = "unknown",
) -> dict[str, object]:
    zero_cost = 0 if status in {"no_trade"} and costs_known else None
    return {
        "key": bucket.key,
        "label": bucket.label,
        "startAt": bucket.start_at.isoformat(),
        "endAt": bucket.end_at.isoformat(),
        "reportedRealizedPnlKrw": 0 if status == "no_trade" else None,
        "feeKrw": zero_cost,
        "taxKrw": zero_cost,
        "interestKrw": zero_cost,
        "fillCount": 0 if status == "no_trade" else None,
        "status": status,
        "activityStatus": "no_trade" if status == "no_trade" else activity_status,
        "costInclusion": "unknown",
        "issues": [issue] if issue else [],
    }


def _report_status(buckets: Sequence[Mapping[str, object]]) -> str:
    available = [bucket for bucket in buckets if bucket.get("reportedRealizedPnlKrw") is not None]
    if not available:
        return "unavailable"
    relevant = [bucket for bucket in buckets if bucket.get("status") != "market_closed"]
    if any(bucket.get("status") in {"partial", "unavailable"} for bucket in relevant):
        return "partial"
    if relevant and all(bucket.get("activityStatus") == "no_trade" for bucket in relevant):
        return "empty"
    return "complete"


def _account_record_activity_status(record: AccountProfitRecord) -> str:
    if record.activity_status in {"trade", "no_trade"}:
        return record.activity_status
    if any(
        value != 0
        for value in (
            record.reported_realized_pnl,
            record.fee,
            record.tax,
            record.loan_interest,
        )
    ):
        return "trade"
    return "unknown"


def _aggregate_activity_status(values: Sequence[_DailyValue]) -> str:
    if any(value.activity_status == "trade" for value in values):
        return "trade"
    if not values or any(
        value.status in {"partial", "unavailable"} or value.activity_status == "unknown"
        for value in values
    ):
        return "unknown"
    if all(value.activity_status == "no_trade" for value in values):
        return "no_trade"
    return "unknown"


def _account_record_from_kis_row(row: object, observed_at: datetime) -> AccountProfitRecord:
    try:
        trading_date = _date_value(getattr(row, "trading_date"), "trading_date")
        realized = _decimal_value(getattr(row, "realized_pnl"), "realized_pnl")
        fee = _nonnegative_decimal(getattr(row, "fee"), "fee")
        tax = _nonnegative_decimal(getattr(row, "tax"), "tax")
        interest = _nonnegative_decimal(getattr(row, "loan_interest"), "loan_interest")
        activity_status = _activity_status(getattr(row, "has_activity", None))
    except AttributeError as exc:
        raise ValueError("invalid KIS profit row") from exc
    return AccountProfitRecord(
        trading_date,
        realized,
        fee,
        tax,
        interest,
        activity_status,
        observed_at,
    )


def _day_bucket(day: date) -> _BucketPeriod:
    start = datetime.combine(day, time.min, tzinfo=KST)
    return _BucketPeriod(day.isoformat(), day.isoformat(), start, start + timedelta(days=1))


def _hour_key(value: datetime) -> str:
    return _as_kst(value).strftime("%Y-%m-%dT%H")


def _month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def _date_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _datetime_range(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    values: list[datetime] = []
    cursor = start
    while cursor < end:
        values.append(cursor)
        cursor += step
    return values


def _anchor_date(value: str | date | None, fallback: date) -> date:
    if value is None or value == "":
        return fallback
    if isinstance(value, datetime):
        raise ValueError("profit report anchor must be YYYY-MM-DD")
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("profit report anchor must be YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("profit report anchor must be YYYY-MM-DD")
    return parsed


def _date_value(value: object, field: str) -> date:
    if isinstance(value, datetime):
        return _as_kst(value).date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"invalid profit analytics {field}") from exc


def _decimal_value(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid profit analytics {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"invalid profit analytics {field}")
    return parsed


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    parsed = _decimal_value(value, field)
    if parsed < 0:
        raise ValueError(f"invalid profit analytics {field}")
    return parsed


def _activity_status(value: object) -> str:
    if value is True:
        return "trade"
    if value is False:
        return "no_trade"
    if value is None:
        return "unknown"
    raise ValueError("invalid profit analytics activity status")


def _stored_activity_status(value: object) -> str:
    parsed = str(value or "").strip()
    if parsed not in {"trade", "no_trade", "unknown"}:
        raise ValueError("invalid profit analytics activity status")
    return parsed


def _number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)
