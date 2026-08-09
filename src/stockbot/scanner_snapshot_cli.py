from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

from .scanner_collector import (
    collect_http_scanner_snapshot,
    collect_naver_market_scanner_snapshot,
    parse_header_options,
    parse_key_value_options,
)
from .scanner_snapshot import SnapshotWriteOptions, write_scanner_snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert Kiwoom or external scanner exports into stockbot scanner_snapshot.json.",
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--input", help="Path to the collector JSON export")
    source_group.add_argument("--url", help="HTTP endpoint returning collector JSON")
    source_group.add_argument(
        "--naver-market",
        action="store_true",
        help="Collect a no-key Naver mobile market snapshot before writing scanner_snapshot.json",
    )
    parser.add_argument("--output", default="data/scanner_snapshot.json", help="Path to write scanner_snapshot.json")
    parser.add_argument("--provider", default="external-json", help="Provider label stored in the snapshot")
    parser.add_argument("--max-candidates", type=int, default=None, help="Maximum candidates to keep after filtering")
    parser.add_argument("--min-price", type=_decimal_arg, default=None, help="Drop candidates cheaper than this price")
    parser.add_argument("--max-price", type=_decimal_arg, default=None, help="Drop candidates more expensive than this price")
    parser.add_argument("--min-volume", type=int, default=None, help="Drop candidates below this volume")
    parser.add_argument(
        "--market",
        action="append",
        choices=("all", "kospi", "kosdaq"),
        default=[],
        help="Market for --naver-market. Repeat for multiple markets. Default: all",
    )
    parser.add_argument("--pages", type=int, default=0, help="Pages per market for --naver-market. 0 means all pages")
    parser.add_argument("--page-size", type=int, default=100, help="Page size for --naver-market, capped at 100")
    parser.add_argument("--header", action="append", default=[], help="HTTP header for --url, e.g. 'X-Key: value'")
    parser.add_argument("--query", action="append", default=[], help="HTTP query for --url, e.g. 'market=KOSPI'")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds for --url")
    parser.add_argument("--compact", action="store_true", help="Write compact JSON instead of pretty JSON")
    args = parser.parse_args(argv)

    try:
        options = SnapshotWriteOptions(
            provider=args.provider,
            max_candidates=args.max_candidates,
            min_price=args.min_price,
            max_price=args.max_price,
            min_volume=args.min_volume,
            pretty=not args.compact,
        )
        if args.naver_market:
            written_count = collect_naver_market_scanner_snapshot(
                args.output,
                options,
                markets=tuple(args.market) if args.market else ("all",),
                pages=args.pages,
                page_size=args.page_size,
                timeout=args.timeout,
            )
        elif args.url:
            written_count = collect_http_scanner_snapshot(
                args.url,
                args.output,
                options,
                headers=parse_header_options(args.header),
                query=parse_key_value_options(args.query),
                timeout=args.timeout,
            )
        else:
            written_count = write_scanner_snapshot(args.input, args.output, options)
    except Exception as exc:
        print(f"stockbot-scanner-snapshot failed: {exc.__class__.__name__}", file=sys.stderr)
        return 1

    print(f"wrote {written_count} candidates to {Path(args.output).name}")
    return 0


def _decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid decimal: {value}") from exc


if __name__ == "__main__":
    raise SystemExit(main())
