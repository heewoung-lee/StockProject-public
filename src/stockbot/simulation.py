from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .runtime import PaperTradingRuntime


@dataclass(frozen=True)
class LocalSimulationReport:
    mode: str
    cycles_requested: int
    cycles_completed: int
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    open_positions: int
    filled_trades: int
    rejected_trades: int
    winning_exits: int
    losing_exits: int
    flat_exits: int
    win_rate_pct: Decimal

    def to_json_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "cycles_requested": self.cycles_requested,
            "cycles_completed": self.cycles_completed,
            "cash": str(self.cash),
            "equity": str(self.equity),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_pnl": str(self.total_pnl),
            "open_positions": self.open_positions,
            "filled_trades": self.filled_trades,
            "rejected_trades": self.rejected_trades,
            "winning_exits": self.winning_exits,
            "losing_exits": self.losing_exits,
            "flat_exits": self.flat_exits,
            "win_rate_pct": str(self.win_rate_pct),
        }


def run_local_simulation(
    runtime: PaperTradingRuntime,
    *,
    cycles: int,
    ignore_market_hours: bool = True,
    ignore_rate_limits: bool = True,
) -> LocalSimulationReport:
    cycles = int(cycles)
    if cycles <= 0:
        raise ValueError("cycles must be greater than 0")

    original_market_hours = runtime.market_hours
    original_rate_limiter = runtime.rate_limiter
    original_status = runtime.status
    started_by_simulation = not runtime.status.running
    starting_cycle_count = runtime.cycle_count
    try:
        if ignore_market_hours:
            runtime.market_hours = None
        if ignore_rate_limits:
            runtime.rate_limiter = None
        if started_by_simulation:
            runtime.start()
        for _ in range(cycles):
            runtime.run_cycle()
    finally:
        runtime.market_hours = original_market_hours
        runtime.rate_limiter = original_rate_limiter
        if started_by_simulation:
            runtime.status = original_status

    metrics = runtime.performance_metrics
    return LocalSimulationReport(
        mode="local-simulation",
        cycles_requested=cycles,
        cycles_completed=runtime.cycle_count - starting_cycle_count,
        cash=metrics.cash,
        equity=metrics.equity,
        realized_pnl=metrics.realized_pnl,
        unrealized_pnl=metrics.unrealized_pnl,
        total_pnl=metrics.total_pnl,
        open_positions=metrics.open_positions,
        filled_trades=metrics.filled_trades,
        rejected_trades=metrics.rejected_trades,
        winning_exits=metrics.winning_exits,
        losing_exits=metrics.losing_exits,
        flat_exits=metrics.flat_exits,
        win_rate_pct=metrics.win_rate_pct,
    )
