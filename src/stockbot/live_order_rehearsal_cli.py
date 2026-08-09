from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from .config import BotConfig, load_config
from .kis import KisLiveOrderClient, KisLiveReadOnlyClient, Transport
from .kis_smoke import _mask_account
from .live_broker import LiveBroker
from .live_order_safety_context import LiveOrderSafetyContext
from .live_safety import (
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
    load_live_kis_credentials,
    live_order_gate_configured,
    read_env_file,
)
from .models import Fill, Order
from .redaction import is_sensitive_key, redact_sensitive_text
from .runtime_factory import _build_live_broker


REHEARSAL_CONFIRM_PHRASE = "I_UNDERSTAND_ONE_REAL_ORDER"
DEFAULT_REHEARSAL_MAX_QUANTITY = 1
DEFAULT_REHEARSAL_MAX_NOTIONAL = Decimal("100000")

OrderClientFactory = Callable[..., KisLiveOrderClient]
QuoteClientFactory = Callable[..., KisLiveReadOnlyClient]
BrokerFactory = Callable[..., LiveBroker]
SafetyContextFactory = Callable[[], LiveOrderSafetyContext]


def run_live_order_rehearsal(
    *,
    order: Order,
    config: BotConfig,
    env_file: str | Path = ".env",
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    timeout: float = 10.0,
    confirm: str = "",
    account_confirmation: str = "",
    expected_account_suffix: str = "",
    max_quantity: int = DEFAULT_REHEARSAL_MAX_QUANTITY,
    max_notional: Decimal | int | str = DEFAULT_REHEARSAL_MAX_NOTIONAL,
    quote_client_factory: QuoteClientFactory | None = None,
    client_factory: OrderClientFactory | None = None,
    broker_factory: BrokerFactory | None = None,
    safety_context_factory: SafetyContextFactory = LiveOrderSafetyContext,
) -> dict[str, object]:
    """Submit at most one tightly capped live-account rehearsal order.

    This path exists between read-only dry-run and continuous live runtime.
    It uses the production LiveBroker safety gates, but adds explicit
    one-shot confirmation, quantity, and notional caps before the broker can
    reach the KIS order endpoint.
    """

    if confirm.strip() != REHEARSAL_CONFIRM_PHRASE:
        raise ValueError(f"--confirm must be {REHEARSAL_CONFIRM_PHRASE}")
    if order.side not in {"BUY", "SELL"}:
        raise ValueError("one-shot rehearsal only supports BUY or SELL")
    if order.quantity <= 0:
        raise ValueError("quantity must be positive")
    if max_quantity <= 0:
        raise ValueError("max_quantity must be positive")
    if order.quantity > max_quantity:
        raise ValueError(f"quantity exceeds one-shot rehearsal cap: {order.quantity}>{max_quantity}")

    notional_cap = _decimal_arg(max_notional, "max_notional")
    if notional_cap <= 0:
        raise ValueError("max_notional must be positive")

    env_path = Path(env_file)
    env_values = _live_env_values(env_path, env)
    credentials = load_live_kis_credentials(env_values)
    account_suffix = credentials.account_no[-2:] if len(credentials.account_no) >= 2 else credentials.account_no
    expected_suffix = expected_account_suffix.strip() or account_suffix
    effective_confirmation = account_confirmation.strip()
    if not effective_confirmation:
        raise ValueError("account confirmation is required for one-shot rehearsal")
    if expected_suffix and effective_confirmation != expected_suffix:
        raise ValueError(f"account confirmation must match live account suffix {expected_suffix}")
    env_values[LIVE_ACCOUNT_CONFIRMATION_ENV_KEY] = effective_confirmation

    quote_client = (quote_client_factory or _default_quote_client_factory)(
        credentials=credentials,
        transport=transport,
        timeout=timeout,
    )
    access_token = quote_client.issue_access_token()
    price_bar = quote_client.price_bar(order.symbol)
    estimated_price = price_bar.buy_price if order.side == "BUY" else price_bar.sell_price
    notional = estimated_price * Decimal(order.quantity)
    if notional > notional_cap:
        raise ValueError(f"notional exceeds one-shot rehearsal cap: {notional}>{notional_cap}")

    order_gate = live_order_gate_configured(config, env_values)
    client = (client_factory or _default_order_client_factory)(
        credentials=credentials,
        transport=transport,
        timeout=timeout,
        allow_order_placement=order_gate,
        access_token=access_token,
        access_token_expires_at=_access_token_expires_at(quote_client),
    )
    safety_context = safety_context_factory()
    broker = (broker_factory or _default_broker_factory)(
        config,
        env_values,
        client=client,
        env_file=env_path,
        live_order_safety_context=safety_context,
    )

    fill: Fill | None = None
    try:
        safety_context.approve_session(allow_new_entries=order.side == "BUY")
        fill = broker.place_order(order, price_bar)
    finally:
        safety_context.reset()

    return {
        "mode": "kis-live-one-shot-rehearsal",
        "read_only": False,
        "dry_run": False,
        "live_order_enabled": False,
        "session_approval_cleared": True,
        "order_submitted_confirmed": bool(fill and fill.accepted),
        "accepted": bool(fill and fill.accepted),
        "symbol": order.symbol,
        "side": order.side,
        "quantity": order.quantity,
        "reason": order.reason,
        "estimated_price": _decimal_to_output(estimated_price),
        "notional": _decimal_to_output(notional),
        "fill_price": _decimal_to_output(fill.price) if fill else "0",
        "fill_quantity": fill.quantity if fill else 0,
        "reject_reason": "" if fill is None else fill.reject_reason,
        "account": _mask_account(credentials.account_no, credentials.account_product_code),
        "note": "One-shot rehearsal clears in-process approval after a single broker attempt.",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Submit one tightly capped KIS live order rehearsal through the production safety gates.",
    )
    parser.add_argument("--config", required=True, help="Path to live BotConfig YAML")
    parser.add_argument("--env-file", default=".env", help="Path to local env file")
    parser.add_argument("--symbol", required=True, help="Domestic stock code to rehearse")
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY", help="Order side to rehearse")
    parser.add_argument("--quantity", type=int, required=True, help="Order quantity to rehearse")
    parser.add_argument("--reason", default="operator one-shot live rehearsal", help="Human-readable order reason")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--confirm", required=True, help=f"Must equal {REHEARSAL_CONFIRM_PHRASE}")
    parser.add_argument("--account-confirmation", required=True, help="Live account suffix typed by the operator")
    parser.add_argument("--expected-account-suffix", default="", help="Expected live account suffix")
    parser.add_argument("--max-quantity", type=int, default=DEFAULT_REHEARSAL_MAX_QUANTITY, help="One-shot quantity cap")
    parser.add_argument("--max-notional", default=str(DEFAULT_REHEARSAL_MAX_NOTIONAL), help="One-shot notional cap in KRW")
    args = parser.parse_args(argv)

    try:
        order = Order.buy(args.symbol, args.quantity, args.reason) if args.side == "BUY" else Order.sell(args.symbol, args.quantity, args.reason)
        result = run_live_order_rehearsal(
            order=order,
            config=load_config(Path(args.config)),
            env_file=Path(args.env_file),
            timeout=args.timeout,
            confirm=args.confirm,
            account_confirmation=args.account_confirmation,
            expected_account_suffix=args.expected_account_suffix,
            max_quantity=args.max_quantity,
            max_notional=args.max_notional,
        )
    except Exception as exc:
        print(
            json.dumps({"ready": False, "error": _redact_cli_text(str(exc), Path(args.env_file))}, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 1

    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)
    print(_redact_cli_text(rendered, Path(args.env_file)))
    return 0


def _default_quote_client_factory(
    *,
    credentials,
    transport: Transport | None,
    timeout: float,
) -> KisLiveReadOnlyClient:
    return KisLiveReadOnlyClient(
        credentials,
        transport=transport,
        timeout=timeout,
    )


def _default_order_client_factory(
    *,
    credentials,
    transport: Transport | None,
    timeout: float,
    allow_order_placement: bool,
    access_token: str | None = None,
    access_token_expires_at=None,
) -> KisLiveOrderClient:
    return KisLiveOrderClient(
        credentials,
        transport=transport,
        timeout=timeout,
        allow_order_placement=allow_order_placement,
        access_token=access_token,
        access_token_expires_at=access_token_expires_at,
    )


def _default_broker_factory(
    config: BotConfig,
    env_values: dict[str, str],
    *,
    client: KisLiveOrderClient,
    env_file: Path,
    live_order_safety_context: LiveOrderSafetyContext,
) -> LiveBroker:
    return _build_live_broker(
        config,
        env_values,
        client=client,
        env_file=env_file,
        live_order_safety_context=live_order_safety_context,
    )


def _live_env_values(env_file: Path, env: Mapping[str, str] | None) -> dict[str, str]:
    values = read_env_file(env_file)
    if env is not None:
        values.update(dict(env))
        return values
    if values:
        return values
    values.update(dict(os.environ))
    return values


def _decimal_arg(value: Decimal | int | str, name: str) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc


def _decimal_to_output(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral_value() else format(value.quantize(Decimal("1")), "f")


def _access_token_expires_at(client: KisLiveReadOnlyClient):
    getter = getattr(client, "access_token_expires_at", None)
    return getter() if callable(getter) else None


def _redact_cli_text(text: str, env_file: Path) -> str:
    values = read_env_file(env_file)
    if not values:
        values = dict(os.environ)
    extra_values = [value for key, value in values.items() if is_sensitive_key(key)]
    return redact_sensitive_text(text, extra_values=extra_values)


if __name__ == "__main__":
    raise SystemExit(main())
