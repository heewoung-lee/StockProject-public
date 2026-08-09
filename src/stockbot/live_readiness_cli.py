from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping

from .config import BotConfig
from .live_audit import JsonlLiveAuditLog
from .live_order_state import JsonManualReconciliationStore, JsonPendingLiveOrderStore, ManualReconciliationBlocker, PendingLiveOrder
from .live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from .live_safety import LIVE_ACCOUNT_CONFIRMATION_ENV_KEY, assess_live_trading_readiness, live_order_gate_configured, read_env_file
from .redaction import redact_sensitive_text
from .scanner import JsonScannerProvider
from .scanner_collector import (
    DEFAULT_NAVER_MINUTE_HISTORY_CANDIDATES,
    DEFAULT_NAVER_MINUTE_HISTORY_TIMEOUT_SECONDS,
    DEFAULT_NAVER_MINUTE_HISTORY_WORKERS,
    collect_naver_market_scanner_snapshot,
)
from .scanner_snapshot import SnapshotWriteOptions

MANUAL_RECONCILIATION_CLEAR_PHRASE = "I_CONFIRMED_LIVE_ACCOUNT_RECONCILED"
DEFAULT_SCANNER_SNAPSHOT_PATH = "data/scanner_snapshot.json"


LIVE_READINESS_CONFIG_KEYS = {
    "trading_mode",
    "allow_live_trading",
    "live_trading_enabled",
    "allow_paper_short",
    "journal_path",
}
LIVE_READINESS_RUNTIME_KEYS = {
    "market_data_source",
    "scanner_source",
    "scanner_snapshot_path",
    "scanner_snapshot_max_age_seconds",
}
LIVE_READINESS_SCANNER_REFRESH_KEYS = {
    "initial_cash",
    "kis_market_data_scan_limit",
    "max_position_amount",
    "max_positions",
    "scan_limit_per_cycle",
}
LIVE_READINESS_ALL_KEYS = LIVE_READINESS_CONFIG_KEYS | LIVE_READINESS_RUNTIME_KEYS | LIVE_READINESS_SCANNER_REFRESH_KEYS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check live trading readiness without placing orders.")
    parser.add_argument("--config", default="config.live.example.yaml", help="Path to a local live-readiness config file")
    parser.add_argument("--env-file", default=".env", help="Path to a local env file")
    parser.add_argument(
        "--clear-manual-reconciliation",
        metavar="CONFIRMATION",
        default="",
        help=(
            "Clear the live manual-reconciliation blocker only after comparing the local ledger "
            f"with the live KIS account. Must equal {MANUAL_RECONCILIATION_CLEAR_PHRASE}."
        ),
    )
    parser.add_argument(
        "--refresh-scanner-snapshot",
        action="store_true",
        help=(
            "Refresh the configured JSON scanner snapshot from the external no-key scanner before checking freshness. "
            "This command still never places orders."
        ),
    )
    args = parser.parse_args(argv)

    try:
        payload = run_live_readiness_check(
            config_path=args.config,
            env_file=args.env_file,
            clear_manual_reconciliation=args.clear_manual_reconciliation,
            refresh_scanner_snapshot=args.refresh_scanner_snapshot,
        )
    except Exception as exc:
        print(json.dumps({"ready": False, "error": redact_sensitive_text(str(exc))}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload.get("ready") else 1


def run_live_readiness_check(
    *,
    config_path: str | Path = "config.live.example.yaml",
    env_file: str | Path = ".env",
    clear_manual_reconciliation: str = "",
    refresh_scanner_snapshot: bool = False,
    config_values: Mapping[str, object] | None = None,
) -> dict[str, object]:
    config_path = Path(config_path)
    env_file = Path(env_file)
    scanner_snapshot_refreshed = False

    loaded_config_values = _load_live_readiness_config_values(config_path)
    if config_values is not None:
        loaded_config_values.update(config_values)
    config = _live_readiness_config_from_values(loaded_config_values)
    env = _live_readiness_env_values(env_file)
    if refresh_scanner_snapshot:
        scanner_snapshot_refreshed = _refresh_scanner_snapshot(config_path, loaded_config_values)
    manual_blocker_clear = _clear_manual_reconciliation_if_requested(
        config,
        env_file,
        env,
        confirmation=clear_manual_reconciliation,
    )
    manual_reconciliation_blockers = _manual_reconciliation_blockers(config, env_file, env)
    ledger_blockers = _managed_position_ledger_blockers(config, env_file, env)
    readiness = assess_live_trading_readiness(
        config,
        env=env,
        live_broker_available=True,
        fill_reconciliation_available=True,
        managed_position_ledger_available=not ledger_blockers,
    )
    order_gate_blockers = _live_order_gate_blockers(config, env)
    runtime_blockers = _scanner_snapshot_blockers(config_path, loaded_config_values)
    blockers = _dedupe(
        [
            *readiness.blockers,
            *order_gate_blockers,
            *runtime_blockers,
            *manual_reconciliation_blockers,
            *ledger_blockers,
        ]
    )
    ready = (
        readiness.ready
        and not order_gate_blockers
        and not runtime_blockers
        and not manual_reconciliation_blockers
        and not ledger_blockers
    )
    return {
        "ready": ready,
        "blockers": blockers,
        "manual_reconciliation_cleared": manual_blocker_clear,
        "scanner_snapshot_refreshed": scanner_snapshot_refreshed,
        "live_order_enabled": False,
        "note": "This command never places orders. Live orders still require session approval, market-hours checks, fresh scanner data, audit logging, and per-order preflight.",
    }


def dashboard_live_readiness_config_values(config: BotConfig) -> dict[str, object]:
    """Derive the live-readiness contract used by dashboard real mode.

    The desktop dashboard normally boots from a paper-safe config file. Real-mode
    readiness still needs to validate the same external scanner and live order
    gates that the live runtime will use after the start button is pressed.
    """
    values = {
        key: getattr(config, key)
        for key in LIVE_READINESS_ALL_KEYS
        if hasattr(config, key)
    }
    scanner_source = str(values.get("scanner_source", "") or "").strip().lower()
    if scanner_source not in {"json", "kiwoom"}:
        scanner_source = "json"
    scanner_snapshot_path = str(values.get("scanner_snapshot_path", "") or "").strip()
    if scanner_source == "json" and not scanner_snapshot_path:
        scanner_snapshot_path = DEFAULT_SCANNER_SNAPSHOT_PATH

    values.update(
        {
            "trading_mode": "live",
            "allow_live_trading": True,
            "live_trading_enabled": True,
            "allow_paper_short": False,
            "market_data_source": "external-scan-kis",
            "scanner_source": scanner_source,
            "scanner_snapshot_path": scanner_snapshot_path,
        }
    )
    return values


def _clear_manual_reconciliation_if_requested(
    config: BotConfig,
    env_file: Path,
    env: Mapping[str, str],
    *,
    confirmation: str,
) -> bool:
    if not confirmation:
        return False
    if confirmation != MANUAL_RECONCILIATION_CLEAR_PHRASE:
        raise ValueError("manual reconciliation clear confirmation is invalid")
    store = _manual_reconciliation_store(config, env_file, env)
    store.ensure_ready()
    pending_store = _pending_order_store(config, env_file, env)
    pending_store.ensure_ready()
    blocker = store.blocker()
    manual_orders = [order for order in pending_store.all() if order.order_no.startswith("manual:")]
    audit = _live_readiness_audit_log(config, env_file, env)
    audit_payload = {
        "scope": _live_readiness_managed_positions_scope(env),
        "had_blocker": blocker is not None,
        "manual_pending_order_count": len(manual_orders),
    }
    audit.record("live_manual_reconciliation_clear_requested", audit_payload)
    try:
        store.clear()
        _clear_manual_pending_orders_from_store(pending_store)
        audit.record("live_manual_reconciliation_cleared_by_operator", audit_payload)
    except Exception:
        _restore_manual_reconciliation_state(store, pending_store, blocker, manual_orders)
        raise
    return True


def _manual_reconciliation_blockers(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> list[str]:
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return ["manual reconciliation account scope is known"]
    try:
        store = _manual_reconciliation_store(config, env_file, env)
        store.ensure_ready()
        blocker = store.blocker()
    except Exception as exc:
        return [f"manual reconciliation store is ready: {redact_sensitive_text(str(exc))}"]
    if blocker is None:
        return _manual_pending_order_blockers(config, env_file, env)
    details = ", ".join(
        item
        for item in (
            f"symbol={blocker.symbol}" if blocker.symbol else "",
            f"side={blocker.side}" if blocker.side else "",
            f"quantity={blocker.quantity}" if blocker.quantity else "",
            f"reason={blocker.reason}" if blocker.reason else "",
        )
        if item
    )
    if details:
        return [
            f"manual live account reconciliation is required: {details}",
            *_manual_pending_order_blockers(config, env_file, env),
        ]
    return [
        "manual live account reconciliation is required",
        *_manual_pending_order_blockers(config, env_file, env),
    ]


def _clear_manual_pending_orders(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> None:
    store = _pending_order_store(config, env_file, env)
    store.ensure_ready()
    _clear_manual_pending_orders_from_store(store)


def _clear_manual_pending_orders_from_store(store: JsonPendingLiveOrderStore) -> None:
    for order in store.all():
        if order.order_no.startswith("manual:"):
            store.remove(order.order_no)


def _restore_manual_reconciliation_state(
    store: JsonManualReconciliationStore,
    pending_store: JsonPendingLiveOrderStore,
    blocker: ManualReconciliationBlocker | None,
    manual_orders: list[PendingLiveOrder],
) -> None:
    if blocker is not None:
        store.latch(blocker)
    for order in manual_orders:
        pending_store.upsert(order)


def _live_readiness_audit_log(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> JsonlLiveAuditLog:
    return JsonlLiveAuditLog(_live_readiness_audit_path(config, env_file), redact_values=tuple(env.values()))


def _manual_pending_order_blockers(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> list[str]:
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return ["manual reconciliation account scope is known"]
    try:
        store = _pending_order_store(config, env_file, env)
        store.ensure_ready()
        orders = list(store.all())
    except Exception as exc:
        return [f"manual pending order store is ready: {redact_sensitive_text(str(exc))}"]
    manual_orders = [order for order in orders if order.order_no.startswith("manual:")]
    broker_orders = [order for order in orders if not order.order_no.startswith("manual:")]
    blockers: list[str] = []
    if manual_orders:
        symbols = ",".join(sorted({order.symbol for order in manual_orders if order.symbol})[:5])
        detail = f" count={len(manual_orders)}"
        if symbols:
            detail = f"{detail} symbols={symbols}"
        blockers.append(f"manual pending live order requires reconciliation:{detail}")
    if broker_orders:
        symbols = ",".join(sorted({order.symbol for order in broker_orders if order.symbol})[:5])
        detail = f" count={len(broker_orders)}"
        if symbols:
            detail = f"{detail} symbols={symbols}"
        blockers.append(f"pending live order requires reconciliation before live readiness:{detail}")
    return blockers


def _manual_reconciliation_store(
    config: BotConfig,
    env_file: Path,
    env: Mapping[str, str],
) -> JsonManualReconciliationStore:
    return JsonManualReconciliationStore(
        _live_readiness_manual_reconciliation_path(config, env_file, env),
        scope=_live_readiness_managed_positions_scope(env),
    )


def _pending_order_store(
    config: BotConfig,
    env_file: Path,
    env: Mapping[str, str],
) -> JsonPendingLiveOrderStore:
    return JsonPendingLiveOrderStore(
        _live_readiness_pending_orders_path(config, env_file, env),
        scope=_live_readiness_managed_positions_scope(env),
    )


def _managed_position_ledger_blockers(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> list[str]:
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return ["managed live position ledger account scope is known"]
    try:
        ledger = JsonManagedLivePositionLedger(
            _live_readiness_managed_positions_path(config, env_file, env),
            scope=scope,
        )
        ledger.ensure_ready()
    except Exception as exc:
        return [f"managed live position ledger is ready: {redact_sensitive_text(str(exc))}"]
    return []


def _live_readiness_manual_reconciliation_path(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> Path:
    base_path = _live_readiness_audit_path(config, env_file).with_name("live_manual_reconciliation_required.json")
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return base_path
    return base_path.with_name(f"live_manual_reconciliation_required_{scope}.json")


def _live_readiness_pending_orders_path(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> Path:
    base_path = _live_readiness_audit_path(config, env_file).with_name("pending_live_orders.json")
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return base_path
    return base_path.with_name(f"pending_live_orders_{scope}.json")


def _live_readiness_managed_positions_path(config: BotConfig, env_file: Path, env: Mapping[str, str]) -> Path:
    base_path = _live_readiness_audit_path(config, env_file).with_name("managed_live_positions.json")
    scope = _live_readiness_managed_positions_scope(env)
    if not scope:
        return base_path
    return base_path.with_name(f"managed_live_positions_{scope}.json")


def _live_readiness_audit_path(config: BotConfig, env_file: Path) -> Path:
    journal_path = Path(config.journal_path)
    if journal_path.is_absolute():
        return journal_path.parent / "live_orders.jsonl"
    return env_file.resolve().parent / "logs" / "live_orders.jsonl"


def _live_readiness_managed_positions_scope(env: Mapping[str, str]) -> str:
    account_no = str(env.get("KIS_LIVE_ACCOUNT_NO") or "").strip()
    product_code = str(env.get("KIS_LIVE_ACCOUNT_PRODUCT_CODE") or "").strip()
    return managed_live_position_ledger_scope(account_no, product_code)


def _live_readiness_env_values(env_file: Path) -> dict[str, str]:
    return read_env_file(env_file)


def _live_order_gate_blockers(config: BotConfig, env: Mapping[str, str]) -> list[str]:
    if live_order_gate_configured(config, env):
        return []
    account_no = str(env.get("KIS_LIVE_ACCOUNT_NO") or "").strip()
    expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
    if expected_suffix:
        return [f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}={expected_suffix}"]
    return [f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=<live account suffix>"]


def _load_live_readiness_config(path: Path) -> BotConfig:
    return _live_readiness_config_from_values(_load_live_readiness_config_values(path))


def _refresh_scanner_snapshot(config_path: Path, values: Mapping[str, object]) -> bool:
    configured_path = _effective_scanner_snapshot_path(values)
    if not configured_path:
        return False

    snapshot_path = _resolve_configured_path(config_path, configured_path)
    count = collect_naver_market_scanner_snapshot(
        snapshot_path,
        SnapshotWriteOptions(
            provider="naver-mobile-auto",
            max_price=_scanner_snapshot_refresh_max_price(values),
        ),
        markets=("all",),
        pages=0,
        page_size=100,
        timeout=10.0,
        minute_history_candidates=DEFAULT_NAVER_MINUTE_HISTORY_CANDIDATES,
        minute_history_workers=DEFAULT_NAVER_MINUTE_HISTORY_WORKERS,
        minute_history_timeout=DEFAULT_NAVER_MINUTE_HISTORY_TIMEOUT_SECONDS,
    )
    if count <= 0:
        raise ValueError("scanner snapshot refresh produced no candidates")
    return True


def _scanner_snapshot_refresh_max_price(values: Mapping[str, object]) -> Decimal:
    initial_cash = _decimal_config_value(values, "initial_cash", BotConfig.default().initial_cash)
    max_position_amount = _decimal_config_value(
        values,
        "max_position_amount",
        BotConfig.default().max_position_amount,
    )
    budget = initial_cash
    if max_position_amount > 0:
        budget = min(budget, max_position_amount)
    return max(Decimal("0"), budget)


def _decimal_config_value(values: Mapping[str, object], key: str, default: Decimal) -> Decimal:
    raw = values.get(key, default)
    try:
        return Decimal(str(raw).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{key} must be a decimal number") from exc


def _int_config_value(values: Mapping[str, object], key: str, default: int) -> int:
    raw = values.get(key, default)
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"{key} must be 0 or greater")
    return parsed


def _load_live_readiness_config_values(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}

    values: dict[str, object] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in LIVE_READINESS_ALL_KEYS:
            continue
        parsed = value.strip().strip('"').strip("'")
        if key in {"allow_live_trading", "live_trading_enabled", "allow_paper_short"}:
            values[key] = _to_bool_value(parsed, key)
        else:
            values[key] = parsed

    return values


def _live_readiness_config_from_values(values: Mapping[str, object]) -> BotConfig:
    config_values = {key: value for key, value in values.items() if key in LIVE_READINESS_CONFIG_KEYS}
    return BotConfig(**config_values)


def _scanner_snapshot_blockers(config_path: Path, values: Mapping[str, object]) -> list[str]:
    configured_path = _effective_scanner_snapshot_path(values)
    if not configured_path:
        return []

    display_path = configured_path
    snapshot_path = _resolve_configured_path(config_path, configured_path)
    max_age_seconds, max_age_error = _scanner_snapshot_max_age_seconds(values)
    if max_age_error is not None:
        return [max_age_error]
    try:
        provider = JsonScannerProvider(snapshot_path, max_snapshot_age_seconds=max_age_seconds)
        symbols = provider.rank_symbols([])
    except Exception as exc:
        return [f"scanner_snapshot_path is usable by runtime: {display_path} {redact_sensitive_text(str(exc))}"]

    if not symbols:
        return [f"scanner_snapshot_path has scanner candidates: {display_path}"]
    snapshot = provider.snapshot(symbols[:1])
    if not snapshot.bars:
        detail = "; ".join(snapshot.diagnostics.messages)
        suffix = f" {detail}" if detail else ""
        return [f"scanner_snapshot_path has usable prices: {display_path}{suffix}"]
    return []


def _effective_scanner_snapshot_path(values: Mapping[str, object]) -> str:
    configured_path = str(values.get("scanner_snapshot_path", "") or "").strip()
    if configured_path:
        return configured_path
    scanner_source = str(values.get("scanner_source", "") or "").strip().lower()
    if scanner_source == "kiwoom":
        return ""
    return DEFAULT_SCANNER_SNAPSHOT_PATH


def _resolve_configured_path(config_path: Path, configured_path: str) -> Path:
    path = Path(configured_path)
    if path.is_absolute():
        return path
    base = config_path.resolve().parent if config_path.exists() else Path.cwd()
    return base / path


def _scanner_snapshot_max_age_seconds(values: Mapping[str, object]) -> tuple[int, str | None]:
    raw = values.get("scanner_snapshot_max_age_seconds", 300)
    try:
        parsed = int(str(raw).strip())
    except Exception:
        return 0, "scanner_snapshot_max_age_seconds is an integer"
    if parsed < 0:
        return 0, "scanner_snapshot_max_age_seconds is 0 or greater"
    return parsed, None


def _dedupe(items: list[str] | tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(items))


def _to_bool_value(value: object, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} has invalid boolean value: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
