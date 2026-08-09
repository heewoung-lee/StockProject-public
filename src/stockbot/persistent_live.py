from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil, isfinite
import re
from threading import Event, RLock
from time import monotonic
from typing import Callable, Protocol


_ERROR_CODE_PATTERN = re.compile(r"[^A-Za-z0-9_]")
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TIMING_SAMPLE_LIMIT = 120
_CADENCE_RESET_ACTIONS = frozenset(
    {
        "service_stopped",
        "manual_pause",
        "credential_scope_pending",
        "credential_scope_changed",
        "market_status_unavailable",
        "market_closed",
        "live_account_probe_blocked",
        "runtime_started",
        "runtime_start_blocked",
    }
)


class PersistentLiveController(Protocol):
    state: object
    _runtime_running: bool

    def select_trading_mode(self, mode: str): ...

    def run_kis_live_check(self, *, activate_real_mode: bool = True): ...

    def live_account_scope_verified(self) -> bool: ...

    def live_account_probe_revision(self) -> int: ...

    def run_persistent_live_account_probe(self) -> bool: ...

    def start_paper_runtime(self): ...

    def pause_paper_runtime(self): ...

    def run_paper_cycle(self): ...


@dataclass(frozen=True)
class ServiceCycleOutcome:
    action: str
    runtime_running: bool
    market_open: bool | None


@dataclass(frozen=True)
class SchedulerTimingSnapshot:
    active: bool
    interval_seconds: float
    cycle_in_progress: bool
    seconds_until_next_cycle: float | None
    configured_idle_seconds: float | None = None
    last_cycle_duration_seconds: float | None = None
    last_cycle_start_interval_seconds: float | None = None
    cycle_duration_sample_count: int = 0
    cycle_duration_p95_seconds: float | None = None
    cycle_start_interval_sample_count: int = 0
    cycle_start_interval_p95_seconds: float | None = None
    current_action: str = "not_started"
    last_cycle_completed_at: str | None = None


