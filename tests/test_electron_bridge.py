from __future__ import annotations

import json
import contextlib
import io
import sys
import threading
import time
import unittest
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib import request
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.dashboard import ActivityLogEntry, DashboardController, DashboardServices
from stockbot.config import BotConfig, KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, KIS_INTRADAY_REHEARSAL_SCAN_LIMIT
from stockbot.electron_bridge import (
    _controller_log_datetimes,
    _dispatch_action,
    create_bridge_server,
    dashboard_state_to_view_model,
    main,
    redact_sensitive_text,
)
from stockbot.live_audit import JsonlLiveAuditLog
from stockbot.live_broker import LiveBroker
from stockbot.live_order_state import JsonManualReconciliationStore, JsonPendingLiveOrderStore, PendingLiveOrder
from stockbot.live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from stockbot.live_reconciliation import KisLiveOrderReconciler
from stockbot.live_safety import (
    LIVE_CONFIRMATION_PHRASE,
    live_credential_scope_fingerprint,
    read_env_file,
)
from stockbot.market_hours import KST, MarketSessionStatus
from stockbot.models import Position
from stockbot.risk import RiskConfig, RiskManager
from stockbot.runtime import CustomStrategySettings, RuntimeEvent, RuntimeStatus
from stockbot.strategy import FlowScalperConfig, FlowScalperStrategy
from stockbot.trade_log import build_trade_log_entry


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
        return self.cash


class FakeBroker:
    def __init__(self):
        self.positions = {
            "005930": Position(
                symbol="005930",
                quantity=4,
                avg_price=Decimal("70000"),
                last_price=Decimal("71400"),
                opened_at=datetime(2026, 6, 11, 9, 0),
                highest_price=Decimal("71800"),
            )
        }

    def snapshot(self):
        return FakeSnapshot(self.positions)


class FakeApprovedLiveBroker(FakeBroker):
    def session_approved(self):
        return True

    def risk_limits_ok(self):
        return True


@dataclass(frozen=True)
class FakeSchedulerTimingSnapshot:
    active: object
    interval_seconds: object
    cycle_in_progress: object
    seconds_until_next_cycle: object
    configured_idle_seconds: object = 15.0
    last_cycle_duration_seconds: object = 12.5
    last_cycle_start_interval_seconds: object = 27.5
    cycle_duration_sample_count: object = 12
    cycle_duration_p95_seconds: object = 18.25
    cycle_start_interval_sample_count: object = 11
    cycle_start_interval_p95_seconds: object = 33.5
    current_action: object = "cycle_completed"
    last_cycle_completed_at: object = "2026-07-30T01:02:03+00:00"


class FakeSchedulerControl:
    def __init__(self):
        self.cycle_label = "Windows 서비스가 다음 cycle 예약"
        self.snapshot_active = True
        self.snapshot_interval_seconds = 60.0
        self.snapshot_seconds_until_next_cycle = 42.25
        self.snapshot_cycle_in_progress = True
        self.snapshot_configured_idle_seconds = 15.0
        self.snapshot_last_cycle_duration_seconds = 12.5
        self.snapshot_last_cycle_start_interval_seconds = 27.5
        self.snapshot_cycle_duration_sample_count = 12
        self.snapshot_cycle_duration_p95_seconds = 18.25
        self.snapshot_cycle_start_interval_sample_count = 11
        self.snapshot_cycle_start_interval_p95_seconds = 33.5
        self.snapshot_current_action = "cycle_completed"
        self.snapshot_last_cycle_completed_at = "2026-07-30T01:02:03+00:00"
        self.timing_snapshot_calls = 0
        self.consecutive_failures = 2
        self.last_error_stage = "runtime_cycle"
        self.last_error_code = "TimeoutError"
        self.resume_calls = 0
        self.suspend_calls = 0
        self.bind_saved_credential_scope_calls = 0
        self.credential_binding_pending = False
        self.expected_credential_fingerprint = ""
        self.current_credential_scope_authorized = True
        self.validate_current_credential_scope_calls = 0
        self.validated_credential_candidates: list[str] = []
        self.bound_credential_candidates: list[str] = []

    @property
    def active(self):
        raise AssertionError("bridge must read scheduler active state through the timing snapshot")

    @property
    def interval_seconds(self):
        raise AssertionError("bridge must read scheduler timing through one snapshot")

    @property
    def seconds_until_next_cycle(self):
        raise AssertionError("bridge must read scheduler timing through one snapshot")

    @property
    def cycle_in_progress(self):
        raise AssertionError("bridge must read scheduler timing through one snapshot")

    def timing_snapshot(self):
        self.timing_snapshot_calls += 1
        return FakeSchedulerTimingSnapshot(
            active=self.snapshot_active,
            interval_seconds=self.snapshot_interval_seconds,
            cycle_in_progress=self.snapshot_cycle_in_progress,
            seconds_until_next_cycle=self.snapshot_seconds_until_next_cycle,
            configured_idle_seconds=self.snapshot_configured_idle_seconds,
            last_cycle_duration_seconds=self.snapshot_last_cycle_duration_seconds,
            last_cycle_start_interval_seconds=self.snapshot_last_cycle_start_interval_seconds,
            cycle_duration_sample_count=self.snapshot_cycle_duration_sample_count,
            cycle_duration_p95_seconds=self.snapshot_cycle_duration_p95_seconds,
            cycle_start_interval_sample_count=self.snapshot_cycle_start_interval_sample_count,
            cycle_start_interval_p95_seconds=self.snapshot_cycle_start_interval_p95_seconds,
            current_action=self.snapshot_current_action,
            last_cycle_completed_at=self.snapshot_last_cycle_completed_at,
        )

    def resume(self):
        self.resume_calls += 1
        self.snapshot_active = True

    def suspend(self):
        self.suspend_calls += 1
        self.snapshot_active = False

    def validate_current_credential_scope(self):
        self.validate_current_credential_scope_calls += 1
        if self.credential_binding_pending:
            raise PermissionError("credential bootstrap is pending")
        if not self.current_credential_scope_authorized:
            raise PermissionError(
                "saved credential scope changed; restore the bound scope"
            )

    def validate_candidate_credential_scope(self, candidate_fingerprint):
        self.validated_credential_candidates.append(candidate_fingerprint)
        if (
            not self.credential_binding_pending
            and self.expected_credential_fingerprint
            and candidate_fingerprint != self.expected_credential_fingerprint
        ):
            raise PermissionError(
                "saved credential scope changed; reinstall the Windows service"
            )

    def bind_saved_credential_scope(self, candidate_fingerprint):
        self.bind_saved_credential_scope_calls += 1
        self.bound_credential_candidates.append(candidate_fingerprint)
        self.expected_credential_fingerprint = candidate_fingerprint
        self.credential_binding_pending = False
        return True


