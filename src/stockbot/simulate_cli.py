from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .runtime_factory import (
    _build_paper_runtime,
    _configured_market_hours_from_config,
    _load_app_config,
    _load_default_symbol_directory,
)
from .simulation import run_local_simulation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run fast local paper simulations without KIS order APIs.")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--cycles", type=int, default=100, help="Number of local replay cycles to run")
    parser.add_argument(
        "--keep-market-hours",
        action="store_true",
        help="Respect configured market-hours gate instead of forcing local replay",
    )
    args = parser.parse_args(argv)

    try:
        config = _load_app_config(Path(args.config))
        if config.trading_mode != "paper":
            raise ValueError("local simulation only supports trading_mode=paper")

        runtime = _build_paper_runtime(
            config,
            _load_default_symbol_directory(),
            data_path=config.data_path,
            rate_limiter=None,
        )
        if args.keep_market_hours:
            runtime.market_hours = _configured_market_hours_from_config(config)
        report = run_local_simulation(
            runtime,
            cycles=args.cycles,
            ignore_market_hours=not args.keep_market_hours,
            ignore_rate_limits=True,
        )
    except Exception as exc:
        print(f"stockbot-simulate failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(report.to_json_dict(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
