from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .models import MarketBar


_VALID_LABELS = {"uptrend", "downtrend", "range", "unknown"}
_VALID_DIRECTIONS = {"up", "down", "flat", "unknown"}


@dataclass(frozen=True)
class MarketRegime:
    label: str
    direction: str
    volatility: float
    confidence: float
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.label not in _VALID_LABELS:
            raise ValueError("market regime label is invalid")
        if self.direction not in _VALID_DIRECTIONS:
            raise ValueError("market regime direction is invalid")
        object.__setattr__(self, "volatility", _clamp_float(self.volatility))
        object.__setattr__(self, "confidence", _clamp_float(self.confidence))
        object.__setattr__(self, "reasons", tuple(self.reasons))


class MarketRegimeDetector:
    def __init__(
        self,
        *,
        min_bars: int = 6,
        trend_threshold: Decimal = Decimal("0.02"),
        range_threshold: Decimal = Decimal("0.015"),
        low_volatility_threshold: Decimal = Decimal("0.04"),
        full_volatility_threshold: Decimal = Decimal("0.10"),
    ) -> None:
        if min_bars < 2:
            raise ValueError("min_bars must be at least 2")
        self.min_bars = min_bars
        self.trend_threshold = trend_threshold
        self.range_threshold = range_threshold
        self.low_volatility_threshold = low_volatility_threshold
        self.full_volatility_threshold = full_volatility_threshold

    def detect(self, bars: Sequence[MarketBar]) -> MarketRegime:
        ordered_bars = _normalized_bars(bars)
        if len(ordered_bars) < self.min_bars:
            return MarketRegime(
                label="unknown",
                direction="unknown",
                volatility=self._volatility(ordered_bars),
                confidence=0.1,
                reasons=("insufficient_data",),
            )

        early, recent = self._split_window(ordered_bars)
        early_close_average = _average(bar.close for bar in early)
        if early_close_average <= 0:
            return MarketRegime(
                label="unknown",
                direction="unknown",
                volatility=self._volatility(ordered_bars),
                confidence=0.1,
                reasons=("invalid_price",),
            )

        latest_close = ordered_bars[-1].close
        close_change = (latest_close - early_close_average) / early_close_average
        volatility = self._volatility(ordered_bars)

        early_high_average = _average(bar.high for bar in early)
        recent_high_average = _average(bar.high for bar in recent)
        early_low_average = _average(bar.low for bar in early)
        recent_low_average = _average(bar.low for bar in recent)
        highs_and_lows_rising = recent_high_average > early_high_average and recent_low_average > early_low_average
        highs_and_lows_falling = recent_high_average < early_high_average and recent_low_average < early_low_average

        if close_change >= self.trend_threshold and highs_and_lows_rising:
            return MarketRegime(
                label="uptrend",
                direction="up",
                volatility=volatility,
                confidence=self._trend_confidence(close_change),
                reasons=("close_above_early_average", "higher_highs_and_lows"),
            )

        if close_change <= -self.trend_threshold and highs_and_lows_falling:
            return MarketRegime(
                label="downtrend",
                direction="down",
                volatility=volatility,
                confidence=self._trend_confidence(abs(close_change)),
                reasons=("close_below_early_average", "lower_highs_and_lows"),
            )

        raw_volatility = self._raw_volatility(ordered_bars)
        weak_direction = abs(close_change) <= self.range_threshold
        low_volatility = raw_volatility <= self.low_volatility_threshold
        if weak_direction and low_volatility:
            return MarketRegime(
                label="range",
                direction="flat",
                volatility=volatility,
                confidence=self._range_confidence(close_change, raw_volatility),
                reasons=("weak_direction", "low_volatility"),
            )

        reasons: list[str] = []
        if not weak_direction:
            reasons.append("directional_move_without_confirmation")
        if not low_volatility:
            reasons.append("high_volatility")
        if not reasons:
            reasons.append("mixed_signals")
        return MarketRegime(
            label="unknown",
            direction="unknown",
            volatility=volatility,
            confidence=0.3,
            reasons=tuple(reasons),
        )

    def _split_window(self, bars: Sequence[MarketBar]) -> tuple[Sequence[MarketBar], Sequence[MarketBar]]:
        window_size = len(bars) // 2
        return bars[:window_size], bars[-window_size:]

    def _volatility(self, bars: Sequence[MarketBar]) -> float:
        raw = self._raw_volatility(bars)
        if self.full_volatility_threshold <= 0:
            return 0.0
        return _clamp_float(raw / self.full_volatility_threshold)

    def _raw_volatility(self, bars: Sequence[MarketBar]) -> Decimal:
        if not bars:
            return Decimal("0")
        average_close = _average(bar.close for bar in bars)
        if average_close <= 0:
            return Decimal("0")
        price_range = max(bar.high for bar in bars) - min(bar.low for bar in bars)
        if price_range <= 0:
            return Decimal("0")
        return price_range / average_close

    def _trend_confidence(self, close_change: Decimal) -> float:
        if self.trend_threshold <= 0:
            return 0.6
        strength = _clamp_decimal(close_change / (self.trend_threshold * Decimal("4")))
        return _clamp_float(Decimal("0.6") + (strength * Decimal("0.4")))

    def _range_confidence(self, close_change: Decimal, raw_volatility: Decimal) -> float:
        direction_score = Decimal("1")
        if self.range_threshold > 0:
            direction_score = Decimal("1") - _clamp_decimal(abs(close_change) / self.range_threshold)
        volatility_score = Decimal("1")
        if self.low_volatility_threshold > 0:
            volatility_score = Decimal("1") - _clamp_decimal(raw_volatility / self.low_volatility_threshold)
        return _clamp_float(
            Decimal("0.55") + (direction_score * Decimal("0.25")) + (volatility_score * Decimal("0.20"))
        )


def _average(values) -> Decimal:
    values = tuple(values)
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _normalized_bars(bars: Sequence[MarketBar]) -> list[MarketBar]:
    ordered = sorted(bars, key=lambda bar: bar.timestamp)
    if not ordered:
        return []
    symbol = ordered[0].symbol
    if any(bar.symbol != symbol for bar in ordered):
        raise ValueError("market regime bars must have the same symbol")
    return ordered


def _clamp_decimal(value: Decimal) -> Decimal:
    return min(Decimal("1"), max(Decimal("0"), value))


def _clamp_float(value: Decimal | float) -> float:
    return float(min(Decimal("1"), max(Decimal("0"), Decimal(str(value)))))


__all__ = ["MarketRegime", "MarketRegimeDetector"]
