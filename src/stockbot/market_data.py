from __future__ import annotations

import csv
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from .models import MarketBar


REQUIRED_COLUMNS = {"timestamp", "symbol", "open", "high", "low", "close", "volume", "vwap"}


def read_csv_bars(path: str | Path) -> Iterable[MarketBar]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"missing required columns: {', '.join(sorted(missing))}")
        bars = [_row_to_bar(row) for row in reader]

    yield from sorted(bars, key=lambda item: item.timestamp)


def _row_to_bar(row: dict[str, str]) -> MarketBar:
    return MarketBar(
        symbol=row["symbol"],
        timestamp=datetime.fromisoformat(row["timestamp"]),
        open=Decimal(row["open"]),
        high=Decimal(row["high"]),
        low=Decimal(row["low"]),
        close=Decimal(row["close"]),
        volume=int(row["volume"]),
        vwap=Decimal(row["vwap"]),
        bid=_optional_decimal(row.get("bid")),
        ask=_optional_decimal(row.get("ask")),
    )


def _optional_decimal(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)
