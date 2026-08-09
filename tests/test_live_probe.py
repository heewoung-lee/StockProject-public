import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import BotConfig
from stockbot.kis import KisApiError, KisCredentials
from stockbot.kis_market_data import KisTokenFileCache
from stockbot.live_probe import run_live_order_dry_run, run_live_read_only_probe
from stockbot.live_safety import LIVE_CONFIRMATION_PHRASE
from stockbot.models import AccountSnapshot, Order, Position
from stockbot.rate_limit import RateLimitDecision


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = [
            {"access_token": "live-token-123"},
            {"rt_cd": "0", "output": {"stck_prpr": "70000", "temp_stop_yn": "N"}},
            {"rt_cd": "0", "output1": {"askp1": "70000", "bidp1": "70000"}, "output2": {}},
            {"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "1000000", "ord_psbl_cash": "1000000"}]},
            {"rt_cd": "0", "output1": [], "output2": {"tot_rlzt_pfls": "0"}},
        ]

    def __call__(self, request):
        self.calls.append(request)
        return self.responses.pop(0)


class PremarketQuoteTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[2] = {
            "rt_cd": "0",
            "output1": {"askp1": "0", "bidp1": "0"},
            "output2": {},
        }


class MissingPremarketQuoteTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[2] = {
            "rt_cd": "0",
            "output1": {"askp1": "", "bidp1": "69900"},
            "output2": {},
        }


class MalformedQuoteTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[2] = {
            "rt_cd": "0",
            "output1": {"askp1": "not-a-number", "bidp1": "69900"},
            "output2": {},
        }


class MissingAskMalformedBidTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[2] = {
            "rt_cd": "0",
            "output1": {"askp1": "", "bidp1": "not-a-number"},
            "output2": {},
        }


class NegativeQuoteTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[2] = {
            "rt_cd": "0",
            "output1": {"askp1": "-1", "bidp1": "-2"},
            "output2": {},
        }


class MissingOrderableCashTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses = [
            {"access_token": "live-token-123"},
            {"rt_cd": "0", "output": {"stck_prpr": "70000", "temp_stop_yn": "N"}},
            {"rt_cd": "0", "output1": {"askp1": "70000", "bidp1": "70000"}, "output2": {}},
            {"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "1000000", "tot_evlu_amt": "1000000"}]},
            {"rt_cd": "0", "output1": [], "output2": {"tot_rlzt_pfls": "0"}},
        ]


class UnknownDailyPnlTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[-1] = {
            "rt_cd": "1",
            "msg_cd": "PROFIT_UNAVAILABLE",
            "msg1": "period profit unavailable",
        }


class DailyLossLimitTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses[-1] = {
            "rt_cd": "0",
            "output1": [],
            "output2": {"tot_rlzt_pfls": "-100001"},
        }


class TokenRejectingCachedTransport:
    def __init__(self):
        self.calls = []
        self.responses = [
            {"rt_cd": "0", "output": {"stck_prpr": "70000"}},
            {"rt_cd": "0", "output1": {"askp1": "70000", "bidp1": "70000"}, "output2": {}},
            {"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "1000000", "ord_psbl_cash": "1000000"}]},
            {"rt_cd": "0", "output1": [], "output2": {"tot_rlzt_pfls": "0"}},
        ]

    def __call__(self, request):
        self.calls.append(request)
        if request.path == "/oauth2/tokenP":
            raise AssertionError("probe should reuse the shared live token cache")
        return self.responses.pop(0)


class HoldingsTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses = [
            {"access_token": "live-token-123"},
            {"rt_cd": "0", "output": {"stck_prpr": "70000"}},
            {"rt_cd": "0", "output1": {"askp1": "70000", "bidp1": "70000"}, "output2": {}},
            {
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "hldg_qty": "3",
                        "pchs_avg_pric": "70000",
                        "prpr": "71000",
                        "ord_psbl_qty": "2",
                    },
                    {
                        "pdno": "000660",
                        "hldg_qty": "1",
                        "pchs_avg_pric": "120000",
                        "prpr": "119500",
                        "ord_psbl_qty": "1",
                    },
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "1000000",
                        "ord_psbl_cash": "850000",
                        "tot_evlu_amt": "1332500",
                    }
                ],
            },
            {"rt_cd": "0", "output1": [], "output2": {"tot_rlzt_pfls": "0"}},
        ]


