from __future__ import annotations

import argparse
import json
import math
import re
import secrets
from collections import Counter
from threading import RLock
from dataclasses import is_dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import KIS_INTRADAY_REHEARSAL_MAX_POSITIONS, KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, load_config
from .dashboard import DashboardController
from .live_broker import LIVE_PENDING_ORDER_CANCEL_AFTER
from .live_safety import (
    LIVE_KIS_ENV_KEYS,
    live_credential_scope_fingerprint,
)
from .runtime_factory import create_default_controller
from .trade_log import build_trade_log_entry


APP_TITLE = "개미親주식"
APP_SUBTITLE = "가상 자동매매"
APP_AUTHOR_LABEL = "MadeBy :heewoung-lee"
APP_AUTHOR_URL = "https://github.com/heewoung-lee"

SENSITIVE_PATTERNS = (
    re.compile(r"Authorization\s*:\s*Bearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[^,\s]+", re.IGNORECASE),
    re.compile(r"KIS_[A-Z0-9_]*(KEY|SECRET|TOKEN|ACCOUNT|APP)[A-Z0-9_]*\s*[=:]\s*[^,\s]+", re.IGNORECASE),
    re.compile(
        r"['\"]?\b(app[_\s-]?secret|appsecret|app[_\s-]?key|appkey|api[_\s-]?key|apikey|authorization|token|account[_\s-]?no|accountno|account|acct)\b['\"]?\s*[:=]\s*['\"]?[^,\s;\"'}]+['\"]?",
        re.IGNORECASE,
    ),
    re.compile(r"C:\\Users\\[^,\s]+", re.IGNORECASE),
    re.compile(r"\b\d{8,}(?:-\d{2})?\b"),
)
DEFAULT_ALLOWED_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")
BRIDGE_TOKEN_HEADER = "X-StockBot-Bridge-Token"
LIVE_FIRST_CYCLE_GRACE_SECONDS = 75.0
REAL_MODE_BLOCKED_ACTIONS = frozenset({"data-source", "kis-check"})
CREDENTIAL_SCOPE_RECOVERY_ALLOWED_ACTIONS = frozenset(
    {
        "kis-live-credentials",
        "pause",
    }
)
BRIDGE_LOCK_FREE_ACTIONS = frozenset({"cycle", "kis-check", "kis-live-check"})
SCHEDULER_ACTIONS = frozenset(
    {
        "not_started",
        "cycle_running",
        "service_stopped",
        "manual_pause",
        "credential_scope_pending",
        "credential_scope_changed",
        "market_status_unavailable",
        "market_closed",
        "live_account_probe_blocked",
        "runtime_started",
        "runtime_start_blocked",
        "cycle_completed",
        "scheduler_error",
    }
)


def dashboard_state_to_view_model(
    controller: DashboardController,
    *,
    scheduler_owner: str = "renderer",
    scheduler_control: object | None = None,
) -> dict[str, Any]:
    state = controller.state
    runtime_running = bool(getattr(controller, "_runtime_running", False))
    trading_mode = state.trading_mode if state.trading_mode in {"virtual", "real"} else "virtual"
    live_order_ready = trading_mode == "real" and _live_order_session_ready(controller)

    view_model = {
        "stateRevision": int(getattr(controller, "state_revision", 0)),
        "app": {
            "title": APP_TITLE,
            "subtitle": APP_SUBTITLE,
            "authorLabel": APP_AUTHOR_LABEL,
            "authorUrl": APP_AUTHOR_URL,
            "version": "0.1.0",
        },
        "mode": {
            "key": trading_mode,
            "label": "리얼 모드" if trading_mode == "real" else "가상 모드",
            "isReal": trading_mode == "real",
        },
        "runtime": {
            "status": _runtime_status_label(
                trading_mode,
                runtime_running,
                state.runtime_status,
                live_order_ready=live_order_ready,
            ),
            "running": runtime_running,
            "cycleLabel": "다음 cycle 예정됨" if runtime_running else "예약 없음",
            "lastUpdated": _safe_text(state.account.updated_at),
            "dataSource": _runtime_data_source_label(controller),
            "dataSourceKind": _runtime_data_source_kind(controller),
            "dataModeLabel": _runtime_data_mode_label(controller),
            "dataModeDescription": _runtime_data_mode_description(controller),
            "safetySummary": _runtime_safety_summary(controller),
            "cleanupMode": controller.current_custom_settings().kill_switch,
        },
        "notice": _notice_view_model(controller),
        "account": _account_view_model(controller),
        "positions": [_position_row_to_view_model(row) for row in state.active_positions],
        "selectedPosition": _position_detail_to_view_model(state.selected_position),
        "logs": {
            "trades": [_trade_log_to_view_model(entry) for entry in state.trade_log],
            "system": [_system_log_to_view_model(entry) for entry in state.system_log],
        },
        "settings": {
            "kisLiveCredentials": controller.kis_live_credential_status(),
            "liveOrderApproval": controller.live_order_approval_status(),
        },
        "debug": _debug_view_model(controller, runtime_running, trading_mode),
    }
    runtime_view = view_model["runtime"]
    runtime_view["schedulerOwner"] = scheduler_owner
    if scheduler_owner != "renderer":
        scheduler_timing = (
            _scheduler_timing_snapshot(scheduler_control)
            if scheduler_control is not None
            else None
        )
        runtime_view["schedulerActive"] = _scheduler_boolean_value(
            scheduler_timing,
            "active",
        )
    else:
        scheduler_timing = None
        runtime_view["schedulerActive"] = (
            bool(getattr(scheduler_control, "active", False))
            if scheduler_control is not None
            else runtime_running
        )
    if scheduler_owner != "renderer" and scheduler_control is not None:
        runtime_view["schedulerCycleInProgress"] = _scheduler_boolean_value(
            scheduler_timing,
            "cycle_in_progress",
        )
        runtime_view["schedulerIntervalSeconds"] = _scheduler_timing_value(
            scheduler_timing,
            "interval_seconds",
            allow_zero=False,
        )
        runtime_view["schedulerSecondsUntilNextCycle"] = _scheduler_timing_value(
            scheduler_timing,
            "seconds_until_next_cycle",
            allow_zero=True,
        )
        scheduler_diagnostic = _scheduler_diagnostic_view(scheduler_timing)
        runtime_view["schedulerConfiguredIdleSeconds"] = scheduler_diagnostic[
            "configuredIdleSeconds"
        ]
        runtime_view["schedulerLastCycleDurationSeconds"] = scheduler_diagnostic[
            "lastCycleDurationSeconds"
        ]
        runtime_view["schedulerLastCycleStartIntervalSeconds"] = (
            scheduler_diagnostic["lastCycleStartIntervalSeconds"]
        )
        runtime_view["schedulerCycleDurationSampleCount"] = scheduler_diagnostic[
            "cycleDurationSampleCount"
        ]
        runtime_view["schedulerCycleDurationP95Seconds"] = scheduler_diagnostic[
            "cycleDurationP95Seconds"
        ]
        runtime_view["schedulerCycleStartIntervalSampleCount"] = (
            scheduler_diagnostic["cycleStartIntervalSampleCount"]
        )
        runtime_view["schedulerCycleStartIntervalP95Seconds"] = (
            scheduler_diagnostic["cycleStartIntervalP95Seconds"]
        )
        runtime_view["schedulerCurrentAction"] = scheduler_diagnostic[
            "currentAction"
        ]
        runtime_view["schedulerLastCycleCompletedAt"] = scheduler_diagnostic[
            "lastCycleCompletedAt"
        ]
        view_model["debug"]["scheduler"] = scheduler_diagnostic
        diagnostic_capabilities = view_model["debug"].get(
            "diagnosticCapabilities"
        )
        if (
            isinstance(diagnostic_capabilities, list)
            and "scheduler-cycle-timing" not in diagnostic_capabilities
        ):
            diagnostic_capabilities.append("scheduler-cycle-timing")
        runtime_view["cycleLabel"] = str(
            getattr(scheduler_control, "cycle_label", "")
            or runtime_view["cycleLabel"]
        )
        runtime_view["schedulerFailureCount"] = max(
            0,
            int(getattr(scheduler_control, "consecutive_failures", 0) or 0),
        )
        runtime_view["schedulerErrorStage"] = _scheduler_diagnostic_code(
            getattr(scheduler_control, "last_error_stage", "")
        )
        runtime_view["schedulerErrorCode"] = _scheduler_diagnostic_code(
            getattr(scheduler_control, "last_error_code", "")
        )
    return view_model


def _scheduler_timing_snapshot(scheduler_control: object) -> object | None:
    try:
        snapshot_provider = getattr(scheduler_control, "timing_snapshot")
        if not callable(snapshot_provider):
            return None
        return snapshot_provider()
    except Exception:
        return None


def _scheduler_timing_value(
    scheduler_control: object,
    attribute: str,
    *,
    allow_zero: bool,
) -> float | None:
    try:
        raw_value = getattr(scheduler_control, attribute)
        if isinstance(raw_value, bool):
            return None
        value = float(raw_value)
    except Exception:
        return None
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        return None
    return value


def _scheduler_boolean_value(
    scheduler_control: object,
    attribute: str,
) -> bool:
    try:
        value = getattr(scheduler_control, attribute)
    except Exception:
        return False
    return value if isinstance(value, bool) else False


def _scheduler_diagnostic_view(scheduler_timing: object | None) -> dict[str, Any]:
    configured_idle_seconds = _scheduler_timing_value(
        scheduler_timing,
        "configured_idle_seconds",
        allow_zero=False,
    )
    if configured_idle_seconds is None:
        configured_idle_seconds = _scheduler_timing_value(
            scheduler_timing,
            "interval_seconds",
            allow_zero=False,
        )
    return {
        "configuredIdleSeconds": configured_idle_seconds,
        "lastCycleDurationSeconds": _scheduler_timing_value(
            scheduler_timing,
            "last_cycle_duration_seconds",
            allow_zero=True,
        ),
        "lastCycleStartIntervalSeconds": _scheduler_timing_value(
            scheduler_timing,
            "last_cycle_start_interval_seconds",
            allow_zero=True,
        ),
        "cycleDurationSampleCount": _scheduler_count_value(
            scheduler_timing,
            "cycle_duration_sample_count",
        ),
        "cycleDurationP95Seconds": _scheduler_timing_value(
            scheduler_timing,
            "cycle_duration_p95_seconds",
            allow_zero=True,
        ),
        "cycleStartIntervalSampleCount": _scheduler_count_value(
            scheduler_timing,
            "cycle_start_interval_sample_count",
        ),
        "cycleStartIntervalP95Seconds": _scheduler_timing_value(
            scheduler_timing,
            "cycle_start_interval_p95_seconds",
            allow_zero=True,
        ),
        "currentAction": _scheduler_action_value(
            scheduler_timing,
            "current_action",
        ),
        "lastCycleCompletedAt": _scheduler_timestamp_value(
            scheduler_timing,
            "last_cycle_completed_at",
        ),
    }


def _scheduler_count_value(
    scheduler_control: object,
    attribute: str,
) -> int | None:
    try:
        raw_value = getattr(scheduler_control, attribute)
        if isinstance(raw_value, bool):
            return None
        numeric_value = float(raw_value)
        value = int(numeric_value)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric_value) or numeric_value != value or value < 0:
        return None
    return value


def _scheduler_action_value(
    scheduler_control: object,
    attribute: str,
) -> str:
    try:
        value = getattr(scheduler_control, attribute)
    except Exception:
        return ""
    return value if isinstance(value, str) and value in SCHEDULER_ACTIONS else ""


