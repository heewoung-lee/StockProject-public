from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import load_config
from .live_probe import run_live_order_dry_run
from .live_safety import read_env_file
from .models import Order
from .redaction import is_sensitive_key, redact_sensitive_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate a KIS live order candidate without placing an order.",
    )
    parser.add_argument("--config", default=None, help="Path to live BotConfig YAML")
    parser.add_argument("--env-file", default=".env", help="Path to local env file")
    parser.add_argument("--symbol", required=True, help="Domestic stock code to evaluate")
    parser.add_argument("--side", choices=("BUY", "SELL"), default="BUY", help="Order side to evaluate")
    parser.add_argument("--quantity", type=int, required=True, help="Order quantity to evaluate")
    parser.add_argument("--reason", default="operator live order dry-run", help="Human-readable order reason")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--market-open", action="store_true", help="Declare that the regular market is open")
    parser.add_argument("--session-approved", action="store_true", help="Declare operator approval for this dry-run session")
    parser.add_argument("--account-confirmation", default="", help="Live account suffix typed by the operator")
    parser.add_argument("--expected-account-suffix", default="", help="Expected live account suffix")
    parser.add_argument("--fill-reconciliation-available", action="store_true", help="Fill reconciliation path is available")
    parser.add_argument("--audit-log-ready", action="store_true", help="Audit log path is ready")
    parser.add_argument("--managed-position-ledger-available", action="store_true", help="Managed-position ledger is available")
    parser.add_argument("--risk-limits-ok", action="store_true", help="Risk limits are healthy")
    parser.add_argument("--new-entries-allowed", action="store_true", help="New BUY entries are allowed")
    args = parser.parse_args(argv)

    try:
        env_file = Path(args.env_file)
        order = Order.buy(args.symbol, args.quantity, args.reason) if args.side == "BUY" else Order.sell(args.symbol, args.quantity, args.reason)
        result = run_live_order_dry_run(
            order=order,
            config=load_config(Path(args.config)) if args.config else load_config(None),
            env_file=env_file,
            timeout=args.timeout,
            market_is_open=args.market_open,
            session_approved=args.session_approved,
            account_confirmation=args.account_confirmation,
            expected_account_suffix=args.expected_account_suffix,
            fill_reconciliation_available=args.fill_reconciliation_available,
            audit_log_ready=args.audit_log_ready,
            managed_position_ledger_available=args.managed_position_ledger_available,
            risk_limits_ok=args.risk_limits_ok,
            new_entries_allowed=args.new_entries_allowed,
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


def _redact_cli_text(text: str, env_file: Path) -> str:
    values = read_env_file(env_file)
    if not values:
        values = dict(os.environ)
    extra_values = [value for key, value in values.items() if is_sensitive_key(key)]
    return redact_sensitive_text(text, extra_values=extra_values)


if __name__ == "__main__":
    raise SystemExit(main())