def live_env() -> dict[str, str]:
    return {
        "KIS_LIVE_APP_KEY": "live-app-key",
        "KIS_LIVE_APP_SECRET": "live-secret-value",
        "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
        "STOCKBOT_ALLOW_LIVE_TRADING": "true",
        "STOCKBOT_LIVE_TRADING_ENABLED": "true",
        "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "21",
    }


def live_config() -> BotConfig:
    return BotConfig(
        trading_mode="live",
        allow_live_trading=True,
        live_trading_enabled=True,
        max_order_amount=Decimal("100000"),
    )


class LiveProbeTest(unittest.TestCase):
    def test_live_read_only_probe_uses_live_credentials_and_never_orders(self):
        transport = FakeTransport()

        result = run_live_read_only_probe(
            env={
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-secret-value",
                "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
        )

        self.assertEqual("kis-live-read-only", result["mode"])
        self.assertTrue(result["read_only"])
        self.assertFalse(result["live_order_enabled"])
        self.assertEqual("******21-01", result["account"])
        self.assertEqual("70000", result["last_price"])
        self.assertEqual("1000000", result["cash"])
        self.assertEqual("1000000", result["buying_power"])
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            [call.path for call in transport.calls],
        )
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("live-secret-value", rendered)
        self.assertNotIn("live-token-123", rendered)
        self.assertNotIn("test-live-account21", rendered)

    def test_live_read_only_probe_allows_unavailable_premarket_orderbook(self):
        transport = PremarketQuoteTransport()

        result = run_live_read_only_probe(
            env={
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-secret-value",
                "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
        )

        self.assertTrue(result["read_only"])
        self.assertEqual("70000", result["last_price"])
        self.assertEqual("1000000", result["cash"])
        self.assertEqual(0, result["balance_positions"])
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            [call.path for call in transport.calls],
        )

    def test_live_read_only_probe_allows_missing_premarket_quote(self):
        result = run_live_read_only_probe(
            env={
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-secret-value",
                "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=MissingPremarketQuoteTransport(),
        )

        self.assertTrue(result["read_only"])
        self.assertEqual("70000", result["last_price"])
        self.assertEqual("1000000", result["cash"])

    def test_live_read_only_probe_does_not_ignore_malformed_orderbook(self):
        with self.assertRaisesRegex(ValueError, "invalid KIS best quote field: askp1"):
            run_live_read_only_probe(
                env={
                    "KIS_LIVE_APP_KEY": "live-app-key",
                    "KIS_LIVE_APP_SECRET": "live-secret-value",
                    "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                },
                symbol="005930",
                transport=MalformedQuoteTransport(),
            )

    def test_live_read_only_probe_validates_bid_when_ask_is_unavailable(self):
        with self.assertRaisesRegex(ValueError, "invalid KIS best quote field: bidp1"):
            run_live_read_only_probe(
                env={
                    "KIS_LIVE_APP_KEY": "live-app-key",
                    "KIS_LIVE_APP_SECRET": "live-secret-value",
                    "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                },
                symbol="005930",
                transport=MissingAskMalformedBidTransport(),
            )

    def test_live_read_only_probe_does_not_treat_negative_quotes_as_unavailable(self):
        with self.assertRaisesRegex(ValueError, "invalid KIS best quote field: askp1"):
            run_live_read_only_probe(
                env={
                    "KIS_LIVE_APP_KEY": "live-app-key",
                    "KIS_LIVE_APP_SECRET": "live-secret-value",
                    "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                },
                symbol="005930",
                transport=NegativeQuoteTransport(),
            )

    def test_live_read_only_probe_exports_current_holdings_without_secrets(self):
        transport = HoldingsTransport()

        result = run_live_read_only_probe(
            env={
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-secret-value",
                "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
        )

        self.assertEqual(2, result["balance_positions"])
        self.assertEqual("1332500", result["equity"])
        self.assertEqual("850000", result["buying_power"])
        self.assertEqual(
            [
                {
                    "symbol": "005930",
                    "side": "LONG",
                    "quantity": 3,
                    "avg_price": "70000",
                    "last_price": "71000",
                    "market_value": "213000",
                    "unrealized_pnl": "3000",
                    "sellable_quantity": 2,
                },
                {
                    "symbol": "000660",
                    "side": "LONG",
                    "quantity": 1,
                    "avg_price": "120000",
                    "last_price": "119500",
                    "market_value": "119500",
                    "unrealized_pnl": "-500",
                    "sellable_quantity": 1,
                },
            ],
            result["positions"],
        )
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("live-secret-value", rendered)
        self.assertNotIn("live-token-123", rendered)
        self.assertNotIn("test-live-account21", rendered)

    def test_live_read_only_probe_writes_live_token_cache_without_exporting_token(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            token_cache = KisTokenFileCache(Path(tmpdir) / "kis-token-cache.json", namespace="kis-live")
            transport = FakeTransport()

            result = run_live_read_only_probe(
                env={
                    "KIS_LIVE_APP_KEY": "live-app-key",
                    "KIS_LIVE_APP_SECRET": "live-secret-value",
                    "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                },
                symbol="005930",
                transport=transport,
                token_cache=token_cache,
            )

            cached = token_cache.read(
                KisCredentials(
                    app_key="live-app-key",
                    app_secret="live-secret-value",
                    account_no="test-live-account21",
                    account_product_code="01",
                )
            )
            self.assertIsNotNone(cached)
            self.assertEqual("live-token-123", cached.access_token)
            rendered = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("live-token-123", rendered)

    def test_live_read_only_probe_issues_cache_miss_token_through_shared_limiter(self):
        class RecordingLimiter:
            def __init__(self):
                self.kinds = []
                self.token_issues = 0

            def run_request(self, kind, operation, **kwargs):
                self.kinds.append(kind)
                return RateLimitDecision(True, 0.0, "allowed"), operation()

            def record_token_issue(self):
                self.token_issues += 1

            def record_rate_limit_error(self, retry_after_seconds=None):
                return None

        with tempfile.TemporaryDirectory() as tmpdir:
            limiter = RecordingLimiter()
            run_live_read_only_probe(
                env=live_env(),
                symbol="005930",
                transport=FakeTransport(),
                token_cache=KisTokenFileCache(
                    Path(tmpdir) / "kis-token-cache.json",
                    namespace="kis-live",
                ),
                rate_limiter=limiter,
            )

        self.assertEqual("kis_token", limiter.kinds[0])
        self.assertEqual(1, limiter.token_issues)
        self.assertTrue(all(kind == "kis_live_read" for kind in limiter.kinds[1:]))

    def test_live_read_only_probe_reuses_shared_token_cache_when_cache_file_is_unwritable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            blocked_parent = Path(tmpdir) / "cache-parent"
            blocked_parent.write_text("not a directory", encoding="utf-8")
            token_cache = KisTokenFileCache(
                blocked_parent / "kis-token-cache.json",
                namespace="kis-live",
                clock=lambda: datetime(2026, 7, 8, 9, 0, 0),
            )
            first_transport = FakeTransport()

            run_live_read_only_probe(
                env=live_env(),
                symbol="005930",
                transport=first_transport,
                token_cache=token_cache,
            )
            second_transport = TokenRejectingCachedTransport()

            result = run_live_read_only_probe(
                env=live_env(),
                symbol="005930",
                transport=second_transport,
                token_cache=token_cache,
            )

        self.assertEqual("1000000", result["buying_power"])
        self.assertEqual(
            [
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            [call.path for call in second_transport.calls],
        )

    def test_live_read_only_probe_replaces_expired_shared_token_for_next_probe(self):
        class ExpiredTokenTransport:
            def __init__(self):
                self.calls = []
                self.price_calls = 0

            def __call__(self, request):
                self.calls.append(request)
                if request.path == "/oauth2/tokenP":
                    return {
                        "access_token": "fresh-live-token",
                        "access_token_token_expired": "2026-07-24 23:00:00",
                    }
                if request.path == "/uapi/domestic-stock/v1/quotations/inquire-price":
                    self.price_calls += 1
                    if self.price_calls == 1:
                        raise KisApiError("KIS HTTP 500: EGW00123 expired")
                    return {"rt_cd": "0", "output": {"stck_prpr": "70000"}}
                if request.path == "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn":
                    return {
                        "rt_cd": "0",
                        "output1": {"askp1": "70000", "bidp1": "70000"},
                        "output2": {},
                    }
                if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance":
                    return {
                        "rt_cd": "0",
                        "output1": [],
                        "output2": [{"dnca_tot_amt": "1000000", "ord_psbl_cash": "1000000"}],
                    }
                if request.path == "/uapi/domestic-stock/v1/trading/inquire-period-profit":
                    return {"rt_cd": "0", "output1": [], "output2": {"tot_rlzt_pfls": "0"}}
                raise AssertionError(f"unexpected path: {request.path}")

        with tempfile.TemporaryDirectory() as tmpdir:
            token_cache = KisTokenFileCache(
                Path(tmpdir) / "kis-token-cache.json",
                namespace="kis-live",
                clock=lambda: datetime(2026, 7, 23, 14, 0, 0),
            )
            live_credentials = KisCredentials(
                app_key="live-app-key",
                app_secret="live-secret-value",
                account_no="test-live-account21",
                account_product_code="01",
            )
            token_cache.write(
                live_credentials,
                "stale-live-token",
                datetime(2026, 7, 24, 9, 0, 0),
            )
            first_transport = ExpiredTokenTransport()

            run_live_read_only_probe(
                env=live_env(),
                symbol="005930",
                transport=first_transport,
                token_cache=token_cache,
            )

            cached = token_cache.read(live_credentials)
            self.assertIsNotNone(cached)
            self.assertEqual("fresh-live-token", cached.access_token)

            second_transport = TokenRejectingCachedTransport()
            run_live_read_only_probe(
                env=live_env(),
                symbol="005930",
                transport=second_transport,
                token_cache=token_cache,
            )

        self.assertTrue(
            all(
                call.headers.get("authorization") == "Bearer fresh-live-token"
                for call in second_transport.calls
            )
        )

    def test_live_order_dry_run_uses_read_only_requests_and_never_orders(self):
        transport = FakeTransport()

        result = run_live_order_dry_run(
            order=Order.buy("005930", 1, "entry"),
            config=live_config(),
            env=live_env(),
            transport=transport,
            market_is_open=True,
            session_approved=True,
            account_confirmation="21",
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        self.assertEqual("kis-live-order-dry-run", result["mode"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["live_order_enabled"])
        self.assertFalse(result["order_submitted"])
        self.assertTrue(result["approved"])
        self.assertEqual([], result["blockers"])
        self.assertEqual("005930", result["symbol"])
        self.assertEqual("BUY", result["side"])
        self.assertEqual(1, result["quantity"])
        self.assertEqual("70000", result["estimated_price"])
        self.assertEqual("70000", result["notional"])
        paths = [call.path for call in transport.calls]
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            paths,
        )
        self.assertNotIn("/uapi/hashkey", paths)
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", paths)
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("live-secret-value", rendered)
        self.assertNotIn("live-token-123", rendered)
        self.assertNotIn("test-live-account21", rendered)

    def test_live_order_dry_run_blocks_buy_when_daily_pnl_query_fails(self):
        transport = UnknownDailyPnlTransport()

        result = run_live_order_dry_run(
            order=Order.buy("005930", 1, "entry"),
            config=live_config(),
            env=live_env(),
            transport=transport,
            market_is_open=True,
            session_approved=True,
            account_confirmation="21",
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        self.assertFalse(result["approved"])
        self.assertIn("live_daily_realized_pnl_unknown", result["blockers"])
        self.assertIn(
            "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            [call.path for call in transport.calls],
        )

    def test_live_order_dry_run_blocks_buy_after_account_daily_loss_limit(self):
        transport = DailyLossLimitTransport()

        result = run_live_order_dry_run(
            order=Order.buy("005930", 1, "entry"),
            config=live_config(),
            env=live_env(),
            transport=transport,
            market_is_open=True,
            session_approved=True,
            account_confirmation="21",
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        self.assertFalse(result["approved"])
        self.assertIn("live_daily_loss_limit_reached", result["blockers"])

    def test_live_order_dry_run_allows_sell_when_daily_pnl_is_unknown(self):
        transport = UnknownDailyPnlTransport()
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=datetime(2026, 7, 10, 9, 0, 0),
                    highest_price=Decimal("70000"),
                    sellable_quantity=1,
                    managed_quantity=1,
                )
            },
            realized_pnl_today_known=False,
        )

        with patch(
            "stockbot.live_probe.parse_kis_account_snapshot",
            return_value=account,
        ):
            result = run_live_order_dry_run(
                order=Order.sell("005930", 1, "exit"),
                config=live_config(),
                env=live_env(),
                transport=transport,
                market_is_open=True,
                session_approved=True,
                account_confirmation="21",
                fill_reconciliation_available=True,
                audit_log_ready=True,
                managed_position_ledger_available=True,
                risk_limits_ok=True,
                new_entries_allowed=True,
            )

        self.assertTrue(result["approved"])
        self.assertEqual([], result["blockers"])

    def test_live_order_dry_run_reports_preflight_blockers_without_ordering(self):
        transport = FakeTransport()

        result = run_live_order_dry_run(
            order=Order.buy("005930", 2, "entry"),
            config=BotConfig.default(),
            env=live_env(),
            transport=transport,
            market_is_open=False,
            session_approved=False,
            account_confirmation="wrong",
            expected_account_suffix="21",
            audit_log_ready=False,
            risk_limits_ok=False,
            new_entries_allowed=False,
        )

        self.assertFalse(result["approved"])
        self.assertTrue(result["read_only"])
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["live_order_enabled"])
        self.assertFalse(result["order_submitted"])
        self.assertIn("trading_mode=live", result["blockers"])
        self.assertIn("market_is_open=true", result["blockers"])
        self.assertIn("session_approved=true", result["blockers"])
        self.assertIn("account_confirmation=21", result["blockers"])
        self.assertIn("new_entries_allowed=true", result["blockers"])
        paths = [call.path for call in transport.calls]
        self.assertNotIn("/uapi/hashkey", paths)
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", paths)

    def test_live_order_dry_run_fails_closed_without_orderable_cash(self):
        transport = MissingOrderableCashTransport()

        result = run_live_order_dry_run(
            order=Order.buy("005930", 1, "entry"),
            config=live_config(),
            env=live_env(),
            transport=transport,
            market_is_open=True,
            session_approved=True,
            account_confirmation="21",
            expected_account_suffix="21",
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        self.assertFalse(result["approved"])
        self.assertEqual("1000000", result["cash"])
        self.assertEqual("0", result["buying_power"])
        self.assertIn("buying_power", result["blockers"])
        paths = [call.path for call in transport.calls]
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            paths,
        )
        self.assertNotIn("/uapi/hashkey", paths)
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", paths)

    def test_live_probe_prefers_saved_env_file_over_process_env(self):
        transport = FakeTransport()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=file-key",
                        "KIS_LIVE_APP_SECRET=file-secret",
                        "KIS_LIVE_ACCOUNT_NO=file-live-account21",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "KIS_LIVE_APP_KEY": "stale-key",
                    "KIS_LIVE_APP_SECRET": "stale-secret",
                    "KIS_LIVE_ACCOUNT_NO": "stale-account99",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "99",
                },
            ):
                result = run_live_read_only_probe(
                    env_file=env_path,
                    symbol="005930",
                    transport=transport,
                )

        self.assertEqual("******21-01", result["account"])


if __name__ == "__main__":
    unittest.main()
