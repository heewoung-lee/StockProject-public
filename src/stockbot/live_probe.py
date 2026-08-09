from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Mapping

from .config import BotConfig
from .kis import KisLiveReadOnlyClient, Transport
from .kis_market_data import KisTokenFileCache
from .kis_models import KisQuoteUnavailableError, parse_kis_account_snapshot, parse_kis_price_bar
from .kis_smoke import _mask_account
from .live_readiness_cli import redact_sensitive_text
from .live_safety import LiveOrderPreflightRequest, assess_live_order_preflight, load_live_kis_credentials, read_env_file
from .models import AccountSnapshot, Order


def run_live_read_only_probe(
    *,
    symbol: str,
    env_file: str | Path = ".env",
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    timeout: float = 10.0,
    token_cache: KisTokenFileCache | None = None,
    rate_limiter: object | None = None,
) -> dict[str, object]:
    values = _live_env_values(env_file, env)
    credentials = load_live_kis_credentials(values)
    cache = token_cache or (KisTokenFileCache(namespace="kis-live") if transport is None else None)
    client = KisLiveReadOnlyClient(
        credentials,
        transport=transport,
        timeout=timeout,
        token_cache=cache,
        rate_limiter=rate_limiter,
    )
    token_issued = _ensure_live_access_token(client, credentials, cache)
    price_response = client.inquire_price(symbol)
    try:
        price_bar = parse_kis_price_bar(
            price_response,
            symbol=symbol,
            orderbook_response=client.inquire_asking_price_exp_ccn(symbol),
        )
    except KisQuoteUnavailableError:
        price_bar = parse_kis_price_bar(price_response, symbol=symbol)
    account = client.account_snapshot()

    return {
        "mode": "kis-live-read-only",
        "read_only": True,
        "live_order_enabled": False,
        "token_issued": token_issued,
        "symbol": symbol,
        "last_price": _decimal_to_output(price_bar.close),
        "balance_positions": len(account.positions),
        "cash": _decimal_to_output(account.cash),
        "equity": _decimal_to_output(account.equity),
        "buying_power": _decimal_to_output(account.buying_power),
        "positions": [_position_to_output(position) for position in account.positions.values()],
        "account": _mask_account(credentials.account_no, credentials.account_product_code),
        "note": "Live account probe is read-only and never places orders.",
    }


