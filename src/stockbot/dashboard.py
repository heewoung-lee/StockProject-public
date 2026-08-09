from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Callable, Mapping, Protocol

from .advisor import StrategyAdvisor
from .advisor_cli import _latest_bars
from .config import BotConfig, KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, load_config
from .kis import KisApiError
from .kis_market_data import KisTokenFileCache
from .kis_smoke import run_read_only_smoke
from .live_broker import LiveBroker
from .live_order_state import JsonManualReconciliationStore, JsonPendingLiveOrderStore
from .live_order_safety_context import LiveOrderSafetyContext
from .live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from .live_probe import run_live_read_only_probe
from .live_readiness_cli import dashboard_live_readiness_config_values, run_live_readiness_check
from .live_reconciliation import KisLiveOrderReconciler
from .live_safety import (
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
    LIVE_ALLOW_ENV_KEY,
    LIVE_CONFIRMATION_ENV_KEY,
    LIVE_CONFIRMATION_PHRASE,
    LIVE_ENABLED_ENV_KEY,
    LIVE_KIS_ENV_KEYS,
    live_credential_scope_fingerprint,
    live_order_gate_configured,
    read_env_file,
)
from .market_data import read_csv_bars
from .models import Position
from .position_view import PositionDetail, PositionRow, build_position_detail, build_position_rows
from .profiles import get_profile_settings
from .risk import RiskConfig
from .runtime import CustomStrategySettings, RuntimeEvent, _safe_error_detail
from .redaction import redact_sensitive_text
from .strategy import FlowScalperConfig
from .symbols import SymbolDirectory
from .trade_log import TradeLogEntry, build_trade_log_entry


PROFILE_LABELS = {
    "conservative": "보수형",
    "balanced": "균형형",
    "aggressive": "공격형",
    "custom": "커스텀",
}

CONFIDENCE_LABELS = {
    "low": "낮음",
    "medium": "중간",
    "high": "높음",
}

TRADING_MODE_LABELS = {
    "virtual": "가상",
    "real": "리얼",
}

RUNTIME_DATA_SOURCE_LABELS = {
    "local": "로컬 가상 테스트",
    "kis-vts": "KIS 장중 테스트",
    "external-scan-kis": "KIS 하이브리드 장중 테스트",
}

VIRTUAL_MODE_NOTICE = (
    "가상모드입니다. 현재 자동매매는 로컬 paper runtime으로 실행되며, KIS 모의투자 계좌는 계좌 조회로 확인합니다. "
    "KIS VTS 가상머니 주문 전송은 별도 주문 어댑터와 안전 검증 후 활성화합니다."
)

REAL_MODE_NOTICE = (
    "리얼모드는 실제 계좌를 사용합니다. 주문 전송은 기본 잠금 상태이며, 실전 주문 승인, 장중 검증, "
    "손실 제한, 감사 로그, 미체결 재동기화 게이트가 모두 통과할 때만 활성화합니다."
)


def _live_order_gate_blocker_present(blockers: list[str]) -> bool:
    joined = "\n".join(blockers)
    return any(
        token in joined
        for token in (
            "allow_live_trading=true",
            "live_trading_enabled=true",
            "STOCKBOT_ALLOW_LIVE_TRADING",
            "STOCKBOT_LIVE_TRADING_ENABLED",
            "STOCKBOT_LIVE_TRADING_CONFIRM",
            "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION",
            "live order gate is not configured",
            "live order approval env gate",
        )
    )

DEFAULT_CONFIG_PATH = "config.example.yaml"
DEFAULT_ENV_FILE = ".env"
MAX_DASHBOARD_LOG_ENTRIES = 50
KIS_ENV_KEYS = {
    "app_key": "KIS_VTS_APP_KEY",
    "app_secret": "KIS_VTS_APP_SECRET",
    "account_no": "KIS_VTS_ACCOUNT_NO",
    "product_code": "KIS_VTS_ACCOUNT_PRODUCT_CODE",
}
MASKED_CREDENTIAL_PLACEHOLDER = "**********"
SAFE_KIS_ERROR_CODE_PATTERN = re.compile(r"^[A-Z]{2,8}\d{3,6}$")
SENSITIVE_KIS_TERMS = (
    "secret",
    "appsecret",
    "appkey",
    "apikey",
    "bearer",
    "authorization",
    "token",
    "account",
    "cano",
    "acnt",
    "kisvtsapp",
)


class RuntimeBrokerLike(Protocol):
    def snapshot(self):
        ...


class RuntimeLike(Protocol):
    broker: RuntimeBrokerLike

    @property
    def latest_cycle_account_snapshot(self) -> object | None:
        ...

    def start(self) -> RuntimeEvent:
        ...

    def pause(self) -> RuntimeEvent:
        ...

    def run_cycle(self) -> list[RuntimeEvent]:
        ...

    def apply_strategy_settings(
        self,
        *,
        settings: CustomStrategySettings,
        strategy_config: FlowScalperConfig,
        risk_config: RiskConfig,
        profile_label: str,
    ) -> RuntimeEvent:
        ...


@dataclass(frozen=True)
class AccountPanel:
    status: str
    masked_account: str
    cash: str
    equity: str
    positions: str
    buying_power: str
    currency: str
    last_price: str
    updated_at: str
    runtime_metrics: tuple[tuple[str, str], ...] = ()


def _empty_account_panel(*, status: str, masked_account: str = "******") -> AccountPanel:
    zero = format_krw(Decimal("0"))
    return AccountPanel(
        status=status,
        masked_account=masked_account,
        cash=zero,
        equity=zero,
        positions="0개",
        buying_power=zero,
        currency="KRW",
        last_price=zero,
        updated_at=_now_label(),
        runtime_metrics=(),
    )


@dataclass(frozen=True)
class ProfileOption:
    key: str
    label: str
    description: str
    selected: bool


@dataclass(frozen=True)
class AdvisorPanel:
    selected_profile: str
    selected_profile_label: str
    confidence_label: str
    summary: str
    profile_options: tuple[ProfileOption, ...]
    metrics: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ReadOnlyNoticePanel:
    title: str
    description: str
    locked: bool
    order_enabled: bool


@dataclass(frozen=True)
class ActivityLogEntry:
    level: str
    title: str
    message: str
    timestamp: str


@dataclass(frozen=True)
class DashboardState:
    trading_mode: str
    mode_label: str
    account: AccountPanel
    advisor: AdvisorPanel
    custom_settings: tuple[tuple[str, str], ...]
    active_positions: tuple[PositionRow, ...]
    selected_position: PositionDetail
    trade_log: tuple[TradeLogEntry, ...]
    system_log: tuple[ActivityLogEntry, ...]
    read_only_notice: ReadOnlyNoticePanel
    runtime_status: str


@dataclass(frozen=True)
class DashboardServices:
    kis_check: Callable[[], dict[str, object]] | None = None
    kis_live_check: Callable[..., dict[str, object]] | None = None
    live_readiness_check: Callable[..., dict[str, object]] | None = None
    advisor: Callable[[], dict[str, object]] | None = None
    runtime: RuntimeLike | None = None
    runtime_builder: Callable[[str], RuntimeLike] | None = None
    live_runtime_builder: Callable[[], RuntimeLike] | None = None
    kis_market_status: Callable[[], object] | None = None
    symbol_names: Mapping[str, str] | None = None
    kis_rate_limiter: object | None = None
    paper_kis_rate_limiter: object | None = None
    profit_report: Callable[..., dict[str, object]] | None = None


@dataclass(frozen=True)
class RuntimeSettingsUpdate:
    settings: CustomStrategySettings
    strategy_config: FlowScalperConfig
    risk_config: RiskConfig
    profile_label: str


def _synchronized(method):
    def wrapper(self, *args, **kwargs):
        with self._mutation_lock:
            return method(self, *args, **kwargs)

    return wrapper


