import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from stockbot.models import MarketBar
from stockbot.scanner import (
    BarProviderScanner,
    JsonScannerProvider,
    ScannerCandidate,
    ScannerSnapshot,
    StaticScannerProvider,
)


def _bar(symbol: str, close: str = "10000", volume: int = 1000) -> MarketBar:
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 18, 9, 0),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        vwap=price,
        bid=price,
        ask=price,
    )


def _bar_at(symbol: str, minute: int, close: str = "10000") -> MarketBar:
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 18, 8, minute, tzinfo=timezone(timedelta(hours=9))),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        vwap=price,
    )


class ScannerTest(unittest.TestCase):
    def test_snapshot_orders_candidates_by_priority(self):
        snapshot = ScannerSnapshot(
            bars={
                "LOW001": _bar("LOW001", volume=10),
                "HIGH01": _bar("HIGH01", volume=100),
            },
            candidates=(
                ScannerCandidate("LOW001", priority=10.0),
                ScannerCandidate("HIGH01", priority=100.0),
            ),
        )

        self.assertEqual(["HIGH01", "LOW001"], [candidate.symbol for candidate in snapshot.ordered_candidates()])
        self.assertEqual(["HIGH01", "LOW001"], snapshot.ordered_symbols(["LOW001", "HIGH01"]))

    def test_static_provider_returns_snapshot_for_requested_symbols_only(self):
        provider = StaticScannerProvider(
            bars={
                "BUY001": _bar("BUY001", "12000", 500),
                "BUY002": _bar("BUY002", "8000", 900),
                "SKIP01": _bar("SKIP01", "7000", 100),
            },
            priorities={"BUY001": 500.0, "BUY002": 900.0, "SKIP01": 100.0},
        )

        snapshot = provider.snapshot(["BUY001", "BUY002"])

        self.assertEqual(["BUY002", "BUY001"], snapshot.ordered_symbols(["BUY001", "BUY002"]))
        self.assertEqual({"BUY001", "BUY002"}, set(snapshot.bars))
        self.assertNotIn("SKIP01", snapshot.bars)
        self.assertEqual(900.0, snapshot.priority("BUY002"))
        self.assertEqual(0.0, snapshot.priority("UNKNOWN"))

    def test_static_provider_returns_sorted_deduplicated_history_for_requested_symbols(self):
        provider = StaticScannerProvider(
            bars={
                "BUY001": _bar_at("BUY001", 59),
                "SKIP01": _bar_at("SKIP01", 59),
            },
            histories={
                "BUY001": (
                    _bar_at("BUY001", 58, "9800"),
                    _bar_at("BUY001", 57, "9700"),
                    _bar_at("BUY001", 58, "9850"),
                    _bar_at("OTHER1", 56, "9600"),
                    _bar_at("BUY001", 59, "9900"),
                ),
                "SKIP01": (_bar_at("SKIP01", 58),),
            },
        )

        snapshot = provider.snapshot(["BUY001"])

        self.assertEqual({"BUY001"}, set(snapshot.histories))
        self.assertEqual(
            [
                datetime(2026, 6, 18, 8, 57, tzinfo=timezone(timedelta(hours=9))),
                datetime(2026, 6, 18, 8, 58, tzinfo=timezone(timedelta(hours=9))),
            ],
            [bar.timestamp for bar in snapshot.histories["BUY001"]],
        )
        self.assertEqual(Decimal("9850"), snapshot.histories["BUY001"][-1].close)

    def test_static_provider_ranks_full_known_universe_for_empty_request(self):
        provider = StaticScannerProvider(
            bars={
                "LOW001": _bar("LOW001"),
                "HIGH01": _bar("HIGH01"),
                "MID001": _bar("MID001"),
            },
            priorities={"LOW001": 10.0, "HIGH01": 100.0, "MID001": 50.0},
        )

        self.assertEqual(["HIGH01", "MID001", "LOW001"], provider.rank_symbols([]))

    def test_static_provider_nonempty_rank_request_keeps_requested_filter(self):
        provider = StaticScannerProvider(
            bars={
                "LOW001": _bar("LOW001"),
                "HIGH01": _bar("HIGH01"),
                "MID001": _bar("MID001"),
            },
            priorities={"LOW001": 10.0, "HIGH01": 100.0, "MID001": 50.0},
        )

        self.assertEqual(
            ["MID001", "LOW001", "UNKNOWN"],
            provider.rank_symbols(["LOW001", "UNKNOWN", "MID001"]),
        )

    def test_bar_provider_scanner_wraps_price_source_and_priority(self):
        requested_symbols: list[str] = []
        bars = {
            "BUY001": _bar("BUY001", "12000", 500),
            "BUY002": _bar("BUY002", "8000", 900),
        }

        def bar_provider(symbol: str) -> MarketBar:
            requested_symbols.append(symbol)
            return bars[symbol]

        provider = BarProviderScanner(
            bar_provider,
            priority_provider=lambda symbol: {"BUY001": 500.0, "BUY002": 900.0}[symbol],
            label="test scanner",
            kind="test",
        )

        snapshot = provider.snapshot(["BUY001", "BUY002"])

        self.assertEqual(["BUY001", "BUY002"], requested_symbols)
        self.assertEqual({"BUY001", "BUY002"}, set(snapshot.bars))
        self.assertEqual(["BUY002", "BUY001"], snapshot.ordered_symbols(["BUY001", "BUY002"]))
        self.assertEqual("test", snapshot.diagnostics.provider)

    def test_bar_provider_scanner_ranks_without_fetching_bars(self):
        requested_symbols: list[str] = []

        def bar_provider(symbol: str) -> MarketBar:
            requested_symbols.append(symbol)
            return _bar(symbol)

        provider = BarProviderScanner(
            bar_provider,
            priority_provider=lambda symbol: {"BUY001": 20.0, "BUY002": 100.0}[symbol],
        )

        self.assertEqual(["BUY002", "BUY001"], provider.rank_symbols(["BUY001", "BUY002"]))
        self.assertEqual([], requested_symbols)

    def test_bar_provider_scanner_diagnostics_do_not_store_raw_exception_text(self):
        def bar_provider(_symbol: str) -> MarketBar:
            raise RuntimeError("Authorization: Bearer secret-token-123")

        snapshot = BarProviderScanner(bar_provider).snapshot(["BUY001"])

        rendered = "\n".join(snapshot.diagnostics.messages)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("Bearer", rendered)
        self.assertNotIn("secret-token-123", rendered)

    def test_json_scanner_provider_ranks_external_candidates_without_kis(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {
                                "symbol": "BUY002",
                                "price": "8000",
                                "volume": 9000,
                                "priority": 100,
                                "reason": "volume_rank",
                                "timestamp": "2026-06-18T09:01:00+09:00",
                                "bid": "7990",
                                "ask": "8010",
                            },
                            {
                                "symbol": "BUY001",
                                "close": "12000",
                                "volume": 5000,
                                "priority": 100,
                                "reason": "tie_order",
                                "timestamp": "2026-06-18T09:01:00",
                            },
                            {
                                "symbol": "SKIP01",
                                "price": "7000",
                                "volume": 3000,
                                "priority": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            provider = JsonScannerProvider(snapshot_path)
            snapshot = provider.snapshot(["BUY001", "BUY002"])
            ranked = provider.rank_symbols(["BUY001", "BUY002"])

        self.assertEqual(["BUY002", "BUY001"], ranked)
        self.assertEqual(["BUY002", "BUY001"], snapshot.ordered_symbols(["BUY001", "BUY002"]))
        self.assertEqual({"BUY001", "BUY002"}, set(snapshot.bars))
        self.assertNotIn("SKIP01", snapshot.bars)
        self.assertEqual(Decimal("12000"), snapshot.bars["BUY001"].close)
        self.assertEqual("kiwoom-file", snapshot.diagnostics.provider)
        self.assertEqual({}, snapshot.histories)

    def test_json_scanner_provider_parses_completed_candidate_history(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-18T09:03:10+09:00",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "timestamp": "2026-06-18T09:03:10+09:00",
                                "price": "12000",
                                "volume": 5000,
                                "history": [
                                    {
                                        "symbol": "BUY001",
                                        "timestamp": "2026-06-18T09:01:00+09:00",
                                        "open": "11000",
                                        "high": "11100",
                                        "low": "10900",
                                        "close": "11000",
                                        "volume": 1100,
                                        "vwap": "11000",
                                    },
                                    {
                                        "symbol": "BUY001",
                                        "timestamp": "2026-06-18T09:00:00+09:00",
                                        "open": "10000",
                                        "high": "10100",
                                        "low": "9900",
                                        "close": "10000",
                                        "volume": 1000,
                                        "vwap": "10000",
                                    },
                                    {
                                        "symbol": "BUY001",
                                        "timestamp": "2026-06-18T09:01:00+09:00",
                                        "open": "11100",
                                        "high": "11200",
                                        "low": "11000",
                                        "close": "11100",
                                        "volume": 1200,
                                        "vwap": "11100",
                                        "bid": "11090",
                                        "ask": "11110",
                                    },
                                    {
                                        "symbol": "OTHER1",
                                        "timestamp": "2026-06-18T09:02:00+09:00",
                                        "open": "11500",
                                        "high": "11600",
                                        "low": "11400",
                                        "close": "11500",
                                        "volume": 1300,
                                        "vwap": "11500",
                                    },
                                    {
                                        "symbol": "BUY001",
                                        "timestamp": "2026-06-18T09:02:00+09:00",
                                        "open": "11500",
                                        "high": "11600",
                                        "low": "11400",
                                        "close": "11500",
                                        "volume": 1300,
                                    },
                                    {
                                        "symbol": "BUY001",
                                        "timestamp": "2026-06-18T09:03:00+09:00",
                                        "open": "11900",
                                        "high": "12100",
                                        "low": "11800",
                                        "close": "12000",
                                        "volume": 1400,
                                        "vwap": "11950",
                                    },
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        history = snapshot.histories["BUY001"]
        self.assertEqual(
            [
                datetime(2026, 6, 18, 9, 0, tzinfo=timezone(timedelta(hours=9))),
                datetime(2026, 6, 18, 9, 1, tzinfo=timezone(timedelta(hours=9))),
            ],
            [bar.timestamp for bar in history],
        )
        self.assertEqual(Decimal("11100"), history[-1].close)
        self.assertEqual(Decimal("11090"), history[-1].bid)
        self.assertEqual(Decimal("11110"), history[-1].ask)

    def test_json_scanner_provider_does_not_fabricate_history_from_candidate_aggregates(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "timestamp": "2026-06-18T09:03:00+09:00",
                                "open": "10000",
                                "high": "12000",
                                "low": "9000",
                                "close": "11000",
                                "volume": 500000,
                                "vwap": "10500",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        self.assertEqual({}, snapshot.histories)

    def test_json_scanner_provider_derives_flow_from_change_rate_and_trading_value(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "11000",
                                "change_rate": "10",
                                "trading_value": "11000000",
                                "priority": 100,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        bar = snapshot.bars["BUY001"]
        self.assertEqual(Decimal("10000"), bar.open)
        self.assertEqual(Decimal("11000"), bar.high)
        self.assertEqual(Decimal("10000"), bar.low)
        self.assertEqual(Decimal("10500"), bar.vwap)
        self.assertEqual(1000, bar.volume)

    def test_json_scanner_provider_treats_small_change_rate_as_percent(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "10030",
                                "change_rate": "0.3",
                                "volume": 1000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        self.assertEqual(Decimal("1.003"), (snapshot.bars["BUY001"].close / snapshot.bars["BUY001"].open).quantize(Decimal("0.001")))

    def test_json_scanner_provider_reports_sanitized_load_errors(self):
        provider = JsonScannerProvider("C:/secret/path/with/account/scanner.json")

        snapshot = provider.snapshot(["BUY001"])

        rendered = "\n".join(snapshot.diagnostics.messages)
        self.assertIn("FileNotFoundError", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("account", rendered)

    def test_json_scanner_provider_raises_sanitized_rank_error_when_unavailable(self):
        provider = JsonScannerProvider("C:/secret/path/with/account/scanner.json")

        with self.assertRaisesRegex(RuntimeError, "json: FileNotFoundError") as context:
            provider.rank_symbols(["BUY001"])

        self.assertNotIn("secret", str(context.exception))
        self.assertNotIn("account", str(context.exception))

    def test_json_scanner_provider_sanitizes_provider_metadata(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "Authorization: Bearer secret-token-123",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "10000",
                                "volume": 1000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        self.assertEqual("json", snapshot.diagnostics.provider)

    def test_json_scanner_provider_sanitizes_account_like_metadata(self):
        for unsafe_value in ("12345678", "12345678-01", "account 12345678", "acct 1234567"):
            with self.subTest(unsafe_value=unsafe_value), TemporaryDirectory() as directory:
                snapshot_path = Path(directory) / "scanner.json"
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "provider": unsafe_value,
                            "candidates": [
                                {
                                    "symbol": "BUY001",
                                    "price": "10000",
                                    "volume": 1000,
                                    "reason": unsafe_value,
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

                snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

            self.assertEqual("json", snapshot.diagnostics.provider)
            self.assertEqual("json", snapshot.candidates[0].reason)

    def test_json_scanner_provider_filters_stale_generated_snapshot(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "generated_at": "2026-06-19T09:00:00+09:00",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 100,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=60,
                now_provider=lambda: datetime(2026, 6, 19, 9, 2, 1, tzinfo=timezone(timedelta(hours=9))),
            )
            snapshot = provider.snapshot(["BUY001"])

            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])

        self.assertEqual({}, snapshot.bars)
        self.assertTrue(any("stale" in message for message in snapshot.diagnostics.messages))

    def test_json_scanner_provider_filters_stale_file_without_generated_at(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 100,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            old_timestamp = datetime(2026, 6, 19, 9, 0, tzinfo=timezone.utc).timestamp()
            os.utime(snapshot_path, (old_timestamp, old_timestamp))

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=60,
                now_provider=lambda: datetime(2026, 6, 19, 9, 2, 1, tzinfo=timezone.utc),
            )
            snapshot = provider.snapshot(["BUY001"])

            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])

        self.assertEqual({}, snapshot.bars)
        self.assertTrue(any("stale" in message for message in snapshot.diagnostics.messages))

    def test_json_scanner_provider_refreshes_stale_snapshot_before_ranking(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "stale-file",
                        "generated_at": "2026-06-19T09:00:00+09:00",
                        "candidates": [
                            {
                                "symbol": "OLD001",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def refresh_snapshot():
                refresh_calls.append("refresh")
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "provider": "fresh-file",
                            "generated_at": "2026-06-19T09:02:01+09:00",
                            "candidates": [
                                {
                                    "symbol": "BUY001",
                                    "price": "10000",
                                    "volume": 1000,
                                    "priority": 100,
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=60,
                now_provider=lambda: datetime(2026, 6, 19, 9, 2, 1, tzinfo=timezone(timedelta(hours=9))),
                refresh_callback=refresh_snapshot,
            )

            ranked = provider.rank_symbols([])
            snapshot = provider.snapshot(["BUY001"])

        self.assertEqual(["refresh"], refresh_calls)
        self.assertEqual(["BUY001"], ranked)
        self.assertEqual({"BUY001"}, set(snapshot.bars))
        self.assertEqual("fresh-file", snapshot.diagnostics.provider)

    def test_json_scanner_provider_refreshes_previous_minute_snapshot_for_live_confirmation(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "previous-minute",
                        "generated_at": "2026-06-19T09:00:59+09:00",
                        "candidates": [
                            {
                                "symbol": "OLD001",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 1,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def refresh_snapshot():
                refresh_calls.append("refresh")
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "provider": "current-minute",
                            "generated_at": "2026-06-19T09:01:01+09:00",
                            "candidates": [
                                {
                                    "symbol": "BUY001",
                                    "price": "10000",
                                    "volume": 1000,
                                    "priority": 100,
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=300,
                now_provider=lambda: datetime(
                    2026,
                    6,
                    19,
                    9,
                    1,
                    1,
                    tzinfo=timezone(timedelta(hours=9)),
                ),
                refresh_callback=refresh_snapshot,
                require_current_minute=True,
            )

            ranked = provider.rank_symbols([])
            snapshot = provider.snapshot(["BUY001"])

        self.assertEqual(["refresh"], refresh_calls)
        self.assertEqual(["BUY001"], ranked)
        self.assertEqual({"BUY001"}, set(snapshot.bars))

    def test_json_scanner_provider_filters_previous_minute_candidate_timestamp(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "current-minute",
                        "generated_at": "2026-06-19T09:01:10+09:00",
                        "candidates": [
                            {
                                "symbol": "OLD001",
                                "timestamp": "2026-06-19T09:00:59+09:00",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 200,
                            },
                            {
                                "symbol": "BUY001",
                                "timestamp": "2026-06-19T09:01:05+09:00",
                                "price": "10000",
                                "volume": 1000,
                                "priority": 100,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=300,
                now_provider=lambda: datetime(
                    2026,
                    6,
                    19,
                    9,
                    1,
                    20,
                    tzinfo=timezone(timedelta(hours=9)),
                ),
                require_current_minute=True,
            )

            ranked = provider.rank_symbols([])
            snapshot = provider.snapshot(["OLD001", "BUY001"])

        self.assertEqual(["BUY001"], ranked)
        self.assertEqual({"BUY001"}, set(snapshot.bars))
        self.assertTrue(
            any(
                "stale_candidate_minute_filtered count=1" in message
                for message in snapshot.diagnostics.messages
            )
        )

    def test_json_scanner_provider_reports_when_all_candidates_are_from_previous_minute(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "current-minute",
                        "generated_at": "2026-06-19T09:01:10+09:00",
                        "candidates": [
                            {
                                "symbol": "OLD001",
                                "timestamp": "2026-06-19T09:00:59+09:00",
                                "price": "10000",
                                "volume": 1000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=300,
                now_provider=lambda: datetime(
                    2026,
                    6,
                    19,
                    9,
                    1,
                    20,
                    tzinfo=timezone(timedelta(hours=9)),
                ),
                require_current_minute=True,
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "no current-minute scanner candidates filtered_count=1",
            ):
                provider.rank_symbols([])

    def test_json_scanner_provider_backs_off_after_current_minute_refresh_failure(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "previous-minute",
                        "generated_at": "2026-06-19T09:00:59+09:00",
                        "candidates": [],
                    }
                ),
                encoding="utf-8",
            )
            current_time = [
                datetime(
                    2026,
                    6,
                    19,
                    9,
                    1,
                    1,
                    tzinfo=timezone(timedelta(hours=9)),
                )
            ]
            refresh_calls = []

            def fail_refresh():
                refresh_calls.append("refresh")
                raise RuntimeError("unavailable")

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=300,
                now_provider=lambda: current_time[0],
                refresh_callback=fail_refresh,
                require_current_minute=True,
                refresh_failure_retry_seconds=60,
            )

            with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                provider.rank_symbols([])
            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])
            self.assertEqual(["refresh"], refresh_calls)

            current_time[0] += timedelta(seconds=61)
            with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                provider.rank_symbols([])

        self.assertEqual(["refresh", "refresh"], refresh_calls)

    def test_json_scanner_provider_backs_off_when_refresh_result_remains_stale(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "previous-minute",
                        "generated_at": "2026-06-19T09:00:59+09:00",
                        "candidates": [],
                    }
                ),
                encoding="utf-8",
            )
            current_time = [
                datetime(
                    2026,
                    6,
                    19,
                    9,
                    1,
                    1,
                    tzinfo=timezone(timedelta(hours=9)),
                )
            ]
            refresh_calls = []

            def stale_refresh():
                refresh_calls.append("refresh")

            provider = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=300,
                now_provider=lambda: current_time[0],
                refresh_callback=stale_refresh,
                require_current_minute=True,
                refresh_failure_retry_seconds=60,
            )

            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])
            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])
            self.assertEqual(["refresh"], refresh_calls)

            current_time[0] += timedelta(seconds=61)
            with self.assertRaisesRegex(RuntimeError, "stale scanner snapshot"):
                provider.rank_symbols([])

        self.assertEqual(["refresh", "refresh"], refresh_calls)

    def test_json_scanner_provider_uses_generated_at_for_missing_candidate_timestamp(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "generated_at": "2026-06-19T09:01:00+09:00",
                        "candidates": [
                            {
                                "symbol": "BUY001",
                                "price": "10000",
                                "volume": 1000,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            snapshot = JsonScannerProvider(snapshot_path).snapshot(["BUY001"])

        self.assertEqual(
            datetime(2026, 6, 19, 9, 1, tzinfo=timezone(timedelta(hours=9))).timestamp(),
            snapshot.bars["BUY001"].timestamp.timestamp(),
        )


if __name__ == "__main__":
    unittest.main()