def run_live_order_dry_run(
    *,
    order: Order,
    config: BotConfig,
    symbol: str | None = None,
    env_file: str | Path = ".env",
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    timeout: float = 10.0,
    token_cache: KisTokenFileCache | None = None,
    rate_limiter: object | None = None,
    market_is_open: bool = False,
    session_approved: bool = False,
    account_confirmation: str = "",
    expected_account_suffix: str = "",
    fill_reconciliation_available: bool = False,
    audit_log_ready: bool = False,
    managed_position_ledger_available: bool = False,
    risk_limits_ok: bool = False,
    new_entries_allowed: bool = False,
) -> dict[str, object]:
    """Evaluate a live-account order candidate without any order surface.

    This function intentionally uses KisLiveReadOnlyClient only. It may request
    token, quote, and balance data, but it cannot submit hashkey or order-cash
    requests because the read-only client does not expose those methods.
    """

    values = _live_env_values(env_file, env)
    credentials = load_live_kis_credentials(values)
    cache = token_cache or (KisTokenFileCache(namespace="kis-live") if transport is None else None)
    client = KisLiveReadOnlyClient(
        credentials,
        transport=transport,
        timeout=timeout,
        token_cache=cache,
        rate_limiter=rate_limiter,
    )
    token_issued = _ensure_live_access_token(client, credentials, cache)
    target_symbol = symbol or order.symbol
    price_bar = client.price_bar(target_symbol)
    account = parse_kis_account_snapshot(client.inquire_balance(), allow_deposit_cash_fallback=False)
    try:
        realized_pnl_today = client.realized_pnl_today()
    except Exception:
        pass
    else:
        account = replace(
            account,
            realized_pnl_today=realized_pnl_today,
            realized_pnl_today_known=True,
        )
    estimated_price = price_bar.buy_price if order.side == "BUY" else price_bar.sell_price
    decision = assess_live_order_preflight(
        LiveOrderPreflightRequest(
            config=config,
            env=values,
            order=order,
            account=account,
            estimated_price=estimated_price,
            market_is_open=market_is_open,
            session_approved=session_approved,
            account_confirmation=account_confirmation,
            expected_account_suffix=expected_account_suffix,
            live_broker_available=True,
            fill_reconciliation_available=fill_reconciliation_available,
            audit_log_ready=audit_log_ready,
            managed_position_ledger_available=managed_position_ledger_available,
            risk_limits_ok=risk_limits_ok,
            new_entries_allowed=new_entries_allowed,
        )
    )
    blockers = list(decision.blockers)
    if order.side == "BUY" and not account.realized_pnl_today_known:
        blockers.append("live_daily_realized_pnl_unknown")
    elif order.side == "BUY" and _account_day_pnl(account) <= -config.max_daily_loss:
        blockers.append("live_daily_loss_limit_reached")
    blockers = list(dict.fromkeys(blockers))

    notional = estimated_price * Decimal(order.quantity)
    return {
        "mode": "kis-live-order-dry-run",
        "read_only": True,
        "dry_run": True,
        "live_order_enabled": False,
        "order_submitted": False,
        "token_issued": token_issued,
        "approved": not blockers,
        "blockers": blockers,
        "symbol": target_symbol,
        "side": order.side,
        "quantity": order.quantity,
        "reason": order.reason,
        "estimated_price": _decimal_to_output(estimated_price),
        "notional": _decimal_to_output(notional),
        "cash": _decimal_to_output(account.cash),
        "equity": _decimal_to_output(account.equity),
        "buying_power": _decimal_to_output(account.buying_power),
        "balance_positions": len(account.positions),
        "account": _mask_account(credentials.account_no, credentials.account_product_code),
        "note": "Live order dry-run is read-only and never places orders.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only KIS live account probe.")
    parser.add_argument("--env-file", default=".env", help="Path to local env file")
    parser.add_argument("--symbol", default="005930", help="Domestic stock code to query")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)

    try:
        result = run_live_read_only_probe(env_file=args.env_file, symbol=args.symbol, timeout=args.timeout)
    except Exception as exc:
        print(json.dumps({"ready": False, "error": redact_sensitive_text(str(exc))}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _live_env_values(env_file: str | Path, env: Mapping[str, str] | None) -> dict[str, str]:
    values = read_env_file(env_file)
    if env is not None:
        values.update(dict(env))
        return values
    if values:
        return values
    values.update(dict(os.environ))
    return values


def _ensure_live_access_token(
    client: KisLiveReadOnlyClient,
    credentials,
    token_cache: KisTokenFileCache | None,
) -> bool:
    if token_cache is not None:
        cached_token = token_cache.read(credentials)
        if cached_token is not None:
            client.set_access_token(cached_token.access_token, expires_at=cached_token.expires_at)
            return False

    access_token = client.issue_access_token_with_rate_limit()
    if token_cache is not None:
        token_cache.write(credentials, access_token, client.access_token_expires_at())
    return True


def _decimal_to_output(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral_value() else format(value.quantize(Decimal("1")), "f")


def _account_day_pnl(account: AccountSnapshot) -> Decimal:
    unrealized = sum(position.unrealized_pnl for position in account.positions.values())
    return account.realized_pnl_today + unrealized


def _position_to_output(position) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": position.symbol,
        "side": position.side,
        "quantity": int(position.quantity),
        "avg_price": _decimal_to_output(position.avg_price),
        "last_price": _decimal_to_output(position.last_price),
        "market_value": _decimal_to_output(position.market_value),
        "unrealized_pnl": _decimal_to_output(position.unrealized_pnl),
    }
    if position.sellable_quantity is not None:
        payload["sellable_quantity"] = int(position.sellable_quantity)
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
