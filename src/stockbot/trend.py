from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from .models import MarketBar


TrendDirection = str


@dataclass(frozen=True)
class TrendBoundary:
    direction: TrendDirection
    center: Decimal
    lower: Decimal
    upper: Decimal
    slope: Decimal

    def contains(self, price: Decimal) -> bool:
        return self.lower <= price <= self.upper

    def touches_upper(self, price: Decimal) -> bool:
        return price >= self.upper

    def touches_lower(self, price: Decimal) -> bool:
        return price <= self.lower


class TrendBoundaryAnalyzer:
    def __init__(
        self,
        *,
        window: int = 3,
        min_trend_pct: Decimal = Decimal("0.005"),
        min_channel_pct: Decimal = Decimal("0.01"),
        projection_steps: int = 1,
    ) -> None:
        if int(window) < 2:
            raise ValueError("window must be at least 2")
        if int(projection_steps) < 0:
            raise ValueError("projection_steps must be zero or greater")
        self.window = int(window)
        self.min_trend_pct = Decimal(str(min_trend_pct))
        self.min_channel_pct = Decimal(str(min_channel_pct))
        self.projection_steps = int(projection_steps)

    def project(self, bars: Sequence[MarketBar]) -> TrendBoundary:
        if len(bars) < self.window:
            raise ValueError("not enough bars for trend boundary")

        recent = list(bars[-self.window :])
        first = recent[0].close
        last = recent[-1].close
        if first <= 0:
            raise ValueError("first close must be positive")

        steps = Decimal(len(recent) - 1)
        slope = (last - first) / steps
        projected_center = last + (slope * Decimal(self.projection_steps))
        trend_pct = (last - first) / first
        direction = _direction_for(trend_pct, self.min_trend_pct)
        channel_half_width = self._channel_half_width(recent, first, slope, projected_center)

        return TrendBoundary(
            direction=direction,
            center=projected_center,
            lower=projected_center - channel_half_width,
            upper=projected_center + channel_half_width,
            slope=slope,
        )

    def _channel_half_width(
        self,
        bars: Sequence[MarketBar],
        first: Decimal,
        slope: Decimal,
        projected_center: Decimal,
    ) -> Decimal:
        max_deviation = Decimal("0")
        for index, bar in enumerate(bars):
            fitted = first + slope * Decimal(index)
            deviation = abs(bar.close - fitted)
            if deviation > max_deviation:
                max_deviation = deviation

        minimum_width = abs(projected_center) * self.min_channel_pct
        return max(max_deviation, minimum_width)


def _direction_for(trend_pct: Decimal, min_trend_pct: Decimal) -> TrendDirection:
    if trend_pct >= min_trend_pct:
        return "bullish"
    if trend_pct <= -min_trend_pct:
        return "bearish"
    return "flat"
