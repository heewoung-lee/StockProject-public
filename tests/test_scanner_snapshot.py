import io
import json
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stockbot.scanner import JsonScannerProvider
from stockbot.scanner_collector import collect_http_scanner_snapshot, collect_naver_market_scanner_snapshot
from stockbot.scanner_snapshot import SnapshotWriteOptions, build_scanner_snapshot_payload, write_scanner_snapshot
from stockbot.scanner_snapshot_cli import main as scanner_snapshot_main


class _FakeHttpResponse:
    def __init__(self, payload: object):
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._payload


class _FakeRawHttpResponse:
    def __init__(self, raw_payload: bytes):
        self._payload = raw_payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self._payload


class ScannerSnapshotWriterTest(unittest.TestCase):
    def test_writes_provider_readable_snapshot_from_external_aliases(self):
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "kiwoom-export.json"
            output_path = Path(directory) / "scanner_snapshot.json"
            input_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-openapi",
                        "items": [
                            {
                                "code": "A000660",
                                "name": "SK하이닉스",
                                "current_price": "85,000",
                                "trade_volume": "20,000",
                                "rank_score": "300",
                                "reason": "거래량 상위",
                                "timestamp": "2026-06-19T09:01:00+09:00",
                            },
                            {
                                "stock_code": "005930.KS",
                                "company_name": "삼성전자",
                                "close": "72,000",
                                "volume": "10,000",
                                "priority": "200",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            write_scanner_snapshot(
                input_path,
                output_path,
                SnapshotWriteOptions(provider="kiwoom-openapi", max_price=Decimal("100000")),
            )

            provider = JsonScannerProvider(output_path)
            snapshot = provider.snapshot(["000660", "005930"])
            ranked = provider.rank_symbols([])

        self.assertEqual(["000660", "005930"], ranked)
        self.assertEqual({"000660", "005930"}, set(snapshot.bars))
        self.assertEqual(Decimal("85000"), snapshot.bars["000660"].close)
        self.assertEqual(20000, snapshot.bars["000660"].volume)
        self.assertEqual("kiwoom-openapi", snapshot.diagnostics.provider)

    def test_preserves_external_order_when_priorities_tie(self):
        snapshot = build_scanner_snapshot_payload(
            [
                {"code": "FIRST1", "price": "1000", "volume": "100", "priority": 10},
                {"code": "SECOND", "price": "1000", "volume": "100", "priority": 10},
            ],
            SnapshotWriteOptions(),
        )

        self.assertEqual(["FIRST1", "SECOND"], [candidate["symbol"] for candidate in snapshot["candidates"]])

    def test_filters_unaffordable_or_incomplete_records_before_writing_snapshot(self):
        payload = {
            "provider": "external-source",
            "candidates": [
                {"symbol": "012330", "price": "608000", "volume": "100000", "priority": 999},
                {"symbol": "035720", "price": "40800", "volume": "90000", "priority": 100},
                {"symbol": "EMPTY1", "volume": "90000", "priority": 200},
                {"symbol": "LOWVOL", "price": "5000", "volume": "3", "priority": 300},
            ],
        }

        snapshot = build_scanner_snapshot_payload(
            payload,
            SnapshotWriteOptions(max_price=Decimal("100000"), min_volume=1000),
        )

        self.assertEqual(["035720"], [candidate["symbol"] for candidate in snapshot["candidates"]])
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("012330", rendered)
        self.assertNotIn("EMPTY1", rendered)
        self.assertNotIn("LOWVOL", rendered)

    def test_sanitizes_metadata_before_snapshot_hits_runtime(self):
        snapshot = build_scanner_snapshot_payload(
            {
                "provider": "Authorization: Bearer secret-token-123",
                "items": [
                    {
                        "code": "005930",
                        "price": "72000",
                        "volume": "1000",
                        "reason": "C:/Users/example-user/.env",
                    }
                ],
            },
            SnapshotWriteOptions(provider="Authorization: Bearer secret-token-123"),
        )

        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual("external-json", snapshot["provider"])
        self.assertEqual("external_rank", snapshot["candidates"][0]["reason"])
        self.assertNotIn("Bearer", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("C:/Users", rendered)

    def test_sanitizes_account_metadata_even_without_long_account_number(self):
        snapshot = build_scanner_snapshot_payload(
            {
                "provider": "account 1234567",
                "items": [
                    {
                        "code": "005930",
                        "price": "72000",
                        "volume": "1000",
                        "reason": "계좌 1234567",
                    }
                ],
            },
            SnapshotWriteOptions(provider="account 1234567"),
        )

        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertEqual("external-json", snapshot["provider"])
        self.assertEqual("external_rank", snapshot["candidates"][0]["reason"])
        self.assertNotIn("account", rendered.lower())
        self.assertNotIn("계좌", rendered)

    def test_preserves_common_stock_name_punctuation(self):
        snapshot = build_scanner_snapshot_payload(
            {
                "provider": "external-source",
                "items": [
                    {
                        "code": "028050",
                        "name": "삼성E&A",
                        "price": "50450",
                        "volume": "1000",
                    },
                    {
                        "code": "000001",
                        "name": "샘플우(전환)",
                        "price": "1000",
                        "volume": "1000",
                    },
                ],
            },
            SnapshotWriteOptions(),
        )

        self.assertEqual("삼성E&A", snapshot["candidates"][0]["name"])
        self.assertEqual("샘플우(전환)", snapshot["candidates"][1]["name"])

    def test_cli_writes_scanner_snapshot_json(self):
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "collector.json"
            output_path = Path(directory) / "scanner_snapshot.json"
            input_path.write_text(
                json.dumps(
                    [
                        {"symbol": "BUY001", "price": "9000", "volume": 5000, "priority": 10},
                        {"symbol": "HIGH01", "price": "250000", "volume": 9000, "priority": 99},
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                exit_code = scanner_snapshot_main(
                    [
                        "--input",
                        str(input_path),
                        "--output",
                        str(output_path),
                        "--provider",
                        "external-test",
                        "--max-price",
                        "100000",
                    ]
                )

            provider = JsonScannerProvider(output_path)
            ranked = provider.rank_symbols([])

        self.assertEqual(0, exit_code)
        self.assertIn("scanner_snapshot.json", stdout.getvalue())
        self.assertEqual(["BUY001"], ranked)

    def test_cli_can_fetch_scanner_snapshot_from_url(self):
        stdout = io.StringIO()

        with patch("stockbot.scanner_snapshot_cli.collect_http_scanner_snapshot", return_value=7) as collector:
            with redirect_stdout(stdout):
                exit_code = scanner_snapshot_main(
                    [
                        "--url",
                        "https://scanner.example.test/candidates",
                        "--output",
                        "data/scanner_snapshot.json",
                        "--provider",
                        "external-http",
                        "--header",
                        "X-Scanner-Key: secret-value",
                        "--query",
                        "market=KOSPI",
                        "--max-price",
                        "100000",
                        "--min-volume",
                        "1000",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertIn("wrote 7 candidates", stdout.getvalue())
        collector.assert_called_once()
        args, kwargs = collector.call_args
        self.assertEqual("https://scanner.example.test/candidates", args[0])
        self.assertEqual("data/scanner_snapshot.json", args[1])
        self.assertEqual(Decimal("100000"), args[2].max_price)
        self.assertEqual(1000, args[2].min_volume)
        self.assertEqual({"X-Scanner-Key": "secret-value"}, kwargs["headers"])
        self.assertEqual({"market": "KOSPI"}, kwargs["query"])

    def test_cli_can_collect_naver_market_snapshot(self):
        stdout = io.StringIO()

        with patch("stockbot.scanner_snapshot_cli.collect_naver_market_scanner_snapshot", return_value=11) as collector:
            with redirect_stdout(stdout):
                exit_code = scanner_snapshot_main(
                    [
                        "--naver-market",
                        "--market",
                        "kospi",
                        "--pages",
                        "2",
                        "--output",
                        "data/scanner_snapshot.json",
                        "--provider",
                        "naver-mobile",
                        "--max-price",
                        "100000",
                        "--min-volume",
                        "1000",
                    ]
                )

        self.assertEqual(0, exit_code)
        self.assertIn("wrote 11 candidates", stdout.getvalue())
        collector.assert_called_once()
        args, kwargs = collector.call_args
        self.assertEqual("data/scanner_snapshot.json", args[0])
        self.assertEqual(Decimal("100000"), args[1].max_price)
        self.assertEqual(1000, args[1].min_volume)
        self.assertEqual(("kospi",), kwargs["markets"])
        self.assertEqual(2, kwargs["pages"])

    def test_cli_accepts_dashboard_diagnostics_positions_as_snapshot_input(self):
        with TemporaryDirectory() as directory:
            input_path = Path(directory) / "stockbot-diagnostics.json"
            output_path = Path(directory) / "scanner_snapshot.json"
            input_path.write_text(
                json.dumps(
                    {
                        "provider": "stockbot-diagnostics",
                        "positions": [
                            {
                                "symbol": "002170",
                                "companyName": "SYTS",
                                "quantity": 18,
                                "lastPrice": "5,382원",
                            },
                            {
                                "symbol": "012330",
                                "companyName": "현대모비스",
                                "quantity": 1,
                                "lastPrice": "608,000원",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            exit_code = scanner_snapshot_main(
                [
                    "--input",
                    str(input_path),
                    "--output",
                    str(output_path),
                    "--provider",
                    "diagnostic-bootstrap",
                    "--max-price",
                    "100000",
                ]
            )
            provider = JsonScannerProvider(output_path)
            snapshot = provider.snapshot(["002170"])
            ranked = provider.rank_symbols([])
            rendered = output_path.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertEqual(["002170"], ranked)
        self.assertEqual(Decimal("5382"), snapshot.bars["002170"].close)
        self.assertNotIn("012330", rendered)

    def test_collects_http_scanner_snapshot_with_price_and_volume_filters(self):
        captured_request = {}

        def fake_opener(request, *, timeout):
            captured_request["url"] = request.full_url
            captured_request["timeout"] = timeout
            captured_request["headers"] = dict(request.header_items())
            return _FakeHttpResponse(
                {
                    "items": [
                        {
                            "code": "A035720",
                            "companyName": "Kakao",
                            "currentPrice": "40,800",
                            "trade_volume": "90,000",
                            "rank_score": "100",
                        },
                        {
                            "code": "012330",
                            "companyName": "Hyundai Mobis",
                            "currentPrice": "608,000",
                            "trade_volume": "110,000",
                            "rank_score": "200",
                        },
                        {
                            "code": "LOWVOL",
                            "currentPrice": "4,000",
                            "trade_volume": "3",
                            "rank_score": "300",
                        },
                    ]
                }
            )

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "scanner_snapshot.json"
            written_count = collect_http_scanner_snapshot(
                "https://scanner.example.test/candidates",
                output_path,
                SnapshotWriteOptions(
                    provider="external-http",
                    max_price=Decimal("100000"),
                    min_volume=1000,
                ),
                headers={"X-Scanner-Key": "secret-value"},
                query={"market": "KOSPI"},
                timeout=3.5,
                opener=fake_opener,
            )
            provider = JsonScannerProvider(output_path)
            ranked = provider.rank_symbols([])
            rendered = output_path.read_text(encoding="utf-8")

        self.assertEqual(1, written_count)
        self.assertEqual(["035720"], ranked)
        self.assertIn("market=KOSPI", captured_request["url"])
        self.assertEqual(3.5, captured_request["timeout"])
        self.assertEqual("secret-value", captured_request["headers"]["X-scanner-key"])
        self.assertNotIn("012330", rendered)
        self.assertNotIn("LOWVOL", rendered)
        self.assertNotIn("secret-value", rendered)

    def test_collects_naver_market_snapshot_filters_stocks_and_sorts_by_trading_value(self):
        captured_urls = []

        def fake_opener(request, *, timeout):
            captured_urls.append(request.full_url)
            if "page=1" in request.full_url:
                payload = {
                    "stocks": [
                        {
                            "stockType": "domestic",
                            "stockEndType": "stock",
                            "itemCode": "035720",
                            "stockName": "카카오",
                            "closePrice": "40,800",
                            "accumulatedTradingVolume": "90,000",
                            "accumulatedTradingValue": "3,600",
                            "fluctuationsRatio": "1.5",
                            "localTradedAt": "2026-06-22T09:45:16+09:00",
                        },
                        {
                            "stockType": "domestic",
                            "stockEndType": "stock",
                            "itemCode": "012330",
                            "stockName": "현대모비스",
                            "closePrice": "608,000",
                            "accumulatedTradingVolume": "100,000",
                            "accumulatedTradingValue": "60,000",
                            "localTradedAt": "2026-06-22T09:45:16+09:00",
                        },
                        {
                            "stockType": "domestic",
                            "stockEndType": "etf",
                            "itemCode": "ETF001",
                            "stockName": "샘플ETF",
                            "closePrice": "10,000",
                            "accumulatedTradingVolume": "500,000",
                            "accumulatedTradingValue": "5,000",
                            "localTradedAt": "2026-06-22T09:45:16+09:00",
                        },
                    ],
                    "totalCount": 200,
                    "page": 1,
                    "pageSize": 100,
                }
            else:
                payload = {
                    "stocks": [
                        {
                            "stockType": "domestic",
                            "stockEndType": "stock",
                            "itemCode": "000020",
                            "stockName": "동화약품",
                            "closePrice": "8,000",
                            "accumulatedTradingVolume": "700,000",
                            "accumulatedTradingValue": "9,000",
                            "localTradedAt": "2026-06-22T09:46:16+09:00",
                        }
                    ],
                    "totalCount": 200,
                    "page": 2,
                    "pageSize": 100,
                }
            return _FakeRawHttpResponse(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "scanner_snapshot.json"
            written_count = collect_naver_market_scanner_snapshot(
                output_path,
                SnapshotWriteOptions(
                    provider="naver-mobile",
                    max_price=Decimal("100000"),
                    min_volume=1000,
                ),
                markets=("kospi",),
                pages=2,
                opener=fake_opener,
            )
            provider = JsonScannerProvider(output_path)
            ranked = provider.rank_symbols([])
            provider_snapshot = provider.snapshot(ranked)
            snapshot = json.loads(output_path.read_text(encoding="utf-8"))

        self.assertEqual(2, written_count)
        self.assertEqual(["000020", "035720"], ranked)
        self.assertEqual(2, len(captured_urls))
        self.assertEqual("naver-mobile", snapshot["provider"])
        self.assertEqual(
            {"KOSPI"},
            {candidate["market"] for candidate in snapshot["candidates"]},
        )
        self.assertEqual(
            {"KOSPI"},
            {bar.market for bar in provider_snapshot.bars.values()},
        )
        self.assertEqual(9000000000.0, snapshot["candidates"][0]["priority"])
        self.assertEqual("9000000000", snapshot["candidates"][0]["trading_value"])
        self.assertEqual("2026-06-22T09:46:16+09:00", snapshot["candidates"][0]["timestamp"])
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("012330", rendered)
        self.assertNotIn("ETF001", rendered)

    def test_collects_naver_market_snapshot_with_bounded_real_minute_history(self):
        captured_urls = []

        def market_payload():
            return {
                "stocks": [
                    {
                        "stockType": "domestic",
                        "stockEndType": "stock",
                        "itemCode": "111111",
                        "stockName": "First",
                        "closePrice": "10,500",
                        "accumulatedTradingVolume": "90,000",
                        "accumulatedTradingValue": "9,000",
                        "localTradedAt": "2026-08-04T09:45:16+09:00",
                    },
                    {
                        "stockType": "domestic",
                        "stockEndType": "stock",
                        "itemCode": "222222",
                        "stockName": "Second",
                        "closePrice": "8,000",
                        "accumulatedTradingVolume": "80,000",
                        "accumulatedTradingValue": "8,000",
                        "localTradedAt": "2026-08-04T09:45:10+09:00",
                    },
                ],
                "totalCount": 2,
                "page": 1,
                "pageSize": 100,
            }

        def minute_rows():
            return [
                {
                    "localDateTime": f"2026080409{minute:02d}00",
                    "openPrice": 10000 + minute,
                    "highPrice": 10020 + minute,
                    "lowPrice": 9990 + minute,
                    "currentPrice": 10010 + minute,
                    "accumulatedTradingVolume": 1000 + minute,
                }
                for minute in range(40, 46)
            ]

        def fake_opener(request, *, timeout):
            captured_urls.append((request.full_url, timeout))
            if "/chart/domestic/item/111111/minute" in request.full_url:
                return _FakeRawHttpResponse(json.dumps(minute_rows()).encode("utf-8"))
            if "/chart/domestic/item/" in request.full_url:
                raise AssertionError("history candidate limit was not enforced")
            return _FakeRawHttpResponse(json.dumps(market_payload()).encode("utf-8"))

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "scanner_snapshot.json"
            written_count = collect_naver_market_scanner_snapshot(
                output_path,
                SnapshotWriteOptions(provider="naver-mobile"),
                markets=("kospi",),
                pages=1,
                opener=fake_opener,
                minute_history_candidates=1,
                minute_history_workers=1,
                minute_history_timeout=2.5,
            )
            provider = JsonScannerProvider(output_path)
            snapshot = provider.snapshot(["111111", "222222"])

        self.assertEqual(2, written_count)
        self.assertEqual(["111111"], list(snapshot.histories))
        self.assertEqual(5, len(snapshot.histories["111111"]))
        self.assertEqual(
            "2026-08-04T09:44:00+09:00",
            snapshot.histories["111111"][-1].timestamp.isoformat(),
        )
        self.assertEqual(Decimal("10044"), snapshot.histories["111111"][-1].open)
        self.assertEqual(Decimal("10064"), snapshot.histories["111111"][-1].high)
        self.assertEqual(Decimal("10034"), snapshot.histories["111111"][-1].low)
        self.assertEqual(Decimal("10054"), snapshot.histories["111111"][-1].close)
        self.assertEqual(1044, snapshot.histories["111111"][-1].volume)
        self.assertEqual(1, sum("/chart/domestic/item/" in url for url, _ in captured_urls))
        self.assertEqual(
            [2.5],
            [timeout for url, timeout in captured_urls if "/chart/domestic/item/" in url],
        )

    def test_naver_minute_history_failure_isolated_from_wide_snapshot(self):
        market_payload = {
            "stocks": [
                {
                    "stockType": "domestic",
                    "stockEndType": "stock",
                    "itemCode": symbol,
                    "stockName": symbol,
                    "closePrice": "10,000",
                    "accumulatedTradingVolume": "100,000",
                    "accumulatedTradingValue": priority,
                    "localTradedAt": "2026-08-04T09:45:16+09:00",
                }
                for symbol, priority in (("111111", "9,000"), ("222222", "8,000"))
            ],
            "totalCount": 2,
            "page": 1,
            "pageSize": 100,
        }
        valid_history = [
            {
                "localDateTime": f"2026080409{minute:02d}00",
                "openPrice": 10000,
                "highPrice": 10020,
                "lowPrice": 9990,
                "currentPrice": 10010,
                "accumulatedTradingVolume": 1000,
            }
            for minute in range(40, 46)
        ]

        def fake_opener(request, *, timeout):
            if "/chart/domestic/item/111111/minute" in request.full_url:
                raise OSError("external minute endpoint unavailable")
            if "/chart/domestic/item/222222/minute" in request.full_url:
                return _FakeRawHttpResponse(json.dumps(valid_history).encode("utf-8"))
            return _FakeRawHttpResponse(json.dumps(market_payload).encode("utf-8"))

        with TemporaryDirectory() as directory:
            output_path = Path(directory) / "scanner_snapshot.json"
            written_count = collect_naver_market_scanner_snapshot(
                output_path,
                SnapshotWriteOptions(provider="naver-mobile"),
                markets=("kospi",),
                pages=1,
                opener=fake_opener,
                minute_history_candidates=2,
                minute_history_workers=1,
            )
            snapshot = JsonScannerProvider(output_path).snapshot(["111111", "222222"])

        self.assertEqual(2, written_count)
        self.assertEqual({"111111", "222222"}, set(snapshot.bars))
        self.assertEqual(["222222"], list(snapshot.histories))


if __name__ == "__main__":
    unittest.main()
