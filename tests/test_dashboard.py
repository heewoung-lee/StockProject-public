import os
import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.dashboard import (
    DashboardController,
    DashboardServices,
    build_initial_dashboard_state,
    format_krw,
    mask_account_display,
)
from stockbot.config import BotConfig, KIS_INTRADAY_REHEARSAL_MAX_POSITIONS
from stockbot.kis import KisApiError
from stockbot.live_audit import JsonlLiveAuditLog
from stockbot.live_broker import LiveBroker
from stockbot.live_order_state import (
    JsonManualReconciliationStore,
    JsonPendingLiveOrderStore,
    ManualReconciliationBlocker,
    PendingLiveOrder,
)
from stockbot.live_order_safety_context import LiveOrderSafetyContext
from stockbot.live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from stockbot.live_reconciliation import KisLiveOrderReconciler
from stockbot.live_safety import (
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
    LIVE_ALLOW_ENV_KEY,
    LIVE_CONFIRMATION_ENV_KEY,
    LIVE_CONFIRMATION_PHRASE,
    LIVE_ENABLED_ENV_KEY,
)
from stockbot.market_hours import KST, MarketSessionStatus
from stockbot.metrics import PaperPerformanceMetrics
from stockbot.models import Position
from stockbot.rate_limit import RateLimitDecision
from stockbot.runtime import CustomStrategySettings, RuntimeEvent, RuntimeStatus


@dataclass
class FakeSnapshot:
    positions: dict[str, Position]
    cash: Decimal = Decimal("1000000")
    realized_pnl_today: Decimal = Decimal("0")

    @property
    def equity(self):
        return self.cash + sum(position.market_value for position in self.positions.values())

    @property
    def short_proceeds(self):
        return sum(
            position.avg_price * position.quantity
            for position in self.positions.values()
            if position.side == "SHORT"
        )

    @property
    def free_cash(self):
        return self.cash - (self.short_proceeds * Decimal("2"))

    @property
    def buying_power(self):
        capped = min(self.free_cash, self.equity)
        return max(Decimal("0"), capped)


class FakeBroker:
    def __init__(self, positions=None, cash=Decimal("1000000")):
        self._positions = positions or {}
        self._cash = Decimal(str(cash))
        self.snapshot_calls = 0

    def snapshot(self):
        self.snapshot_calls += 1
        return FakeSnapshot(cash=self._cash, positions=self._positions)


class FakeApprovedLiveBroker(FakeBroker):
    def session_approved(self):
        return True

    def risk_limits_ok(self):
        return True


