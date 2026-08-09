from __future__ import annotations

from dataclasses import replace
import json
import os
import sys
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stockbot.dashboard import DashboardController
from stockbot.kis import KisCredentials
from stockbot.kis_market_data import KisTokenFileCache
from stockbot.models import AccountSnapshot, MarketBar, Signal
from stockbot.broker import PaperBroker
from stockbot.config import BotConfig, KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, KIS_INTRADAY_REHEARSAL_SCAN_LIMIT
from stockbot.rate_limit import RateLimitDecision
from stockbot.live_audit import JsonlLiveAuditLog
from stockbot.live_broker import LiveBroker
from stockbot.runtime_factory import (
    CsvCycleBarProvider,
    LIVE_KIS_PHYSICAL_MARKET_READ_BUDGET,
    _build_paper_runtime,
    create_default_controller,
    create_live_runtime,
    create_paper_runtime,
    create_paper_runtime_for_data_source,
    _build_live_broker,
    _CachedKisOpeningDayGate,
    _history_entry_budget_from_config,
    _live_managed_positions_path,
    _live_profit_analytics_path,
    _live_manual_reconciliation_path,
    _live_env_values,
    _live_pending_orders_path,
    _profit_analytics_service,
    _restore_live_entry_counts,
    _synthetic_universe_bar,
    resolve_app_asset_path,
)
from stockbot.live_order_state import JsonManualReconciliationStore, JsonPendingLiveOrderStore
from stockbot.live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from stockbot.live_order_safety_context import LiveOrderSafetyContext
from stockbot.live_reconciliation import KisLiveOrderReconciler
from stockbot.live_safety import LIVE_CONFIRMATION_PHRASE
from stockbot.scanner import ScannerCandidate, ScannerSnapshot, StaticScannerProvider
from stockbot.strategy import FlowScalperConfig, FlowScalperStrategy
from stockbot.symbols import SymbolDirectory

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal


def make_bar(symbol, offset, volume):
    price = Decimal("10000")
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        vwap=price,
        bid=price,
        ask=price,
    )


class FakeLiveClient:
    def account_snapshot(self, *, timestamp=None):
        return None

    def inquire_daily_orders(self, **_kwargs):
        return {"output1": []}


class FakeKisProvider:
    def __call__(self, symbol):
        return make_bar(symbol, 0, 10000)


class AlwaysBuyStrategy:
    def seed_history(self, symbol, bars):
        return len(tuple(bars))

    def last_entry_score(self, symbol):
        return 100

    def on_bar(self, bar, account):
        return [Signal.buy(bar.symbol, "live path smoke")]


class AlwaysOpenMarketHours:
    class _Status:
        is_open = True
        label = "open"
        message = "open"

    def status(self):
        return self._Status()


class RecordingEntryCountRiskManager:
    def __init__(self):
        self.restored = None

    def restore_entry_counts(self, counts):
        self.restored = dict(counts)


class RecordingLiveTransport:
    def __init__(
        self,
        *,
        orderable_cash: Decimal = Decimal("1000000"),
        deposit_cash: Decimal | None = None,
        total_equity: Decimal | None = None,
    ):
        self.requests = []
        self.order_payloads = []
        self.order_no = "0000012345"
        self.orderable_cash = orderable_cash
        self.deposit_cash = deposit_cash
        self.total_equity = total_equity

    def __call__(self, request):
        self.requests.append(request)

        if request.path == "/oauth2/tokenP":
            return {
                "access_token": "live-token",
                "access_token_token_expired": "2026-06-29 23:00:00",
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-price":
            return {
                "rt_cd": "0",
                "output": {
                    "stck_prpr": "10000",
                    "stck_oprc": "9900",
                    "stck_hgpr": "10100",
                    "stck_lwpr": "9900",
                    "acml_vol": "900000",
                    "wghn_avrg_stck_prc": "10000",
                    "temp_stop_yn": "N",
                },
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn":
            return {
                "rt_cd": "0",
                "output1": {"askp1": "10010", "bidp1": "9990"},
                "output2": {},
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice":
            trading_date = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
            return {
                "rt_cd": "0",
                "output1": {"stck_prpr": "10000"},
                "output2": [
                    {
                        "stck_bsop_date": trading_date,
                        "stck_cntg_hour": f"090{index}00",
                        "stck_oprc": str(9900 + (index * 20)),
                        "stck_hgpr": str(9920 + (index * 20)),
                        "stck_lwpr": str(9890 + (index * 20)),
                        "stck_prpr": str(9910 + (index * 20)),
                        "cntg_vol": "1000",
                    }
                    for index in range(5)
                ],
            }

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-period-profit":
            trading_date = str(request.params.get("INQR_STRT_DT") or "")
            return {
                "rt_cd": "0",
                "output1": [{"trad_dt": trading_date, "rlzt_pfls": "0"}],
                "output2": [{"tot_rlzt_pfls": "0"}],
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/chk-holiday":
            trading_date = str(request.params.get("BASS_DT") or "")
            return {"rt_cd": "0", "output": [{"bass_dt": trading_date, "opnd_yn": "Y"}]}

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance":
            summary = {"ord_psbl_cash": str(self.orderable_cash)}
            if self.deposit_cash is not None:
                summary["dnca_tot_amt"] = str(self.deposit_cash)
            if self.total_equity is not None:
                summary["tot_evlu_amt"] = str(self.total_equity)
            return {
                "rt_cd": "0",
                "output1": [],
                "output2": [summary],
            }

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-psbl-order":
            price = Decimal(str(request.params.get("ORD_UNPR") or "0"))
            quantity = int(self.orderable_cash / price) if price > 0 else 0
            return {
                "rt_cd": "0",
                "output": {
                    "ord_psbl_cash": str(self.orderable_cash),
                    "ord_psbl_qty": str(quantity),
                },
            }

        if request.path == "/uapi/hashkey":
            return {"rt_cd": "0", "HASH": "hash-live"}

        if request.path == "/uapi/domestic-stock/v1/trading/order-cash":
            self.order_payloads.append(dict(request.json or {}))
            return {"rt_cd": "0", "output": {"ODNO": self.order_no}}

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-daily-ccld":
            payload = self.order_payloads[-1] if self.order_payloads else {}
            quantity = str(payload.get("ORD_QTY", "1"))
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": self.order_no,
                        "pdno": payload.get("PDNO", "BUY001"),
                        "sll_buy_dvsn_cd": "02",
                        "ord_qty": quantity,
                        "tot_ccld_qty": quantity,
                        "rmn_qty": "0",
                        "ord_unpr": payload.get("ORD_UNPR", "10000"),
                        "avg_prvs": "10000",
                        "ord_tmd": "090001",
                    }
                ],
            }

        raise AssertionError(f"unexpected KIS request path: {request.path}")


class StatefulLiveTransport:
    def __init__(self):
        self.requests = []
        self.order_payloads = []
        self.order_sides = []
        self.order_no = 1000000000
        self.cash = Decimal("1000000")
        self.prices = {
            "EXIT01": Decimal("9900"),
            "BUY002": Decimal("10000"),
        }
        self.positions = {
            "EXIT01": {"quantity": 5, "avg_price": Decimal("10000")},
        }

    def __call__(self, request):
        self.requests.append(request)

        if request.path == "/oauth2/tokenP":
            return {
                "access_token": "live-token",
                "access_token_token_expired": "2026-06-29 23:00:00",
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-price":
            symbol = str(request.params.get("fid_input_iscd") or request.params.get("FID_INPUT_ISCD") or "BUY002")
            price = self.prices.get(symbol, Decimal("10000"))
            return {
                "rt_cd": "0",
                "output": {
                    "stck_prpr": str(price),
                    "stck_oprc": str(price),
                    "stck_hgpr": str(price),
                    "stck_lwpr": str(price),
                    "acml_vol": "900000",
                    "wghn_avrg_stck_prc": str(price),
                    "temp_stop_yn": "N",
                },
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn":
            symbol = str(request.params.get("FID_INPUT_ISCD") or "BUY002")
            price = self.prices.get(symbol, Decimal("10000"))
            return {
                "rt_cd": "0",
                "output1": {"askp1": str(price), "bidp1": str(price)},
                "output2": {},
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice":
            symbol = str(request.params.get("FID_INPUT_ISCD") or "BUY002")
            price = self.prices.get(symbol, Decimal("10000"))
            trading_date = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y%m%d")
            return {
                "rt_cd": "0",
                "output1": {"stck_prpr": str(price)},
                "output2": [
                    {
                        "stck_bsop_date": trading_date,
                        "stck_cntg_hour": f"090{index}00",
                        "stck_oprc": str(price),
                        "stck_hgpr": str(price),
                        "stck_lwpr": str(price),
                        "stck_prpr": str(price),
                        "cntg_vol": "1000",
                    }
                    for index in range(5)
                ],
            }

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-period-profit":
            trading_date = str(request.params.get("INQR_STRT_DT") or "")
            return {
                "rt_cd": "0",
                "output1": [{"trad_dt": trading_date, "rlzt_pfls": "0"}],
                "output2": [{"tot_rlzt_pfls": "0"}],
            }

        if request.path == "/uapi/domestic-stock/v1/quotations/chk-holiday":
            trading_date = str(request.params.get("BASS_DT") or "")
            return {"rt_cd": "0", "output": [{"bass_dt": trading_date, "opnd_yn": "Y"}]}

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance":
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": symbol,
                        "hldg_qty": str(position["quantity"]),
                        "ord_psbl_qty": str(position["quantity"]),
                        "pchs_avg_pric": str(position["avg_price"]),
                        "prpr": str(self.prices.get(symbol, position["avg_price"])),
                    }
                    for symbol, position in self.positions.items()
                    if int(position["quantity"]) > 0
                ],
                "output2": [{"ord_psbl_cash": str(self.cash)}],
            }

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-psbl-order":
            price = Decimal(str(request.params.get("ORD_UNPR") or "0"))
            quantity = int(self.cash / price) if price > 0 else 0
            return {
                "rt_cd": "0",
                "output": {
                    "ord_psbl_cash": str(self.cash),
                    "ord_psbl_qty": str(quantity),
                },
            }

        if request.path == "/uapi/hashkey":
            return {"rt_cd": "0", "HASH": "hash-live"}

        if request.path == "/uapi/domestic-stock/v1/trading/order-cash":
            payload = dict(request.json or {})
            side = "SELL" if request.headers.get("tr_id") == "TTTC0011U" else "BUY"
            self.order_no += 1
            payload["_ODNO"] = str(self.order_no)
            self.order_payloads.append(payload)
            self.order_sides.append(side)
            self._apply_order(payload, side)
            return {"rt_cd": "0", "output": {"ODNO": str(self.order_no)}}

        if request.path == "/uapi/domestic-stock/v1/trading/inquire-daily-ccld":
            payload = self.order_payloads[-1] if self.order_payloads else {}
            side = self.order_sides[-1] if self.order_sides else "BUY"
            quantity = str(payload.get("ORD_QTY", "1"))
            price = str(payload.get("ORD_UNPR", "10000"))
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": payload.get("_ODNO", str(self.order_no)),
                        "pdno": payload.get("PDNO", "BUY002"),
                        "sll_buy_dvsn_cd": "01" if side == "SELL" else "02",
                        "ord_qty": quantity,
                        "tot_ccld_qty": quantity,
                        "rmn_qty": "0",
                        "ord_unpr": price,
                        "avg_prvs": price,
                        "ord_tmd": "090001",
                    }
                ],
            }

        raise AssertionError(f"unexpected KIS request path: {request.path}")

    def _apply_order(self, payload: dict, side: str) -> None:
        symbol = str(payload["PDNO"])
        quantity = int(payload["ORD_QTY"])
        price = Decimal(str(payload["ORD_UNPR"]))
        notional = price * Decimal(quantity)
        if side == "SELL":
            position = self.positions.get(symbol)
            if position is not None:
                remaining = int(position["quantity"]) - quantity
                if remaining <= 0:
                    self.positions.pop(symbol, None)
                else:
                    position["quantity"] = remaining
            self.cash += notional
            return

        existing = self.positions.get(symbol)
        if existing is None:
            self.positions[symbol] = {"quantity": quantity, "avg_price": price}
        else:
            old_quantity = int(existing["quantity"])
            total_quantity = old_quantity + quantity
            existing["avg_price"] = (
                (Decimal(existing["avg_price"]) * Decimal(old_quantity)) + notional
            ) / Decimal(total_quantity)
            existing["quantity"] = total_quantity
        self.cash -= notional


