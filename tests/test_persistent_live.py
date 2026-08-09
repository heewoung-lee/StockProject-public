from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.persistent_live import PersistentLiveScheduler
from stockbot.dashboard import DashboardController, DashboardServices
from stockbot.kis import KisApiError


class FakeController:
    def __init__(self, *, market_open: bool, live_check_success: bool = True):
        self.state = SimpleNamespace(trading_mode="virtual")
        self._runtime_running = False
        self.market_status = SimpleNamespace(
            is_open=market_open,
            label="장중" if market_open else "장 대기",
            message="정규장이 열려 있습니다." if market_open else "정규장이 아닙니다.",
        )
        self.mode_calls: list[str] = []
        self.live_check_calls = 0
        self.live_check_success = live_check_success
        self.live_probe_revision = 0
        self.scope_verified = False
        self.start_calls = 0
        self.pause_calls = 0
        self.cycle_calls = 0

    def select_trading_mode(self, mode: str):
        self.mode_calls.append(mode)
        self.state.trading_mode = mode
        return self.state

    def run_kis_live_check(self, *, activate_real_mode: bool = True):
        self.live_check_calls += 1
        if activate_real_mode:
            self.state.trading_mode = "real"
        if self.live_check_success:
            self.live_probe_revision += 1
            self.scope_verified = True
        return self.state

    def live_account_scope_verified(self):
        return self.scope_verified

    def live_account_probe_revision(self):
        return self.live_probe_revision

    def run_persistent_live_account_probe(self):
        revision_before = self.live_probe_revision
        self.run_kis_live_check(activate_real_mode=True)
        return (
            self.live_probe_revision > revision_before
            and self.live_account_scope_verified()
        )

    def start_paper_runtime(self):
        self.start_calls += 1
        self._runtime_running = True
        return self.state

    def run_paper_cycle(self):
        self.cycle_calls += 1
        return self.state

    def pause_paper_runtime(self):
        self.pause_calls += 1
        self._runtime_running = False
        return self.state


class BlockingStartController(FakeController):
    def __init__(self):
        super().__init__(market_open=True)
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def start_paper_runtime(self):
        self.start_calls += 1
        self.start_entered.set()
        self.release_start.wait(timeout=2)
        self._runtime_running = True
        return self.state


class BlockingCycleController(FakeController):
    def __init__(self):
        super().__init__(market_open=True)
        self.cycle_entered = threading.Event()
        self.release_cycle = threading.Event()

    def run_paper_cycle(self):
        self.cycle_calls += 1
        self.cycle_entered.set()
        self.release_cycle.wait(timeout=2)
        return self.state


class ControlledStopEvent:
    def __init__(self):
        self._event = threading.Event()
        self.wait_entered = threading.Event()
        self.release_wait = threading.Event()
        self.wait_calls = 0

    def is_set(self):
        return self._event.is_set()

    def set(self):
        self._event.set()
        self.release_wait.set()

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_entered.set()
        self.release_wait.wait(timeout=2)
        return self._event.is_set()


class BlockingMonotonicClock:
    def __init__(self, now: float):
        self.now = now
        self.block_calls = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self):
        if self.block_calls:
            self.entered.set()
            self.release.wait(timeout=2)
        return self.now


