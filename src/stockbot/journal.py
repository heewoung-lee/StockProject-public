from __future__ import annotations

import csv
from pathlib import Path

from .models import AccountSnapshot, Fill, MarketBar, Order


class CsvTradeJournal:
    fieldnames = [
        "timestamp",
        "event",
        "symbol",
        "side",
        "order_quantity",
        "order_price",
        "fill_quantity",
        "fill_price",
        "trade_pnl",
        "reason",
        "reject_reason",
        "cash",
        "realized_pnl_today",
        "mode",
    ]

    def __init__(self, path: str | Path, mode: str = "paper"):
        self.path = Path(path)
        self.mode = mode
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
                writer.writeheader()

    def record_fill(self, fill: Fill, account: AccountSnapshot) -> None:
        self._write_row(
            timestamp=fill.timestamp.isoformat(),
            event="FILL",
            symbol=fill.order.symbol,
            side=fill.order.side,
            order_quantity=str(fill.order.quantity),
            order_price=str(fill.price),
            fill_quantity=str(fill.quantity),
            fill_price=str(fill.price),
            trade_pnl=str(fill.realized_pnl),
            reason=fill.order.reason,
            reject_reason="",
            cash=str(account.cash),
            realized_pnl_today=str(account.realized_pnl_today),
            mode=self.mode,
        )

    def record_reject(
        self,
        order: Order,
        bar: MarketBar,
        account: AccountSnapshot,
        reject_reason: str,
        order_price,
    ) -> None:
        self._write_row(
            timestamp=bar.timestamp.isoformat(),
            event="REJECT",
            symbol=order.symbol,
            side=order.side,
            order_quantity=str(order.quantity),
            order_price=str(order_price),
            fill_quantity="0",
            fill_price="",
            trade_pnl="0",
            reason=order.reason,
            reject_reason=reject_reason,
            cash=str(account.cash),
            realized_pnl_today=str(account.realized_pnl_today),
            mode=self.mode,
        )

    def _write_row(self, **row: str) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fieldnames)
            writer.writerow(row)
            handle.flush()