class ExitThenBuyStrategy:
    def __init__(self):
        self.config = type(
            "TestStrategyConfig",
            (),
            {
                "momentum_window": 1,
                "volume_window": 1,
                "trend_boundary_window": 2,
            },
        )()

    def seed_history(self, symbol, bars):
        return len(tuple(bars))

    def last_entry_score(self, symbol):
        return type(
            "TestSignalScore",
            (),
            {
                "confidence": 1.0,
                "long_score": 1.0 if symbol == "BUY002" else 0.0,
                "short_score": 0.0,
            },
        )()

    def on_bar(self, bar, account):
        if bar.symbol == "EXIT01" and bar.symbol in account.positions:
            return [Signal.sell("EXIT01", "live same-cycle exit")]
        if bar.symbol == "BUY002" and bar.symbol not in account.positions:
            return [Signal.buy("BUY002", "live same-cycle refill")]
        return []

    def revalidate_signal(self, provisional_signal, provisional_bar, final_bar, account):
        return next(
            (
                signal
                for signal in self.on_bar(final_bar, account)
                if signal.side == provisional_signal.side
            ),
            None,
        )


def make_approved_live_broker(root: Path, *, account_no: str = "12345678") -> LiveBroker:
    env = {
        "KIS_LIVE_APP_KEY": "live-app-key",
        "KIS_LIVE_APP_SECRET": "live-app-secret",
        "KIS_LIVE_ACCOUNT_NO": account_no,
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
        "STOCKBOT_ALLOW_LIVE_TRADING": "true",
        "STOCKBOT_LIVE_TRADING_ENABLED": "true",
        "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": account_no[-2:],
    }
    client = FakeLiveClient()
    config = BotConfig(
        trading_mode="live",
        allow_live_trading=True,
        live_trading_enabled=True,
        journal_path=str(root / "logs" / "trades.csv"),
    )
    scope = managed_live_position_ledger_scope(account_no, "01")
    env_path = root / ".env"
    return LiveBroker(
        client=client,
        config=config,
        env=env,
        audit_log=JsonlLiveAuditLog(root / "logs" / "live_audit.jsonl", redact_values=env.values()),
        market_is_open=True,
        session_approved=True,
        account_confirmation=account_no[-2:],
        expected_account_suffix=account_no[-2:],
        fill_reconciler=KisLiveOrderReconciler(client),
        pending_order_store=JsonPendingLiveOrderStore(
            _live_pending_orders_path(config, env_path, env),
            scope=scope,
        ),
        manual_reconciliation_store=JsonManualReconciliationStore(
            _live_manual_reconciliation_path(config, env_path, env),
            scope=scope,
        ),
        managed_position_ledger=JsonManagedLivePositionLedger(
            _live_managed_positions_path(config, env_path, env),
            scope=scope,
        ),
        risk_limits_ok=True,
        new_entries_allowed=True,
    )


class RecordingRateLimiter:
    def __init__(self):
        self.allow_calls = []
        self.recorded_requests = []

    def allow_request(self, kind="query"):
        self.allow_calls.append(kind)
        return RateLimitDecision(True, 0.0, "allowed")

    def record_request(self, kind="query"):
        self.recorded_requests.append(kind)

    def record_rate_limit_error(self, retry_after_seconds=None):
        return None