class PersistentLiveSchedulerTest(unittest.TestCase):
    def make_scheduler(
        self,
        controller: FakeController,
        *,
        expected_fingerprint: str = "scope-a",
        current_fingerprint: str = "scope-a",
    ) -> PersistentLiveScheduler:
        return PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint=expected_fingerprint,
            credential_fingerprint_provider=lambda: current_fingerprint,
            market_status_provider=lambda: controller.market_status,
        )

    def test_constructor_rejects_invalid_cycle_intervals(self):
        controller = FakeController(market_open=True)
        invalid_intervals = (
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            -0.01,
        )

        for invalid_interval in invalid_intervals:
            with self.subTest(invalid_interval=invalid_interval):
                with self.assertRaises(ValueError):
                    PersistentLiveScheduler(
                        controller,
                        interval_seconds=invalid_interval,
                        expected_credential_fingerprint="scope-a",
                        credential_fingerprint_provider=lambda: "scope-a",
                        market_status_provider=lambda: controller.market_status,
                    )

    def test_initialize_selects_real_mode_without_calling_kis_outside_a_cycle(self):
        controller = FakeController(market_open=False)
        scheduler = self.make_scheduler(controller)

        scheduler.initialize()

        self.assertEqual(["real"], controller.mode_calls)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual("real", controller.state.trading_mode)
        self.assertTrue(scheduler.active)

    def test_closed_market_tick_waits_without_kis_start_or_cycle_calls(self):
        controller = FakeController(market_open=False)
        scheduler = self.make_scheduler(controller)
        scheduler.initialize()

        outcome = scheduler.run_once()

        self.assertEqual("market_closed", outcome.action)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)
        self.assertIn("장", scheduler.cycle_label)

    def test_open_market_starts_once_then_runs_cycles_on_later_ticks(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(controller)
        scheduler.initialize()

        started = scheduler.run_once()
        cycled = scheduler.run_once()

        self.assertEqual("runtime_started", started.action)
        self.assertEqual("cycle_completed", cycled.action)
        self.assertEqual(1, controller.live_check_calls)
        self.assertEqual(1, controller.start_calls)
        self.assertEqual(1, controller.cycle_calls)

    def test_failed_account_probe_does_not_fall_through_to_runtime_start(self):
        controller = FakeController(market_open=True, live_check_success=False)
        scheduler = self.make_scheduler(controller)

        first = scheduler.run_once()
        second = scheduler.run_once()

        self.assertEqual("live_account_probe_blocked", first.action)
        self.assertEqual("live_account_probe_blocked", second.action)
        self.assertEqual(2, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)

    def test_failed_account_probe_cannot_reuse_a_stale_verified_scope(self):
        controller = FakeController(market_open=True, live_check_success=False)
        controller.scope_verified = True
        controller.live_probe_revision = 7
        scheduler = self.make_scheduler(controller)

        outcome = scheduler.run_once()

        self.assertEqual("live_account_probe_blocked", outcome.action)
        self.assertEqual(1, controller.live_check_calls)
        self.assertEqual(7, controller.live_probe_revision)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)

    def test_market_close_during_account_probe_blocks_kis_and_runtime_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
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
            market_checks = 0
            kis_calls = 0

            def market_status():
                nonlocal market_checks
                market_checks += 1
                return SimpleNamespace(
                    is_open=market_checks == 1,
                    label="장중" if market_checks == 1 else "장 마감",
                )

            def forbidden_live_probe(*, symbol: str, env_file: str, env=None):
                nonlocal kis_calls
                kis_calls += 1
                raise AssertionError("persistent probe must not call KIS after market close")

            controller = DashboardController(
                env_file=str(env_file),
                services=DashboardServices(
                    kis_live_check=forbidden_live_probe,
                    kis_market_status=market_status,
                ),
            )
            scheduler = PersistentLiveScheduler(
                controller,
                interval_seconds=60,
                expected_credential_fingerprint="scope-a",
                credential_fingerprint_provider=lambda: "scope-a",
                market_status_provider=market_status,
            )

            outcome = scheduler.run_once()

        self.assertEqual("market_closed", outcome.action)
        self.assertEqual(0, kis_calls)
        self.assertFalse(controller._runtime_running)

    def test_market_close_after_account_probe_blocks_runtime_start(self):
        controller = FakeController(market_open=True)
        market_checks = 0

        def market_status():
            nonlocal market_checks
            market_checks += 1
            return SimpleNamespace(
                is_open=market_checks == 1,
                label="장중" if market_checks == 1 else "장 마감",
            )

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=market_status,
        )

        outcome = scheduler.run_once()

        self.assertEqual("market_closed", outcome.action)
        self.assertEqual(1, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)

    def test_market_close_before_runtime_cycle_pauses_without_strategy_work(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(controller)
        scheduler.run_once()
        market_checks = 0

        def closing_market_status():
            nonlocal market_checks
            market_checks += 1
            return SimpleNamespace(
                is_open=market_checks == 1,
                label="장중" if market_checks == 1 else "장 마감",
            )

        scheduler.market_status_provider = closing_market_status

        outcome = scheduler.run_once()

        self.assertEqual("market_closed", outcome.action)
        self.assertEqual(1, controller.pause_calls)
        self.assertEqual(0, controller.cycle_calls)
        self.assertFalse(controller._runtime_running)

    def test_dashboard_rate_limit_preserves_display_but_cannot_admit_scheduler(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / ".env"
            env_file.write_text(
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
            probe_calls = 0

            def live_probe(*, symbol: str, env_file: str, env=None):
                nonlocal probe_calls
                probe_calls += 1
                if probe_calls > 1:
                    raise KisApiError(
                        'KIS HTTP 403: {"error_code":"EGW00133","error_description":"rate limit 1 minute"}'
                    )
                return {
                    "account": "******78-01",
                    "cash": "100000",
                    "equity": "100000",
                    "buying_power": "100000",
                    "balance_positions": 0,
                    "last_price": "70000",
                    "read_only": True,
                    "live_order_enabled": False,
                }

            controller = DashboardController(
                env_file=str(env_file),
                services=DashboardServices(
                    kis_live_check=live_probe,
                    kis_market_status=lambda: SimpleNamespace(
                        is_open=True,
                        label="장중",
                    ),
                ),
            )
            controller.run_kis_live_check(activate_real_mode=True)
            verified_revision = controller.live_account_probe_revision()
            scheduler = PersistentLiveScheduler(
                controller,
                interval_seconds=60,
                expected_credential_fingerprint="scope-a",
                credential_fingerprint_provider=lambda: "scope-a",
                market_status_provider=lambda: SimpleNamespace(
                    is_open=True,
                    label="장중",
                ),
            )

            outcome = scheduler.run_once()
            scope_preserved = controller.live_account_scope_verified()
            account_status = controller.state.account.status

        self.assertEqual("live_account_probe_blocked", outcome.action)
        self.assertEqual(2, probe_calls)
        self.assertEqual(1, verified_revision)
        self.assertEqual(verified_revision, controller.live_account_probe_revision())
        self.assertTrue(scope_preserved)
        self.assertEqual("실전 조회 재시도 대기", account_status)
        self.assertFalse(controller._runtime_running)

    def test_market_close_pauses_runtime_and_next_open_rechecks_account(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(controller)

        scheduler.run_once()
        controller.market_status = SimpleNamespace(
            is_open=False,
            label="장 마감",
            message="정규장이 아닙니다.",
        )
        closed = scheduler.run_once()
        controller.market_status = SimpleNamespace(
            is_open=True,
            label="장중",
            message="정규장이 열려 있습니다.",
        )
        reopened = scheduler.run_once()

        self.assertEqual("market_closed", closed.action)
        self.assertEqual("runtime_started", reopened.action)
        self.assertEqual(1, controller.pause_calls)
        self.assertEqual(2, controller.live_check_calls)
        self.assertEqual(2, controller.start_calls)

    def test_changed_live_credential_scope_blocks_unattended_start(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(
            controller,
            expected_fingerprint="scope-a",
            current_fingerprint="scope-b",
        )
        scheduler.initialize()

        outcome = scheduler.run_once()

        self.assertEqual("credential_scope_changed", outcome.action)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)
        self.assertFalse(scheduler.authorized_scope)

    def test_changed_live_credential_scope_pauses_an_active_runtime(self):
        controller = FakeController(market_open=True)
        controller._runtime_running = True
        scheduler = self.make_scheduler(
            controller,
            expected_fingerprint="scope-a",
            current_fingerprint="scope-b",
        )

        outcome = scheduler.run_once()

        self.assertEqual("credential_scope_changed", outcome.action)
        self.assertEqual(1, controller.pause_calls)
        self.assertFalse(controller._runtime_running)
        self.assertEqual(0, controller.cycle_calls)

    def test_current_scope_validation_rejects_bound_disk_drift(self):
        controller = FakeController(market_open=True)
        provider_calls = 0

        def changed_fingerprint():
            nonlocal provider_calls
            provider_calls += 1
            return "scope-b"

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=changed_fingerprint,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(
            PermissionError,
            "credential scope changed",
        ):
            scheduler.validate_current_credential_scope()

        self.assertEqual(1, provider_calls)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual(0, controller.cycle_calls)

    def test_current_scope_validation_hides_provider_exception_details(self):
        controller = FakeController(market_open=True)

        def unavailable_fingerprint():
            raise OSError("KIS_LIVE_APP_SECRET=must-not-leak")

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=unavailable_fingerprint,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(
            PermissionError,
            "authorization is unavailable",
        ) as caught:
            scheduler.validate_current_credential_scope()

        self.assertNotIn("must-not-leak", str(caught.exception))

    def test_bound_scope_can_recover_only_with_the_expected_candidate(self):
        controller = FakeController(market_open=True)
        current = "b" * 64

        def current_fingerprint():
            return current

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="a" * 64,
            credential_fingerprint_provider=current_fingerprint,
            market_status_provider=lambda: controller.market_status,
        )
        self.assertFalse(scheduler.authorized_scope)

        current = "a" * 64
        rebound = scheduler.bind_saved_credential_scope("a" * 64)

        self.assertFalse(rebound)
        self.assertTrue(scheduler.authorized_scope)
        self.assertEqual("a" * 64, scheduler.expected_credential_fingerprint)

    def test_pending_credential_scope_returns_before_fingerprint_market_or_kis_calls(self):
        controller = FakeController(market_open=True)
        fingerprint_calls = 0
        market_calls = 0

        def fingerprint_provider():
            nonlocal fingerprint_calls
            fingerprint_calls += 1
            raise AssertionError("pending bootstrap must not read a bound scope")

        def market_status_provider():
            nonlocal market_calls
            market_calls += 1
            raise AssertionError("pending bootstrap must not query market status")

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="",
            credential_fingerprint_provider=fingerprint_provider,
            credential_binding_pending=True,
            market_status_provider=market_status_provider,
        )

        outcome = scheduler.run_once()

        self.assertEqual("credential_scope_pending", outcome.action)
        self.assertEqual(0, fingerprint_calls)
        self.assertEqual(0, market_calls)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)
        self.assertTrue(scheduler.credential_binding_pending)

    def test_pending_scope_binds_once_after_persistence_and_is_idempotent(self):
        controller = FakeController(market_open=False)
        fingerprint = "a" * 64
        persisted: list[str] = []
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="",
            credential_fingerprint_provider=lambda: fingerprint,
            credential_binding_pending=True,
            credential_scope_persistence_callback=persisted.append,
            market_status_provider=lambda: controller.market_status,
        )

        first = scheduler.bind_saved_credential_scope(fingerprint)
        scheduler._live_probe_complete = True
        second = scheduler.bind_saved_credential_scope(fingerprint)

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual([fingerprint], persisted)
        self.assertFalse(scheduler.credential_binding_pending)
        self.assertEqual(fingerprint, scheduler.expected_credential_fingerprint)
        self.assertTrue(scheduler.authorized_scope)
        self.assertFalse(scheduler._live_probe_complete)

    def test_pending_scope_stays_pending_when_persistence_fails(self):
        controller = FakeController(market_open=False)

        def fail_persistence(_fingerprint: str):
            raise OSError("KIS_LIVE_APP_SECRET=must-not-leak")

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="",
            credential_fingerprint_provider=lambda: "a" * 64,
            credential_binding_pending=True,
            credential_scope_persistence_callback=fail_persistence,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(RuntimeError, "persistence failed") as caught:
            scheduler.bind_saved_credential_scope("a" * 64)

        self.assertNotIn("must-not-leak", str(caught.exception))
        self.assertTrue(scheduler.credential_binding_pending)
        self.assertEqual("", scheduler.expected_credential_fingerprint)
        self.assertFalse(scheduler.authorized_scope)

    def test_pending_scope_rejects_invalid_current_fingerprint_before_persistence(self):
        controller = FakeController(market_open=False)
        persisted: list[str] = []
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="",
            credential_fingerprint_provider=lambda: "not-a-fingerprint",
            credential_binding_pending=True,
            credential_scope_persistence_callback=persisted.append,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(ValueError, "fingerprint is invalid"):
            scheduler.bind_saved_credential_scope("a" * 64)

        self.assertEqual([], persisted)
        self.assertTrue(scheduler.credential_binding_pending)

    def test_bound_scope_change_requires_reinstallation_and_does_not_persist(self):
        controller = FakeController(market_open=False)
        persisted: list[str] = []
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="a" * 64,
            credential_fingerprint_provider=lambda: "b" * 64,
            credential_binding_pending=False,
            credential_scope_persistence_callback=persisted.append,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(PermissionError, "reinstall"):
            scheduler.bind_saved_credential_scope("b" * 64)

        self.assertEqual([], persisted)
        self.assertFalse(scheduler.credential_binding_pending)
        self.assertEqual("a" * 64, scheduler.expected_credential_fingerprint)

    def test_candidate_scope_is_checked_against_disk_before_pending_persistence(self):
        controller = FakeController(market_open=False)
        candidate = "a" * 64
        persisted: list[str] = []
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="",
            credential_fingerprint_provider=lambda: "b" * 64,
            credential_binding_pending=True,
            credential_scope_persistence_callback=persisted.append,
            market_status_provider=lambda: controller.market_status,
        )

        scheduler.validate_candidate_credential_scope(candidate)
        with self.assertRaisesRegex(PermissionError, "saved credentials"):
            scheduler.bind_saved_credential_scope(candidate)

        self.assertEqual([], persisted)
        self.assertTrue(scheduler.credential_binding_pending)
        self.assertEqual("", scheduler.expected_credential_fingerprint)

    def test_bound_candidate_is_rejected_before_current_scope_is_read(self):
        controller = FakeController(market_open=False)
        provider_calls = 0

        def fingerprint_provider():
            nonlocal provider_calls
            provider_calls += 1
            return "a" * 64

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="a" * 64,
            credential_fingerprint_provider=fingerprint_provider,
            credential_binding_pending=False,
            market_status_provider=lambda: controller.market_status,
        )

        with self.assertRaisesRegex(PermissionError, "reinstall"):
            scheduler.validate_candidate_credential_scope("b" * 64)

        self.assertEqual(0, provider_calls)

    def test_manual_pause_stays_suspended_until_explicit_resume(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(controller)
        scheduler.initialize()

        scheduler.suspend()
        suspended = scheduler.run_once()
        self.assertEqual(0, controller.start_calls)
        scheduler.resume()
        resumed = scheduler.run_once()

        self.assertEqual("manual_pause", suspended.action)
        self.assertEqual("runtime_started", resumed.action)
        self.assertEqual(1, controller.start_calls)

    def test_run_forever_exposes_monotonic_countdown_only_while_waiting(self):
        controller = FakeController(market_open=True)
        stop_event = ControlledStopEvent()
        monotonic_now = [100.0]
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            stop_event=stop_event,
            monotonic_provider=lambda: monotonic_now[0],
        )
        worker = threading.Thread(target=scheduler.run_forever)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(scheduler.stop)
        self.assertTrue(stop_event.wait_entered.wait(timeout=1))

        waiting_snapshot = scheduler.timing_snapshot()
        self.assertTrue(waiting_snapshot.active)
        self.assertFalse(waiting_snapshot.cycle_in_progress)
        self.assertEqual(60.0, waiting_snapshot.seconds_until_next_cycle)
        monotonic_now[0] += 15
        self.assertEqual(45.0, scheduler.seconds_until_next_cycle)

        scheduler.suspend()
        paused_snapshot = scheduler.timing_snapshot()
        self.assertFalse(paused_snapshot.active)
        self.assertFalse(paused_snapshot.cycle_in_progress)
        self.assertIsNone(paused_snapshot.seconds_until_next_cycle)
        monotonic_now[0] += 10
        scheduler.resume()
        resumed_snapshot = scheduler.timing_snapshot()
        self.assertTrue(resumed_snapshot.active)
        self.assertFalse(resumed_snapshot.cycle_in_progress)
        self.assertEqual(35.0, resumed_snapshot.seconds_until_next_cycle)
        scheduler.stop()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertIsNone(scheduler.seconds_until_next_cycle)
        self.assertFalse(scheduler.cycle_in_progress)

    def test_safe_cycles_publish_non_sensitive_scheduler_timing_aggregates(self):
        controller = FakeController(market_open=True)
        controller._runtime_running = True
        telemetry_times = iter((100.0, 104.0, 120.0, 126.0))
        completion_times = iter(
            (
                datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc),
                datetime(2026, 7, 30, 1, 2, 29, tzinfo=timezone.utc),
            )
        )
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            telemetry_monotonic_provider=lambda: next(telemetry_times),
            completion_time_provider=lambda: next(completion_times),
        )

        first = scheduler.run_safely_once()
        first_snapshot = scheduler.timing_snapshot()
        second = scheduler.run_safely_once()
        second_snapshot = scheduler.timing_snapshot()

        self.assertEqual("cycle_completed", first.action)
        self.assertEqual(15.0, first_snapshot.configured_idle_seconds)
        self.assertEqual(4.0, first_snapshot.last_cycle_duration_seconds)
        self.assertIsNone(first_snapshot.last_cycle_start_interval_seconds)
        self.assertEqual(1, first_snapshot.cycle_duration_sample_count)
        self.assertEqual(4.0, first_snapshot.cycle_duration_p95_seconds)
        self.assertEqual(0, first_snapshot.cycle_start_interval_sample_count)
        self.assertIsNone(first_snapshot.cycle_start_interval_p95_seconds)
        self.assertEqual("cycle_completed", first_snapshot.current_action)
        self.assertEqual(
            "2026-07-30T01:02:03+00:00",
            first_snapshot.last_cycle_completed_at,
        )

        self.assertEqual("cycle_completed", second.action)
        self.assertEqual(6.0, second_snapshot.last_cycle_duration_seconds)
        self.assertEqual(20.0, second_snapshot.last_cycle_start_interval_seconds)
        self.assertEqual(2, second_snapshot.cycle_duration_sample_count)
        self.assertEqual(6.0, second_snapshot.cycle_duration_p95_seconds)
        self.assertEqual(1, second_snapshot.cycle_start_interval_sample_count)
        self.assertEqual(20.0, second_snapshot.cycle_start_interval_p95_seconds)
        self.assertEqual("cycle_completed", second_snapshot.current_action)
        self.assertEqual(
            "2026-07-30T01:02:29+00:00",
            second_snapshot.last_cycle_completed_at,
        )

    def test_timing_snapshot_marks_the_current_cycle_action_without_completion_data(self):
        controller = BlockingCycleController()
        controller._runtime_running = True
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            telemetry_monotonic_provider=lambda: 100.0,
        )
        worker = threading.Thread(target=scheduler.run_safely_once)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(controller.release_cycle.set)

        self.assertTrue(controller.cycle_entered.wait(timeout=1))
        snapshot = scheduler.timing_snapshot()

        self.assertTrue(snapshot.cycle_in_progress)
        self.assertEqual("cycle_running", snapshot.current_action)
        self.assertIsNone(snapshot.last_cycle_duration_seconds)
        self.assertIsNone(snapshot.last_cycle_completed_at)
        controller.release_cycle.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())

    def test_non_trading_ticks_do_not_dilute_completed_cycle_timing_samples(self):
        controller = FakeController(market_open=False)
        telemetry_times = iter((100.0, 100.1, 115.1, 115.2))
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            telemetry_monotonic_provider=lambda: next(telemetry_times),
        )

        first = scheduler.run_safely_once()
        second = scheduler.run_safely_once()
        snapshot = scheduler.timing_snapshot()

        self.assertEqual("market_closed", first.action)
        self.assertEqual("market_closed", second.action)
        self.assertEqual(0, snapshot.cycle_duration_sample_count)
        self.assertIsNone(snapshot.cycle_duration_p95_seconds)
        self.assertEqual(0, snapshot.cycle_start_interval_sample_count)
        self.assertIsNone(snapshot.cycle_start_interval_p95_seconds)
        self.assertIsNone(snapshot.last_cycle_completed_at)

    def test_market_close_resets_completed_cycle_start_interval_baseline(self):
        controller = FakeController(market_open=True)
        controller._runtime_running = True
        telemetry_times = iter(
            (100.0, 104.0, 1000.0, 1000.1, 2000.0, 2000.1, 2020.0, 2024.0)
        )
        completion_times = iter(
            (
                datetime(2026, 7, 30, 1, 2, 3, tzinfo=timezone.utc),
                datetime(2026, 7, 31, 1, 2, 3, tzinfo=timezone.utc),
            )
        )
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=15,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            telemetry_monotonic_provider=lambda: next(telemetry_times),
            completion_time_provider=lambda: next(completion_times),
        )

        first = scheduler.run_safely_once()
        controller.market_status.is_open = False
        closed = scheduler.run_safely_once()
        controller.market_status.is_open = True
        restarted = scheduler.run_safely_once()
        second = scheduler.run_safely_once()
        snapshot = scheduler.timing_snapshot()

        self.assertEqual("cycle_completed", first.action)
        self.assertEqual("market_closed", closed.action)
        self.assertEqual("runtime_started", restarted.action)
        self.assertEqual("cycle_completed", second.action)
        self.assertEqual(2, snapshot.cycle_duration_sample_count)
        self.assertEqual(0, snapshot.cycle_start_interval_sample_count)
        self.assertIsNone(snapshot.last_cycle_start_interval_seconds)
        self.assertIsNone(snapshot.cycle_start_interval_p95_seconds)

    def test_run_forever_clears_deadline_before_cycle_execution(self):
        controller = BlockingCycleController()
        stop_event = ControlledStopEvent()
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            stop_event=stop_event,
            monotonic_provider=lambda: 100.0,
        )
        worker = threading.Thread(target=scheduler.run_forever)
        worker.start()
        self.addCleanup(worker.join, 1)
        self.addCleanup(controller.release_cycle.set)
        self.addCleanup(scheduler.stop)
        self.assertTrue(stop_event.wait_entered.wait(timeout=1))
        self.assertEqual(60.0, scheduler.seconds_until_next_cycle)
        self.assertFalse(scheduler.cycle_in_progress)

        stop_event.release_wait.set()
        self.assertTrue(controller.cycle_entered.wait(timeout=1))

        self.assertIsNone(scheduler.seconds_until_next_cycle)
        self.assertTrue(scheduler.cycle_in_progress)
        scheduler.stop()
        controller.release_cycle.set()
        worker.join(timeout=1)

        self.assertFalse(worker.is_alive())
        self.assertIsNone(scheduler.seconds_until_next_cycle)
        self.assertFalse(scheduler.cycle_in_progress)

    def test_timing_snapshot_retries_when_cycle_starts_during_monotonic_read(self):
        controller = BlockingCycleController()
        stop_event = ControlledStopEvent()
        monotonic_clock = BlockingMonotonicClock(100.0)
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            stop_event=stop_event,
            monotonic_provider=monotonic_clock,
        )
        worker = threading.Thread(target=scheduler.run_forever)
        reader = None
        worker.start()
        try:
            self.assertTrue(stop_event.wait_entered.wait(timeout=1))
            waiting_snapshot = scheduler.timing_snapshot()
            self.assertTrue(waiting_snapshot.active)
            self.assertFalse(waiting_snapshot.cycle_in_progress)
            self.assertEqual(60.0, waiting_snapshot.seconds_until_next_cycle)

            monotonic_clock.block_calls = True
            snapshots = []
            snapshot_errors = []

            def read_snapshot():
                try:
                    snapshots.append(scheduler.timing_snapshot())
                except Exception as error:
                    snapshot_errors.append(error)

            reader = threading.Thread(target=read_snapshot)
            reader.start()
            self.assertTrue(monotonic_clock.entered.wait(timeout=1))

            stop_event.release_wait.set()
            self.assertTrue(controller.cycle_entered.wait(timeout=1))

            monotonic_clock.release.set()
            reader.join(timeout=1)

            self.assertFalse(reader.is_alive())
            self.assertEqual([], snapshot_errors)
            self.assertEqual(1, len(snapshots))
            self.assertTrue(snapshots[0].active)
            self.assertEqual(60.0, snapshots[0].interval_seconds)
            self.assertTrue(snapshots[0].cycle_in_progress)
            self.assertIsNone(snapshots[0].seconds_until_next_cycle)
        finally:
            monotonic_clock.release.set()
            scheduler.stop()
            controller.release_cycle.set()
            if reader is not None:
                reader.join(timeout=1)
            worker.join(timeout=1)

    def test_timing_snapshot_retries_when_paused_during_monotonic_read(self):
        controller = FakeController(market_open=True)
        stop_event = ControlledStopEvent()
        monotonic_clock = BlockingMonotonicClock(100.0)
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: controller.market_status,
            stop_event=stop_event,
            monotonic_provider=monotonic_clock,
        )
        worker = threading.Thread(target=scheduler.run_forever)
        reader = None
        worker.start()
        try:
            self.assertTrue(stop_event.wait_entered.wait(timeout=1))
            monotonic_clock.block_calls = True
            snapshots = []
            snapshot_errors = []

            def read_snapshot():
                try:
                    snapshots.append(scheduler.timing_snapshot())
                except Exception as error:
                    snapshot_errors.append(error)

            reader = threading.Thread(target=read_snapshot)
            reader.start()
            self.assertTrue(monotonic_clock.entered.wait(timeout=1))

            scheduler.suspend()
            monotonic_clock.release.set()
            reader.join(timeout=1)

            self.assertFalse(reader.is_alive())
            self.assertEqual([], snapshot_errors)
            self.assertEqual(1, len(snapshots))
            self.assertFalse(snapshots[0].active)
            self.assertFalse(snapshots[0].cycle_in_progress)
            self.assertIsNone(snapshots[0].seconds_until_next_cycle)

            scheduler.resume()
            resumed_snapshot = scheduler.timing_snapshot()
            self.assertTrue(resumed_snapshot.active)
            self.assertFalse(resumed_snapshot.cycle_in_progress)
            self.assertEqual(60.0, resumed_snapshot.seconds_until_next_cycle)
        finally:
            monotonic_clock.release.set()
            scheduler.stop()
            if reader is not None:
                reader.join(timeout=1)
            worker.join(timeout=1)

    def test_real_mode_is_restored_before_an_open_market_start(self):
        controller = FakeController(market_open=True)
        scheduler = self.make_scheduler(controller)
        scheduler.initialize()
        controller.state.trading_mode = "virtual"

        outcome = scheduler.run_once()

        self.assertEqual("runtime_started", outcome.action)
        self.assertEqual(["real", "real"], controller.mode_calls)
        self.assertEqual(1, controller.live_check_calls)

    def test_unknown_market_status_fails_closed(self):
        controller = FakeController(market_open=True)
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: None,
        )
        scheduler.initialize()

        outcome = scheduler.run_once()

        self.assertEqual("market_status_unavailable", outcome.action)
        self.assertEqual(0, controller.live_check_calls)
        self.assertEqual(0, controller.start_calls)
        self.assertEqual(0, controller.cycle_calls)

    def test_unknown_market_status_pauses_an_active_runtime(self):
        controller = FakeController(market_open=True)
        controller._runtime_running = True
        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=lambda: None,
        )

        outcome = scheduler.run_once()

        self.assertEqual("market_status_unavailable", outcome.action)
        self.assertEqual(1, controller.pause_calls)
        self.assertFalse(controller._runtime_running)

    def test_scheduler_failure_records_sanitized_stage_and_type_without_message(self):
        controller = FakeController(market_open=True)

        def failed_market_status():
            raise RuntimeError("KIS secret account detail must not appear")

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=failed_market_status,
        )

        outcome = scheduler.run_safely_once()

        self.assertEqual("scheduler_error", outcome.action)
        self.assertEqual(1, scheduler.consecutive_failures)
        self.assertEqual("market_status", scheduler.last_error_stage)
        self.assertEqual("RuntimeError", scheduler.last_error_code)
        self.assertIn("연속 1회", scheduler.cycle_label)
        self.assertNotIn("secret", scheduler.cycle_label.lower())
        self.assertNotIn("account detail", scheduler.cycle_label.lower())

    def test_successful_scheduler_tick_clears_previous_failure_diagnostics(self):
        controller = FakeController(market_open=False)
        attempts = 0

        def market_status():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("sensitive request detail")
            return controller.market_status

        scheduler = PersistentLiveScheduler(
            controller,
            interval_seconds=60,
            expected_credential_fingerprint="scope-a",
            credential_fingerprint_provider=lambda: "scope-a",
            market_status_provider=market_status,
        )

        failed = scheduler.run_safely_once()
        recovered = scheduler.run_safely_once()

        self.assertEqual("scheduler_error", failed.action)
        self.assertEqual("market_closed", recovered.action)
        self.assertEqual(0, scheduler.consecutive_failures)
        self.assertEqual("", scheduler.last_error_stage)
        self.assertEqual("", scheduler.last_error_code)

    def test_state_properties_remain_readable_during_a_slow_runtime_start(self):
        controller = BlockingStartController()
        scheduler = self.make_scheduler(controller)
        scheduler.initialize()
        worker = threading.Thread(target=scheduler.run_once)
        worker.start()
        self.assertTrue(controller.start_entered.wait(timeout=1))

        state_read_finished = threading.Event()

        def read_state():
            _ = scheduler.active
            _ = scheduler.cycle_label
            state_read_finished.set()

        reader = threading.Thread(target=read_state)
        reader.start()
        self.assertTrue(state_read_finished.wait(timeout=0.2))

        controller.release_start.set()
        worker.join(timeout=1)
        reader.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
