from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .config import BotConfig
from .kis import KisCredentials
from .models import AccountSnapshot, Order


LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_REAL_MONEY_TRADING_RISK"
LIVE_KIS_ENV_KEYS = {
    "app_key": "KIS_LIVE_APP_KEY",
    "app_secret": "KIS_LIVE_APP_SECRET",
    "account_no": "KIS_LIVE_ACCOUNT_NO",
    "product_code": "KIS_LIVE_ACCOUNT_PRODUCT_CODE",
}
LIVE_CONFIRMATION_ENV_KEY = "STOCKBOT_LIVE_TRADING_CONFIRM"
LIVE_ALLOW_ENV_KEY = "STOCKBOT_ALLOW_LIVE_TRADING"
LIVE_ENABLED_ENV_KEY = "STOCKBOT_LIVE_TRADING_ENABLED"
LIVE_ACCOUNT_CONFIRMATION_ENV_KEY = "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION"


@dataclass(frozen=True)
class LiveTradingReadiness:
    ready: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class LiveOrderPreflightRequest:
    config: BotConfig
    env: Mapping[str, str | None]
    order: Order
    account: AccountSnapshot
    estimated_price: Decimal
    market_is_open: bool
    session_approved: bool = False
    account_confirmation: str = ""
    expected_account_suffix: str = ""
    live_broker_available: bool = False
    fill_reconciliation_available: bool = False
    audit_log_ready: bool = False
    managed_position_ledger_available: bool = False
    risk_limits_ok: bool = False
    new_entries_allowed: bool = False
    allow_managed_partial_sell: bool = False


@dataclass(frozen=True)
class LiveOrderPreflightDecision:
    approved: bool
    blockers: tuple[str, ...]