class DashboardController:
    def __init__(
        self,
        *,
        state: DashboardState | None = None,
        services: DashboardServices | None = None,
        config_path: str = DEFAULT_CONFIG_PATH,
        env_file: str = DEFAULT_ENV_FILE,
        symbol: str = "005930",
        max_bars: int = 120,
        live_order_safety_context: LiveOrderSafetyContext | None = None,
        live_token_cache: KisTokenFileCache | None = None,
    ):
        self.services = services or DashboardServices()
        self.symbol_directory = SymbolDirectory(dict(self.services.symbol_names or {}))
        self.state = state or build_initial_dashboard_state()
        self.config_path = _resolve_config_path(config_path)
        self.env_file = _resolve_env_file_path(env_file)
        self.symbol = symbol
        self.max_bars = max_bars
        self._runtime_running = False
        self._runtime_busy = False
        self._runtime_cycle_generation = 0
        self._runtime_busy_generation: int | None = None
        self._pending_runtime_settings: RuntimeSettingsUpdate | None = None
        self._latest_runtime_settings: CustomStrategySettings | None = None
        self._latest_strategy_config: FlowScalperConfig | None = None
        self._latest_risk_config: RiskConfig | None = None
        self._latest_runtime_profile_label: str | None = None
        self._strategy_revision = 0
        self._state_revision = 0
        self._runtime_action_lock = RLock()
        self._mutation_lock = RLock()
        self._kis_check_lock = RLock()
        self._live_readiness_lock = RLock()
        # Composite lock order: runtime action -> live readiness -> KIS check -> mutation.
        self._live_order_safety_context = live_order_safety_context or LiveOrderSafetyContext()
        self._live_read_only_verified_account_suffix: str | None = None
        self._live_read_only_verified_product_code: str | None = None
        self._live_read_only_verified_fingerprint: str | None = None
        self._live_read_only_probe_revision = 0
        self._live_runtime_readiness_ready = False
        self._live_token_cache = live_token_cache or KisTokenFileCache(namespace="kis-live")

    @property
    def state_revision(self) -> int:
        return self._state_revision

    def bump_state_revision(self) -> None:
        with self._mutation_lock:
            self._state_revision += 1

    def profit_report(
        self,
        *,
        granularity: str,
        scope: str,
        anchor: str,
    ) -> dict[str, object]:
        service = self.services.profit_report
        if not callable(service):
            raise ValueError("profit report service is unavailable")
        result = service(
            granularity=granularity,
            scope=scope,
            anchor=anchor,
        )
        if not isinstance(result, dict):
            raise ValueError("profit report service returned an invalid response")
        return result

    def run_kis_check(self) -> DashboardState:
        with self._kis_check_lock:
            return self._run_kis_check_locked()

    def run_kis_live_check(self, *, activate_real_mode: bool = True) -> DashboardState:
        with self._kis_check_lock:
            return self._run_kis_live_check_locked(activate_real_mode=activate_real_mode)

    def run_live_readiness_check(
        self,
        *,
        refresh_scanner_snapshot: bool = False,
    ) -> tuple[DashboardState, dict[str, object]]:
        with self._runtime_action_lock:
            with self._live_readiness_lock:
                with self._kis_check_lock:
                    with self._mutation_lock:
                        return self._run_live_readiness_check_locked(refresh_scanner_snapshot=refresh_scanner_snapshot)

    def _run_live_readiness_check_locked(
        self,
        *,
        refresh_scanner_snapshot: bool = False,
        check_live_runtime: bool = True,
    ) -> tuple[DashboardState, dict[str, object]]:
        try:
            result = (self.services.live_readiness_check or self._default_live_readiness_check)(
                config_path=self.config_path,
                env_file=self.env_file,
                refresh_scanner_snapshot=refresh_scanner_snapshot,
            )
        except Exception as exc:
            self._live_runtime_readiness_ready = False
            result = {
                "ready": False,
                "blockers": [redact_sensitive_text(str(exc))],
                "manual_reconciliation_cleared": False,
                "scanner_snapshot_refreshed": False,
                "live_order_enabled": False,
                "note": "Live readiness check failed before any order-capable action was attempted.",
            }
            state = self._with_system_log("error", "Live readiness", str(result["blockers"][0]))
            return state, result

        safe_result = _safe_live_readiness_result(result)
        if check_live_runtime:
            safe_result = self._with_live_runtime_readiness(safe_result)
        self._live_runtime_readiness_ready = safe_result.get("ready") is True
        blockers = [str(blocker) for blocker in safe_result.get("blockers", [])]
        if safe_result.get("ready") is True:
            state = self._with_system_log(
                "success",
                "Live readiness",
                "Live readiness check passed. This check did not enable or submit live orders.",
            )
            return state, safe_result

        blocker_preview = "; ".join(blockers[:3]) if blockers else "unknown blocker"
        message = f"Live readiness blocked: {blocker_preview}"
        if _live_order_gate_blocker_present(blockers):
            message = (
                f"{message}. 주문 게이트 차단이면 리얼모드에서 자동매매 시작을 눌러 "
                "현재 세션 계좌 확인과 안전 게이트를 다시 실행하세요."
            )
        state = self._with_system_log(
            "warning",
            "Live readiness",
            message,
        )
        return state, safe_result

    def clear_live_manual_reconciliation(
        self,
        *,
        confirmation_phrase: str,
    ) -> tuple[DashboardState, dict[str, object]]:
        with self._runtime_action_lock:
            with self._live_readiness_lock:
                with self._kis_check_lock:
                    with self._mutation_lock:
                        try:
                            result = (self.services.live_readiness_check or self._default_live_readiness_check)(
                                config_path=self.config_path,
                                env_file=self.env_file,
                                refresh_scanner_snapshot=False,
                                clear_manual_reconciliation=confirmation_phrase,
                            )
                        except Exception as exc:
                            self._live_runtime_readiness_ready = False
                            result = {
                                "ready": False,
                                "blockers": [redact_sensitive_text(str(exc))],
                                "manual_reconciliation_cleared": False,
                                "scanner_snapshot_refreshed": False,
                                "live_order_enabled": False,
                                "note": "Manual reconciliation clear failed before any order-capable action was attempted.",
                            }
                            state = self._with_system_log(
                                "error",
                                "Live manual reconciliation",
                                str(result["blockers"][0]),
                            )
                            return state, result

                safe_result = _safe_live_readiness_result(result)
                safe_result = self._with_live_runtime_readiness(safe_result)
                self._live_runtime_readiness_ready = False
                if safe_result.get("manual_reconciliation_cleared"):
                    state = self._with_system_log(
                        "warning",
                        "Live manual reconciliation",
                        "Manual reconciliation was cleared locally. Run live readiness again before enabling real orders.",
                    )
                    return state, safe_result

                blockers = [str(blocker) for blocker in safe_result.get("blockers", [])]
                blocker_preview = "; ".join(blockers[:3]) if blockers else "manual reconciliation was not cleared"
                state = self._with_system_log("warning", "Live manual reconciliation", blocker_preview)
                return state, safe_result

    def _clear_live_read_only_verification(self) -> None:
        self._live_read_only_verified_account_suffix = None
        self._live_read_only_verified_product_code = None
        self._live_read_only_verified_fingerprint = None
        self._live_runtime_readiness_ready = False

    def _live_read_only_scope_fingerprint(self, values: Mapping[str, str]) -> str:
        return live_credential_scope_fingerprint(values)

    def _mark_live_read_only_verified(self, values: Mapping[str, str]) -> None:
        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        product_code = str(values.get(LIVE_KIS_ENV_KEYS["product_code"]) or "").strip()
        self._live_read_only_verified_account_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        self._live_read_only_verified_product_code = product_code
        self._live_read_only_verified_fingerprint = self._live_read_only_scope_fingerprint(values)
        self._live_read_only_probe_revision += 1

    def _live_read_only_verification_matches(self, values: Mapping[str, str]) -> bool:
        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        product_code = str(values.get(LIVE_KIS_ENV_KEYS["product_code"]) or "").strip()
        expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        return (
            bool(expected_suffix)
            and bool(product_code)
            and self._live_read_only_verified_account_suffix == expected_suffix
            and self._live_read_only_verified_product_code == product_code
            and self._live_read_only_verified_fingerprint == self._live_read_only_scope_fingerprint(values)
        )

    def _validate_live_read_only_probe_result(self, result: Mapping[str, object], values: Mapping[str, str]) -> None:
        if result.get("read_only") is not True:
            raise ValueError("live account probe did not confirm account inquiry mode")
        if result.get("live_order_enabled") is not False:
            raise ValueError("live account probe unexpectedly reported live order enabled")
        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        product_code = str(values.get(LIVE_KIS_ENV_KEYS["product_code"]) or "").strip()
        expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        account_display = str(result.get("account") or "")
        if not expected_suffix or not product_code:
            raise ValueError("live account probe cannot verify missing account scope")
        if not account_display.endswith(f"{expected_suffix}-{product_code}"):
            raise ValueError("live account probe returned a different account scope")

    def _run_kis_check_locked(self) -> DashboardState:
        if self.state.trading_mode == "real":
            return self._with_system_log(
                "warning",
                "KIS paper check blocked",
                "Real mode uses the live KIS account check only.",
            )
        rate_limit = self._kis_rate_limit_decision()
        if rate_limit is not None and not rate_limit.allowed:
            return self.mark_kis_check_cooldown(remaining_seconds=rate_limit.retry_after_seconds)
        self._record_kis_limited_request()
        try:
            result = (self.services.kis_check or self._default_kis_check)()
        except Exception as exc:
            message = _safe_kis_error_message(exc)
            with self._mutation_lock:
                account = replace(self.state.account, status="연결 실패", updated_at=_now_label())
                notice = replace(self.state.read_only_notice, description=f"KIS 연결 실패: {message}")
                self.state = replace(self.state, account=account, read_only_notice=notice)
                return self._with_system_log("error", "KIS 모의투자 연결 확인", message)

        self._record_kis_token_issue()
        account = AccountPanel(
            status="연결됨",
            masked_account=mask_account_display(str(result.get("account", "******"))),
            cash=format_krw(_decimal(result.get("cash", "0"))),
            equity=format_krw(_decimal(result.get("equity", "0"))),
            positions=f"{int(result.get('balance_positions', 0))}개",
            buying_power=format_krw(_decimal(result.get("buying_power", result.get("cash", "0")))),
            currency="KRW",
            last_price=format_krw(_decimal(result.get("last_price", "0"))),
            updated_at=_now_label(),
            runtime_metrics=self.state.account.runtime_metrics,
        )
        with self._mutation_lock:
            notice = replace(
                self.state.read_only_notice,
                description="KIS 연결 완료: 계좌와 조회 종목 현재가를 갱신했습니다. 주문 전송은 비활성화되어 있습니다.",
            )
            self.state = replace(self.state, account=account, read_only_notice=notice)
            return self._with_system_log(
                "success",
                "KIS 모의투자 연결 확인",
                "계좌와 조회 종목 현재가를 갱신했습니다. 다음 단계로 AI 추천 또는 paper runtime을 실행할 수 있습니다.",
            )

    def _run_kis_live_check_locked(
        self,
        *,
        activate_real_mode: bool = True,
        require_open_market: bool = False,
    ) -> DashboardState:
        live_env_values = _read_env_values(Path(self.env_file))
        market_status_provider = self.services.kis_market_status
        try:
            market_status = market_status_provider() if callable(market_status_provider) else None
        except Exception:
            market_status = None
        if require_open_market and (
            market_status is None
            or not hasattr(market_status, "is_open")
            or not bool(getattr(market_status, "is_open", False))
        ):
            return self.state
        try:
            result = (self.services.kis_live_check or self._default_kis_live_check)(
                symbol=self.symbol,
                env_file=self.env_file,
                env={} if Path(self.env_file).exists() else None,
            )
            self._validate_live_read_only_probe_result(result, live_env_values)
        except Exception as exc:
            message = _safe_kis_error_message(exc, live=True)
            preserve_live_snapshot = (
                self.state.trading_mode == "real"
                and _is_kis_token_rate_limit_error(exc)
                and self._live_read_only_verification_matches(live_env_values)
            )
            if preserve_live_snapshot:
                self._live_runtime_readiness_ready = False
            else:
                self._clear_live_read_only_verification()
            with self._mutation_lock:
                if not activate_real_mode:
                    if self.state.trading_mode == "real":
                        account = self._live_probe_failure_account(
                            preserve_existing=preserve_live_snapshot,
                            status="실전 조회 재시도 대기" if preserve_live_snapshot else "실전 조회 실패",
                        )
                        notice = replace(
                            self.state.read_only_notice,
                            description=f"KIS 실전 계좌 조회 실패: {message}",
                            locked=True,
                            order_enabled=False,
                        )
                        self.state = self._replace_after_live_probe_failure(
                            account=account,
                            notice=notice,
                            preserve_existing=preserve_live_snapshot,
                        )
                    return self._with_system_log("error", "KIS 실전 조회 확인", message)
                account = self._live_probe_failure_account(
                    preserve_existing=preserve_live_snapshot,
                    status="실전 조회 재시도 대기" if preserve_live_snapshot else "실전 조회 실패",
                )
                notice = replace(
                    self.state.read_only_notice,
                    description=f"KIS 실전 계좌 조회 실패: {message}",
                    locked=True,
                    order_enabled=False,
                )
                self.state = self._replace_after_live_probe_failure(
                    account=account,
                    notice=notice,
                    preserve_existing=preserve_live_snapshot,
                )
                return self._with_system_log("error", "KIS 실전 조회 확인", message)

        account = AccountPanel(
            status="실전 조회 완료",
            masked_account=mask_account_display(str(result.get("account", "******"))),
            cash=format_krw(_decimal(result.get("cash", "0"))),
            equity=format_krw(_decimal(result.get("equity", "0"))),
            positions=f"{int(result.get('balance_positions', 0))}개",
            buying_power=format_krw(_decimal(result.get("buying_power", result.get("cash", "0")))),
            currency="KRW",
            last_price=format_krw(_decimal(result.get("last_price", "0"))),
            updated_at=_now_label(),
            runtime_metrics=(),
        )
        live_position_rows, live_selected_position = self._live_position_state_from_probe_result(result)
        market_closed_message = _live_market_closed_message(market_status)
        with self._mutation_lock:
            self._mark_live_read_only_verified(live_env_values)
            if not activate_real_mode:
                if self.state.trading_mode == "real":
                    notice = replace(
                        self.state.read_only_notice,
                        description="KIS 실전 계좌 조회가 완료되었습니다. 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
                        locked=True,
                        order_enabled=False,
                    )
                    self.state = replace(
                        self.state,
                        account=account,
                        read_only_notice=notice,
                        active_positions=live_position_rows,
                        selected_position=live_selected_position,
                    )
                if market_closed_message:
                    return self._with_system_log("warning", "장중 아님", market_closed_message)
                return self._with_system_log(
                    "success",
                    "KIS 실전 조회 확인",
                    f"실전 계좌 조회에 성공했습니다. 연결 계좌: {account.masked_account}. 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
                )
            notice = replace(
                self.state.read_only_notice,
                description="KIS 실전 계좌 조회가 완료되었습니다. 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
                locked=True,
                order_enabled=False,
            )
            self.state = replace(
                self.state,
                trading_mode="real",
                mode_label=TRADING_MODE_LABELS["real"],
                account=account,
                read_only_notice=notice,
                active_positions=live_position_rows,
                selected_position=live_selected_position,
            )
            if market_closed_message:
                return self._with_system_log("warning", "장중 아님", market_closed_message)
            return self._with_system_log(
                "success",
                "KIS 실전 조회 확인",
                "실전 계좌 잔고와 기준 종목 현재가를 확인했습니다. 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
            )

    @_synchronized
    def save_kis_credentials(self, *, app_key: str, app_secret: str, account_no: str, product_code: str) -> DashboardState:
        values = {
            KIS_ENV_KEYS["app_key"]: _validated_env_value(app_key, "app_key"),
            KIS_ENV_KEYS["app_secret"]: _validated_env_value(app_secret, "app_secret"),
            KIS_ENV_KEYS["account_no"]: _validated_env_value(account_no, "account_no"),
            KIS_ENV_KEYS["product_code"]: _validated_env_value(product_code, "product_code"),
        }
        _write_env_values(Path(self.env_file), values)
        account = replace(self.state.account, status="KIS 설정 저장됨", updated_at=_now_label())
        notice = replace(
            self.state.read_only_notice,
            description="KIS API 설정을 로컬 .env에 저장했습니다. KIS 연결 확인으로 계좌 조회를 검증하세요.",
        )
        self.state = replace(self.state, account=account, read_only_notice=notice)
        return self._with_system_log(
            "success",
            "KIS API 설정 저장",
            "KIS API 설정을 로컬 .env에 저장했습니다. 키와 계좌번호는 로그에 표시하지 않습니다.",
        )

    @_synchronized
    def save_kis_live_credentials(
        self,
        *,
        app_key: str,
        app_secret: str,
        account_no: str,
        product_code: str,
    ) -> DashboardState:
        if self.state.trading_mode == "real" and self._runtime_running:
            return self._with_system_log(
                "error",
                "KIS 실전 설정 저장 차단",
                "실전 자동매매 실행 중에는 API 키와 계좌 설정을 바꿀 수 없습니다. 먼저 일시정지한 뒤 다시 저장하세요.",
            )
        app_key_value = _validated_env_value(app_key, "live_app_key")
        app_secret_value = _validated_env_value(app_secret, "live_app_secret")
        account_no_value = _validated_env_value(account_no, "live_account_no")
        product_code_value = _validated_env_value(product_code, "live_product_code")
        if MASKED_CREDENTIAL_PLACEHOLDER in {
            app_key_value,
            app_secret_value,
            account_no_value,
            product_code_value,
        }:
            raise ValueError("masked live KIS credentials cannot be saved")
        values = {
            LIVE_KIS_ENV_KEYS["app_key"]: app_key_value,
            LIVE_KIS_ENV_KEYS["app_secret"]: app_secret_value,
            LIVE_KIS_ENV_KEYS["account_no"]: account_no_value,
            LIVE_KIS_ENV_KEYS["product_code"]: product_code_value,
        }
        _write_env_values(Path(self.env_file), values)
        _remove_env_values(
            Path(self.env_file),
            {
                LIVE_ALLOW_ENV_KEY,
                LIVE_ENABLED_ENV_KEY,
                LIVE_CONFIRMATION_ENV_KEY,
                LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
            },
        )
        self._live_order_safety_context.reset()
        self._clear_live_read_only_verification()
        account = _empty_account_panel(
            status="KIS 실전 조회 설정 저장됨",
            masked_account=mask_account_display(f"{account_no_value}-{product_code_value}"),
        )
        notice = replace(
            self.state.read_only_notice,
            description="KIS 실전 조회용 API 설정을 로컬 .env에 저장했습니다. 주문 전송은 계속 비활성화되어 있습니다.",
        )
        self.state = replace(self.state, account=account, read_only_notice=notice)
        return self._with_system_log(
            "success",
            "KIS 실전 조회 설정 저장",
            "KIS_LIVE 설정을 로컬 .env에 저장했습니다. 키와 계좌번호는 로그에 표시하지 않습니다.",
        )

    def kis_live_credential_status(self) -> dict[str, bool]:
        values = _read_env_values(Path(self.env_file))
        return {
            "appKeySaved": bool(values.get(LIVE_KIS_ENV_KEYS["app_key"], "").strip()),
            "appSecretSaved": bool(values.get(LIVE_KIS_ENV_KEYS["app_secret"], "").strip()),
            "accountNoSaved": bool(values.get(LIVE_KIS_ENV_KEYS["account_no"], "").strip()),
            "productCodeSaved": bool(values.get(LIVE_KIS_ENV_KEYS["product_code"], "").strip()),
        }

    @_synchronized
    def live_account_scope_verified(self) -> bool:
        return self._live_read_only_verification_matches(
            _read_env_values(Path(self.env_file))
        )

    @_synchronized
    def live_account_probe_revision(self) -> int:
        return self._live_read_only_probe_revision

    def run_persistent_live_account_probe(self) -> bool:
        with self._kis_check_lock:
            with self._mutation_lock:
                revision_before = self._live_read_only_probe_revision
                self._run_kis_live_check_locked(
                    activate_real_mode=True,
                    require_open_market=True,
                )
                return (
                    self._live_read_only_probe_revision > revision_before
                    and self._live_read_only_verification_matches(
                        _read_env_values(Path(self.env_file))
                    )
                )

    @_synchronized
    def save_live_order_approval(self, *, confirmation_phrase: str, account_confirmation: str) -> DashboardState:
        values = _read_env_values(Path(self.env_file))
        missing = [
            key
            for key in LIVE_KIS_ENV_KEYS.values()
            if not str(values.get(key) or "").strip()
        ]
        if missing:
            raise ValueError("live KIS credentials must be saved before live order approval")

        phrase = _validated_env_value(confirmation_phrase, "live_confirmation_phrase")
        if phrase != LIVE_CONFIRMATION_PHRASE:
            raise ValueError("live confirmation phrase does not match")

        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        suffix = _validated_env_value(account_confirmation, "live_account_confirmation")
        if not expected_suffix or suffix != expected_suffix:
            raise ValueError("live account confirmation does not match")
        if not self._live_read_only_verification_matches(values):
            raise ValueError("successful live account inquiry check is required before live order approval")

        self._write_live_order_gate_for_account(expected_suffix)
        self._live_order_safety_context.approve_session(
            allow_new_entries=not self.current_custom_settings().kill_switch
        )
        self._live_runtime_readiness_ready = False
        account = replace(self.state.account, status="실전 주문 승인 저장됨", updated_at=_now_label())
        notice = replace(
            self.state.read_only_notice,
            description="실전 주문 승인 값이 로컬 .env에 저장되었습니다. 주문 전송은 여전히 live preflight와 장중/위험 검사를 통과해야 합니다.",
        )
        self.state = replace(self.state, account=account, read_only_notice=notice)
        return self._with_system_log(
            "success",
            "실전 주문 승인 저장",
            "실전 주문 승인 값이 저장되었습니다. 계좌번호와 승인 문구는 화면과 로그에 표시하지 않습니다.",
        )

    def _write_live_order_gate_for_account(self, expected_suffix: str) -> None:
        _write_env_values(
            Path(self.env_file),
            {
                LIVE_ALLOW_ENV_KEY: "true",
                LIVE_ENABLED_ENV_KEY: "true",
                LIVE_CONFIRMATION_ENV_KEY: LIVE_CONFIRMATION_PHRASE,
                LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: expected_suffix,
            },
        )

    def _live_order_gate_env_snapshot(self) -> dict[str, str | None]:
        values = _read_env_values(Path(self.env_file))
        return {
            LIVE_ALLOW_ENV_KEY: values.get(LIVE_ALLOW_ENV_KEY),
            LIVE_ENABLED_ENV_KEY: values.get(LIVE_ENABLED_ENV_KEY),
            LIVE_CONFIRMATION_ENV_KEY: values.get(LIVE_CONFIRMATION_ENV_KEY),
            LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: values.get(LIVE_ACCOUNT_CONFIRMATION_ENV_KEY),
        }

    def _restore_live_order_gate_env_snapshot(self, snapshot: Mapping[str, str | None]) -> None:
        values_to_restore = {
            key: value
            for key, value in snapshot.items()
            if value is not None
        }
        keys_to_remove = {
            key
            for key, value in snapshot.items()
            if value is None
        }
        if values_to_restore:
            _write_env_values(Path(self.env_file), values_to_restore)
        if keys_to_remove:
            _remove_env_values(Path(self.env_file), keys_to_remove)

    def _approve_live_order_session_for_real_start_locked(self) -> bool:
        values = _read_env_values(Path(self.env_file))
        missing = [
            key
            for key in LIVE_KIS_ENV_KEYS.values()
            if not str(values.get(key) or "").strip()
        ]
        if missing:
            self._runtime_running = False
            self.state = replace(self.state, runtime_status="실전 잠금")
            self._with_system_log(
                "warning",
                "Live readiness",
                "live KIS credentials must be saved before real auto-trading start",
            )
            return False

        if not self._live_read_only_verification_matches(values):
            self._run_kis_live_check_locked(activate_real_mode=True)
            values = _read_env_values(Path(self.env_file))
            if not self._live_read_only_verification_matches(values):
                self._runtime_running = False
                self.state = replace(self.state, runtime_status="실전 잠금")
                return False

        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        if not expected_suffix:
            self._runtime_running = False
            self.state = replace(self.state, runtime_status="실전 잠금")
            self._with_system_log(
                "warning",
                "Live readiness",
                "live account suffix could not be verified before real auto-trading start",
            )
            return False

        self._write_live_order_gate_for_account(expected_suffix)
        self._live_order_safety_context.approve_session(
            allow_new_entries=not self.current_custom_settings().kill_switch
        )
        self._live_runtime_readiness_ready = False
        return True

    def _rollback_failed_real_start_locked(
        self,
        *,
        previous_runtime: RuntimeLike | None,
        live_order_gate_snapshot: Mapping[str, str | None] | None,
    ) -> None:
        self._runtime_running = False
        self._live_order_safety_context.reset()
        self._clear_live_read_only_verification()
        if live_order_gate_snapshot is not None:
            self._restore_live_order_gate_env_snapshot(live_order_gate_snapshot)
        self.services = replace(self.services, runtime=previous_runtime)

    def live_order_approval_status(self) -> dict[str, bool]:
        values = _read_env_values(Path(self.env_file))
        account_no = str(values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
        confirmation = str(values.get(LIVE_CONFIRMATION_ENV_KEY) or "").strip()
        account_confirmation = str(values.get(LIVE_ACCOUNT_CONFIRMATION_ENV_KEY) or "").strip()
        session = self._live_order_safety_context
        return {
            "allowSaved": str(values.get(LIVE_ALLOW_ENV_KEY) or "").strip().lower() == "true",
            "enabledSaved": str(values.get(LIVE_ENABLED_ENV_KEY) or "").strip().lower() == "true",
            "confirmationSaved": confirmation == LIVE_CONFIRMATION_PHRASE,
            "accountConfirmationSaved": bool(expected_suffix) and account_confirmation == expected_suffix,
            "sessionApproved": bool(session.session_approved),
            "riskLimitsOk": bool(session.risk_limits_ok),
            "newEntriesAllowed": bool(session.new_entries_allowed),
        }

    @_synchronized
    def mark_kis_check_pending(self) -> DashboardState:
        account = replace(self.state.account, status="연결 확인 중", updated_at=_now_label())
        notice = replace(self.state.read_only_notice, description="KIS 연결 확인 중입니다. 완료될 때까지 기다려 주세요.")
        self.state = replace(self.state, account=account, read_only_notice=notice)
        return self._with_system_log("info", "KIS 모의투자 연결 확인", "KIS 계좌 조회를 실행하고 있습니다.")

    @_synchronized
    def mark_kis_check_cooldown(self, *, remaining_seconds: float) -> DashboardState:
        seconds = max(1, math.ceil(remaining_seconds))
        account = replace(self.state.account, status="재시도 대기", updated_at=_now_label())
        notice = replace(
            self.state.read_only_notice,
            description=f"KIS 접근토큰 제한 때문에 {seconds}초 후 다시 시도할 수 있습니다.",
        )
        self.state = replace(self.state, account=account, read_only_notice=notice)
        return self._with_system_log(
            "warning",
            "KIS 모의투자 연결 확인",
            f"KIS 접근토큰 제한 대기 중입니다. {seconds}초 후 다시 시도하세요.",
        )

    def run_ai_advisor(self) -> DashboardState:
        with self._mutation_lock:
            request_strategy_revision = self._strategy_revision
        try:
            result = (self.services.advisor or self._default_advisor)()
        except Exception:
            with self._mutation_lock:
                return self._with_system_log(
                    "error",
                    "AI 추천 실행",
                    "오류가 발생했습니다. 설정 파일과 시장 데이터 파일을 확인하세요.",
                )

        profile = str(result.get("recommended_profile", "balanced"))
        confidence = str(result.get("confidence", "medium"))
        reasons = [str(reason) for reason in result.get("reasons", [])]
        advisor = AdvisorPanel(
            selected_profile=profile,
            selected_profile_label=PROFILE_LABELS.get(profile, profile),
            confidence_label=CONFIDENCE_LABELS.get(confidence, confidence),
            summary=_korean_reason_summary(reasons),
            profile_options=_profile_options(profile),
            metrics=tuple((key, str(value)) for key, value in dict(result.get("metrics", {})).items()),
        )
        with self._mutation_lock:
            if self._strategy_revision != request_strategy_revision:
                return self._with_system_log(
                    "warning",
                    "AI 추천 실행",
                    "AI 추천 결과를 받았지만 최신 사용자 선택을 유지했습니다.",
                )
            self.state = replace(self.state, advisor=advisor)
            deferred = self._apply_profile_to_runtime(profile)
            self._strategy_revision += 1
            message = (
                f"{advisor.selected_profile_label} 전략을 추천하고 다음 cycle부터 적용합니다."
                if deferred
                else f"{advisor.selected_profile_label} 전략을 추천하고 적용했습니다."
            )
            return self._with_system_log("success", "AI 추천 실행", message)

    @_synchronized
    def select_trading_mode(self, mode: str) -> DashboardState:
        normalized = mode.strip().lower()
        if normalized not in TRADING_MODE_LABELS:
            return self._with_system_log("error", "거래 모드 변경", "알 수 없는 거래 모드입니다. 가상모드 또는 리얼모드를 선택하세요.")

        stop_event: RuntimeEvent | None = None
        current_runtime = self.services.runtime
        switching_from_live_runtime = normalized == "virtual" and self._is_live_runtime(current_runtime)
        should_stop_running_runtime = self._runtime_running and (
            normalized == "real" or switching_from_live_runtime
        )
        if should_stop_running_runtime:
            runtime = current_runtime
            pause = getattr(runtime, "pause", None)
            if self._runtime_busy:
                self._runtime_cycle_generation += 1
                stop_event = RuntimeEvent.system("리얼모드 전환으로 paper runtime 중단을 예약했습니다.")
            elif callable(pause):
                try:
                    stop_event = pause()
                except Exception:
                    stop_event = RuntimeEvent.system("리얼모드 전환 중 paper runtime 일시정지에 실패했습니다.")
            self._runtime_running = False
            self._pending_runtime_settings = None

        notice = replace(
            self.state.read_only_notice,
            description=_trading_mode_notice(normalized),
            locked=True,
            order_enabled=False,
        )
        self._live_order_safety_context.reset()
        self._clear_live_read_only_verification()
        self.state = replace(
            self.state,
            trading_mode=normalized,
            mode_label=TRADING_MODE_LABELS[normalized],
            read_only_notice=notice,
            runtime_status="실전 잠금" if normalized == "real" else self.state.runtime_status,
        )
        if normalized == "real":
            self.state = replace(
                self.state,
                active_positions=(),
                selected_position=PositionDetail.empty(),
            )
        else:
            runtime = self.services.runtime
            if switching_from_live_runtime:
                builder = self.services.runtime_builder
                next_runtime: RuntimeLike | None = None
                if callable(builder):
                    try:
                        next_runtime = builder("local")
                    except Exception as exc:
                        detail = _safe_runtime_builder_error(exc)
                        self.services = replace(self.services, runtime=None)
                        if stop_event is not None:
                            self._apply_runtime_events([stop_event])
                        return self._with_system_log(
                            "error",
                            "Paper runtime",
                            f"paper runtime builder failed: {detail}",
                        )
                self.services = replace(self.services, runtime=next_runtime)
                self._runtime_running = False
                self._runtime_busy = False
                self._runtime_busy_generation = None
                self._runtime_cycle_generation += 1
                self._pending_runtime_settings = None
                runtime = next_runtime
            if runtime is not None:
                self._refresh_runtime_status_from_runtime(runtime, fallback=self.state.runtime_status)
                self._refresh_virtual_account_from_runtime(runtime)
                self._refresh_active_positions_from_runtime(runtime)
            else:
                self.state = replace(
                    self.state,
                    account=AccountPanel(
                        status=self.state.runtime_status or "정지",
                        masked_account="가상계좌",
                        cash=format_krw(Decimal("0")),
                        equity=format_krw(Decimal("0")),
                        positions="0개",
                        buying_power=format_krw(Decimal("0")),
                        currency="KRW",
                        last_price=format_krw(Decimal("0")),
                        updated_at=_now_label(),
                        runtime_metrics=(),
                    ),
                    active_positions=(),
                    selected_position=PositionDetail.empty(),
                )
        if stop_event is not None:
            self._apply_runtime_events([stop_event])
        return self._with_system_log(
            "info",
            "거래 모드 변경",
            f"{TRADING_MODE_LABELS[normalized]}모드로 전환했습니다.",
        )

    @_synchronized
    def select_runtime_data_source(self, source: str) -> DashboardState:
        normalized = source.strip().lower()
        if normalized not in RUNTIME_DATA_SOURCE_LABELS:
            return self._with_system_log("error", "테스트 데이터 전환", "알 수 없는 테스트 데이터 출처입니다.")
        if self.state.trading_mode == "real":
            return self._with_system_log(
                "warning",
                "테스트 데이터 전환 잠금",
                "리얼모드에서는 가상 테스트 데이터 출처를 변경하지 않습니다.",
            )
        if self._runtime_busy:
            return self._with_system_log(
                "warning",
                "테스트 데이터 전환 대기",
                "현재 cycle이 진행 중입니다. cycle이 끝난 뒤 다시 전환하세요.",
            )

        current_runtime = self.services.runtime
        current_source = str(getattr(current_runtime, "data_source_kind", "") or "").strip().lower()
        if normalized == current_source:
            return self._with_system_log(
                "info",
                "테스트 데이터 전환",
                f"{RUNTIME_DATA_SOURCE_LABELS[normalized]}가 이미 선택되어 있습니다.",
            )

        if normalized in {"kis-vts", "external-scan-kis"}:
            status_provider = self.services.kis_market_status
            market_status = status_provider() if callable(status_provider) else None
            if market_status is not None and not bool(getattr(market_status, "is_open", False)):
                message = str(
                    getattr(
                        market_status,
                        "message",
                        "장 대기 - 정규장 시간이 아닙니다. paper 자동매매는 정규장(09:00-15:30 KST)에만 실행합니다.",
                    )
                )
                return self._with_system_log("warning", "장중 테스트 전환 차단", message)

        builder = self.services.runtime_builder
        if not callable(builder):
            return self._with_system_log(
                "error",
                "테스트 데이터 전환",
                "테스트 데이터 출처를 전환할 runtime builder가 설정되지 않았습니다.",
            )

        stop_event: RuntimeEvent | None = None
        if self._runtime_running and current_runtime is not None:
            pause = getattr(current_runtime, "pause", None)
            if callable(pause):
                try:
                    stop_event = pause()
                except Exception:
                    stop_event = RuntimeEvent.system("데이터 출처 전환 중 기존 paper runtime 일시정지에 실패했습니다.")

        try:
            next_runtime = builder(normalized)
        except Exception as exc:
            detail = _safe_runtime_builder_error(exc)
            return self._with_system_log(
                "error",
                "테스트 데이터 전환",
                f"새 테스트 데이터 runtime을 준비하는 중 오류가 발생했습니다: {detail}",
            )

        self.services = replace(self.services, runtime=next_runtime)
        self._runtime_running = False
        self._runtime_busy = False
        self._pending_runtime_settings = None
        if stop_event is not None:
            self._apply_runtime_events([stop_event])
        self._apply_latest_runtime_settings_to_current_runtime()
        self._refresh_runtime_status_from_runtime(next_runtime, fallback="정지")
        self._refresh_virtual_account_from_runtime(next_runtime)
        self._refresh_active_positions_from_runtime(next_runtime)
        self.state = replace(self.state, selected_position=PositionDetail.empty())
        return self._with_system_log(
            "success",
            "테스트 데이터 전환",
            f"{RUNTIME_DATA_SOURCE_LABELS[normalized]}로 전환했습니다. 자동매매를 다시 시작하면 새 데이터 출처로 실행됩니다.",
        )

    @_synchronized
    def select_strategy_profile(self, profile: str) -> DashboardState:
        normalized = profile.strip().lower()
        try:
            settings = self._settings_for_profile(normalized)
            settings = settings.with_updates(kill_switch=self.current_custom_settings().kill_switch)
            strategy_config = self._strategy_config_for_profile(normalized, settings)
            risk_config = self._risk_config_for_profile(normalized, settings)
        except Exception:
            return self._with_system_log("error", "전략 프로필 적용", "알 수 없는 전략 프로필이거나 설정 범위를 벗어났습니다.")

        label = PROFILE_LABELS.get(normalized, normalized)
        advisor = AdvisorPanel(
            selected_profile=normalized,
            selected_profile_label=label,
            confidence_label="직접 선택",
            summary=f"{label} 설정을 paper 자동매매에 적용했습니다.",
            profile_options=_profile_options(normalized),
            metrics=(),
        )
        self.state = replace(self.state, advisor=advisor, custom_settings=_custom_settings_entries(settings))
        deferred = self._apply_runtime_settings(settings, strategy_config, risk_config, label)
        self._strategy_revision += 1
        message = f"{label} 설정을 다음 cycle부터 적용합니다." if deferred else f"{label} 설정을 적용했습니다."
        return self._with_system_log("success", "전략 프로필 적용", message)

    @_synchronized
    def apply_custom_settings(self, settings: CustomStrategySettings) -> DashboardState:
        try:
            strategy_config = self._strategy_config_for_custom(settings)
            risk_config = self._risk_config_for_custom(settings)
        except Exception:
            return self._with_system_log("error", "커스텀 설정 적용", "커스텀 설정 값이 허용 범위를 벗어났습니다.")

        advisor = AdvisorPanel(
            selected_profile="custom",
            selected_profile_label="커스텀",
            confidence_label="직접 설정",
            summary="사용자 커스텀 설정을 paper 자동매매에 적용했습니다.",
            profile_options=_profile_options("custom"),
            metrics=(),
        )
        self.state = replace(self.state, advisor=advisor, custom_settings=_custom_settings_entries(settings))
        deferred = self._apply_runtime_settings(settings, strategy_config, risk_config, "커스텀")
        self._strategy_revision += 1
        message = "커스텀 전략 설정을 다음 cycle부터 적용합니다." if deferred else "커스텀 전략 설정을 적용했습니다."
        return self._with_system_log("success", "커스텀 설정 적용", message)

    @_synchronized
    def set_cleanup_mode(self, enabled: bool) -> DashboardState:
        if self.state.trading_mode == "real" and not enabled:
            raise PermissionError("real mode cleanup can only be enabled from the dashboard")
        settings = self.current_custom_settings().with_updates(kill_switch=bool(enabled))
        self._live_order_safety_context.set_cleanup_mode(bool(enabled))
        try:
            strategy_config = self._strategy_config_for_custom(settings)
            risk_config = self._risk_config_for_custom(settings)
        except Exception:
            return self._with_system_log("error", "정리 모드", "정리 모드 설정을 적용하지 못했습니다.")

        deferred = self._apply_runtime_settings(
            settings,
            strategy_config,
            risk_config,
            self.state.advisor.selected_profile_label,
        )
        mode_label = "켜짐" if enabled else "꺼짐"
        timing = "다음 cycle부터 적용됩니다." if deferred else "즉시 적용했습니다."
        return self._with_system_log("success", "정리 모드", f"정리 모드 {mode_label} - {timing}")

    @_synchronized
    def current_custom_settings(self) -> CustomStrategySettings:
        if self._pending_runtime_settings is not None:
            return self._pending_runtime_settings.settings
        if self._latest_runtime_settings is not None:
            if self._latest_strategy_config is not None and self._latest_risk_config is not None:
                settings, _, _ = self._cap_settings_for_current_runtime(
                    self._latest_runtime_settings,
                    self._latest_strategy_config,
                    self._latest_risk_config,
                )
                return settings
            return self._latest_runtime_settings
        runtime = self.services.runtime
        settings = getattr(runtime, "settings", None)
        if isinstance(settings, CustomStrategySettings):
            return settings
        return CustomStrategySettings.default()

    def start_paper_runtime(self) -> DashboardState:
        with self._runtime_action_lock:
            with self._live_readiness_lock:
                with self._kis_check_lock:
                    with self._mutation_lock:
                        return self._start_paper_runtime_locked()

    def _start_paper_runtime_locked(self) -> DashboardState:
        if self.state.trading_mode == "real" and self.services.live_runtime_builder is None:
            self._runtime_running = False
            self.state = replace(self.state, runtime_status="실전 잠금")
            return self._with_system_log(
                "warning",
                "리얼모드 주문 잠금",
                "실전 계좌 자동주문은 live runtime builder가 없어 잠금 상태입니다. Electron 실전 경로와 안전 게이트를 통해 시작하세요.",
            )

        runtime = self.services.runtime
        previous_runtime = runtime
        live_order_gate_snapshot: dict[str, str | None] | None = None
        if self.state.trading_mode == "real":
            if (
                self._live_order_safety_context.session_approved
                and self._live_read_only_verified_fingerprint is not None
                and not self._live_read_only_verification_matches(_read_env_values(Path(self.env_file)))
            ):
                live_order_gate_snapshot = self._live_order_gate_env_snapshot()
                self._live_order_safety_context.reset()
                self._clear_live_read_only_verification()
            if not self._live_order_safety_context.session_approved:
                if live_order_gate_snapshot is None:
                    live_order_gate_snapshot = self._live_order_gate_env_snapshot()
                if not self._approve_live_order_session_for_real_start_locked():
                    self._runtime_running = False
                    self.state = replace(self.state, runtime_status="실전 잠금")
                    return self.state
            with self._live_readiness_lock:
                _, readiness = self._run_live_readiness_check_locked(
                    refresh_scanner_snapshot=True,
                    check_live_runtime=False,
                )
                runtime_pending_sync_required = _readiness_blocked_only_by_runtime_syncable_pending_orders(readiness)
                if readiness.get("ready") is not True and not runtime_pending_sync_required:
                    self._rollback_failed_real_start_locked(
                        previous_runtime=previous_runtime,
                        live_order_gate_snapshot=live_order_gate_snapshot,
                    )
                    self.state = replace(self.state, runtime_status="실전 잠금")
                    return self.state
                if runtime_pending_sync_required:
                    self._live_runtime_readiness_ready = True
                    self._with_system_log(
                        "info",
                        "Live readiness",
                        "Pending live orders will be reconciled by the live runtime admission gate.",
                    )
            runtime = self._build_live_runtime_for_real_mode()
            if runtime is None:
                self._rollback_failed_real_start_locked(
                    previous_runtime=previous_runtime,
                    live_order_gate_snapshot=live_order_gate_snapshot,
                )
                self.state = replace(self.state, runtime_status="실전 잠금")
                return self.state
        if runtime is None:
            return self._with_system_log("warning", "Paper runtime", "runtime service가 설정되지 않았습니다.")

        try:
            event = runtime.start()
        except Exception:
            if self.state.trading_mode == "real" or self._is_live_runtime(runtime):
                self._rollback_failed_real_start_locked(
                    previous_runtime=previous_runtime,
                    live_order_gate_snapshot=live_order_gate_snapshot,
                )
            else:
                self._runtime_running = False
            self.state = replace(self.state, runtime_status="시작 실패")
            return self._with_system_log("error", "Paper runtime", "runtime 시작 중 오류가 발생했습니다.")
        self._runtime_running = True
        self._refresh_runtime_status_from_runtime(runtime, fallback="실행 중")
        self._refresh_runtime_account_display(runtime, context="start", include_positions=True)
        return self._apply_runtime_events([event])

    @_synchronized
    def current_market_session_status(self):
        runtime = self.services.runtime
        market_hours = getattr(runtime, "market_hours", None)
        status = getattr(market_hours, "status", None)
        if not callable(status):
            return None
        return status()

    @_synchronized
    def mark_runtime_start_blocked_by_market_hours(self, status) -> DashboardState:
        self._runtime_running = False
        label = getattr(status, "label", "장 대기")
        message = getattr(status, "message", "국내 정규장 시간이 아닙니다.")
        self.state = replace(self.state, runtime_status=str(label))
        return self._with_system_log("warning", "장중 아님", str(message))

    @_synchronized
    def pause_paper_runtime(self) -> DashboardState:
        runtime = self.services.runtime
        if runtime is None:
            return self._with_system_log("warning", "Paper runtime", "runtime service가 설정되지 않았습니다.")

        try:
            event = runtime.pause()
        except Exception:
            if self.state.trading_mode == "real" or self._is_live_runtime(runtime):
                self._runtime_running = False
                self._runtime_busy = False
                self._pending_runtime_settings = None
                self._live_order_safety_context.reset()
                self._clear_live_read_only_verification()
                self.state = replace(self.state, runtime_status="pause failed")
            return self._with_system_log("error", "Paper runtime", "runtime 일시정지 중 오류가 발생했습니다.")
        self._runtime_running = False
        self._live_order_safety_context.reset()
        self._clear_live_read_only_verification()
        self.state = replace(self.state, runtime_status="일시정지")
        return self._apply_runtime_events([event])

    def run_paper_cycle(self) -> DashboardState:
        if not self._runtime_action_lock.acquire(blocking=False):
            with self._mutation_lock:
                return self._with_system_log(
                    "warning",
                    "Paper runtime",
                    "이전 runtime cycle이 아직 실행 중입니다. 이번 cycle은 건너뜁니다.",
                )
        try:
            return self._run_paper_cycle_locked()
        finally:
            self._runtime_action_lock.release()

    def _run_paper_cycle_locked(self) -> DashboardState:
        with self._mutation_lock:
            runtime = self.services.runtime
            if runtime is None:
                return self._with_system_log(
                    "warning",
                    "Paper runtime",
                    "runtime service가 설정되지 않았습니다. paper runtime을 구성한 뒤 다시 실행하세요.",
                )
            if self.state.trading_mode == "real" and not self._is_live_runtime(runtime):
                self._runtime_running = False
                self.state = replace(self.state, runtime_status="실전 잠금")
                return self._with_system_log(
                    "warning",
                    "리얼모드 주문 잠금",
                    "리얼모드에서는 paper runtime cycle을 실행하지 않습니다.",
                )
            if (
                self.state.trading_mode == "real"
                and self._runtime_running
                and self._live_order_safety_context.approved_at is not None
                and not self._live_order_safety_context.session_approved
            ):
                pause = getattr(runtime, "pause", None)
                pause_failed = False
                if callable(pause):
                    try:
                        pause()
                    except Exception:
                        pause_failed = True
                self._runtime_running = False
                self._runtime_busy = False
                self._pending_runtime_settings = None
                self._live_order_safety_context.reset()
                self._clear_live_read_only_verification()
                self.state = replace(self.state, runtime_status="실전 세션 만료")
                pause_diagnostic = " runtime_pause_failed=true" if pause_failed else ""
                return self._with_system_log(
                    "warning",
                    "실전 세션 만료",
                    "실전 주문 승인 세션이 만료되어 자동매매를 일시정지했습니다. 자동매매 시작을 다시 눌러 당일 계좌와 안전 조건을 확인하세요."
                    f"{pause_diagnostic}",
                )
            if not self._runtime_running:
                return self._with_system_log(
                    "warning",
                    "Paper runtime",
                    "runtime이 실행 중이 아닙니다. 먼저 start_paper_runtime을 호출하세요.",
                )
            if self._runtime_busy:
                return self._with_system_log(
                    "warning",
                    "Paper runtime",
                    "이전 runtime cycle이 아직 실행 중입니다. 이번 cycle은 건너뜁니다.",
                )
            self._runtime_cycle_generation += 1
            cycle_generation = self._runtime_cycle_generation
            cycle_mode = self.state.trading_mode
            self._runtime_busy = True
            self._runtime_busy_generation = cycle_generation
            try:
                self._apply_pending_runtime_settings()
            except Exception:
                self._runtime_busy = False
                self._runtime_busy_generation = None
                return self._with_system_log("error", "Paper runtime", "전략 설정 적용 중 오류가 발생했습니다.")
            cleanup_mode_active = self.current_custom_settings().kill_switch

        try:
            events = runtime.run_cycle()
        except Exception as exc:
            with self._mutation_lock:
                if self._runtime_cycle_generation != cycle_generation or self.services.runtime is not runtime:
                    if self._runtime_busy_generation == cycle_generation:
                        self._runtime_busy = False
                        self._runtime_busy_generation = None
                    return self.state
                self._runtime_busy = False
                self._runtime_busy_generation = None
                return self._with_system_log("error", "Paper runtime", _runtime_cycle_exception_message("run_cycle", exc))

        with self._mutation_lock:
            if self._runtime_busy_generation == cycle_generation:
                self._runtime_busy = False
                self._runtime_busy_generation = None
            if (
                self._runtime_cycle_generation != cycle_generation
                or self.services.runtime is not runtime
                or self.state.trading_mode != cycle_mode
            ):
                return self.state
            if self.state.trading_mode == "real" and not self._is_live_runtime(runtime):
                self._runtime_running = False
                self.state = replace(self.state, runtime_status="실전 잠금")
                return self._with_system_log(
                    "warning",
                    "리얼모드 주문 잠금",
                    "리얼모드 전환으로 paper runtime cycle 결과를 적용하지 않았습니다.",
                )
            self._apply_runtime_events(events)
            self._refresh_runtime_status_from_runtime(runtime)
            self._refresh_runtime_account_display(runtime, context="cycle", include_positions=True)
            if (
                cleanup_mode_active
                and self._runtime_positions_empty_for_cleanup(runtime)
            ):
                self._stop_runtime_after_cleanup_complete(runtime)
            return self.state

    @_synchronized
    def mark_background_action_failed(self, action_kind: str) -> DashboardState:
        if action_kind in {"start_runtime", "runtime_cycle"}:
            self._runtime_running = False
            self._runtime_busy = False
            self._pending_runtime_settings = None
            self.state = replace(self.state, runtime_status="오류")
        return self._with_system_log(
            "error",
            "앱 작업 오류",
            "백그라운드 작업 중 오류가 발생했습니다. 자동매매를 일시정지했습니다.",
        )

    @_synchronized
    def select_position(self, symbol: str) -> DashboardState:
        positions = self._runtime_positions()
        detail = self._build_position_detail(symbol, positions)
        self.state = replace(self.state, selected_position=detail)
        return self.state

    def _default_kis_check(self) -> dict[str, object]:
        env_override = {} if Path(self.env_file).exists() else None
        return run_read_only_smoke(symbol=self.symbol, env_file=self.env_file, env=env_override)

    def _default_kis_live_check(self, *, symbol: str, env_file: str, env=None) -> dict[str, object]:
        return run_live_read_only_probe(
            symbol=symbol,
            env_file=env_file,
            env=env,
            token_cache=self._live_token_cache,
            rate_limiter=self.services.kis_rate_limiter,
        )

    def _default_live_readiness_check(self, **kwargs) -> dict[str, object]:
        if "config_values" not in kwargs:
            kwargs["config_values"] = dashboard_live_readiness_config_values(
                load_config(kwargs.get("config_path", self.config_path))
            )
        return run_live_readiness_check(**kwargs)

    def _with_live_runtime_readiness(self, result: Mapping[str, object]) -> dict[str, object]:
        safe_result = dict(result)
        if safe_result.get("ready") is not True:
            return safe_result

        runtime_blockers = self._live_runtime_readiness_blockers()
        if not runtime_blockers:
            return safe_result

        blockers = [str(blocker) for blocker in safe_result.get("blockers", [])]
        blockers.extend(runtime_blockers)
        safe_result["ready"] = False
        safe_result["blockers"] = [redact_sensitive_text(blocker) for blocker in blockers]
        safe_result["live_order_enabled"] = False
        return safe_result

    def _live_runtime_readiness_blockers(self) -> list[str]:
        builder = self.services.live_runtime_builder
        if not callable(builder):
            return ["live runtime builder is not configured"]

        try:
            runtime = builder()
        except Exception as exc:
            detail = _safe_error_detail(exc) or exc.__class__.__name__
            return [f"live runtime cannot be constructed: {detail}"]

        return self._live_runtime_order_gate_blockers(runtime, require_readiness=False)

    def _default_advisor(self) -> dict[str, object]:
        config = self._load_config_with_local_paths()
        bars = _latest_bars(list(read_csv_bars(config.data_path)), self.max_bars)
        return StrategyAdvisor().recommend(config, bars).to_dict()

    def _load_config_with_local_paths(self) -> BotConfig:
        config = load_config(self.config_path)
        config_base = Path(self.config_path).resolve().parent
        if not Path(config.data_path).is_absolute():
            config = replace(config, data_path=str(config_base / config.data_path))
        if not Path(config.journal_path).is_absolute():
            config = replace(config, journal_path=str(config_base / config.journal_path))
        return config

    def _apply_profile_to_runtime(self, profile: str) -> bool:
        settings = self._settings_for_profile(profile)
        settings = settings.with_updates(kill_switch=self.current_custom_settings().kill_switch)
        self.state = replace(self.state, custom_settings=_custom_settings_entries(settings))
        return self._apply_runtime_settings(
            settings,
            self._strategy_config_for_profile(profile, settings),
            self._risk_config_for_profile(profile, settings),
            PROFILE_LABELS.get(profile, profile),
        )

    def _apply_runtime_settings(
        self,
        settings: CustomStrategySettings,
        strategy_config: FlowScalperConfig,
        risk_config: RiskConfig,
        profile_label: str,
    ) -> bool:
        effective_settings, effective_strategy_config, effective_risk_config = self._cap_settings_for_current_runtime(
            settings,
            strategy_config,
            risk_config,
        )
        self._latest_runtime_settings = settings
        self._latest_strategy_config = strategy_config
        self._latest_risk_config = risk_config
        self._latest_runtime_profile_label = profile_label
        self.state = replace(self.state, custom_settings=_custom_settings_entries(effective_settings))
        if self._runtime_running or self._runtime_busy:
            self._pending_runtime_settings = RuntimeSettingsUpdate(
                settings=effective_settings,
                strategy_config=effective_strategy_config,
                risk_config=effective_risk_config,
                profile_label=profile_label,
            )
            runtime = self.services.runtime
            if runtime is not None:
                self._refresh_runtime_account_display(runtime, context="settings update", include_positions=True)
            else:
                self._refresh_selected_position_detail_from_runtime()
            return True
        self._pending_runtime_settings = None
        self._refresh_selected_position_detail_from_runtime()
        self._apply_runtime_settings_now(
            settings=effective_settings,
            strategy_config=effective_strategy_config,
            risk_config=effective_risk_config,
            profile_label=profile_label,
        )
        return False

    def _apply_pending_runtime_settings(self) -> None:
        pending = self._pending_runtime_settings
        if pending is None:
            return
        self._apply_runtime_settings_now(
            settings=pending.settings,
            strategy_config=pending.strategy_config,
            risk_config=pending.risk_config,
            profile_label=pending.profile_label,
        )
        if self._pending_runtime_settings is pending:
            self._pending_runtime_settings = None

    def _apply_runtime_settings_now(
        self,
        *,
        settings: CustomStrategySettings,
        strategy_config: FlowScalperConfig,
        risk_config: RiskConfig,
        profile_label: str,
    ) -> None:
        self._apply_runtime_settings_now_to_runtime(
            self.services.runtime,
            settings=settings,
            strategy_config=strategy_config,
            risk_config=risk_config,
            profile_label=profile_label,
        )

    def _apply_runtime_settings_now_to_runtime(
        self,
        runtime: RuntimeLike | None,
        *,
        settings: CustomStrategySettings,
        strategy_config: FlowScalperConfig,
        risk_config: RiskConfig,
        profile_label: str,
    ) -> None:
        apply_settings = getattr(runtime, "apply_strategy_settings", None)
        if callable(apply_settings):
            self._apply_runtime_events(
                [
                    apply_settings(
                        settings=settings,
                        strategy_config=strategy_config,
                        risk_config=risk_config,
                        profile_label=profile_label,
                    )
                ]
            )

    def _apply_latest_runtime_settings_to_runtime(self, runtime: RuntimeLike | None) -> bool:
        if (
            self._latest_runtime_settings is None
            or self._latest_strategy_config is None
            or self._latest_risk_config is None
        ):
            return True
        try:
            self._apply_runtime_settings_now_to_runtime(
                runtime,
                settings=self._latest_runtime_settings,
                strategy_config=self._latest_strategy_config,
                risk_config=self._latest_risk_config,
                profile_label=self._latest_runtime_profile_label or self.state.advisor.selected_profile_label,
            )
        except Exception as exc:
            detail = _safe_error_detail(exc)
            message = "Live runtime strategy settings could not be applied."
            if detail:
                message = f"{message} reason: {detail}"
            self._with_system_log("error", "Live runtime", message)
            return False
        return True

    def _apply_latest_runtime_settings_to_current_runtime(self) -> None:
        if (
            self._latest_runtime_settings is None
            or self._latest_strategy_config is None
            or self._latest_risk_config is None
        ):
            return
        effective_settings, effective_strategy_config, effective_risk_config = self._cap_settings_for_current_runtime(
            self._latest_runtime_settings,
            self._latest_strategy_config,
            self._latest_risk_config,
        )
        self.state = replace(self.state, custom_settings=_custom_settings_entries(effective_settings))
        try:
            self._apply_runtime_settings_now_to_runtime(
                self.services.runtime,
                settings=effective_settings,
                strategy_config=effective_strategy_config,
                risk_config=effective_risk_config,
                profile_label=self._latest_runtime_profile_label or self.state.advisor.selected_profile_label,
            )
        except Exception:
            self._with_system_log(
                "warning",
                "테스트 데이터 전환",
                "새 runtime에 기존 전략 설정을 적용하지 못했습니다. 현재 화면의 전략 설정을 다시 선택하세요.",
            )

    def _cap_settings_for_current_runtime(
        self,
        settings: CustomStrategySettings,
        strategy_config: FlowScalperConfig,
        risk_config: RiskConfig,
    ) -> tuple[CustomStrategySettings, FlowScalperConfig, RiskConfig]:
        runtime = self.services.runtime
        source_kind = str(getattr(runtime, "data_source_kind", "") or "").strip().lower()
        if source_kind != "kis-vts":
            return settings, strategy_config, risk_config

        cap = self._kis_intraday_position_cap(runtime)
        requested_positions = int(settings.max_positions)
        safe_positions = cap if requested_positions <= 0 else min(requested_positions, cap)
        if settings.max_positions == safe_positions and risk_config.max_positions == safe_positions:
            return settings, strategy_config, risk_config
        return (
            settings.with_updates(max_positions=safe_positions),
            strategy_config,
            replace(risk_config, max_positions=safe_positions),
        )

    def _kis_intraday_position_cap(self, runtime: RuntimeLike | None) -> int:
        return KIS_INTRADAY_REHEARSAL_MAX_POSITIONS

    def _settings_for_profile(self, profile: str) -> CustomStrategySettings:
        config = self._load_config_with_local_paths()
        values = get_profile_settings(profile)
        max_position_amount = _setting_decimal(values, "max_position_amount")
        return CustomStrategySettings(
            order_cash_amount=_setting_decimal(values, "order_cash_amount"),
            cash_allocation_pct=_setting_decimal(values, "cash_allocation_pct"),
            max_order_amount=_setting_decimal(values, "max_order_amount"),
            max_position_amount=max_position_amount,
            max_symbol_exposure=_symbol_exposure(max_position_amount, config.initial_cash),
            max_positions=config.max_positions,
            max_daily_entries_per_symbol=int(values["max_daily_entries_per_symbol"]),
            stop_loss_pct=_setting_decimal(values, "stop_loss_pct"),
            take_profit_pct=_setting_decimal(values, "take_profit_pct"),
            trailing_stop_pct=_setting_decimal(values, "trailing_stop_pct"),
            max_holding_minutes=config.max_holding_minutes,
            daily_loss_limit=_setting_decimal(values, "max_daily_loss"),
            allow_paper_short=config.allow_paper_short,
            kill_switch=config.kill_switch,
        )

    def _strategy_config_for_profile(self, profile: str, settings: CustomStrategySettings) -> FlowScalperConfig:
        config = self._load_config_with_local_paths()
        values = get_profile_settings(profile)
        momentum = _setting_decimal(values, "min_momentum_pct")
        return replace(
            self._base_strategy_config(config),
            min_momentum_pct=momentum,
            min_short_momentum_pct=-momentum,
            min_signal_confidence=_setting_decimal(values, "min_signal_confidence"),
            min_volume_ratio=_setting_decimal(values, "min_volume_ratio"),
            max_spread_bps=_setting_decimal(values, "max_spread_bps"),
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            max_holding_minutes=settings.max_holding_minutes,
            daily_loss_exit_amount=settings.daily_loss_limit,
            allow_paper_short=settings.allow_paper_short,
        )

    def _risk_config_for_profile(self, profile: str, settings: CustomStrategySettings) -> RiskConfig:
        config = self._load_config_with_local_paths()
        values = get_profile_settings(profile)
        return RiskConfig(
            max_order_amount=Decimal("0"),
            max_position_amount=_setting_decimal(values, "max_position_amount"),
            max_positions=settings.max_positions,
            max_daily_loss=settings.daily_loss_limit,
            max_daily_entries_per_symbol=settings.max_daily_entries_per_symbol,
            max_consecutive_order_failures=config.max_consecutive_order_failures,
            kill_switch=settings.kill_switch,
        )

    def _strategy_config_for_custom(self, settings: CustomStrategySettings) -> FlowScalperConfig:
        config = self._load_config_with_local_paths()
        return replace(
            self._current_strategy_config(config),
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            max_holding_minutes=settings.max_holding_minutes,
            daily_loss_exit_amount=settings.daily_loss_limit,
            allow_paper_short=settings.allow_paper_short,
        )

    def _risk_config_for_custom(self, settings: CustomStrategySettings) -> RiskConfig:
        config = self._load_config_with_local_paths()
        return replace(
            self._current_risk_config(config),
            max_order_amount=Decimal("0"),
            max_position_amount=settings.max_position_amount,
            max_positions=settings.max_positions,
            max_daily_loss=settings.daily_loss_limit,
            max_daily_entries_per_symbol=settings.max_daily_entries_per_symbol,
            kill_switch=settings.kill_switch,
        )

    def _current_strategy_config(self, config: BotConfig) -> FlowScalperConfig:
        if self._pending_runtime_settings is not None:
            return self._pending_runtime_settings.strategy_config
        if self._latest_strategy_config is not None:
            return self._latest_strategy_config
        runtime = self.services.runtime
        strategy_config = getattr(getattr(runtime, "strategy", None), "config", None)
        if isinstance(strategy_config, FlowScalperConfig):
            return strategy_config
        return self._base_strategy_config(config)

    def _current_risk_config(self, config: BotConfig) -> RiskConfig:
        if self._pending_runtime_settings is not None:
            return self._pending_runtime_settings.risk_config
        if self._latest_risk_config is not None:
            return self._latest_risk_config
        runtime = self.services.runtime
        risk_config = getattr(getattr(runtime, "risk_manager", None), "config", None)
        if isinstance(risk_config, RiskConfig):
            return risk_config
        return RiskConfig(
            max_order_amount=Decimal("0"),
            max_position_amount=config.max_position_amount,
            max_positions=config.max_positions,
            max_daily_loss=config.max_daily_loss,
            max_daily_entries_per_symbol=config.max_daily_entries_per_symbol,
            max_consecutive_order_failures=config.max_consecutive_order_failures,
            kill_switch=config.kill_switch,
        )

    def _base_strategy_config(self, config: BotConfig) -> FlowScalperConfig:
        return FlowScalperConfig(
            momentum_window=config.momentum_window,
            min_momentum_pct=config.min_momentum_pct,
            min_short_momentum_pct=-config.min_momentum_pct,
            min_signal_confidence=config.min_signal_confidence,
            volume_window=config.volume_window,
            min_volume_ratio=config.min_volume_ratio,
            max_spread_bps=config.max_spread_bps,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            trailing_stop_pct=config.trailing_stop_pct,
            transaction_tax_pct=config.transaction_tax_pct,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            min_net_profit_pct=config.min_net_profit_pct,
            max_holding_minutes=config.max_holding_minutes,
            daily_loss_exit_amount=config.daily_loss_exit_amount,
            forced_exit_time=config.forced_exit_time,
            allow_paper_short=config.allow_paper_short,
        )

    def _apply_runtime_events(self, events: list[RuntimeEvent]) -> DashboardState:
        trade_entries: list[TradeLogEntry] = []
        system_entries: list[ActivityLogEntry] = []
        for event in events:
            if event.kind == "trade":
                trade_entries.append(build_trade_log_entry(event))
            else:
                system_entries.append(_activity_from_runtime_event(event))

        self.state = replace(
            self.state,
            trade_log=(tuple(reversed(trade_entries)) + self.state.trade_log)[:MAX_DASHBOARD_LOG_ENTRIES],
            system_log=(tuple(reversed(system_entries)) + self.state.system_log)[:MAX_DASHBOARD_LOG_ENTRIES],
        )
        return self.state

    def _live_position_state_from_probe_result(
        self,
        result: Mapping[str, object],
    ) -> tuple[tuple[PositionRow, ...], PositionDetail]:
        positions = _positions_from_live_probe_result(result)
        rows = tuple(build_position_rows(positions, self.symbol_directory))
        selected = self.state.selected_position
        if selected.symbol:
            selected = self._build_position_detail(selected.symbol, positions)
        else:
            selected = PositionDetail.empty()
        return rows, selected

    def _live_probe_failure_account(self, *, preserve_existing: bool, status: str) -> AccountPanel:
        if preserve_existing:
            return replace(self.state.account, status=status, updated_at=_now_label(), runtime_metrics=())
        return _empty_account_panel(status=status)

    def _replace_after_live_probe_failure(
        self,
        *,
        account: AccountPanel,
        notice: ReadOnlyNoticePanel,
        preserve_existing: bool,
    ) -> DashboardState:
        if preserve_existing:
            return replace(self.state, account=account, read_only_notice=notice)
        return replace(
            self.state,
            account=account,
            read_only_notice=notice,
            active_positions=(),
            selected_position=PositionDetail.empty(),
        )

    def _refresh_active_positions_from_runtime(self, runtime: RuntimeLike) -> DashboardState:
        positions = _positions_from_runtime(runtime)
        return self._refresh_active_positions_from_positions(positions)

    def _refresh_active_positions_from_positions(self, positions: Mapping[str, Position]) -> DashboardState:
        rows = tuple(build_position_rows(positions, self.symbol_directory))
        selected = self.state.selected_position
        if selected.symbol:
            selected = self._build_position_detail(selected.symbol, positions)
        self.state = replace(self.state, active_positions=rows, selected_position=selected)
        return self.state

    def _refresh_selected_position_detail_from_runtime(self) -> None:
        selected = self.state.selected_position
        if not selected.symbol:
            return
        self.state = replace(
            self.state,
            selected_position=self._build_position_detail(selected.symbol, self._runtime_positions()),
        )

    def _build_position_detail(self, symbol: str, positions: Mapping[str, Position]) -> PositionDetail:
        settings = self._runtime_chart_settings()
        return build_position_detail(
            symbol,
            positions,
            self.symbol_directory,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
        )

    def _runtime_chart_settings(self) -> CustomStrategySettings:
        if self._pending_runtime_settings is not None:
            return self._pending_runtime_settings.settings
        if self._latest_runtime_settings is not None:
            return self._latest_runtime_settings
        runtime = self.services.runtime
        settings = getattr(runtime, "settings", None)
        if isinstance(settings, CustomStrategySettings):
            return settings
        return CustomStrategySettings.default()

    def _refresh_runtime_metrics_from_runtime(self, runtime: RuntimeLike) -> DashboardState:
        metrics = getattr(runtime, "performance_metrics", None)
        if metrics is None:
            return self.state
        account = replace(self.state.account, runtime_metrics=_runtime_metrics_entries(metrics))
        self.state = replace(self.state, account=account)
        return self.state

    def _stop_runtime_after_cleanup_complete(self, runtime: RuntimeLike) -> DashboardState:
        pause = getattr(runtime, "pause", None)
        pause_failed = False
        if self.state.trading_mode == "real" or self._is_live_runtime(runtime):
            self._live_order_safety_context.reset()
            self._clear_live_read_only_verification()
        if callable(pause):
            try:
                pause_event = pause()
                if pause_event is not None:
                    self._apply_runtime_events([pause_event])
            except Exception:
                pause_failed = True
        self._runtime_running = False
        self.state = replace(self.state, runtime_status="일시정지", account=replace(self.state.account, status="일시정지"))
        if pause_failed:
            return self._with_system_log(
                "warning",
                "정리 모드 완료",
                "보유 포지션은 모두 정리되어 자동매매 예약을 중지했습니다. 다만 runtime pause 이벤트 기록에 실패했습니다.",
            )
        return self._with_system_log(
            "success",
            "정리 모드 완료",
            "보유 포지션이 모두 정리되어 자동매매를 일시정지했습니다. 새 진입을 하려면 정리모드를 끄고 다시 시작하세요.",
        )

    def _refresh_runtime_account_display(
        self,
        runtime: RuntimeLike,
        *,
        context: str,
        include_positions: bool = False,
    ) -> DashboardState:
        if context == "cycle" and self._is_live_runtime(runtime):
            return self._refresh_live_cycle_account_from_runtime(runtime)
        try:
            return self._refresh_virtual_account_from_runtime(runtime, include_positions=include_positions)
        except Exception as exc:
            if self.state.trading_mode != "real" and not self._is_live_runtime(runtime):
                raise

            detail = _safe_error_detail(exc) or exc.__class__.__name__
            message = (
                f"Live account display refresh failed after {context}; "
                "automation remains running and the next cycle will retry broker account checks."
            )
            if detail:
                message = f"{message} detail: {detail}"
            return self._with_system_log("warning", "Live account", redact_sensitive_text(message))

    def _refresh_live_cycle_account_from_runtime(self, runtime: RuntimeLike) -> DashboardState:
        snapshot = getattr(runtime, "latest_cycle_account_snapshot", None)
        if snapshot is None:
            return self.state
        return self._refresh_account_from_snapshot(runtime, snapshot, include_positions=True)

    def _runtime_positions_empty_for_cleanup(self, runtime: RuntimeLike) -> bool:
        try:
            return not _positions_from_runtime(runtime)
        except Exception as exc:
            if self.state.trading_mode != "real" and not self._is_live_runtime(runtime):
                raise

            detail = _safe_error_detail(exc) or exc.__class__.__name__
            message = (
                "Live cleanup position refresh failed; automation remains running until holdings can be verified."
            )
            if detail:
                message = f"{message} detail: {detail}"
            self._with_system_log("warning", "Live positions", redact_sensitive_text(message))
            return False

    def _refresh_virtual_account_from_runtime(
        self,
        runtime: RuntimeLike,
        *,
        include_positions: bool = False,
    ) -> DashboardState:
        broker = getattr(runtime, "broker", None)
        snapshot = broker.snapshot() if broker is not None and callable(getattr(broker, "snapshot", None)) else None
        if snapshot is None:
            return self._refresh_runtime_metrics_from_runtime(runtime)

        return self._refresh_account_from_snapshot(runtime, snapshot, include_positions=include_positions)

    def _refresh_account_from_snapshot(
        self,
        runtime: RuntimeLike,
        snapshot: object,
        *,
        include_positions: bool,
    ) -> DashboardState:
        positions = getattr(snapshot, "positions", {}) or {}
        cash = _decimal(getattr(snapshot, "cash", "0"))
        short_proceeds = _decimal(getattr(snapshot, "short_proceeds", "0"))
        display_cash = _decimal(getattr(snapshot, "free_cash", cash - (short_proceeds * Decimal("2"))))
        equity = _decimal(getattr(snapshot, "equity", cash))
        buying_power = _decimal(getattr(snapshot, "buying_power", cash))
        runtime_label = str(getattr(getattr(runtime, "status", None), "label", "") or self.state.runtime_status or "가상")
        last_price = self.state.account.last_price
        if positions:
            selected_symbol = self.state.selected_position.symbol
            selected_position = positions.get(selected_symbol) if selected_symbol else None
            display_position = selected_position or next(iter(positions.values()))
            last_price = format_krw(_decimal(getattr(display_position, "last_price", "0")))

        metrics = getattr(runtime, "performance_metrics", None)
        masked_account = self.state.account.masked_account if self.state.trading_mode == "real" else "가상계좌"
        account = AccountPanel(
            status=runtime_label,
            masked_account=masked_account,
            cash=format_krw(display_cash),
            equity=format_krw(equity),
            positions=f"{len(positions)}개",
            buying_power=format_krw(buying_power),
            currency="KRW",
            last_price=last_price,
            updated_at=_now_label(),
            runtime_metrics=_runtime_metrics_entries(metrics) if metrics is not None else self.state.account.runtime_metrics,
        )
        self.state = replace(self.state, account=account)
        if include_positions:
            self._refresh_active_positions_from_positions(positions)
        return self.state

    def _refresh_runtime_status_from_runtime(self, runtime: RuntimeLike, *, fallback: str = "") -> DashboardState:
        status = getattr(runtime, "status", None)
        label = getattr(status, "label", "") or fallback
        if not label:
            return self.state
        self.state = replace(self.state, runtime_status=str(label))
        return self.state

    def _kis_rate_limit_decision(self):
        limiter = self._paper_kis_rate_limiter()
        allow_request = getattr(limiter, "allow_request", None)
        if not callable(allow_request):
            return None
        return allow_request("kis_check")

    def _record_kis_limited_request(self) -> None:
        limiter = self._paper_kis_rate_limiter()
        record_request = getattr(limiter, "record_request", None)
        if callable(record_request):
            record_request("kis_check")

    def _record_kis_token_issue(self) -> None:
        limiter = self._paper_kis_rate_limiter()
        record_token_issue = getattr(limiter, "record_token_issue", None)
        if callable(record_token_issue):
            record_token_issue()

    def _paper_kis_rate_limiter(self):
        limiter = self.services.paper_kis_rate_limiter
        if limiter is not None:
            return limiter
        return self.services.kis_rate_limiter

    def _build_live_runtime_for_real_mode(self) -> RuntimeLike | None:
        builder = self.services.live_runtime_builder
        if not callable(builder):
            self._live_runtime_readiness_ready = False
            return None
        try:
            runtime = builder()
        except Exception as exc:
            self._live_runtime_readiness_ready = False
            detail = _safe_error_detail(exc)
            message = "실전 runtime 구성 중 오류가 발생했습니다."
            if detail:
                message = f"{message} 원인: {detail}"
            self._with_system_log("error", "Live runtime", message)
            return None
        if not self._is_live_runtime(runtime):
            self._live_runtime_readiness_ready = False
            self._with_system_log("error", "Live runtime", "실전모드는 execution_mode=live runtime만 실행할 수 있습니다.")
            return None
        if not self._apply_latest_runtime_settings_to_runtime(runtime):
            self._live_runtime_readiness_ready = False
            return None
        blockers = self._live_runtime_order_gate_blockers(
            runtime,
            allow_runtime_sync_pending_orders=True,
        )
        if blockers:
            self._live_runtime_readiness_ready = False
            detail = "; ".join(blockers[:3])
            self._with_system_log(
                "warning",
                "Live runtime",
                f"Live runtime order gate blocked: {detail}",
            )
            return None
        self.services = replace(self.services, runtime=runtime)
        return runtime

    @staticmethod
    def _is_live_runtime(runtime: RuntimeLike | None) -> bool:
        return str(getattr(runtime, "execution_mode", "") or "").strip().lower() == "live"

    def _live_runtime_order_gate_approved(self, runtime: RuntimeLike | None) -> bool:
        return not self._live_runtime_order_gate_blockers(runtime)

    def _live_runtime_order_gate_blockers(
        self,
        runtime: RuntimeLike | None,
        *,
        require_readiness: bool = True,
        allow_runtime_sync_pending_orders: bool = False,
    ) -> list[str]:
        blockers: list[str] = []
        if not self._is_live_runtime(runtime):
            blockers.append("live runtime execution_mode is not live")
            return blockers

        if require_readiness and not self._live_runtime_readiness_ready:
            blockers.append("live readiness check has not passed in this session")

        broker = getattr(runtime, "broker", None)
        if type(broker) is not LiveBroker:
            blockers.append("live broker is not configured")
            return blockers

        config = broker.config
        if not bool(config.allow_live_trading) or not bool(config.live_trading_enabled):
            blockers.append("live trading config gate is disabled")

        env_values = _read_env_values(Path(self.env_file))
        if not self._live_broker_scope_matches_current_env(broker, env_values):
            blockers.append("live broker account scope does not match saved live account stores")
        if not live_order_gate_configured(config, env_values):
            blockers.append("live order gate is not configured for the current session")

        try:
            dependencies_ready = broker.order_dependencies_ready()
        except Exception as exc:
            detail = _safe_error_detail(exc) or exc.__class__.__name__
            blockers.append(f"live broker order dependencies check failed: {detail}")
            dependencies_ready = False
        if not dependencies_ready:
            blockers.append("live broker order dependencies are not ready")
        try:
            blockers.extend(
                blocker
                for blocker in (str(item) for item in broker.order_submission_blockers())
                if not blocker.startswith("live pending order fills require runtime sync:")
                and not (
                    allow_runtime_sync_pending_orders
                    and blocker.startswith("live pending orders unresolved:")
                )
            )
        except Exception as exc:
            detail = _safe_error_detail(exc) or exc.__class__.__name__
            blockers.append(f"live broker order blocker check failed: {detail}")
        if not DashboardController._callable_bool(getattr(broker, "session_approved", None)):
            blockers.append("live order session is not approved")
        if not DashboardController._callable_bool(getattr(broker, "risk_limits_ok", None)):
            blockers.append("live risk limits are blocking orders")
        if self.current_custom_settings().kill_switch:
            return blockers
        if not DashboardController._callable_bool(getattr(broker, "new_entries_allowed", None)):
            blockers.append("live new entries are blocked")
        return blockers

    @staticmethod
    def _live_broker_scope_matches_current_env(broker: LiveBroker, env_values: Mapping[str, str]) -> bool:
        account_no = str(env_values.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
        product_code = str(env_values.get(LIVE_KIS_ENV_KEYS["product_code"]) or "").strip()
        expected_scope = managed_live_position_ledger_scope(account_no, product_code)
        if not expected_scope:
            return False
        if str(broker.env.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip() != account_no:
            return False
        if str(broker.env.get(LIVE_KIS_ENV_KEYS["product_code"]) or "").strip() != product_code:
            return False

        pending_store = broker.pending_order_store
        if type(pending_store) is not JsonPendingLiveOrderStore or pending_store.scope != expected_scope:
            return False

        manual_store = broker.manual_reconciliation_store
        if type(manual_store) is not JsonManualReconciliationStore or manual_store.scope != expected_scope:
            return False

        managed_ledger = broker.managed_position_ledger
        if type(managed_ledger) is not JsonManagedLivePositionLedger or managed_ledger.scope != expected_scope:
            return False

        reconciler = broker.fill_reconciler
        if type(reconciler) is not KisLiveOrderReconciler or reconciler.client is not broker.client:
            return False

        return True

    @staticmethod
    def _callable_bool(value: object) -> bool:
        if callable(value):
            return bool(value())
        return bool(value)

    def _runtime_positions(self) -> Mapping[str, Position]:
        runtime = self.services.runtime
        if runtime is None:
            return {}
        return _positions_from_runtime(runtime)

    def _with_system_log(self, level: str, title: str, message: str) -> DashboardState:
        entry = ActivityLogEntry(level=level, title=title, message=message, timestamp=_now_label())
        self.state = replace(self.state, system_log=(entry, *self.state.system_log)[:MAX_DASHBOARD_LOG_ENTRIES])
        return self.state


def _safe_live_readiness_result(result: Mapping[str, object]) -> dict[str, object]:
    raw_blockers = result.get("blockers", ())
    if isinstance(raw_blockers, (str, bytes)) or not isinstance(raw_blockers, (list, tuple)):
        blockers = [redact_sensitive_text(str(raw_blockers))]
    else:
        blockers = [redact_sensitive_text(str(blocker)) for blocker in raw_blockers]
    raw_live_order_enabled = result.get("live_order_enabled") is True
    ready = result.get("ready") is True and not raw_live_order_enabled
    if raw_live_order_enabled:
        blockers = [*blockers, "live readiness result attempted to enable live orders"]
    return {
        "ready": ready,
        "blockers": blockers,
        "manual_reconciliation_cleared": bool(result.get("manual_reconciliation_cleared")),
        "scanner_snapshot_refreshed": bool(result.get("scanner_snapshot_refreshed")),
        "live_order_enabled": False,
        "note": redact_sensitive_text(str(result.get("note") or "")),
    }


def _readiness_blocked_only_by_runtime_syncable_pending_orders(result: Mapping[str, object]) -> bool:
    if result.get("ready") is True:
        return False
    blockers = result.get("blockers", ())
    if isinstance(blockers, (str, bytes)) or not isinstance(blockers, (list, tuple)):
        normalized = [str(blockers)]
    else:
        normalized = [str(blocker) for blocker in blockers]
    normalized = [blocker.strip() for blocker in normalized if blocker.strip()]
    if not normalized:
        return False
    return all(
        blocker.startswith("pending live order requires reconciliation before live readiness:")
        for blocker in normalized
    )


def build_initial_dashboard_state() -> DashboardState:
    return DashboardState(
        trading_mode="virtual",
        mode_label="가상",
        account=AccountPanel(
            status="연결 전",
            masked_account="******",
            cash="0원",
            equity="0원",
            positions="0개",
            buying_power="0원",
            currency="KRW",
            last_price="0원",
            updated_at="-",
        ),
        advisor=AdvisorPanel(
            selected_profile="balanced",
            selected_profile_label="균형형",
            confidence_label="대기",
            summary="AI 추천을 실행하면 현재 시장 흐름 기준 설정을 제시합니다.",
            profile_options=_profile_options("balanced"),
            metrics=(),
        ),
        custom_settings=_custom_settings_entries(CustomStrategySettings.default()),
        active_positions=(),
        selected_position=PositionDetail.empty(),
        trade_log=(),
        system_log=(
            ActivityLogEntry("info", "앱 시작", "paper dashboard로 시작했습니다.", _now_label()),
        ),
        read_only_notice=ReadOnlyNoticePanel(
            title="거래 모드 보호",
            description=VIRTUAL_MODE_NOTICE,
            locked=True,
            order_enabled=False,
        ),
        runtime_status="정지",
    )


def resolve_dashboard_asset_path(*parts: str, bundle_root: str | Path | None = None) -> Path:
    root = Path(bundle_root or getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root.joinpath(*parts)


def _resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.is_absolute() or config_path != DEFAULT_CONFIG_PATH:
        return str(path)
    bundled_path = resolve_dashboard_asset_path(DEFAULT_CONFIG_PATH)
    if bundled_path.exists():
        return str(bundled_path)
    return str(path)


def _resolve_env_file_path(env_file: str) -> str:
    path = Path(env_file)
    if path.is_absolute() or env_file != DEFAULT_ENV_FILE:
        return str(path)
    candidates: list[Path] = []
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        candidates.extend(
            [
                executable.parent / DEFAULT_ENV_FILE,
                executable.parent.parent / DEFAULT_ENV_FILE,
                executable.parent.parent.parent / DEFAULT_ENV_FILE,
            ]
        )
    candidates.extend(
        [
            Path.cwd() / DEFAULT_ENV_FILE,
            Path.cwd().parent / DEFAULT_ENV_FILE,
            Path.cwd().parent.parent / DEFAULT_ENV_FILE,
        ]
    )
    candidates.append(resolve_dashboard_asset_path(DEFAULT_ENV_FILE))
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(path)


def _validated_env_value(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field_name} is required")
    if "\n" in cleaned or "\r" in cleaned:
        raise ValueError(f"{field_name} must be a single line")
    return cleaned


def _write_env_values(path: Path, values: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    updated_lines: list[str] = []
    written: set[str] = set()
    for line in existing_lines:
        key = _env_line_key(line)
        if key in values:
            updated_lines.append(f"{key}={values[key]}")
            written.add(key)
        else:
            updated_lines.append(line)
    for key in values:
        if key not in written:
            updated_lines.append(f"{key}={values[key]}")
    path.write_text("\n".join(updated_lines).rstrip() + "\n", encoding="utf-8")


def _remove_env_values(path: Path, keys: set[str]) -> None:
    if not path.exists():
        return
    kept_lines = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if _env_line_key(line) not in keys
    ]
    path.write_text("\n".join(kept_lines).rstrip() + "\n", encoding="utf-8")


def _read_env_values(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    return read_env_file(path)


def _env_line_key(line: str) -> str | None:
    if not line or line.lstrip().startswith("#") or "=" not in line:
        return None
    return line.split("=", 1)[0].strip()


def format_krw(value: Decimal) -> str:
    whole = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    return f"{whole:,.0f}원" if whole == whole.to_integral_value() else f"{whole:,.2f}원"


def mask_account_display(value: str) -> str:
    account, separator, product_code = value.partition("-")
    digits = "".join(character for character in account if character.isdigit())
    if len(digits) >= 2:
        masked = f"******{digits[-2:]}"
    elif digits:
        masked = f"******{digits}"
    else:
        masked = "******"
    return f"{masked}{separator}{product_code}" if separator else masked


def _safe_runtime_builder_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    if _contains_sensitive_kis_term(text):
        return exc.__class__.__name__
    text = re.sub(r"[A-Za-z]:\\[^\s,;]+", "<path>", text)
    text = re.sub(r"(?:/[^/\s,;]+)+", "<path>", text)
    text = re.sub(r"\b\d{6,}-\d{2}\b", "******-**", text)
    text = re.sub(r"\b\d{8,}\b", "********", text)
    return text[:180]


def _profile_options(selected: str) -> tuple[ProfileOption, ...]:
    return (
        ProfileOption("conservative", "보수형", "위험 축소 / 작은 진입", selected == "conservative"),
        ProfileOption("balanced", "균형형", "보통 위험 / 균형 목표", selected == "balanced"),
        ProfileOption("aggressive", "공격형", "강한 흐름 / 큰 진입", selected == "aggressive"),
    )


def _trading_mode_notice(mode: str) -> str:
    return REAL_MODE_NOTICE if mode == "real" else VIRTUAL_MODE_NOTICE


def _custom_settings_entries(settings: CustomStrategySettings) -> tuple[tuple[str, str], ...]:
    return (
        ("매수 방식", "현금 기준 자동 계산"),
        ("현금 사용 비율", _format_pct(settings.cash_allocation_pct)),
        ("종목 안전 상한", format_krw(settings.max_position_amount)),
        ("최대 보유 종목", _format_max_positions(settings.max_positions)),
        ("손절", _format_pct(settings.stop_loss_pct)),
        ("익절", _format_pct(settings.take_profit_pct)),
        ("트레일링", _format_pct(settings.trailing_stop_pct)),
        ("최대 보유 시간", "꺼짐" if settings.max_holding_minutes <= 0 else f"{settings.max_holding_minutes}분"),
        ("일 손실 한도", format_krw(settings.daily_loss_limit)),
        ("숏 허용", "허용" if settings.allow_paper_short else "차단"),
    )


def _runtime_metrics_entries(metrics) -> tuple[tuple[str, str], ...]:
    return (
        ("Paper 총손익", format_krw(_decimal(getattr(metrics, "total_pnl", "0")))),
        ("실현손익", format_krw(_decimal(getattr(metrics, "realized_pnl", "0")))),
        ("평가손익", format_krw(_decimal(getattr(metrics, "unrealized_pnl", "0")))),
        ("승률", _format_percent_value(_decimal(getattr(metrics, "win_rate_pct", "0")))),
        (
            "체결/거절",
            f"{int(getattr(metrics, 'filled_trades', 0)):,} / {int(getattr(metrics, 'rejected_trades', 0)):,}",
        ),
    )


def _format_max_positions(value: int) -> str:
    return "제한 없음" if int(value) <= 0 else f"{int(value)}개"


def _format_pct(value: Decimal) -> str:
    return f"{format((value * Decimal('100')).normalize(), 'f')}%"


def _format_percent_value(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    if quantized == quantized.to_integral_value():
        return f"{quantized:.0f}%"
    return f"{quantized:.2f}%"


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _setting_decimal(values: Mapping[str, object], key: str) -> Decimal:
    return Decimal(str(values[key]))


def _symbol_exposure(max_position_amount: Decimal, initial_cash: Decimal) -> Decimal:
    if initial_cash <= 0:
        return CustomStrategySettings.default().max_symbol_exposure
    return max_position_amount / initial_cash


def _korean_reason_summary(reasons: list[str]) -> str:
    text = " ".join(reasons).lower()
    if "mixed" in text:
        return "현재 흐름은 혼합적입니다. 균형형 설정을 우선합니다."
    if "volatility" in text or "spread" in text:
        return "변동성 또는 스프레드 부담이 있어 보수형 설정을 우선합니다."
    if "momentum" in text and "volume" in text:
        return "모멘텀과 거래량이 확인되어 공격형 설정을 검토할 수 있습니다."
    if not reasons:
        return "추천 사유가 없습니다."
    return "현재 시장 조건을 반영한 설정을 추천했습니다."


def _safe_kis_error_message(exc: Exception, *, live: bool = False) -> str:
    message = str(exc)
    if isinstance(exc, ValueError) and message.startswith("missing KIS VTS credentials:"):
        missing = message.removeprefix("missing KIS VTS credentials:").strip()
        return f".env 파일에서 KIS 모의투자 설정을 찾지 못했습니다. 누락 항목: {missing}"
    if isinstance(exc, ValueError) and message.startswith("missing KIS live credentials:"):
        missing = message.removeprefix("missing KIS live credentials:").strip()
        return f".env 파일에서 KIS 실전 계좌 설정을 찾지 못했습니다. 누락 항목: {missing}"
    if isinstance(exc, UnicodeDecodeError):
        return ".env 파일을 UTF-8로 읽지 못했습니다. .env 파일을 UTF-8 형식으로 다시 저장한 뒤 재시도하세요."
    if isinstance(exc, OSError):
        return ".env 파일을 읽지 못했습니다. 파일 위치와 읽기 권한을 확인하세요."
    if "EGW00201" in message or "초당 거래건수" in message:
        return "KIS 초당 요청 제한을 초과했습니다. 잠시 후 다시 시도하세요. 앱이 조회 중이면 자동으로 1회 재시도합니다."
    if "EGW00133" in message or "1분" in message or "rate limit" in message.lower():
        return "KIS 접근토큰은 1분당 1회만 발급됩니다. 방금 연결 확인을 실행했다면 1분 후 다시 시도하세요."
    if isinstance(exc, KisApiError):
        parsed_message = _kis_api_error_message(message, live=live)
        if parsed_message:
            return parsed_message
        if message.startswith("KIS network error:"):
            return "KIS 네트워크 오류가 발생했습니다. 인터넷 연결 또는 KIS API 상태를 확인하세요."
        account_label = "실전" if live else "모의투자"
        return f"KIS API 오류가 발생했습니다. API 권한, {account_label} 계좌번호, 상품코드 설정을 확인하세요."
    account_label = "실전" if live else "모의투자"
    return f"오류가 발생했습니다. API 또는 {account_label} 계좌 설정을 확인하세요."


def _live_market_closed_message(status: object | None) -> str:
    if status is None or getattr(status, "is_open", None) is not False:
        return ""
    message = (
        "실전 계좌 조회는 완료했습니다. 현재 정규장 시간이 아닙니다. "
        "실전 자동매매는 정규장(09:00-15:30 KST)에만 실행됩니다."
    )
    next_open = getattr(status, "next_open", None)
    if isinstance(next_open, datetime):
        message = f"{message} 다음 정규장: {next_open:%Y-%m-%d %H:%M KST}"
    return message


def _kis_api_error_message(message: str, *, live: bool = False) -> str | None:
    payload = _kis_error_payload(message)
    http_match = re.search(r"KIS HTTP\s+(\d+)", message)
    code = _kis_error_code(message, payload)
    detail = _kis_error_detail(payload)

    tags = []
    if http_match:
        tags.append(f"HTTP {http_match.group(1)}")
    if code:
        tags.append(code)
    if not tags and not detail:
        return None

    suffix = f"({', '.join(tags)})" if tags else ""
    account_label = "실전" if live else "모의투자"
    guidance = f"API 권한, {account_label} 계좌번호, 상품코드 설정을 확인하세요."
    if detail and _is_safe_kis_detail(detail):
        return f"KIS API 오류{suffix}: {detail}. {guidance}"
    return f"KIS API 오류{suffix}. {guidance}"


def _kis_error_payload(message: str) -> dict[str, object]:
    start = message.find("{")
    if start < 0:
        return {}
    try:
        payload, _ = json.JSONDecoder().raw_decode(message[start:])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _kis_error_code(message: str, payload: Mapping[str, object]) -> str:
    for key in ("error_code", "msg_cd"):
        value = payload.get(key)
        if value and _is_safe_kis_error_code(str(value)):
            return str(value)
    if payload:
        return ""
    match = re.search(r"\b[A-Z]{2,8}\d{3,6}\b", message)
    if match and _is_safe_kis_error_code(match.group(0)):
        return match.group(0)
    return ""


def _is_safe_kis_error_code(value: str) -> bool:
    stripped = value.strip()
    return SAFE_KIS_ERROR_CODE_PATTERN.fullmatch(stripped) is not None and not _contains_sensitive_kis_term(stripped)


def _kis_error_detail(payload: Mapping[str, object]) -> str:
    for key in ("error_description", "msg1", "message", "msg"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    return ""


def _is_safe_kis_detail(detail: str) -> bool:
    return not _contains_sensitive_kis_term(detail) and re.search(r"\d{8,}", detail) is None


def _contains_sensitive_kis_term(value: str) -> bool:
    normalized = re.sub(r"[\s_-]+", "", value.lower())
    return any(term in normalized for term in SENSITIVE_KIS_TERMS)


def _activity_from_runtime_event(event: RuntimeEvent) -> ActivityLogEntry:
    timestamp = event.timestamp.strftime("%H:%M:%S")
    return ActivityLogEntry("info", "Paper runtime", _safe_runtime_message(event.message), timestamp)


def _positions_from_runtime(runtime: RuntimeLike) -> Mapping[str, Position]:
    snapshot = runtime.broker.snapshot()
    return snapshot.positions


def _positions_from_live_probe_result(result: Mapping[str, object]) -> Mapping[str, Position]:
    raw_positions = result.get("positions")
    if not isinstance(raw_positions, (list, tuple)):
        return {}

    opened_at = datetime.now()
    positions: dict[str, Position] = {}
    for raw_position in raw_positions:
        if not isinstance(raw_position, Mapping):
            continue
        symbol = str(raw_position.get("symbol") or "").strip()
        quantity = _integer(raw_position.get("quantity"))
        avg_price = _decimal(raw_position.get("avg_price", raw_position.get("average_price", "0")))
        last_price = _decimal(raw_position.get("last_price", raw_position.get("current_price", "0")))
        if not symbol or quantity <= 0 or avg_price <= 0 or last_price <= 0:
            continue
        side = str(raw_position.get("side") or "LONG").strip().upper()
        if side not in {"LONG", "SHORT"}:
            side = "LONG"
        sellable_quantity = _optional_integer(raw_position.get("sellable_quantity"))
        positions[symbol] = Position(
            symbol=symbol,
            quantity=quantity,
            avg_price=avg_price,
            last_price=last_price,
            opened_at=opened_at,
            highest_price=max(avg_price, last_price),
            lowest_price=min(avg_price, last_price),
            side=side,
            sellable_quantity=sellable_quantity,
        )
    return positions


def _integer(value: object) -> int:
    parsed = _decimal(value)
    if parsed != parsed.to_integral_value():
        return 0
    return int(parsed)


def _optional_integer(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    parsed = _integer(value)
    return max(0, parsed)


def _is_kis_token_rate_limit_error(exc: Exception) -> bool:
    message = str(exc)
    return "EGW00133" in message or (
        "접근토큰" in message and ("1분" in message or "1 minute" in message.lower())
    )


def _safe_runtime_message(message: str) -> str:
    if _contains_sensitive_kis_term(message) or re.search(r"\d{8,}", message):
        return "runtime 메시지에 민감정보가 포함되어 내용을 숨겼습니다."
    return message


def _runtime_cycle_exception_message(stage: str, exc: Exception) -> str:
    error_type = type(exc).__name__
    detail = _safe_error_detail(exc)
    if detail:
        return f"cycle_exception - stage={stage}, error={error_type}, detail={detail}"
    return f"cycle_exception - stage={stage}, error={error_type}"


def _now_label() -> str:
    return datetime.now().strftime("%H:%M:%S")
