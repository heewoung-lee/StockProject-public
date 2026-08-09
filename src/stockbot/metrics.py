from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .models import AccountSnapshot, Fill


@dataclass(frozen=True)
class PaperPerformanceMetrics:
    cash: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    open_positions: int
    long_positions: int
    short_positions: int
    filled_trades: int
    rejected_trades: int
    winning_exits: int
    losing_exits: int
    win_rate_pct: Decimal
    flat_exits: int = 0


def account_unrealized_pnl(account: AccountSnapshot) -> Decimal:
    return sum((position.unrealized_pnl for position in account.positions.values()), Decimal("0"))


class PaperMetricsTracker:
    def __init__(self) -> None:
        self.filled_trades = 0
        self.rejected_trades = 0
        self.winning_exits = 0
        self.losing_exits = 0
        self.flat_exits = 0

    def record_fill(self, fill: Fill) -> None:
        if not fill.accepted:
            self.record_rejection()
            return

        self.filled_trades += 1
        if fill.order.side not in {"SELL", "SHORT_EXIT"}:
            return
        if fill.realized_pnl > 0:
            self.winning_exits += 1
        elif fill.realized_pnl < 0:
            self.losing_exits += 1
        else:
            self.flat_exits += 1

    def record_rejection(self) -> None:
        self.rejected_trades += 1

    def snapshot(self, account: AccountSnapshot) -> PaperPerformanceMetrics:
        unrealized_pnl = account_unrealized_pnl(account)
        long_positions = sum(1 for position in account.positions.values() if position.side == "LONG")
        short_positions = sum(1 for position in account.positions.values() if position.side == "SHORT")
        exit_count = self.winning_exits + self.losing_exits + self.flat_exits
        win_rate = (
            (Decimal(self.winning_exits) / Decimal(exit_count)) * Decimal("100")
            if exit_count
            else Decimal("0")
        )
        return PaperPerformanceMetrics(
            cash=account.free_cash,
            equity=account.equity,
            realized_pnl=account.realized_pnl_today,
            unrealized_pnl=unrealized_pnl,
            total_pnl=account.realized_pnl_today + unrealized_pnl,
            open_positions=len(account.positions),
            long_positions=long_positions,
            short_positions=short_positions,
            filled_trades=self.filled_trades,
            rejected_trades=self.rejected_trades,
            winning_exits=self.winning_exits,
            losing_exits=self.losing_exits,
            win_rate_pct=win_rate,
            flat_exits=self.flat_exits,
        )