def live_credential_scope_fingerprint(env: Mapping[str, str | None]) -> str:
    """Return a non-reversible identifier for the exact saved live credential scope."""

    payload = "\0".join(
        f"{env_key}={str(env.get(env_key) or '').strip()}"
        for env_key in sorted(LIVE_KIS_ENV_KEYS.values())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assess_live_trading_readiness(
    config: BotConfig,
    *,
    env: Mapping[str, str | None],
    live_broker_available: bool = False,
    fill_reconciliation_available: bool = False,
    managed_position_ledger_available: bool = False,
) -> LiveTradingReadiness:
    blockers: list[str] = []
    if config.trading_mode != "live":
        blockers.append("trading_mode=live")
    if not live_allow_configured(config, env):
        blockers.append(f"allow_live_trading=true and {LIVE_ALLOW_ENV_KEY}=true")
    if not live_enabled_configured(config, env):
        blockers.append(f"live_trading_enabled=true and {LIVE_ENABLED_ENV_KEY}=true")
    if config.allow_paper_short:
        blockers.append("allow_paper_short=false")

    confirmation = str(env.get(LIVE_CONFIRMATION_ENV_KEY) or "").strip()
    if confirmation != LIVE_CONFIRMATION_PHRASE:
        blockers.append(f"{LIVE_CONFIRMATION_ENV_KEY}={LIVE_CONFIRMATION_PHRASE}")

    missing = _missing_live_credentials(env)
    if missing:
        blockers.append(f"missing KIS live credentials: {', '.join(missing)}")

    if not live_broker_available:
        blockers.append("live broker is not implemented")
    if not fill_reconciliation_available:
        blockers.append("live fill reconciliation is not implemented")
    if not managed_position_ledger_available:
        blockers.append("managed live position ledger is not available")

    return LiveTradingReadiness(ready=not blockers, blockers=tuple(blockers))


def live_allow_configured(config: BotConfig, env: Mapping[str, str | None]) -> bool:
    return bool(config.allow_live_trading) and _env_exact_true(env, LIVE_ALLOW_ENV_KEY)


def live_enabled_configured(config: BotConfig, env: Mapping[str, str | None]) -> bool:
    return bool(config.live_trading_enabled) and _env_exact_true(env, LIVE_ENABLED_ENV_KEY)


def live_order_gate_configured(config: BotConfig, env: Mapping[str, str | None]) -> bool:
    confirmation = str(env.get(LIVE_CONFIRMATION_ENV_KEY) or "").strip()
    expected_suffix = _live_account_suffix(env)
    account_confirmation = str(env.get(LIVE_ACCOUNT_CONFIRMATION_ENV_KEY) or "").strip()
    return (
        live_allow_configured(config, env)
        and live_enabled_configured(config, env)
        and confirmation == LIVE_CONFIRMATION_PHRASE
        and bool(expected_suffix)
        and account_confirmation == expected_suffix
    )


def assess_live_order_preflight(request: LiveOrderPreflightRequest) -> LiveOrderPreflightDecision:
    blockers = list(
        assess_live_trading_readiness(
            request.config,
            env=request.env,
            live_broker_available=request.live_broker_available,
            fill_reconciliation_available=request.fill_reconciliation_available,
            managed_position_ledger_available=request.managed_position_ledger_available,
        ).blockers
    )

    if not live_allow_configured(request.config, request.env):
        blockers.append(f"allow_live_trading=true and {LIVE_ALLOW_ENV_KEY}=true")
    if not live_enabled_configured(request.config, request.env):
        blockers.append(f"live_trading_enabled=true and {LIVE_ENABLED_ENV_KEY}=true")

    if not request.market_is_open:
        blockers.append("market_is_open=true")
    if not request.session_approved:
        blockers.append("session_approved=true")
    if not request.audit_log_ready:
        blockers.append("audit_log_ready=true")
    if not request.risk_limits_ok:
        blockers.append("risk_limits_ok=true")
    if request.config.kill_switch and request.order.side == "BUY":
        blockers.append("kill_switch=false")

    expected_suffix = request.expected_account_suffix.strip() or _live_account_suffix(request.env)
    saved_account_confirmation = str(request.env.get(LIVE_ACCOUNT_CONFIRMATION_ENV_KEY) or "").strip()
    if expected_suffix and saved_account_confirmation != expected_suffix:
        blockers.append(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}={expected_suffix}")
    if expected_suffix and request.account_confirmation.strip() != expected_suffix:
        blockers.append(f"account_confirmation={expected_suffix}")
    elif not expected_suffix:
        blockers.append("account_confirmation=<live account suffix>")

    if request.order.quantity <= 0:
        blockers.append("order.quantity>0")
    if request.estimated_price <= 0:
        blockers.append("estimated_price>0")
    if request.order.side not in {"BUY", "SELL"}:
        blockers.append("order.side=BUY_or_SELL")
    if request.order.side == "BUY" and not request.new_entries_allowed:
        blockers.append("new_entries_allowed=true")

    notional = request.estimated_price * Decimal(request.order.quantity)
    if request.config.max_order_amount > 0 and notional > request.config.max_order_amount:
        blockers.append("max_order_amount")
    if request.order.side == "BUY":
        if notional > request.account.buying_power:
            blockers.append("buying_power")
        position = request.account.positions.get(request.order.symbol)
        if position is not None and position.quantity > 0:
            managed_quantity = getattr(position, "managed_quantity", None)
            if managed_quantity is None or position.quantity > managed_quantity:
                blockers.append("manual_position_overlap")
    if request.order.side == "SELL":
        position = request.account.positions.get(request.order.symbol)
        sellable_quantity = None if position is None else getattr(position, "sellable_quantity", None)
        managed_quantity = None if position is None else getattr(position, "managed_quantity", None)
        if position is None or position.quantity < request.order.quantity:
            blockers.append("sellable_position")
        elif sellable_quantity is None:
            blockers.append("sellable_position_known")
        elif sellable_quantity < request.order.quantity:
            blockers.append("sellable_position")
        elif managed_quantity is None:
            blockers.append("managed_position_known")
        elif managed_quantity < request.order.quantity:
            blockers.append("managed_position")
        elif position.quantity > managed_quantity and not request.allow_managed_partial_sell:
            blockers.append("manual_position_overlap")

    unique_blockers = tuple(dict.fromkeys(blockers))
    return LiveOrderPreflightDecision(approved=not unique_blockers, blockers=unique_blockers)


def load_live_kis_credentials(env: Mapping[str, str | None]) -> KisCredentials:
    missing = _missing_live_credentials(env)
    if missing:
        raise ValueError(f"missing KIS live credentials: {', '.join(missing)}")
    return KisCredentials(
        app_key=str(env[LIVE_KIS_ENV_KEYS["app_key"]]),
        app_secret=str(env[LIVE_KIS_ENV_KEYS["app_secret"]]),
        account_no=str(env[LIVE_KIS_ENV_KEYS["account_no"]]),
        account_product_code=str(env[LIVE_KIS_ENV_KEYS["product_code"]]),
    )


def read_env_file(path: str | Path) -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _missing_live_credentials(env: Mapping[str, str | None]) -> list[str]:
    return [
        env_key
        for env_key in LIVE_KIS_ENV_KEYS.values()
        if not str(env.get(env_key) or "").strip()
    ]


def _live_account_suffix(env: Mapping[str, str | None]) -> str:
    account = str(env.get(LIVE_KIS_ENV_KEYS["account_no"]) or "").strip()
    return account[-2:] if len(account) >= 2 else account


def _env_exact_true(env: Mapping[str, str | None], key: str) -> bool:
    return str(env.get(key) or "").strip().lower() == "true"