def write_live_order_approval_env(path: Path, account_no: str = "12345678") -> None:
    path.write_text(
        "\n".join(
            [
                "KIS_LIVE_APP_KEY=live-bridge-key",
                "KIS_LIVE_APP_SECRET=live-bridge-secret",
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
                "KIS_LIVE_APP_KEY=live-bridge-key",
                "KIS_LIVE_APP_SECRET=live-bridge-secret",
                f"KIS_LIVE_ACCOUNT_NO={account_no}",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
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
    def __init__(self, snapshot=None):
        self._snapshot = snapshot or FakeSnapshot(positions={})

    def account_snapshot(self, *, timestamp=None):
        return self._snapshot

    def inquire_daily_orders(self, **_kwargs):
        return {"output1": []}


def make_approved_live_broker(
    root: Path,
    *,
    account_no: str = "12345678",
    include_managed_ledger: bool = True,
    include_manual_reconciliation_store: bool = True,
) -> LiveBroker:
    product_code = "01"
    scope = managed_live_position_ledger_scope(account_no, product_code)
    env = {
        "KIS_LIVE_APP_KEY": "live-bridge-key",
        "KIS_LIVE_APP_SECRET": "live-bridge-secret",
        "KIS_LIVE_ACCOUNT_NO": account_no,
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": product_code,
        "STOCKBOT_ALLOW_LIVE_TRADING": "true",
        "STOCKBOT_LIVE_TRADING_ENABLED": "true",
        "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
        "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": account_no[-2:],
    }
    client = FakeLiveClient()
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
        session_approved=True,
        account_confirmation=account_no[-2:],
        expected_account_suffix=account_no[-2:],
        fill_reconciler=KisLiveOrderReconciler(client),
        pending_order_store=JsonPendingLiveOrderStore(
            root / f"pending_live_orders_{scope}.json",
            scope=scope,
        ),
        manual_reconciliation_store=manual_store,
        managed_position_ledger=managed_ledger,
        risk_limits_ok=True,
        new_entries_allowed=True,
    )


def make_running_live_controller(root: Path) -> tuple[DashboardController, FakeRuntime]:
    env_file = root / ".env"
    write_live_order_approval_env(env_file)
    runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
    runtime.execution_mode = "live"
    runtime.broker = make_approved_live_broker(root)
    controller = DashboardController(
        services=DashboardServices(runtime=runtime),
        env_file=str(env_file),
    )
    controller.select_trading_mode("real")
    controller._live_order_safety_context.approve_session()
    controller._live_runtime_readiness_ready = True
    controller._runtime_running = True
    return controller, runtime


class FakeRuntime:
    def __init__(self, data_source_kind="kis-vts", data_source_label="KIS VTS quote / paper fills"):
        self.broker = FakeBroker()
        self.status = RuntimeStatus(label="running", running=False)
        self.data_source_label = data_source_label
        self.data_source_kind = data_source_kind
        self.symbols = ["005930", "000660", "035420", "051910", "005380"]
        self.scan_limit_per_cycle = KIS_INTRADAY_REHEARSAL_SCAN_LIMIT
        self.max_bar_requests_per_cycle = KIS_INTRADAY_REHEARSAL_SCAN_LIMIT
        self.cycle_count = 7
        self.settings = CustomStrategySettings.default()
        self.strategy = FlowScalperStrategy(
            FlowScalperConfig(
                min_signal_confidence=Decimal("0.25"),
                min_momentum_pct=Decimal("0"),
                min_volume_ratio=Decimal("0"),
                require_vwap_alignment=False,
            )
        )
        self.risk_manager = RiskManager(
            RiskConfig(max_positions=KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, max_order_amount=Decimal("0"))
        )
        self.performance_metrics = {"win_rate": Decimal("0.5")}
        self.started = False
        self.paused = False

    def start(self):
        self.started = True
        self.status = RuntimeStatus(label="running", running=True)
        return RuntimeEvent.system("runtime started", timestamp=datetime(2026, 6, 11, 9, 1))

    def pause(self):
        self.paused = True
        self.status = RuntimeStatus(label="paused", running=False)
        return RuntimeEvent.system("runtime paused", timestamp=datetime(2026, 6, 11, 9, 2))

    def run_cycle(self):
        return [
            RuntimeEvent.trade(
                symbol="005930",
                company_name="삼성전자",
                side="BUY",
                quantity=4,
                price=Decimal("71400"),
                reason="flow_score_100",
                result="filled",
                timestamp=datetime(2026, 6, 11, 9, 3),
            )
        ]


class SlowActionController(DashboardController):
    def __init__(self):
        super().__init__()
        self.active_actions = 0
        self.max_active_actions = 0
        self._test_lock = threading.Lock()

    def start_paper_runtime(self):
        with self._test_lock:
            self.active_actions += 1
            self.max_active_actions = max(self.max_active_actions, self.active_actions)
        time.sleep(0.1)
        with self._test_lock:
            self.active_actions -= 1
        return self.state


class ReadOnlyKisCheckController(DashboardController):
    def __init__(self):
        super().__init__()
        self.kis_check_ran = False
        self.kis_live_check_ran = False

    def run_kis_check(self):
        self.kis_check_ran = True
        return self.state

    def run_kis_live_check(self, **_kwargs):
        self.kis_live_check_ran = True
        return self.state


class SlowKisCheckController(DashboardController):
    def __init__(self):
        super().__init__()
        self.in_check = threading.Event()
        self.release_check = threading.Event()

    def run_kis_check(self):
        self.in_check.set()
        self.release_check.wait(2)
        return self.state


class SlowCycleRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.in_cycle = threading.Event()
        self.release_cycle = threading.Event()

    def run_cycle(self):
        self.in_cycle.set()
        self.release_cycle.wait(2)
        return []


class ClearingRuntime(FakeRuntime):
    def run_cycle(self):
        self.broker.positions = {}
        return [
            RuntimeEvent.trade(
                symbol="005930",
                company_name="삼성전자",
                side="SELL",
                quantity=4,
                price=Decimal("71400"),
                reason="cleanup exit",
                result="filled",
                timestamp=datetime(2026, 6, 11, 9, 4),
            )
        ]


class ElectronBridgeSerializationTest(unittest.TestCase):
    def test_view_model_exposes_backend_service_scheduler_ownership(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        scheduler = FakeSchedulerControl()

        view = dashboard_state_to_view_model(
            controller,
            scheduler_owner="service",
            scheduler_control=scheduler,
        )

        self.assertEqual("service", view["runtime"]["schedulerOwner"])
        self.assertTrue(view["runtime"]["schedulerActive"])
        self.assertEqual(scheduler.cycle_label, view["runtime"]["cycleLabel"])
        self.assertEqual(60.0, view["runtime"]["schedulerIntervalSeconds"])
        self.assertEqual(42.25, view["runtime"]["schedulerSecondsUntilNextCycle"])
        self.assertTrue(view["runtime"]["schedulerCycleInProgress"])
        self.assertEqual(15.0, view["runtime"]["schedulerConfiguredIdleSeconds"])
        self.assertEqual(12.5, view["runtime"]["schedulerLastCycleDurationSeconds"])
        self.assertEqual(27.5, view["runtime"]["schedulerLastCycleStartIntervalSeconds"])
        self.assertEqual(12, view["runtime"]["schedulerCycleDurationSampleCount"])
        self.assertEqual(18.25, view["runtime"]["schedulerCycleDurationP95Seconds"])
        self.assertEqual(11, view["runtime"]["schedulerCycleStartIntervalSampleCount"])
        self.assertEqual(33.5, view["runtime"]["schedulerCycleStartIntervalP95Seconds"])
        self.assertEqual("cycle_completed", view["runtime"]["schedulerCurrentAction"])
        self.assertEqual(
            "2026-07-30T01:02:03+00:00",
            view["runtime"]["schedulerLastCycleCompletedAt"],
        )
        self.assertEqual(
            {
                "configuredIdleSeconds": 15.0,
                "lastCycleDurationSeconds": 12.5,
                "lastCycleStartIntervalSeconds": 27.5,
                "cycleDurationSampleCount": 12,
                "cycleDurationP95Seconds": 18.25,
                "cycleStartIntervalSampleCount": 11,
                "cycleStartIntervalP95Seconds": 33.5,
                "currentAction": "cycle_completed",
                "lastCycleCompletedAt": "2026-07-30T01:02:03+00:00",
            },
            view["debug"]["scheduler"],
        )
        self.assertEqual(1, scheduler.timing_snapshot_calls)
        self.assertEqual(2, view["runtime"]["schedulerFailureCount"])
        self.assertEqual("runtime_cycle", view["runtime"]["schedulerErrorStage"])
        self.assertEqual("TimeoutError", view["runtime"]["schedulerErrorCode"])

    def test_view_model_reads_inactive_service_state_from_timing_snapshot(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        scheduler = FakeSchedulerControl()
        scheduler.snapshot_active = False
        scheduler.snapshot_cycle_in_progress = False
        scheduler.snapshot_seconds_until_next_cycle = None

        view = dashboard_state_to_view_model(
            controller,
            scheduler_owner="service",
            scheduler_control=scheduler,
        )

        self.assertFalse(view["runtime"]["schedulerActive"])
        self.assertFalse(view["runtime"]["schedulerCycleInProgress"])
        self.assertIsNone(view["runtime"]["schedulerSecondsUntilNextCycle"])
        self.assertEqual(1, scheduler.timing_snapshot_calls)

    def test_view_model_rejects_non_numeric_scheduler_timing_values(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        scheduler = FakeSchedulerControl()
        scheduler.snapshot_interval_seconds = "KIS_LIVE_APP_SECRET=must-not-leak"
        scheduler.snapshot_seconds_until_next_cycle = "account=12345678"
        scheduler.snapshot_configured_idle_seconds = "KIS_LIVE_APP_SECRET=must-not-leak"
        scheduler.snapshot_last_cycle_duration_seconds = "account=12345678"
        scheduler.snapshot_last_cycle_start_interval_seconds = "token=must-not-leak"
        scheduler.snapshot_cycle_duration_sample_count = "account=12345678"
        scheduler.snapshot_cycle_duration_p95_seconds = "token=must-not-leak"
        scheduler.snapshot_cycle_start_interval_sample_count = "account=12345678"
        scheduler.snapshot_cycle_start_interval_p95_seconds = "token=must-not-leak"
        scheduler.snapshot_current_action = "KIS_LIVE_APP_SECRET=must-not-leak"
        scheduler.snapshot_last_cycle_completed_at = "account=12345678"

        view = dashboard_state_to_view_model(
            controller,
            scheduler_owner="service",
            scheduler_control=scheduler,
        )

        self.assertIsNone(view["runtime"]["schedulerIntervalSeconds"])
        self.assertIsNone(view["runtime"]["schedulerSecondsUntilNextCycle"])
        self.assertIsNone(view["runtime"]["schedulerConfiguredIdleSeconds"])
        self.assertIsNone(view["runtime"]["schedulerLastCycleDurationSeconds"])
        self.assertIsNone(view["runtime"]["schedulerLastCycleStartIntervalSeconds"])
        self.assertIsNone(view["runtime"]["schedulerCycleDurationSampleCount"])
        self.assertIsNone(view["runtime"]["schedulerCycleDurationP95Seconds"])
        self.assertIsNone(view["runtime"]["schedulerCycleStartIntervalSampleCount"])
        self.assertIsNone(view["runtime"]["schedulerCycleStartIntervalP95Seconds"])
        self.assertEqual("", view["runtime"]["schedulerCurrentAction"])
        self.assertEqual("", view["runtime"]["schedulerLastCycleCompletedAt"])
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("must-not-leak", rendered)
        self.assertNotIn("12345678", rendered)

    def test_view_model_rejects_invalid_scheduler_timing_numbers(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        invalid_values = (
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            -0.01,
        )

        for invalid_value in invalid_values:
            with self.subTest(invalid_value=invalid_value):
                scheduler = FakeSchedulerControl()
                scheduler.snapshot_interval_seconds = invalid_value
                scheduler.snapshot_seconds_until_next_cycle = invalid_value

                view = dashboard_state_to_view_model(
                    controller,
                    scheduler_owner="service",
                    scheduler_control=scheduler,
                )

                self.assertIsNone(view["runtime"]["schedulerIntervalSeconds"])
                self.assertIsNone(view["runtime"]["schedulerSecondsUntilNextCycle"])

    def test_view_model_rejects_non_boolean_scheduler_cycle_state(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        scheduler = FakeSchedulerControl()
        scheduler.snapshot_active = "account=12345678"
        scheduler.snapshot_cycle_in_progress = "KIS_LIVE_APP_SECRET=must-not-leak"

        view = dashboard_state_to_view_model(
            controller,
            scheduler_owner="service",
            scheduler_control=scheduler,
        )

        self.assertFalse(view["runtime"]["schedulerActive"])
        self.assertFalse(view["runtime"]["schedulerCycleInProgress"])
        self.assertNotIn("must-not-leak", json.dumps(view, ensure_ascii=False))
        self.assertNotIn("12345678", json.dumps(view, ensure_ascii=False))

    def test_view_model_state_read_does_not_reconcile_live_orders(self):
        with TemporaryDirectory() as tmpdir:
            controller, runtime = make_running_live_controller(Path(tmpdir))

            with patch.object(runtime.broker, "sync_pending_order_statuses") as reconcile:
                view = dashboard_state_to_view_model(controller)

            reconcile.assert_not_called()
            self.assertTrue(view["notice"]["orderEnabled"])

    def test_view_model_disables_entry_notice_when_cycle_has_pending_buy_block(self):
        with TemporaryDirectory() as tmpdir:
            controller, runtime = make_running_live_controller(Path(tmpdir))
            runtime._cycle_new_entries_blocked_for_live_pending_order = True

            view = dashboard_state_to_view_model(controller)

            self.assertFalse(view["notice"]["orderEnabled"])

    def test_real_account_labels_deposit_cash_separately_from_buying_power(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        controller.state = replace(
            controller.state,
            trading_mode="real",
            account=replace(
                controller.state.account,
                cash="1,200,000원",
                buying_power="750,000원",
            ),
        )

        view = dashboard_state_to_view_model(controller)

        metrics = {metric["label"]: metric["value"] for metric in view["account"]["metrics"]}
        self.assertEqual("1,200,000원", metrics["예수금"])
        self.assertEqual("750,000원", metrics["매수 가능"])
        self.assertNotIn("현금", metrics)

    def test_view_model_uses_korean_ui_contract_without_secret_values(self):
        controller = DashboardController(
            services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"})
        )
        controller.start_paper_runtime()
        controller.run_paper_cycle()
        controller.select_position("005930")

        view = dashboard_state_to_view_model(controller)

        self.assertEqual(0, view["stateRevision"])
        self.assertEqual("개미親주식", view["app"]["title"])
        self.assertEqual("virtual", view["mode"]["key"])
        self.assertTrue(view["runtime"]["running"])
        self.assertEqual("KIS VTS quote / paper fills", view["runtime"]["dataSource"])
        self.assertEqual("kis-vts", view["runtime"]["dataSourceKind"])
        self.assertEqual("KIS 장중 가상", view["runtime"]["dataModeLabel"])
        self.assertIn("현재가", view["runtime"]["dataModeDescription"])
        self.assertEqual(
            "실제 주문 없음 · 후보군 5종목 · cycle당 조회 최대 10종목 · 최대 보유 10종목",
            view["runtime"]["safetySummary"],
        )
        self.assertFalse(view["mode"]["isReal"])
        self.assertFalse(view["notice"]["orderEnabled"])
        self.assertIn("KIS VTS quote", view["notice"]["description"])
        self.assertEqual("계좌 상태", view["account"]["title"])
        account_metric_labels = {metric["label"] for metric in view["account"]["metrics"]}
        self.assertIn("현금", account_metric_labels)
        self.assertNotIn("예수금", account_metric_labels)
        self.assertEqual("삼성전자", view["positions"][0]["companyName"])
        self.assertEqual("005930", view["selectedPosition"]["symbol"])
        self.assertNotIn("advisor", view)
        self.assertEqual(7, view["debug"]["cycle"]["id"])
        self.assertTrue(view["debug"]["cycle"]["running"])
        self.assertEqual("kis-vts", view["debug"]["runtime"]["dataSourceKind"])
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, view["debug"]["runtime"]["scanLimitPerCycle"])
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, view["debug"]["runtime"]["maxBarRequestsPerCycle"])
        self.assertEqual(5, view["debug"]["runtime"]["symbolCount"])
        effective_policy = view["debug"]["effectivePolicy"]
        self.assertNotIn("profile", effective_policy)
        self.assertEqual("0.25", effective_policy["strategyConfig"]["min_signal_confidence"])
        self.assertEqual("0", effective_policy["strategyConfig"]["min_momentum_pct"])
        self.assertFalse(effective_policy["strategyConfig"]["require_vwap_alignment"])
        self.assertEqual(
            KIS_INTRADAY_REHEARSAL_MAX_POSITIONS,
            effective_policy["riskConfig"]["max_positions"],
        )
        self.assertEqual("0", effective_policy["riskConfig"]["max_order_amount"])
        self.assertEqual("0.5", view["debug"]["performance"]["win_rate"])
        rendered = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("selectedProfile", rendered)
        self.assertNotIn("customSettings", rendered)
        self.assertNotIn("cashAllocationPct", rendered)
        self.assertNotIn("effectiveStrategy", rendered)
        self.assertNotIn("KIS_VTS_APP_SECRET", rendered)
        self.assertNotIn("12345678", rendered)

    def test_view_model_describes_external_scan_kis_as_hybrid_final_quote_mode(self):
        runtime = FakeRuntime(data_source_kind="external-scan-kis", data_source_label="wide scanner / KIS final quote paper")
        runtime.symbols = ["005930", "000660", "035420", "051910", "005380", "012330"]
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        view = dashboard_state_to_view_model(controller)

        self.assertEqual("external-scan-kis", view["runtime"]["dataSourceKind"])
        self.assertEqual("KIS 하이브리드 테스트", view["runtime"]["dataModeLabel"])
        self.assertEqual(
            "넓은 후보군을 먼저 선별하고, 주문 직전 KIS 현재가로 최종 확인합니다.",
            view["runtime"]["dataModeDescription"],
        )
        self.assertEqual(
            "실제 주문 없음 · 후보군 6종목 · 스캐너 선별 · KIS 현재가 최종 확인",
            view["runtime"]["safetySummary"],
        )
        self.assertEqual("external-scan-kis", view["debug"]["runtime"]["dataSourceKind"])

    def test_view_model_preserves_specific_runtime_status(self):
        controller = DashboardController(
            services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"})
        )
        controller.state = replace(controller.state, runtime_status="장 대기")

        view = dashboard_state_to_view_model(controller)

        self.assertEqual("장 대기", view["runtime"]["status"])
        self.assertIn("장 대기", [metric["value"] for metric in view["account"]["metrics"]])

    def test_trade_log_view_model_includes_structured_trade_fields(self):
        controller = DashboardController(
            services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"})
        )
        controller.state = replace(
            controller.state,
            trade_log=(
                build_trade_log_entry(
                    RuntimeEvent.trade(
                        symbol="005930",
                        company_name="삼성전자",
                        side="SELL",
                        quantity=2,
                        price=Decimal("71000"),
                        reason="take_profit",
                        result="filled",
                        realized_pnl=Decimal("2000"),
                        timestamp=datetime(2026, 6, 11, 9, 30),
                    )
                ),
            ),
        )

        view = dashboard_state_to_view_model(controller)
        trade = view["logs"]["trades"][0]

        self.assertEqual("005930", trade["symbol"])
        self.assertEqual("삼성전자", trade["companyName"])
        self.assertEqual("SELL", trade["side"])
        self.assertEqual("매도", trade["sideLabel"])
        self.assertEqual(2, trade["quantity"])
        self.assertEqual(71000.0, trade["price"])
        self.assertEqual("71,000원", trade["priceText"])
        self.assertEqual("take_profit", trade["reason"])
        self.assertEqual("filled", trade["result"])
        self.assertEqual(2000.0, trade["realizedPnl"])
        self.assertEqual("2,000원", trade["realizedPnlText"])

    def test_debug_view_model_includes_full_runtime_trade_history(self):
        runtime = FakeRuntime()
        runtime.events = [
            RuntimeEvent.system(
                "scanner_diagnostic - external_scan_cycle: candidates=10 entry_candidates=2",
                timestamp=datetime(2026, 6, 11, 9, 0),
            )
        ] + [
            RuntimeEvent.trade(
                symbol=f"{index:06d}",
                company_name=f"Company {index}",
                side="BUY" if index % 2 else "SELL",
                quantity=index,
                price=Decimal("1000") + Decimal(index),
                reason=f"reason_{index}",
                result="filled",
                realized_pnl=Decimal(index - 8),
                timestamp=datetime(2026, 6, 11, 9, index % 60),
            )
            for index in range(1, 17)
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(
            controller.state,
            trade_log=(build_trade_log_entry(runtime.events[1]),),
        )

        view = dashboard_state_to_view_model(controller)

        self.assertEqual(1, len(view["logs"]["trades"]))
        self.assertEqual(16, len(view["debug"]["fullTradeLogs"]))
        self.assertEqual(17, len(view["debug"]["fullRuntimeEvents"]))
        self.assertEqual("system", view["debug"]["fullRuntimeEvents"][0]["kind"])
        self.assertIn("scanner_diagnostic", view["debug"]["fullRuntimeEvents"][0]["message"])
        self.assertEqual("000001", view["debug"]["fullTradeLogs"][0]["symbol"])
        self.assertEqual("000016", view["debug"]["fullTradeLogs"][-1]["symbol"])
        self.assertEqual(16, view["debug"]["runtime"]["fullTradeLogCount"])
        self.assertEqual(17, view["debug"]["runtime"]["rawRuntimeEventCount"])
        self.assertEqual(16, view["debug"]["runtime"]["rawTradeEventCount"])
        self.assertEqual(1, view["debug"]["runtime"]["rawSystemEventCount"])
        self.assertEqual(0, view["debug"]["runtime"]["fullTradeLogSkippedCount"])
        self.assertEqual(0, view["debug"]["runtime"]["fullRuntimeEventSkippedCount"])
        self.assertTrue(view["debug"]["runtime"]["runtimeEventStoreReadable"])

    def test_debug_view_model_includes_root_cause_diagnostic_analysis(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.system(
                "scanner_diagnostic - external_scan_cycle: "
                "candidates=2566, selected=2041, processed=2041, sparse_candidates=155, "
                "history_ready_candidates=18, history_fallback_candidates=137, "
                "history_failures=insufficient_count:120,latest_mismatch:10,gap:5,provider_exception:2, "
                "confirmation_candidates=155, "
                "confirmation_reasons=scanner_quote_missing:155, "
                "final_quotes=10/10, physical_reads=12/14, confirmed=0, holds=2031, entry_candidates=141, "
                "entry_fills=0, entry_deferred=135, entry_capacity_stop=0, "
                "planner_phase=monitoring, entry_slot_capacity=0, open_target_slots=6, "
                "exact_zero_cooldown_active=1, exact_zero_cooldown_retry_after_seconds=123.4, "
                "prescan_rejections=entry_unaffordable:525, "
                "hold_reasons=volume_expansion:1647,downward_momentum:1539",
                timestamp=datetime(2026, 7, 8, 14, 16),
            ),
            RuntimeEvent.trade(
                symbol="009270",
                company_name="Symbol A",
                side="BUY",
                quantity=7,
                price=Decimal("997"),
                reason="live_order_pending",
                result="rejected",
                mode="live",
                timestamp=datetime(2026, 7, 8, 14, 16, 1),
            ),
            RuntimeEvent.trade(
                symbol="040300",
                company_name="Symbol B",
                side="BUY",
                quantity=3,
                price=Decimal("2005"),
                reason="live_pending_orders_unresolved",
                result="rejected",
                mode="live",
                timestamp=datetime(2026, 7, 8, 14, 16, 2),
            ),
            RuntimeEvent.trade(
                symbol="001234",
                company_name="Symbol C",
                side="BUY",
                quantity=1,
                price=Decimal("8250"),
                reason="order_failure_limit_reached",
                result="rejected",
                mode="live",
                timestamp=datetime(2026, 7, 8, 14, 16, 3),
            ),
            RuntimeEvent.system(
                "live_pending_orders_unresolved - pending_count=1, symbols=040300",
                timestamp=datetime(2026, 7, 8, 14, 17),
            ),
            RuntimeEvent.system(
                "KIS HTTP 500: EGW00215 ledger rate limit exceeded",
                timestamp=datetime.now(),
            ),
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(
            controller.state,
            trading_mode="real",
            trade_log=tuple(build_trade_log_entry(event) for event in runtime.events if event.kind == "trade"),
        )

        view = dashboard_state_to_view_model(controller)
        analysis = view["debug"]["analysis"]

        self.assertEqual(1, analysis["schemaVersion"])
        self.assertEqual("live", analysis["snapshot"]["dataSourceKind"])
        self.assertEqual(3, analysis["reasonCounts"]["rejectedModes"]["live"])
        self.assertEqual(1, analysis["reasonCounts"]["rejectedReasons"]["live_order_pending"])
        self.assertEqual(1, analysis["reasonCounts"]["rejectedReasons"]["live_pending_orders_unresolved"])
        self.assertEqual(1, analysis["reasonCounts"]["rejectedReasons"]["order_failure_limit_reached"])
        self.assertEqual(["040300"], analysis["liveOrderBlockers"]["pendingSymbols"])
        self.assertEqual(141, analysis["latestScannerCycles"][0]["entryCandidates"])
        self.assertEqual(0, analysis["latestScannerCycles"][0]["entryFills"])
        self.assertEqual(12, analysis["latestScannerCycles"][0]["physicalReadRequests"])
        self.assertEqual(14, analysis["latestScannerCycles"][0]["physicalReadCap"])
        self.assertEqual(18, analysis["latestScannerCycles"][0]["historyReadyCandidates"])
        self.assertEqual(137, analysis["latestScannerCycles"][0]["historyFallbackCandidates"])
        self.assertEqual(
            {
                "insufficient_count": 120,
                "latest_mismatch": 10,
                "gap": 5,
                "provider_exception": 2,
            },
            analysis["latestScannerCycles"][0]["historyFailures"],
        )
        self.assertEqual(155, analysis["latestScannerCycles"][0]["confirmationCandidates"])
        self.assertEqual(
            {"scanner_quote_missing": 155},
            analysis["latestScannerCycles"][0]["confirmationReasons"],
        )
        self.assertEqual("monitoring", analysis["latestScannerCycles"][0]["plannerPhase"])
        self.assertEqual(0, analysis["latestScannerCycles"][0]["entrySlotCapacity"])
        self.assertTrue(analysis["latestScannerCycles"][0]["exactZeroBuyingPowerCooldownActive"])
        self.assertEqual(
            123.4,
            analysis["latestScannerCycles"][0]["exactZeroBuyingPowerRetryAfterSeconds"],
        )
        self.assertEqual(525, analysis["latestScannerCycles"][0]["prescanRejections"]["entry_unaffordable"])
        self.assertEqual(["009270", "040300", "001234"], [order["symbol"] for order in analysis["recentRejectedOrders"]])
        cause_codes = {cause["code"] for cause in analysis["inferredRootCauses"]}
        self.assertIn("historical_live_pending_order_activity", cause_codes)
        self.assertNotIn("live_pending_orders_blocking_entries", cause_codes)
        self.assertIn("order_failure_limit_reached", cause_codes)
        self.assertIn("scanner_candidates_without_fills", cause_codes)
        self.assertIn("kis_rate_limit", cause_codes)

    def test_debug_view_model_flags_unknown_live_entry_count_with_aggregate_evidence(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.trade(
                symbol="005930",
                company_name="Sensitive Company",
                side="BUY",
                quantity=2,
                price=Decimal("70000"),
                reason="live_entry_count_unknown",
                result="rejected",
                mode="live",
                timestamp=datetime(2026, 7, 13, 10, 5),
            ),
            RuntimeEvent.trade(
                symbol="000660",
                company_name="Other Sensitive Company",
                side="BUY",
                quantity=1,
                price=Decimal("200000"),
                reason="live_entry_count_unknown",
                result="rejected",
                mode="live",
                timestamp=datetime(2026, 7, 13, 10, 5, 1),
            ),
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)

        cause = next(
            cause
            for cause in view["debug"]["analysis"]["inferredRootCauses"]
            if cause["code"] == "live_entry_count_unknown"
        )
        self.assertEqual("high", cause["severity"])
        self.assertEqual({"rejectedTrades": 2}, cause["evidence"])
        self.assertIn("managed entry counts", cause["nextCheck"])
        self.assertIn("authoritative KIS same-day reconciliation", cause["nextCheck"])
        rendered_cause = json.dumps(cause, ensure_ascii=False)
        self.assertNotIn("005930", rendered_cause)
        self.assertNotIn("000660", rendered_cause)
        self.assertNotIn("Sensitive Company", rendered_cause)
        self.assertNotIn("Other Sensitive Company", rendered_cause)

    def test_debug_view_model_flags_running_live_runtime_without_completed_cycle(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.cycle_count = 0
        runtime.events = [
            RuntimeEvent.system("자동 모의투자 루프 시작 - 데이터 출처: KIS live orders / scanner"),
            RuntimeEvent.system("live_existing_positions_adopted - count=1"),
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True
        controller._with_system_log(
            "error",
            "Paper runtime",
            'cycle_exception - stage=run_cycle, error=KisApiError, detail=KIS HTTP 500: {"msg_cd":"EGW00215"}',
        )

        view = dashboard_state_to_view_model(controller)

        causes = view["debug"]["analysis"]["inferredRootCauses"]
        cause_codes = {cause["code"] for cause in causes}
        self.assertIn("kis_rate_limit", cause_codes)
        cause = next(cause for cause in causes if cause["code"] == "kis_rate_limit")
        self.assertEqual(["EGW00215"], cause["evidence"]["kisCodes"])
        cause = next(
            cause
            for cause in causes
            if cause["code"] == "runtime_cycle_not_completed_after_start"
        )
        self.assertEqual("high", cause["severity"])
        self.assertEqual(0, cause["evidence"]["cycleId"])
        self.assertEqual(2, cause["evidence"]["runtimeEventCount"])

    def test_debug_view_model_does_not_resurrect_rate_limit_outside_controller_log_window(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.system(
                "KIS HTTP 500: EGW00215 old ledger rate limit",
                timestamp=datetime.now() - timedelta(minutes=5),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        for index in range(50):
            controller._with_system_log("info", "runtime", f"later event {index}")

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertNotIn("kis_rate_limit", cause_codes)

    def test_controller_log_datetime_reconstruction_crosses_midnight(self):
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 7, 13, 9, 31, tzinfo=tz)

        logs = [
            ActivityLogEntry("success", "KIS 실전 조회 확인", "recovered", "09:30:00"),
            ActivityLogEntry("error", "Paper runtime", "EGW00215", "14:00:00"),
        ]

        with patch("stockbot.electron_bridge.datetime", FrozenDateTime):
            reconstructed = _controller_log_datetimes(logs)

        self.assertEqual(datetime(2026, 7, 13, 9, 30), reconstructed[0])
        self.assertEqual(datetime(2026, 7, 12, 14, 0), reconstructed[1])

    def test_debug_view_model_waits_for_live_first_cycle_grace_period(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.cycle_count = 0
        runtime.events = [
            RuntimeEvent.system(
                "자동 모의투자 루프 시작 - 데이터 출처: KIS live orders / scanner",
                timestamp=datetime.now() - timedelta(seconds=18),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertNotIn("runtime_cycle_not_completed_after_start", cause_codes)

    def test_debug_view_model_flags_live_first_cycle_after_grace_period(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.cycle_count = 0
        runtime.events = [
            RuntimeEvent.system(
                "자동 모의투자 루프 시작 - 데이터 출처: KIS live orders / scanner",
                timestamp=datetime.now() - timedelta(seconds=76),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertIn("runtime_cycle_not_completed_after_start", cause_codes)

    def test_debug_view_model_marks_older_controller_rate_limit_as_recovered(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.system("KIS HTTP 500: EGW00215 ledger rate limit exceeded")
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._with_system_log(
            "error",
            "KIS 실전 조회 확인",
            "KIS HTTP 500: EGW00215 ledger rate limit exceeded",
        )
        controller._with_system_log(
            "success",
            "KIS 실전 조회 확인",
            "실전 계좌 조회에 성공했습니다.",
        )

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertNotIn("kis_rate_limit", cause_codes)

    def test_debug_view_model_uses_current_limiter_state_to_clear_historical_rate_limit(self):
        class RecoveredLimiter:
            def diagnostic_snapshot(self, kind):
                return {
                    "allowed": True,
                    "reason": "allowed",
                    "retryAfterSeconds": 0,
                }

        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.rate_limiter = RecoveredLimiter()
        runtime.events = [RuntimeEvent.system("KIS HTTP 500: EGW00215 ledger rate limit exceeded")]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertNotIn("kis_rate_limit", cause_codes)

    def test_debug_view_model_does_not_apply_old_cycle_exception_to_new_start(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.cycle_count = 0
        runtime.events = [
            RuntimeEvent.system(
                "자동 모의투자 루프 시작 - 데이터 출처: KIS live orders / scanner",
                timestamp=datetime.now() - timedelta(seconds=18),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._runtime_running = True
        controller._with_system_log(
            "error",
            "Paper runtime",
            "cycle_exception - previous live run failed",
        )
        controller._with_system_log(
            "info",
            "Paper runtime",
            "자동 모의투자 루프 시작 - 데이터 출처: KIS live orders / scanner",
        )

        view = dashboard_state_to_view_model(controller)

        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}
        self.assertNotIn("runtime_cycle_not_completed_after_start", cause_codes)

    def test_debug_view_model_keeps_newer_runtime_rate_limit_active(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.system("KIS HTTP 500: EGW00201 per-second rate limit")
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._with_system_log(
            "success",
            "KIS 실전 조회 확인",
            "실전 계좌 조회에 성공했습니다.",
        )
        controller._with_system_log(
            "error",
            "Paper runtime",
            "cycle_exception - KIS HTTP 500: EGW00201 per-second rate limit",
        )

        view = dashboard_state_to_view_model(controller)

        causes = view["debug"]["analysis"]["inferredRootCauses"]
        cause_codes = {cause["code"] for cause in causes}
        self.assertIn("kis_rate_limit", cause_codes)
        cause = next(cause for cause in causes if cause["code"] == "kis_rate_limit")
        self.assertEqual(["EGW00201"], cause["evidence"]["kisCodes"])

    def test_debug_view_model_detects_rate_limit_in_trade_reject_reason(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.events = [
            RuntimeEvent.trade(
                symbol="005930",
                company_name="Sensitive Company",
                side="BUY",
                quantity=1,
                price=Decimal("70000"),
                reason='live_snapshot_failed: KIS HTTP 500: {"msg_cd":"EGW00215"}',
                result="rejected",
                mode="live",
                timestamp=datetime.now(),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)

        causes = view["debug"]["analysis"]["inferredRootCauses"]
        rate_limit_cause = next(cause for cause in causes if cause["code"] == "kis_rate_limit")
        self.assertEqual(["EGW00215"], rate_limit_cause["evidence"]["kisCodes"])
        self.assertIn("trade_rejection", rate_limit_cause["evidence"]["stages"])

    def test_debug_view_model_includes_safe_pending_live_order_summary(self):
        submitted_at = datetime.now() - timedelta(minutes=3)

        class PendingStore:
            def all(self):
                return (
                    PendingLiveOrder(
                        order_no="1234567890",
                        symbol="463480",
                        side="SELL",
                        requested_quantity=1,
                        remaining_quantity=1,
                        submitted_at=submitted_at,
                        estimated_price=Decimal("4205"),
                        reason="pending",
                        order_org_no="98765",
                    ),
                )

        class BrokerWithPending:
            pending_order_store = PendingStore()

        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime.broker = BrokerWithPending()
        runtime._cycle_paused_for_live_pending_order = False
        runtime._last_pending_live_order_sync_summary = {
            "outcome": "sell_isolated",
            "remainingCount": 1,
            "entryBlockingCount": 0,
            "isolatedSellCount": 1,
            "fillCount": 0,
            "storeUnavailable": False,
        }
        runtime.events = [
            RuntimeEvent.system(
                "live_pending_orders_unresolved - pending_count=1, symbols=463480",
                timestamp=datetime(2026, 7, 8, 15, 11),
            )
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)
        blockers = view["debug"]["analysis"]["liveOrderBlockers"]

        self.assertFalse(blockers["blockedByPendingLiveOrderSync"])
        self.assertTrue(blockers["pendingStoreReadable"])
        self.assertEqual(1, blockers["currentPendingOrderCount"])
        self.assertEqual(0, blockers["entryBlockingPendingOrderCount"])
        self.assertEqual(1, blockers["isolatedPendingSellOrderCount"])
        self.assertFalse(blockers["cyclePausedForPendingOrder"])
        self.assertEqual(runtime._last_pending_live_order_sync_summary, blockers["lastPendingOrderSync"])
        pending_order = blockers["pendingOrders"][0]
        self.assertEqual("463480", pending_order["symbol"])
        self.assertEqual("SELL", pending_order["side"])
        self.assertFalse(pending_order["entryBlocking"])
        self.assertEqual("same_symbol", pending_order["blockingScope"])
        self.assertTrue(pending_order["cancelEligibleByAge"])
        self.assertGreaterEqual(pending_order["ageSeconds"], 120)
        self.assertEqual(120, pending_order["cancelAfterSeconds"])
        self.assertFalse(pending_order["cancelRequested"])
        isolated_cause = next(
            cause
            for cause in view["debug"]["analysis"]["inferredRootCauses"]
            if cause["code"] == "live_pending_sell_isolated"
        )
        self.assertEqual(1, isolated_cause["evidence"]["isolatedPendingSellOrderCount"])
        self.assertNotIn(
            "live_pending_orders_blocking_entries",
            {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]},
        )
        rendered = json.dumps(view["debug"]["analysis"], ensure_ascii=False)
        self.assertNotIn("1234567890", rendered)
        self.assertNotIn("98765", rendered)

    def test_debug_view_model_marks_unreadable_pending_store_as_current_hard_blocker(self):
        class UnreadablePendingStore:
            def all(self):
                raise RuntimeError("pending state file unavailable account_no=12345678")

        class BrokerWithUnreadablePendingStore:
            pending_order_store = UnreadablePendingStore()

        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime.broker = BrokerWithUnreadablePendingStore()
        runtime._cycle_paused_for_live_pending_order = False
        runtime._cycle_new_entries_blocked_for_live_pending_order = False
        runtime._last_pending_live_order_sync_summary = {
            "outcome": "failed",
            "remainingCount": 0,
            "entryBlockingCount": 0,
            "isolatedSellCount": 0,
            "fillCount": 0,
            "storeUnavailable": True,
            "errorType": "RuntimeError",
            "errorDetail": "pending state file unavailable",
        }
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)
        blockers = view["debug"]["analysis"]["liveOrderBlockers"]
        causes = view["debug"]["analysis"]["inferredRootCauses"]

        self.assertTrue(blockers["blockedByPendingLiveOrderSync"])
        self.assertTrue(blockers["pendingSyncHardFailure"])
        self.assertFalse(blockers["pendingStoreReadable"])
        self.assertIn("live_pending_order_state_unavailable", {cause["code"] for cause in causes})
        self.assertNotIn("12345678", json.dumps(view["debug"]["analysis"], ensure_ascii=False))

    def test_debug_view_model_exports_cycle_allocation_and_limiter_state(self):
        class DiagnosticLimiter:
            def diagnostic_snapshot(self, kind):
                self.kind = kind
                return {
                    "allowed": False,
                    "reason": "api_backoff",
                    "retryAfterSeconds": 1.25,
                }

        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime.rate_limiter = DiagnosticLimiter()
        runtime._cycle_start_buying_power = Decimal("100000")
        runtime._cycle_entry_spent = Decimal("70000")
        runtime._cycle_entry_symbols = {"005930", "000660"}
        runtime._cycle_exit_symbols = {"035420"}
        runtime._cycle_entry_slot_target = 10
        runtime._cycle_entry_slot_capacity = 2
        runtime._cycle_entry_sizing_slots = 8
        runtime._cycle_live_planner_phase = "entry_reserved"
        runtime._next_live_planner_phase = "monitoring"
        runtime._last_live_planning_buying_power = Decimal("0")
        runtime._last_live_planning_buying_power_at = datetime(2026, 7, 21, 14, 0)
        runtime._live_exact_zero_buying_power_retry_after_seconds = lambda: 123.4
        runtime._cycle_new_entries_blocked_for_live_entry_count = True
        runtime._last_live_entry_count_sync_ready = False
        runtime.risk_manager.record_order_result(False)
        runtime._last_order_failure_class = "transient_or_preflight"
        runtime._last_order_failure_reason = "live_snapshot_failed: rate limit"
        runtime._cycle_blocked_symbols = {"005930"}
        runtime._cycle_symbol_trading_block_reasons = {
            "005930": "temporary_stop"
        }
        runtime._last_market_trading_block_reason = "temporary_stop"
        runtime._last_market_trading_block_market = "KOSDAQ"
        runtime._last_market_trading_block_symbol = "005930"
        runtime._last_market_trading_block_source = "KIS_CURRENT_PRICE"
        runtime._last_market_trading_block_at = datetime(2026, 7, 29, 12, 34)
        runtime._market_trading_block_count = 2
        runtime.broker._pending_order_batch_active = True
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        view = dashboard_state_to_view_model(controller)
        debug = view["debug"]
        internals = debug["runtimeInternals"]

        self.assertEqual(2, debug["diagnosticSchemaVersion"])
        self.assertIn("pending-order-scope", debug["diagnosticCapabilities"])
        self.assertIn("planner-phase", debug["diagnosticCapabilities"])
        self.assertIn("buying-power-probe", debug["diagnosticCapabilities"])
        self.assertIn("buying-power-planner", debug["diagnosticCapabilities"])
        self.assertNotIn("cash-allocation", debug["diagnosticCapabilities"])
        self.assertIn("market-trading-state", debug["diagnosticCapabilities"])
        self.assertEqual("100000", internals["cycleStartBuyingPower"])
        self.assertEqual("70000", internals["cycleEntrySpent"])
        self.assertEqual("30000", internals["remainingBuyingPower"])
        self.assertNotIn("allocationTargetCash", internals)
        self.assertNotIn("remainingAllocationCash", internals)
        self.assertEqual(2, internals["cycleEntrySymbolCount"])
        self.assertEqual(1, internals["cycleExitSymbolCount"])
        self.assertEqual(10, internals["entrySlotTarget"])
        self.assertEqual(2, internals["entrySlotCapacity"])
        self.assertEqual(8, internals["entrySizingSlots"])
        self.assertEqual("entry_reserved", internals["livePlannerPhase"])
        self.assertEqual("monitoring", internals["nextLivePlannerPhase"])
        self.assertTrue(internals["livePlannerPhaseEligible"])
        self.assertEqual("zero", internals["liveBuyingPowerProbe"]["lastExactState"])
        self.assertTrue(internals["liveBuyingPowerProbe"]["cooldownActive"])
        self.assertEqual(123.4, internals["liveBuyingPowerProbe"]["retryAfterSeconds"])
        self.assertTrue(internals["cycleNewEntriesBlockedForEntryCount"])
        self.assertFalse(internals["entryCountSyncReady"])
        self.assertEqual(1, internals["orderFailureState"]["consecutiveFailures"])
        self.assertEqual(
            runtime.risk_manager.config.max_consecutive_order_failures,
            internals["orderFailureState"]["failureLimit"],
        )
        self.assertEqual("transient_or_preflight", internals["orderFailureState"]["lastClass"])
        self.assertIn("live_snapshot_failed", internals["orderFailureState"]["lastReason"])
        self.assertEqual(["005930"], internals["marketTradingState"]["blockedSymbolsThisCycle"])
        self.assertEqual(
            {"005930": "temporary_stop"},
            internals["marketTradingState"]["blockedReasonsThisCycle"],
        )
        self.assertEqual("SECURITY_HALT", internals["marketTradingState"]["currentState"])
        self.assertEqual("SYMBOL", internals["marketTradingState"]["scope"])
        self.assertEqual("temporary_stop", internals["marketTradingState"]["lastReason"])
        self.assertEqual("KOSDAQ", internals["marketTradingState"]["lastMarket"])
        self.assertEqual("005930", internals["marketTradingState"]["lastSymbol"])
        self.assertEqual("KIS_CURRENT_PRICE", internals["marketTradingState"]["source"])
        self.assertEqual("2026-07-29T12:34:00", internals["marketTradingState"]["observedAt"])
        self.assertEqual("AWAITING_FRESH_STATE", internals["marketTradingState"]["recoveryPhase"])
        self.assertEqual(2, internals["marketTradingState"]["totalBlocks"])
        self.assertTrue(internals["pendingOrderBatchActive"])
        self.assertEqual("api_backoff", internals["rateLimiter"]["reason"])
        self.assertEqual("kis_live_mutation", runtime.rate_limiter.kind)

    def test_debug_market_state_preserves_per_symbol_block_reasons(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime._cycle_blocked_symbols = {"STOP01", "UNKNOWN1"}
        runtime._cycle_symbol_trading_block_reasons = {
            "STOP01": "temporary_stop",
            "UNKNOWN1": "trading_state_unknown",
        }
        runtime._last_market_trading_block_reason = "trading_state_unknown"
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        state = dashboard_state_to_view_model(controller)["debug"]["runtimeInternals"][
            "marketTradingState"
        ]

        self.assertEqual("MIXED", state["currentState"])
        self.assertEqual(
            {
                "STOP01": "temporary_stop",
                "UNKNOWN1": "trading_state_unknown",
            },
            state["blockedReasonsThisCycle"],
        )

    def test_debug_view_model_classifies_market_state_deferral_as_non_program_failure(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime._cycle_blocked_symbols = {"005930"}
        runtime._last_market_trading_block_reason = "temporary_stop"
        runtime._last_market_trading_block_market = "KOSDAQ"
        runtime._last_market_trading_block_symbol = "005930"
        runtime._last_market_trading_block_source = "KIS_CURRENT_PRICE"
        runtime._last_market_trading_block_at = datetime(2026, 7, 29, 12, 34)
        runtime._market_trading_block_count = 1
        runtime.events = [
            RuntimeEvent.system(
                "market_trading_deferred - symbol=005930, market=KOSDAQ, "
                "reason=temporary_stop, scope=symbol, retry=next_cycle"
            ),
            RuntimeEvent.system(
                "scanner_diagnostic - external_scan_cycle: "
                "candidates=2, selected=2, processed=2, sparse_candidates=0, "
                "final_quotes=2/10, physical_reads=2/14, confirmed=2, holds=0, "
                "entry_candidates=2, entry_fills=0, entry_deferred=2, "
                "entry_capacity_stop=0, planner_phase=monitoring, "
                "entry_slot_capacity=2, open_target_slots=2"
            ),
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        analysis = dashboard_state_to_view_model(controller)["debug"]["analysis"]
        causes = {
            cause["code"]: cause
            for cause in analysis["inferredRootCauses"]
        }

        self.assertEqual(
            1,
            analysis["reasonCounts"]["systemCategories"]["market_trading_state"],
        )
        self.assertIn("market_trading_temporarily_deferred", causes)
        self.assertEqual(
            "low",
            causes["market_trading_temporarily_deferred"]["severity"],
        )
        self.assertIn("scanner_candidates_without_fills", causes)

    def test_historical_market_state_event_does_not_hide_current_no_fill_diagnostic(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime._cycle_blocked_symbols = set()
        runtime.events = [
            RuntimeEvent.system(
                "market_trading_deferred - symbol=005930, market=KOSDAQ, "
                "reason=temporary_stop, scope=symbol, retry=next_cycle"
            ),
            RuntimeEvent.system(
                "scanner_diagnostic - external_scan_cycle: "
                "candidates=10, selected=10, processed=10, sparse_candidates=0, "
                "final_quotes=2/10, physical_reads=2/14, confirmed=2, holds=0, "
                "entry_candidates=2, entry_fills=0, entry_deferred=2, "
                "entry_capacity_stop=0, planner_phase=monitoring, "
                "entry_slot_capacity=2, open_target_slots=2"
            ),
        ]
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        causes = {
            cause["code"]
            for cause in dashboard_state_to_view_model(controller)["debug"]["analysis"][
                "inferredRootCauses"
            ]
        }

        self.assertIn("scanner_candidates_without_fills", causes)
        self.assertNotIn("market_trading_temporarily_deferred", causes)

    def test_debug_view_model_marks_unstarted_live_planner_ineligible(self):
        runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
        runtime.execution_mode = "live"
        runtime._cycle_live_planner_phase = None
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")

        internals = dashboard_state_to_view_model(controller)["debug"]["runtimeInternals"]

        self.assertEqual("not_started", internals["livePlannerPhase"])
        self.assertFalse(internals["livePlannerPhaseEligible"])

    def test_debug_view_model_flags_controller_live_readiness_pending_blocker(self):
        runtime = FakeRuntime(data_source_kind="real-prep", data_source_label="KIS live account")
        runtime.events = []
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._with_system_log(
            "warning",
            "Live readiness",
            "Live readiness blocked: pending live order requires reconciliation before live readiness: "
            "count=1 symbols=005930",
        )

        view = dashboard_state_to_view_model(controller)
        blockers = view["debug"]["analysis"]["liveOrderBlockers"]
        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}

        self.assertTrue(blockers["blockedByPendingLiveOrderSync"])
        self.assertEqual(1, blockers["pendingEventCount"])
        self.assertEqual(["005930"], blockers["pendingSymbols"])
        self.assertIn("live_pending_orders_blocking_entries", cause_codes)

    def test_debug_view_model_flags_controller_live_runtime_pending_blocker(self):
        runtime = FakeRuntime(data_source_kind="real-prep", data_source_label="KIS live account")
        runtime.events = []
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._with_system_log(
            "warning",
            "Live runtime",
            "Live runtime order gate blocked: live pending orders unresolved: 1",
        )

        view = dashboard_state_to_view_model(controller)
        blockers = view["debug"]["analysis"]["liveOrderBlockers"]
        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}

        self.assertTrue(blockers["blockedByPendingLiveOrderSync"])
        self.assertEqual(1, blockers["pendingEventCount"])
        self.assertIn("live_pending_orders_blocking_entries", cause_codes)

    def test_debug_view_model_does_not_keep_resolved_controller_pending_warning_active(self):
        runtime = FakeRuntime(data_source_kind="real-prep", data_source_label="KIS live account")
        runtime.events = []
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        controller.state = replace(controller.state, trading_mode="real")
        controller._with_system_log(
            "warning",
            "Live readiness",
            "Live readiness blocked: pending live order requires reconciliation before live readiness: "
            "count=1 symbols=005930",
        )
        controller._live_runtime_readiness_ready = True
        controller._with_system_log(
            "success",
            "Live readiness",
            "Live readiness check passed. This check did not enable or submit live orders.",
        )

        view = dashboard_state_to_view_model(controller)
        blockers = view["debug"]["analysis"]["liveOrderBlockers"]
        cause_codes = {cause["code"] for cause in view["debug"]["analysis"]["inferredRootCauses"]}

        self.assertFalse(blockers["blockedByPendingLiveOrderSync"])
        self.assertEqual(1, blockers["historicalPendingEventCount"])
        self.assertIn("historical_live_pending_order_activity", cause_codes)
        self.assertNotIn("live_pending_orders_blocking_entries", cause_codes)

    def test_debug_view_model_skips_malformed_runtime_trade_history(self):
        class MalformedTradeEvent:
            kind = "trade"

        runtime = FakeRuntime()
        runtime.events = 123
        controller = DashboardController(services=DashboardServices(runtime=runtime))

        non_iterable_view = dashboard_state_to_view_model(controller)

        self.assertEqual([], non_iterable_view["debug"]["fullTradeLogs"])
        self.assertEqual([], non_iterable_view["debug"]["fullRuntimeEvents"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["fullTradeLogCount"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["rawRuntimeEventCount"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["rawTradeEventCount"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["rawSystemEventCount"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["fullTradeLogSkippedCount"])
        self.assertEqual(0, non_iterable_view["debug"]["runtime"]["fullRuntimeEventSkippedCount"])
        self.assertFalse(non_iterable_view["debug"]["runtime"]["runtimeEventStoreReadable"])

        runtime.events = [
            MalformedTradeEvent(),
            RuntimeEvent.trade(
                symbol="005930",
                company_name="Samsung Electronics",
                side="BUY",
                quantity=1,
                price=Decimal("70000"),
                reason="flow",
                result="filled",
                timestamp=datetime(2026, 6, 11, 9, 1),
            ),
        ]

        partially_malformed_view = dashboard_state_to_view_model(controller)

        self.assertEqual(1, len(partially_malformed_view["debug"]["fullTradeLogs"]))
        self.assertEqual(1, len(partially_malformed_view["debug"]["fullRuntimeEvents"]))
        self.assertEqual("005930", partially_malformed_view["debug"]["fullTradeLogs"][0]["symbol"])
        self.assertEqual(2, partially_malformed_view["debug"]["runtime"]["rawRuntimeEventCount"])
        self.assertEqual(2, partially_malformed_view["debug"]["runtime"]["rawTradeEventCount"])
        self.assertEqual(1, partially_malformed_view["debug"]["runtime"]["fullTradeLogSkippedCount"])
        self.assertEqual(1, partially_malformed_view["debug"]["runtime"]["fullRuntimeEventSkippedCount"])
        self.assertTrue(partially_malformed_view["debug"]["runtime"]["runtimeEventStoreReadable"])

    def test_redact_sensitive_text_masks_keys_tokens_accounts_and_local_paths(self):
        raw = (
            "Authorization: Bearer secret-token-123 "
            "KIS_VTS_APP_SECRET=abc app_secret=app-secret acct 123456789 "
            '{"appSecret":"json-secret","accountNo":"87654321","token":"json-token"} '
            "C:\\Users\\example\\StockProject\\.env"
        )

        redacted = redact_sensitive_text(raw)

        self.assertNotIn("abc", redacted)
        self.assertNotIn("secret-token-123", redacted)
        self.assertNotIn("app-secret", redacted)
        self.assertNotIn("json-secret", redacted)
        self.assertNotIn("json-token", redacted)
        self.assertNotIn("token-value", redacted)
        self.assertNotIn("123456789", redacted)
        self.assertNotIn("87654321", redacted)
        self.assertNotIn("C:\\Users\\example", redacted)
        self.assertNotIn("StockProject", redacted)


class ElectronBridgePersistentSchedulerTest(unittest.TestCase):
    def test_backend_service_without_scheduler_control_blocks_non_bootstrap_action(self):
        controller = DashboardController(
            services=DashboardServices(
                runtime=FakeRuntime(),
                live_readiness_check=lambda **_kwargs: self.fail(
                    "missing scheduler control must block readiness"
                ),
            )
        )

        with self.assertRaisesRegex(
            PermissionError,
            "credential bootstrap is pending",
        ):
            _dispatch_action(
                controller,
                "live-readiness-check",
                {"refreshScannerSnapshot": True},
                scheduler_owner="service",
                scheduler_control=None,
            )

    def test_backend_service_rejects_renderer_cycle_and_virtual_mode_actions(self):
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()))
        controller.select_trading_mode("real")
        scheduler = FakeSchedulerControl()

        with self.assertRaises(PermissionError):
            _dispatch_action(
                controller,
                "cycle",
                {},
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
        with self.assertRaises(PermissionError):
            _dispatch_action(
                controller,
                "mode",
                {"mode": "virtual"},
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )

    def test_backend_service_pause_and_start_update_scheduler_control(self):
        runtime = FakeRuntime(data_source_kind="local", data_source_label="local")
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        scheduler = FakeSchedulerControl()

        _dispatch_action(
            controller,
            "pause",
            {},
            scheduler_owner="service",
            scheduler_control=scheduler,
        )
        _dispatch_action(
            controller,
            "start",
            {},
            scheduler_owner="service",
            scheduler_control=scheduler,
        )

        self.assertEqual(1, scheduler.suspend_calls)
        self.assertEqual(1, scheduler.resume_calls)
        self.assertFalse(runtime.started)
        self.assertFalse(controller._runtime_running)


class ElectronBridgeStartupConfigTest(unittest.TestCase):
    def test_main_accepts_config_path_and_passes_it_to_serve_forever(self):
        config_path = "config.live.example.yaml"

        with patch("stockbot.electron_bridge.serve_forever") as serve:
            exit_code = main(
                [
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "0",
                    "--token",
                    "test-token",
                    "--config",
                    config_path,
                ]
            )

        self.assertEqual(0, exit_code)
        serve.assert_called_once_with(
            host="127.0.0.1",
            port=0,
            token="test-token",
            config_path=config_path,
            allow_real_actions=False,
        )

    def test_main_rejects_legacy_standalone_persistent_scheduler_flag(self):
        with (
            patch("stockbot.electron_bridge.serve_forever") as serve,
            patch("sys.stderr"),
        ):
            with self.assertRaises(SystemExit):
                main(["--persistent-live"])

        serve.assert_not_called()

    def test_create_bridge_server_uses_config_path_for_default_controller(self):
        config_path = "config.live.example.yaml"
        controller = DashboardController(services=DashboardServices(runtime=FakeRuntime()), config_path=config_path)

        with patch("stockbot.electron_bridge.create_default_controller", return_value=controller) as factory:
            server = create_bridge_server(host="127.0.0.1", port=0, token="test-token", config_path=config_path)
            try:
                self.assertIs(server.controller, controller)
            finally:
                server.server_close()

        factory.assert_called_once_with(config_path=config_path)


class ElectronBridgeHttpTest(unittest.TestCase):
    def setUp(self):
        self.live_check_calls = []
        self.temp_dir = TemporaryDirectory()
        self.env_file = Path(self.temp_dir.name) / ".env"
        self.env_file.write_text(
            "\n".join(
                [
                    "KIS_LIVE_APP_KEY=live-bridge-key",
                    "KIS_LIVE_APP_SECRET=live-bridge-secret",
                    "KIS_LIVE_ACCOUNT_NO=12345678",
                    "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        def fake_live_probe(*, env_file, env, symbol):
            self.live_check_calls.append({"env_file": env_file, "env": env, "symbol": symbol})
            return {
                "account": "******78-01",
                "cash": "1200000",
                "equity": "1250000",
                "balance_positions": 2,
                "last_price": "71500",
                "read_only": True,
                "live_order_enabled": False,
                "positions": [
                    {
                        "symbol": "005930",
                        "side": "LONG",
                        "quantity": 4,
                        "avg_price": "70000",
                        "last_price": "71400",
                        "unrealized_pnl": "5600",
                    }
                ],
            }

        self.controller = DashboardController(
            services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"}, kis_live_check=fake_live_probe),
            env_file=str(self.env_file),
        )
        self.server = create_bridge_server(self.controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.temp_dir.cleanup()

    def test_health_and_state_endpoints(self):
        with request.urlopen(f"{self.base_url}/api/health", timeout=2) as response:
            health = json.loads(response.read().decode("utf-8"))
        with request.urlopen(self._request("/api/state", method="GET"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertTrue(health["ok"])
        self.assertEqual(0, state["stateRevision"])
        self.assertEqual("개미親주식", state["app"]["title"])

    def test_profit_report_endpoint_is_read_only_and_uses_a_dedicated_service(self):
        calls = []

        def profit_report(*, granularity, scope, anchor):
            calls.append((granularity, scope, anchor))
            return {
                "schemaVersion": 1,
                "generatedAt": "2026-07-29T15:31:00+09:00",
                "status": "complete",
                "query": {
                    "granularity": granularity,
                    "scope": scope,
                    "anchor": anchor,
                    "timezone": "Asia/Seoul",
                },
                "range": {
                    "label": "2026.07.01 - 2026.07.29",
                    "startAt": "2026-07-01T00:00:00+09:00",
                    "endAt": "2026-07-30T00:00:00+09:00",
                    "anchor": anchor,
                    "previousAnchor": "2026-06-01",
                    "nextAnchor": None,
                },
                "summary": {
                    "reportedRealizedPnlKrw": 1200,
                    "profitableBucketsTotalKrw": 1200,
                    "losingBucketsTotalKrw": 0,
                    "tradingCostKrw": 80,
                    "profitableBucketCount": 1,
                    "losingBucketCount": 0,
                    "availableBucketCount": 1,
                },
                "buckets": [],
                "issues": [],
                "costInclusion": "unknown",
            }

        self.controller.services = replace(self.controller.services, profit_report=profit_report)
        revision_before = self.controller.state_revision
        endpoint = (
            "/api/profit-report?granularity=day&scope=account"
            "&anchor=2026-07-29&timezone=Asia%2FSeoul"
        )

        with request.urlopen(self._request(endpoint, method="GET"), timeout=2) as response:
            report = json.loads(response.read().decode("utf-8"))

        self.assertEqual([("day", "account", "2026-07-29")], calls)
        self.assertEqual(1200, report["summary"]["reportedRealizedPnlKrw"])
        self.assertEqual("unknown", report["costInclusion"])
        self.assertNotIn("stateRevision", report)
        self.assertEqual(revision_before, self.controller.state_revision)

    def test_profit_report_endpoint_rejects_invalid_or_extra_query_values(self):
        calls = []
        self.controller.services = replace(
            self.controller.services,
            profit_report=lambda **kwargs: calls.append(kwargs),
        )

        for endpoint in (
            "/api/profit-report?granularity=minute&scope=account&anchor=2026-07-29&timezone=Asia%2FSeoul",
            "/api/profit-report?granularity=day&scope=all&anchor=2026-07-29&timezone=Asia%2FSeoul",
            "/api/profit-report?granularity=day&scope=account&anchor=07%2F29%2F2026&timezone=Asia%2FSeoul",
            "/api/profit-report?granularity=day&scope=account&anchor=2026-07-29&timezone=UTC",
            "/api/profit-report?granularity=day&scope=account&anchor=2026-07-29&timezone=Asia%2FSeoul&token=secret",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(HTTPError) as context:
                    request.urlopen(self._request(endpoint, method="GET"), timeout=2)
                self.assertEqual(400, context.exception.code)

        self.assertEqual([], calls)

    def test_state_endpoint_does_not_reconcile_live_orders(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        self.controller, runtime = make_running_live_controller(Path(self.temp_dir.name))
        self.server = create_bridge_server(self.controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

        with patch.object(runtime.broker, "sync_pending_order_statuses") as reconcile:
            with request.urlopen(self._request("/api/state", method="GET"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

        reconcile.assert_not_called()
        self.assertTrue(state["notice"]["orderEnabled"])

    def test_post_action_returns_updated_state_with_cors_headers(self):
        payload = json.dumps({}).encode("utf-8")
        req = self._request("/api/actions/start", data=payload, method="POST")

        with request.urlopen(req, timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))
            cors = response.headers.get("Access-Control-Allow-Origin")

        self.assertNotEqual("*", cors)
        self.assertGreater(state["stateRevision"], 0)
        self.assertTrue(state["runtime"]["running"])
        self.assertTrue(self.controller.services.runtime.started)

    def test_start_and_pause_actions_keep_runtime_status_aligned(self):
        with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
            started = json.loads(response.read().decode("utf-8"))
        with request.urlopen(self._request("/api/actions/pause", data=b"{}", method="POST"), timeout=2) as response:
            paused = json.loads(response.read().decode("utf-8"))

        self.assertTrue(started["runtime"]["running"])
        self.assertEqual("running", started["runtime"]["status"])
        self.assertFalse(paused["runtime"]["running"])
        self.assertEqual("일시정지", paused["runtime"]["status"])
        self.assertGreater(paused["stateRevision"], started["stateRevision"])

    def test_mode_action_keeps_real_mode_order_locked(self):
        payload = json.dumps({"mode": "real"}).encode("utf-8")
        req = self._request("/api/actions/mode", data=payload, method="POST")

        with request.urlopen(req, timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("real", state["mode"]["key"])
        self.assertFalse(state["notice"]["orderEnabled"])
        self.assertEqual("danger", state["notice"]["tone"])
        self.assertEqual("real-prep", state["runtime"]["dataSourceKind"])
        self.assertEqual("KIS live account", state["runtime"]["dataSource"])
        self.assertEqual("KIS 실전 계좌", state["runtime"]["dataModeLabel"])
        self.assertNotIn("read-only", json.dumps(state, ensure_ascii=False).lower())
        self.assertNotIn("읽기 전용", json.dumps(state, ensure_ascii=False))
        self.assertEqual(1, len(self.live_check_calls))
        rendered = json.dumps(state, ensure_ascii=False)
        self.assertIn("******78-01", rendered)
        self.assertEqual(["005930"], [position["symbol"] for position in state["positions"]])
        self.assertEqual("삼성전자", state["positions"][0]["companyName"])
        self.assertEqual(4, state["positions"][0]["quantity"])
        self.assertEqual("71,400원", state["positions"][0]["lastPrice"])

    def test_data_source_action_returns_one_shot_market_hours_popup_when_kis_switch_is_blocked(self):
        closed_status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장 대기 - 정규장 시간이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )

        def fail_builder(source):
            raise AssertionError(f"runtime builder should not run outside market hours: {source}")

        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="샘플 CSV")
        self.controller.services = DashboardServices(
            runtime=current_runtime,
            symbol_names={"005930": "삼성전자"},
            runtime_builder=fail_builder,
            kis_market_status=lambda: closed_status,
        )
        payload = json.dumps({"source": "kis-vts"}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/data-source", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("local", state["runtime"]["dataSourceKind"])
        self.assertEqual("장중 테스트 불가", state["actionPopup"]["title"])
        self.assertIn("정규장", state["actionPopup"]["message"])
        self.assertIs(current_runtime, self.controller.services.runtime)

        with request.urlopen(self._request("/api/state", method="GET"), timeout=2) as response:
            refreshed = json.loads(response.read().decode("utf-8"))

        self.assertNotIn("actionPopup", refreshed)

    def test_data_source_action_returns_market_hours_popup_for_external_scan_kis_switch(self):
        closed_status = MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message="장 대기 - 정규장 시간이 아닙니다.",
            checked_at=datetime(2026, 6, 11, 20, 0, tzinfo=KST),
        )

        def fail_builder(source):
            raise AssertionError(f"runtime builder should not run outside market hours: {source}")

        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        self.controller.services = DashboardServices(
            runtime=current_runtime,
            symbol_names={"005930": "삼성전자"},
            runtime_builder=fail_builder,
            kis_market_status=lambda: closed_status,
        )
        payload = json.dumps({"source": "external-scan-kis"}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/data-source", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("local", state["runtime"]["dataSourceKind"])
        self.assertEqual("장중 테스트 불가", state["actionPopup"]["title"])
        self.assertIn("정규장", state["actionPopup"]["message"])
        self.assertIs(current_runtime, self.controller.services.runtime)

    def test_data_source_action_normalizes_legacy_kis_vts_payload_to_external_scan_kis(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="open",
            message="market open",
            checked_at=datetime(2026, 6, 19, 10, 0, tzinfo=KST),
        )
        external_runtime = FakeRuntime(
            data_source_kind="external-scan-kis",
            data_source_label="wide scanner / KIS final quote paper",
        )
        requested_sources = []

        def build_runtime(source):
            requested_sources.append(source)
            if source != "external-scan-kis":
                raise AssertionError(f"unexpected legacy dashboard source: {source}")
            return external_runtime

        self.controller.services = DashboardServices(
            runtime=FakeRuntime(data_source_kind="local", data_source_label="sample CSV"),
            symbol_names={"005930": "Samsung Electronics"},
            runtime_builder=build_runtime,
            kis_market_status=lambda: open_status,
        )
        payload = json.dumps({"source": "kis-vts"}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/data-source", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual(["external-scan-kis"], requested_sources)
        self.assertEqual("external-scan-kis", state["runtime"]["dataSourceKind"])
        self.assertIs(external_runtime, self.controller.services.runtime)

    def test_data_source_action_returns_snapshot_setup_popup_when_external_snapshot_is_missing(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="장중",
            message="장중입니다.",
            checked_at=datetime(2026, 6, 19, 10, 0, tzinfo=KST),
        )
        missing_snapshot_message = (
            "scanner_snapshot.json 파일이 없습니다. 외부 수집기로 data 폴더에 scanner_snapshot.json을 먼저 생성하세요"
        )

        def fail_builder(source):
            if source == "external-scan-kis":
                raise ValueError(missing_snapshot_message)
            raise AssertionError(f"unexpected source: {source}")

        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        self.controller.services = DashboardServices(
            runtime=current_runtime,
            symbol_names={"005930": "삼성전자"},
            runtime_builder=fail_builder,
            kis_market_status=lambda: open_status,
        )
        payload = json.dumps({"source": "external-scan-kis"}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/data-source", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("local", state["runtime"]["dataSourceKind"])
        self.assertEqual("장중 테스트 준비 필요", state["actionPopup"]["title"])
        self.assertIn("scanner_snapshot.json", state["actionPopup"]["message"])
        self.assertIs(current_runtime, self.controller.services.runtime)

    def test_data_source_action_returns_wait_popup_when_cycle_is_busy(self):
        open_status = MarketSessionStatus(
            is_open=True,
            label="장중",
            message="장중입니다.",
            checked_at=datetime(2026, 6, 19, 10, 0, tzinfo=KST),
        )
        current_runtime = FakeRuntime(data_source_kind="local", data_source_label="sample CSV")
        self.controller.services = DashboardServices(
            runtime=current_runtime,
            symbol_names={"005930": "삼성전자"},
            runtime_builder=lambda source: FakeRuntime(data_source_kind=source),
            kis_market_status=lambda: open_status,
        )
        self.controller._runtime_busy = True
        payload = json.dumps({"source": "external-scan-kis"}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/data-source", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("local", state["runtime"]["dataSourceKind"])
        self.assertEqual("전환 대기 필요", state["actionPopup"]["title"])
        self.assertIn("cycle이 끝난 뒤", state["actionPopup"]["message"])

    def test_removed_strategy_actions_are_rejected_without_mutating_state(self):
        original_state = self.controller.state
        original_settings = self.controller.current_custom_settings()
        original_state_revision = self.controller.state_revision
        original_strategy_revision = self.controller._strategy_revision
        removed_actions = {
            "ai-advisor": {},
            "profile": {"profile": "aggressive"},
            "custom-settings": {
                "cashAllocationPct": 90,
                "maxPositionAmount": 600000,
            },
        }

        for action, body in removed_actions.items():
            with self.subTest(action=action):
                payload = json.dumps(body).encode("utf-8")
                req = self._request(f"/api/actions/{action}", data=payload, method="POST")

                with self.assertRaises(HTTPError) as caught:
                    request.urlopen(req, timeout=2)

                self.assertEqual(400, caught.exception.code)
                error = json.loads(caught.exception.read().decode("utf-8"))
                self.assertFalse(error["ok"])
                self.assertIn(f"unknown action: {action}", error["error"])

        self.assertEqual(original_state, self.controller.state)
        self.assertEqual(original_settings, self.controller.current_custom_settings())
        self.assertEqual(original_state_revision, self.controller.state_revision)
        self.assertEqual(original_strategy_revision, self.controller._strategy_revision)

    def test_cleanup_mode_action_is_global_without_strategy_view_model(self):
        payload = json.dumps({"enabled": True}).encode("utf-8")
        req = self._request("/api/actions/cleanup-mode", data=payload, method="POST")

        with request.urlopen(req, timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertTrue(state["runtime"]["cleanupMode"])
        self.assertNotIn("advisor", state)
        self.assertIn("정리 모드", json.dumps(state["logs"]["system"], ensure_ascii=False))

    def test_cleanup_mode_action_is_allowed_in_real_mode_for_exit_only_management(self):
        mode_payload = json.dumps({"mode": "real"}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/mode", data=mode_payload, method="POST"), timeout=2):
            pass
        payload = json.dumps({"enabled": True}).encode("utf-8")

        with request.urlopen(self._request("/api/actions/cleanup-mode", data=payload, method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("real", state["mode"]["key"])
        self.assertTrue(state["runtime"]["cleanupMode"])
        self.assertNotIn("advisor", state)

    def test_cleanup_mode_action_cannot_reopen_real_mode_new_entries(self):
        mode_payload = json.dumps({"mode": "real"}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/mode", data=mode_payload, method="POST"), timeout=2):
            pass
        enable_payload = json.dumps({"enabled": True}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/cleanup-mode", data=enable_payload, method="POST"), timeout=2):
            pass

        disable_payload = json.dumps({"enabled": False}).encode("utf-8")
        with self.assertRaises(HTTPError) as caught:
            request.urlopen(self._request("/api/actions/cleanup-mode", data=disable_payload, method="POST"), timeout=2)

        self.assertEqual(423, caught.exception.code)
        self.assertTrue(self.controller.current_custom_settings().kill_switch)

    def test_cleanup_cycle_response_stops_runtime_after_positions_clear(self):
        self.controller.services = replace(self.controller.services, runtime=ClearingRuntime())
        with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
            started = json.loads(response.read().decode("utf-8"))
        payload = json.dumps({"enabled": True}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/cleanup-mode", data=payload, method="POST"), timeout=2):
            pass
        with request.urlopen(self._request("/api/actions/cycle", data=b"{}", method="POST"), timeout=2) as response:
            stopped = json.loads(response.read().decode("utf-8"))

        self.assertTrue(started["runtime"]["running"])
        self.assertFalse(stopped["runtime"]["running"])
        self.assertEqual("예약 없음", stopped["runtime"]["cycleLabel"])
        self.assertEqual("일시정지", stopped["runtime"]["status"])
        self.assertEqual([], stopped["positions"])
        self.assertTrue(stopped["runtime"]["cleanupMode"])
        self.assertIn("정리 모드 완료", json.dumps(stopped["logs"]["system"], ensure_ascii=False))

    def test_kis_credentials_action_writes_local_env_without_returning_secrets(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"}),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": "bridge-test-value-a",
                    "appSecret": "bridge-test-value-b",
                    "accountNo": "paper-account-test",
                    "productCode": "01",
                }
            ).encode("utf-8")

            with request.urlopen(self._request("/api/actions/kis-credentials", data=payload, method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("KIS_VTS_APP_KEY=bridge-test-value-a", env_text)
            self.assertIn("KIS_VTS_APP_SECRET=bridge-test-value-b", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_NO=paper-account-test", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_PRODUCT_CODE=01", env_text)
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertIn("KIS API 설정 저장", rendered)
            self.assertNotIn("bridge-test-value-a", rendered)
            self.assertNotIn("bridge-test-value-b", rendered)
            self.assertNotIn("paper-account-test", rendered)

    def test_kis_live_credentials_action_writes_live_env_without_returning_secrets(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=existing-paper-key",
                        "KIS_VTS_APP_SECRET=existing-paper-secret",
                        "KIS_VTS_ACCOUNT_NO=existing-paper-account",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=99",
                        "KIS_LIVE_ACCOUNT_NO=existing-live-account",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=99",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"}),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": "live-bridge-key",
                    "appSecret": "live-bridge-secret",
                    "accountNo": "12345678",
                    "productCode": "01",
                }
            ).encode("utf-8")

            with request.urlopen(self._request("/api/actions/kis-live-credentials", data=payload, method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("KIS_LIVE_APP_KEY=live-bridge-key", env_text)
            self.assertIn("KIS_LIVE_APP_SECRET=live-bridge-secret", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_NO=12345678", env_text)
            self.assertIn("KIS_LIVE_ACCOUNT_PRODUCT_CODE=01", env_text)
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING", env_text)
            self.assertNotIn("STOCKBOT_LIVE_TRADING_ENABLED", env_text)
            self.assertNotIn("STOCKBOT_LIVE_TRADING_CONFIRM", env_text)
            self.assertNotIn("STOCKBOT_LIVE_ACCOUNT_CONFIRMATION", env_text)
            self.assertIn("KIS_VTS_APP_KEY=existing-paper-key", env_text)
            self.assertIn("KIS_VTS_APP_SECRET=existing-paper-secret", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_NO=existing-paper-account", env_text)
            self.assertIn("KIS_VTS_ACCOUNT_PRODUCT_CODE=99", env_text)
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertIn("KIS 실전 조회 설정 저장", rendered)
            self.assertNotIn("live-bridge-key", rendered)
            self.assertNotIn("live-bridge-secret", rendered)
            self.assertNotIn("12345678", rendered)

    def test_service_kis_live_credential_save_binds_scope_after_env_write(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "삼성전자"},
                ),
                env_file=str(env_file),
            )
            scheduler = FakeSchedulerControl()
            scheduler.credential_binding_pending = True
            observed_env_text: list[str] = []

            def bind_saved_credential_scope(candidate_fingerprint):
                observed_env_text.append(env_file.read_text(encoding="utf-8"))
                scheduler.bind_saved_credential_scope_calls += 1
                scheduler.bound_credential_candidates.append(
                    candidate_fingerprint
                )
                scheduler.credential_binding_pending = False
                return True

            scheduler.bind_saved_credential_scope = bind_saved_credential_scope
            self.server = create_bridge_server(
                controller,
                host="127.0.0.1",
                port=0,
                token="test-token",
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
            )
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": "live-bind-key",
                    "appSecret": "live-bind-secret",
                    "accountNo": "12345678",
                    "productCode": "01",
                }
            ).encode("utf-8")

            with request.urlopen(
                self._request(
                    "/api/actions/kis-live-credentials",
                    data=payload,
                    method="POST",
                ),
                timeout=2,
            ):
                pass

            self.assertEqual(1, scheduler.bind_saved_credential_scope_calls)
            self.assertEqual(
                scheduler.validated_credential_candidates,
                scheduler.bound_credential_candidates,
            )
            self.assertEqual(1, len(scheduler.validated_credential_candidates))
            self.assertEqual(1, len(observed_env_text))
            self.assertIn("KIS_LIVE_APP_KEY=live-bind-key", observed_env_text[0])
            self.assertIn(
                "KIS_LIVE_APP_SECRET=live-bind-secret",
                observed_env_text[0],
            )

    def test_service_kis_live_credential_save_rejection_does_not_bind_scope(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "삼성전자"},
                ),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            controller._runtime_running = True
            scheduler = FakeSchedulerControl()
            scheduler.credential_binding_pending = True
            self.server = create_bridge_server(
                controller,
                host="127.0.0.1",
                port=0,
                token="test-token",
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
            )
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": "blocked-live-key",
                    "appSecret": "blocked-live-secret",
                    "accountNo": "12345678",
                    "productCode": "01",
                }
            ).encode("utf-8")

            with request.urlopen(
                self._request(
                    "/api/actions/kis-live-credentials",
                    data=payload,
                    method="POST",
                ),
                timeout=2,
            ) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual(0, scheduler.bind_saved_credential_scope_calls)
            self.assertFalse(env_file.exists())
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertIn("KIS 실전 설정 저장 차단", rendered)
            self.assertNotIn("blocked-live-secret", rendered)

    def test_pending_service_blocks_live_readiness_before_refresh_market_or_kis(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        readiness_calls = 0
        market_calls = 0
        kis_calls = 0

        def forbidden_readiness(**_kwargs):
            nonlocal readiness_calls
            readiness_calls += 1
            raise AssertionError("pending scope must block scanner refresh")

        def forbidden_market():
            nonlocal market_calls
            market_calls += 1
            raise AssertionError("pending scope must block market access")

        def forbidden_kis(**_kwargs):
            nonlocal kis_calls
            kis_calls += 1
            raise AssertionError("pending scope must block KIS access")

        controller = DashboardController(
            services=DashboardServices(
                runtime=FakeRuntime(),
                symbol_names={"005930": "삼성전자"},
                live_readiness_check=forbidden_readiness,
                kis_market_status=forbidden_market,
                kis_live_check=forbidden_kis,
            ),
            env_file=str(self.env_file),
        )
        scheduler = FakeSchedulerControl()
        scheduler.credential_binding_pending = True
        self.server = create_bridge_server(
            controller,
            host="127.0.0.1",
            port=0,
            token="test-token",
            scheduler_owner="service",
            scheduler_control=scheduler,
            persistent_real_mode=True,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        payload = json.dumps({"refreshScannerSnapshot": True}).encode("utf-8")

        with self.assertRaises(HTTPError) as caught:
            request.urlopen(
                self._request(
                    "/api/actions/live-readiness-check",
                    data=payload,
                    method="POST",
                ),
                timeout=2,
            )

        self.assertEqual(423, caught.exception.code)
        rendered = caught.exception.read().decode("utf-8")
        self.assertIn("credential bootstrap", rendered)
        self.assertEqual(0, readiness_calls)
        self.assertEqual(0, market_calls)
        self.assertEqual(0, kis_calls)

    def test_bound_scope_drift_blocks_service_network_actions_while_runtime_paused(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        readiness_calls = 0
        market_calls = 0
        kis_calls = 0

        def forbidden_readiness(**_kwargs):
            nonlocal readiness_calls
            readiness_calls += 1
            raise AssertionError("scope drift must block readiness")

        def forbidden_market():
            nonlocal market_calls
            market_calls += 1
            raise AssertionError("scope drift must block market access")

        def forbidden_kis(**_kwargs):
            nonlocal kis_calls
            kis_calls += 1
            raise AssertionError("scope drift must block KIS access")

        controller = DashboardController(
            services=DashboardServices(
                runtime=FakeRuntime(),
                symbol_names={"005930": "삼성전자"},
                live_readiness_check=forbidden_readiness,
                kis_market_status=forbidden_market,
                kis_live_check=forbidden_kis,
            ),
            env_file=str(self.env_file),
        )
        controller._runtime_running = False
        scheduler = FakeSchedulerControl()
        scheduler.current_credential_scope_authorized = False
        self.assertFalse(controller._runtime_running)
        self.server = create_bridge_server(
            controller,
            host="127.0.0.1",
            port=0,
            token="test-token",
            scheduler_owner="service",
            scheduler_control=scheduler,
            persistent_real_mode=True,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        actions = (
            ("kis-live-check", {}),
            ("live-readiness-check", {"refreshScannerSnapshot": True}),
            ("start", {}),
            ("mode", {"mode": "real"}),
        )

        for action, body in actions:
            with self.subTest(action=action):
                payload = json.dumps(body).encode("utf-8")
                with self.assertRaises(HTTPError) as caught:
                    request.urlopen(
                        self._request(
                            f"/api/actions/{action}",
                            data=payload,
                            method="POST",
                        ),
                        timeout=2,
                    )
                self.assertEqual(423, caught.exception.code)
                rendered = caught.exception.read().decode("utf-8")
                self.assertIn("credential scope", rendered)

        with request.urlopen(
            self._request(
                "/api/actions/pause",
                data=b"{}",
                method="POST",
            ),
            timeout=2,
        ):
            pass

        self.assertEqual(
            len(actions),
            scheduler.validate_current_credential_scope_calls,
        )
        self.assertEqual(1, scheduler.suspend_calls)
        self.assertEqual(0, scheduler.resume_calls)
        self.assertEqual(0, readiness_calls)
        self.assertEqual(0, market_calls)
        self.assertEqual(0, kis_calls)

    def test_credential_save_rejects_quotes_and_line_breaks_without_mutation(self):
        original = self.env_file.read_bytes()
        invalid_values = (
            '"quoted-live-key"',
            "'quoted-live-key'",
            "line\nbreak",
            "line\rbreak",
        )
        actions = ("kis-credentials", "kis-live-credentials")

        for action in actions:
            for invalid_value in invalid_values:
                with self.subTest(
                    action=action,
                    invalid_value=repr(invalid_value),
                ):
                    self.env_file.write_bytes(original)
                    payload = json.dumps(
                        {
                            "appKey": invalid_value,
                            "appSecret": "must-not-echo-secret",
                            "accountNo": "12345678",
                            "productCode": "01",
                        }
                    ).encode("utf-8")

                    with self.assertRaises(HTTPError) as caught:
                        request.urlopen(
                            self._request(
                                f"/api/actions/{action}",
                                data=payload,
                                method="POST",
                            ),
                            timeout=2,
                        )

                    self.assertEqual(400, caught.exception.code)
                    rendered = caught.exception.read().decode("utf-8")
                    self.assertNotIn("quoted-live-key", rendered)
                    self.assertNotIn("must-not-echo-secret", rendered)
                    self.assertNotIn("12345678", rendered)
                    self.assertEqual(original, self.env_file.read_bytes())

    def test_bound_scope_drift_allows_same_scope_credential_recovery(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=drifted-key",
                        "KIS_LIVE_APP_SECRET=drifted-secret",
                        "KIS_LIVE_ACCOUNT_NO=87654321",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            expected_values = {
                "KIS_LIVE_APP_KEY": "bound-live-key",
                "KIS_LIVE_APP_SECRET": "bound-live-secret",
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
            }
            expected_fingerprint = live_credential_scope_fingerprint(
                expected_values
            )
            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "삼성전자"},
                ),
                env_file=str(env_file),
            )
            scheduler = FakeSchedulerControl()
            scheduler.expected_credential_fingerprint = expected_fingerprint
            scheduler.current_credential_scope_authorized = False
            self.server = create_bridge_server(
                controller,
                host="127.0.0.1",
                port=0,
                token="test-token",
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
            )
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": expected_values["KIS_LIVE_APP_KEY"],
                    "appSecret": expected_values["KIS_LIVE_APP_SECRET"],
                    "accountNo": expected_values["KIS_LIVE_ACCOUNT_NO"],
                    "productCode": expected_values[
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE"
                    ],
                }
            ).encode("utf-8")

            with request.urlopen(
                self._request(
                    "/api/actions/kis-live-credentials",
                    data=payload,
                    method="POST",
                ),
                timeout=2,
            ):
                pass

            self.assertEqual(
                expected_fingerprint,
                live_credential_scope_fingerprint(read_env_file(env_file)),
            )
            self.assertEqual(
                [expected_fingerprint],
                scheduler.validated_credential_candidates,
            )
            self.assertEqual(
                [expected_fingerprint],
                scheduler.bound_credential_candidates,
            )
            self.assertEqual(0, scheduler.validate_current_credential_scope_calls)

    def test_bound_service_rejects_different_credentials_before_env_mutation(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
            original = env_file.read_bytes()
            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "삼성전자"},
                ),
                env_file=str(env_file),
            )
            scheduler = FakeSchedulerControl()
            scheduler.expected_credential_fingerprint = (
                live_credential_scope_fingerprint(read_env_file(env_file))
            )
            self.server = create_bridge_server(
                controller,
                host="127.0.0.1",
                port=0,
                token="test-token",
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
            self.thread = threading.Thread(
                target=self.server.serve_forever,
                daemon=True,
            )
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "appKey": "different-live-key",
                    "appSecret": "different-live-secret",
                    "accountNo": "87654321",
                    "productCode": "01",
                }
            ).encode("utf-8")

            with self.assertRaises(HTTPError) as caught:
                request.urlopen(
                    self._request(
                        "/api/actions/kis-live-credentials",
                        data=payload,
                        method="POST",
                    ),
                    timeout=2,
                )

            self.assertEqual(423, caught.exception.code)
            rendered = caught.exception.read().decode("utf-8")
            self.assertNotIn("different-live-key", rendered)
            self.assertNotIn("different-live-secret", rendered)
            self.assertNotIn("87654321", rendered)
            self.assertEqual(original, env_file.read_bytes())
            self.assertEqual(1, len(scheduler.validated_credential_candidates))
            self.assertEqual(0, scheduler.bind_saved_credential_scope_calls)

    def test_live_order_approval_action_writes_gate_values_without_returning_secrets(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-bridge-key",
                        "KIS_LIVE_APP_SECRET=live-bridge-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            def fake_live_probe(*, env_file, env, symbol):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "71500",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "?쇱꽦?꾩옄"}),
                env_file=str(env_file),
            )
            controller.services = replace(controller.services, kis_live_check=fake_live_probe)
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            with request.urlopen(self._request("/api/actions/kis-live-check", data=b"{}", method="POST"), timeout=2):
                pass
            payload = json.dumps(
                {
                    "confirmationPhrase": LIVE_CONFIRMATION_PHRASE,
                    "accountConfirmation": "78",
                }
            ).encode("utf-8")

            with request.urlopen(self._request("/api/actions/live-order-approval", data=payload, method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertIn("STOCKBOT_LIVE_TRADING_ENABLED=true", env_text)
            self.assertIn(f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}", env_text)
            self.assertIn("STOCKBOT_LIVE_ACCOUNT_CONFIRMATION=78", env_text)
            self.assertTrue(state["settings"]["liveOrderApproval"]["allowSaved"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["enabledSaved"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["confirmationSaved"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["accountConfirmationSaved"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["sessionApproved"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["riskLimitsOk"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["newEntriesAllowed"])
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-bridge-key", rendered)
            self.assertNotIn("live-bridge-secret", rendered)
            self.assertNotIn("12345678", rendered)
            self.assertNotIn(LIVE_CONFIRMATION_PHRASE, rendered)

    def test_live_order_approval_action_requires_successful_live_read_only_check(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-bridge-key",
                        "KIS_LIVE_APP_SECRET=live-bridge-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "Samsung"}),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "confirmationPhrase": LIVE_CONFIRMATION_PHRASE,
                    "accountConfirmation": "78",
                }
            ).encode("utf-8")

            with self.assertRaises(HTTPError) as context:
                request.urlopen(self._request("/api/actions/live-order-approval", data=payload, method="POST"), timeout=2)

            self.assertEqual(400, context.exception.code)
            error_body = context.exception.read().decode("utf-8")
            self.assertNotIn("live-bridge-key", error_body)
            self.assertNotIn("live-bridge-secret", error_body)
            self.assertNotIn("12345678", error_body)
            env_text = env_file.read_text(encoding="utf-8")
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertFalse(controller._live_order_safety_context.session_approved)

    def test_saved_live_order_approval_keeps_notice_locked_without_session_approval(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "Samsung"}),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")

            state = dashboard_state_to_view_model(controller)

            self.assertTrue(state["notice"]["locked"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertFalse(state["notice"]["ready"])
            self.assertEqual("danger", state["notice"]["tone"])
            self.assertEqual("REAL 주문 잠금", state["notice"]["title"])
            self.assertEqual("실전 잠금", state["runtime"]["status"])

    def test_saved_live_order_approval_stays_locked_until_live_readiness_passes(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-bridge-key",
                        "KIS_LIVE_APP_SECRET=live-bridge-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_live_probe(*, env_file, env, symbol):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "71500",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "Samsung"},
                    kis_live_check=fake_live_probe,
                ),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            controller.run_kis_live_check(activate_real_mode=False)
            controller.save_live_order_approval(
                confirmation_phrase=LIVE_CONFIRMATION_PHRASE,
                account_confirmation="78",
            )

            state = dashboard_state_to_view_model(controller)

            self.assertTrue(state["notice"]["locked"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertFalse(state["notice"]["ready"])
            self.assertEqual("danger", state["notice"]["tone"])
            self.assertEqual("REAL 주문 잠금", state["notice"]["title"])
            self.assertEqual("실전 잠금", state["runtime"]["status"])

            controller._live_runtime_readiness_ready = True
            state = dashboard_state_to_view_model(controller)

            self.assertTrue(state["notice"]["locked"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertTrue(state["notice"]["ready"])
            self.assertEqual("neutral", state["notice"]["tone"])
            self.assertEqual("REAL 주문 준비", state["notice"]["title"])
            self.assertIn("현재 세션 실전 시작 의도", state["notice"]["description"])
            self.assertNotIn("실전 주문 승인값이 저장되었습니다", state["notice"]["description"])
            self.assertEqual("실전 준비", state["runtime"]["status"])

    def test_live_order_approval_action_rejects_wrong_confirmation_without_writing_gate(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-bridge-key",
                        "KIS_LIVE_APP_SECRET=live-bridge-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "?쇱꽦?꾩옄"}),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps(
                {
                    "confirmationPhrase": "wrong",
                    "accountConfirmation": "78",
                }
            ).encode("utf-8")

            with self.assertRaises(HTTPError) as context:
                request.urlopen(self._request("/api/actions/live-order-approval", data=payload, method="POST"), timeout=2)

            self.assertEqual(400, context.exception.code)
            error_body = context.exception.read().decode("utf-8")
            self.assertNotIn("live-bridge-key", error_body)
            self.assertNotIn("live-bridge-secret", error_body)
            self.assertNotIn("12345678", error_body)
            self.assertNotIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_file.read_text(encoding="utf-8"))

    def test_kis_live_read_only_action_uses_live_probe_without_returning_secrets(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_live_probe(*, env_file, env, symbol):
                captured["env_file"] = env_file
                captured["env"] = env
                captured["symbol"] = symbol
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "balance_positions": 2,
                    "last_price": "71500",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "?쇱꽦?꾩옄"},
                    kis_live_check=fake_live_probe,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/kis-live-check", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual(str(env_file), captured["env_file"])
            self.assertEqual({}, captured["env"])
            self.assertEqual("005930", captured["symbol"])
            self.assertEqual("virtual", state["mode"]["key"])
            self.assertNotEqual("real-read-only", state["runtime"]["dataSourceKind"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertEqual("실전 계좌 조회 성공", state["actionPopup"]["title"])
            self.assertIn("******78-01", state["actionPopup"]["message"])
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)
            self.assertNotIn("12345678", rendered)

    def test_real_mode_action_outside_market_returns_market_hours_popup(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            closed_status = MarketSessionStatus(
                is_open=False,
                label="장 대기",
                message="장 대기 - 정규장 시간이 아닙니다.",
                checked_at=datetime(2026, 7, 13, 8, 42, tzinfo=KST),
                next_open=datetime(2026, 7, 13, 9, 0, tzinfo=KST),
            )

            def fake_live_probe(*, env_file, env, symbol):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "71500",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    kis_market_status=lambda: closed_status,
                ),
                env_file=str(env_file),
            )

            result = _dispatch_action(controller, "mode", {"mode": "real"}, allow_real_actions=True)

        self.assertEqual("real", controller.state.trading_mode)
        self.assertEqual("******78-01", controller.state.account.masked_account)
        self.assertIsNotNone(result)
        self.assertEqual("장중 시간이 아닙니다", result["actionPopup"]["title"])
        self.assertIn("09:00-15:30 KST", result["actionPopup"]["message"])
        self.assertEqual("warning", result["actionPopup"]["tone"])

    def test_kis_live_check_market_hours_popup_is_one_shot(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            closed_status = MarketSessionStatus(
                is_open=False,
                label="장 대기",
                message="장 대기 - 정규장 시간이 아닙니다.",
                checked_at=datetime(2026, 7, 13, 8, 42, tzinfo=KST),
                next_open=datetime(2026, 7, 13, 9, 0, tzinfo=KST),
            )

            def fake_live_probe(*, env_file, env, symbol):
                return {
                    "account": "******78-01",
                    "cash": "1200000",
                    "equity": "1250000",
                    "buying_power": "750000",
                    "balance_positions": 2,
                    "last_price": "71500",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                services=DashboardServices(
                    kis_live_check=fake_live_probe,
                    kis_market_status=lambda: closed_status,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(
                self._request("/api/actions/kis-live-check", data=b"{}", method="POST"), timeout=2
            ) as response:
                action_state = json.loads(response.read().decode("utf-8"))
            with request.urlopen(self._request("/api/state"), timeout=2) as response:
                polled_state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("장중 시간이 아닙니다", action_state["actionPopup"]["title"])
        self.assertIn("09:00-15:30 KST", action_state["actionPopup"]["message"])
        self.assertNotIn("actionPopup", polled_state)

    def test_live_readiness_action_returns_blockers_without_enabling_orders(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            captured = {}

            def fake_readiness(**kwargs):
                captured.update(kwargs)
                return {
                    "ready": False,
                    "blockers": [
                        "pending order for account=12345678-01 KIS_LIVE_APP_SECRET=live-state-secret",
                    ],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": True,
                    "note": "access_token=token-123",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "Samsung"},
                    live_readiness_check=fake_readiness,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps({"refreshScannerSnapshot": True}).encode("utf-8")

            with request.urlopen(self._request("/api/actions/live-readiness-check", data=payload, method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual(str(env_file), captured["env_file"])
            self.assertTrue(captured["refresh_scanner_snapshot"])
            self.assertNotIn("clear_manual_reconciliation", captured)
            self.assertEqual("live-readiness", state["actionResult"]["type"])
            self.assertFalse(state["actionResult"]["ready"])
            self.assertFalse(state["actionResult"]["liveOrderEnabled"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertEqual("virtual", state["mode"]["key"])
            popup_message = state["actionPopup"]["message"]
            self.assertIn("실전 주문 안전 점검", popup_message)
            self.assertNotIn("pending order", popup_message)
            self.assertNotIn("KIS_LIVE_APP_SECRET", popup_message)
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)
            self.assertNotIn("token-123", rendered)
            self.assertNotIn("12345678", rendered)

    def test_live_readiness_gate_blocker_does_not_point_to_removed_approval_controls(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_readiness(**_kwargs):
                return {
                    "ready": False,
                    "blockers": [
                        "STOCKBOT_LIVE_TRADING_CONFIRM is required before live runtime startup",
                    ],
                    "manual_reconciliation_cleared": False,
                    "scanner_snapshot_refreshed": True,
                    "live_order_enabled": False,
                    "note": "no orders were placed",
                }

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    live_readiness_check=fake_readiness,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/live-readiness-check", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            popup_message = state["actionPopup"]["message"]
            self.assertIn("자동매매 시작", popup_message)
            self.assertNotIn("실전 주문 승인 문구", popup_message)
            self.assertNotIn("계좌 끝 2자리", popup_message)

    def test_clear_manual_reconciliation_action_is_separate_from_live_readiness(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            captured = {}

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
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "Samsung"},
                    live_readiness_check=fake_readiness,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps({"confirmationPhrase": "CLEAR_MANUAL_LIVE_RECONCILIATION"}).encode("utf-8")

            with request.urlopen(self._request("/api/actions/clear-manual-reconciliation", data=payload, method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual(str(env_file), captured["env_file"])
            self.assertFalse(captured["refresh_scanner_snapshot"])
            self.assertEqual("CLEAR_MANUAL_LIVE_RECONCILIATION", captured["clear_manual_reconciliation"])
            self.assertEqual("manual-reconciliation-clear", state["actionResult"]["type"])
            self.assertTrue(state["actionResult"]["manualReconciliationCleared"])
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)
            self.assertNotIn("12345678", rendered)

    def test_clear_manual_reconciliation_action_rejects_invalid_confirmation(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def fake_readiness(**kwargs):
                raise ValueError("manual reconciliation clear confirmation is invalid")

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "Samsung"},
                    live_readiness_check=fake_readiness,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
            payload = json.dumps({"confirmationPhrase": "wrong"}).encode("utf-8")

            with request.urlopen(
                self._request("/api/actions/clear-manual-reconciliation", data=payload, method="POST"),
                timeout=2,
            ) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual("manual-reconciliation-clear", state["actionResult"]["type"])
            self.assertFalse(state["actionResult"]["ready"])
            self.assertFalse(state["actionResult"]["manualReconciliationCleared"])
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertIn("manual reconciliation clear confirmation is invalid", rendered)
            self.assertNotIn("12345678", rendered)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)

    def test_kis_live_read_only_action_returns_failure_popup_without_switching_modes(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            def failing_live_probe(*, env_file, env, symbol):
                raise RuntimeError("live-state-secret 12345678 failed")

            controller = DashboardController(
                services=DashboardServices(
                    runtime=FakeRuntime(),
                    symbol_names={"005930": "삼성전자"},
                    kis_live_check=failing_live_probe,
                ),
                env_file=str(env_file),
            )
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/kis-live-check", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual("virtual", state["mode"]["key"])
            self.assertEqual("실전 계좌 조회 실패", state["actionPopup"]["title"])
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)
            self.assertNotIn("12345678", rendered)

    def test_view_model_reports_full_live_credential_saved_status(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=live-state-key",
                        "KIS_LIVE_APP_SECRET=live-state-secret",
                        "KIS_LIVE_ACCOUNT_NO=12345678",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"}),
                env_file=str(env_file),
            )

            state = dashboard_state_to_view_model(controller)

            self.assertEqual(
                {"appKeySaved": True, "appSecretSaved": True, "accountNoSaved": True, "productCodeSaved": True},
                state["settings"]["kisLiveCredentials"],
            )
            rendered = json.dumps(state, ensure_ascii=False)
            self.assertNotIn("live-state-key", rendered)
            self.assertNotIn("live-state-secret", rendered)

    def test_partial_live_credentials_are_not_reported_as_fully_saved(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("KIS_LIVE_APP_KEY=live-state-key\n", encoding="utf-8")
            controller = DashboardController(
                services=DashboardServices(runtime=FakeRuntime(), symbol_names={"005930": "삼성전자"}),
                env_file=str(env_file),
            )

            state = dashboard_state_to_view_model(controller)

            self.assertEqual(
                {"appKeySaved": True, "appSecretSaved": False, "accountNoSaved": False, "productCodeSaved": False},
                state["settings"]["kisLiveCredentials"],
            )

    def test_state_requires_bridge_token(self):
        with self.assertRaises(HTTPError) as context:
            request.urlopen(f"{self.base_url}/api/state", timeout=2)

        self.assertEqual(403, context.exception.code)

    def test_state_rejects_wrong_token(self):
        with self.assertRaises(HTTPError) as context:
            request.urlopen(self._request("/api/state", method="GET", token="wrong-token"), timeout=2)

        self.assertEqual(403, context.exception.code)

    def test_state_rejects_forbidden_and_null_origins(self):
        for origin in ("http://evil.local", "null"):
            with self.subTest(origin=origin):
                with self.assertRaises(HTTPError) as context:
                    request.urlopen(
                        self._request("/api/state", method="GET", extra_headers={"Origin": origin}),
                        timeout=2,
                    )

                self.assertEqual(403, context.exception.code)

    def test_malformed_post_returns_json_400(self):
        req = self._request("/api/actions/start", data=b"{bad-json", method="POST")

        with self.assertRaises(HTTPError) as context:
            request.urlopen(req, timeout=2)

        self.assertEqual(400, context.exception.code)
        payload = json.loads(context.exception.read().decode("utf-8"))
        self.assertFalse(payload["ok"])

    def test_real_mode_start_reaches_controller_lock_without_live_runtime(self):
        mode_payload = json.dumps({"mode": "real"}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/mode", data=mode_payload, method="POST"), timeout=2):
            pass

        with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
            state = json.loads(response.read().decode("utf-8"))

        self.assertEqual("real", state["mode"]["key"])
        self.assertFalse(state["runtime"]["running"])
        self.assertFalse(self.controller.services.runtime.started)

    def test_real_mode_start_can_use_injected_live_runtime(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
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
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(state["runtime"]["running"])
            self.assertTrue(state["notice"]["locked"])

            controller._live_order_safety_context.approve_session()
            with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertTrue(live_runtime.started)
            self.assertTrue(state["runtime"]["running"])
            self.assertEqual("live", state["runtime"]["dataSourceKind"])
            self.assertFalse(state["notice"]["locked"])
            self.assertTrue(state["notice"]["orderEnabled"])
            self.assertEqual("real", state["notice"]["tone"])

    def test_real_mode_start_verifies_saved_live_account_and_starts_live_runtime(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_file = root / ".env"
            write_live_credentials_env(env_file)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(root)
            calls = {"live_probe": 0, "readiness": 0}

            def fake_live_probe(*, env_file, env, symbol):
                calls["live_probe"] += 1
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
                calls["readiness"] += 1
                self.assertTrue(kwargs.get("refresh_scanner_snapshot"))
                return successful_live_readiness(**kwargs)

            controller = DashboardController(
                services=DashboardServices(
                    runtime=paper_runtime,
                    kis_live_check=fake_live_probe,
                    live_readiness_check=fake_readiness,
                    live_runtime_builder=lambda: live_runtime,
                ),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertEqual(1, calls["live_probe"])
            self.assertEqual(1, calls["readiness"])
            self.assertFalse(paper_runtime.started)
            self.assertTrue(live_runtime.started)
            self.assertTrue(state["runtime"]["running"])
            self.assertEqual("live", state["runtime"]["dataSourceKind"])
            self.assertTrue(state["settings"]["liveOrderApproval"]["sessionApproved"])
            env_text = env_file.read_text(encoding="utf-8")
            self.assertIn("STOCKBOT_ALLOW_LIVE_TRADING=true", env_text)
            self.assertIn("STOCKBOT_LIVE_TRADING_ENABLED=true", env_text)

    def test_real_mode_start_without_managed_position_ledger_keeps_orders_locked(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir), include_managed_ledger=False)
            controller = DashboardController(
                services=DashboardServices(runtime=paper_runtime, live_runtime_builder=lambda: live_runtime),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(state["runtime"]["running"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertTrue(state["notice"]["locked"])
            self.assertEqual("danger", state["notice"]["tone"])

    def test_running_real_mode_without_managed_position_ledger_keeps_orders_locked(self):
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = make_approved_live_broker(Path(tmpdir), include_managed_ledger=False)
            controller = DashboardController(
                services=DashboardServices(runtime=live_runtime),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            controller._runtime_running = True

            state = dashboard_state_to_view_model(controller)

            self.assertTrue(state["runtime"]["running"])
            self.assertFalse(state["notice"]["orderEnabled"])
            self.assertTrue(state["notice"]["locked"])
            self.assertEqual("danger", state["notice"]["tone"])

    def test_real_mode_start_rejects_fake_live_broker_even_with_order_gate(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        with TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            write_live_order_approval_env(env_file)
            paper_runtime = FakeRuntime()
            live_runtime = FakeRuntime(data_source_kind="live", data_source_label="KIS live orders")
            live_runtime.execution_mode = "live"
            live_runtime.broker = FakeApprovedLiveBroker()
            controller = DashboardController(
                services=DashboardServices(runtime=paper_runtime, live_runtime_builder=lambda: live_runtime),
                env_file=str(env_file),
            )
            controller.select_trading_mode("real")
            self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

            with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2) as response:
                state = json.loads(response.read().decode("utf-8"))

            self.assertFalse(paper_runtime.started)
            self.assertFalse(live_runtime.started)
            self.assertFalse(state["runtime"]["running"])
            self.assertNotEqual("live", state["runtime"]["dataSourceKind"])

    def test_real_mode_blocks_paper_kis_connection_check(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        controller = ReadOnlyKisCheckController()
        self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        mode_payload = json.dumps({"mode": "real"}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/mode", data=mode_payload, method="POST"), timeout=2):
            pass

        with self.assertRaises(HTTPError) as context:
            request.urlopen(self._request("/api/actions/kis-check", data=b"{}", method="POST"), timeout=2).read()

        self.assertEqual(423, context.exception.code)
        self.assertTrue(controller.kis_live_check_ran)
        self.assertFalse(controller.kis_check_ran)

    def test_real_mode_rejects_removed_custom_settings_action_without_mutation(self):
        mode_payload = json.dumps({"mode": "real"}).encode("utf-8")
        with request.urlopen(self._request("/api/actions/mode", data=mode_payload, method="POST"), timeout=2):
            pass

        original_state = self.controller.state
        original_settings = self.controller.current_custom_settings()
        original_state_revision = self.controller.state_revision
        custom_payload = json.dumps({"cashAllocationPct": 70}).encode("utf-8")
        with self.assertRaises(HTTPError) as context:
            request.urlopen(self._request("/api/actions/custom-settings", data=custom_payload, method="POST"), timeout=2)

        self.assertEqual(400, context.exception.code)
        error = json.loads(context.exception.read().decode("utf-8"))
        self.assertIn("unknown action: custom-settings", error["error"])
        self.assertEqual(original_state, self.controller.state)
        self.assertEqual(original_settings, self.controller.current_custom_settings())
        self.assertEqual(original_state_revision, self.controller.state_revision)

    def test_bridge_serializes_concurrent_actions_against_shared_controller(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        controller = SlowActionController()
        self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        errors = []

        def post_start():
            try:
                request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2).read()
            except Exception as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        workers = [threading.Thread(target=post_start), threading.Thread(target=post_start)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)

        self.assertEqual([], errors)
        self.assertEqual(1, controller.max_active_actions)

    def test_client_disconnect_during_slow_action_does_not_print_bridge_traceback(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        controller = SlowActionController()
        self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(Exception):
                request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=0.001).read()
            time.sleep(0.2)

        rendered_stderr = stderr.getvalue()
        self.assertNotIn("Exception occurred during processing", rendered_stderr)
        self.assertNotIn("BrokenPipe", rendered_stderr)

    def test_state_and_pause_do_not_timeout_while_cycle_is_running(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        runtime = SlowCycleRuntime()
        controller = DashboardController(services=DashboardServices(runtime=runtime))
        self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        errors = []

        with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2):
            pass

        def post_cycle():
            try:
                request.urlopen(self._request("/api/actions/cycle", data=b"{}", method="POST"), timeout=3).read()
            except Exception as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        cycle_thread = threading.Thread(target=post_cycle)
        cycle_thread.start()
        self.assertTrue(runtime.in_cycle.wait(1))

        with request.urlopen(self._request("/api/state", method="GET"), timeout=0.3) as response:
            state = json.loads(response.read().decode("utf-8"))
        with request.urlopen(self._request("/api/actions/pause", data=b"{}", method="POST"), timeout=0.3) as response:
            paused = json.loads(response.read().decode("utf-8"))

        runtime.release_cycle.set()
        cycle_thread.join(timeout=3)

        self.assertEqual([], errors)
        self.assertTrue(state["runtime"]["running"])
        self.assertFalse(paused["runtime"]["running"])

    def test_state_and_pause_do_not_timeout_while_kis_check_is_running(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        controller = SlowKisCheckController()
        self.server = create_bridge_server(controller, host="127.0.0.1", port=0, token="test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        errors = []

        with request.urlopen(self._request("/api/actions/start", data=b"{}", method="POST"), timeout=2):
            pass

        def post_kis_check():
            try:
                request.urlopen(self._request("/api/actions/kis-check", data=b"{}", method="POST"), timeout=3).read()
            except Exception as exc:  # pragma: no cover - surfaced by assertion below
                errors.append(exc)

        kis_check_thread = threading.Thread(target=post_kis_check)
        kis_check_thread.start()
        self.assertTrue(controller.in_check.wait(1))

        with request.urlopen(self._request("/api/state", method="GET"), timeout=0.3) as response:
            state = json.loads(response.read().decode("utf-8"))
        with request.urlopen(self._request("/api/actions/pause", data=b"{}", method="POST"), timeout=0.3) as response:
            paused = json.loads(response.read().decode("utf-8"))

        controller.release_check.set()
        kis_check_thread.join(timeout=3)

        self.assertEqual([], errors)
        self.assertIn("running", state["runtime"])
        self.assertFalse(paused["runtime"]["running"])

    def _request(self, path, *, data=None, method="GET", token="test-token", extra_headers=None):
        headers = {
            "Content-Type": "application/json",
            "X-StockBot-Bridge-Token": token,
        }
        if extra_headers:
            headers.update(extra_headers)
        return request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )


if __name__ == "__main__":
    unittest.main()
