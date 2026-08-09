from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Mapping, TypeVar

from .kis import KisApiError, KisCredentials, KisVtsClient, Transport


REQUIRED_ENV_KEYS = (
    "KIS_VTS_APP_KEY",
    "KIS_VTS_APP_SECRET",
    "KIS_VTS_ACCOUNT_NO",
    "KIS_VTS_ACCOUNT_PRODUCT_CODE",
)

KIS_PER_SECOND_RATE_LIMIT_CODE = "EGW00201"
KIS_PER_SECOND_RATE_LIMIT_TEXT = "초당 거래건수"
T = TypeVar("T")


def load_kis_vts_credentials(env_file: str | Path = ".env", env: Mapping[str, str] | None = None) -> KisCredentials:
    merged = _read_env_file(Path(env_file))
    merged.update(dict(os.environ if env is None else env))

    missing = [key for key in REQUIRED_ENV_KEYS if not merged.get(key)]
    if missing:
        raise ValueError(f"missing KIS VTS credentials: {', '.join(missing)}")

    return KisCredentials(
        app_key=merged["KIS_VTS_APP_KEY"],
        app_secret=merged["KIS_VTS_APP_SECRET"],
        account_no=merged["KIS_VTS_ACCOUNT_NO"],
        account_product_code=merged["KIS_VTS_ACCOUNT_PRODUCT_CODE"],
    )


def run_read_only_smoke(
    *,
    symbol: str,
    env_file: str | Path = ".env",
    env: Mapping[str, str] | None = None,
    transport: Transport | None = None,
    timeout: float = 10.0,
    sleep: Callable[[float], None] = time.sleep,
    rate_limit_retry_delay: float = 1.1,
) -> dict[str, object]:
    credentials = load_kis_vts_credentials(env_file, env)
    client = KisVtsClient(credentials, transport=transport, timeout=timeout, allow_order_placement=False)

    client.issue_access_token()
    price_bar = _with_per_second_rate_limit_retry(
        lambda: client.price_bar(symbol),
        sleep=sleep,
        delay=rate_limit_retry_delay,
    )
    account = _with_per_second_rate_limit_retry(
        client.account_snapshot,
        sleep=sleep,
        delay=rate_limit_retry_delay,
    )

    return {
        "mode": "kis-vts",
        "read_only": True,
        "token_issued": True,
        "symbol": symbol,
        "last_price": _decimal_to_output(price_bar.close),
        "balance_positions": len(account.positions),
        "cash": _decimal_to_output(account.cash),
        "equity": _decimal_to_output(account.equity),
        "account": _mask_account(credentials.account_no, credentials.account_product_code),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a read-only KIS VTS smoke test.")
    parser.add_argument("--env-file", default=".env", help="Path to local env file")
    parser.add_argument("--symbol", default="005930", help="Domestic stock code to query")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    args = parser.parse_args(argv)

    try:
        result = run_read_only_smoke(env_file=args.env_file, symbol=args.symbol, timeout=args.timeout)
    except Exception as exc:
        print(_safe_error_message(exc), file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _mask_account(account_no: str, product_code: str) -> str:
    visible = account_no[-2:] if len(account_no) >= 2 else account_no
    return f"******{visible}-{product_code}"


def _decimal_to_output(value: Decimal) -> str:
    return format(value.normalize(), "f") if value != value.to_integral_value() else format(value.quantize(Decimal("1")), "f")


def _with_per_second_rate_limit_retry(call: Callable[[], T], *, sleep: Callable[[float], None], delay: float) -> T:
    try:
        return call()
    except KisApiError as exc:
        if not _is_per_second_rate_limit(exc):
            raise
        sleep(delay)
        return call()


def _is_per_second_rate_limit(exc: KisApiError) -> bool:
    message = str(exc)
    return KIS_PER_SECOND_RATE_LIMIT_CODE in message or KIS_PER_SECOND_RATE_LIMIT_TEXT in message


def _safe_error_message(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, ValueError) and message.startswith("missing KIS VTS credentials:"):
        return message
    return f"KIS VTS smoke failed: {type(exc).__name__}"


if __name__ == "__main__":
    raise SystemExit(main())
