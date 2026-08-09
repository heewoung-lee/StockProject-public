from __future__ import annotations

import argparse
from itertools import islice
from pathlib import Path

from .broker import PaperBroker
from .config import BotConfig, load_config
from .execution import ExecutionEngine, ExecutionSettings
from .journal import CsvTradeJournal
from .market_data import read_csv_bars
from .risk import RiskConfig, RiskManager
from .strategy import FlowScalperConfig, FlowScalperStrategy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local paper trading bot.")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--once", action="store_true", help="Process one market bar and stop")
    parser.add_argument("--max-bars", type=int, default=None, help="Maximum bars to process")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    config.validate_safety()
    if config.trading_mode != "paper":
        raise ValueError("execution loop only supports paper mode; use KIS client separately for kis-vts smoke tests")

    bars = read_csv_bars(config.data_path)
    limit = 1 if args.once else args.max_bars
    if limit is not None:
        bars = islice(bars, limit)

    engine = build_engine(config)
    engine.run(bars)
    print(f"mode={config.trading_mode} journal={config.journal_path}")
    return 0


def build_engine(config: BotConfig) -> ExecutionEngine:
    broker = PaperBroker(config.initial_cash)
    strategy = FlowScalperStrategy(
        FlowScalperConfig(
            momentum_window=config.momentum_window,
            min_momentum_pct=config.min_momentum_pct,
            min_signal_confidence=config.min_signal_confidence,
            volume_window=config.volume_window,
            min_volume_ratio=config.min_volume_ratio,
            max_spread_bps=config.max_spread_bps,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            trailing_stop_pct=config.trailing_stop_pct,
            transaction_tax_pct=config.transaction_tax_pct,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            min_net_profit_pct=config.min_net_profit_pct,
            max_holding_minutes=config.max_holding_minutes,
            daily_loss_exit_amount=config.daily_loss_exit_amount,
            forced_exit_time=config.forced_exit_time,
        )
    )
    risk_manager = RiskManager(
        RiskConfig(
            max_order_amount=config.max_order_amount,
            max_position_amount=config.max_position_amount,
            max_positions=config.max_positions,
            max_daily_loss=config.max_daily_loss,
            max_daily_entries_per_symbol=config.max_daily_entries_per_symbol,
            max_consecutive_order_failures=config.max_consecutive_order_failures,
            kill_switch=config.kill_switch,
        )
    )
    journal = CsvTradeJournal(Path(config.journal_path), mode=config.trading_mode)
    return ExecutionEngine(
        broker=broker,
        strategy=strategy,
        risk_manager=risk_manager,
        journal=journal,
        settings=ExecutionSettings(
            order_cash_amount=min(
                config.initial_cash,
                config.max_position_amount
                if config.max_position_amount > 0
                else config.initial_cash,
            )
        ),
    )

if __name__ == "__main__":
    raise SystemExit(main())