class PersistentLiveScheduler:
    """Own the unattended live lifecycle while keeping all order gates in the controller."""

    def __init__(
        self,
        controller: PersistentLiveController,
        *,
        interval_seconds: float,
        expected_credential_fingerprint: str,
        credential_fingerprint_provider: Callable[[], str],
        credential_binding_pending: bool = False,
        credential_scope_persistence_callback: Callable[[str], object] | None = None,
        market_status_provider: Callable[[], object | None] | None = None,
        stop_event: Event | None = None,
        monotonic_provider: Callable[[], float] | None = None,
        telemetry_monotonic_provider: Callable[[], float] | None = None,
        completion_time_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if isinstance(interval_seconds, bool):
            raise ValueError("persistent live cycle interval must be positive")
        try:
            parsed_interval_seconds = float(interval_seconds)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("persistent live cycle interval must be positive") from error
        if not isfinite(parsed_interval_seconds) or parsed_interval_seconds <= 0:
            raise ValueError("persistent live cycle interval must be positive")
        self.controller = controller
        self.interval_seconds = parsed_interval_seconds
        self.expected_credential_fingerprint = expected_credential_fingerprint.strip()
        self.credential_fingerprint_provider = credential_fingerprint_provider
        self._credential_binding_pending = credential_binding_pending is True
        self.credential_scope_persistence_callback = (
            credential_scope_persistence_callback
        )
        self.market_status_provider = market_status_provider or self._default_market_status
        self.stop_event = stop_event or Event()
        self.monotonic_provider = (
            monotonic if monotonic_provider is None else monotonic_provider
        )
        self.telemetry_monotonic_provider = (
            monotonic
            if telemetry_monotonic_provider is None
            else telemetry_monotonic_provider
        )
        self.completion_time_provider = (
            self._utc_now
            if completion_time_provider is None
            else completion_time_provider
        )
        self._lock = RLock()
        self._initialized = False
        self._initializing = False
        self._live_probe_complete = False
        self._suspended = False
        self._current_stage = "not_started"
        self._consecutive_failures = 0
        self._last_error_stage = ""
        self._last_error_code = ""
        self._next_cycle_deadline_monotonic: float | None = None
        self._cycle_in_progress = False
        self._timing_version = 0
        self._last_completed_cycle_started_monotonic: float | None = None
        self._last_cycle_duration_seconds: float | None = None
        self._last_cycle_start_interval_seconds: float | None = None
        self._cycle_duration_samples: deque[float] = deque(
            maxlen=_TIMING_SAMPLE_LIMIT
        )
        self._cycle_start_interval_samples: deque[float] = deque(
            maxlen=_TIMING_SAMPLE_LIMIT
        )
        self._current_action = "not_started"
        self._last_cycle_completed_at: str | None = None
        self._cycle_label = "백그라운드 서비스 시작 대기"
        self._last_outcome = ServiceCycleOutcome(
            action="not_started",
            runtime_running=False,
            market_open=None,
        )

    @property
    def active(self) -> bool:
        with self._lock:
            return self._initialized and not self._suspended and not self.stop_event.is_set()

    @property
    def cycle_in_progress(self) -> bool:
        with self._lock:
            return self._cycle_in_progress

    @property
    def authorized_scope(self) -> bool:
        try:
            self.validate_current_credential_scope()
        except PermissionError:
            return False
        return True

    def validate_current_credential_scope(self) -> None:
        with self._lock:
            if self._credential_binding_pending:
                raise PermissionError("credential bootstrap is pending")
            expected = self.expected_credential_fingerprint
        if not expected:
            raise PermissionError("credential scope authorization is unavailable")
        try:
            current = str(self.credential_fingerprint_provider() or "").strip()
        except Exception:
            raise PermissionError(
                "credential scope authorization is unavailable"
            ) from None
        if not current or current != expected:
            raise PermissionError(
                "saved credential scope changed; restore the bound scope"
            )

    @property
    def credential_binding_pending(self) -> bool:
        with self._lock:
            return self._credential_binding_pending

    @property
    def cycle_label(self) -> str:
        with self._lock:
            return self._cycle_label

    @property
    def seconds_until_next_cycle(self) -> float | None:
        return self.timing_snapshot().seconds_until_next_cycle

    def timing_snapshot(self) -> SchedulerTimingSnapshot:
        for _ in range(3):
            with self._lock:
                version = self._timing_version
                initialized = self._initialized
                suspended = self._suspended
                stopped = self.stop_event.is_set()
                cycle_in_progress = self._cycle_in_progress
                deadline = self._next_cycle_deadline_monotonic
                active = initialized and not suspended and not stopped
                if (
                    not initialized
                    or suspended
                    or stopped
                    or cycle_in_progress
                    or deadline is None
                ):
                    return self._build_timing_snapshot(
                        active=active,
                        cycle_in_progress=cycle_in_progress,
                        seconds_until_next_cycle=None,
                    )

            now = self._monotonic_time()

            with self._lock:
                if (
                    self._timing_version != version
                    or self._next_cycle_deadline_monotonic != deadline
                    or self.stop_event.is_set() != stopped
                ):
                    continue
                remaining = None if now is None else max(0.0, deadline - now)
                return self._build_timing_snapshot(
                    active=active,
                    cycle_in_progress=cycle_in_progress,
                    seconds_until_next_cycle=remaining,
                )

        with self._lock:
            stopped = self.stop_event.is_set()
            return self._build_timing_snapshot(
                active=self._initialized and not self._suspended and not stopped,
                cycle_in_progress=self._cycle_in_progress,
                seconds_until_next_cycle=None,
            )

    @property
    def last_outcome(self) -> ServiceCycleOutcome:
        with self._lock:
            return self._last_outcome

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    @property
    def last_error_stage(self) -> str:
        with self._lock:
            return self._last_error_stage

    @property
    def last_error_code(self) -> str:
        with self._lock:
            return self._last_error_code

    def initialize(self) -> None:
        with self._lock:
            if self._initialized or self._initializing:
                return
            self._initializing = True
        try:
            self._select_real_mode()
        except Exception:
            with self._lock:
                self._initializing = False
            raise
        with self._lock:
            self._initialized = True
            self._initializing = False
            self._timing_version += 1
            self._cycle_label = "시장 상태 확인 대기"

    def suspend(self) -> None:
        with self._lock:
            if not self._suspended:
                self._suspended = True
                self._timing_version += 1
            self._cycle_label = "백그라운드 자동매매 수동 일시정지"

    def resume(self) -> None:
        with self._lock:
            if self._suspended:
                self._suspended = False
                self._timing_version += 1
            self._cycle_label = "시장 상태 확인 대기"

    def stop(self) -> None:
        with self._lock:
            self.stop_event.set()
            self._next_cycle_deadline_monotonic = None
            self._timing_version += 1
            self._cycle_label = "백그라운드 서비스 종료"

    def validate_candidate_credential_scope(
        self,
        candidate_fingerprint: str,
    ) -> None:
        candidate = str(candidate_fingerprint or "").strip()
        if not _FINGERPRINT_PATTERN.fullmatch(candidate):
            raise ValueError("candidate credential scope fingerprint is invalid")
        with self._lock:
            if (
                not self._credential_binding_pending
                and candidate != self.expected_credential_fingerprint
            ):
                raise PermissionError(
                    "saved credential scope changed; reinstall the Windows service"
                )

    def bind_saved_credential_scope(
        self,
        candidate_fingerprint: str,
    ) -> bool:
        candidate = str(candidate_fingerprint or "").strip()
        self.validate_candidate_credential_scope(candidate)
        with self._lock:
            try:
                current = str(self.credential_fingerprint_provider() or "").strip()
            except Exception:
                raise RuntimeError(
                    "saved credential scope fingerprint is unavailable"
                ) from None
            if not _FINGERPRINT_PATTERN.fullmatch(current):
                raise ValueError("saved credential scope fingerprint is invalid")
            if current != candidate:
                raise PermissionError(
                    "saved credentials do not match the verified candidate scope"
                )
            if not self._credential_binding_pending:
                self._live_probe_complete = False
                self._cycle_label = (
                    "실전 계좌 범위 재확인 대기 - 시장 상태 확인 대기"
                )
                self._timing_version += 1
                return False
            persist = self.credential_scope_persistence_callback
            if not callable(persist):
                raise RuntimeError(
                    "credential scope binding persistence is unavailable"
                )
            try:
                persist(candidate)
            except Exception:
                raise RuntimeError(
                    "credential scope binding persistence failed"
                ) from None
            self.expected_credential_fingerprint = candidate
            self._credential_binding_pending = False
            self._live_probe_complete = False
            self._cycle_label = "실전 계좌 범위 결속 완료 - 시장 상태 확인 대기"
            self._timing_version += 1
            return True

    def run_forever(self) -> None:
        try:
            while not self.stop_event.is_set():
                self.run_safely_once()
                self._schedule_next_cycle_deadline()
                self.stop_event.wait(self.interval_seconds)
        finally:
            self._clear_next_cycle_deadline()

    def run_safely_once(self) -> ServiceCycleOutcome:
        cycle_started_monotonic = self._telemetry_monotonic_time()
        with self._lock:
            self._next_cycle_deadline_monotonic = None
            self._cycle_in_progress = True
            self._current_action = "cycle_running"
            self._timing_version += 1
        try:
            try:
                outcome = self.run_once()
            except Exception as error:
                return self._record_scheduler_failure(error)
            self._clear_scheduler_failure()
            return outcome
        finally:
            cycle_completed_monotonic = self._telemetry_monotonic_time()
            with self._lock:
                completed_action = self._last_outcome.action
                if completed_action == "cycle_completed":
                    self._last_cycle_start_interval_seconds = self._elapsed_seconds(
                        self._last_completed_cycle_started_monotonic,
                        cycle_started_monotonic,
                    )
                    if self._last_cycle_start_interval_seconds is not None:
                        self._cycle_start_interval_samples.append(
                            self._last_cycle_start_interval_seconds
                        )
                    if cycle_started_monotonic is not None:
                        self._last_completed_cycle_started_monotonic = (
                            cycle_started_monotonic
                        )

                    self._last_cycle_duration_seconds = self._elapsed_seconds(
                        cycle_started_monotonic,
                        cycle_completed_monotonic,
                    )
                    if self._last_cycle_duration_seconds is not None:
                        self._cycle_duration_samples.append(
                            self._last_cycle_duration_seconds
                        )
                    self._last_cycle_completed_at = self._completion_timestamp()
                elif completed_action in _CADENCE_RESET_ACTIONS:
                    self._last_completed_cycle_started_monotonic = None
                self._current_action = completed_action
                self._cycle_in_progress = False
                self._timing_version += 1

    def run_once(self) -> ServiceCycleOutcome:
        self._set_stage("initialize")
        self.initialize()
        with self._lock:
            if self.stop_event.is_set():
                return self._record_outcome("service_stopped", market_open=None)
            if self._suspended:
                self._cycle_label = "백그라운드 자동매매 수동 일시정지"
                return self._record_outcome("manual_pause", market_open=None)
            credential_binding_pending = self._credential_binding_pending
        if credential_binding_pending:
            self._set_stage("credential_scope_pending")
            self._pause_runtime_for_recheck()
            self._set_cycle_label("실전 계좌 설정 입력 대기 - 주문 및 KIS 조회 차단")
            return self._record_outcome(
                "credential_scope_pending",
                market_open=None,
            )
        self._set_stage("credential_scope")
        if not self.authorized_scope:
            self._pause_runtime_for_recheck()
            self._set_cycle_label("실전 계좌 설정 변경 감지 - 서비스 재승인 필요")
            return self._record_outcome("credential_scope_changed", market_open=None)
        if str(getattr(self.controller.state, "trading_mode", "") or "") != "real":
            self._set_stage("real_mode")
            self._select_real_mode()

        market_gate_outcome = self._check_market_gate(stage="market_status")
        if market_gate_outcome is not None:
            return market_gate_outcome

        self._set_stage("account_probe")
        if not self._ensure_live_account_probe():
            market_gate_outcome = self._check_market_gate(
                stage="market_status_after_account_probe"
            )
            if market_gate_outcome is not None:
                return market_gate_outcome
            self._set_cycle_label("실전 계좌 조회 안전 확인 대기 - 다음 주기에 재시도")
            return self._record_outcome("live_account_probe_blocked", market_open=True)
        market_gate_outcome = self._check_market_gate(
            stage="market_status_before_runtime"
        )
        if market_gate_outcome is not None:
            return market_gate_outcome
        with self._lock:
            if self._suspended:
                self._cycle_label = "백그라운드 자동매매 수동 일시정지"
                return self._record_outcome("manual_pause", market_open=True)
        if not bool(getattr(self.controller, "_runtime_running", False)):
            self._set_stage("runtime_start")
            self.controller.start_paper_runtime()
            if bool(getattr(self.controller, "_runtime_running", False)):
                self._set_cycle_label("실전 runtime 시작 - 다음 주기부터 거래 판단")
                return self._record_outcome("runtime_started", market_open=True)
            self._set_cycle_label("실전 시작 안전 게이트 대기 - 다음 주기에 재시도")
            return self._record_outcome("runtime_start_blocked", market_open=True)

        self._set_stage("runtime_cycle")
        self.controller.run_paper_cycle()
        self._set_cycle_label("백그라운드 서비스가 다음 cycle 예약")
        return self._record_outcome("cycle_completed", market_open=True)

    def _check_market_gate(self, *, stage: str) -> ServiceCycleOutcome | None:
        self._set_stage(stage)
        market_status = self.market_status_provider()
        if market_status is None or not hasattr(market_status, "is_open"):
            self._pause_runtime_for_recheck()
            self._set_cycle_label("시장 상태 확인 불가 - 주문 차단")
            return self._record_outcome("market_status_unavailable", market_open=None)
        if bool(getattr(market_status, "is_open", False)):
            return None
        self._pause_runtime_for_recheck()
        label = str(getattr(market_status, "label", "") or "장 대기")
        self._set_cycle_label(f"{label} - 백그라운드 대기")
        return self._record_outcome("market_closed", market_open=False)

    def _select_real_mode(self) -> None:
        if str(getattr(self.controller.state, "trading_mode", "") or "") != "real":
            self.controller.select_trading_mode("real")
            with self._lock:
                self._live_probe_complete = False

    def _ensure_live_account_probe(self) -> bool:
        with self._lock:
            if self._live_probe_complete:
                return True
        probe = getattr(self.controller, "run_persistent_live_account_probe", None)
        if not callable(probe) or probe() is not True:
            return False
        with self._lock:
            self._live_probe_complete = True
        return True

    def _pause_runtime_for_recheck(self) -> None:
        if bool(getattr(self.controller, "_runtime_running", False)):
            self.controller.pause_paper_runtime()
        with self._lock:
            self._live_probe_complete = False

    def _default_market_status(self) -> object | None:
        services = getattr(self.controller, "services", None)
        provider = getattr(services, "kis_market_status", None)
        return provider() if callable(provider) else None

    def _record_outcome(self, action: str, *, market_open: bool | None) -> ServiceCycleOutcome:
        outcome = ServiceCycleOutcome(
            action=action,
            runtime_running=bool(getattr(self.controller, "_runtime_running", False)),
            market_open=market_open,
        )
        with self._lock:
            self._last_outcome = outcome
        return outcome

    def _set_cycle_label(self, label: str) -> None:
        with self._lock:
            self._cycle_label = label

    def _schedule_next_cycle_deadline(self) -> None:
        now = self._monotonic_time()
        deadline = None if now is None else now + self.interval_seconds
        if deadline is not None and not isfinite(deadline):
            deadline = None
        with self._lock:
            if self.stop_event.is_set():
                self._next_cycle_deadline_monotonic = None
                self._timing_version += 1
                return
            self._next_cycle_deadline_monotonic = deadline
            self._timing_version += 1

    def _monotonic_time(self) -> float | None:
        return self._safe_monotonic_time(self.monotonic_provider)

    def _telemetry_monotonic_time(self) -> float | None:
        return self._safe_monotonic_time(self.telemetry_monotonic_provider)

    @staticmethod
    def _safe_monotonic_time(provider: Callable[[], float]) -> float | None:
        try:
            raw_value = provider()
            if isinstance(raw_value, bool):
                return None
            value = float(raw_value)
        except Exception:
            return None
        return value if isfinite(value) else None

    @staticmethod
    def _elapsed_seconds(
        started_monotonic: float | None,
        completed_monotonic: float | None,
    ) -> float | None:
        if started_monotonic is None or completed_monotonic is None:
            return None
        elapsed = completed_monotonic - started_monotonic
        return elapsed if isfinite(elapsed) and elapsed >= 0 else None

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _completion_timestamp(self) -> str | None:
        try:
            completed_at = self.completion_time_provider()
            if not isinstance(completed_at, datetime):
                return None
            if completed_at.tzinfo is None or completed_at.utcoffset() is None:
                return None
            return completed_at.astimezone(timezone.utc).isoformat()
        except Exception:
            return None

    def _build_timing_snapshot(
        self,
        *,
        active: bool,
        cycle_in_progress: bool,
        seconds_until_next_cycle: float | None,
    ) -> SchedulerTimingSnapshot:
        return SchedulerTimingSnapshot(
            active=active,
            interval_seconds=self.interval_seconds,
            cycle_in_progress=cycle_in_progress,
            seconds_until_next_cycle=seconds_until_next_cycle,
            configured_idle_seconds=self.interval_seconds,
            last_cycle_duration_seconds=self._last_cycle_duration_seconds,
            last_cycle_start_interval_seconds=(
                self._last_cycle_start_interval_seconds
            ),
            cycle_duration_sample_count=len(self._cycle_duration_samples),
            cycle_duration_p95_seconds=self._p95(self._cycle_duration_samples),
            cycle_start_interval_sample_count=len(
                self._cycle_start_interval_samples
            ),
            cycle_start_interval_p95_seconds=self._p95(
                self._cycle_start_interval_samples
            ),
            current_action=self._current_action,
            last_cycle_completed_at=self._last_cycle_completed_at,
        )

    @staticmethod
    def _p95(samples: deque[float]) -> float | None:
        if not samples:
            return None
        ordered = sorted(samples)
        return ordered[max(0, ceil(len(ordered) * 0.95) - 1)]

    def _clear_next_cycle_deadline(self) -> None:
        with self._lock:
            if self._next_cycle_deadline_monotonic is not None:
                self._next_cycle_deadline_monotonic = None
                self._timing_version += 1

    def _set_stage(self, stage: str) -> None:
        with self._lock:
            self._current_stage = stage

    def _record_scheduler_failure(self, error: Exception) -> ServiceCycleOutcome:
        raw_code = type(error).__name__
        error_code = _ERROR_CODE_PATTERN.sub("_", raw_code)[:64] or "UnknownError"
        with self._lock:
            self._consecutive_failures += 1
            self._last_error_stage = self._current_stage or "unknown"
            self._last_error_code = error_code
            failure_count = self._consecutive_failures
            error_stage = self._last_error_stage
        outcome = self._record_outcome("scheduler_error", market_open=None)
        self._set_cycle_label(
            f"백그라운드 cycle 오류 ({error_stage}/{error_code}, 연속 {failure_count}회)"
            " - 다음 주기에 재시도"
        )
        return outcome

    def _clear_scheduler_failure(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._last_error_stage = ""
            self._last_error_code = ""
