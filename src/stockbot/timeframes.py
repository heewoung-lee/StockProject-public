from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from .models import MarketBar


def aggregate_bars(bars: Sequence[MarketBar], minutes: int) -> list[MarketBar]:
    if minutes <= 0:
        raise ValueError("minutes must be positive")

    sorted_bars = sorted(bars, key=lambda bar: bar.timestamp)
    if not sorted_bars:
        return []

    symbol = sorted_bars[0].symbol
    if any(bar.symbol != symbol for bar in sorted_bars):
        raise ValueError("all bars must have the same symbol")
    seen_timestamps: set[datetime] = set()
    for bar in sorted_bars:
        if bar.timestamp in seen_timestamps:
            raise ValueError("duplicate symbol timestamp rows are not allowed")
        seen_timestamps.add(bar.timestamp)

    aggregated: list[MarketBar] = []
    bucket_start: datetime | None = None
    bucket: list[MarketBar] = []

    for bar in sorted_bars:
        current_start = _bucket_start(bar.timestamp, minutes)
        if bucket_start is None:
            bucket_start = current_start
        elif current_start != bucket_start:
            aggregated.append(_aggregate_bucket(bucket, bucket_start))
            bucket_start = current_start
            bucket = []
        bucket.append(bar)

    if bucket_start is not None:
        aggregated.append(_aggregate_bucket(bucket, bucket_start))

    return aggregated


def _bucket_start(timestamp: datetime, minutes: int) -> datetime:
    day_start = timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed_minutes = timestamp.hour * 60 + timestamp.minute
    bucket_minutes = (elapsed_minutes // minutes) * minutes
    return day_start + timedelta(minutes=bucket_minutes)


def _aggregate_bucket(bucket: Sequence[MarketBar], timestamp: datetime) -> MarketBar:
    first = bucket[0]
    last = bucket[-1]
    volume = sum(bar.volume for bar in bucket)
    return MarketBar(
        symbol=first.symbol,
        timestamp=timestamp,
        open=first.open,
        high=max(bar.high for bar in bucket),
        low=min(bar.low for bar in bucket),
        close=last.close,
        volume=volume,
        vwap=_aggregate_vwap(bucket, volume),
        bid=last.bid,
        ask=last.ask,
    )


def _aggregate_vwap(bucket: Sequence[MarketBar], volume: int) -> Decimal:
    if volume <= 0:
        return bucket[-1].vwap
    weighted_total = sum((bar.vwap * Decimal(bar.volume) for bar in bucket), Decimal("0"))
    return weighted_total / Decimal(volume)
