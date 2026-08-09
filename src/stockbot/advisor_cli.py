from __future__ import annotations

import argparse
import json
import sys

from .advisor import StrategyAdvisor
from .config import load_config
from .market_data import read_csv_bars
from .models import MarketBar


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Recommend a strategy profile without placing orders.")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--max-bars", type=int, default=120, help="Maximum market bars to analyze")
    args = parser.parse_args(argv)

    try:
        recommendation = _run(args.config, args.max_bars)
    except Exception as exc:
        print(_safe_error_message(exc), file=sys.stderr)
        return 1

    print(json.dumps(recommendation.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _run(config_path: str, max_bars: int):
    config = load_config(config_path)
    bars = _latest_bars(list(read_csv_bars(config.data_path)), max_bars)
    return StrategyAdvisor().recommend(config, bars)


def _latest_bars(bars: list[MarketBar], max_bars: int) -> list[MarketBar]:
    if max_bars <= 0:
        return []
    return bars[-max_bars:]


def _safe_error_message(exc: Exception) -> str:
    return f"advisor failed: {type(exc).__name__}"


if __name__ == "__main__":
    raise SystemExit(main())