def _scheduler_timestamp_value(
    scheduler_control: object,
    attribute: str,
) -> str:
    try:
        raw_value = getattr(scheduler_control, attribute)
        if not isinstance(raw_value, str) or len(raw_value) > 64:
            return ""
        parsed = datetime.fromisoformat(raw_value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return ""
        return parsed.astimezone(timezone.utc).isoformat()
    except Exception:
        return ""


def _scheduler_diagnostic_code(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))[:64]


def redact_sensitive_text(value: object) -> str:
    text = str(value)
    for pattern in SENSITIVE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def create_bridge_server(
    controller: DashboardController | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    config_path: str | None = None,
    allowed_origins: tuple[str, ...] = DEFAULT_ALLOWED_ORIGINS,
    allow_real_actions: bool = False,
    scheduler_owner: str = "renderer",
    scheduler_control: object | None = None,
    persistent_real_mode: bool = False,
) -> ThreadingHTTPServer:
    bridge_controller = controller or create_default_controller(config_path=config_path)
    bridge_token = token or secrets.token_urlsafe(32)
    bridge_lock = RLock()
    origin_allowlist = set(allowed_origins)

    class StockBotBridgeHandler(BaseHTTPRequestHandler):
        server_version = "StockBotElectronBridge/0.1"

        def do_OPTIONS(self) -> None:
            if not self._origin_allowed():
                self._send_error(403, "origin_not_allowed")
                return
            self._send_json({"ok": True})

        def do_GET(self) -> None:
            parsed_url = urlparse(self.path)
            path = parsed_url.path
            if path == "/api/health":
                self._send_json({"ok": True, "app": APP_TITLE})
                return
            if not self._request_authorized():
                self._send_error(403, "unauthorized")
                return
            if path == "/api/state":
                with bridge_lock:
                    self._send_json(
                        dashboard_state_to_view_model(
                            bridge_controller,
                            scheduler_owner=scheduler_owner,
                            scheduler_control=scheduler_control,
                        )
                    )
                return
            if path == "/api/profit-report":
                try:
                    query = _profit_report_query(parsed_url.query)
                except ValueError as exc:
                    self._send_error(400, redact_sensitive_text(exc))
                    return
                try:
                    with bridge_lock:
                        report = bridge_controller.profit_report(**query)
                except Exception as exc:
                    self._send_error(500, redact_sensitive_text(exc))
                    return
                self._send_json(report)
                return
            self._send_error(404, "not_found")

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/actions/"):
                self._send_error(404, "not_found")
                return
            action = path.rsplit("/", 1)[-1]
            if not self._request_authorized():
                self._send_error(403, "unauthorized")
                return
            try:
                payload = self._read_json_body()
                if action in BRIDGE_LOCK_FREE_ACTIONS:
                    view_model = self._run_action(action, payload)
                else:
                    with bridge_lock:
                        view_model = self._run_action(action, payload)
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._send_error(400, redact_sensitive_text(exc))
                return
            except PermissionError as exc:
                self._send_error(423, redact_sensitive_text(exc))
                return
            except Exception as exc:
                self._send_error(500, redact_sensitive_text(exc))
                return
            self._send_json(view_model)

        def _run_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
            action_result = _dispatch_action(
                bridge_controller,
                action,
                payload,
                allow_real_actions=allow_real_actions,
                scheduler_owner=scheduler_owner,
                scheduler_control=scheduler_control,
                persistent_real_mode=persistent_real_mode,
            )
            view_model = dashboard_state_to_view_model(
                bridge_controller,
                scheduler_owner=scheduler_owner,
                scheduler_control=scheduler_control,
            )
            if action_result:
                view_model.update(action_result)
            return view_model

        def log_message(self, format: str, *args: object) -> None:
            return

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            decoded = raw.decode("utf-8")
            payload = json.loads(decoded)
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            return payload

        def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                origin = self.headers.get("Origin")
                if origin in origin_allowlist:
                    self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", f"Content-Type, {BRIDGE_TOKEN_HEADER}")
                self.end_headers()
                self.wfile.write(encoded)
            except OSError as exc:
                if _is_client_disconnect(exc):
                    return
                raise

        def _send_error(self, status: int, message: object) -> None:
            self._send_json({"ok": False, "error": redact_sensitive_text(message)}, status=status)

        def _origin_allowed(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in origin_allowlist

        def _request_authorized(self) -> bool:
            if not self._origin_allowed():
                return False
            return secrets.compare_digest(self.headers.get(BRIDGE_TOKEN_HEADER, ""), bridge_token)

    server = ThreadingHTTPServer((host, port), StockBotBridgeHandler)
    server.controller = bridge_controller  # type: ignore[attr-defined]
    server.bridge_token = bridge_token  # type: ignore[attr-defined]
    server.scheduler_owner = scheduler_owner  # type: ignore[attr-defined]
    server.scheduler_control = scheduler_control  # type: ignore[attr-defined]
    return server


def serve_forever(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str | None = None,
    config_path: str | None = None,
    allow_real_actions: bool = False,
) -> None:
    server = create_bridge_server(
        host=host,
        port=port,
        token=token,
        config_path=config_path,
        allow_real_actions=allow_real_actions,
    )
    actual_host, actual_port = server.server_address
    print(
        json.dumps(
            {"ok": True, "host": actual_host, "port": actual_port, "token": server.bridge_token},
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the StockBot Electron dashboard bridge.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--allow-real-actions", action="store_true")
    args = parser.parse_args(argv)
    serve_forever(
        host=args.host,
        port=args.port,
        token=args.token or None,
        config_path=args.config_path,
        allow_real_actions=args.allow_real_actions,
    )
    return 0


def _dispatch_action(
    controller: DashboardController,
    action: str,
    payload: dict[str, Any],
    *,
    allow_real_actions: bool = False,
    scheduler_owner: str = "renderer",
    scheduler_control: object | None = None,
    persistent_real_mode: bool = False,
) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    if (
        scheduler_owner != "renderer"
        and action not in CREDENTIAL_SCOPE_RECOVERY_ALLOWED_ACTIONS
    ):
        _validate_service_credential_scope(
            scheduler_control,
        )
    if scheduler_owner != "renderer" and action == "cycle":
        raise PermissionError("backend service owns runtime cycle scheduling")
    if persistent_real_mode and action == "mode":
        requested_mode = _required_string(payload, "mode").strip().lower()
        if requested_mode != "real":
            raise PermissionError("persistent live service keeps the controller in real mode")
    if (
        not allow_real_actions
        and controller.state.trading_mode == "real"
        and action in REAL_MODE_BLOCKED_ACTIONS
    ):
        raise PermissionError("real mode action is locked until live-order safety gates are approved")
    if action == "start":
        resume = getattr(scheduler_control, "resume", None)
        if scheduler_owner != "renderer":
            if not callable(resume):
                raise RuntimeError("backend service scheduler control is unavailable")
            resume()
        else:
            controller.start_paper_runtime()
    elif action == "pause":
        suspend = getattr(scheduler_control, "suspend", None)
        if callable(suspend):
            suspend()
        controller.pause_paper_runtime()
    elif action == "cycle":
        controller.run_paper_cycle()
    elif action == "kis-check":
        controller.run_kis_check()
    elif action == "kis-live-check":
        controller.run_kis_live_check(activate_real_mode=False)
        result = _kis_live_check_action_result(controller)
    elif action == "live-readiness-check":
        _, readiness = controller.run_live_readiness_check(
            refresh_scanner_snapshot=_payload_bool(payload, "refreshScannerSnapshot", False),
        )
        result = _live_readiness_action_result(readiness)
    elif action == "clear-manual-reconciliation":
        _, readiness = controller.clear_live_manual_reconciliation(
            confirmation_phrase=_required_string(payload, "confirmationPhrase"),
        )
        result = _manual_reconciliation_clear_action_result(readiness)
    elif action == "mode":
        mode = _required_string(payload, "mode")
        controller.select_trading_mode(mode)
        if mode.strip().lower() == "real":
            controller.run_kis_live_check(activate_real_mode=True)
            result = _kis_live_check_action_result(controller)
    elif action == "data-source":
        source = _dashboard_data_source(_required_string(payload, "source"))
        state = controller.select_runtime_data_source(source)
        result = _data_source_action_result(source, state)
    elif action == "cleanup-mode":
        cleanup_enabled = _payload_bool(payload, "enabled", False)
        if controller.state.trading_mode == "real" and not cleanup_enabled:
            raise PermissionError("real mode cleanup can only be enabled from the dashboard")
        controller.set_cleanup_mode(cleanup_enabled)
    elif action == "kis-credentials":
        controller.save_kis_credentials(
            app_key=_required_env_credential_value(payload, "appKey"),
            app_secret=_required_env_credential_value(payload, "appSecret"),
            account_no=_required_env_credential_value(payload, "accountNo"),
            product_code=_required_env_credential_value(payload, "productCode"),
        )
    elif action == "kis-live-credentials":
        app_key = _required_env_credential_value(payload, "appKey")
        app_secret = _required_env_credential_value(payload, "appSecret")
        account_no = _required_env_credential_value(payload, "accountNo")
        product_code = _required_env_credential_value(payload, "productCode")
        candidate_fingerprint = live_credential_scope_fingerprint(
            {
                LIVE_KIS_ENV_KEYS["app_key"]: app_key,
                LIVE_KIS_ENV_KEYS["app_secret"]: app_secret,
                LIVE_KIS_ENV_KEYS["account_no"]: account_no,
                LIVE_KIS_ENV_KEYS["product_code"]: product_code,
            }
        )
        if scheduler_owner != "renderer":
            validate_candidate = getattr(
                scheduler_control,
                "validate_candidate_credential_scope",
                None,
            )
            if not callable(validate_candidate):
                raise RuntimeError(
                    "backend service credential validation control is unavailable"
                )
            validate_candidate(candidate_fingerprint)
        previous_system_log = getattr(controller.state, "system_log", ())
        previous_latest_log = (
            previous_system_log[0]
            if isinstance(previous_system_log, tuple) and previous_system_log
            else None
        )
        saved_state = controller.save_kis_live_credentials(
            app_key=app_key,
            app_secret=app_secret,
            account_no=account_no,
            product_code=product_code,
        )
        if (
            scheduler_owner != "renderer"
            and _kis_live_credential_save_succeeded(
                saved_state,
                previous_latest_log=previous_latest_log,
            )
        ):
            bind_saved_scope = getattr(
                scheduler_control,
                "bind_saved_credential_scope",
                None,
            )
            if not callable(bind_saved_scope):
                raise RuntimeError(
                    "backend service credential binding control is unavailable"
                )
            bind_saved_scope(candidate_fingerprint)
    elif action == "live-order-approval":
        controller.save_live_order_approval(
            confirmation_phrase=_required_string(payload, "confirmationPhrase"),
            account_confirmation=_required_string(payload, "accountConfirmation"),
        )
    elif action == "position":
        controller.select_position(_required_string(payload, "symbol"))
    else:
        raise ValueError(f"unknown action: {action}")
    bump_revision = getattr(controller, "bump_state_revision", None)
    if callable(bump_revision):
        bump_revision()
    if action in {"data-source", "kis-live-check", "live-readiness-check", "clear-manual-reconciliation", "mode"}:
        return result
    return None


def _dashboard_data_source(source: str) -> str:
    normalized = source.strip().lower()
    if normalized == "kis-vts":
        return "external-scan-kis"
    return normalized


def _profit_report_query(raw_query: str) -> dict[str, str]:
    parsed = parse_qs(raw_query, keep_blank_values=True, strict_parsing=True)
    expected = {"granularity", "scope", "anchor", "timezone"}
    if set(parsed) != expected or any(len(values) != 1 for values in parsed.values()):
        raise ValueError("invalid profit report query")
    granularity = parsed["granularity"][0]
    scope = parsed["scope"][0]
    anchor = parsed["anchor"][0]
    timezone_name = parsed["timezone"][0]
    if granularity not in {"hour", "day", "month", "year"}:
        raise ValueError("invalid profit report granularity")
    if scope not in {"account", "stockbot"}:
        raise ValueError("invalid profit report scope")
    if timezone_name != "Asia/Seoul":
        raise ValueError("invalid profit report timezone")
    try:
        parsed_anchor = datetime.strptime(anchor, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("invalid profit report anchor") from exc
    if parsed_anchor.isoformat() != anchor:
        raise ValueError("invalid profit report anchor")
    return {
        "granularity": granularity,
        "scope": scope,
        "anchor": anchor,
    }


def _is_client_disconnect(exc: OSError) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)):
        return True
    return getattr(exc, "errno", None) in {32, 104} or getattr(exc, "winerror", None) in {10053, 10054}


def _data_source_action_result(source: str, state) -> dict[str, Any] | None:
    if source.strip().lower() not in {"kis-vts", "external-scan-kis"} or not state.system_log:
        return None
    latest = state.system_log[0]
    if latest.level == "warning" and latest.title == "장중 테스트 전환 차단":
        return {
            "actionPopup": {
                "title": "장중 테스트 불가",
                "message": redact_sensitive_text(latest.message),
                "tone": "warning",
            }
        }
    if latest.level == "warning" and latest.title == "테스트 데이터 전환 대기":
        return {
            "actionPopup": {
                "title": "전환 대기 필요",
                "message": redact_sensitive_text(latest.message),
                "tone": "warning",
            }
        }
    if latest.level == "error" and latest.title == "테스트 데이터 전환":
        return {
            "actionPopup": {
                "title": "장중 테스트 준비 필요",
                "message": redact_sensitive_text(latest.message),
                "tone": "warning",
            }
        }
    return None


def _kis_live_credential_save_succeeded(
    state: object,
    *,
    previous_latest_log: object | None,
) -> bool:
    system_log = getattr(state, "system_log", ())
    if not isinstance(system_log, tuple) or not system_log:
        return False
    latest = system_log[0]
    return (
        latest is not previous_latest_log
        and getattr(latest, "level", "") == "success"
    )


def _scheduler_credential_binding_pending(
    scheduler_control: object | None,
) -> bool:
    if scheduler_control is None:
        return True
    try:
        pending = getattr(scheduler_control, "credential_binding_pending")
    except Exception:
        return True
    return pending is not False


def _validate_service_credential_scope(
    scheduler_control: object | None,
) -> None:
    if _scheduler_credential_binding_pending(scheduler_control):
        raise PermissionError(
            "credential bootstrap is pending; this action is unavailable"
        )
    validate_current = getattr(
        scheduler_control,
        "validate_current_credential_scope",
        None,
    )
    if not callable(validate_current):
        raise PermissionError("credential scope validation is unavailable")
    validate_current()


def _kis_live_check_action_result(controller: DashboardController) -> dict[str, Any] | None:
    if not controller.state.system_log:
        return None
    latest = controller.state.system_log[0]
    level = str(latest.level).lower()
    success = level == "success"
    market_closed = level == "warning" and latest.title == "장중 아님"
    return {
        "actionPopup": {
            "title": (
                "장중 시간이 아닙니다"
                if market_closed
                else ("실전 계좌 조회 성공" if success else "실전 계좌 조회 실패")
            ),
            "message": redact_sensitive_text(latest.message),
            "tone": "paper" if success else "warning",
        }
    }


def _operator_live_readiness_messages(blockers: list[str]) -> list[str]:
    joined = "\n".join(blockers)
    messages: list[str] = []
    if (
        "allow_live_trading=true" in joined
        or "STOCKBOT_ALLOW_LIVE_TRADING" in joined
        or "live_trading_enabled=true" in joined
        or "STOCKBOT_LIVE_TRADING_ENABLED" in joined
        or "STOCKBOT_LIVE_TRADING_CONFIRM" in joined
        or "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION" in joined
    ):
        messages.append("실전 계좌 설정을 저장한 뒤 리얼모드에서 자동매매 시작을 눌러 계좌 확인과 주문 안전 게이트를 통과하세요.")
    if "manual" in joined.lower() or "reconciliation" in joined.lower():
        messages.append("미체결/보유 종목 상태를 다시 확인한 뒤 실전 준비도 점검을 실행하세요.")
    if "market" in joined.lower() or "장" in joined:
        messages.append("정규장 시간인지 확인한 뒤 다시 시작하세요.")
    if not messages and blockers:
        messages.append("실전 주문 안전 점검을 통과하지 못했습니다. 리얼모드에서 자동매매 시작을 다시 눌러 백엔드 게이트의 차단 사유를 확인하세요.")
    if not messages:
        messages.append("실전 주문 안전 점검을 통과하지 못했습니다.")
    return list(dict.fromkeys(messages))


def _live_readiness_action_result(readiness: dict[str, object]) -> dict[str, Any]:
    raw_blockers = readiness.get("blockers", [])
    if isinstance(raw_blockers, (str, bytes)) or not isinstance(raw_blockers, (list, tuple)):
        raw_blockers = [raw_blockers]
    blockers = [
        redact_sensitive_text(str(blocker))
        for blocker in raw_blockers
        if str(blocker).strip()
    ]
    ready = readiness.get("ready") is True and readiness.get("live_order_enabled") is False
    if ready:
        message = (
            "실전 주문 준비 조건이 충족되었습니다. 이 점검은 주문을 전송하지 않습니다. "
            "리얼모드에서 자동매매 시작을 누르면 같은 세션에서 계좌 확인과 주문 안전 게이트를 다시 확인합니다."
        )
    else:
        message = " ".join(_operator_live_readiness_messages(blockers))
    return {
        "actionResult": {
            "type": "live-readiness",
            "ready": ready,
            "blockers": blockers,
            "manualReconciliationCleared": bool(readiness.get("manual_reconciliation_cleared")),
            "scannerSnapshotRefreshed": bool(readiness.get("scanner_snapshot_refreshed")),
            "liveOrderEnabled": False,
            "note": redact_sensitive_text(readiness.get("note", "")),
        },
        "actionPopup": {
            "title": "실전 준비도 점검 완료" if ready else "실전 준비도 차단",
            "message": message,
            "tone": "paper" if ready else "warning",
        },
    }


def _manual_reconciliation_clear_action_result(readiness: dict[str, object]) -> dict[str, Any]:
    result = _live_readiness_action_result(readiness)
    action_result = dict(result.get("actionResult", {}))
    action_result["type"] = "manual-reconciliation-clear"
    result["actionResult"] = action_result
    if bool(readiness.get("manual_reconciliation_cleared")):
        result["actionPopup"] = {
            "title": "Manual reconciliation cleared",
            "message": "Manual live-account reconciliation was cleared locally. Run live readiness again before enabling real orders.",
            "tone": "warning",
        }
    return result


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string field: {key}")
    return value.strip()


def _required_env_credential_value(
    payload: dict[str, Any],
    key: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"missing required string field: {key}")
    if any(character in value for character in ("\0", "\r", "\n", '"', "'")):
        raise ValueError(
            "credential values must be single-line and must not contain quotes"
        )
    return value.strip()


def _payload_string(payload: dict[str, Any], key: str, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be string")
    return value.strip()


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _notice_view_model(controller: DashboardController) -> dict[str, Any]:
    state = controller.state
    real_mode = state.trading_mode == "real"
    data_source = _runtime_data_source_label(controller)
    live_order_enabled = real_mode and _live_runtime_orders_enabled(controller)
    live_order_ready = real_mode and _live_order_session_ready(controller)
    real_locked = real_mode and not live_order_enabled
    return {
        "title": (
            "REAL 주문 활성화"
            if live_order_enabled
            else "REAL 주문 준비"
            if live_order_ready
            else "REAL 주문 잠금"
            if real_mode
            else "PAPER 안전 모드"
        ),
        "description": (
            "현재 세션 실전 주문 게이트가 통과되었습니다. 장중 preflight와 리스크 게이트를 통과한 주문만 전송됩니다."
            if live_order_enabled
            else "현재 세션 실전 시작 의도와 준비 조건이 확인되었습니다. 자동매매 시작 시 장중 preflight와 리스크 게이트를 다시 확인합니다."
            if live_order_ready
            else "리얼모드는 현재 주문 전송이 잠겨 있습니다. 별도 안전 검증 전까지 실계좌 주문은 실행되지 않습니다."
            if real_mode
            else f"가상모드입니다. 데이터 출처: {data_source}. 주문은 로컬 paper broker에서만 가상 체결되며 KIS 계좌는 조회로만 사용합니다."
        ),
        "tone": "real" if live_order_enabled else "neutral" if live_order_ready else "danger" if real_locked else "paper",
        "locked": real_locked,
        "orderEnabled": live_order_enabled,
        "ready": live_order_ready,
    }


def _live_runtime_orders_enabled(controller: DashboardController) -> bool:
    try:
        if not bool(getattr(controller, "_runtime_running", False)):
            return False
        runtime = getattr(controller.services, "runtime", None)
        if str(getattr(runtime, "execution_mode", "") or "").strip().lower() != "live":
            return False
        if getattr(controller, "_live_runtime_readiness_ready", False) is not True:
            return False

        approval = controller.live_order_approval_status()
        required_approval_keys = (
            "allowSaved",
            "enabledSaved",
            "confirmationSaved",
            "accountConfirmationSaved",
            "sessionApproved",
            "riskLimitsOk",
            "newEntriesAllowed",
        )
        if any(approval.get(key) is not True for key in required_approval_keys):
            return False
        if any(value is not True for value in approval.values()):
            return False
        if bool(getattr(controller.current_custom_settings(), "kill_switch", True)):
            return False
        if bool(getattr(runtime, "_cycle_paused_for_live_pending_order", False)):
            return False
        if bool(getattr(runtime, "_cycle_new_entries_blocked_for_live_pending_order", False)):
            return False

        broker = getattr(runtime, "broker", None)
        if bool(getattr(broker, "_manual_reconciliation_blocker", "")):
            return False
        pending_store = getattr(broker, "pending_order_store", None)
        manual_store = getattr(broker, "manual_reconciliation_store", None)
        if getattr(pending_store, "is_durable", False) is not True:
            return False
        if getattr(manual_store, "is_durable", False) is not True:
            return False
        read_pending_orders = getattr(pending_store, "all", None)
        read_manual_blocker = getattr(manual_store, "blocker", None)
        if not callable(read_pending_orders) or not callable(read_manual_blocker):
            return False
        if tuple(read_pending_orders() or ()):
            return False
        return read_manual_blocker() is None
    except Exception:
        return False


def _live_order_session_ready(controller: DashboardController) -> bool:
    status = controller.live_order_approval_status()
    return bool(
        status.get("allowSaved")
        and status.get("enabledSaved")
        and status.get("confirmationSaved")
        and status.get("accountConfirmationSaved")
        and status.get("sessionApproved")
        and status.get("riskLimitsOk")
        and getattr(controller, "_live_runtime_readiness_ready", False) is True
    )


def _account_view_model(controller: DashboardController) -> dict[str, Any]:
    state = controller.state
    runtime_running = bool(getattr(controller, "_runtime_running", False))
    mode = state.trading_mode
    metrics = [
        (
            "상태",
            _runtime_status_label(
                mode,
                runtime_running,
                state.runtime_status,
                live_order_ready=mode == "real" and _live_order_session_ready(controller),
            ),
            True,
        ),
        ("계좌", "가상계좌" if mode == "virtual" else _safe_text(state.account.masked_account), False),
        ("현금" if mode == "virtual" else "예수금", _money_text(state.account.cash), True),
        ("평가금", _money_text(state.account.equity), True),
        ("보유 종목", _count_text(state.account.positions), True),
        ("매수 가능", _money_text(state.account.buying_power), True),
        ("조회 종목 현재가", _money_text(state.account.last_price), False),
        ("최근 갱신", _safe_text(state.account.updated_at), False),
    ]
    summary = [_metric_to_view_model(label, value) for label, value in state.account.runtime_metrics]
    return {
        "title": "계좌 상태",
        "metrics": [
            {"label": label, "value": value, "emphasis": emphasis}
            for label, value, emphasis in metrics
        ],
        "summary": summary,
    }


def _debug_view_model(
    controller: DashboardController,
    runtime_running: bool,
    trading_mode: str,
) -> dict[str, Any]:
    state = controller.state
    runtime = getattr(controller.services, "runtime", None)
    settings = controller.current_custom_settings()
    strategy = getattr(runtime, "strategy", None)
    risk_manager = getattr(runtime, "risk_manager", None)
    trade_log_diagnostics = _runtime_trade_log_diagnostics(runtime)
    full_trade_logs = trade_log_diagnostics["logs"]
    full_runtime_events = trade_log_diagnostics["events"]
    return {
        "diagnosticSchemaVersion": 2,
        "diagnosticCapabilities": [
            "pending-order-scope",
            "pending-order-age",
            "buying-power-planner",
            "rate-limiter-state",
            "physical-read-capacity",
            "planner-phase",
            "buying-power-probe",
            "market-trading-state",
        ],
        "stateRevision": _debug_int(getattr(controller, "state_revision", 0)),
        "strategyRevision": _debug_int(getattr(controller, "_strategy_revision", 0)),
        "cycle": {
            "id": _debug_int(getattr(runtime, "cycle_count", 0)),
            "running": bool(runtime_running),
            "busy": bool(getattr(controller, "_runtime_busy", False)),
            "status": _runtime_status_label(
                trading_mode,
                runtime_running,
                state.runtime_status,
                live_order_ready=trading_mode == "real" and _live_order_session_ready(controller),
            ),
            "lastUpdated": _safe_text(state.account.updated_at),
        },
        "runtime": {
            "dataSource": _runtime_data_source_label(controller),
            "dataSourceKind": _runtime_data_source_kind(controller),
            "dataModeLabel": _runtime_data_mode_label(controller),
            "dataModeDescription": _runtime_data_mode_description(controller),
            "safetySummary": _runtime_safety_summary(controller),
            "cleanupMode": bool(settings.kill_switch),
            "scanLimitPerCycle": _debug_int(getattr(runtime, "scan_limit_per_cycle", None)),
            "maxBarRequestsPerCycle": _debug_int(getattr(runtime, "max_bar_requests_per_cycle", None)),
            "symbolCount": len(getattr(runtime, "symbols", []) or []),
            "positionCount": len(state.active_positions),
            "tradeLogCount": len(state.trade_log),
            "fullTradeLogCount": len(full_trade_logs),
            "rawRuntimeEventCount": trade_log_diagnostics["raw_runtime_event_count"],
            "rawTradeEventCount": trade_log_diagnostics["raw_trade_event_count"],
            "rawSystemEventCount": trade_log_diagnostics["raw_system_event_count"],
            "fullTradeLogSkippedCount": trade_log_diagnostics["skipped_count"],
            "fullRuntimeEventSkippedCount": trade_log_diagnostics["event_skipped_count"],
            "runtimeEventStoreReadable": trade_log_diagnostics["event_store_readable"],
            "systemLogCount": len(state.system_log),
        },
        "fullTradeLogs": full_trade_logs,
        "fullRuntimeEvents": full_runtime_events,
        "analysis": _diagnostic_analysis_model(controller, runtime, full_runtime_events),
        "liveSafety": _live_safety_debug_model(controller, trading_mode),
        "runtimeInternals": _runtime_internals_debug_model(runtime),
        "effectivePolicy": {
            "strategyConfig": _debug_object(getattr(strategy, "config", None)),
            "riskConfig": _debug_object(getattr(risk_manager, "config", None)),
        },
        "performance": _debug_object(getattr(runtime, "performance_metrics", None)),
    }


def _diagnostic_analysis_model(
    controller: DashboardController,
    runtime: Any,
    runtime_events: list[dict[str, Any]],
) -> dict[str, Any]:
    rejected_reasons: Counter[str] = Counter()
    rejected_modes: Counter[str] = Counter()
    trade_results: Counter[str] = Counter()
    trade_sides: Counter[str] = Counter()
    system_categories: Counter[str] = Counter()
    recent_rejected_orders: list[dict[str, Any]] = []
    scanner_cycles: list[dict[str, Any]] = []
    pending_symbols: set[str] = set()
    pending_event_count = 0
    rate_limit_event_count = 0
    rate_limit_messages: list[str] = []
    rate_limit_stages: set[str] = set()
    pending_order_summary = _pending_live_order_debug_model(runtime)
    controller_system_event_count = 0
    controller_cycle_exception_count = 0

    for event in runtime_events:
        kind = str(event.get("kind") or "")
        message = str(event.get("message") or "")
        if kind == "trade":
            result = str(event.get("result") or "")
            reason = str(event.get("reason") or "")
            side = str(event.get("side") or "")
            mode = str(event.get("mode") or "")
            trade_results[result or "unknown"] += 1
            trade_sides[side or "unknown"] += 1
            if result == "rejected":
                rejected_reasons[reason or "unknown"] += 1
                rejected_modes[mode or "unknown"] += 1
                recent_rejected_orders.append(
                    {
                        "timestamp": event.get("timestamp") or "",
                        "symbol": event.get("symbol") or "",
                        "companyName": event.get("companyName") or "",
                        "side": side,
                        "quantity": _debug_int(event.get("quantity")),
                        "price": event.get("price"),
                        "reason": redact_sensitive_text(reason),
                        "mode": mode,
                    }
                )
            if _is_kis_rate_limit_message(reason):
                rate_limit_event_count += 1
                rate_limit_messages.append(reason)
                rate_limit_stages.add("trade_rejection")
        elif kind == "system":
            category = _system_event_category(message)
            system_categories[category] += 1
            if "scanner_diagnostic - external_scan_cycle:" in message:
                scanner_cycles.append(_parse_scanner_cycle_diagnostic(message))
            if "live_pending_orders_unresolved" in message:
                pending_event_count += 1
                pending_symbols.update(_symbols_from_pending_message(message))
            if _is_kis_rate_limit_message(message):
                rate_limit_event_count += 1
                rate_limit_messages.append(message)
                rate_limit_stages.add("runtime_system")

    latest_controller_runtime_start_index = next(
        (
            index
            for index, log in enumerate(controller.state.system_log)
            if str(getattr(log, "message", "") or "").startswith("자동 모의투자 루프 시작")
        ),
        None,
    )

    for log_index, log in enumerate(controller.state.system_log):
        message = str(getattr(log, "message", "") or "")
        if not message:
            continue
        controller_system_event_count += 1
        category = _system_event_category(message)
        system_categories[category] += 1
        if "cycle_exception" in message and (
            latest_controller_runtime_start_index is None
            or log_index < latest_controller_runtime_start_index
        ):
            controller_cycle_exception_count += 1
        if category == "live_pending_order":
            pending_event_count += 1
            pending_symbols.update(_symbols_from_pending_message(message))
        if _is_kis_rate_limit_message(message):
            rate_limit_event_count += 1
            rate_limit_messages.append(message)
            rate_limit_stages.add("controller_system")

    latest_scanner_cycles = scanner_cycles[-5:]
    cycle_paused_for_pending = bool(getattr(runtime, "_cycle_paused_for_live_pending_order", False))
    cycle_entries_blocked_for_pending = bool(
        getattr(runtime, "_cycle_new_entries_blocked_for_live_pending_order", False)
    )
    last_pending_sync = _debug_object(
        getattr(runtime, "_last_pending_live_order_sync_summary", None)
    ) or {}
    pending_sync_outcome = _safe_text(last_pending_sync.get("outcome"))
    pending_sync_hard_failure = bool(
        pending_sync_outcome in {"failed", "store_unavailable", "sync_unavailable"}
        or (
            pending_order_summary["storeAvailable"]
            and not pending_order_summary["storeReadable"]
        )
    )
    controller_pending_gate_active = (
        pending_event_count > 0
        and _safe_text(getattr(runtime, "data_source_kind", "") or _runtime_data_source_kind(controller)) != "live"
        and not bool(getattr(controller, "_live_runtime_readiness_ready", False))
    )
    blocked_by_pending_live_order_sync = bool(
        cycle_paused_for_pending
        or cycle_entries_blocked_for_pending
        or pending_order_summary["entryBlockingOrderCount"]
        or pending_sync_hard_failure
        or controller_pending_gate_active
    )
    limiter_snapshot = _rate_limiter_diagnostic_snapshot(runtime)
    limiter_rate_limit_active = _rate_limiter_rate_limit_active(limiter_snapshot)
    rate_limit_active = (
        limiter_rate_limit_active
        if limiter_rate_limit_active is not None
        else _latest_kis_rate_limit_is_active(runtime_events, controller.state.system_log)
    )
    market_trading_state = _runtime_internals_debug_model(runtime).get(
        "marketTradingState",
        {},
    )
    return {
        "schemaVersion": 1,
        "snapshot": {
            "tradingMode": controller.state.trading_mode,
            "runtimeRunning": bool(getattr(controller, "_runtime_running", False)),
            "runtimeBusy": bool(getattr(controller, "_runtime_busy", False)),
            "runtimeStatus": _safe_text(controller.state.runtime_status),
            "dataSourceKind": _safe_text(getattr(runtime, "data_source_kind", "") or _runtime_data_source_kind(controller)),
            "cycleId": _debug_int(getattr(runtime, "cycle_count", 0)),
            "positionCount": len(controller.state.active_positions),
            "visibleTradeLogCount": len(controller.state.trade_log),
            "visibleSystemLogCount": len(controller.state.system_log),
        },
        "reasonCounts": {
            "rejectedReasons": dict(sorted(rejected_reasons.items())),
            "rejectedModes": dict(sorted(rejected_modes.items())),
            "tradeResults": dict(sorted(trade_results.items())),
            "tradeSides": dict(sorted(trade_sides.items())),
            "systemCategories": dict(sorted(system_categories.items())),
        },
        "latestScannerCycles": latest_scanner_cycles,
        "liveOrderBlockers": {
            "blockedByPendingLiveOrderSync": blocked_by_pending_live_order_sync,
            "pendingEventCount": pending_event_count,
            "pendingSymbols": sorted(pending_symbols),
            "historicalPendingEventCount": pending_event_count,
            "historicalPendingSymbols": sorted(pending_symbols),
            "currentPendingOrderCount": pending_order_summary["currentOrderCount"],
            "currentPendingSymbols": sorted(
                row["symbol"] for row in pending_order_summary["orders"] if row["symbol"]
            ),
            "entryBlockingPendingOrderCount": pending_order_summary["entryBlockingOrderCount"],
            "isolatedPendingSellOrderCount": pending_order_summary["isolatedSellOrderCount"],
            "oldestPendingOrderAgeSeconds": pending_order_summary["oldestOrderAgeSeconds"],
            "cyclePausedForPendingOrder": cycle_paused_for_pending,
            "cycleNewEntriesBlockedForPendingOrder": cycle_entries_blocked_for_pending,
            "pendingStoreAvailable": pending_order_summary["storeAvailable"],
            "pendingStoreReadable": pending_order_summary["storeReadable"],
            "pendingStoreError": pending_order_summary["storeError"],
            "pendingSyncHardFailure": pending_sync_hard_failure,
            "pendingOrders": pending_order_summary["orders"],
            "lastPendingOrderSync": last_pending_sync,
        },
        "recentRejectedOrders": recent_rejected_orders[-20:],
        "marketTradingState": market_trading_state,
        "inferredRootCauses": _infer_diagnostic_root_causes(
            runtime_running=bool(getattr(controller, "_runtime_running", False)),
            runtime_busy=bool(getattr(controller, "_runtime_busy", False)),
            data_source_kind=_safe_text(getattr(runtime, "data_source_kind", "") or _runtime_data_source_kind(controller)),
            cycle_id=_debug_int(getattr(runtime, "cycle_count", 0)),
            runtime_event_count=len(runtime_events),
            controller_system_event_count=controller_system_event_count,
            controller_cycle_exception_count=controller_cycle_exception_count,
            rejected_reasons=rejected_reasons,
            scanner_cycles=latest_scanner_cycles,
            pending_event_count=pending_event_count,
            pending_symbols=sorted(pending_symbols),
            pending_orders=pending_order_summary["orders"],
            blocked_by_pending_live_order_sync=blocked_by_pending_live_order_sync,
            pending_sync_hard_failure=pending_sync_hard_failure,
            last_pending_sync=last_pending_sync,
            current_pending_order_count=pending_order_summary["currentOrderCount"],
            entry_blocking_pending_order_count=pending_order_summary["entryBlockingOrderCount"],
            isolated_pending_sell_order_count=pending_order_summary["isolatedSellOrderCount"],
            rate_limit_event_count=rate_limit_event_count,
            rate_limit_active=rate_limit_active,
            rate_limit_codes=_kis_rate_limit_codes(rate_limit_messages),
            rate_limit_stages=sorted(rate_limit_stages),
            market_trading_state_event_count=system_categories[
                "market_trading_state"
            ],
            market_trading_state_active=bool(
                market_trading_state.get("blockedSymbolsThisCycle")
            ),
            market_trading_state=market_trading_state,
            runtime_start_elapsed_seconds=_runtime_start_elapsed_seconds(runtime_events),
        ),
    }


def _pending_live_order_debug_model(runtime: Any) -> dict[str, Any]:
    broker = getattr(runtime, "broker", None)
    store = getattr(broker, "pending_order_store", None)
    if store is None:
        return {
            "storeAvailable": False,
            "storeReadable": False,
            "storeError": "",
            "orders": [],
            "currentOrderCount": 0,
            "entryBlockingOrderCount": 0,
            "isolatedSellOrderCount": 0,
            "oldestOrderAgeSeconds": None,
        }

    read_orders = getattr(store, "all", None)
    if not callable(read_orders):
        return {
            "storeAvailable": True,
            "storeReadable": False,
            "storeError": "pending order store has no all() reader",
            "orders": [],
            "currentOrderCount": 0,
            "entryBlockingOrderCount": 0,
            "isolatedSellOrderCount": 0,
            "oldestOrderAgeSeconds": None,
        }

    try:
        orders = tuple(read_orders() or ())
    except Exception as exc:
        return {
            "storeAvailable": True,
            "storeReadable": False,
            "storeError": redact_sensitive_text(exc.__class__.__name__),
            "orders": [],
            "currentOrderCount": 0,
            "entryBlockingOrderCount": 0,
            "isolatedSellOrderCount": 0,
            "oldestOrderAgeSeconds": None,
        }

    all_rows = [_pending_live_order_debug_row(order) for order in orders]
    rows = all_rows[-20:]
    order_ages = [row["ageSeconds"] for row in all_rows if row["ageSeconds"] is not None]
    return {
        "storeAvailable": True,
        "storeReadable": True,
        "storeError": "",
        "orders": rows,
        "currentOrderCount": len(all_rows),
        "entryBlockingOrderCount": sum(bool(row["entryBlocking"]) for row in all_rows),
        "isolatedSellOrderCount": sum(not bool(row["entryBlocking"]) for row in all_rows),
        "oldestOrderAgeSeconds": max(order_ages) if order_ages else None,
    }


def _pending_live_order_debug_row(order: Any) -> dict[str, Any]:
    submitted_at = getattr(order, "submitted_at", "")
    if isinstance(submitted_at, datetime):
        submitted_at_text = submitted_at.isoformat()
    else:
        submitted_at_text = _safe_text(submitted_at)
    submitted_datetime = _diagnostic_datetime(submitted_at_text)
    age_seconds = (
        None
        if submitted_datetime is None
        else max(0, int((datetime.now() - submitted_datetime).total_seconds()))
    )
    cancel_after_seconds = int(LIVE_PENDING_ORDER_CANCEL_AFTER.total_seconds())
    side = _safe_text(getattr(order, "side", "")).upper()
    entry_blocking = side != "SELL"
    reason = _safe_text(getattr(order, "reason", ""))
    return {
        "symbol": _safe_text(getattr(order, "symbol", "")),
        "side": side,
        "requestedQuantity": _debug_int(getattr(order, "requested_quantity", None)),
        "remainingQuantity": _debug_int(getattr(order, "remaining_quantity", None)),
        "submittedAt": submitted_at_text,
        "ageSeconds": age_seconds,
        "cancelAfterSeconds": cancel_after_seconds,
        "cancelEligibleByAge": age_seconds is not None and age_seconds >= cancel_after_seconds,
        "cancelRequested": "cancel_requested" in reason.lower(),
        "entryBlocking": entry_blocking,
        "blockingScope": "new_entries" if entry_blocking else "same_symbol",
        "estimatedPrice": _safe_text(getattr(order, "estimated_price", "")),
        "reason": reason,
        "orderNoMasked": _masked_order_reference(getattr(order, "order_no", "")),
        "orderOrgNoMasked": _masked_order_reference(getattr(order, "order_org_no", "")),
    }


def _masked_order_reference(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 4:
        return "*" * len(text)
    return f"{'*' * (len(text) - 4)}{text[-4:]}"


def _live_safety_debug_model(controller: DashboardController, trading_mode: str) -> dict[str, Any]:
    credentials = controller.kis_live_credential_status()
    credential_values = (
        credentials.get("appKeySaved"),
        credentials.get("appSecretSaved"),
        credentials.get("accountNoSaved"),
        credentials.get("productCodeSaved"),
    )
    saved_count = sum(1 for value in credential_values if value)
    approval = controller.live_order_approval_status()
    return {
        "tradingMode": trading_mode,
        "credentialFields": {
            "requiredCount": len(credential_values),
            "savedCount": saved_count,
            "complete": saved_count == len(credential_values),
        },
        "orderGate": {
            "allowSaved": bool(approval.get("allowSaved")),
            "enabledSaved": bool(approval.get("enabledSaved")),
            "confirmationPhraseSaved": bool(approval.get("confirmationSaved")),
            "scopeConfirmationSaved": bool(approval.get("accountConfirmationSaved")),
            "sessionApproved": bool(approval.get("sessionApproved")),
            "riskLimitsOk": bool(approval.get("riskLimitsOk")),
            "newEntriesAllowed": bool(approval.get("newEntriesAllowed")),
            "sessionReady": trading_mode == "real" and _live_order_session_ready(controller),
        },
        "runtimeAdmission": {
            "readinessPassedInProcess": bool(getattr(controller, "_live_runtime_readiness_ready", False)),
            "readOnlyProbeVerified": bool(getattr(controller, "_live_read_only_verified_fingerprint", None)),
            "pendingSettings": bool(getattr(controller, "_pending_runtime_settings", None)),
        },
    }


def _runtime_internals_debug_model(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {}
    cycle_start_buying_power = getattr(runtime, "_cycle_start_buying_power", None)
    cycle_entry_spent = getattr(runtime, "_cycle_entry_spent", None)
    remaining_buying_power = None
    try:
        if cycle_start_buying_power is not None:
            remaining_buying_power = max(
                Decimal("0"),
                Decimal(str(cycle_start_buying_power))
                - Decimal(str(cycle_entry_spent or 0)),
            )
    except Exception:
        remaining_buying_power = None

    limiter_snapshot = _rate_limiter_diagnostic_snapshot(runtime) or {}
    risk_manager = getattr(runtime, "risk_manager", None)
    risk_config = getattr(risk_manager, "config", None)
    raw_live_planner_phase = getattr(runtime, "_cycle_live_planner_phase", None)
    live_planner_phase = (
        "not_started"
        if raw_live_planner_phase is None
        else _safe_text(raw_live_planner_phase) or "not_started"
    )
    last_planning_buying_power = getattr(runtime, "_last_live_planning_buying_power", None)
    if last_planning_buying_power is None:
        planning_buying_power_state = "unknown"
    else:
        try:
            planning_buying_power_state = (
                "zero" if Decimal(str(last_planning_buying_power)) <= 0 else "positive"
            )
        except Exception:
            planning_buying_power_state = "unknown"
    retry_after_reader = getattr(
        runtime,
        "_live_exact_zero_buying_power_retry_after_seconds",
        None,
    )
    try:
        exact_zero_retry_after = (
            max(0.0, float(retry_after_reader()))
            if callable(retry_after_reader)
            else 0.0
        )
    except Exception:
        exact_zero_retry_after = 0.0
    observed_at = getattr(runtime, "_last_live_planning_buying_power_at", None)
    observed_at_text = observed_at.isoformat() if isinstance(observed_at, datetime) else ""
    market_blocked_at = getattr(runtime, "_last_market_trading_block_at", None)
    market_blocked_at_text = (
        market_blocked_at.isoformat()
        if isinstance(market_blocked_at, datetime)
        else ""
    )
    market_block_age_seconds = None
    if isinstance(market_blocked_at, datetime):
        try:
            now = (
                datetime.now(tz=market_blocked_at.tzinfo)
                if market_blocked_at.tzinfo is not None
                else datetime.now()
            )
            market_block_age_seconds = max(
                0.0,
                round((now - market_blocked_at).total_seconds(), 1),
            )
        except Exception:
            market_block_age_seconds = None
    blocked_symbols = sorted(
        _safe_text(symbol)
        for symbol in (getattr(runtime, "_cycle_blocked_symbols", ()) or ())
        if _safe_text(symbol)
    )
    last_market_block_reason = _safe_text(
        getattr(runtime, "_last_market_trading_block_reason", "")
    )
    raw_block_reasons = getattr(
        runtime,
        "_cycle_symbol_trading_block_reasons",
        {},
    )
    blocked_reasons = {
        symbol: _safe_text(
            raw_block_reasons.get(symbol, last_market_block_reason)
            if isinstance(raw_block_reasons, dict)
            else last_market_block_reason
        )
        for symbol in blocked_symbols
    }
    distinct_block_reasons = {
        reason for reason in blocked_reasons.values() if reason
    }

    return {
        "maxFinalQuoteRequestsPerCycle": _debug_int(getattr(runtime, "max_final_quote_requests_per_cycle", None)),
        "finalQuoteRequestsThisCycle": _debug_int(getattr(runtime, "_final_quote_requests_this_cycle", None)),
        "cyclePausedForLivePendingOrder": bool(getattr(runtime, "_cycle_paused_for_live_pending_order", False)),
        "cycleNewEntriesBlockedForLivePendingOrder": bool(
            getattr(runtime, "_cycle_new_entries_blocked_for_live_pending_order", False)
        ),
        "cycleNewEntriesBlockedForEntryCount": bool(
            getattr(runtime, "_cycle_new_entries_blocked_for_live_entry_count", False)
        ),
        "entryCountSyncReady": bool(
            getattr(runtime, "_last_live_entry_count_sync_ready", True)
        ),
        "cycleStartBuyingPower": _debug_object(cycle_start_buying_power),
        "cycleEntrySpent": _debug_object(cycle_entry_spent),
        "remainingBuyingPower": _debug_object(remaining_buying_power),
        "cycleEntrySymbolCount": len(getattr(runtime, "_cycle_entry_symbols", ()) or ()),
        "cycleExitSymbolCount": len(getattr(runtime, "_cycle_exit_symbols", ()) or ()),
        "entrySlotTarget": _debug_int(getattr(runtime, "_cycle_entry_slot_target", None)),
        "entrySlotCapacity": _debug_int(getattr(runtime, "_cycle_entry_slot_capacity", None)),
        "entrySizingSlots": _debug_int(getattr(runtime, "_cycle_entry_sizing_slots", None)),
        "livePlannerPhase": live_planner_phase,
        "nextLivePlannerPhase": _safe_text(
            getattr(runtime, "_next_live_planner_phase", "entry_reserved")
        ),
        "livePlannerPhaseEligible": live_planner_phase != "not_started",
        "liveBuyingPowerProbe": {
            "lastExactState": planning_buying_power_state,
            "observedAt": observed_at_text,
            "cooldownActive": exact_zero_retry_after > 0,
            "retryAfterSeconds": round(exact_zero_retry_after, 1),
        },
        "orderFailureState": {
            "consecutiveFailures": _debug_int(
                getattr(risk_manager, "_consecutive_order_failures", 0)
            ),
            "failureLimit": _debug_int(
                getattr(risk_config, "max_consecutive_order_failures", None)
            ),
            "lastClass": _safe_text(getattr(runtime, "_last_order_failure_class", "none")),
            "lastReason": redact_sensitive_text(
                getattr(runtime, "_last_order_failure_reason", "")
            ),
        },
        "marketTradingState": {
            "currentState": (
                "MIXED"
                if len(distinct_block_reasons) > 1
                else "UNKNOWN"
                if distinct_block_reasons == {"trading_state_unknown"}
                else "SECURITY_HALT"
                if blocked_symbols
                else "OPEN_OR_UNCHECKED"
            ),
            "scope": "SYMBOL" if blocked_symbols else "NONE",
            "blockedSymbolsThisCycle": blocked_symbols,
            "blockedReasonsThisCycle": blocked_reasons,
            "lastReason": last_market_block_reason,
            "lastMarket": _safe_text(
                getattr(runtime, "_last_market_trading_block_market", "")
            ),
            "lastSymbol": _safe_text(
                getattr(runtime, "_last_market_trading_block_symbol", "")
            ),
            "source": _safe_text(
                getattr(runtime, "_last_market_trading_block_source", "")
            ),
            "observedAt": market_blocked_at_text,
            "ageSeconds": market_block_age_seconds,
            "recoveryPhase": (
                "AWAITING_FRESH_STATE" if blocked_symbols else "IDLE"
            ),
            "totalBlocks": _debug_int(
                getattr(runtime, "_market_trading_block_count", 0)
            ),
        },
        "lastPendingOrderSync": _debug_object(
            getattr(runtime, "_last_pending_live_order_sync_summary", None)
        ),
        "pendingOrderBatchActive": bool(
            getattr(getattr(runtime, "broker", None), "_pending_order_batch_active", False)
        ),
        "rateLimiter": limiter_snapshot,
        "cycleAccountSnapshotReadable": getattr(runtime, "_cycle_account_snapshot", None) is not None,
        "latestCycleSnapshotReadable": getattr(runtime, "_latest_cycle_account_snapshot", None) is not None,
    }


def _rate_limiter_diagnostic_snapshot(runtime: Any) -> dict[str, Any] | None:
    limiter = getattr(runtime, "rate_limiter", None)
    limiter_diagnostics = getattr(limiter, "diagnostic_snapshot", None)
    if not callable(limiter_diagnostics):
        return None
    try:
        return _debug_object(limiter_diagnostics("kis_live_mutation"))
    except Exception as exc:
        return {"readable": False, "errorType": exc.__class__.__name__}


def _rate_limiter_rate_limit_active(snapshot: dict[str, Any] | None) -> bool | None:
    if not snapshot or snapshot.get("readable") is False:
        return None
    if bool(snapshot.get("allowed")):
        return False
    reason = _safe_text(snapshot.get("reason")).lower()
    try:
        retry_after = max(0.0, float(snapshot.get("retryAfterSeconds") or 0.0))
    except (TypeError, ValueError):
        retry_after = 0.0
    if reason == "api_backoff" and retry_after > 0:
        return True
    return False


def _infer_diagnostic_root_causes(
    *,
    runtime_running: bool,
    runtime_busy: bool,
    data_source_kind: str,
    cycle_id: int,
    runtime_event_count: int,
    controller_system_event_count: int,
    controller_cycle_exception_count: int,
    rejected_reasons: Counter[str],
    scanner_cycles: list[dict[str, Any]],
    pending_event_count: int,
    pending_symbols: list[str],
    pending_orders: list[dict[str, Any]],
    blocked_by_pending_live_order_sync: bool,
    pending_sync_hard_failure: bool,
    last_pending_sync: dict[str, Any],
    current_pending_order_count: int,
    entry_blocking_pending_order_count: int,
    isolated_pending_sell_order_count: int,
    rate_limit_event_count: int,
    rate_limit_active: bool,
    rate_limit_codes: list[str],
    rate_limit_stages: list[str],
    market_trading_state_event_count: int,
    market_trading_state_active: bool,
    market_trading_state: dict[str, Any],
    runtime_start_elapsed_seconds: float | None,
) -> list[dict[str, Any]]:
    causes: list[dict[str, Any]] = []
    first_cycle_grace_elapsed = (
        runtime_start_elapsed_seconds is None
        or runtime_start_elapsed_seconds >= LIVE_FIRST_CYCLE_GRACE_SECONDS
    )
    if (
        runtime_running
        and not runtime_busy
        and data_source_kind == "live"
        and cycle_id == 0
        and (runtime_event_count > 0 or controller_cycle_exception_count > 0)
        and not scanner_cycles
        and (controller_cycle_exception_count > 0 or first_cycle_grace_elapsed)
    ):
        causes.append(
            {
                "code": "runtime_cycle_not_completed_after_start",
                "severity": "high",
                "evidence": {
                    "dataSourceKind": data_source_kind,
                    "cycleId": cycle_id,
                    "runtimeEventCount": runtime_event_count,
                    "controllerSystemEventCount": controller_system_event_count,
                    "controllerCycleExceptionCount": controller_cycle_exception_count,
                    "scannerCycleCount": 0,
                    "runtimeStartElapsedSeconds": runtime_start_elapsed_seconds,
                    "firstCycleGraceSeconds": LIVE_FIRST_CYCLE_GRACE_SECONDS,
                },
                "nextCheck": "실전 start 이후 첫 runtime cycle이 완료되지 않았습니다. start 액션, Electron scheduler, backend cycle action 경로를 확인하세요.",
            }
        )
    live_pending_rejections = sum(
        count
        for reason, count in rejected_reasons.items()
        if reason.startswith("live_order_pending")
        or reason.startswith("live_pending_orders_unresolved")
        or reason.startswith("live_order_reconciliation_failed")
    )
    if pending_sync_hard_failure:
        causes.append(
            {
                "code": "live_pending_order_state_unavailable",
                "severity": "high",
                "evidence": {
                    "blockedByPendingLiveOrderSync": blocked_by_pending_live_order_sync,
                    "lastPendingOrderSync": last_pending_sync,
                    "currentPendingOrderCount": current_pending_order_count,
                },
                "nextCheck": "로컬 미체결 주문 상태 저장소와 마지막 KIS 동기화 결과를 확인하세요.",
            }
        )
    elif blocked_by_pending_live_order_sync:
        causes.append(
            {
                "code": "live_pending_orders_blocking_entries",
                "severity": "high",
                "evidence": {
                    "blockedByPendingLiveOrderSync": blocked_by_pending_live_order_sync,
                    "pendingEventCount": pending_event_count,
                    "pendingSymbols": pending_symbols,
                    "pendingOrders": pending_orders,
                    "currentPendingOrderCount": current_pending_order_count,
                    "entryBlockingPendingOrderCount": entry_blocking_pending_order_count,
                    "livePendingRejectedTrades": live_pending_rejections,
                },
                "nextCheck": "KIS 미체결/체결 상태와 local pending live order store를 먼저 동기화하세요.",
            }
        )
    elif pending_event_count or live_pending_rejections:
        causes.append(
            {
                "code": "historical_live_pending_order_activity",
                "severity": "low",
                "evidence": {
                    "pendingEventCount": pending_event_count,
                    "pendingSymbols": pending_symbols,
                    "currentPendingOrderCount": current_pending_order_count,
                    "livePendingRejectedTrades": live_pending_rejections,
                },
                "nextCheck": "과거 미체결 이벤트입니다. 현재 store와 마지막 동기화 결과를 기준으로 판단하세요.",
            }
        )
    if isolated_pending_sell_order_count:
        causes.append(
            {
                "code": "live_pending_sell_isolated",
                "severity": "medium",
                "evidence": {
                    "isolatedPendingSellOrderCount": isolated_pending_sell_order_count,
                    "currentPendingOrderCount": current_pending_order_count,
                },
                "nextCheck": "해당 종목의 SELL만 격리됩니다. 다른 보유 종목의 청산과 신규 후보 처리는 계속되어야 합니다.",
            }
        )
    unknown_entry_count_rejections = rejected_reasons.get("live_entry_count_unknown", 0)
    if unknown_entry_count_rejections:
        causes.append(
            {
                "code": "live_entry_count_unknown",
                "severity": "high",
                "evidence": {"rejectedTrades": unknown_entry_count_rejections},
                "nextCheck": "managed entry counts에는 authoritative KIS same-day reconciliation이 필요합니다. "
                "KIS 당일 주문/체결 내역을 account-scoped managed ledger와 재동기화한 뒤 live BUY를 다시 확인하세요.",
            }
        )
    if rejected_reasons.get("order_failure_limit_reached", 0):
        causes.append(
            {
                "code": "order_failure_limit_reached",
                "severity": "high",
                "evidence": {"rejectedTrades": rejected_reasons["order_failure_limit_reached"]},
                "nextCheck": "직전 rejected reason들을 확인해 실패 잠금이 실제 broker reject인지 pending pause인지 구분하세요.",
            }
        )
    if market_trading_state_event_count and market_trading_state_active:
        causes.append(
            {
                "code": "market_trading_temporarily_deferred",
                "severity": "low",
                "evidence": {
                    "events": market_trading_state_event_count,
                    "state": market_trading_state,
                },
                "nextCheck": "시장·종목 거래 상태에 따른 정상 보류입니다. 다음 cycle의 새 KIS 상태와 재개 여부를 확인하세요.",
            }
        )
    for cycle in scanner_cycles:
        if int(cycle.get("entryCandidates") or 0) > 0 and int(cycle.get("entryFills") or 0) == 0:
            causes.append(
                {
                    "code": "scanner_candidates_without_fills",
                    "severity": "medium",
                    "evidence": {
                        "entryCandidates": cycle.get("entryCandidates"),
                        "entryDeferred": cycle.get("entryDeferred"),
                        "openTargetSlots": cycle.get("openTargetSlots"),
                        "finalQuotes": cycle.get("finalQuotes"),
                    },
                    "nextCheck": "후보는 있었으므로 risk/order blocker, final quote cap, pending order 상태를 우선 보세요.",
                }
            )
            break
    latest_prescan = scanner_cycles[-1].get("prescanRejections", {}) if scanner_cycles else {}
    if isinstance(latest_prescan, dict) and latest_prescan.get("entry_unaffordable", 0):
        causes.append(
            {
                "code": "entry_unaffordable_candidates",
                "severity": "medium",
                "evidence": {"entryUnaffordable": latest_prescan.get("entry_unaffordable")},
                "nextCheck": "현재 현금/슬롯당 예산으로 1주 이상 가능한 후보가 충분한지 확인하세요.",
            }
        )
    if rate_limit_event_count and rate_limit_active:
        causes.append(
            {
                "code": "kis_rate_limit",
                "severity": "medium",
                "evidence": {
                    "events": rate_limit_event_count,
                    "kisCodes": rate_limit_codes,
                    "stages": rate_limit_stages,
                    "type": "per_second_rate_limit",
                },
                "nextCheck": "KIS ledger/quote 호출이 cycle마다 과도하게 반복되는지 확인하세요.",
            }
        )
    return causes


def _runtime_start_elapsed_seconds(runtime_events: list[dict[str, Any]]) -> float | None:
    for event in reversed(runtime_events):
        if str(event.get("kind") or "") != "system":
            continue
        if not str(event.get("message") or "").startswith("자동 모의투자 루프 시작"):
            continue
        try:
            started_at = datetime.fromisoformat(str(event.get("timestamp") or ""))
        except ValueError:
            return None
        now = datetime.now(tz=started_at.tzinfo) if started_at.tzinfo is not None else datetime.now()
        return max(0.0, round((now - started_at).total_seconds(), 3))
    return None


def _is_kis_rate_limit_message(message: str) -> bool:
    text = message.lower()
    return any(
        marker in text
        for marker in (
            "egw00201",
            "egw00215",
            "초당 거래건수",
            "kis 초당 요청 제한",
            "kis local rate limit",
            "rate_limit_skip",
            "api_backoff",
        )
    )


def _kis_rate_limit_codes(messages: list[str]) -> list[str]:
    codes = {
        code
        for code in ("EGW00201", "EGW00215")
        if any(code.lower() in message.lower() for message in messages)
    }
    return sorted(codes)


def _latest_kis_rate_limit_is_active(runtime_events: list[dict[str, Any]], system_log: Any) -> bool:
    controller_logs = list(system_log or ())
    controller_datetimes = _controller_log_datetimes(controller_logs)
    controller_times = [value for value in controller_datetimes if value is not None]
    oldest_controller_time = min(controller_times) if controller_times else None
    rate_times: list[datetime] = []
    success_times: list[datetime] = []

    for event in runtime_events:
        detail = " ".join(
            (
                str(event.get("message") or ""),
                str(event.get("reason") or ""),
            )
        )
        if not _is_kis_rate_limit_message(detail):
            continue
        event_time = _diagnostic_datetime(event.get("timestamp"))
        if event_time is None or oldest_controller_time is None or event_time >= oldest_controller_time:
            if event_time is not None:
                rate_times.append(event_time)

    latest_rate_index: int | None = None
    latest_success_index: int | None = None
    for index, (log, event_time) in enumerate(zip(controller_logs, controller_datetimes)):
        message = str(getattr(log, "message", "") or "")
        if _is_kis_rate_limit_message(message):
            if latest_rate_index is None:
                latest_rate_index = index
            if event_time is not None:
                rate_times.append(event_time)
        if (
            str(getattr(log, "level", "") or "") == "success"
            and str(getattr(log, "title", "") or "") == "KIS 실전 조회 확인"
        ):
            if latest_success_index is None:
                latest_success_index = index
            if event_time is not None:
                success_times.append(event_time)

    if not rate_times:
        return False
    if not success_times:
        return True
    latest_rate_time = max(rate_times)
    latest_success_time = max(success_times)
    if latest_rate_time != latest_success_time:
        return latest_rate_time > latest_success_time
    if latest_rate_index is not None and latest_success_index is not None:
        return latest_rate_index < latest_success_index
    return False


def _diagnostic_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed.replace(microsecond=0)


def _controller_log_datetimes(controller_logs: list[Any]) -> list[datetime | None]:
    now = datetime.now()
    current_date = now.date()
    previous_seconds: int | None = None
    result: list[datetime | None] = []
    for log in controller_logs:
        text = str(getattr(log, "timestamp", "") or "").strip()
        try:
            parsed_time = datetime.strptime(text, "%H:%M:%S").time()
        except ValueError:
            result.append(None)
            continue
        seconds = parsed_time.hour * 3600 + parsed_time.minute * 60 + parsed_time.second
        if previous_seconds is None:
            now_seconds = now.hour * 3600 + now.minute * 60 + now.second
            if seconds > now_seconds:
                current_date -= timedelta(days=1)
        elif seconds > previous_seconds:
            current_date -= timedelta(days=1)
        result.append(datetime.combine(current_date, parsed_time))
        previous_seconds = seconds
    return result


def _system_event_category(message: str) -> str:
    text = message.lower()
    if "market_trading_deferred" in text:
        return "market_trading_state"
    if "scanner_diagnostic" in text:
        return "scanner_diagnostic"
    if (
        "live_pending_orders_unresolved" in text
        or "live pending orders unresolved" in text
        or "pending live order" in text
    ):
        return "live_pending_order"
    if (
        "egw00201" in text
        or "egw00215" in text
        or "rate limit" in text
        or "rate_limit_skip" in text
        or "api_backoff" in text
    ):
        return "kis_rate_limit"
    if "egw00123" in text or "token" in text:
        return "kis_token"
    if "error" in text or "failed" in text or "오류" in text:
        return "error"
    return "other"


def _parse_scanner_cycle_diagnostic(message: str) -> dict[str, Any]:
    _, _, payload = message.partition("external_scan_cycle:")
    fields = _parse_key_value_diagnostic_payload(payload)
    final_quote_text = str(fields.get("final_quotes") or "")
    final_quote_requests, final_quote_cap = _parse_final_quote_counts(final_quote_text)
    physical_read_text = str(fields.get("physical_reads") or "")
    physical_read_requests, physical_read_cap = _parse_final_quote_counts(physical_read_text)
    return {
        "raw": redact_sensitive_text(message),
        "candidates": _debug_int(fields.get("candidates")),
        "selected": _debug_int(fields.get("selected")),
        "processed": _debug_int(fields.get("processed")),
        "historyReadyCandidates": _debug_int(fields.get("history_ready_candidates")),
        "historyFallbackCandidates": _debug_int(fields.get("history_fallback_candidates")),
        "historyFailures": _parse_reason_counts(fields.get("history_failures")),
        "sparseCandidates": _debug_int(fields.get("sparse_candidates")),
        "confirmationCandidates": _debug_int(fields.get("confirmation_candidates")),
        "confirmationReasons": _parse_reason_counts(fields.get("confirmation_reasons")),
        "finalQuotes": final_quote_text,
        "finalQuoteRequests": final_quote_requests,
        "finalQuoteCap": final_quote_cap,
        "physicalReads": physical_read_text,
        "physicalReadRequests": physical_read_requests,
        "physicalReadCap": physical_read_cap,
        "confirmed": _debug_int(fields.get("confirmed")),
        "holds": _debug_int(fields.get("holds")),
        "entryCandidates": _debug_int(fields.get("entry_candidates")),
        "entryFills": _debug_int(fields.get("entry_fills")),
        "entryDeferred": _debug_int(fields.get("entry_deferred")),
        "entryCapacityStop": _debug_int(fields.get("entry_capacity_stop")),
        "entryBlockedByPending": bool(_debug_int(fields.get("entry_blocked_by_pending")) or 0),
        "entryBlockedByEntryCount": bool(
            _debug_int(fields.get("entry_blocked_by_entry_count")) or 0
        ),
        "plannerPhase": _safe_text(fields.get("planner_phase")),
        "entrySlotCapacity": _debug_int(fields.get("entry_slot_capacity")),
        "openTargetSlots": _debug_int(fields.get("open_target_slots")),
        "exactZeroBuyingPowerCooldownActive": bool(
            _debug_int(fields.get("exact_zero_cooldown_active")) or 0
        ),
        "exactZeroBuyingPowerRetryAfterSeconds": _debug_float(
            fields.get("exact_zero_cooldown_retry_after_seconds")
        ),
        "prescanRejections": _parse_reason_counts(fields.get("prescan_rejections")),
        "holdReasons": _parse_reason_counts(fields.get("hold_reasons")),
    }


def _parse_key_value_diagnostic_payload(payload: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for match in re.finditer(r"([A-Za-z_]+)=([^=]*?)(?=,\s*[A-Za-z_]+=|$)", payload.strip()):
        result[match.group(1)] = match.group(2).strip().rstrip(",")
    return result


def _parse_final_quote_counts(value: str) -> tuple[int | None, int | str | None]:
    request_text, separator, cap_text = value.partition("/")
    if not separator:
        return _debug_int(request_text), None
    cap_text = cap_text.strip()
    cap: int | str | None = "unlimited" if cap_text == "unlimited" else _debug_int(cap_text)
    return _debug_int(request_text), cap


def _parse_reason_counts(value: object) -> dict[str, int]:
    text = str(value or "").strip()
    if not text or text in {"none", "-"}:
        return {}
    counts: dict[str, int] = {}
    for item in text.split(","):
        reason, separator, count = item.strip().partition(":")
        if not separator or not reason:
            continue
        counts[reason] = _debug_int(count) or 0
    return counts


def _symbols_from_pending_message(message: str) -> list[str]:
    match = re.search(r"symbols=([0-9A-Za-z_, -]+)", message)
    if not match:
        return []
    return [
        symbol.strip()
        for symbol in match.group(1).split(",")
        if symbol.strip()
    ]


def _runtime_trade_log_diagnostics(runtime: Any) -> dict[str, Any]:
    if runtime is None:
        return {
            "logs": [],
            "events": [],
            "raw_runtime_event_count": 0,
            "raw_trade_event_count": 0,
            "raw_system_event_count": 0,
            "skipped_count": 0,
            "event_skipped_count": 0,
            "event_store_readable": True,
        }

    events_source = getattr(runtime, "events", ())
    if events_source is None:
        events_source = ()
    try:
        events = list(events_source)
    except TypeError:
        return {
            "logs": [],
            "events": [],
            "raw_runtime_event_count": 0,
            "raw_trade_event_count": 0,
            "raw_system_event_count": 0,
            "skipped_count": 0,
            "event_skipped_count": 0,
            "event_store_readable": False,
        }

    trade_logs: list[dict[str, Any]] = []
    runtime_events: list[dict[str, Any]] = []
    raw_trade_event_count = 0
    raw_system_event_count = 0
    skipped_count = 0
    event_skipped_count = 0
    for event in events:
        kind = getattr(event, "kind", "")
        if kind == "trade":
            raw_trade_event_count += 1
        elif kind == "system":
            raw_system_event_count += 1

        try:
            runtime_events.append(_runtime_event_to_debug_model(event))
        except Exception:
            event_skipped_count += 1

        if kind == "trade":
            try:
                entry = build_trade_log_entry(event)
                trade_logs.append(_trade_log_to_view_model(entry))
            except Exception:
                skipped_count += 1
    return {
        "logs": trade_logs,
        "events": runtime_events,
        "raw_runtime_event_count": len(events),
        "raw_trade_event_count": raw_trade_event_count,
        "raw_system_event_count": raw_system_event_count,
        "skipped_count": skipped_count,
        "event_skipped_count": event_skipped_count,
        "event_store_readable": True,
    }


def _runtime_event_to_debug_model(event: Any) -> dict[str, Any]:
    timestamp = getattr(event, "timestamp")
    if isinstance(timestamp, datetime):
        timestamp_text = timestamp.isoformat()
    else:
        timestamp_text = _safe_text(timestamp)
    return {
        "kind": _safe_text(getattr(event, "kind", "")),
        "timestamp": timestamp_text,
        "message": _safe_text(getattr(event, "message", "")),
        "symbol": _safe_text(getattr(event, "symbol", "")),
        "companyName": _safe_text(getattr(event, "company_name", "")),
        "side": _safe_text(getattr(event, "side", "")),
        "quantity": _debug_int(getattr(event, "quantity", 0)),
        "price": _decimal_to_number(getattr(event, "price", Decimal("0"))),
        "reason": _safe_text(getattr(event, "reason", "")),
        "result": _safe_text(getattr(event, "result", "")),
        "mode": _safe_text(getattr(event, "mode", "")),
        "realizedPnl": _decimal_to_number(getattr(event, "realized_pnl", Decimal("0"))),
    }

def _debug_object(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if is_dataclass(value):
        return {
            _debug_key(name): _debug_object(getattr(value, name))
            for name in getattr(value, "__dataclass_fields__", {})
            if not str(name).startswith("_")
        }
    if isinstance(value, dict):
        return {_debug_key(key): _debug_object(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_debug_object(item) for item in value]
    return _safe_text(value)


def _debug_key(key: object) -> str:
    text = str(key)
    if re.search(r"(secret|token|authorization|app[_-]?key|api[_-]?key|account)", text, re.IGNORECASE):
        return "[REDACTED_KEY]"
    return text


def _debug_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _debug_float(value: object) -> float | None:
    try:
        parsed = Decimal(str(value))
    except Exception:
        return None
    if not parsed.is_finite():
        return None
    return float(parsed)


def _position_row_to_view_model(row: Any) -> dict[str, Any]:
    pnl = _money_text(row.unrealized_pnl)
    return {
        "symbol": _safe_text(row.symbol),
        "companyName": _safe_text(row.company_name),
        "label": _safe_text(row.label),
        "side": _normalize_side(row.side_label),
        "quantity": row.quantity,
        "avgPrice": _money_text(row.avg_price),
        "lastPrice": _money_text(row.last_price),
        "unrealizedPnl": pnl,
        "pnlTone": "negative" if pnl.strip().startswith("-") else "positive" if pnl != "0원" else "neutral",
    }


def _position_detail_to_view_model(detail: Any) -> dict[str, Any] | None:
    if not getattr(detail, "symbol", ""):
        return None
    return {
        "symbol": _safe_text(detail.symbol),
        "companyName": _safe_text(detail.company_name),
        "label": _safe_text(detail.label),
        "side": _normalize_side(detail.side_label),
        "quantity": detail.quantity,
        "summary": _safe_text(detail.summary),
        "avgPrice": _money_text(detail.avg_price),
        "lastPrice": _money_text(detail.last_price),
        "unrealizedPnl": _money_text(detail.unrealized_pnl),
        "pricePoints": [
            {"time": _datetime_to_label(timestamp), "value": _decimal_to_float(price)}
            for timestamp, price in detail.price_points
        ],
        "referenceLines": [
            {"label": _reference_line_label(index), "value": _decimal_to_float(value)}
            for index, (_label, value) in enumerate(detail.reference_lines)
        ],
        "legend": [
            "실선: 최근 모의 가격 흐름",
            "점선: 평균 진입가, 익절선, 손절선 기준",
            "진입/현재: paper 포지션 가격 기준",
        ],
    }


def _trade_log_to_view_model(entry: Any) -> dict[str, Any]:
    title = _safe_text(entry.title)
    detail = _money_text(entry.detail)
    lowered = f"{title} {detail}".lower()
    if "rejected" in lowered or "거절" in lowered:
        level = "rejected"
    elif "sell" in lowered or "매도" in lowered:
        level = "sell"
    elif "short" in lowered or "숏" in lowered:
        level = "short"
    else:
        level = "buy"
    price = _trade_decimal(getattr(entry, "price", Decimal("0")))
    realized_pnl = _trade_decimal(getattr(entry, "realized_pnl", Decimal("0")))
    return {
        "title": title,
        "detail": detail,
        "level": level,
        "timestamp": _trade_timestamp(getattr(entry, "timestamp", "")),
        "symbol": _safe_text(getattr(entry, "symbol", "")),
        "companyName": _safe_text(getattr(entry, "company_name", "")),
        "side": _safe_text(getattr(entry, "side", "")),
        "sideLabel": _safe_text(getattr(entry, "side_label", "")),
        "quantity": int(getattr(entry, "quantity", 0) or 0),
        "price": _decimal_to_float(price),
        "priceText": _format_trade_money(price),
        "result": _safe_text(getattr(entry, "result", "")),
        "reason": _safe_text(getattr(entry, "reason", "")),
        "mode": _safe_text(getattr(entry, "mode", "")),
        "realizedPnl": _decimal_to_float(realized_pnl),
        "realizedPnlText": _format_trade_money(realized_pnl),
    }


def _system_log_to_view_model(entry: Any) -> dict[str, str]:
    return {
        "timestamp": _safe_text(entry.timestamp),
        "level": _safe_text(entry.level),
        "title": _safe_text(entry.title),
        "message": _safe_text(entry.message),
    }


def _metric_to_view_model(label: object, value: object) -> dict[str, str]:
    return {"label": _safe_text(label), "value": _money_text(value)}


def _runtime_is_live_execution(controller: DashboardController) -> bool:
    runtime = getattr(controller.services, "runtime", None)
    return str(getattr(runtime, "execution_mode", "") or "").strip().lower() == "live"


def _runtime_status_label(
    mode: str,
    running: bool,
    current_status: object = "",
    *,
    live_order_ready: bool = False,
) -> str:
    status = _safe_text(current_status).strip()
    if mode == "real" and not running:
        return "실전 준비" if live_order_ready else "실전 잠금"
    if mode == "real" and running:
        return status or "실행 중"
    if status:
        return status
    return "실행 중" if running else "정지"


def _runtime_data_source_label(controller: DashboardController) -> str:
    if controller.state.trading_mode == "real" and not _runtime_is_live_execution(controller):
        return "KIS live account"
    runtime = getattr(controller.services, "runtime", None)
    return _safe_text(getattr(runtime, "data_source_label", "로컬 paper"))


def _runtime_data_source_kind(controller: DashboardController) -> str:
    if controller.state.trading_mode == "real" and not _runtime_is_live_execution(controller):
        return "real-prep"
    runtime = getattr(controller.services, "runtime", None)
    kind = _safe_text(getattr(runtime, "data_source_kind", "")).strip().lower()
    if kind in {"local", "kis-vts", "external-scan-kis", "live"}:
        return kind
    return "unknown"


def _runtime_data_mode_label(controller: DashboardController) -> str:
    kind = _runtime_data_source_kind(controller)
    if kind == "live":
        return "KIS 실전 주문"
    if kind in {"real-prep", "real-read-only"}:
        return "KIS 실전 계좌"
    if kind == "external-scan-kis":
        return "KIS 하이브리드 테스트"
    if kind == "kis-vts":
        return "KIS 장중 가상"
    if kind == "local":
        return "로컬 가상"
    return "출처 미확인 가상"


def _runtime_data_mode_description(controller: DashboardController) -> str:
    kind = _runtime_data_source_kind(controller)
    if kind == "live":
        return "실전 주문 runtime입니다. 주문 전 live preflight, 계좌, 장중, 감사 로그, 리스크 게이트를 확인합니다."
    if kind in {"real-prep", "real-read-only"}:
        return "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다."
    if kind == "external-scan-kis":
        return "넓은 후보군을 먼저 선별하고, 주문 직전 KIS 현재가로 최종 확인합니다."
    if kind == "kis-vts":
        return "KIS 현재가를 받아 paper 계좌로만 체결합니다."
    if kind == "local":
        return "샘플/로컬 데이터로 반복 검증합니다."
    return "데이터 출처가 명시되지 않은 paper 모드입니다."


def _runtime_safety_summary(controller: DashboardController) -> str:
    state = controller.state
    runtime = getattr(controller.services, "runtime", None)
    if state.trading_mode == "real" and _runtime_is_live_execution(controller):
        return "실전 주문 runtime · live preflight 통과 시에만 주문 전송"
    if state.trading_mode == "real":
        return "실전 거래 준비 · 시작 시 안전 게이트 확인"

    kind = _runtime_data_source_kind(controller)
    if kind == "external-scan-kis":
        candidate_count = len(getattr(runtime, "symbols", []) or [])
        candidate_label = f"후보군 {candidate_count}종목" if candidate_count > 0 else "후보군 확인 중"
        return f"실제 주문 없음 · {candidate_label} · 스캐너 선별 · KIS 현재가 최종 확인"

    if kind == "kis-vts":
        scan_limit = _positive_int(
            getattr(runtime, "scan_limit_per_cycle", KIS_INTRADAY_REHEARSAL_SCAN_LIMIT),
            fallback=KIS_INTRADAY_REHEARSAL_SCAN_LIMIT,
        )
        settings = getattr(runtime, "settings", None)
        max_positions = _positive_int(
            getattr(settings, "max_positions", KIS_INTRADAY_REHEARSAL_MAX_POSITIONS),
            fallback=KIS_INTRADAY_REHEARSAL_MAX_POSITIONS,
        )
        candidate_count = len(getattr(runtime, "symbols", []) or [])
        candidate_label = f"후보군 {candidate_count}종목" if candidate_count > 0 else "후보군 확인 중"
        return f"실제 주문 없음 · {candidate_label} · cycle당 조회 최대 {scan_limit}종목 · 최대 보유 {max_positions}종목"

    if kind == "local":
        return "실제 주문 없음 · 로컬 데이터 replay"
    return "실제 주문 없음 · paper 체결"


def _positive_int(value: object, *, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _normalize_side(value: object) -> str:
    text = str(value)
    if "short" in text.lower() or "숏" in text or (text.strip() == "??"):
        return "숏"
    return "롱"


def _reference_line_label(index: int) -> str:
    labels = ("평균 진입가", "손절선", "익절선", "트레일링선")
    if index < len(labels):
        return labels[index]
    return "기준선"


def _money_text(value: object) -> str:
    text = _safe_text(value).strip()
    text = text.replace("??", "원")
    text = text.replace("二?", "주")
    text = text.replace("媛?", "개")
    text = text.replace("濡?", "롱")
    return text


def _count_text(value: object) -> str:
    text = _money_text(value)
    return text if text.endswith("개") else text


def _safe_text(value: object) -> str:
    if is_dataclass(value):
        return "[object]"
    return redact_sensitive_text(value)


def _datetime_to_label(value: Any) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M")
    return _safe_text(value)


def _decimal_to_float(value: Any) -> float:
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _decimal_to_number(value: Decimal) -> int | float:
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _decimal_value(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _format_pct(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01')):.2f}%"


def _format_compact_pct(value: Decimal) -> str:
    percent = (value * Decimal("100")).quantize(Decimal("0.01"))
    if percent == percent.to_integral_value():
        return f"{percent:.0f}%"
    return f"{percent:.2f}%"


def _format_ratio(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.1')):.1f}"


def _format_bps(value: Decimal) -> str:
    return f"{value.quantize(Decimal('1')):.0f}bp"


def _trade_timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return ""
    return _safe_text(value)


def _trade_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _format_trade_money(value: Decimal) -> str:
    whole = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    if whole == whole.to_integral_value():
        return f"{whole:,.0f}원"
    return f"{whole:,.2f}원"


if __name__ == "__main__":
    raise SystemExit(main())