def write_approved_live_inputs(root: Path) -> tuple[Path, Path]:
    scanner_path = root / "scanner.json"
    env_path = root / ".env"
    scanner_path.write_text(
        json.dumps(
            {
                "provider": "external-file",
                "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
            }
        ),
        encoding="utf-8",
    )
    env_path.write_text(
        "\n".join(
            [
                "KIS_LIVE_APP_KEY=live-app-key",
                "KIS_LIVE_APP_SECRET=live-app-secret",
                "KIS_LIVE_ACCOUNT_NO=12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                "STOCKBOT_ALLOW_LIVE_TRADING=true",
                "STOCKBOT_LIVE_TRADING_ENABLED=true",
                f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return scanner_path, env_path


def create_runtime_with_injected_broker(root: Path, broker: LiveBroker, scanner_path: Path, env_path: Path):
    return create_live_runtime(
        config=BotConfig(
            trading_mode="live",
            market_data_source="external-scan-kis",
            scanner_source="json",
            scanner_snapshot_path=str(scanner_path),
            allow_live_trading=True,
            live_trading_enabled=True,
            journal_path=str(root / "logs" / "trades.csv"),
        ),
        symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
        kis_bar_provider=FakeKisProvider(),
        live_broker=broker,
        env_file=env_path,
    )


class RuntimeFactoryTest(unittest.TestCase):
    def test_cached_kis_opening_day_gate_caches_authoritative_result_per_trading_date(self):
        class Client:
            def __init__(self, result=True):
                self.result = result
                self.calls = []

            def is_opening_day(self, trading_date):
                self.calls.append(trading_date)
                if isinstance(self.result, Exception):
                    raise self.result
                return self.result

        open_client = Client(True)
        open_gate = _CachedKisOpeningDayGate(open_client, AlwaysOpenMarketHours())

        self.assertEqual(10, open_gate.pending_market_read_cost())
        self.assertTrue(open_gate())
        self.assertEqual(0, open_gate.pending_market_read_cost())
        self.assertTrue(open_gate())
        self.assertEqual(1, len(open_client.calls))

        closed_client = Client(False)
        closed_gate = _CachedKisOpeningDayGate(closed_client, AlwaysOpenMarketHours())

        self.assertFalse(closed_gate())
        self.assertFalse(closed_gate())
        self.assertEqual(1, len(closed_client.calls))

    def test_cached_kis_opening_day_gate_estimate_is_zero_when_local_market_is_closed(self):
        class ClosedMarketHours:
            class _Status:
                is_open = False

            def status(self):
                return self._Status()

        gate = _CachedKisOpeningDayGate(object(), ClosedMarketHours())

        self.assertEqual(0, gate.pending_market_read_cost())

    def test_cached_kis_opening_day_gate_estimate_is_conservative_when_status_or_date_is_uncertain(self):
        class UncertainMarketHours:
            def status(self):
                raise RuntimeError("market status unavailable")

        status_gate = _CachedKisOpeningDayGate(object(), UncertainMarketHours())
        date_gate = _CachedKisOpeningDayGate(object(), AlwaysOpenMarketHours())

        self.assertEqual(10, status_gate.pending_market_read_cost())
        with patch("stockbot.runtime_factory.datetime") as mocked_datetime:
            mocked_datetime.now.side_effect = RuntimeError("clock unavailable")
            self.assertEqual(10, date_gate.pending_market_read_cost())

    def test_cached_kis_opening_day_gate_retries_after_transient_failure(self):
        class RecoveringClient:
            def __init__(self):
                self.calls = []
                self.responses = [RuntimeError("holiday unavailable"), None, True]

            def is_opening_day(self, trading_date):
                self.calls.append(trading_date)
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        client = RecoveringClient()
        gate = _CachedKisOpeningDayGate(client, AlwaysOpenMarketHours())

        self.assertFalse(gate())
        self.assertFalse(gate())
        self.assertTrue(gate())
        self.assertTrue(gate())
        self.assertEqual(3, len(client.calls))

    def test_cached_kis_opening_day_gate_skips_kis_when_local_market_is_closed(self):
        class ClosedMarketHours:
            class _Status:
                is_open = False

            def status(self):
                return self._Status()

        class UnexpectedClient:
            def is_opening_day(self, trading_date):
                raise AssertionError("KIS must not be called outside local market hours")

        gate = _CachedKisOpeningDayGate(UnexpectedClient(), ClosedMarketHours())

        self.assertFalse(gate())

    def test_live_kis_physical_market_read_budget_is_exactly_twenty_six(self):
        self.assertEqual(26, LIVE_KIS_PHYSICAL_MARKET_READ_BUDGET)

    def test_create_live_runtime_reuses_cached_live_token_from_account_probe(self):
        class TokenRejectingLiveTransport(RecordingLiveTransport):
            def __call__(self, request):
                if request.path == "/oauth2/tokenP":
                    raise AssertionError("runtime should reuse cached live token")
                return super().__call__(request)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            token_cache = KisTokenFileCache(
                root / "kis-token-cache.json",
                namespace="kis-live",
                clock=lambda: datetime(2026, 7, 8, 9, 0, 0),
            )
            token_cache.write(
                KisCredentials(
                    app_key="live-app-key",
                    app_secret="live-app-secret",
                    account_no="12345678",
                    account_product_code="01",
                ),
                "cached-live-token",
                datetime(2026, 7, 8, 23, 0, 0),
            )
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = TokenRejectingLiveTransport()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_positions=1,
                        scan_limit_per_cycle=1,
                        kis_market_data_scan_limit=1,
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    env_file=env_path,
                    live_transport=transport,
                    live_order_safety_context=safety_context,
                    live_token_cache=token_cache,
                )

            bar = runtime.final_quote_provider("BUY001")

            self.assertEqual(Decimal("10000"), bar.close)
            self.assertIs(runtime.broker.client.token_cache, token_cache)
            self.assertNotIn("/oauth2/tokenP", [request.path for request in transport.requests])
            price_request = next(
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/quotations/inquire-price"
            )
            self.assertEqual("Bearer cached-live-token", price_request.headers["authorization"])

    def test_app_asset_resolver_supports_pyinstaller_bundle_root(self):
        bundle_root = Path("C:/StockBotBundle")

        self.assertEqual(
            bundle_root / "assets" / "stockbot-donghak-ant-icon.png",
            resolve_app_asset_path("assets", "stockbot-donghak-ant-icon.png", bundle_root=bundle_root),
        )

    def test_create_default_controller_wires_local_paper_runtime(self):
        controller = create_default_controller()

        self.assertIsInstance(controller, DashboardController)
        self.assertIsNotNone(controller.services.runtime)
        self.assertTrue(callable(controller.services.live_runtime_builder))
        self.assertTrue(callable(controller.services.profit_report))
        self.assertIsNot(controller.services.kis_rate_limiter, controller.services.runtime.rate_limiter)
        self.assertIs(controller.services.paper_kis_rate_limiter, controller.services.runtime.rate_limiter)
        self.assertEqual(0.15, controller.services.kis_rate_limiter.min_interval_seconds)
        self.assertEqual(1.25, controller.services.runtime.rate_limiter.min_interval_seconds)
        self.assertIsNone(controller.services.runtime.market_hours)
        self.assertEqual("샘플 CSV", controller.services.runtime.data_source_label)

        started = controller.start_paper_runtime()

        self.assertEqual("실행 중", started.runtime_status)
        self.assertIn("샘플 CSV", started.system_log[0].message)

    def test_profit_analytics_service_uses_account_scoped_local_files_without_identifiers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            config = BotConfig(journal_path=str(root / "logs" / "trades.csv"))
            env_values = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "87654321",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            }
            service = _profit_analytics_service(
                config=config,
                env_file=env_path,
                env_values=env_values,
            )
            service.account_store.record_kis_period(
                (
                    SimpleNamespace(
                        trading_date=date(2026, 7, 29),
                        realized_pnl=Decimal("1200"),
                        fee=Decimal("20"),
                        tax=Decimal("30"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                observed_at=datetime(2026, 7, 29, 15, 31, tzinfo=timezone(timedelta(hours=9))),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 29),
            )

            report = service.query(
                granularity="day",
                scope="account",
                anchor="2026-07-29",
            )
            analytics_path = _live_profit_analytics_path(config, env_path, env_values)

            self.assertEqual(1200, report["summary"]["reportedRealizedPnlKrw"])
            self.assertTrue(analytics_path.exists())
            self.assertNotIn("87654321", analytics_path.name)
            self.assertNotIn("87654321", json.dumps(report))

    def test_paper_runtime_ignores_market_hours_gate_for_local_virtual_replay(self):
        runtime = _build_paper_runtime(
            BotConfig(allow_after_hours_simulation=False, enforce_market_hours=True),
            SymbolDirectory({"005930": "Samsung Electronics"}),
            bars=[make_bar("005930", 0, 1000)],
        )

        self.assertIsNone(runtime.market_hours)

    def test_default_controller_watches_broad_local_universe(self):
        controller = create_default_controller()
        runtime = controller.services.runtime

        self.assertIsNotNone(runtime)
        self.assertGreaterEqual(len(runtime.symbols), 1000)
        self.assertIn("005930", runtime.symbols)
        self.assertIn("000660", runtime.symbols)
        self.assertIn("005380", runtime.symbols)

    def test_live_json_scanner_requires_a_current_minute_snapshot(self):
        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text("[]", encoding="utf-8")
            config = BotConfig(
                market_data_source="external-scan-kis",
                scanner_source="json",
                scanner_snapshot_path=str(snapshot_path),
            )
            scanner = StaticScannerProvider(
                bars={"BUY001": make_bar("BUY001", 0, 1000)},
            )

            with patch(
                "stockbot.runtime_factory._scanner_provider_from_config",
                return_value=scanner,
            ) as provider_factory:
                _build_paper_runtime(
                    config,
                    SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    execution_mode="live",
                )

        self.assertTrue(provider_factory.call_args.kwargs["require_current_minute"])
        self.assertEqual(
            60.0,
            provider_factory.call_args.kwargs["refresh_failure_retry_seconds"],
        )

    def test_default_controller_provides_synthetic_bars_outside_sample_csv(self):
        controller = create_default_controller()
        runtime = controller.services.runtime

        self.assertIsNotNone(runtime)
        bar = runtime.bar_provider("005380")

        self.assertIsNotNone(bar)
        self.assertEqual("005380", bar.symbol)

    def test_csv_cycle_bar_provider_advances_timestamps_after_sample_end(self):
        bars = [
            make_bar("005930", 0, 1000),
            replace(
                make_bar("005930", 1, 1000),
                open=Decimal("10100"),
                high=Decimal("10100"),
                low=Decimal("10100"),
                close=Decimal("10100"),
                vwap=Decimal("10100"),
                bid=Decimal("10100"),
                ask=Decimal("10100"),
            ),
        ]
        provider = CsvCycleBarProvider(bars)

        first = provider("005930")
        second = provider("005930")
        third = provider("005930")

        self.assertEqual(datetime(2026, 6, 8, 9, 0), first.timestamp)
        self.assertEqual(datetime(2026, 6, 8, 9, 1), second.timestamp)
        self.assertEqual(datetime(2026, 6, 8, 9, 2), third.timestamp)
        self.assertEqual(Decimal("10000"), third.close)

    def test_synthetic_universe_bar_staggers_momentum_spikes_by_symbol(self):
        first_symbol_spikes = [
            index for index in range(10) if _synthetic_universe_bar("000001", index).volume >= 6000
        ]
        second_symbol_spikes = [
            index for index in range(10) if _synthetic_universe_bar("000002", index).volume >= 6000
        ]

        self.assertNotEqual(first_symbol_spikes, second_symbol_spikes)

    def test_synthetic_universe_can_generate_paper_short_entries_when_enabled(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(allow_paper_short=True))
        account = AccountSnapshot(cash=Decimal("10000000"), positions={}, realized_pnl_today=Decimal("0"))
        short_entries = []

        for index in range(12):
            for symbol_number in range(1, 61):
                symbol = f"{symbol_number:06d}"
                signals = strategy.on_bar(_synthetic_universe_bar(symbol, index), account)
                short_entries.extend(signal for signal in signals if signal.side == "SHORT_ENTRY")

        self.assertTrue(short_entries)

    def test_default_controller_can_open_paper_short_positions_when_enabled(self):
        controller = create_default_controller()
        runtime = controller.services.runtime
        runtime.market_hours = None
        runtime.rate_limiter = None
        runtime.symbols = runtime.symbols[:120]
        controller.apply_custom_settings(runtime.settings.with_updates(allow_paper_short=True))
        controller.start_paper_runtime()

        short_position_seen = False
        for _ in range(8):
            controller.run_paper_cycle()
            short_position_seen = short_position_seen or any(
                position.side == "SHORT" for position in runtime.broker.snapshot().positions.values()
            )

        self.assertTrue(short_position_seen)

    def test_default_controller_sample_runtime_fills_multiple_symbols_and_marks_equity(self):
        controller = create_default_controller()
        runtime = controller.services.runtime
        runtime.market_hours = None
        runtime.rate_limiter = None
        controller.start_paper_runtime()

        for _ in range(4):
            state = controller.run_paper_cycle()

        self.assertGreater(len(state.active_positions), 3)
        self.assertGreater(len({row.symbol for row in state.active_positions}), 1)
        self.assertNotEqual("1,000,000원", state.account.cash)
        self.assertEqual("1,000,000원", state.account.equity)

        state = controller.run_paper_cycle()

        self.assertNotEqual("1,000,000원", state.account.equity)
        self.assertEqual("가상계좌", state.account.masked_account)

    def test_default_controller_keeps_some_open_positions_across_cycles(self):
        controller = create_default_controller()
        runtime = controller.services.runtime
        runtime.market_hours = None
        runtime.rate_limiter = None
        controller.start_paper_runtime()

        state = controller.state
        for _ in range(4):
            state = controller.run_paper_cycle()
        opened_symbols = {row.symbol for row in state.active_positions}

        next_state = controller.run_paper_cycle()
        state_after_two_more_cycles = controller.run_paper_cycle()

        self.assertTrue(opened_symbols)
        self.assertTrue(opened_symbols & {row.symbol for row in next_state.active_positions})
        self.assertTrue(opened_symbols & {row.symbol for row in state_after_two_more_cycles.active_positions})

    def test_default_controller_refills_after_liquidation_without_empty_visible_cycle(self):
        controller = create_default_controller()
        runtime = controller.services.runtime
        runtime.market_hours = None
        runtime.rate_limiter = None
        controller.start_paper_runtime()

        seen_open_positions = False
        empty_cycles_after_open = []
        for cycle_number in range(1, 13):
            state = controller.run_paper_cycle()
            if state.active_positions:
                seen_open_positions = True
            elif seen_open_positions:
                empty_cycles_after_open.append(cycle_number)

        self.assertTrue(seen_open_positions)
        self.assertEqual([], empty_cycles_after_open)

    def test_default_controller_active_positions_are_not_locked_to_sample_symbols(self):
        controller = create_default_controller()
        runtime = controller.services.runtime
        runtime.market_hours = None
        runtime.rate_limiter = None
        controller.start_paper_runtime()

        for _ in range(4):
            state = controller.run_paper_cycle()

        sample_symbols = {"005930", "000660", "035420"}
        active_symbols = {row.symbol for row in state.active_positions}

        self.assertTrue(active_symbols - sample_symbols)

    def test_default_controller_defers_sample_csv_loading_until_runtime_cycle(self):
        with patch("stockbot.runtime_factory.read_csv_bars") as read_csv_bars:
            controller = create_default_controller()

        self.assertIsNotNone(controller.services.runtime)
        read_csv_bars.assert_not_called()

    def test_create_default_controller_shares_live_token_cache_with_live_runtime_builder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            data_path = root / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            shared_cache = object()
            live_runtime = object()

            with (
                patch("stockbot.runtime_factory.KisTokenFileCache", return_value=shared_cache),
                patch("stockbot.runtime_factory.create_live_runtime", return_value=live_runtime) as create_live,
            ):
                controller = create_default_controller(config_path=config_path)
                built = controller.services.live_runtime_builder()

        self.assertIs(built, live_runtime)
        self.assertIs(controller._live_token_cache, shared_cache)
        self.assertIs(create_live.call_args.kwargs["live_token_cache"], shared_cache)

    def test_default_controller_keeps_paper_and_live_request_pacing_separate(self):
        live_runtime = object()

        with patch("stockbot.runtime_factory.create_live_runtime", return_value=live_runtime) as create_live:
            controller = create_default_controller()
            built = controller.services.live_runtime_builder()

        paper_limiter = controller.services.runtime.rate_limiter
        live_limiter = create_live.call_args.kwargs["rate_limiter"]
        self.assertIs(built, live_runtime)
        self.assertEqual(1.25, paper_limiter.min_interval_seconds)
        self.assertEqual(0.15, live_limiter.min_interval_seconds)
        self.assertIsNot(paper_limiter, live_limiter)
        self.assertIs(controller.services.kis_rate_limiter, live_limiter)
        self.assertIs(controller.services.paper_kis_rate_limiter, paper_limiter)

    def test_create_default_controller_live_builder_derives_live_config_from_dashboard_env(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            data_path = root / "bars.csv"
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000",
                    ]
                ),
                encoding="utf-8",
            )
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=test-live-account",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=nt",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "allow_paper_short: true",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path.as_posix()}",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            live_runtime = object()

            with patch("stockbot.runtime_factory.create_live_runtime", return_value=live_runtime) as create_live:
                controller = create_default_controller(config_path=config_path, env_file=env_path)
                built = controller.services.live_runtime_builder()

        self.assertIs(built, live_runtime)
        self.assertEqual(str(env_path), controller.env_file)
        live_config = create_live.call_args.kwargs["config"]
        self.assertEqual("live", live_config.trading_mode)
        self.assertEqual("external-scan-kis", live_config.market_data_source)
        self.assertEqual("json", live_config.scanner_source)
        self.assertEqual(scanner_path.resolve(), Path(live_config.scanner_snapshot_path).resolve())
        self.assertTrue(live_config.allow_live_trading)
        self.assertTrue(live_config.live_trading_enabled)
        self.assertFalse(live_config.allow_paper_short)
        self.assertEqual(str(env_path), str(create_live.call_args.kwargs["env_file"]))

    def test_default_controller_real_start_then_cycle_submits_live_order_from_dashboard_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            data_path = root / "bars.csv"
            scanner_path = root / "data" / "scanner_snapshot.json"
            env_path = root / ".env"
            scanner_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,BUY001,10000,10000,10000,10000,900000,10000",
                    ]
                ),
                encoding="utf-8",
            )

            def write_scanner_snapshot(path: Path) -> None:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(
                        {
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "provider": "external-file",
                            "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                        }
                    ),
                    encoding="utf-8",
                )

            write_scanner_snapshot(scanner_path)
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=test-live-account",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path.as_posix()}",
                        f"data_path: {data_path.as_posix()}",
                        "scanner_snapshot_max_age_seconds: 3600",
                        "initial_cash: 1000000",
                        "max_order_amount: 0",
                        "max_position_amount: 300000",
                        "max_positions: 1",
                        "scan_limit_per_cycle: 1",
                        "kis_market_data_scan_limit: 1",
                        "min_signal_confidence: 0",
                        "min_volume_ratio: 0",
                        "transaction_tax_pct: 0",
                        "slippage_pct: 0",
                        "min_net_profit_pct: 0",
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )
            transport = RecordingLiveTransport()

            def fake_scanner_refresh(output_path, *_args, **_kwargs):
                write_scanner_snapshot(Path(output_path))
                return 1

            def create_live_runtime_with_fake_transport(**kwargs):
                kwargs = dict(kwargs)
                kwargs["live_transport"] = transport
                kwargs["live_token_cache"] = None
                return create_live_runtime(**kwargs)

            with (
                patch("stockbot.runtime_factory._load_default_symbol_directory", return_value=SymbolDirectory({"BUY001": "Buy One"})),
                patch("stockbot.runtime_factory.create_live_runtime", side_effect=create_live_runtime_with_fake_transport),
                patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()),
                patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", side_effect=fake_scanner_refresh),
                patch("stockbot.runtime_factory.collect_naver_market_scanner_snapshot", side_effect=fake_scanner_refresh),
            ):
                controller = create_default_controller(config_path=config_path, env_file=env_path)
                controller.services = replace(
                    controller.services,
                    kis_live_check=lambda **_: {
                        "account": "******nt-01",
                        "cash": "1000000",
                        "equity": "1000000",
                        "buying_power": "1000000",
                        "balance_positions": 0,
                        "last_price": "10000",
                        "read_only": True,
                        "live_order_enabled": False,
                    },
                )
                controller.select_trading_mode("real")

                start_state = controller.start_paper_runtime()
                runtime = controller.services.runtime
                runtime.strategy = AlwaysBuyStrategy()
                controller.run_paper_cycle()

        order_requests = [
            request
            for request in transport.requests
            if request.path == "/uapi/domestic-stock/v1/trading/order-cash"
        ]
        self.assertEqual("real", start_state.trading_mode)
        self.assertTrue(getattr(controller, "_runtime_running"))
        self.assertEqual("live", runtime.execution_mode)
        self.assertEqual(1, len(order_requests))
        self.assertEqual("TTTC0012U", order_requests[0].headers["tr_id"])
        self.assertEqual("BUY001", transport.order_payloads[0]["PDNO"])
        self.assertGreater(int(transport.order_payloads[0]["ORD_QTY"]), 0)

    def test_create_default_controller_boots_paper_runtime_when_config_contains_live_flags(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "market_data_source: local",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch("stockbot.runtime_factory._default_config_path", return_value=config_path):
                controller = create_default_controller()

        self.assertIsInstance(controller, DashboardController)
        self.assertEqual("paper", controller.services.runtime.execution_mode)
        self.assertTrue(callable(controller.services.live_runtime_builder))

    def test_create_default_controller_accepts_explicit_config_path(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )

            controller = create_default_controller(config_path=config_path)

        self.assertEqual(str(config_path), controller.config_path)
        self.assertEqual("paper", controller.services.runtime.execution_mode)
        self.assertTrue(callable(controller.services.live_runtime_builder))

    def test_create_default_controller_prefers_stockbot_config_path_env(self):
        with TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"STOCKBOT_CONFIG_PATH": str(config_path)}):
                controller = create_default_controller()

        self.assertEqual(str(config_path), controller.config_path)
        self.assertEqual("paper", controller.services.runtime.execution_mode)
        self.assertTrue(callable(controller.services.live_runtime_builder))

    def test_create_paper_runtime_exposes_broker(self):
        runtime = create_paper_runtime()

        self.assertTrue(hasattr(runtime, "broker"))

    def test_build_live_broker_uses_in_process_safety_context(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "78",
            }
            context = LiveOrderSafetyContext()
            broker = _build_live_broker(
                BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    journal_path=str(root / "logs" / "trades.csv"),
                ),
                env,
                client=FakeLiveClient(),
                env_file=root / ".env",
                live_order_safety_context=context,
            )

            self.assertFalse(broker.session_approved())
            self.assertFalse(broker.risk_limits_ok())
            self.assertFalse(broker.new_entries_allowed())

            context.approve_session()

            self.assertTrue(broker.session_approved())
            self.assertTrue(broker.risk_limits_ok())
            self.assertTrue(broker.new_entries_allowed())

            context.set_cleanup_mode(True)

            self.assertTrue(broker.session_approved())
            self.assertTrue(broker.risk_limits_ok())
            self.assertFalse(broker.new_entries_allowed())

    def test_build_live_broker_requires_live_config_before_synthesizing_broker_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "78",
            }

            with self.assertRaisesRegex(ValueError, "live runtime requires trading_mode=live"):
                _build_live_broker(
                    BotConfig(
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    env,
                    client=FakeLiveClient(),
                    env_file=root / ".env",
                )

    def test_build_live_broker_rejects_paper_short_enabled_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "78",
            }

            with self.assertRaisesRegex(ValueError, "live runtime requires allow_paper_short=false"):
                _build_live_broker(
                    BotConfig(
                        trading_mode="live",
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        allow_paper_short=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    env,
                    client=FakeLiveClient(),
                    env_file=root / ".env",
                )

    def test_create_live_runtime_reuses_scanner_strategy_with_injected_live_broker(self):
        class FakeKisProvider:
            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, len(self.symbols), 10000)

        provider = FakeKisProvider()

        with TemporaryDirectory() as directory:
            root = Path(directory)
            broker = make_approved_live_broker(root)
            restored_trading_day = date(2026, 7, 10)
            broker.managed_position_ledger.record_entry("BUY001", restored_trading_day)
            scanner_path = Path(directory) / "scanner.json"
            env_path = Path(directory) / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 900000, "priority": 900},
                            {"symbol": "BUY002", "price": "12000", "volume": 800000, "priority": 800},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            runtime = create_live_runtime(
                config=BotConfig(
                    trading_mode="live",
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    initial_cash=Decimal("1000000"),
                    max_positions=10,
                    scan_limit_per_cycle=100,
                    allow_live_trading=True,
                    live_trading_enabled=True,
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
                kis_bar_provider=provider,
                live_broker=broker,
                env_file=env_path,
            )

        self.assertIs(runtime.broker, broker)
        self.assertEqual("live", runtime.execution_mode)
        self.assertEqual("live", runtime.data_source_kind)
        self.assertEqual("KIS live orders / scanner", runtime.data_source_label)
        self.assertEqual(["BUY001", "BUY002"], runtime.symbols)
        self.assertIs(runtime.final_quote_provider, provider)
        self.assertTrue(runtime._uses_authoritative_scanner())
        self.assertEqual({}, runtime._successful_bar_samples)
        self.assertTrue(runtime.risk_manager.entry_limit_reached("BUY001", restored_trading_day))
        self.assertEqual(
            LIVE_KIS_PHYSICAL_MARKET_READ_BUDGET,
            runtime.max_physical_market_reads_per_cycle,
        )

    def test_create_live_runtime_wires_shared_rate_limiter_to_live_client_and_runtime(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            limiter = RecordingRateLimiter()
            transport = RecordingLiveTransport()
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=limiter,
                    live_order_safety_context=safety_context,
                )

            self.assertIs(runtime.rate_limiter, limiter)
            self.assertIs(runtime.broker.client.rate_limiter, limiter)
            self.assertIs(runtime.final_quote_provider.__self__, runtime.broker.client)
            self.assertTrue(callable(runtime.broker.client.profit_observer))
            runtime.broker.client.profit_observer(
                (
                    SimpleNamespace(
                        trading_date=date(2026, 7, 29),
                        realized_pnl=Decimal("500"),
                        fee=Decimal("10"),
                        tax=Decimal("20"),
                        loan_interest=Decimal("0"),
                    ),
                ),
                datetime(2026, 7, 29, 15, 31, tzinfo=timezone(timedelta(hours=9))),
                date(2026, 7, 1),
                date(2026, 7, 29),
            )
            self.assertEqual(1, len(list((root / "logs").glob("profit_analytics_*.sqlite3"))))

    def test_create_live_runtime_uses_live_request_interval_when_limiter_is_not_injected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            broker = make_approved_live_broker(root)

            runtime = create_runtime_with_injected_broker(root, broker, scanner_path, env_path)

        self.assertEqual(0.15, runtime.rate_limiter.min_interval_seconds)

    def test_create_live_runtime_run_cycle_submits_and_reconciles_real_cash_buy_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = RecordingLiveTransport()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_order_amount=Decimal("0"),
                        max_position_amount=Decimal("300000"),
                        max_positions=1,
                        scan_limit_per_cycle=1,
                        kis_market_data_scan_limit=1,
                        min_signal_confidence=Decimal("0"),
                        min_volume_ratio=Decimal("0"),
                        transaction_tax_pct=Decimal("0"),
                        slippage_pct=Decimal("0"),
                        min_net_profit_pct=Decimal("0"),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=RecordingRateLimiter(),
                    live_order_safety_context=safety_context,
                )

            runtime.strategy = AlwaysBuyStrategy()
            runtime.start()
            events = runtime.run_cycle()

            order_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/order-cash"
            ]
            order_payloads = transport.order_payloads
            trade_events = [event for event in events if event.kind == "trade"]
            balance_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance"
            ]

            self.assertEqual(1, len(order_requests))
            self.assertEqual(2, len(balance_requests))
            self.assertEqual("TTTC0012U", order_requests[0].headers["tr_id"])
            self.assertEqual(1, len(order_payloads))
            self.assertEqual("BUY001", order_payloads[0]["PDNO"])
            self.assertGreater(int(order_payloads[0]["ORD_QTY"]), 0)
            self.assertEqual(
                int(order_payloads[0]["ORD_QTY"]),
                runtime.broker.managed_position_ledger.quantity_for("BUY001"),
            )
            self.assertTrue(
                any(
                    event.result == "filled" and event.side == "BUY" and event.symbol == "BUY001"
                    for event in trade_events
                )
            )

    def test_create_live_runtime_sizes_real_buy_from_kis_orderable_cash_and_equity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = RecordingLiveTransport(
                orderable_cash=Decimal("600000"),
                deposit_cash=Decimal("1000000"),
                total_equity=Decimal("1000000"),
            )

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_order_amount=Decimal("0"),
                        max_position_amount=Decimal("200000"),
                        max_positions=1,
                        scan_limit_per_cycle=1,
                        kis_market_data_scan_limit=1,
                        min_signal_confidence=Decimal("0"),
                        min_volume_ratio=Decimal("0"),
                        transaction_tax_pct=Decimal("0"),
                        slippage_pct=Decimal("0"),
                        min_net_profit_pct=Decimal("0"),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=RecordingRateLimiter(),
                    live_order_safety_context=safety_context,
                )

            runtime.strategy = AlwaysBuyStrategy()
            runtime.start()
            runtime.run_cycle()

            self.assertEqual(1, len(transport.order_payloads))
            self.assertEqual("19", transport.order_payloads[0]["ORD_QTY"])

    def test_create_live_runtime_refills_after_live_exit_in_same_cycle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {
                                "symbol": "EXIT01",
                                "price": "9900",
                                "open": "10000",
                                "high": "10100",
                                "low": "9800",
                                "volume": 950000,
                                "priority": 100,
                            },
                            {
                                "symbol": "BUY002",
                                "price": "10000",
                                "open": "9900",
                                "high": "10100",
                                "low": "9900",
                                "volume": 900000,
                                "priority": 90,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = StatefulLiveTransport()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_order_amount=Decimal("0"),
                        max_position_amount=Decimal("300000"),
                        max_positions=1,
                        scan_limit_per_cycle=2,
                        kis_market_data_scan_limit=4,
                        min_signal_confidence=Decimal("0"),
                        min_volume_ratio=Decimal("0"),
                        transaction_tax_pct=Decimal("0"),
                        slippage_pct=Decimal("0"),
                        min_net_profit_pct=Decimal("0"),
                        journal_path=str(root / "logs" / "trades.csv"),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"EXIT01": "Exit One", "BUY002": "Buy Two"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=RecordingRateLimiter(),
                    live_order_safety_context=safety_context,
                )

            runtime.broker.managed_position_ledger.add("EXIT01", 5)
            runtime.strategy = ExitThenBuyStrategy()
            runtime.start()
            events = runtime.run_cycle()

            order_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/order-cash"
            ]
            trade_events = [event for event in events if event.kind == "trade"]
            balance_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance"
            ]

            self.assertEqual(["TTTC0011U", "TTTC0012U"], [request.headers["tr_id"] for request in order_requests])
            self.assertEqual(3, len(balance_requests))
            self.assertEqual(["EXIT01", "BUY002"], [payload["PDNO"] for payload in transport.order_payloads])
            self.assertEqual(0, runtime.broker.managed_position_ledger.quantity_for("EXIT01"))
            self.assertGreater(runtime.broker.managed_position_ledger.quantity_for("BUY002"), 0)
            self.assertTrue(
                any(
                    event.result == "filled" and event.side == "SELL" and event.symbol == "EXIT01"
                    for event in trade_events
                )
            )
            self.assertTrue(
                any(
                    event.result == "filled" and event.side == "BUY" and event.symbol == "BUY002"
                    for event in trade_events
                )
            )

    def test_create_live_runtime_adopts_existing_account_positions_for_strategy_exit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {
                                "symbol": "EXIT01",
                                "price": "9900",
                                "open": "10000",
                                "high": "10100",
                                "low": "9800",
                                "volume": 950000,
                                "priority": 100,
                            },
                            {
                                "symbol": "BUY002",
                                "price": "10000",
                                "open": "9900",
                                "high": "10100",
                                "low": "9900",
                                "volume": 900000,
                                "priority": 90,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = StatefulLiveTransport()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_order_amount=Decimal("0"),
                        max_position_amount=Decimal("300000"),
                        max_positions=1,
                        scan_limit_per_cycle=2,
                        kis_market_data_scan_limit=4,
                        min_signal_confidence=Decimal("0"),
                        min_volume_ratio=Decimal("0"),
                        transaction_tax_pct=Decimal("0"),
                        slippage_pct=Decimal("0"),
                        min_net_profit_pct=Decimal("0"),
                        journal_path=str(root / "logs" / "trades.csv"),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"EXIT01": "Exit One", "BUY002": "Buy Two"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=RecordingRateLimiter(),
                    live_order_safety_context=safety_context,
                )

            runtime.strategy = ExitThenBuyStrategy()
            runtime.start()
            events = runtime.run_cycle()

            order_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/order-cash"
            ]
            trade_events = [event for event in events if event.kind == "trade"]
            balance_requests = [
                request
                for request in transport.requests
                if request.path == "/uapi/domestic-stock/v1/trading/inquire-balance"
            ]

            self.assertEqual(["TTTC0011U", "TTTC0012U"], [request.headers["tr_id"] for request in order_requests])
            self.assertEqual(3, len(balance_requests))
            self.assertEqual(["EXIT01", "BUY002"], [payload["PDNO"] for payload in transport.order_payloads])
            self.assertEqual(0, runtime.broker.managed_position_ledger.quantity_for("EXIT01"))
            self.assertGreater(runtime.broker.managed_position_ledger.quantity_for("BUY002"), 0)
            self.assertTrue(
                any(
                    event.result == "filled" and event.side == "SELL" and event.symbol == "EXIT01"
                    for event in trade_events
                )
            )
            self.assertTrue(
                any(
                    event.result == "filled" and event.side == "BUY" and event.symbol == "BUY002"
                    for event in trade_events
                )
            )

    def test_create_live_runtime_quotes_adopted_holding_missing_from_scanner_for_strategy_exit(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {
                                "symbol": "BUY002",
                                "price": "10000",
                                "open": "9900",
                                "high": "10100",
                                "low": "9900",
                                "volume": 900000,
                                "priority": 90,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            safety_context = LiveOrderSafetyContext()
            safety_context.approve_session()
            transport = StatefulLiveTransport()

            with patch("stockbot.runtime_factory._regular_market_hours_from_config", return_value=AlwaysOpenMarketHours()):
                runtime = create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=3600,
                        initial_cash=Decimal("1000000"),
                        max_order_amount=Decimal("0"),
                        max_position_amount=Decimal("300000"),
                        max_positions=1,
                        scan_limit_per_cycle=1,
                        kis_market_data_scan_limit=4,
                        min_signal_confidence=Decimal("0"),
                        min_volume_ratio=Decimal("0"),
                        transaction_tax_pct=Decimal("0"),
                        slippage_pct=Decimal("0"),
                        min_net_profit_pct=Decimal("0"),
                        journal_path=str(root / "logs" / "trades.csv"),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"EXIT01": "Exit One", "BUY002": "Buy Two"}),
                    env_file=env_path,
                    live_transport=transport,
                    rate_limiter=RecordingRateLimiter(),
                    live_order_safety_context=safety_context,
                )

            runtime.strategy = ExitThenBuyStrategy()
            runtime.start()
            runtime.run_cycle()

            self.assertEqual(["EXIT01", "BUY002"], [payload["PDNO"] for payload in transport.order_payloads])

    def test_build_live_broker_wires_reconciler_pending_store_manual_blocker_and_managed_ledger(self):
        class FakeLiveClient:
            def inquire_daily_orders(self, **_kwargs):
                return {"output1": []}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            journal_path = root / "logs" / "trades.csv"
            config = BotConfig(
                trading_mode="live",
                allow_live_trading=True,
                live_trading_enabled=True,
                journal_path=str(journal_path),
            )
            env_values = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "87654321",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "21",
            }

            broker = _build_live_broker(
                config,
                env_values,
                client=FakeLiveClient(),
                env_file=env_path,
            )
            broker.pending_order_store.ensure_ready()

            self.assertIsInstance(broker.fill_reconciler, KisLiveOrderReconciler)
            self.assertTrue(broker.config.allow_live_trading)
            self.assertTrue(broker.config.live_trading_enabled)
            self.assertIsInstance(broker.pending_order_store, JsonPendingLiveOrderStore)
            self.assertIsInstance(broker.manual_reconciliation_store, JsonManualReconciliationStore)
            self.assertIsInstance(broker.managed_position_ledger, JsonManagedLivePositionLedger)
            expected_pending_orders_path = _live_pending_orders_path(config, env_path, env_values)
            self.assertEqual(expected_pending_orders_path, broker.pending_order_store.path)
            self.assertEqual(managed_live_position_ledger_scope("87654321", "01"), broker.pending_order_store.scope)
            expected_manual_reconciliation_path = _live_manual_reconciliation_path(config, env_path, env_values)
            self.assertEqual(expected_manual_reconciliation_path, broker.manual_reconciliation_store.path)
            self.assertEqual(
                managed_live_position_ledger_scope("87654321", "01"),
                broker.manual_reconciliation_store.scope,
            )
            expected_managed_positions_path = _live_managed_positions_path(config, env_path, env_values)
            self.assertEqual(expected_managed_positions_path, broker.managed_position_ledger.path)
            self.assertEqual(managed_live_position_ledger_scope("87654321", "01"), broker.managed_position_ledger.scope)
            self.assertTrue(expected_pending_orders_path.name.startswith("pending_live_orders_"))
            self.assertTrue(expected_managed_positions_path.name.startswith("managed_live_positions_"))
            self.assertTrue(broker.pending_order_store.path.exists())
            self.assertEqual(
                journal_path.with_name("pending_live_orders.json"),
                _live_pending_orders_path(config, env_path),
            )
            self.assertEqual(
                expected_pending_orders_path,
                _live_pending_orders_path(config, env_path, env_values),
            )
            self.assertEqual(
                expected_manual_reconciliation_path,
                _live_manual_reconciliation_path(config, env_path, env_values),
            )
            self.assertEqual(
                expected_managed_positions_path,
                _live_managed_positions_path(config, env_path, env_values),
            )

    def test_restore_live_entry_counts_reconciles_unknown_day_before_risk_restore(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "managed-live-positions.json"
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

            class ReconcilingBroker:
                managed_position_ledger = ledger

                def __init__(self):
                    self.calls = 0

                def reconcile_managed_entry_counts(self):
                    self.calls += 1
                    ledger.replace_entry_counts_for_date(trading_day, {"005930": 2})
                    return True

            class Runtime:
                risk_manager = RecordingEntryCountRiskManager()

            broker = ReconcilingBroker()
            runtime = Runtime()

            _restore_live_entry_counts(runtime, broker)

            self.assertEqual(1, broker.calls)
            self.assertEqual({("005930", trading_day): 2}, runtime.risk_manager.restored)

    def test_build_live_broker_keeps_order_gate_closed_without_local_approval_flags(self):
        class FakeLiveClient:
            def inquire_daily_orders(self, **_kwargs):
                return {"output1": []}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            config = BotConfig(
                trading_mode="live",
                allow_live_trading=True,
                live_trading_enabled=True,
                journal_path=str(root / "logs" / "trades.csv"),
            )
            env_values = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "87654321",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "21",
            }

            broker = _build_live_broker(
                config,
                env_values,
                client=FakeLiveClient(),
                env_file=env_path,
            )

            self.assertFalse(broker.config.allow_live_trading)
            self.assertFalse(broker.config.live_trading_enabled)
            self.assertFalse(broker.session_approved())
            self.assertFalse(broker.risk_limits_ok())

    def test_build_live_broker_keeps_session_and_risk_gates_closed_with_local_order_approval(self):
        class FakeLiveClient:
            def inquire_daily_orders(self, **_kwargs):
                return {"output1": []}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            env_path = root / ".env"
            config = BotConfig(
                trading_mode="live",
                allow_live_trading=True,
                live_trading_enabled=True,
                journal_path=str(root / "logs" / "trades.csv"),
            )
            env_values = {
                "KIS_LIVE_APP_KEY": "live-app-key",
                "KIS_LIVE_APP_SECRET": "live-app-secret",
                "KIS_LIVE_ACCOUNT_NO": "87654321",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "21",
            }

            broker = _build_live_broker(
                config,
                env_values,
                client=FakeLiveClient(),
                env_file=env_path,
            )

            self.assertTrue(broker.config.allow_live_trading)
            self.assertTrue(broker.config.live_trading_enabled)
            self.assertFalse(broker.session_approved())
            self.assertFalse(broker.risk_limits_ok())
            self.assertFalse(broker.new_entries_allowed())

    def test_live_env_values_prefers_saved_env_file_over_stale_process_env(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=file-key",
                        "KIS_LIVE_APP_SECRET=file-secret",
                        "KIS_LIVE_ACCOUNT_NO=file-account",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "KIS_LIVE_APP_KEY": "stale-process-key",
                    "KIS_LIVE_APP_SECRET": "stale-process-secret",
                    "KIS_LIVE_ACCOUNT_NO": "stale-process-account",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "99",
                },
            ):
                values = _live_env_values(env_path)

        self.assertEqual("file-key", values["KIS_LIVE_APP_KEY"])
        self.assertEqual("file-secret", values["KIS_LIVE_APP_SECRET"])
        self.assertEqual("file-account", values["KIS_LIVE_ACCOUNT_NO"])
        self.assertEqual("01", values["KIS_LIVE_ACCOUNT_PRODUCT_CODE"])

    def test_live_env_values_does_not_fall_back_to_process_env_when_local_file_is_missing(self):
        with TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            with patch.dict(
                os.environ,
                {
                    "KIS_LIVE_APP_KEY": "process-key",
                    "KIS_LIVE_APP_SECRET": "process-secret",
                    "KIS_LIVE_ACCOUNT_NO": "12345678",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                    "STOCKBOT_ALLOW_LIVE_TRADING": "true",
                    "STOCKBOT_LIVE_TRADING_ENABLED": "true",
                    "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                    "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "78",
                },
            ):
                values = _live_env_values(env_path)

        self.assertEqual({}, values)

    def test_create_live_runtime_rejects_injected_live_broker_without_local_order_approval(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            env_path = Path(directory) / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    live_broker=PaperBroker(initial_cash=Decimal("1000000")),
                    env_file=env_path,
                )

    def test_create_live_runtime_requires_config_live_order_gate_before_broker_construction(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            broker = make_approved_live_broker(root)

            with self.assertRaisesRegex(ValueError, "live runtime requires trading_mode=live"):
                create_live_runtime(
                    config=BotConfig(
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    live_broker=broker,
                    env_file=env_path,
                )

    def test_create_live_runtime_rejects_non_live_broker_even_with_local_order_approval(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            env_path = Path(directory) / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "injected live broker must be"):
                create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    live_broker=PaperBroker(initial_cash=Decimal("1000000")),
                    env_file=env_path,
                )

    def test_create_live_runtime_rejects_injected_live_broker_with_unscoped_pending_store(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            broker = make_approved_live_broker(root)
            broker.pending_order_store = JsonPendingLiveOrderStore(root / "pending_live_orders.json")

            with self.assertRaisesRegex(ValueError, "pending order store scope mismatch"):
                create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    live_broker=broker,
                    env_file=env_path,
                )

    def test_create_live_runtime_rejects_injected_live_broker_without_managed_ledger(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path = root / "scanner.json"
            env_path = root / ".env"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 900000}],
                    }
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-app-key",
                        "KIS_LIVE_APP_SECRET=live-app-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        "STOCKBOT_ALLOW_LIVE_TRADING=true",
                        "STOCKBOT_LIVE_TRADING_ENABLED=true",
                        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            broker = make_approved_live_broker(root)
            broker.managed_position_ledger = None

            with self.assertRaisesRegex(ValueError, "managed position ledger"):
                create_live_runtime(
                    config=BotConfig(
                        trading_mode="live",
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        allow_live_trading=True,
                        live_trading_enabled=True,
                        journal_path=str(root / "logs" / "trades.csv"),
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                    live_broker=broker,
                    env_file=env_path,
                )

    def test_create_live_runtime_rejects_injected_live_broker_safety_component_subclasses(self):
        class PendingStoreSubclass(JsonPendingLiveOrderStore):
            pass

        class ManualStoreSubclass(JsonManualReconciliationStore):
            pass

        class ManagedLedgerSubclass(JsonManagedLivePositionLedger):
            pass

        class ReconcilerSubclass(KisLiveOrderReconciler):
            pass

        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            cases = [
                (
                    "pending",
                    lambda broker: setattr(
                        broker,
                        "pending_order_store",
                        PendingStoreSubclass(broker.pending_order_store.path, scope=broker.pending_order_store.scope),
                    ),
                    "pending order store",
                ),
                (
                    "manual",
                    lambda broker: setattr(
                        broker,
                        "manual_reconciliation_store",
                        ManualStoreSubclass(
                            broker.manual_reconciliation_store.path,
                            scope=broker.manual_reconciliation_store.scope,
                        ),
                    ),
                    "manual reconciliation store",
                ),
                (
                    "managed",
                    lambda broker: setattr(
                        broker,
                        "managed_position_ledger",
                        ManagedLedgerSubclass(
                            broker.managed_position_ledger.path,
                            scope=broker.managed_position_ledger.scope,
                        ),
                    ),
                    "managed position ledger",
                ),
                (
                    "reconciler",
                    lambda broker: setattr(broker, "fill_reconciler", ReconcilerSubclass(FakeLiveClient())),
                    "KIS live order reconciler",
                ),
            ]

            for name, mutate, expected_message in cases:
                with self.subTest(name=name):
                    broker = make_approved_live_broker(root)
                    mutate(broker)
                    with self.assertRaisesRegex(ValueError, expected_message):
                        create_runtime_with_injected_broker(root, broker, scanner_path, env_path)

    def test_create_live_runtime_rejects_injected_live_broker_with_mismatched_reconciler_client(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            scanner_path, env_path = write_approved_live_inputs(root)
            broker = make_approved_live_broker(root)
            broker.fill_reconciler = KisLiveOrderReconciler(FakeLiveClient())

            with self.assertRaisesRegex(ValueError, "reconciler client mismatch"):
                create_runtime_with_injected_broker(root, broker, scanner_path, env_path)

    def test_create_paper_runtime_wires_local_volume_priority_provider(self):
        runtime = create_paper_runtime(
            symbol_directory=SymbolDirectory({"LOW001": "Low", "HIGH01": "High"}),
            bars=[
                make_bar("LOW001", 0, 1000),
                make_bar("HIGH01", 0, 9000),
            ],
        )

        self.assertTrue(callable(runtime.symbol_priority_provider))
        self.assertIsNotNone(runtime.scanner_provider)
        self.assertGreater(runtime._symbol_priority("HIGH01"), runtime._symbol_priority("LOW001"))
        snapshot = runtime.scanner_provider.snapshot(["LOW001", "HIGH01"])
        self.assertEqual(["HIGH01", "LOW001"], snapshot.ordered_symbols(["LOW001", "HIGH01"]))

    def test_create_paper_runtime_uses_data_path_volume_for_priority_provider(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,111111,10000,10000,10000,10000,100,10000",
                        "2026-06-08T09:00:00,222222,10000,10000,10000,10000,50000,10000",
                    ]
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                symbol_directory=SymbolDirectory({"111111": "Low", "222222": "High"}),
                data_path=str(data_path),
            )

            self.assertEqual(100.0, runtime._symbol_priority("111111"))
            self.assertEqual(50000.0, runtime._symbol_priority("222222"))

    def test_create_paper_runtime_wires_cost_filter_to_strategy(self):
        runtime = create_paper_runtime(
            config=BotConfig(
                transaction_tax_pct=Decimal("0.002"),
                commission_pct=Decimal("0.00015"),
                slippage_pct=Decimal("0.001"),
                min_net_profit_pct=Decimal("0.0025"),
            ),
            symbol_directory=SymbolDirectory({"005930": "Samsung"}),
            bars=[make_bar("005930", 0, 10000)],
        )

        strategy_config = runtime.strategy.config

        self.assertEqual(Decimal("0.002"), strategy_config.transaction_tax_pct)
        self.assertEqual(Decimal("0.00015"), strategy_config.commission_pct)
        self.assertEqual(Decimal("0.001"), strategy_config.slippage_pct)
        self.assertEqual(Decimal("0.0025"), strategy_config.min_net_profit_pct)

    def test_create_paper_runtime_can_use_kis_price_data_with_paper_broker(self):
        class FakeKisProvider:
            data_source_label = "KIS VTS 현재가 / paper 체결"

            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, len(self.symbols), 10000)

            def priority(self, symbol):
                return 10000.0 if symbol == "000660" else 1000.0

        provider = FakeKisProvider()

        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930,000660",
                kis_market_data_scan_limit=1,
            ),
            symbol_directory=SymbolDirectory({"005930": "Samsung", "000660": "SK hynix"}),
            kis_bar_provider=provider,
        )

        self.assertIs(runtime.bar_provider, provider)
        self.assertIsInstance(runtime.broker, PaperBroker)
        self.assertEqual("KIS VTS 현재가 / paper 체결", runtime.data_source_label)
        self.assertEqual("kis-vts", runtime.data_source_kind)
        self.assertEqual(["005930", "000660"], runtime.symbols)
        self.assertEqual(1, runtime.scan_limit_per_cycle)
        self.assertIsNotNone(runtime.market_hours)
        self.assertGreater(runtime._symbol_priority("000660"), runtime._symbol_priority("005930"))

    def test_create_paper_runtime_rejects_live_trading_config(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
        )

        with self.assertRaisesRegex(ValueError, "paper runtime requires trading_mode=paper"):
            create_paper_runtime(config=config)

    def test_create_paper_runtime_wires_external_scanner_with_kis_final_quote_provider(self):
        class FakeKisProvider:
            data_source_label = "KIS final quote"

            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, len(self.symbols), 10000)

        provider = FakeKisProvider()
        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 900000, "priority": 900},
                            {"symbol": "BUY002", "price": "12000", "volume": 800000, "priority": 800},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    initial_cash=Decimal("1000000"),
                    max_positions=20,
                    scan_limit_per_cycle=100,
                    max_order_amount=Decimal("100000"),
                    kis_market_data_scan_limit=50,
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
                kis_bar_provider=provider,
            )
            snapshot_symbols = set(runtime.scanner_provider.snapshot(runtime.symbols).bars)

        self.assertEqual("external-scan-kis", runtime.data_source_kind)
        self.assertIs(runtime.final_quote_provider, provider)
        self.assertIsNot(runtime.bar_provider, provider)
        self.assertIsNotNone(runtime.scanner_provider)
        self.assertIsNone(runtime.max_bar_requests_per_cycle)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, runtime.max_final_quote_requests_per_cycle)
        self.assertEqual(20, runtime.settings.max_positions)
        self.assertEqual(20, runtime.risk_manager.config.max_positions)
        self.assertEqual(["BUY001", "BUY002"], runtime.symbols)
        self.assertIsNotNone(runtime.market_hours)
        self.assertEqual({"BUY001", "BUY002"}, snapshot_symbols)
        self.assertEqual([], provider.symbols)

    def test_data_source_switch_defaults_external_scan_to_json_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            (data_dir / "scanner_snapshot.json").write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 900000, "priority": 900},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.example.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "market_data_source: local",
                        "scanner_source: local",
                        "scanner_snapshot_path:",
                        "data_path: data/sample_bars.csv",
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime_for_data_source(
                "external-scan-kis",
                config_path=config_path,
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            )

        self.assertEqual("external-scan-kis", runtime.data_source_kind)
        self.assertIsNotNone(runtime.scanner_provider)
        self.assertEqual("json", runtime.scanner_provider.kind)
        self.assertEqual(["BUY001"], runtime.symbols)

    def test_data_source_switch_refreshes_stale_external_scan_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            scanner_path = data_dir / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "stale-file",
                        "generated_at": "2000-01-01T00:00:00+00:00",
                        "candidates": [
                            {"symbol": "OLD001", "price": "10000", "volume": 900000, "priority": 900},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.example.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "market_data_source: local",
                        "scanner_source: local",
                        "scanner_snapshot_path:",
                        "scanner_snapshot_max_age_seconds: 60",
                        "initial_cash: 100000",
                        "max_positions: 2",
                        "max_order_amount: 50000",
                        "max_position_amount: 50000",
                        "data_path: data/sample_bars.csv",
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def refresh_snapshot(output_path, options, **kwargs):
                refresh_calls.append(
                    (
                        Path(output_path),
                        options.max_price,
                        kwargs.get("minute_history_candidates"),
                        kwargs.get("minute_history_timeout"),
                    )
                )
                Path(output_path).write_text(
                    json.dumps(
                        {
                            "provider": "naver-mobile-auto",
                            "generated_at": "2099-01-01T00:00:00+00:00",
                            "candidates": [
                                {"symbol": "BUY001", "price": "10000", "volume": 900000, "priority": 900},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return 1

            with patch("stockbot.runtime_factory.collect_naver_market_scanner_snapshot", side_effect=refresh_snapshot):
                runtime = create_paper_runtime_for_data_source(
                    "external-scan-kis",
                    config_path=config_path,
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One", "OLD001": "Old One"}),
                )

        self.assertEqual([(scanner_path, Decimal("50000"), 128, 2.0)], refresh_calls)
        self.assertEqual("external-scan-kis", runtime.data_source_kind)
        self.assertEqual(["BUY001"], runtime.symbols)

    def test_external_scan_kis_requires_configured_external_scanner_without_local_fallback(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,HIGH01,500000,500000,500000,500000,999999,500000,499900,500100",
                        "2026-06-08T09:00:00,LOW001,10000,10000,10000,10000,800000,10000,9990,10010",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "external-scan-kis requires scanner_source=json"):
                create_paper_runtime(
                    config=BotConfig(
                        market_data_source="external-scan-kis",
                        trading_mode="paper",
                        initial_cash=Decimal("1000000"),
                        max_positions=20,
                        scan_limit_per_cycle=100,
                        max_order_amount=Decimal("50000"),
                        data_path=str(data_path),
                    ),
                    symbol_directory=SymbolDirectory({"HIGH01": "High Price", "LOW001": "Low Price"}),
                    kis_bar_provider=FakeKisProvider(),
                )

    def test_external_scan_kis_rejects_local_bars_shortcut(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100}],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "external-scan-kis does not accept local bars fallback"):
                create_paper_runtime(
                    config=BotConfig(
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        trading_mode="paper",
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    bars=[make_bar("BUY001", 0, 1000)],
                    kis_bar_provider=FakeKisProvider(),
                )

    def test_external_scan_kis_runtime_uses_json_scanner_before_kis_final_quote(self):
        class FakeKisProvider:
            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, 0, 10000)

        provider = FakeKisProvider()
        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {
                                "symbol": "HIGH01",
                                "price": "500000",
                                "volume": 999999,
                                "priority": 999,
                            },
                            {
                                "symbol": "BUY002",
                                "price": "12000",
                                "volume": 800000,
                                "priority": 800,
                            },
                            {
                                "symbol": "BUY001",
                                "price": "10000",
                                "volume": 700000,
                                "priority": 700,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    initial_cash=Decimal("100000"),
                    max_positions=2,
                    scan_limit_per_cycle=100,
                    max_order_amount=Decimal("50000"),
                    max_position_amount=Decimal("50000"),
                    data_path=str(Path(directory) / "unused.csv"),
                ),
                symbol_directory=SymbolDirectory({"HIGH01": "High", "BUY001": "Buy One", "BUY002": "Buy Two"}),
                kis_bar_provider=provider,
            )

        self.assertEqual(["HIGH01", "BUY002", "BUY001"], runtime.symbols)
        self.assertEqual("external-scan-kis", runtime.data_source_kind)
        self.assertIs(runtime.final_quote_provider, provider)
        self.assertIsNot(runtime.bar_provider, provider)
        self.assertIsNotNone(runtime.scanner_provider)
        self.assertEqual("json", runtime.scanner_provider.kind)
        self.assertEqual([], provider.symbols)

    def test_external_scan_kis_runtime_refreshes_stale_json_scanner_during_build(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            data_dir.mkdir()
            scanner_path = data_dir / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "stale-file",
                        "generated_at": "2026-06-19T09:00:00+09:00",
                        "candidates": [
                            {"symbol": "OLD001", "price": "10000", "volume": 1000, "priority": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config_path = root / "config.example.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "market_data_source: local",
                        "scanner_source: local",
                        "scanner_snapshot_path:",
                        "scanner_snapshot_max_age_seconds: 60",
                        "initial_cash: 100000",
                        "max_positions: 2",
                        "max_order_amount: 50000",
                        "max_position_amount: 50000",
                        "data_path: data/sample_bars.csv",
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def refresh_snapshot(output_path, options, **kwargs):
                refresh_calls.append(
                    (
                        Path(output_path),
                        options.max_price,
                        kwargs.get("minute_history_candidates"),
                        kwargs.get("minute_history_timeout"),
                    )
                )
                Path(output_path).write_text(
                    json.dumps(
                        {
                            "provider": "fresh-file",
                            "candidates": [
                                {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                return 1

            with patch("stockbot.runtime_factory.collect_naver_market_scanner_snapshot", side_effect=refresh_snapshot) as collector:
                runtime = create_paper_runtime_for_data_source(
                    "external-scan-kis",
                    config_path=config_path,
                    symbol_directory=SymbolDirectory({"OLD001": "Old One", "BUY001": "Buy One"}),
                )
                scanner_path.write_text(
                    json.dumps(
                        {
                            "provider": "stale-again",
                            "generated_at": "2000-01-01T00:00:00+09:00",
                            "candidates": [
                                {"symbol": "OLD001", "price": "10000", "volume": 1000, "priority": 1},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                ranked = runtime.scanner_provider.rank_symbols([])

        self.assertEqual(
            [
                (scanner_path, Decimal("50000"), 128, 2.0),
                (scanner_path, Decimal("50000"), 128, 2.0),
            ],
            refresh_calls,
        )
        self.assertEqual(2, collector.call_count)
        self.assertEqual(["BUY001"], runtime.symbols)
        self.assertEqual(["BUY001"], ranked)

    def test_external_scan_kis_json_scanner_excludes_candidates_without_current_price(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {"symbol": "NOPRCE", "volume": 999999, "priority": 999},
                            {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    initial_cash=Decimal("100000"),
                    max_positions=2,
                    scan_limit_per_cycle=100,
                    max_order_amount=Decimal("50000"),
                    max_position_amount=Decimal("50000"),
                ),
                symbol_directory=SymbolDirectory({"NOPRCE": "No Price", "BUY001": "Buy One"}),
                kis_bar_provider=FakeKisProvider(),
            )

        self.assertEqual(["BUY001"], runtime.symbols)

    def test_external_scan_kis_json_scanner_keeps_affordable_later_candidates(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {"symbol": "HIGH01", "price": "400000", "volume": 999999, "priority": 999},
                            {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    initial_cash=Decimal("100000"),
                    max_positions=10,
                    scan_limit_per_cycle=100,
                    max_position_amount=Decimal("50000"),
                ),
                symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY001": "Buy One"}),
                kis_bar_provider=FakeKisProvider(),
            )

        self.assertEqual(["HIGH01", "BUY001"], runtime.symbols)

    def test_external_scan_kis_keeps_entry_gates_without_capping_positions(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100}],
                    }
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    max_positions=20,
                    min_momentum_pct=Decimal("0.01"),
                    min_signal_confidence=Decimal("0.70"),
                    min_volume_ratio=Decimal("2"),
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                kis_bar_provider=FakeKisProvider(),
            )

        self.assertEqual(20, runtime.settings.max_positions)
        self.assertEqual(Decimal("0.01"), runtime.strategy.config.min_momentum_pct)
        self.assertEqual(Decimal("-0.01"), runtime.strategy.config.min_short_momentum_pct)
        self.assertEqual(Decimal("0.70"), runtime.strategy.config.min_signal_confidence)
        self.assertEqual(Decimal("2"), runtime.strategy.config.min_volume_ratio)
        self.assertEqual(Decimal("0.005"), runtime.strategy.config.min_trend_pct)
        self.assertTrue(runtime.strategy.config.require_vwap_alignment)

    def test_external_scan_kis_json_scanner_does_not_fallback_to_local_bars(self):
        class FakeKisProvider:
            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, 0, 10000)

        provider = FakeKisProvider()
        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "kiwoom-file",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,BUY001,10000,10000,10000,10000,900000,10000,9990,10010",
                    ]
                ),
                encoding="utf-8",
            )

            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="json",
                    scanner_snapshot_path=str(scanner_path),
                    trading_mode="paper",
                    data_path=str(data_path),
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                kis_bar_provider=provider,
            )

        runtime._scanner_snapshot = ScannerSnapshot()

        self.assertIsNone(runtime._bar_for("BUY001"))
        self.assertEqual([], provider.symbols)

    def test_external_scan_kis_rejects_unimplemented_scanner_source_without_injected_provider(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with self.assertRaisesRegex(ValueError, "scanner_source=kiwoom requires an injected ScannerProvider"):
            create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="kiwoom",
                    trading_mode="paper",
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                kis_bar_provider=FakeKisProvider(),
            )

    def test_external_scan_kis_rejects_missing_json_scanner_without_local_fallback(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing-scanner.json"

            with self.assertRaisesRegex(ValueError, "scanner_snapshot.json 파일이 없습니다"):
                create_paper_runtime(
                    config=BotConfig(
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(missing_path),
                        trading_mode="paper",
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                )

    def test_external_scan_kis_rejects_stale_json_scanner_without_local_fallback(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        with TemporaryDirectory() as directory:
            scanner_path = Path(directory) / "scanner.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "generated_at": "2000-01-01T00:00:00+09:00",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 900000, "priority": 900},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "stale scanner snapshot"):
                create_paper_runtime(
                    config=BotConfig(
                        market_data_source="external-scan-kis",
                        scanner_source="json",
                        scanner_snapshot_path=str(scanner_path),
                        scanner_snapshot_max_age_seconds=60,
                        trading_mode="paper",
                    ),
                    symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                    kis_bar_provider=FakeKisProvider(),
                )

    def test_external_scan_kis_redacts_injected_scanner_error_details(self):
        class FakeKisProvider:
            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        class LeakyScanner:
            def rank_symbols(self, _symbols):
                raise RuntimeError("Authorization: Bearer secret-token-123 account 12345678")

        with self.assertRaises(ValueError) as raised:
            create_paper_runtime(
                config=BotConfig(
                    market_data_source="external-scan-kis",
                    scanner_source="kiwoom",
                    trading_mode="paper",
                ),
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                kis_bar_provider=FakeKisProvider(),
                scanner_provider=LeakyScanner(),
            )

        message = str(raised.exception)
        self.assertIn("configured scanner_source is unavailable", message)
        self.assertNotIn("Bearer", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("12345678", message)
        self.assertNotIn("account", message.lower())

    def test_external_scan_kis_accepts_injected_scanner_provider(self):
        class FakeKisProvider:
            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, 0, 10000)

        class FakeScannerProvider:
            label = "kiwoom fake"
            kind = "kiwoom"

            def rank_symbols(self, symbols):
                requested = list(symbols) or ["BUY001"]
                return requested

            def snapshot(self, symbols):
                bars = {symbol: make_bar(symbol, 0, 10000) for symbol in symbols}
                return ScannerSnapshot(
                    bars=bars,
                    candidates=tuple(ScannerCandidate(symbol=symbol, priority=100.0) for symbol in symbols),
                )

        kis_provider = FakeKisProvider()
        scanner_provider = FakeScannerProvider()

        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="external-scan-kis",
                trading_mode="paper",
                scan_limit_per_cycle=10,
            ),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            kis_bar_provider=kis_provider,
            scanner_provider=scanner_provider,
        )

        self.assertIs(runtime.scanner_provider, scanner_provider)
        self.assertIs(runtime.final_quote_provider, kis_provider)
        self.assertEqual([], kis_provider.symbols)

    def test_external_scan_kis_injected_scanner_does_not_snapshot_full_universe_at_build(self):
        class FakeKisProvider:
            def __init__(self):
                self.symbols = []

            def __call__(self, symbol):
                self.symbols.append(symbol)
                return make_bar(symbol, 0, 10000)

        class LimitedSnapshotScannerProvider:
            label = "kiwoom limited"
            kind = "kiwoom"

            def __init__(self):
                self.snapshot_calls = []

            def rank_symbols(self, symbols):
                return [f"BUY{index:03d}" for index in range(10)]

            def snapshot(self, symbols):
                requested = list(symbols)
                self.snapshot_calls.append(requested)
                if len(requested) > 2:
                    raise AssertionError("factory should not snapshot the full scanner universe")
                return ScannerSnapshot(
                    bars={symbol: make_bar(symbol, 0, 10000) for symbol in requested},
                    candidates=tuple(ScannerCandidate(symbol=symbol, priority=100.0) for symbol in requested),
                )

        scanner_provider = LimitedSnapshotScannerProvider()

        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="external-scan-kis",
                trading_mode="paper",
                scan_limit_per_cycle=2,
                max_order_amount=Decimal("50000"),
                max_position_amount=Decimal("50000"),
            ),
            symbol_directory=SymbolDirectory({}),
            kis_bar_provider=FakeKisProvider(),
            scanner_provider=scanner_provider,
        )

        self.assertEqual([f"BUY{index:03d}" for index in range(10)], runtime.symbols)
        self.assertEqual([], scanner_provider.snapshot_calls)

    def test_create_paper_runtime_caps_kis_price_data_scan_and_positions_for_rehearsal_safety(self):
        class FakeKisProvider:
            data_source_label = "KIS VTS 현재가 / paper 체결"

            def __call__(self, symbol):
                return make_bar(symbol, 0, 10000)

        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930,000660,035420,051910,005380,000270,068270",
                kis_market_data_scan_limit=50,
                max_positions=0,
            ),
            symbol_directory=SymbolDirectory({}),
            kis_bar_provider=FakeKisProvider(),
        )

        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, runtime.scan_limit_per_cycle)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, runtime.max_bar_requests_per_cycle)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, runtime.settings.max_positions)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, runtime.risk_manager.config.max_positions)

    def test_create_paper_runtime_relaxes_kis_rehearsal_entry_gates_without_affecting_cost_filter(self):
        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930,000660",
                kis_market_data_scan_limit=2,
                min_momentum_pct=Decimal("0.01"),
                min_volume_ratio=Decimal("2"),
                min_net_profit_pct=Decimal("0.002"),
            ),
            symbol_directory=SymbolDirectory({}),
            kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
        )

        strategy_config = runtime.strategy.config

        self.assertEqual(Decimal("0"), strategy_config.min_momentum_pct)
        self.assertEqual(Decimal("0"), strategy_config.min_short_momentum_pct)
        self.assertEqual(Decimal("0.25"), strategy_config.min_signal_confidence)
        self.assertEqual(Decimal("0"), strategy_config.min_trend_pct)
        self.assertEqual(Decimal("0"), strategy_config.min_volume_ratio)
        self.assertEqual(Decimal("0.002"), strategy_config.min_net_profit_pct)
        self.assertFalse(strategy_config.require_vwap_alignment)

    def test_create_paper_runtime_prewarms_kis_strategy_from_local_history(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap",
                        "2026-06-08T09:00:00,005930,100,100,100,100,1000,100",
                        "2026-06-08T09:01:00,005930,101,101,101,101,1000,101",
                        "2026-06-08T09:02:00,005930,102,102,102,102,1000,102",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="kis-vts",
                    kis_market_data_symbols="005930",
                    kis_market_data_scan_limit=1,
                    data_path=str(data_path),
                    min_volume_ratio=Decimal("1"),
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                ),
                symbol_directory=SymbolDirectory({"005930": "Samsung"}),
                kis_bar_provider=lambda symbol: MarketBar(
                    symbol=symbol,
                    timestamp=datetime(2026, 6, 8, 9, 3),
                    open=Decimal("104"),
                    high=Decimal("104"),
                    low=Decimal("104"),
                    close=Decimal("104"),
                    volume=3000,
                    vwap=Decimal("103"),
                    bid=Decimal("104"),
                    ask=Decimal("104"),
                ),
            )

        self.assertEqual("kis-vts", runtime.data_source_kind)
        self.assertEqual(3, runtime._successful_bar_samples["005930"])
        signals = runtime.strategy.on_bar(runtime.bar_provider("005930"), runtime.broker.snapshot())
        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)

    def test_create_paper_runtime_respects_smaller_explicit_kis_position_cap(self):
        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930,000660,035420",
                kis_market_data_scan_limit=3,
                max_positions=2,
            ),
            symbol_directory=SymbolDirectory({}),
            kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
        )

        self.assertEqual(3, runtime.scan_limit_per_cycle)
        self.assertEqual(2, runtime.settings.max_positions)
        self.assertEqual(2, runtime.risk_manager.config.max_positions)

    def test_create_paper_runtime_keeps_default_kis_fallback_symbols_after_affordable_local_history(self):
        runtime = create_paper_runtime(
            config=BotConfig(market_data_source="kis-vts"),
            symbol_directory=SymbolDirectory({}),
            kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
        )

        self.assertEqual(["000660", "005930", "035420"], runtime.symbols[:3])
        self.assertIn("005930", runtime.symbols)
        self.assertIn("035720", runtime.symbols)
        self.assertIn("051910", runtime.symbols)
        self.assertGreaterEqual(len(runtime.symbols), KIS_INTRADAY_REHEARSAL_SCAN_LIMIT)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, runtime.scan_limit_per_cycle)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, runtime.settings.max_positions)

    def test_create_paper_runtime_expands_kis_candidates_from_affordable_local_history(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,005930,400000,400000,400000,400000,100000,400000,399500,400500",
                        "2026-06-08T09:00:00,BUY001,10000,10000,10000,10000,900000,10000,9990,10010",
                        "2026-06-08T09:00:00,BUY002,12000,12000,12000,12000,800000,12000,11990,12010",
                        "2026-06-08T09:01:00,005930,401000,401000,401000,401000,100000,401000,400500,401500",
                        "2026-06-08T09:01:00,BUY001,10100,10100,10100,10100,900000,10100,10090,10110",
                        "2026-06-08T09:01:00,BUY002,12100,12100,12100,12100,800000,12100,12090,12110",
                        "2026-06-08T09:02:00,005930,402000,402000,402000,402000,100000,402000,401500,402500",
                        "2026-06-08T09:02:00,BUY001,10200,10200,10200,10200,900000,10200,10190,10210",
                        "2026-06-08T09:02:00,BUY002,12200,12200,12200,12200,800000,12200,12190,12210",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="kis-vts",
                    kis_market_data_scan_limit=3,
                    data_path=str(data_path),
                ),
                symbol_directory=SymbolDirectory({"005930": "High", "BUY001": "Buy One", "BUY002": "Buy Two"}),
                kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
            )

        self.assertIn("BUY001", runtime.symbols)
        self.assertIn("BUY002", runtime.symbols)
        self.assertNotIn("005930", runtime.symbols)
        self.assertEqual(["BUY001", "BUY002"], runtime._entry_scan_order(runtime.symbols)[:2])
        self.assertEqual(3, runtime._successful_bar_samples["BUY001"])
        self.assertEqual(3, runtime._successful_bar_samples["BUY002"])

    def test_create_paper_runtime_filters_kis_candidates_using_position_risk_cap(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,BIG120,120000,120000,120000,120000,900000,120000,119900,120100",
                        "2026-06-08T09:00:00,BUY090,80000,80000,80000,80000,800000,80000,79900,80100",
                        "2026-06-08T09:00:00,HIGH18,180000,180000,180000,180000,700000,180000,179900,180100",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="kis-vts",
                    kis_market_data_scan_limit=2,
                    max_positions=2,
                    initial_cash=Decimal("250000"),
                    data_path=str(data_path),
                    max_order_amount=Decimal("200000"),
                    max_position_amount=Decimal("125000"),
                ),
                symbol_directory=SymbolDirectory({"BIG120": "Big", "BUY090": "Buy", "HIGH18": "High"}),
                kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
            )

        self.assertIn("BIG120", runtime.symbols)
        self.assertIn("BUY090", runtime.symbols)
        self.assertNotIn("HIGH18", runtime.symbols)
        self.assertEqual(["BIG120", "BUY090"], runtime._entry_scan_order(runtime.symbols)[:2])

    def test_create_paper_runtime_uses_position_risk_cap_when_positions_are_unlimited(self):
        with TemporaryDirectory() as directory:
            data_path = Path(directory) / "bars.csv"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,BUY090,60000,60000,60000,60000,800000,60000,59900,60100",
                        "2026-06-08T09:00:00,HIGH18,180000,180000,180000,180000,700000,180000,179900,180100",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = create_paper_runtime(
                config=BotConfig(
                    market_data_source="kis-vts",
                    kis_market_data_scan_limit=2,
                    max_positions=0,
                    initial_cash=Decimal("1000000"),
                    data_path=str(data_path),
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                ),
                symbol_directory=SymbolDirectory({"BUY090": "Buy", "HIGH18": "High"}),
                kis_bar_provider=lambda symbol: make_bar(symbol, 0, 10000),
            )

        self.assertIn("BUY090", runtime.symbols)
        self.assertNotIn("HIGH18", runtime.symbols)

    def test_external_scan_history_budget_ignores_legacy_policy_and_scan_slots(self):
        budget = _history_entry_budget_from_config(
            BotConfig(
                market_data_source="external-scan-kis",
                initial_cash=Decimal("1000000"),
                strategy_profile="conservative",
                order_cash_amount=Decimal("1"),
                cash_allocation_pct=Decimal("0.70"),
                scan_limit_per_cycle=20,
                kis_market_data_scan_limit=2,
                max_positions=0,
                max_position_amount=Decimal("300000"),
            )
        )

        self.assertEqual(Decimal("300000"), budget)

    def test_create_paper_runtime_labels_local_data_source_kind(self):
        runtime = create_paper_runtime(
            symbol_directory=SymbolDirectory({"005930": "Samsung"}),
            bars=[make_bar("005930", 0, 10000)],
        )

        self.assertEqual("local", runtime.data_source_kind)

    def test_create_paper_runtime_rejects_kis_price_data_without_market_hours_gate(self):
        with self.assertRaisesRegex(ValueError, "KIS market data source requires enforce_market_hours=true"):
            create_paper_runtime(
                config=BotConfig(
                    market_data_source="kis-vts",
                    enforce_market_hours=False,
                ),
                symbol_directory=SymbolDirectory({"005930": "Samsung"}),
                kis_bar_provider=lambda _symbol: make_bar("005930", 0, 10000),
            )

    def test_create_paper_runtime_adds_default_rate_limiter_to_kis_price_provider(self):
        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930",
                kis_market_data_scan_limit=1,
            ),
            symbol_directory=SymbolDirectory({"005930": "Samsung"}),
        )

        self.assertIsNone(runtime.rate_limiter)
        self.assertIsNotNone(getattr(runtime.bar_provider, "rate_limiter", None))

    def test_create_paper_runtime_uses_patient_kis_quote_timeout_for_intraday_rehearsal(self):
        runtime = create_paper_runtime(
            config=BotConfig(
                market_data_source="kis-vts",
                kis_market_data_symbols="005930",
                kis_market_data_scan_limit=1,
            ),
            symbol_directory=SymbolDirectory({"005930": "Samsung"}),
        )

        self.assertGreaterEqual(getattr(runtime.bar_provider, "timeout", 0), 20.0)
        self.assertLessEqual(getattr(runtime.bar_provider, "timeout", 99), 25.0)
        self.assertFalse(getattr(runtime.bar_provider, "retry_quote_timeouts", True))

if __name__ == "__main__":
    unittest.main()