def write_live_order_approval_env(path: Path, account_no: str = "12345678") -> None:
    path.write_text(
        "\n".join(
            [
                "KIS_LIVE_APP_KEY=live-key",
                "KIS_LIVE_APP_SECRET=live-secret",
                f"KIS_LIVE_ACCOUNT_NO={account_no}",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                "STOCKBOT_ALLOW_LIVE_TRADING=true",
                "STOCKBOT_LIVE_TRADING_ENABLED=true",
                f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
                f"STOCKBOT_LIVE_ACCOUNT_CONFIRMATION={account_no[-2:]}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_live_credentials_env(path: Path, account_no: str = "12345678") -> None:
    path.write_text(
        "\n".join(
            [
                "KIS_LIVE_APP_KEY=live-key",
                "KIS_LIVE_APP_SECRET=live-secret",
                f"KIS_LIVE_ACCOUNT_NO={account_no}",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def write_quoted_live_order_approval_env(path: Path, account_no: str = "12345678") -> None:
    path.write_text(
        "\n".join(
            [
                'KIS_LIVE_APP_KEY="live-key"',
                'KIS_LIVE_APP_SECRET="live-secret"',
                f'KIS_LIVE_ACCOUNT_NO="{account_no}"',
                'KIS_LIVE_ACCOUNT_PRODUCT_CODE="01"',
                'STOCKBOT_ALLOW_LIVE_TRADING="true"',
                'STOCKBOT_LIVE_TRADING_ENABLED="true"',
                f'STOCKBOT_LIVE_TRADING_CONFIRM="{LIVE_CONFIRMATION_PHRASE}"',
                f'STOCKBOT_LIVE_ACCOUNT_CONFIRMATION="{account_no[-2:]}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def successful_live_readiness(**_kwargs):
    return {
        "ready": True,
        "blockers": [],
        "manual_reconciliation_cleared": False,
        "scanner_snapshot_refreshed": False,
        "live_order_enabled": False,
        "note": "static readiness passed",
    }


class FakeLiveClient:
    def __init__(self, snapshot=None, daily_orders=None):
        self._snapshot = snapshot or FakeSnapshot(positions={})
        self._daily_orders = daily_orders or []
        self.snapshot_calls = 0

    def account_snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        return self._snapshot

    def inquire_daily_orders(self, **_kwargs):
        return {"output1": list(self._daily_orders)}


class SnapshotFailingLiveClient(FakeLiveClient):
    def account_snapshot(self, *, timestamp=None):
        raise RuntimeError("KIS HTTP 500 EGW00215 live-secret 12345678")


class SecondSnapshotFailingLiveClient(FakeLiveClient):
    def __init__(self, snapshot=None, daily_orders=None):
        super().__init__(snapshot=snapshot, daily_orders=daily_orders)

    def account_snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        if self.snapshot_calls > 1:
            raise RuntimeError("KIS HTTP 500 EGW00215 live-secret 12345678")
        return self._snapshot


def make_approved_live_broker(
    root: Path,
    *,
    account_no: str = "12345678",
    include_managed_ledger: bool = True,
    include_manual_reconciliation_store: bool = True,
    safety_context: LiveOrderSafetyContext | None = None,
    client=None,
) -> LiveBroker:
    product_code = "01"
    scope = managed_live_position_ledger_scope(account_no, product_code)
    env = {
        "KIS_LIVE_APP_KEY": "live-key",
        "KIS_LIVE_APP_SECRET": "live-secret",
        "KIS_LIVE_ACCOUNT_NO": account_no,
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": product_code,
        LIVE_ALLOW_ENV_KEY: "true",
        LIVE_ENABLED_ENV_KEY: "true",
        LIVE_CONFIRMATION_ENV_KEY: LIVE_CONFIRMATION_PHRASE,
        LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: account_no[-2:],
    }
    client = client or FakeLiveClient()
    managed_ledger = None
    if include_managed_ledger:
        managed_ledger = JsonManagedLivePositionLedger(
            root / f"managed_live_positions_{scope}.json",
            scope=scope,
        )
    manual_store = None
    if include_manual_reconciliation_store:
        manual_store = JsonManualReconciliationStore(
            root / f"live_manual_reconciliation_required_{scope}.json",
            scope=scope,
        )
    return LiveBroker(
        client=client,
        config=BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            journal_path=str(root / "logs" / "trades.csv"),
        ),
        env=env,
        audit_log=JsonlLiveAuditLog(root / "logs" / "live_audit.jsonl", redact_values=env.values()),
        market_is_open=True,
        session_approved=True if safety_context is None else lambda: bool(safety_context.session_approved),
        account_confirmation=account_no[-2:],
        expected_account_suffix=account_no[-2:],
        fill_reconciler=KisLiveOrderReconciler(client),
        pending_order_store=JsonPendingLiveOrderStore(
            root / f"pending_live_orders_{scope}.json",
            scope=scope,
        ),
        manual_reconciliation_store=manual_store,
        managed_position_ledger=managed_ledger,
        risk_limits_ok=True if safety_context is None else lambda: bool(safety_context.risk_limits_ok),
        new_entries_allowed=True if safety_context is None else lambda: bool(safety_context.new_entries_allowed),
    )


class FakeMarketHours:
    def __init__(self, status: MarketSessionStatus):
        self._status = status
        self.calls = 0

    def status(self):
        self.calls += 1
        return self._status


class FakeRuntime:
    def __init__(
        self,
        events=None,
        positions=None,
        market_hours=None,
        cash=Decimal("1000000"),
        data_source_kind="local",
        data_source_label="샘플 CSV",
    ):
        self.events = events or []
        self.broker = FakeBroker(positions, cash=cash)
        self.started = False
        self.paused = False
        self.cycles = 0
        self.status = RuntimeStatus()
        self.market_hours = market_hours
        self.data_source_kind = data_source_kind
        self.data_source_label = data_source_label
        self.applied_settings = []
        self.performance_metrics = PaperPerformanceMetrics(
            cash=Decimal("1000000"),
            equity=Decimal("1000000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            total_pnl=Decimal("0"),
            open_positions=0,
            long_positions=0,
            short_positions=0,
            filled_trades=0,
            rejected_trades=0,
            winning_exits=0,
            losing_exits=0,
            win_rate_pct=Decimal("0"),
        )

    def start(self):
        self.started = True
        if self.status.label == "정지":
            self.status = RuntimeStatus(label="실행 중", running=True)
        return RuntimeEvent.system("paper runtime started", timestamp=datetime(2026, 6, 11, 9, 0))

    def pause(self):
        self.paused = True
        self.status = RuntimeStatus(label="일시정지", running=False)
        return RuntimeEvent.system("paper runtime paused", timestamp=datetime(2026, 6, 11, 9, 1))

    def run_cycle(self):
        self.cycles += 1
        return list(self.events)

    def apply_strategy_settings(self, **kwargs):
        self.applied_settings.append(kwargs)
        return RuntimeEvent.system(f"settings applied {kwargs.get('profile_label', '')}", timestamp=datetime(2026, 6, 11, 9, 5))


class FailingApplyRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.fail_next_apply = True

    def apply_strategy_settings(self, **kwargs):
        if self.fail_next_apply:
            self.fail_next_apply = False
            raise RuntimeError("apply failed")
        return super().apply_strategy_settings(**kwargs)


class ClearingRuntime(FakeRuntime):
    def run_cycle(self):
        self.cycles += 1
        self.broker._positions = {}
        return [
            RuntimeEvent.trade(
                symbol="005930",
                company_name="삼성전자",
                side="SELL",
                quantity=1,
                price=Decimal("71000"),
                reason="cleanup exit",
                result="filled",
                timestamp=datetime(2026, 6, 11, 9, 2),
            )
        ]


class ClearingPauseFailingRuntime(ClearingRuntime):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pause_attempted = False

    def pause(self):
        self.pause_attempted = True
        raise RuntimeError("pause failed")


class OrderedRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.order = []

    def run_cycle(self):
        self.order.append("cycle")
        return super().run_cycle()

    def apply_strategy_settings(self, **kwargs):
        self.order.append("apply")
        return super().apply_strategy_settings(**kwargs)


class FakeKisRateLimiter:
    def __init__(self, decision):
        self.decision = decision
        self.checked = []
        self.recorded = []
        self.token_issues = 0

    def allow_request(self, kind="query"):
        self.checked.append(kind)
        return self.decision

    def record_request(self, kind="query"):
        self.recorded.append(kind)

    def record_token_issue(self):
        self.token_issues += 1


class PauseFailingRuntime(FakeRuntime):
    def pause(self):
        raise RuntimeError("pause failed")


class BlockingRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.in_cycle = Event()
        self.release_cycle = Event()

    def run_cycle(self):
        self.in_cycle.set()
        self.release_cycle.wait(2)
        self.cycles += 1
        return []


class FailingOnceBlockingRuntime(BlockingRuntime):
    def __init__(self):
        super().__init__()
        self.should_fail = True

    def run_cycle(self):
        self.in_cycle.set()
        self.release_cycle.wait(2)
        if self.should_fail:
            self.should_fail = False
            raise RuntimeError("cycle failed")
        self.cycles += 1
        return []


class DashboardStateTest(unittest.TestCase):
    def test_initial_state_exposes_redesigned_dashboard_contract(self):
        state = build_initial_dashboard_state()

        self.assertFalse(hasattr(state, "market_signal"))
        self.assertFalse(hasattr(state, "activity_log"))
        self.assertTrue(hasattr(state, "custom_settings"))
        self.assertEqual((), state.active_positions)
        self.assertEqual("", state.selected_position.symbol)
        self.assertEqual((), state.trade_log)
        self.assertGreaterEqual(len(state.system_log), 1)
        self.assertIn(("매수 방식", "현금 기준 자동 계산"), state.custom_settings)
        self.assertIn(("현금 사용 비율", "100%"), state.custom_settings)
        self.assertIn(("종목 안전 상한", "300,000원"), state.custom_settings)
        self.assertNotIn(("종목 최대 노출", "30%"), state.custom_settings)
        self.assertIn(("최대 보유 종목", "제한 없음"), state.custom_settings)
        self.assertNotIn("50,000", " ".join(value for _, value in state.custom_settings))
        self.assertEqual("virtual", state.trading_mode)
        self.assertEqual("가상", state.mode_label)
        self.assertTrue(state.read_only_notice.locked)
        self.assertFalse(state.read_only_notice.order_enabled)

    def test_custom_settings_percentages_are_plain_decimal_strings(self):
        state = build_initial_dashboard_state()
        values = [value for _, value in state.custom_settings]

        self.assertNotIn("30%", values)
        self.assertIn("1.5%", values)
        self.assertNotIn("3E+1%", values)

    def test_dashboard_controller_does_not_expose_removed_or_live_order_actions(self):
        self.assertFalse(hasattr(DashboardController, "refresh_market_signal"))
        self.assertFalse(hasattr(DashboardController, "run_paper_simulation"))
        self.assertFalse(hasattr(DashboardController, "update_strategy_setting"))

    def test_format_krw_uses_korean_currency_display(self):
        self.assertEqual("10,000,000원", format_krw(Decimal("10000000")))
        self.assertEqual("0원", format_krw(Decimal("0")))

    def test_mask_account_display_normalizes_premasked_input(self):
        self.assertEqual("******78-01", mask_account_display("******12345678-01"))
        self.assertEqual("******40-01", mask_account_display("******40-01"))


class DashboardControllerTest(unittest.TestCase):
    def test_default_live_check_shares_token_cache_and_rate_limiter(self):
        token_cache = object()
        rate_limiter = object()
        controller = DashboardController(
            services=DashboardServices(kis_rate_limiter=rate_limiter),
            live_token_cache=token_cache,
        )

        with patch("stockbot.dashboard.run_live_read_only_probe", return_value={"ready": True}) as probe:
            result = controller._default_kis_live_check(
                symbol="005930",
                env_file=".env",
                env={},
            )

        self.assertEqual({"ready": True}, result)
        self.assertIs(probe.call_args.kwargs["token_cache"], token_cache)
        self.assertIs(probe.call_args.kwargs["rate_limiter"], rate_limiter)

    def test_kis_check_updates_account_panel_and_system_log(self):
        controller = DashboardController(
            services=DashboardServices(
                kis_check=lambda: {
                    "account": "******40-01",
                    "cash": "10000000",
                    "equity": "12100000",
                    "balance_positions": 1,
                    "last_price": "70000",
                    "read_only": True,
                }
            )
        )

        state = controller.run_kis_check()

        self.assertEqual("******40-01", state.account.masked_account)
        self.assertEqual("10,000,000원", state.account.cash)
        self.assertEqual("12,100,000원", state.account.equity)
        self.assertEqual("1개", state.account.positions)
        self.assertEqual("70,000원", state.account.last_price)
        self.assertIn("KIS", state.system_log[0].title)
        self.assertNotEqual((), state.system_log)

    def test_kis_check_serializes_concurrent_service_calls(self):
        active = 0
        max_active = 0
        active_lock = Lock()
        entered = Event()
        release = Event()

        def slow_kis_check():
            nonlocal active, max_active
            with active_lock:
                active += 1
                max_active = max(max_active, active)
                entered.set()
            release.wait(1)
            with active_lock:
                active -= 1
            return {
                "account": "******40-01",
                "cash": "10000000",
                "equity": "10000000",
                "balance_positions": 0,
                "last_price": "70000",
                "read_only": True,
            }

        controller = DashboardController(services=DashboardServices(kis_check=slow_kis_check))
        first = Thread(target=controller.run_kis_check)
        second = Thread(target=controller.run_kis_check)

        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        second_started = second.is_alive()
        release.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertTrue(second_started)
        self.assertEqual(1, max_active)

    def test_kis_check_uses_rate_limiter_before_calling_service(self):
        calls = []
        limiter = FakeKisRateLimiter(RateLimitDecision(False, 22.0, "token_cooldown"))
        controller = DashboardController(
            services=DashboardServices(
                kis_check=lambda: calls.append("called"),
                kis_rate_limiter=limiter,
            )
        )

        state = controller.run_kis_check()

        self.assertEqual([], calls)
        self.assertEqual(["kis_check"], limiter.checked)
        self.assertEqual([], limiter.recorded)
        self.assertEqual(0, limiter.token_issues)
        self.assertIn("22", state.system_log[0].message)

    def test_kis_check_records_request_and_token_issue_after_success(self):
        limiter = FakeKisRateLimiter(RateLimitDecision(True, 0.0, "allowed"))
        controller = DashboardController(
            services=DashboardServices(
                kis_check=lambda: {
                    "account": "******40-01",
                    "cash": "10000000",
                    "equity": "12100000",
                    "balance_positions": 1,
                    "last_price": "70000",
                    "read_only": True,
                },
                kis_rate_limiter=limiter,
            )
        )

        controller.run_kis_check()

        self.assertEqual(["kis_check"], limiter.checked)
        self.assertEqual(["kis_check"], limiter.recorded)
        self.assertEqual(1, limiter.token_issues)

    def test_paper_kis_check_does_not_mutate_live_rate_limiter(self):
        paper_limiter = FakeKisRateLimiter(RateLimitDecision(True, 0.0, "allowed"))
        live_limiter = FakeKisRateLimiter(RateLimitDecision(False, 61.0, "token_cooldown"))
        controller = DashboardController(
            services=DashboardServices(
                kis_check=lambda: {
                    "account": "******40-01",
                    "cash": "10000000",
                    "equity": "10000000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                },
                kis_rate_limiter=live_limiter,
                paper_kis_rate_limiter=paper_limiter,
            )
        )

        controller.run_kis_check()

        self.assertEqual(["kis_check"], paper_limiter.checked)
        self.assertEqual(["kis_check"], paper_limiter.recorded)
        self.assertEqual(1, paper_limiter.token_issues)
        self.assertEqual([], live_limiter.checked)
        self.assertEqual([], live_limiter.recorded)
        self.assertEqual(0, live_limiter.token_issues)

    def test_kis_check_masks_raw_account_from_service(self):
        controller = DashboardController(
            services=DashboardServices(
                kis_check=lambda: {
                    "account": "12345678-01",
                    "cash": "10000000",
                    "equity": "10000000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                }
            )
        )

        state = controller.run_kis_check()

        self.assertEqual("******78-01", state.account.masked_account)
        self.assertNotIn("12345678", state.account.masked_account)

    def test_kis_failure_system_log_is_sanitized(self):
        def failing_kis_check():
            raise RuntimeError("secret-value token-123 12345678")

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)

    def test_kis_missing_credentials_log_is_actionable_without_secret_values(self):
        def failing_kis_check():
            raise ValueError("missing KIS VTS credentials: KIS_VTS_APP_KEY, KIS_VTS_APP_SECRET")

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn(".env", rendered)
        self.assertIn("KIS_VTS_APP_KEY", rendered)
        self.assertIn("KIS_VTS_APP_SECRET", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("12345678", rendered)

    def test_kis_token_rate_limit_log_tells_user_to_wait_one_minute(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_code":"EGW00133","error_description":"rate limit 1 minute"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertEqual("error", state.system_log[0].level)
        self.assertIn("1", rendered)
        self.assertIn("KIS", rendered)

    def test_kis_per_second_rate_limit_log_tells_user_to_wait_briefly(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 500: {"error_code":"EGW00201","error_description":"초당 거래건수를 초과하였습니다."}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertEqual("error", state.system_log[0].level)
        self.assertIn("초당", rendered)
        self.assertIn("잠시", rendered)
        self.assertNotIn("계좌번호", rendered)

    def test_kis_api_error_log_keeps_safe_code_and_description(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_code":"EGW00001","error_description":"Invalid permission"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("EGW00001", rendered)
        self.assertIn("Invalid permission", rendered)
        self.assertNotIn("{", rendered)

    def test_kis_api_error_log_redacts_sensitive_description_but_keeps_code(self):
        def failing_kis_check():
            raise KisApiError(
                'KIS HTTP 403: {"error_code":"EGW00002","error_description":"bad appsecret 12345678"}'
            )

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("EGW00002", rendered)
        self.assertNotIn("appsecret", rendered.lower())
        self.assertNotIn("12345678", rendered)

    def test_kis_api_error_log_redacts_unsafe_error_code(self):
        def failing_kis_check():
            raise KisApiError(
                'KIS HTTP 403: {"error_code":"bearer-token-12345678","error_description":"Invalid permission"}'
            )

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("HTTP 403", rendered)
        self.assertIn("Invalid permission", rendered)
        self.assertNotIn("bearer-token", rendered)
        self.assertNotIn("12345678", rendered)

    def test_kis_api_error_log_redacts_unsafe_msg_cd(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"msg_cd":"12345678","msg1":"Invalid permission"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("HTTP 403", rendered)
        self.assertIn("Invalid permission", rendered)
        self.assertNotIn("12345678", rendered)

    def test_kis_api_error_log_redacts_appkey_detail(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_code":"EGW00003","error_description":"invalid appkey value"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("EGW00003", rendered)
        self.assertNotIn("appkey", rendered.lower())

    def test_kis_api_error_log_redacts_spaced_key_detail_variants(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_code":"EGW00004","error_description":"invalid api key value"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("EGW00004", rendered)
        self.assertNotIn("api key", rendered.lower())

    def test_kis_api_error_log_redacts_sensitive_short_code(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_code":"TOKEN123","error_description":"Invalid permission"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("HTTP 403", rendered)
        self.assertIn("Invalid permission", rendered)
        self.assertNotIn("TOKEN123", rendered)

    def test_kis_api_error_log_does_not_extract_code_from_sensitive_detail(self):
        def failing_kis_check():
            raise KisApiError('KIS HTTP 403: {"error_description":"invalid appkey ABCD1234"}')

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("HTTP 403", rendered)
        self.assertNotIn("appkey", rendered.lower())
        self.assertNotIn("ABCD1234", rendered)

    def test_kis_env_file_decode_error_is_actionable(self):
        def failing_kis_check():
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn(".env", rendered)
        self.assertIn("UTF-8", rendered)

    def test_kis_env_file_os_error_does_not_echo_raw_path(self):
        def failing_kis_check():
            raise PermissionError("C:\\Users\\example-user\\Documents\\StockProject\\.env")

        controller = DashboardController(services=DashboardServices(kis_check=failing_kis_check))

        state = controller.run_kis_check()
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn(".env", rendered)
        self.assertNotIn("C:\\Users\\example-user", rendered)

    def test_kis_pending_and_cooldown_write_to_system_log(self):
        controller = DashboardController()

        pending = controller.mark_kis_check_pending()
        cooldown = controller.mark_kis_check_cooldown(remaining_seconds=30)

        self.assertIn("KIS", pending.system_log[0].title)
        self.assertIn("30", cooldown.system_log[0].message)

    def test_select_trading_mode_updates_state_notice_and_log(self):
        controller = DashboardController()

        real = controller.select_trading_mode("real")

        self.assertEqual("real", real.trading_mode)
        self.assertEqual("리얼", real.mode_label)
        self.assertFalse(real.read_only_notice.order_enabled)
        self.assertIn("실전", real.read_only_notice.description)
        self.assertIn("잠금", real.read_only_notice.description)
        self.assertIn("리얼모드", real.system_log[0].message)

        virtual = controller.select_trading_mode("virtual")

        self.assertEqual("virtual", virtual.trading_mode)
        self.assertEqual("가상", virtual.mode_label)
        self.assertFalse(virtual.read_only_notice.order_enabled)
        self.assertIn("가상모드", virtual.system_log[0].message)

    def test_select_trading_mode_rejects_unknown_mode(self):
        controller = DashboardController()

        state = controller.select_trading_mode("margin")

        self.assertEqual("virtual", state.trading_mode)
        self.assertEqual("error", state.system_log[0].level)
        self.assertIn("알 수 없는", state.system_log[0].message)

    def test_current_market_session_status_reads_runtime_market_hours(self):
        status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장중이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )
        hours = FakeMarketHours(status)
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime(market_hours=hours)))

        self.assertIs(status, controller.current_market_session_status())
        self.assertEqual(1, hours.calls)

    def test_mark_runtime_start_blocked_outside_market_updates_state_without_starting_runtime(self):
        status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장 대기 - 정규장 시간이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        state = controller.mark_runtime_start_blocked_by_market_hours(status)

        self.assertFalse(runtime.started)
        self.assertEqual("장 대기", state.runtime_status)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("장중", state.system_log[0].title)
        self.assertIn("정규장", state.system_log[0].message)

    def test_switching_to_kis_market_data_outside_market_hours_is_blocked_without_replacing_runtime(self):
        closed_status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장 대기 - 정규장 시간이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")
        build_calls = []

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=lambda source: build_calls.append(source) or FakeRuntime(data_source_kind=source),
                kis_market_status=lambda: closed_status,
            )
        )

        state = controller.select_runtime_data_source("kis-vts")

        self.assertIs(current_runtime, controller.services.runtime)
        self.assertEqual([], build_calls)
        self.assertEqual("local", controller.services.runtime.data_source_kind)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("장중", state.system_log[0].title)
        self.assertIn("정규장", state.system_log[0].message)

    def test_switching_to_external_scan_kis_outside_market_hours_is_blocked_without_replacing_runtime(self):
        closed_status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장 대기 - 정규장 시간이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        build_calls = []

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=lambda source: build_calls.append(source) or FakeRuntime(data_source_kind=source),
                kis_market_status=lambda: closed_status,
            )
        )

        state = controller.select_runtime_data_source("external-scan-kis")

        self.assertIs(current_runtime, controller.services.runtime)
        self.assertEqual([], build_calls)
        self.assertEqual("local", controller.services.runtime.data_source_kind)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("장중", state.system_log[0].title)
        self.assertIn("정규장", state.system_log[0].message)

    def test_switching_to_external_scan_kis_uses_runtime_builder_when_market_is_open(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="장중",
            message="장중입니다.",
            checked_at=datetime(2026, 6, 11, 10, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        hybrid_runtime = FakeRuntime(data_source_kind="external-scan-kis", data_source_label="wide scanner / KIS final quote paper")
        build_calls = []

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=lambda source: build_calls.append(source) or hybrid_runtime,
                kis_market_status=lambda: open_status,
            )
        )

        state = controller.select_runtime_data_source("external-scan-kis")

        self.assertEqual(["external-scan-kis"], build_calls)
        self.assertIs(hybrid_runtime, controller.services.runtime)
        self.assertEqual("success", state.system_log[0].level)
        self.assertIn("KIS", state.system_log[0].message)

    def test_switching_to_external_scan_kis_records_safe_runtime_builder_error_detail(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="장중",
            message="장중입니다.",
            checked_at=datetime(2026, 6, 11, 10, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")

        def fail_builder(_source):
            raise ValueError("configured scanner_source is unavailable: json: stale scanner snapshot age_seconds=239035")

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=fail_builder,
                kis_market_status=lambda: open_status,
            )
        )

        state = controller.select_runtime_data_source("external-scan-kis")

        self.assertIs(current_runtime, controller.services.runtime)
        self.assertEqual("error", state.system_log[0].level)
        self.assertIn("configured scanner_source is unavailable", state.system_log[0].message)
        self.assertIn("stale scanner snapshot", state.system_log[0].message)

    def test_switching_to_external_scan_kis_reports_missing_snapshot_actionably(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="장중",
            message="장중입니다.",
            checked_at=datetime(2026, 6, 11, 10, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        missing_snapshot_message = (
            "scanner_snapshot.json 파일이 없습니다. 외부 수집기로 data 폴더에 scanner_snapshot.json을 먼저 생성하세요"
        )

        def fail_builder(_source):
            raise ValueError(missing_snapshot_message)

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=fail_builder,
                kis_market_status=lambda: open_status,
            )
        )

        state = controller.select_runtime_data_source("external-scan-kis")

        self.assertIs(current_runtime, controller.services.runtime)
        self.assertEqual("error", state.system_log[0].level)
        self.assertIn("data 폴더에 scanner_snapshot.json", state.system_log[0].message)
        self.assertNotIn("<path>", state.system_log[0].message)

    def test_runtime_builder_error_detail_masks_paths_and_account_like_numbers(self):
        from stockbot.dashboard import _safe_runtime_builder_error

        message = _safe_runtime_builder_error(
            RuntimeError("scanner failed at C:\\Users\\example-user\\data\\scanner.json for 12345678")
        )

        self.assertIn("<path>", message)
        self.assertNotIn("12345678", message)
        self.assertNotIn("example-user", message)

    def test_runtime_builder_error_detail_redacts_sensitive_key_variants(self):
        from stockbot.dashboard import _safe_runtime_builder_error

        sensitive_messages = [
            "invalid api key value",
            "invalid app-key value",
            "invalid api_key value",
            "Authorization: Bearer leaked-token-value",
        ]

        for raw_message in sensitive_messages:
            with self.subTest(raw_message=raw_message):
                message = _safe_runtime_builder_error(RuntimeError(raw_message))

                self.assertEqual("RuntimeError", message)

    def test_switching_to_local_virtual_data_source_replaces_runtime_without_market_hours_gate(self):
        current_runtime = FakeRuntime(data_source_kind="kis-vts", data_source_label="KIS VTS 현재가 / paper 체결")
        local_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")
        status_calls = []

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=lambda source: local_runtime,
                kis_market_status=lambda: status_calls.append("called"),
            )
        )

        state = controller.select_runtime_data_source("local")

        self.assertIs(local_runtime, controller.services.runtime)
        self.assertEqual([], status_calls)
        self.assertEqual("success", state.system_log[0].level)
        self.assertIn("로컬", state.system_log[0].message)

    def test_switching_to_kis_market_data_caps_existing_custom_position_limit(self):
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")
        kis_runtime = FakeRuntime(data_source_kind="kis-vts", data_source_label="KIS VTS 현재가 / paper 체결")
        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=lambda source: kis_runtime,
            )
        )
        controller.apply_custom_settings(CustomStrategySettings.default().with_updates(max_positions=12))

        controller.select_runtime_data_source("kis-vts")

        applied = kis_runtime.applied_settings[-1]
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, applied["settings"].max_positions)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, applied["risk_config"].max_positions)

    def test_switching_back_to_local_restores_requested_custom_position_limit(self):
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")
        kis_runtime = FakeRuntime(data_source_kind="kis-vts", data_source_label="KIS VTS 현재가 / paper 체결")
        restored_local_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")

        def build_runtime(source):
            return kis_runtime if source == "kis-vts" else restored_local_runtime

        controller = DashboardController(
            services=DashboardServices(
                runtime=current_runtime,
                runtime_builder=build_runtime,
            )
        )
        controller.apply_custom_settings(CustomStrategySettings.default().with_updates(max_positions=7))

        controller.select_runtime_data_source("kis-vts")
        controller.select_runtime_data_source("local")

        applied = restored_local_runtime.applied_settings[-1]
        self.assertEqual(7, applied["settings"].max_positions)
        self.assertEqual(7, applied["risk_config"].max_positions)
        self.assertEqual(7, controller.current_custom_settings().max_positions)

    def test_custom_settings_in_kis_market_data_mode_keep_intraday_position_cap(self):
        kis_runtime = FakeRuntime(data_source_kind="kis-vts", data_source_label="KIS VTS 현재가 / paper 체결")
        controller = DashboardController(services=DashboardServices(runtime=kis_runtime))

        state = controller.apply_custom_settings(CustomStrategySettings.default().with_updates(max_positions=12))

        applied = kis_runtime.applied_settings[-1]
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, applied["settings"].max_positions)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, applied["risk_config"].max_positions)
        self.assertIn(("최대 보유 종목", f"{KIS_INTRADAY_REHEARSAL_MAX_POSITIONS}개"), state.custom_settings)

    def test_real_mode_start_is_locked_and_does_not_start_runtime(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.select_trading_mode("real")

        state = controller.start_paper_runtime()

        self.assertFalse(runtime.started)
        self.assertEqual("실전 잠금", state.runtime_status)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("실전", state.system_log[0].message)
        self.assertIn("주문", state.system_log[0].message)

    def test_real_mode_start_uses_explicit_live_runtime_builder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir))
            readiness_calls = {"count": 0}
            builder_calls = {"count": 0}

            def fake_readiness(**_kwargs):
                readiness_calls["count"] += 1
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": False,
                    "live_order_enabled": False,
                    "note": "static readiness passed",
                }

            def build_live_runtime():
                builder_calls["count"] += 1
                return live_runtime

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=lambda **_: (_ for _ in ()).throw(RuntimeError("live probe failed")),
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=build_live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("error", state.system_log[0].level)
            self.assertIn("오류", state.system_log[0].message)
            self.assertEqual(0, readiness_calls["count"])
            self.assertEqual(0, builder_calls["count"])

            controller._live_order_safety_context.approve_session()
            state = controller.start_paper_runtime()

            self.assertEqual(1, readiness_calls["count"])
            self.assertEqual(1, builder_calls["count"])
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertEqual("real", state.trading_mode)
            self.assertTrue(getattr(controller, "_runtime_running"))

    def test_real_mode_start_default_readiness_uses_live_config_not_paper_safe_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            env_path = root / ".env"
            scanner_path = root / "data" / "scanner_snapshot.json"
            scanner_path.parent.mkdir(parents=True, exist_ok=True)
            scanner_path.write_text(
                (
                    '{"provider":"external-file","candidates":'
                    '[{"symbol":"BUY001","price":"10000","volume":900000}]}'
                ),
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
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_runtime_builder=lambda: live_runtime,
                ),
                config_path=str(config_path),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertEqual("live", controller.services.runtime.execution_mode)
            self.assertNotIn("trading_mode=live", " ".join(log.message for log in state.system_log[:3]))

    def test_real_mode_start_reuses_current_session_live_account_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_credentials_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            live_probe_calls = {"count": 0}
            readiness_calls = {"count": 0}

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                live_probe_calls["count"] += 1
                if live_probe_calls["count"] > 1:
                    raise RuntimeError("KIS token issue rate limit")
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            def fake_readiness(**kwargs):
                readiness_calls["count"] += 1
                self.assertTrue(kwargs.get("refresh_scanner_snapshot"))
                env_text = env_path.read_text(encoding="utf-8")
                self.assertIn(f"{LIVE_ALLOW_ENV_KEY}=true", env_text)
                self.assertIn(f"{LIVE_ENABLED_ENV_KEY}=true", env_text)
                self.assertIn(f"{LIVE_CONFIRMATION_ENV_KEY}={LIVE_CONFIRMATION_PHRASE}", env_text)
                self.assertIn(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=78", env_text)
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": False,
                    "note": "static readiness passed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller.run_kis_live_check(activate_real_mode=True)

            state = controller.start_paper_runtime()

            self.assertEqual(1, live_probe_calls["count"])
            self.assertEqual(1, readiness_calls["count"])
            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(controller.live_order_approval_status()["sessionApproved"])
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_rechecks_approved_session_when_live_scope_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            live_probe_calls = {"count": 0}
            readiness_calls = {"count": 0}

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                live_probe_calls["count"] += 1
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            def fake_readiness(**_kwargs):
                readiness_calls["count"] += 1
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": False,
                    "note": "static readiness passed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._mark_live_read_only_verified(
                {
                    "KIS_LIVE_APP_KEY": "live-key",
                    "KIS_LIVE_APP_SECRET": "live-secret",
                    "KIS_LIVE_ACCOUNT_NO": "12345678",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                }
            )
            controller._live_order_safety_context.approve_session()
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key-rotated",
                        "KIS_LIVE_APP_SECRET=live-secret",
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

            state = controller.start_paper_runtime()

            self.assertEqual(1, live_probe_calls["count"])
            self.assertEqual(1, readiness_calls["count"])
            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(controller.live_order_approval_status()["sessionApproved"])
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_keeps_running_when_live_account_display_refresh_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, client=SnapshotFailingLiveClient())

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertEqual("real", state.trading_mode)
            self.assertTrue(any(log.level == "warning" and log.title == "Live account" for log in state.system_log))
            joined_logs = " ".join(log.message for log in state.system_log)
            self.assertNotIn("live-secret", joined_logs)
            self.assertNotIn("12345678", joined_logs)

    def test_real_mode_start_populates_live_positions_from_account_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_client = FakeLiveClient(
                snapshot=FakeSnapshot(
                    cash=Decimal("800000"),
                    positions={"005930": make_position("005930", quantity=3, avg="70000", last="71000")},
                )
            )
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, client=live_client)

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                    symbol_names={"005930": "Samsung Electronics"},
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertEqual("1개", state.account.positions)
            self.assertEqual(["005930"], [row.symbol for row in state.active_positions])
            self.assertEqual("Samsung Electronics", state.active_positions[0].company_name)
            self.assertEqual(3, state.active_positions[0].quantity)
            self.assertEqual(1, live_client.snapshot_calls)

    def test_real_mode_cycle_does_not_trigger_extra_live_account_display_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_client = SecondSnapshotFailingLiveClient()
            live_runtime = FakeRuntime(
                data_source_kind="live",
                data_source_label="KIS live orders",
                events=[RuntimeEvent.system("live cycle completed", timestamp=datetime(2026, 6, 11, 9, 1))],
            )
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, client=live_client)

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()

            state = controller.run_paper_cycle()

            self.assertEqual(1, live_runtime.cycles)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertEqual("real", state.trading_mode)
            self.assertEqual(1, live_client.snapshot_calls)
            self.assertFalse(any(log.title == "Live account" and "after cycle" in log.message for log in state.system_log))
            joined_logs = " ".join(log.message for log in state.system_log)
            self.assertNotIn("live-secret", joined_logs)
            self.assertNotIn("12345678", joined_logs)

    def test_real_cleanup_cycle_keeps_running_when_live_position_empty_check_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            live_client = SecondSnapshotFailingLiveClient()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, client=live_client)

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()
            controller.set_cleanup_mode(True)

            state = controller.run_paper_cycle()

            self.assertEqual(1, live_runtime.cycles)
            self.assertFalse(live_runtime.paused)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertTrue(
                any(log.level == "warning" and log.title == "Live positions" for log in state.system_log)
            )
            joined_logs = " ".join(log.message for log in state.system_log)
            self.assertNotIn("live-secret", joined_logs)
            self.assertNotIn("12345678", joined_logs)

    def test_real_mode_start_removes_auto_order_gate_when_readiness_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_credentials_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            def fake_readiness(**_kwargs):
                return {
                    "ready": False,
                    "blockers": ["scanner snapshot stale"],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": False,
                    "live_order_enabled": False,
                    "note": "static readiness failed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertFalse(controller.live_order_approval_status()["sessionApproved"])
            self.assertFalse(controller._live_runtime_readiness_ready)
            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn(f"{LIVE_ALLOW_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ENABLED_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_removes_auto_order_gate_when_live_runtime_admission_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = FakeApprovedLiveBroker()

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertFalse(controller.live_order_approval_status()["sessionApproved"])
            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn(f"{LIVE_ALLOW_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ENABLED_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_removes_auto_order_gate_when_live_runtime_start_fails(self):
        class FailingStartRuntime(FakeRuntime):
            def start(self):
                self.started = True
                raise RuntimeError("live runtime start failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_credentials_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FailingStartRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertFalse(controller.live_order_approval_status()["sessionApproved"])
            self.assertFalse(controller._live_runtime_readiness_ready)
            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn(f"{LIVE_ALLOW_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ENABLED_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertEqual("시작 실패", state.runtime_status)

    def test_real_mode_start_live_broker_uses_shared_session_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_credentials_env(env_path)
            safety_context = LiveOrderSafetyContext()
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, safety_context=safety_context)

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
                live_order_safety_context=safety_context,
            )
            controller.select_trading_mode("real")

            state = controller.start_paper_runtime()

            self.assertTrue(live_runtime.started)
            self.assertTrue(controller._live_runtime_order_gate_approved(live_runtime))
            safety_context.reset()
            self.assertFalse(controller._live_runtime_order_gate_approved(live_runtime))
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_runs_readiness_after_current_session_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            readiness_calls = {"count": 0}

            def fake_readiness(**kwargs):
                readiness_calls["count"] += 1
                self.assertTrue(kwargs.get("refresh_scanner_snapshot"))
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": False,
                    "note": "static readiness passed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            self.assertFalse(controller._live_runtime_readiness_ready)

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertEqual(1, readiness_calls["count"])
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertTrue(controller.live_order_approval_status()["sessionApproved"])
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_reruns_live_readiness_and_blocks_stale_ready_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir))
            readiness_calls = {"count": 0}

            def fake_readiness(**_kwargs):
                readiness_calls["count"] += 1
                return {
                    "ready": False,
                    "blockers": ["scanner snapshot stale"],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": False,
                    "live_order_enabled": False,
                    "note": "static readiness failed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertEqual(1, readiness_calls["count"])
            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertFalse(controller._live_runtime_readiness_ready)
            self.assertEqual("warning", state.system_log[0].level)
            self.assertIn("Live readiness", state.system_log[0].title)
            self.assertIn("scanner snapshot stale", state.system_log[0].message)

    def test_real_mode_start_applies_latest_strategy_settings_to_live_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir))
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )

            controller.select_strategy_profile("aggressive")
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()

            self.assertTrue(live_runtime.started)
            self.assertGreaterEqual(len(live_runtime.applied_settings), 1)
            applied = live_runtime.applied_settings[-1]
            self.assertEqual(Decimal("80000"), applied["settings"].order_cash_amount)
            self.assertEqual(Decimal("0.0005"), applied["strategy_config"].min_momentum_pct)

    def test_real_mode_start_accepts_quoted_live_env_values_after_readiness_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_quoted_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertEqual("real", state.trading_mode)
            self.assertTrue(getattr(controller, "_runtime_running"))

    def test_real_mode_start_rejects_live_runtime_without_managed_position_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir), include_managed_ledger=False)
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)

    def test_real_mode_start_rejects_live_runtime_with_mismatched_live_account_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path, account_no="12345678")
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, account_no="87654321")
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)

    def test_real_mode_start_rejects_live_runtime_without_manual_reconciliation_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, include_manual_reconciliation_store=False)
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)

    def test_real_mode_start_rejects_live_runtime_with_manual_reconciliation_blocker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            assert live_runtime.broker.manual_reconciliation_store is not None
            live_runtime.broker.manual_reconciliation_store.latch(
                ManualReconciliationBlocker(
                    reason="order number missing",
                    symbol="005930",
                    side="BUY",
                    quantity=1,
                    order_no="manual-1",
                    created_at=datetime(2026, 6, 11, 9, 10),
                )
            )
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)
            self.assertIn("manual reconciliation", state.system_log[0].message)

    def test_real_mode_start_allows_unresolved_pending_order_for_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            assert live_runtime.broker.pending_order_store is not None
            live_runtime.broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 6, 11, 9, 10),
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertFalse(controller._live_runtime_order_gate_approved(live_runtime))
            self.assertTrue(
                all("Live runtime order gate blocked" not in event.message for event in state.system_log)
            )

    def test_real_mode_start_allows_terminal_pending_fill_for_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            broker = make_approved_live_broker(root)
            broker.client._daily_orders = [
                {
                    "odno": "123",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "1",
                    "tot_ccld_qty": "1",
                    "rmn_qty": "0",
                    "avg_prvs": "70000",
                }
            ]
            assert broker.pending_order_store is not None
            broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 6, 11, 9, 10),
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            live_runtime.broker = broker
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertNotEqual("warning", state.system_log[0].level)

    def test_real_mode_start_default_readiness_allows_terminal_pending_fill_for_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            env_path = root / ".env"
            scanner_path = root / "data" / "scanner_snapshot.json"
            scanner_path.parent.mkdir(parents=True, exist_ok=True)
            scanner_path.write_text(
                (
                    '{"provider":"test","candidates":'
                    '[{"symbol":"005930","price":"70000","volume":900000}]}'
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path.as_posix()}",
                        f"journal_path: {(root / 'trades.csv').as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            broker = make_approved_live_broker(root)
            broker.client._daily_orders = [
                {
                    "odno": "123",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "1",
                    "tot_ccld_qty": "1",
                    "rmn_qty": "0",
                    "avg_prvs": "70000",
                }
            ]
            assert broker.pending_order_store is not None
            broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 6, 11, 9, 10),
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            live_runtime.broker = broker
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_runtime_builder=lambda: live_runtime,
                ),
                config_path=str(config_path),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", return_value=1):
                state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertEqual("real", state.trading_mode)

    def test_real_mode_start_default_readiness_allows_unresolved_pending_order_for_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            env_path = root / ".env"
            scanner_path = root / "data" / "scanner_snapshot.json"
            scanner_path.parent.mkdir(parents=True, exist_ok=True)
            scanner_path.write_text(
                (
                    '{"provider":"test","candidates":'
                    '[{"symbol":"005930","price":"70000","volume":900000}]}'
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path.as_posix()}",
                        f"journal_path: {(root / 'trades.csv').as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            broker = make_approved_live_broker(root)
            assert broker.pending_order_store is not None
            broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 6, 11, 9, 10),
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            live_runtime.broker = broker
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_runtime_builder=lambda: live_runtime,
                ),
                config_path=str(config_path),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", return_value=1):
                state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertIs(live_runtime, controller.services.runtime)
            self.assertTrue(getattr(controller, "_runtime_running"))
            self.assertFalse(controller._live_runtime_order_gate_approved(live_runtime))
            self.assertTrue(
                any(
                    "Pending live orders will be reconciled by the live runtime admission gate."
                    in event.message
                    for event in state.system_log
                )
            )

    def test_real_mode_start_default_readiness_blocks_manual_pending_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            env_path = root / ".env"
            scanner_path = root / "data" / "scanner_snapshot.json"
            scanner_path.parent.mkdir(parents=True, exist_ok=True)
            scanner_path.write_text(
                (
                    '{"provider":"test","candidates":'
                    '[{"symbol":"005930","price":"70000","volume":900000}]}'
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path.as_posix()}",
                        f"journal_path: {(root / 'trades.csv').as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            broker = make_approved_live_broker(root)
            assert broker.pending_order_store is not None
            broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="manual:missing-order-no",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 6, 11, 9, 10),
                    estimated_price=Decimal("70000"),
                    reason="submitted_without_order_no",
                )
            )
            live_runtime.broker = broker
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_runtime_builder=lambda: live_runtime,
                ),
                config_path=str(config_path),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", return_value=1):
                state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)
            self.assertIn("manual pending live order", state.system_log[0].message)

    def test_real_mode_start_surfaces_redacted_live_runtime_builder_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()

            def failing_builder():
                raise ValueError(
                    "scanner snapshot refresh failed: stale scanner snapshot age_seconds=239035 "
                    "KIS_LIVE_APP_SECRET=sec "
                    "KIS_LIVE_ACCOUNT_NO=live-account78 "
                    "account=plain-account78 account_no=12345678 STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78"
            )

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=failing_builder,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertEqual("error", state.system_log[0].level)
            self.assertIn("scanner snapshot refresh failed", state.system_log[0].message)
            self.assertIn("stale scanner snapshot", state.system_log[0].message)
            self.assertNotIn("KIS_LIVE_APP_SECRET=sec", state.system_log[0].message)
            self.assertNotIn("live-account78", state.system_log[0].message)
            self.assertNotIn("plain-account78", state.system_log[0].message)
            self.assertNotIn("STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78", state.system_log[0].message)
            self.assertNotIn("12345678", state.system_log[0].message)

    def test_real_mode_start_rejects_injected_live_runtime_without_order_gate(self):
        paper_runtime = FakeRuntime()
        live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        live_runtime.execution_mode = "live"
        live_runtime.broker = FakeApprovedLiveBroker()
        controller = DashboardController(
            services=DashboardServices(
                runtime=paper_runtime,
                live_readiness_check=successful_live_readiness,
                live_runtime_builder=lambda: live_runtime,
            )
        )
        controller.select_trading_mode("real")
        controller._live_order_safety_context.approve_session()

        state = controller.start_paper_runtime()

        self.assertFalse(paper_runtime.started)
        self.assertFalse(live_runtime.started)
        self.assertIs(paper_runtime, controller.services.runtime)
        self.assertEqual("real", state.trading_mode)
        self.assertFalse(getattr(controller, "_runtime_running"))
        self.assertEqual("warning", state.system_log[0].level)

    def test_real_mode_start_rejects_non_live_broker_even_with_order_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = FakeApprovedLiveBroker()
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()

            state = controller.start_paper_runtime()

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("warning", state.system_log[0].level)

    def test_real_mode_cycle_runs_explicit_live_runtime_after_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(
                events=[RuntimeEvent.system("live runtime cycle", mode="live", timestamp=datetime(2026, 6, 11, 9, 10))],
                data_source_kind="live",
                data_source_label="KIS live orders",
            )
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir))
            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_path),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()

            state = controller.run_paper_cycle()

            self.assertFalse(paper_runtime.started)
            self.assertEqual(1, live_runtime.cycles)
            self.assertEqual("real", state.trading_mode)
            self.assertEqual("live runtime cycle", state.system_log[0].message)

    def test_real_mode_cycle_pauses_when_live_order_session_has_expired(self):
        runtime = FakeRuntime(
            events=[RuntimeEvent.system("expired session cycle must not run")],
            data_source_kind="live",
            data_source_label="KIS live orders",
        )
        runtime.execution_mode = "live"
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True
        controller._live_order_safety_context.approve_session(
            timestamp=datetime.now() - timedelta(hours=9)
        )

        state = controller.run_paper_cycle()

        self.assertEqual(0, runtime.cycles)
        self.assertTrue(runtime.paused)
        self.assertFalse(getattr(controller, "_runtime_running"))
        self.assertFalse(controller.live_order_approval_status()["sessionApproved"])
        self.assertEqual("실전 세션 만료", state.runtime_status)
        self.assertEqual("실전 세션 만료", state.system_log[0].title)

    def test_real_mode_expired_session_reports_runtime_pause_failure(self):
        class PauseFailingRuntime(FakeRuntime):
            def pause(self):
                raise RuntimeError("pause failed with secret-token")

        runtime = PauseFailingRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True
        controller._live_order_safety_context.approve_session(
            timestamp=datetime.now() - timedelta(hours=9)
        )

        state = controller.run_paper_cycle()

        self.assertFalse(getattr(controller, "_runtime_running"))
        self.assertFalse(controller.live_order_approval_status()["sessionApproved"])
        self.assertIn("runtime_pause_failed", state.system_log[0].message)
        self.assertNotIn("secret-token", state.system_log[0].message)

    def test_switching_to_real_mode_pauses_running_paper_runtime_and_blocks_cycles(self):
        runtime = FakeRuntime(events=[RuntimeEvent.system("cycle should not run")])
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        real_state = controller.select_trading_mode("real")
        cycle_state = controller.run_paper_cycle()

        self.assertTrue(runtime.paused)
        self.assertEqual("real", real_state.trading_mode)
        self.assertEqual("실전 잠금", real_state.runtime_status)
        self.assertEqual(0, runtime.cycles)
        self.assertEqual("실전 잠금", cycle_state.runtime_status)
        self.assertIn("리얼모드", cycle_state.system_log[0].title)

    def test_switching_from_running_real_mode_to_virtual_pauses_live_runtime_and_replaces_paper_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            original_paper_runtime = FakeRuntime()
            replacement_paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    runtime=original_paper_runtime,
                    runtime_builder=lambda _source: replacement_paper_runtime,
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()

            state = controller.select_trading_mode("virtual")
            cycle_state = controller.run_paper_cycle()

            self.assertTrue(live_runtime.started)
            self.assertTrue(live_runtime.paused)
            self.assertIs(controller.services.runtime, replacement_paper_runtime)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("virtual", state.trading_mode)
            self.assertEqual(0, live_runtime.cycles)
            self.assertEqual(0, replacement_paper_runtime.cycles)
            self.assertEqual("warning", cycle_state.system_log[0].level)

    def test_stale_real_cycle_result_is_ignored_after_switching_to_virtual_mode(self):
        class BlockingLiveRuntime(BlockingRuntime):
            execution_mode = "live"

            def run_cycle(self):
                self.in_cycle.set()
                self.release_cycle.wait(2)
                self.cycles += 1
                self.broker._cash = Decimal("1")
                self.broker._positions = {
                    "005930": make_position("005930", quantity=9, avg="10000", last="11000")
                }
                return [
                    RuntimeEvent.system(
                        "stale live cycle should not apply",
                        mode="live",
                        timestamp=datetime(2026, 6, 11, 9, 30),
                    )
                ]

        live_runtime = BlockingLiveRuntime()
        replacement_paper_runtime = FakeRuntime(cash=Decimal("777000"))
        controller = DashboardController(
            services=DashboardServices(
                runtime=live_runtime,
                runtime_builder=lambda _source: replacement_paper_runtime,
            )
        )
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True
        cycle_thread = Thread(target=controller.run_paper_cycle)

        cycle_thread.start()
        self.assertTrue(live_runtime.in_cycle.wait(1))

        switched = controller.select_trading_mode("virtual")
        log_after_switch = controller.state.system_log
        account_after_switch = controller.state.account
        live_runtime.release_cycle.set()
        cycle_thread.join(1)

        messages = [entry.message for entry in controller.state.system_log]
        self.assertEqual("virtual", switched.trading_mode)
        self.assertIs(controller.services.runtime, replacement_paper_runtime)
        self.assertEqual(format_krw(Decimal("777000")), controller.state.account.cash)
        self.assertEqual(account_after_switch, controller.state.account)
        self.assertEqual(log_after_switch, controller.state.system_log)
        self.assertEqual((), controller.state.active_positions)
        self.assertNotIn("stale live cycle should not apply", messages)

    def test_switching_to_real_mode_clears_visible_paper_positions(self):
        runtime = FakeRuntime(positions={"005930": make_position("005930", quantity=4, avg="10000", last="10500")})
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.run_paper_cycle()
        controller.select_position("005930")

        state = controller.select_trading_mode("real")

        self.assertEqual("real", state.trading_mode)
        self.assertEqual((), state.active_positions)
        self.assertEqual("", state.selected_position.symbol)

    def test_switching_back_to_virtual_refreshes_paper_account_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            runtime = FakeRuntime(
                positions={"005930": make_position("005930", quantity=4, avg="10000", last="10500")},
                cash=Decimal("900000"),
            )
            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    runtime=runtime,
                    kis_live_check=lambda **_: {
                        "account": "******78-01",
                        "cash": "100202",
                        "equity": "100202",
                        "balance_positions": 2,
                        "last_price": "286000",
                        "read_only": True,
                        "live_order_enabled": False,
                    },
                ),
            )
            controller.run_kis_live_check(activate_real_mode=True)
            self.assertEqual("real", controller.state.trading_mode)
            self.assertEqual("******78-01", controller.state.account.masked_account)

            state = controller.select_trading_mode("virtual")

            self.assertEqual("virtual", state.trading_mode)
            self.assertEqual("가상계좌", state.account.masked_account)
            self.assertEqual(format_krw(runtime.broker.snapshot().free_cash), state.account.cash)
            self.assertEqual("1개", state.account.positions)
            self.assertEqual(["005930"], [row.symbol for row in state.active_positions])

    def test_live_activation_does_not_preserve_paper_runtime_metrics(self):
        runtime = FakeRuntime()
        runtime.performance_metrics = PaperPerformanceMetrics(
            cash=Decimal("980000"),
            equity=Decimal("1003000"),
            realized_pnl=Decimal("1000"),
            unrealized_pnl=Decimal("2000"),
            total_pnl=Decimal("3000"),
            open_positions=1,
            long_positions=1,
            short_positions=0,
            filled_trades=4,
            rejected_trades=1,
            winning_exits=2,
            losing_exits=1,
            win_rate_pct=Decimal("66.67"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    runtime=runtime,
                    kis_live_check=lambda **_: {
                        "account": "******78-01",
                        "cash": "100202",
                        "equity": "100202",
                        "balance_positions": 0,
                        "last_price": "286000",
                        "read_only": True,
                        "live_order_enabled": False,
                    },
                ),
            )
            controller.start_paper_runtime()
            paper_state = controller.run_paper_cycle()
            self.assertNotEqual((), paper_state.account.runtime_metrics)

            state = controller.run_kis_live_check(activate_real_mode=True)

            self.assertEqual("real", state.trading_mode)
            self.assertEqual("******78-01", state.account.masked_account)
            self.assertEqual(format_krw(Decimal("100202")), state.account.cash)
            self.assertEqual(format_krw(Decimal("100202")), state.account.equity)
            self.assertEqual((), state.account.runtime_metrics)

    def test_live_activation_displays_probe_buying_power_separately_from_cash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=lambda **_: {
                        "account": "******78-01",
                        "cash": "1200000",
                        "equity": "1250000",
                        "buying_power": "750000",
                        "balance_positions": 2,
                        "last_price": "70000",
                        "read_only": True,
                        "live_order_enabled": False,
                    },
                ),
            )

            state = controller.run_kis_live_check(activate_real_mode=True)

            self.assertEqual(format_krw(Decimal("1200000")), state.account.cash)
            self.assertEqual(format_krw(Decimal("1250000")), state.account.equity)
            self.assertEqual(format_krw(Decimal("750000")), state.account.buying_power)

    def test_live_activation_failure_replaces_stale_paper_account(self):
        runtime = FakeRuntime(
            positions={"005930": make_position("005930", quantity=4, avg="10000", last="10500")},
            cash=Decimal("900000"),
        )
        controller = DashboardController(
            services=DashboardServices(
                runtime=runtime,
                kis_live_check=lambda **_: (_ for _ in ()).throw(RuntimeError("live probe failed")),
            )
        )
        controller.start_paper_runtime()
        paper_state = controller.run_paper_cycle()
        self.assertNotEqual(format_krw(Decimal("0")), paper_state.account.cash)
        self.assertNotEqual((), paper_state.account.runtime_metrics)

        state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual("virtual", state.trading_mode)
        self.assertEqual(format_krw(Decimal("0")), state.account.cash)
        self.assertEqual(format_krw(Decimal("0")), state.account.equity)
        self.assertEqual("0개", state.account.positions)
        self.assertEqual((), state.account.runtime_metrics)

    def test_kis_live_failure_uses_real_account_error_copy(self):
        controller = DashboardController(
            services=DashboardServices(
                kis_live_check=lambda **_: (_ for _ in ()).throw(RuntimeError("live probe failed")),
            )
        )

        state = controller.run_kis_live_check(activate_real_mode=True)
        rendered = " ".join(entry.message for entry in state.system_log)

        self.assertIn("실전 계좌 설정", rendered)
        self.assertNotIn("모의투자 계좌 설정", rendered)

    def test_ai_advisor_updates_recommendation_and_system_log(self):
        controller = DashboardController(
            services=DashboardServices(
                advisor=lambda: {
                    "recommended_profile": "balanced",
                    "confidence": "medium",
                    "reasons": ["market flow is mixed, so balanced settings are preferred"],
                    "metrics": {"momentum_pct": "0.01", "volatility": "0.005"},
                }
            )
        )

        state = controller.run_ai_advisor()

        self.assertEqual("balanced", state.advisor.selected_profile)
        self.assertEqual("AI", state.system_log[0].title[:2])

    def test_ai_advisor_applies_recommended_profile_to_runtime(self):
        runtime = FakeRuntime()
        controller = DashboardController(
            services=DashboardServices(
                runtime=runtime,
                advisor=lambda: {
                    "recommended_profile": "aggressive",
                    "confidence": "high",
                    "reasons": ["momentum and volume are strong"],
                    "metrics": {},
                },
            )
        )

        state = controller.run_ai_advisor()

        self.assertEqual("aggressive", state.advisor.selected_profile)
        self.assertEqual(1, len(runtime.applied_settings))
        self.assertEqual("공격형", runtime.applied_settings[0]["profile_label"])
        self.assertEqual(Decimal("80000"), runtime.applied_settings[0]["settings"].order_cash_amount)
        self.assertEqual(0, runtime.applied_settings[0]["risk_config"].max_positions)

    def test_running_ai_advisor_defers_recommended_profile_until_next_cycle(self):
        runtime = OrderedRuntime()
        controller = DashboardController(
            services=DashboardServices(
                runtime=runtime,
                advisor=lambda: {
                    "recommended_profile": "aggressive",
                    "confidence": "high",
                    "reasons": ["momentum and volume are strong"],
                    "metrics": {},
                },
            )
        )
        controller.start_paper_runtime()

        state = controller.run_ai_advisor()

        self.assertEqual("aggressive", state.advisor.selected_profile)
        self.assertEqual([], runtime.applied_settings)
        self.assertIn("다음 cycle", state.system_log[0].message)

        controller.run_paper_cycle()

        self.assertEqual("공격형", runtime.applied_settings[0]["profile_label"])
        self.assertEqual(["apply", "cycle"], runtime.order)

    def test_ai_advisor_preserves_explicit_config_position_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nstrategy_profile: balanced\nmax_positions: 5\n", encoding="utf-8")
            runtime = FakeRuntime()
            controller = DashboardController(
                services=DashboardServices(
                    runtime=runtime,
                    advisor=lambda: {
                        "recommended_profile": "aggressive",
                        "confidence": "high",
                        "reasons": ["momentum and volume are strong"],
                        "metrics": {},
                    },
                ),
                config_path=str(path),
            )

            controller.run_ai_advisor()

            self.assertEqual(5, runtime.applied_settings[0]["settings"].max_positions)
            self.assertEqual(5, runtime.applied_settings[0]["risk_config"].max_positions)

    def test_select_strategy_profile_applies_profile_to_runtime(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        state = controller.select_strategy_profile("conservative")

        self.assertEqual("conservative", state.advisor.selected_profile)
        self.assertEqual("보수형", state.advisor.selected_profile_label)
        self.assertEqual(1, len(runtime.applied_settings))
        applied = runtime.applied_settings[0]
        self.assertEqual("보수형", applied["profile_label"])
        self.assertEqual(Decimal("30000"), applied["settings"].order_cash_amount)
        self.assertEqual(0, applied["settings"].max_positions)
        self.assertEqual(Decimal("0.003"), applied["strategy_config"].min_momentum_pct)
        self.assertEqual(Decimal("60000"), applied["risk_config"].max_daily_loss)

    def test_select_strategy_profile_preserves_explicit_config_position_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nstrategy_profile: balanced\nmax_positions: 5\n", encoding="utf-8")
            runtime = FakeRuntime()
            controller = DashboardController(services=DashboardServices(runtime=runtime), config_path=str(path))

            controller.select_strategy_profile("conservative")

            applied = runtime.applied_settings[0]
            self.assertEqual(5, applied["settings"].max_positions)
            self.assertEqual(5, applied["risk_config"].max_positions)

    def test_select_strategy_profile_preserves_cost_filter_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "transaction_tax_pct: 0.0025",
                        "commission_pct: 0.00015",
                        "slippage_pct: 0.0015",
                        "min_net_profit_pct: 0.002",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = FakeRuntime()
            controller = DashboardController(services=DashboardServices(runtime=runtime), config_path=str(path))

            controller.select_strategy_profile("aggressive")

            strategy_config = runtime.applied_settings[0]["strategy_config"]
            self.assertEqual(Decimal("0.0025"), strategy_config.transaction_tax_pct)
            self.assertEqual(Decimal("0.00015"), strategy_config.commission_pct)
            self.assertEqual(Decimal("0.0015"), strategy_config.slippage_pct)
            self.assertEqual(Decimal("0.002"), strategy_config.min_net_profit_pct)

    def test_apply_custom_settings_marks_custom_and_applies_to_runtime(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        settings = CustomStrategySettings.default().with_updates(
            cash_allocation_pct=Decimal("0.65"),
            max_order_amount=Decimal("0"),
            max_position_amount=Decimal("250000"),
            max_symbol_exposure=Decimal("0.25"),
            max_positions=5,
            max_daily_entries_per_symbol=12,
            allow_paper_short=True,
        )

        state = controller.apply_custom_settings(settings)

        self.assertEqual("custom", state.advisor.selected_profile)
        self.assertEqual("커스텀", state.advisor.selected_profile_label)
        custom_setting_labels = [label for label, _value in state.custom_settings]
        self.assertNotIn("동적 배분", custom_setting_labels)
        self.assertNotIn("기준 금액", custom_setting_labels)
        self.assertIn(("매수 방식", "현금 기준 자동 계산"), state.custom_settings)
        self.assertIn(("현금 사용 비율", "65%"), state.custom_settings)
        self.assertIn(("종목 안전 상한", "250,000원"), state.custom_settings)
        self.assertNotIn(("종목 최대 노출", "25%"), state.custom_settings)
        self.assertIn(("최대 보유 종목", "5개"), state.custom_settings)
        self.assertNotIn(("주문 금액", "70,000원"), state.custom_settings)
        self.assertEqual(1, len(runtime.applied_settings))
        applied = runtime.applied_settings[0]
        self.assertEqual(settings, applied["settings"])
        self.assertEqual("커스텀", applied["profile_label"])
        self.assertEqual(Decimal("0"), applied["risk_config"].max_order_amount)
        self.assertEqual(Decimal("250000"), applied["risk_config"].max_position_amount)
        self.assertEqual(12, applied["risk_config"].max_daily_entries_per_symbol)
        self.assertTrue(applied["strategy_config"].allow_paper_short)

    def test_cleanup_mode_is_global_and_does_not_select_custom_profile(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        state = controller.set_cleanup_mode(True)

        self.assertEqual("balanced", state.advisor.selected_profile)
        self.assertNotIn(("킬 스위치", "켜짐"), state.custom_settings)
        self.assertEqual(1, len(runtime.applied_settings))
        applied = runtime.applied_settings[0]
        self.assertTrue(applied["settings"].kill_switch)
        self.assertTrue(applied["risk_config"].kill_switch)
        self.assertEqual("균형형", applied["profile_label"])

        controller.select_strategy_profile("aggressive")

        self.assertTrue(controller.current_custom_settings().kill_switch)
        self.assertTrue(runtime.applied_settings[-1]["settings"].kill_switch)

    def test_real_cleanup_mode_cannot_be_disabled_from_controller(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        controller.select_trading_mode("real")
        controller.set_cleanup_mode(True)

        with self.assertRaises(PermissionError):
            controller.set_cleanup_mode(False)

        self.assertTrue(controller.current_custom_settings().kill_switch)

    def test_cleanup_mode_stops_runtime_after_all_positions_are_cleared(self):
        runtime = ClearingRuntime(
            positions={"005930": make_position("005930", quantity=1, avg="70000", last="71000")}
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.set_cleanup_mode(True)

        state = controller.run_paper_cycle()

        self.assertTrue(runtime.paused)
        self.assertEqual("일시정지", state.runtime_status)
        self.assertEqual("일시정지", state.account.status)
        self.assertEqual("0개", state.account.positions)
        self.assertEqual((), state.active_positions)
        self.assertEqual("정리 모드 완료", state.system_log[0].title)
        self.assertIn("자동매매를 일시정지", state.system_log[0].message)

        second_state = controller.run_paper_cycle()
        self.assertEqual(1, runtime.cycles)
        self.assertIn("runtime이 실행 중이 아닙니다", second_state.system_log[0].message)

    def test_cleanup_mode_stops_runtime_when_there_are_no_positions_to_cleanup(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.set_cleanup_mode(True)

        state = controller.run_paper_cycle()

        self.assertTrue(runtime.paused)
        self.assertEqual("일시정지", state.runtime_status)
        self.assertEqual("0개", state.account.positions)
        self.assertEqual("정리 모드 완료", state.system_log[0].title)

    def test_cleanup_mode_stop_surfaces_pause_failure_but_disables_scheduler(self):
        runtime = ClearingPauseFailingRuntime(
            positions={"005930": make_position("005930", quantity=1, avg="70000", last="71000")}
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.set_cleanup_mode(True)

        state = controller.run_paper_cycle()
        second_state = controller.run_paper_cycle()

        self.assertTrue(runtime.pause_attempted)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertEqual("정리 모드 완료", state.system_log[0].title)
        self.assertIn("pause 이벤트 기록에 실패", state.system_log[0].message)
        self.assertEqual(1, runtime.cycles)
        self.assertIn("runtime이 실행 중이 아닙니다", second_state.system_log[0].message)

    def test_real_cleanup_completion_resets_live_order_safety_even_when_pause_fails(self):
        runtime = ClearingPauseFailingRuntime(
            positions={"005930": make_position("005930", quantity=1, avg="70000", last="71000")}
        )
        runtime.execution_mode = "live"
        context = LiveOrderSafetyContext()
        controller = DashboardController(
            services=DashboardServices(runtime=runtime),
            live_order_safety_context=context,
        )
        controller.start_paper_runtime()
        controller.state = replace(controller.state, trading_mode="real")
        context.approve_session()
        controller._mark_live_read_only_verified(
            {
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                "KIS_LIVE_APP_KEY": "live-key",
                "KIS_LIVE_APP_SECRET": "live-secret",
            }
        )
        controller.set_cleanup_mode(True)

        state = controller.run_paper_cycle()

        self.assertEqual("warning", state.system_log[0].level)
        self.assertTrue(runtime.pause_attempted)
        self.assertFalse(context.session_approved)
        self.assertFalse(context.risk_limits_ok)
        self.assertFalse(context.new_entries_allowed)
        self.assertIsNone(controller._live_read_only_verified_account_suffix)
        self.assertIsNone(controller._live_read_only_verified_product_code)
        self.assertIsNone(controller._live_read_only_verified_fingerprint)

    def test_apply_custom_settings_preserves_cost_filter_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "transaction_tax_pct: 0.0025",
                        "commission_pct: 0.00015",
                        "slippage_pct: 0.0015",
                        "min_net_profit_pct: 0.002",
                    ]
                ),
                encoding="utf-8",
            )
            runtime = FakeRuntime()
            controller = DashboardController(services=DashboardServices(runtime=runtime), config_path=str(path))

            controller.apply_custom_settings(CustomStrategySettings.default().with_updates(max_positions=4))

            strategy_config = runtime.applied_settings[0]["strategy_config"]
            self.assertEqual(Decimal("0.0025"), strategy_config.transaction_tax_pct)
            self.assertEqual(Decimal("0.00015"), strategy_config.commission_pct)
            self.assertEqual(Decimal("0.0015"), strategy_config.slippage_pct)
            self.assertEqual(Decimal("0.002"), strategy_config.min_net_profit_pct)

    def test_strategy_selection_does_not_wait_for_running_cycle_mutation(self):
        runtime = BlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        profile_applied = Event()

        def select_profile():
            controller.select_strategy_profile("conservative")
            profile_applied.set()

        profile_thread = Thread(target=select_profile)

        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))
        profile_thread.start()

        self.assertTrue(profile_applied.wait(0.2))
        self.assertEqual([], runtime.applied_settings)

        runtime.release_cycle.set()
        cycle_thread.join(1)
        profile_thread.join(1)
        self.assertEqual([], runtime.applied_settings)

        controller.run_paper_cycle()
        self.assertEqual("보수형", runtime.applied_settings[-1]["profile_label"])

    def test_running_profile_selection_is_deferred_until_next_cycle_start(self):
        runtime = OrderedRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.select_strategy_profile("conservative")

        self.assertEqual("conservative", state.advisor.selected_profile)
        self.assertEqual([], runtime.applied_settings)

        controller.run_paper_cycle()

        self.assertEqual(1, len(runtime.applied_settings))
        self.assertEqual("보수형", runtime.applied_settings[0]["profile_label"])
        self.assertEqual(["apply", "cycle"], runtime.order)

    def test_running_profile_selection_retries_pending_settings_after_apply_failure(self):
        runtime = FailingApplyRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        controller.select_strategy_profile("aggressive")
        failed = controller.run_paper_cycle()

        self.assertEqual([], runtime.applied_settings)
        self.assertEqual(0, runtime.cycles)
        self.assertEqual("aggressive", failed.advisor.selected_profile)
        self.assertFalse(getattr(controller, "_runtime_busy"))
        self.assertIsNone(getattr(controller, "_runtime_busy_generation"))

        controller.run_paper_cycle()

        self.assertEqual(1, len(runtime.applied_settings))
        applied = runtime.applied_settings[0]
        self.assertEqual(Decimal("80000"), applied["settings"].order_cash_amount)
        self.assertEqual(Decimal("0.0005"), applied["strategy_config"].min_momentum_pct)
        self.assertEqual(1, runtime.cycles)

    def test_runtime_cycle_exception_logs_structured_sanitized_diagnostic(self):
        class FailingRuntime(FakeRuntime):
            def run_cycle(self):
                raise RuntimeError(
                    r"KIS HTTP 500 EGW00201 KIS_LIVE_APP_SECRET=secret-token C:\Users\example-user\Documents\StockProject\.env"
                )

        runtime = FailingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        message = state.system_log[0].message
        self.assertIn("cycle_exception", message)
        self.assertIn("stage=run_cycle", message)
        self.assertIn("RuntimeError", message)
        self.assertIn("EGW00201", message)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("KIS_LIVE_APP_SECRET", message)
        self.assertNotIn(r"C:\Users", message)

    def test_real_runtime_cycle_uses_cached_account_snapshot_without_extra_broker_snapshot(self):
        old_position = make_position("OLD001", quantity=1, avg="9000", last="9000")
        new_position = make_position("NEW002", quantity=2, avg="10000", last="10100")
        cycle_snapshot = FakeSnapshot(
            positions={"NEW002": new_position},
            cash=Decimal("100202"),
        )

        class CachedSnapshotRuntime(FakeRuntime):
            def run_cycle(self):
                events = super().run_cycle()
                self.latest_cycle_account_snapshot = cycle_snapshot
                return events

        runtime = CachedSnapshotRuntime(
            positions={"OLD001": old_position},
            cash=Decimal("9000"),
            data_source_kind="live",
            data_source_label="KIS live orders / scanner",
        )
        runtime.execution_mode = "live"
        runtime.status = RuntimeStatus(label="실행 중", running=True)
        runtime.performance_metrics = PaperPerformanceMetrics(
            cash=Decimal("1"),
            equity=Decimal("2"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("200"),
            total_pnl=Decimal("200"),
            open_positions=9,
            long_positions=1,
            short_positions=0,
            filled_trades=0,
            rejected_trades=0,
            winning_exits=0,
            losing_exits=0,
            win_rate_pct=Decimal("0"),
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._refresh_active_positions_from_positions({"OLD001": old_position})
        controller._runtime_running = True
        state = controller.run_paper_cycle()

        self.assertEqual(0, runtime.broker.snapshot_calls)
        self.assertEqual(format_krw(Decimal("100202")), state.account.cash)
        self.assertEqual(format_krw(Decimal("120402")), state.account.equity)
        self.assertEqual(format_krw(Decimal("100202")), state.account.buying_power)
        self.assertEqual("1개", state.account.positions)
        self.assertEqual(["NEW002"], [row.symbol for row in state.active_positions])

    def test_real_runtime_cycle_without_cached_account_preserves_prior_display(self):
        position = make_position("KEEP01", quantity=1, avg="10000", last="10100")
        runtime = FakeRuntime(
            data_source_kind="live",
            data_source_label="KIS live orders / scanner",
        )
        runtime.execution_mode = "live"
        runtime.status = RuntimeStatus(label="실행 중", running=True)
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(
            controller.state,
            trading_mode="real",
            account=replace(
                controller.state.account,
                cash=format_krw(Decimal("333000")),
                equity=format_krw(Decimal("343100")),
                positions="1개",
                buying_power=format_krw(Decimal("222000")),
            ),
        )
        controller._refresh_active_positions_from_positions({"KEEP01": position})
        before = controller.state
        controller._runtime_running = True

        state = controller.run_paper_cycle()

        self.assertEqual(0, runtime.broker.snapshot_calls)
        self.assertEqual(before.account, state.account)
        self.assertEqual(before.active_positions, state.active_positions)

    def test_running_profile_selection_keeps_account_equity_until_next_cycle(self):
        runtime = FakeRuntime(
            cash=Decimal("900000"),
            positions={"005930": make_position("005930", quantity=10, avg="10000", last="11000")},
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        before = controller.run_paper_cycle()

        changed = controller.select_strategy_profile("conservative")

        self.assertEqual(before.account.equity, changed.account.equity)
        self.assertEqual(before.account.cash, changed.account.cash)
        self.assertEqual(before.account.positions, changed.account.positions)
        self.assertEqual([], runtime.applied_settings)

        after = controller.run_paper_cycle()

        self.assertEqual(format_krw(runtime.broker.snapshot().equity), after.account.equity)

    def test_running_custom_settings_refreshes_visible_positions_from_runtime_snapshot(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        runtime.broker._positions = {"005930": make_position("005930", quantity=3, avg="10000", last="10500")}

        state = controller.apply_custom_settings(CustomStrategySettings.default().with_updates(max_positions=20))

        self.assertEqual("1개", state.account.positions)
        self.assertEqual(["005930"], [row.symbol for row in state.active_positions])
        self.assertEqual([], runtime.applied_settings)

    def test_running_profile_selection_refreshes_visible_positions_from_runtime_snapshot(self):
        runtime = FakeRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        runtime.broker._positions = {"000660": make_position("000660", quantity=4, avg="20000", last="19800")}

        state = controller.select_strategy_profile("aggressive")

        self.assertEqual("1개", state.account.positions)
        self.assertEqual(["000660"], [row.symbol for row in state.active_positions])
        self.assertEqual([], runtime.applied_settings)

    def test_current_custom_settings_uses_pending_profile_while_runtime_cycle_is_busy(self):
        runtime = BlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        controller.select_strategy_profile("conservative")

        self.assertEqual(Decimal("30000"), controller.current_custom_settings().order_cash_amount)
        self.assertEqual(Decimal("0.012"), controller.current_custom_settings().stop_loss_pct)

        runtime.release_cycle.set()
        cycle_thread.join(1)

    def test_custom_settings_preserve_pending_profile_entry_config_while_runtime_cycle_is_busy(self):
        runtime = BlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        controller.select_strategy_profile("aggressive")
        controller.apply_custom_settings(controller.current_custom_settings().with_updates(stop_loss_pct=Decimal("0.011")))

        runtime.release_cycle.set()
        cycle_thread.join(1)
        self.assertEqual([], runtime.applied_settings)

        controller.run_paper_cycle()
        applied = runtime.applied_settings[-1]
        self.assertEqual("커스텀", applied["profile_label"])
        self.assertEqual(Decimal("0.0005"), applied["strategy_config"].min_momentum_pct)
        self.assertEqual(Decimal("0.011"), applied["strategy_config"].stop_loss_pct)

    def test_idle_custom_settings_replace_stale_pending_settings_after_failed_cycle(self):
        runtime = FailingOnceBlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        controller.select_strategy_profile("conservative")
        runtime.release_cycle.set()
        cycle_thread.join(1)
        controller.apply_custom_settings(CustomStrategySettings.default().with_updates(order_cash_amount=Decimal("70000")))
        controller.run_paper_cycle()

        self.assertEqual(1, len(runtime.applied_settings))
        self.assertEqual(Decimal("70000"), runtime.applied_settings[0]["settings"].order_cash_amount)

    def test_runtime_start_waits_for_running_cycle_to_finish(self):
        runtime = BlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        start_finished = Event()

        def start_runtime():
            controller.start_paper_runtime()
            start_finished.set()

        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))
        start_thread = Thread(target=start_runtime)
        start_thread.start()

        self.assertFalse(start_finished.wait(0.2))
        runtime.release_cycle.set()
        cycle_thread.join(1)
        start_thread.join(1)

        self.assertTrue(start_finished.is_set())
        self.assertEqual(1, runtime.cycles)

    def test_live_readiness_waits_for_running_cycle_to_finish(self):
        runtime = BlockingRuntime()
        readiness_started = Event()
        readiness_calls = {"count": 0}

        def readiness(**_kwargs):
            readiness_calls["count"] += 1
            readiness_started.set()
            return {
                "ready": False,
                "blockers": ["static blocker"],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": False,
                "note": "blocked",
            }

        controller = DashboardController(
            services=DashboardServices(runtime=runtime, live_readiness_check=readiness)
        )
        controller.start_paper_runtime()
        cycle_thread = Thread(target=controller.run_paper_cycle)
        readiness_thread = Thread(target=controller.run_live_readiness_check)

        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))
        readiness_thread.start()

        self.assertFalse(readiness_started.wait(0.2))
        self.assertEqual(0, readiness_calls["count"])
        runtime.release_cycle.set()
        cycle_thread.join(1)
        readiness_thread.join(1)

        self.assertTrue(readiness_started.is_set())
        self.assertEqual(1, readiness_calls["count"])

    def test_concurrent_runtime_cycle_is_skipped_while_previous_cycle_is_running(self):
        runtime = BlockingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        first_cycle = Thread(target=controller.run_paper_cycle)

        first_cycle.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        skipped = controller.run_paper_cycle()

        self.assertIn("아직 실행 중", skipped.system_log[0].message)
        runtime.release_cycle.set()
        first_cycle.join(1)
        self.assertEqual(1, runtime.cycles)

    def test_profile_selection_does_not_wait_for_slow_kis_service_call(self):
        runtime = FakeRuntime()
        started = Event()
        release = Event()

        def slow_kis_check():
            started.set()
            release.wait(2)
            return {
                "account": "******40-01",
                "cash": "1000000",
                "equity": "1000000",
                "balance_positions": 0,
                "last_price": "70000",
                "read_only": True,
            }

        controller = DashboardController(services=DashboardServices(runtime=runtime, kis_check=slow_kis_check))
        kis_thread = Thread(target=controller.run_kis_check)
        profile_applied = Event()

        def select_profile():
            controller.select_strategy_profile("conservative")
            profile_applied.set()

        kis_thread.start()
        self.assertTrue(started.wait(1))
        profile_thread = Thread(target=select_profile)
        profile_thread.start()

        self.assertTrue(profile_applied.wait(0.2))

        release.set()
        kis_thread.join(1)
        profile_thread.join(1)

    def test_stale_ai_recommendation_does_not_overwrite_newer_manual_profile(self):
        runtime = FakeRuntime()
        started = Event()
        release = Event()

        def slow_advisor():
            started.set()
            release.wait(2)
            return {
                "recommended_profile": "aggressive",
                "confidence": "high",
                "reasons": ["momentum and volume are strong"],
                "metrics": {},
            }

        controller = DashboardController(services=DashboardServices(runtime=runtime, advisor=slow_advisor))
        advisor_thread = Thread(target=controller.run_ai_advisor)
        advisor_thread.start()
        self.assertTrue(started.wait(1))

        controller.select_strategy_profile("conservative")
        release.set()
        advisor_thread.join(1)

        self.assertEqual("conservative", controller.state.advisor.selected_profile)
        self.assertEqual("보수형", controller.state.advisor.selected_profile_label)
        self.assertEqual("보수형", runtime.applied_settings[-1]["profile_label"])
        self.assertIn("최신 사용자 선택", controller.state.system_log[0].message)

    def test_runtime_trade_and_system_events_are_split_into_logs_with_company_name(self):
        runtime = FakeRuntime(
            events=[
                RuntimeEvent.trade(
                    symbol="005930",
                    company_name="Samsung Electronics",
                    side="BUY",
                    quantity=2,
                    price=Decimal("70000"),
                    reason="flow_breakout",
                    result="filled",
                    timestamp=datetime(2026, 6, 11, 9, 2),
                ),
                RuntimeEvent.system("cycle completed", timestamp=datetime(2026, 6, 11, 9, 3)),
            ],
            positions={"005930": make_position("005930", quantity=2, avg="70000", last="71000")},
        )
        controller = DashboardController(
            services=DashboardServices(runtime=runtime, symbol_names={"005930": "Samsung Electronics"})
        )
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertEqual(1, runtime.cycles)
        self.assertEqual(1, len(state.trade_log))
        self.assertIn("Samsung Electronics (005930)", state.trade_log[0].title)
        self.assertIn("cycle completed", state.system_log[0].message)
        self.assertEqual("Samsung Electronics", state.active_positions[0].company_name)

    def test_run_paper_cycle_updates_runtime_performance_metrics(self):
        runtime = FakeRuntime()
        runtime.performance_metrics = PaperPerformanceMetrics(
            cash=Decimal("1000000"),
            equity=Decimal("1003000"),
            realized_pnl=Decimal("1000"),
            unrealized_pnl=Decimal("2000"),
            total_pnl=Decimal("3000"),
            open_positions=1,
            long_positions=1,
            short_positions=0,
            filled_trades=4,
            rejected_trades=1,
            winning_exits=2,
            losing_exits=1,
            win_rate_pct=Decimal("66.6667"),
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertIn(("Paper 총손익", "3,000원"), state.account.runtime_metrics)
        self.assertIn(("실현손익", "1,000원"), state.account.runtime_metrics)
        self.assertIn(("평가손익", "2,000원"), state.account.runtime_metrics)
        self.assertIn(("승률", "66.67%"), state.account.runtime_metrics)
        self.assertIn(("체결/거절", "4 / 1"), state.account.runtime_metrics)

    def test_run_paper_cycle_updates_virtual_account_panel_from_runtime_broker(self):
        runtime = FakeRuntime(
            cash=Decimal("957560"),
            positions={
                "005930": make_position("005930", quantity=4, avg="10610", last="10400"),
                "000660": make_position("000660", quantity=2, avg="50100", last="50500"),
            },
        )
        controller = DashboardController(
            services=DashboardServices(
                runtime=runtime,
                symbol_names={"005930": "삼성전자", "000660": "SK하이닉스"},
            )
        )
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertEqual("가상계좌", state.account.masked_account)
        self.assertEqual("957,560원", state.account.cash)
        self.assertEqual("1,100,160원", state.account.equity)
        self.assertEqual("2개", state.account.positions)
        self.assertEqual("957,560원", state.account.buying_power)

    def test_virtual_account_panel_displays_cash_after_short_margin_reserve(self):
        runtime = FakeRuntime(
            cash=Decimal("600000"),
            positions={
                "005930": make_position("005930", quantity=50, avg="10000", last="10000"),
                "000660": make_position("000660", quantity=5, avg="20000", last="20000", side="SHORT"),
            },
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertEqual("400,000원", state.account.cash)
        self.assertEqual("1,000,000원", state.account.equity)
        self.assertEqual("400,000원", state.account.buying_power)

    def test_runtime_system_events_are_sanitized_before_system_log(self):
        runtime = FakeRuntime(
            events=[
                RuntimeEvent.system(
                    "runtime error secret-value token-123 12345678",
                    timestamp=datetime(2026, 6, 11, 9, 3),
                ),
                RuntimeEvent.system(
                    "runtime appkey leaked-value app-key leaked-value",
                    timestamp=datetime(2026, 6, 11, 9, 4),
                ),
                RuntimeEvent.system(
                    "Authorization: Bearer leaked-value api_key leaked-value",
                    timestamp=datetime(2026, 6, 11, 9, 5),
                ),
            ]
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        rendered = " ".join(entry.message for entry in state.system_log)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)
        self.assertNotIn("appkey", rendered.lower())
        self.assertNotIn("app-key", rendered.lower())
        self.assertNotIn("authorization", rendered.lower())
        self.assertNotIn("bearer", rendered.lower())
        self.assertNotIn("api_key", rendered.lower())
        self.assertIn("민감정보", rendered)

    def test_active_position_selection_exposes_detail_chart_and_legend(self):
        runtime = FakeRuntime(
            positions={"005930": make_position("005930", quantity=2, avg="70000", last="71000")}
        )
        controller = DashboardController(
            services=DashboardServices(runtime=runtime, symbol_names={"005930": "Samsung Electronics"})
        )
        controller.start_paper_runtime()
        controller.run_paper_cycle()

        state = controller.select_position("005930")

        self.assertEqual("005930", state.selected_position.symbol)
        self.assertEqual("Samsung Electronics", state.selected_position.company_name)
        self.assertIn(Decimal("71000"), [value for _, value in state.selected_position.price_points])
        self.assertIn(("평균 진입가", Decimal("70000")), state.selected_position.reference_lines)
        self.assertIn("실선: 최근 모의 가격 흐름", state.selected_position.legend_labels)
        self.assertIn("진입: paper 포지션 시작", state.selected_position.legend_labels)

    def test_apply_custom_settings_rebuilds_selected_position_reference_lines(self):
        runtime = FakeRuntime(
            positions={"005930": make_position("005930", quantity=2, avg="10000", last="10000")}
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.run_paper_cycle()
        controller.select_position("005930")

        state = controller.apply_custom_settings(
            CustomStrategySettings.default().with_updates(take_profit_pct=Decimal("0.05"))
        )

        reference_values = [value for _, value in state.selected_position.reference_lines]
        self.assertIn(Decimal("10500.00"), reference_values)
        self.assertNotIn(Decimal("10300.00"), reference_values)

    def test_busy_custom_settings_rebuild_selected_position_lines_from_new_pending_settings(self):
        runtime = BlockingRuntime()
        runtime.broker._positions = {"005930": make_position("005930", quantity=2, avg="10000", last="10000")}
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()
        controller.select_position("005930")
        cycle_thread = Thread(target=controller.run_paper_cycle)
        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        controller.select_strategy_profile("conservative")
        state = controller.apply_custom_settings(
            controller.current_custom_settings().with_updates(take_profit_pct=Decimal("0.05"))
        )

        reference_values = [value for _, value in state.selected_position.reference_lines]
        self.assertIn(Decimal("10500.00"), reference_values)
        self.assertNotIn(Decimal("10200.00"), reference_values)

        runtime.release_cycle.set()
        cycle_thread.join(1)

    def test_run_paper_cycle_without_runtime_logs_actionable_message(self):
        controller = DashboardController()

        state = controller.run_paper_cycle()

        rendered = " ".join(entry.message for entry in state.system_log[:1])
        self.assertIn("runtime", rendered.lower())
        self.assertEqual((), state.trade_log)

    def test_run_paper_cycle_reflects_runtime_waiting_status(self):
        runtime = FakeRuntime(events=[RuntimeEvent.system("장 대기 - 정규장 시간이 아닙니다.")])
        runtime.status = RuntimeStatus(label="장 대기", running=True)
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertEqual("장 대기", state.runtime_status)

    def test_start_paper_runtime_reflects_waiting_status_immediately(self):
        class WaitingRuntime(FakeRuntime):
            def start(self):
                self.status = RuntimeStatus(label="장 대기", running=True)
                return RuntimeEvent.system("장 대기 - 정규장 시간이 아닙니다.", timestamp=datetime(2026, 6, 11, 20, 0))

        controller = DashboardController(services=DashboardServices(runtime=WaitingRuntime()))

        state = controller.start_paper_runtime()

        self.assertEqual("장 대기", state.runtime_status)
        self.assertIn("장 대기", state.system_log[0].message)

    def test_start_without_runtime_does_not_mark_runtime_as_running(self):
        controller = DashboardController()

        state = controller.start_paper_runtime()
        cycle_state = controller.run_paper_cycle()

        self.assertEqual("정지", state.runtime_status)
        self.assertIn("runtime service", cycle_state.system_log[0].message)

    def test_pause_failure_keeps_runtime_marked_running(self):
        runtime = PauseFailingRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.pause_paper_runtime()
        cycle_state = controller.run_paper_cycle()

        self.assertEqual("실행 중", state.runtime_status)
        self.assertEqual(1, runtime.cycles)
        self.assertNotIn("먼저 start", cycle_state.system_log[0].message)

    def test_live_pause_failure_resets_order_safety_context_and_stops_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path)
            context = LiveOrderSafetyContext()
            context.approve_session()
            runtime = PauseFailingRuntime(data_source_kind="live", data_source_label="KIS live orders")
            runtime.execution_mode = "live"
            runtime.broker = make_approved_live_broker(root)
            controller = DashboardController(
                env_file=str(env_path),
                live_order_safety_context=context,
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    live_readiness_check=successful_live_readiness,
                    live_runtime_builder=lambda: runtime,
                ),
            )
            controller.select_trading_mode("real")
            controller._live_order_safety_context.approve_session()
            controller.start_paper_runtime()

            state = controller.pause_paper_runtime()
            cycle_state = controller.run_paper_cycle()

            self.assertFalse(context.session_approved)
            self.assertFalse(context.risk_limits_ok)
            self.assertFalse(context.new_entries_allowed)
            self.assertFalse(getattr(controller, "_runtime_running"))
            self.assertEqual("error", state.system_log[0].level)
            self.assertEqual(0, runtime.cycles)
            self.assertEqual("warning", cycle_state.system_log[0].level)

    def test_runtime_logs_are_capped_to_recent_entries(self):
        runtime = FakeRuntime(
            events=[
                RuntimeEvent.system(f"cycle event {index}", timestamp=datetime(2026, 6, 11, 9, index % 60))
                for index in range(60)
            ]
        )
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.start_paper_runtime()

        state = controller.run_paper_cycle()

        self.assertLessEqual(len(state.system_log), 50)

    def test_default_kis_env_file_resolves_project_root_when_launched_from_dist_app(self):
        with tempfile.TemporaryDirectory() as project_dir:
            project_root = Path(project_dir)
            dist_app = project_root / "dist" / "StockBot"
            dist_app.mkdir(parents=True)
            env_path = project_root / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            previous_cwd = Path.cwd()
            try:
                os.chdir(dist_app)
                controller = DashboardController()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(str(env_path), controller.env_file)

    def test_default_kis_check_uses_exe_adjacent_env_when_frozen(self):
        with tempfile.TemporaryDirectory() as project_dir, tempfile.TemporaryDirectory() as launch_dir:
            project_root = Path(project_dir)
            dist_app = project_root / "dist" / "StockBot"
            internal_dir = dist_app / "_internal"
            internal_dir.mkdir(parents=True)
            executable = dist_app / "StockBot.exe"
            env_path = dist_app / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret-value",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            Path(launch_dir, ".env").write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=wrong-cwd-key",
                        "KIS_VTS_APP_SECRET=wrong-cwd-secret",
                        "KIS_VTS_ACCOUNT_NO=11111111",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            (internal_dir / ".env").write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=wrong-internal-key",
                        "KIS_VTS_APP_SECRET=wrong-internal-secret",
                        "KIS_VTS_ACCOUNT_NO=99999999",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def fake_smoke(*, symbol: str, env_file: str, env=None):
                captured["symbol"] = symbol
                captured["env_file"] = env_file
                captured["env"] = env
                return {
                    "account": "******78-01",
                    "cash": "1000000",
                    "equity": "1000000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                }

            previous_cwd = Path.cwd()
            previous_executable = sys.executable
            previous_frozen = getattr(sys, "frozen", None)
            previous_meipass = getattr(sys, "_MEIPASS", None)
            sys.executable = str(executable)
            sys.frozen = True
            sys._MEIPASS = str(internal_dir)
            try:
                os.chdir(launch_dir)
                with patch("stockbot.dashboard.run_read_only_smoke", side_effect=fake_smoke):
                    state = DashboardController().run_kis_check()
            finally:
                os.chdir(previous_cwd)
                sys.executable = previous_executable
                if previous_frozen is None:
                    delattr(sys, "frozen")
                else:
                    sys.frozen = previous_frozen
                if previous_meipass is None:
                    delattr(sys, "_MEIPASS")
                else:
                    sys._MEIPASS = previous_meipass

        rendered = " ".join(entry.message for entry in state.system_log)
        self.assertEqual(str(env_path), captured["env_file"])
        self.assertEqual({}, captured["env"])
        self.assertEqual("005930", captured["symbol"])
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("12345678", rendered)

    def test_saved_kis_env_file_is_not_overridden_by_process_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            controller = DashboardController(env_file=str(env_path))
            controller.save_kis_credentials(
                app_key="saved-app-key",
                app_secret="saved-credential-b",
                account_no="saved-paper-account",
                product_code="01",
            )
            captured: dict[str, object] = {}

            def fake_smoke(*, symbol: str, env_file: str, env=None):
                captured["symbol"] = symbol
                captured["env_file"] = env_file
                captured["env"] = env
                return {
                    "account": "******nt-01",
                    "cash": "1000000",
                    "equity": "1000000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                }

            previous_env = {key: os.environ.get(key) for key in ("KIS_VTS_APP_KEY", "KIS_VTS_APP_SECRET", "KIS_VTS_ACCOUNT_NO", "KIS_VTS_ACCOUNT_PRODUCT_CODE")}
            os.environ.update(
                {
                    "KIS_VTS_APP_KEY": "stale-process-key",
                    "KIS_VTS_APP_SECRET": "stale-process-value-b",
                    "KIS_VTS_ACCOUNT_NO": "stale-process-account",
                    "KIS_VTS_ACCOUNT_PRODUCT_CODE": "99",
                }
            )
            try:
                with patch("stockbot.dashboard.run_read_only_smoke", side_effect=fake_smoke):
                    controller.run_kis_check()
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        self.assertEqual(str(env_path), captured["env_file"])
        self.assertEqual({}, captured["env"])

    def test_save_kis_live_credentials_writes_all_live_values_without_overriding_paper_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=paper-key",
                        "KIS_VTS_APP_SECRET=paper-secret",
                        "KIS_VTS_ACCOUNT_NO=paper-account",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=99",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))

            controller.save_kis_live_credentials(
                app_key="live-key",
                app_secret="live-secret",
                account_no="12345678",
                product_code="01",
            )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("KIS_LIVE_APP_KEY=live-key", env_text)
            self.assertIn("KIS_LIVE_APP_SECRET=live-secret", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_NO=12345678", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_PRODUCT_CODE=01", env_text)
            self.assertIn("KIS_VTS_APP_KEY=paper-key", env_text)
            self.assertIn("KIS_VTS_APP_SECRET=paper-secret", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_NO=paper-account", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_PRODUCT_CODE=99", env_text)
            self.assertEqual(
                {
                    "appKeySaved": True,
                    "appSecretSaved": True,
                    "accountNoSaved": True,
                    "productCodeSaved": True,
                },
                controller.kis_live_credential_status(),
            )

    def test_save_kis_live_credentials_clears_stale_live_order_approval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=old-live-key",
                        "KIS_LIVE_APP_SECRET=old-live-secret",
                        "KIS_LIVE_ACCOUNT_NO=11111178",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        f"{LIVE_ALLOW_ENV_KEY}=true",
                        f"{LIVE_ENABLED_ENV_KEY}=true",
                        f"{LIVE_CONFIRMATION_ENV_KEY}={LIVE_CONFIRMATION_PHRASE}",
                        f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=78",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))

            controller.save_kis_live_credentials(
                app_key="new-live-key",
                app_secret="new-live-secret",
                account_no="22222278",
                product_code="01",
            )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("KIS_LIVE_APP_KEY=new-live-key", env_text)
            self.assertIn("KIS_LIVE_APP_SECRET=new-live-secret", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_NO=22222278", env_text)
            self.assertNotIn(f"{LIVE_ALLOW_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ENABLED_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertNotIn(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=", env_text)
            self.assertEqual(
                {
                    "allowSaved": False,
                    "enabledSaved": False,
                    "confirmationSaved": False,
                    "accountConfirmationSaved": False,
                    "sessionApproved": False,
                    "riskLimitsOk": False,
                    "newEntriesAllowed": False,
                },
                controller.live_order_approval_status(),
            )

    def test_save_kis_live_credentials_replaces_stale_visible_account_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=old-live-key",
                        "KIS_LIVE_APP_SECRET=old-live-secret",
                        "KIS_LIVE_ACCOUNT_NO=11111178",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))
            controller.state = replace(
                controller.state,
                trading_mode="real",
                account=replace(
                    controller.state.account,
                    status="실전 조회 완료",
                    masked_account="******78-01",
                    cash=format_krw(Decimal("1200000")),
                    equity=format_krw(Decimal("1250000")),
                    positions="2개",
                    buying_power=format_krw(Decimal("1200000")),
                    last_price=format_krw(Decimal("70000")),
                    runtime_metrics=(("Paper 총손익", "12,000원"),),
                ),
            )

            state = controller.save_kis_live_credentials(
                app_key="new-live-key",
                app_secret="new-live-secret",
                account_no="22222221",
                product_code="01",
            )

            self.assertEqual("KIS 실전 조회 설정 저장됨", state.account.status)
            self.assertEqual("******21-01", state.account.masked_account)
            self.assertEqual(format_krw(Decimal("0")), state.account.cash)
            self.assertEqual(format_krw(Decimal("0")), state.account.equity)
            self.assertEqual("0개", state.account.positions)
            self.assertEqual((), state.account.runtime_metrics)
            self.assertFalse(controller._live_order_safety_context.session_approved)

    def test_save_kis_live_credentials_is_blocked_while_real_runtime_is_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=old-live-key",
                        "KIS_LIVE_APP_SECRET=old-live-secret",
                        "KIS_LIVE_ACCOUNT_NO=11111178",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))
            controller.state = replace(controller.state, trading_mode="real")
            controller._runtime_running = True

            state = controller.save_kis_live_credentials(
                app_key="new-live-key",
                app_secret="new-live-secret",
                account_no="22222278",
                product_code="01",
            )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("KIS_LIVE_APP_KEY=old-live-key", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_NO=11111178", env_text)
            self.assertNotIn("new-live-key", env_text)
            self.assertNotIn("22222278", env_text)
            self.assertEqual("error", state.system_log[0].level)
            self.assertIn("차단", state.system_log[0].title)

    def test_run_kis_live_check_uses_saved_env_file_without_process_env_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            captured: dict[str, object] = {}

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                captured["symbol"] = symbol
                captured["env_file"] = env_file
                captured["env"] = env
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            previous_env = {key: os.environ.get(key) for key in ("KIS_LIVE_APP_KEY", "KIS_LIVE_APP_SECRET", "KIS_LIVE_ACCOUNT_NO", "KIS_LIVE_ACCOUNT_PRODUCT_CODE")}
            os.environ.update(
                {
                    "KIS_LIVE_APP_KEY": "stale-process-key",
                    "KIS_LIVE_APP_SECRET": "stale-process-secret",
                    "KIS_LIVE_ACCOUNT_NO": "87654321",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "99",
                }
            )
            try:
                state = controller.run_kis_live_check()
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

            self.assertEqual(str(env_path), captured["env_file"])
            self.assertEqual({}, captured["env"])

    def test_live_readiness_check_uses_saved_env_and_never_approves_orders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            captured: dict[str, object] = {}

            def fake_readiness(**kwargs):
                captured.update(kwargs)
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": False,
                    "note": "ready but must stay locked",
                }

            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir))
            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
            )

            state, result = controller.run_live_readiness_check(refresh_scanner_snapshot=True)

            self.assertEqual(str(env_path), captured["env_file"])
            self.assertTrue(captured["refresh_scanner_snapshot"])
            self.assertNotIn("clear_manual_reconciliation", captured)
            self.assertTrue(result["ready"])
            self.assertFalse(result["live_order_enabled"])
            self.assertEqual("success", state.system_log[0].level)
            self.assertFalse(live_runtime.started)
            self.assertIsNot(live_runtime, controller.services.runtime)
            self.assertFalse(controller.live_order_approval_status()["sessionApproved"])

    def test_clear_live_manual_reconciliation_uses_explicit_separate_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_path)
            captured: dict[str, object] = {}

            def fake_readiness(**kwargs):
                captured.update(kwargs)
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": True,
                    "scanner_snapshot_refreshed": False,
                    "live_order_enabled": False,
                    "note": "manual reconciliation cleared",
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(live_readiness_check=fake_readiness),
            )

            state, result = controller.clear_live_manual_reconciliation(
                confirmation_phrase="I_CONFIRMED_LIVE_ACCOUNT_RECONCILED",
            )

            self.assertEqual(str(env_path), captured["env_file"])
            self.assertFalse(captured["refresh_scanner_snapshot"])
            self.assertEqual(
                "I_CONFIRMED_LIVE_ACCOUNT_RECONCILED",
                captured["clear_manual_reconciliation"],
            )
            self.assertTrue(result["manual_reconciliation_cleared"])
            self.assertFalse(result["live_order_enabled"])
            self.assertEqual("warning", state.system_log[0].level)

    def test_live_readiness_check_blocks_when_live_runtime_builder_is_missing(self):
        def fake_readiness(**_kwargs):
            return {
                "ready": True,
                "blockers": [],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": False,
                "note": "static readiness passed",
            }

        controller = DashboardController(services=DashboardServices(live_readiness_check=fake_readiness))

        state, result = controller.run_live_readiness_check()

        self.assertFalse(result["ready"])
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("live runtime builder", " ".join(result["blockers"]))

    def test_live_readiness_check_reports_live_runtime_builder_failure_as_failure(self):
        def fake_readiness(**_kwargs):
            return {
                "ready": True,
                "blockers": [],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": False,
                "note": "static readiness passed",
            }

        def failing_builder():
            raise RuntimeError("scanner snapshot missing KIS_LIVE_APP_SECRET=secret")

        controller = DashboardController(
            services=DashboardServices(
                live_readiness_check=fake_readiness,
                live_runtime_builder=failing_builder,
            )
        )

        state, result = controller.run_live_readiness_check()
        blockers = " ".join(result["blockers"])

        self.assertFalse(result["ready"])
        self.assertEqual("warning", state.system_log[0].level)
        self.assertIn("live runtime cannot be constructed", blockers)
        self.assertIn("scanner snapshot missing", blockers)
        self.assertNotIn("KIS_LIVE_APP_SECRET=secret", blockers)

    def test_live_readiness_order_gate_blocker_log_points_to_real_start(self):
        def fake_readiness(**_kwargs):
            return {
                "ready": False,
                "blockers": [
                    "live order gate is not configured for the current session",
                ],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": False,
                "note": "static readiness blocked",
            }

        controller = DashboardController(services=DashboardServices(live_readiness_check=fake_readiness))

        state, result = controller.run_live_readiness_check()
        message = state.system_log[0].message

        self.assertFalse(result["ready"])
        self.assertIn("자동매매 시작", message)
        self.assertIn("현재 세션 계좌 확인", message)
        self.assertNotIn("approval", message.lower())
        self.assertNotIn("실전 주문 승인 문구", message)
        self.assertNotIn("계좌 끝 2자리", message)

    def test_live_readiness_check_uses_live_runtime_gate_without_mutating_runtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            write_live_order_approval_env(env_path, account_no="12345678")
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root, account_no="87654321")

            def fake_readiness(**_kwargs):
                return {
                    "ready": True,
                    "blockers": [],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": False,
                    "live_order_enabled": False,
                    "note": "static readiness passed",
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    runtime=paper_runtime,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
            )

            state, result = controller.run_live_readiness_check()

            self.assertFalse(result["ready"])
            self.assertIs(paper_runtime, controller.services.runtime)
            self.assertFalse(live_runtime.started)
            self.assertEqual("warning", state.system_log[0].level)
            self.assertIn("scope", " ".join(result["blockers"]))

    def test_live_readiness_check_redacts_blockers_and_fails_closed(self):
        def fake_readiness(**_kwargs):
            return {
                "ready": "yes",
                "blockers": [
                    "KIS_LIVE_APP_SECRET=live-secret access_token=token-123 account=12345678-01",
                ],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": True,
                "note": "KIS_LIVE_APP_KEY=live-key",
            }

        controller = DashboardController(services=DashboardServices(live_readiness_check=fake_readiness))

        state, result = controller.run_live_readiness_check()
        rendered = " ".join([*result["blockers"], str(result["note"]), state.system_log[0].message])

        self.assertFalse(result["ready"])
        self.assertFalse(result["live_order_enabled"])
        self.assertEqual("warning", state.system_log[0].level)
        self.assertNotIn("live-secret", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)
        self.assertNotIn("live-key", rendered)

    def test_live_readiness_exception_clears_previous_ready_state(self):
        def fake_readiness(**_kwargs):
            raise RuntimeError("temporary KIS readiness failure")

        controller = DashboardController(services=DashboardServices(live_readiness_check=fake_readiness))
        controller._live_runtime_readiness_ready = True

        state, result = controller.run_live_readiness_check()

        self.assertFalse(result["ready"])
        self.assertFalse(controller._live_runtime_readiness_ready)
        self.assertEqual("error", state.system_log[0].level)

    def test_clear_live_manual_reconciliation_exception_clears_previous_ready_state(self):
        def fake_readiness(**_kwargs):
            raise RuntimeError("temporary manual reconciliation failure")

        controller = DashboardController(services=DashboardServices(live_readiness_check=fake_readiness))
        controller._live_order_safety_context.approve_session()

        state, result = controller.clear_live_manual_reconciliation(
            confirmation_phrase="I_CONFIRMED_LIVE_ACCOUNT_RECONCILED",
        )

        self.assertFalse(result["ready"])
        self.assertFalse(controller._live_runtime_readiness_ready)
        self.assertEqual("error", state.system_log[0].level)

    def test_save_live_order_approval_writes_local_order_gate_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            controller.run_kis_live_check(activate_real_mode=False)

            controller.save_live_order_approval(
                confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                account_confirmation="78",
            )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertIn("STOCKBOT_LIVE_TRADING_ENABLED=true", env_text)
            self.assertIn(f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}", env_text)
            self.assertIn("STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78", env_text)
            self.assertEqual(
                {
                    "allowSaved": True,
                    "enabledSaved": True,
                    "confirmationSaved": True,
                    "accountConfirmationSaved": True,
                    "sessionApproved": True,
                    "riskLimitsOk": True,
                    "newEntriesAllowed": True,
                },
                controller.live_order_approval_status(),
            )
            self.assertTrue(controller._live_order_safety_context.session_approved)
            self.assertTrue(controller._live_order_safety_context.risk_limits_ok)
            self.assertTrue(controller._live_order_safety_context.new_entries_allowed)

    def test_save_live_order_approval_requires_successful_live_read_only_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))

            with self.assertRaises(ValueError):
                controller.save_live_order_approval(
                    confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                    account_confirmation="78",
                )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertFalse(controller._live_order_safety_context.session_approved)

    def test_live_order_session_approval_expires_after_ttl_or_date_rollover(self):
        context = LiveOrderSafetyContext(approval_ttl=timedelta(hours=8))
        approved_at = datetime(2026, 6, 11, 9, 0)

        context.approve_session(timestamp=approved_at)

        self.assertTrue(context.approval_current(now=datetime(2026, 6, 11, 15, 0)))
        self.assertFalse(context.approval_current(now=datetime(2026, 6, 11, 18, 1)))
        self.assertFalse(context.approval_current(now=datetime(2026, 6, 12, 9, 0)))

    def test_live_order_session_approval_properties_fail_closed_after_ttl(self):
        context = LiveOrderSafetyContext(approval_ttl=timedelta(seconds=1))

        context.approve_session(timestamp=datetime.now() - timedelta(seconds=2))

        self.assertFalse(context.session_approved)
        self.assertFalse(context.risk_limits_ok)
        self.assertFalse(context.new_entries_allowed)

    def test_save_live_order_approval_rejects_changed_live_credentials_after_read_only_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            controller.run_kis_live_check(activate_real_mode=False)
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=changed-live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                controller.save_live_order_approval(
                    confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                    account_confirmation="78",
                )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertFalse(controller._live_order_safety_context.session_approved)

    def test_save_live_order_approval_requires_valid_read_only_probe_result(self):
        invalid_results = {
            "not_read_only": {
                "account": "******78-01",
                "cash": "1200000",
                "equity": "1250000",
                "balance_positions": 2,
                "last_price": "70000",
                "read_only": False,
                "live_order_enabled": False,
            },
            "order_enabled": {
                "account": "******78-01",
                "cash": "1200000",
                "equity": "1250000",
                "balance_positions": 2,
                "last_price": "70000",
                "read_only": True,
                "live_order_enabled": True,
            },
            "wrong_account_scope": {
                "account": "******99-01",
                "cash": "1200000",
                "equity": "1250000",
                "balance_positions": 2,
                "last_price": "70000",
                "read_only": True,
                "live_order_enabled": False,
            },
            "suffix_near_miss": {
                "account": "78****99-01",
                "cash": "1200000",
                "equity": "1250000",
                "balance_positions": 2,
                "last_price": "70000",
                "read_only": True,
                "live_order_enabled": False,
            },
        }
        for name, probe_result in invalid_results.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmpdir:
                env_path = Path(tmpdir) / ".env"
                env_path.write_text(
                    "\n".join(
                        [
                            "KIS_LIVE_APP_KEY=live-key",
                            "KIS_LIVE_APP_SECRET=live-secret",
                            "KIS_LIVE_ACCOUNT_NO=12345678",
                            "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )

                def fake_live_probe(*, symbol: str, env_file: str, env=None):
                    return dict(probe_result)

                controller = DashboardController(
                    env_file=str(env_path),
                    services=DashboardServices(kis_live_check=fake_live_probe),
                )
                controller.run_kis_live_check(activate_real_mode=False)

                with self.assertRaises(ValueError):
                    controller.save_live_order_approval(
                        confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                        account_confirmation="78",
                    )

                env_text = env_path.read_text(encoding="utf-8")
                self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
                self.assertFalse(controller._live_order_safety_context.session_approved)

    def test_save_live_order_approval_rejects_wrong_phrase_or_account_suffix(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(env_file=str(env_path))

            with self.assertRaises(ValueError):
                controller.save_live_order_approval(confirmation_phrase="wrong", account_confirmation="78")
            with self.assertRaises(ValueError):
                controller.save_live_order_approval(
                    confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                    account_confirmation="00",
                )

            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)

    def test_live_order_session_approval_resets_on_mode_switch_and_credential_save(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            controller.run_kis_live_check(activate_real_mode=False)

            controller.save_live_order_approval(
                confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                account_confirmation="78",
            )
            self.assertTrue(controller._live_order_safety_context.session_approved)

            controller.select_trading_mode("real")

            self.assertFalse(controller._live_order_safety_context.session_approved)
            self.assertFalse(controller._live_order_safety_context.risk_limits_ok)
            self.assertFalse(controller._live_order_safety_context.new_entries_allowed)

            with self.assertRaises(ValueError):
                controller.save_live_order_approval(
                    confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                    account_confirmation="78",
                )

            controller.run_kis_live_check(activate_real_mode=False)
            controller.save_live_order_approval(
                confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                account_confirmation="78",
            )
            controller.save_kis_live_credentials(
                app_key="new-live-key",
                app_secret="new-live-secret",
                account_no="87654321",
                product_code="01",
            )

            self.assertFalse(controller._live_order_safety_context.session_approved)
            env_text = env_path.read_text(encoding="utf-8")
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)

    def test_kis_live_check_without_real_activation_keeps_virtual_account_panel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            before = controller.state.account

            state = controller.run_kis_live_check(activate_real_mode=False)

        self.assertEqual("virtual", state.trading_mode)
        self.assertEqual(before, state.account)
        self.assertEqual("success", state.system_log[0].level)
        rendered = " ".join(entry.message for entry in state.system_log)
        self.assertIn("******78-01", rendered)
        self.assertNotIn("12345678", rendered)

    def test_kis_live_check_without_real_activation_refreshes_real_account_panel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-key",
                        "KIS_LIVE_APP_SECRET=live-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(kis_live_check=fake_live_probe),
            )
            controller.state = replace(
                controller.state,
                trading_mode="real",
                mode_label="리얼모드",
                account=replace(
                    controller.state.account,
                    masked_account="******99-01",
                    cash=format_krw(Decimal("10")),
                    equity=format_krw(Decimal("10")),
                    positions="9개",
                    runtime_metrics=(("Paper 총손익", "99원"),),
                ),
            )

            state = controller.run_kis_live_check(activate_real_mode=False)

        self.assertEqual("real", state.trading_mode)
        self.assertEqual("******78-01", state.account.masked_account)
        self.assertEqual(format_krw(Decimal("1200000")), state.account.cash)
        self.assertEqual(format_krw(Decimal("1250000")), state.account.equity)
        self.assertEqual("2개", state.account.positions)
        self.assertEqual((), state.account.runtime_metrics)
        self.assertEqual("success", state.system_log[0].level)

    def test_kis_live_check_populates_real_positions_from_probe_holdings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "100202",
                    "equity": "305202",
                    "buying_power": "100202",
                    "balance_positions": 1,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                    "positions": [
                        {
                            "symbol": "005930",
                            "side": "LONG",
                            "quantity": 3,
                            "avg_price": "70000",
                            "last_price": "71000",
                            "market_value": "213000",
                            "unrealized_pnl": "3000",
                            "sellable_quantity": 2,
                        }
                    ],
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    symbol_names={"005930": "삼성전자"},
                ),
            )

            state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual("real", state.trading_mode)
        self.assertEqual("******78-01", state.account.masked_account)
        self.assertEqual("1개", state.account.positions)
        self.assertEqual(["005930"], [row.symbol for row in state.active_positions])
        self.assertEqual("삼성전자", state.active_positions[0].company_name)
        self.assertEqual(3, state.active_positions[0].quantity)
        self.assertEqual(format_krw(Decimal("70000")), state.active_positions[0].avg_price)
        self.assertEqual(format_krw(Decimal("71000")), state.active_positions[0].last_price)
        self.assertEqual(format_krw(Decimal("3000")), state.active_positions[0].unrealized_pnl)

    def test_kis_live_real_activation_outside_market_refreshes_account_with_market_hours_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            probe_calls = []
            closed_status = MarketSessionStatus(
                is_open=False,
                label="장 대기",
                message="장 대기 - 정규장 시간이 아닙니다.",
                checked_at=datetime(2026, 7, 13, 8, 42, tzinfo=KST),
                next_open=datetime(2026, 7, 13, 9, 0, tzinfo=KST),
            )

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                probe_calls.append(symbol)
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    kis_market_status=lambda: closed_status,
                ),
            )

            state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual(["005930"], probe_calls)
        self.assertEqual("real", state.trading_mode)
        self.assertEqual("******78-01", state.account.masked_account)
        self.assertEqual(format_krw(Decimal("1200000")), state.account.cash)
        self.assertEqual("warning", state.system_log[0].level)
        self.assertEqual("장중 아님", state.system_log[0].title)
        self.assertIn("실전 계좌 조회는 완료", state.system_log[0].message)
        self.assertIn("09:00-15:30 KST", state.system_log[0].message)
        self.assertIn("2026-07-13 09:00 KST", state.system_log[0].message)

    def test_kis_live_check_ignores_market_status_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    kis_market_status=lambda: (_ for _ in ()).throw(RuntimeError("status unavailable")),
                ),
            )

            state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual("real", state.trading_mode)
        self.assertEqual("******78-01", state.account.masked_account)
        self.assertEqual("success", state.system_log[0].level)
        self.assertEqual("KIS 실전 조회 확인", state.system_log[0].title)

    def test_persistent_and_interactive_live_account_probes_never_overlap(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            active = 0
            max_active = 0
            call_count = 0
            active_lock = Lock()
            first_entered = Event()
            release_first = Event()
            overlap_detected = Event()
            interactive_started = Event()
            errors = []

            def slow_live_probe(*, symbol: str, env_file: str, env=None):
                nonlocal active, max_active, call_count
                with active_lock:
                    call_count += 1
                    current_call = call_count
                    active += 1
                    max_active = max(max_active, active)
                    if current_call == 1:
                        first_entered.set()
                    if active > 1:
                        overlap_detected.set()
                if current_call == 1:
                    release_first.wait(2)
                with active_lock:
                    active -= 1
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=slow_live_probe,
                    kis_market_status=lambda: SimpleNamespace(
                        is_open=True,
                        label="장중",
                    ),
                ),
            )

            def run_persistent_probe():
                try:
                    controller.run_persistent_live_account_probe()
                except Exception as exc:
                    errors.append(exc)

            def run_interactive_probe():
                interactive_started.set()
                try:
                    controller.run_kis_live_check(activate_real_mode=True)
                except Exception as exc:
                    errors.append(exc)

            persistent_thread = Thread(target=run_persistent_probe, daemon=True)
            interactive_thread = Thread(target=run_interactive_probe, daemon=True)
            persistent_thread.start()
            self.assertTrue(first_entered.wait(1))
            interactive_thread.start()
            self.assertTrue(interactive_started.wait(1))

            overlapped = overlap_detected.wait(0.3)
            release_first.set()
            persistent_thread.join(2)
            interactive_thread.join(2)

        self.assertFalse(persistent_thread.is_alive())
        self.assertFalse(interactive_thread.is_alive())
        self.assertEqual([], errors)
        self.assertFalse(overlapped)
        self.assertEqual(1, max_active)
        self.assertEqual(2, call_count)

    def test_live_readiness_and_interactive_probe_complete_without_lock_inversion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            probe_entered = Event()
            release_probe = Event()
            readiness_attempted = Event()
            readiness_called = Event()
            errors = []

            def slow_live_probe(*, symbol: str, env_file: str, env=None):
                probe_entered.set()
                release_probe.wait(2)
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            def readiness(**_kwargs):
                readiness_called.set()
                return successful_live_readiness()

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=slow_live_probe,
                    live_readiness_check=readiness,
                ),
            )

            def run_interactive_probe():
                try:
                    controller.run_kis_live_check(activate_real_mode=False)
                except Exception as exc:
                    errors.append(exc)

            def run_readiness():
                readiness_attempted.set()
                try:
                    controller.run_live_readiness_check()
                except Exception as exc:
                    errors.append(exc)

            probe_thread = Thread(target=run_interactive_probe, daemon=True)
            readiness_thread = Thread(target=run_readiness, daemon=True)
            probe_thread.start()
            self.assertTrue(probe_entered.wait(1))
            readiness_thread.start()
            self.assertTrue(readiness_attempted.wait(1))
            self.assertFalse(readiness_called.wait(0.2))

            release_probe.set()
            probe_thread.join(2)
            readiness_thread.join(2)

        self.assertFalse(probe_thread.is_alive())
        self.assertFalse(readiness_thread.is_alive())
        self.assertEqual([], errors)
        self.assertTrue(readiness_called.is_set())

    def test_kis_live_token_limit_preserves_verified_real_account_and_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            calls = {"count": 0}

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                calls["count"] += 1
                if calls["count"] > 1:
                    raise KisApiError(
                        'KIS HTTP 403: {"error_code":"EGW00133","error_description":"rate limit 1 minute"}'
                    )
                return {
                    "account": "******78-01",
                    "cash": "100202",
                    "equity": "305202",
                    "buying_power": "100202",
                    "balance_positions": 1,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                    "positions": [
                        {
                            "symbol": "005930",
                            "side": "LONG",
                            "quantity": 3,
                            "avg_price": "70000",
                            "last_price": "71000",
                            "unrealized_pnl": "3000",
                        }
                    ],
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    symbol_names={"005930": "삼성전자"},
                ),
            )
            first = controller.run_kis_live_check(activate_real_mode=True)
            successful_probe_revision = controller.live_account_probe_revision()

            state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual("real", state.trading_mode)
        self.assertEqual(first.account.masked_account, state.account.masked_account)
        self.assertEqual(first.account.cash, state.account.cash)
        self.assertEqual(first.account.equity, state.account.equity)
        self.assertEqual(first.account.positions, state.account.positions)
        self.assertEqual(["005930"], [row.symbol for row in state.active_positions])
        self.assertEqual(3, state.active_positions[0].quantity)
        self.assertEqual(1, successful_probe_revision)
        self.assertEqual(
            successful_probe_revision,
            controller.live_account_probe_revision(),
        )
        self.assertTrue(
            controller._live_read_only_verification_matches(
                {
                    "KIS_LIVE_APP_KEY": "live-key",
                    "KIS_LIVE_APP_SECRET": "live-secret",
                    "KIS_LIVE_ACCOUNT_NO": "12345678",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                }
            )
        )
        self.assertEqual("error", state.system_log[0].level)
        rendered = " ".join(entry.message for entry in state.system_log)
        self.assertNotIn("12345678", rendered)

    def test_kis_live_generic_rate_limit_clears_verification_and_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            write_live_credentials_env(env_path)
            calls = {"count": 0}

            def fake_live_probe(*, symbol: str, env_file: str, env=None):
                calls["count"] += 1
                if calls["count"] > 1:
                    raise KisApiError("KIS HTTP 429: generic quote rate limit")
                return {
                    "account": "******78-01",
                    "cash": "100202",
                    "equity": "305202",
                    "buying_power": "100202",
                    "balance_positions": 1,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                    "positions": [
                        {
                            "symbol": "005930",
                            "side": "LONG",
                            "quantity": 3,
                            "avg_price": "70000",
                            "last_price": "71000",
                            "unrealized_pnl": "3000",
                        }
                    ],
                }

            controller = DashboardController(
                env_file=str(env_path),
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    symbol_names={"005930": "삼성전자"},
                ),
            )
            controller.run_kis_live_check(activate_real_mode=True)

            state = controller.run_kis_live_check(activate_real_mode=True)

        self.assertEqual("real", state.trading_mode)
        self.assertEqual("******", state.account.masked_account)
        self.assertEqual(format_krw(Decimal("0")), state.account.cash)
        self.assertEqual([], [row.symbol for row in state.active_positions])
        self.assertFalse(
            controller._live_read_only_verification_matches(
                {
                    "KIS_LIVE_APP_KEY": "live-key",
                    "KIS_LIVE_APP_SECRET": "live-secret",
                    "KIS_LIVE_ACCOUNT_NO": "12345678",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                }
            )
        )
        self.assertEqual("error", state.system_log[0].level)


def make_position(symbol="005930", quantity=2, avg="70000", last="71000", side="LONG"):
    last_price = Decimal(last)
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_price=Decimal(avg),
        last_price=last_price,
        opened_at=datetime(2026, 6, 11, 9, 0),
        highest_price=max(Decimal(avg), last_price),
        side=side,
        lowest_price=min(Decimal(avg), last_price),
    )


if __name__ == "__main__":
    unittest.main()
