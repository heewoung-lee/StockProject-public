from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Sequence

from .models import MarketBar

Direction = Literal["long", "short", "hold"]


@dataclass(frozen=True)
class SignalScore:
    symbol: str
    long_score: float
    short_score: float
    confidence: float
    direction: Direction
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MarketFlowContext:
    volume_ratio: Decimal | None = None
    foreign_institution_net_amount: Decimal | None = None
    ranking_score: float | None = None
    short_pressure_score: float | None = None
    overextension_pct: Decimal | None = None
    spread_bps: Decimal | None = None

    def __post_init__(self) -> None:
        for name in ("volume_ratio", "foreign_institution_net_amount", "overextension_pct", "spread_bps"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, Decimal(str(value)))
        for name in ("ranking_score", "short_pressure_score"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _clamp(float(value)))


class SignalScorer:
    def __init__(
        self,
        min_confidence: float = 0.55,
        short_enabled: bool = True,
        max_spread_bps: Decimal = Decimal("30"),
        max_overextension_pct: Decimal = Decimal("0.10"),
    ) -> None:
        self.min_confidence = min_confidence
        self.short_enabled = short_enabled
        self.max_spread_bps = Decimal(str(max_spread_bps))
        self.max_overextension_pct = Decimal(str(max_overextension_pct))

    def score(
        self,
        symbol: str,
        bars: Sequence[MarketBar],
        regime: object,
        context: MarketFlowContext | None = None,
        *,
        volume_bars: Sequence[MarketBar] | None = None,
    ) -> SignalScore:
        if len(bars) < 3:
            return SignalScore(
                symbol=symbol,
                long_score=0.0,
                short_score=0.0,
                confidence=0.0,
                direction="hold",
                reasons=("insufficient_data",),
            )

        latest = bars[-1]
        previous = bars[-2]
        baseline = bars[-3]
        long_score = 0.0
        short_score = 0.0
        reasons: list[str] = []
        context = context or MarketFlowContext()

        if baseline.close > 0:
            momentum = (latest.close - baseline.close) / baseline.close
            if momentum > Decimal("0"):
                long_score += 0.35
                reasons.append("upward_momentum")
            elif momentum < Decimal("0"):
                short_score += 0.35
                reasons.append("downward_momentum")

        if latest.close > previous.close and latest.close >= latest.vwap:
            long_score += 0.25
            reasons.append("close_strength")
        elif latest.close < previous.close and latest.close <= latest.vwap:
            short_score += 0.25
            reasons.append("close_weakness")

        regime_direction = _regime_direction(regime)
        if regime_direction == "bullish":
            long_score += 0.20
            reasons.append("bullish_regime")
        elif regime_direction == "bearish":
            short_score += 0.20
            reasons.append("bearish_regime")

        volume_series = bars if volume_bars is None else volume_bars
        if len(volume_series) >= 3:
            latest_volume = volume_series[-1].volume
            previous_volume = Decimal(
                volume_series[-2].volume + volume_series[-3].volume
            ) / Decimal("2")
            if (
                previous_volume > 0
                and Decimal(latest_volume) >= previous_volume * Decimal("1.2")
            ):
                if long_score > short_score:
                    long_score += 0.20
                elif short_score > long_score:
                    short_score += 0.20
                reasons.append("volume_expansion")

        if context.volume_ratio is not None and context.volume_ratio >= Decimal("1.5"):
            long_score, short_score = _boost_leader(long_score, short_score, 0.10)
            reasons.append("kis_volume_surge")

        if context.foreign_institution_net_amount is not None:
            if context.foreign_institution_net_amount > 0:
                long_score += 0.10
                reasons.append("foreign_institution_net_buy")
            elif context.foreign_institution_net_amount < 0:
                short_score += 0.10
                reasons.append("foreign_institution_net_sell")

        if context.ranking_score is not None and context.ranking_score >= 0.70:
            long_score, short_score = _boost_leader(long_score, short_score, 0.10)
            reasons.append("kis_rank_strength")

        if context.short_pressure_score is not None and context.short_pressure_score >= 0.70:
            short_score += 0.10
            reasons.append("short_pressure")

        confidence = _clamp(max(long_score, short_score))
        direction: Direction = "hold"
        if long_score >= self.min_confidence and long_score > short_score:
            direction = "long"
        elif short_score >= self.min_confidence and short_score > long_score:
            if self.short_enabled:
                direction = "short"
            else:
                reasons.append("short_disabled")
        if direction == "hold" and max(long_score, short_score) < self.min_confidence:
            reasons.append("signal_confidence_below_minimum")

        if self._is_risk_blocked(latest, context):
            direction = "hold"
            confidence = min(confidence, max(0.0, self.min_confidence - 0.01))
            reasons.extend(self._risk_reasons(latest, context))

        return SignalScore(
            symbol=symbol,
            long_score=_clamp(long_score),
            short_score=_clamp(short_score),
            confidence=confidence,
            direction=direction,
            reasons=tuple(reasons) if reasons else ("no_signal",),
        )

    def _is_risk_blocked(self, latest: MarketBar, context: MarketFlowContext) -> bool:
        return bool(self._risk_reasons(latest, context))

    def _risk_reasons(self, latest: MarketBar, context: MarketFlowContext) -> tuple[str, ...]:
        reasons: list[str] = []
        spread_bps = context.spread_bps if context.spread_bps is not None else latest.spread_bps
        if spread_bps > self.max_spread_bps:
            reasons.append("wide_spread")

        if context.overextension_pct is not None and abs(context.overextension_pct) >= self.max_overextension_pct:
            reasons.append("overextended_move")
        return tuple(reasons)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _boost_leader(long_score: float, short_score: float, amount: float) -> tuple[float, float]:
    if long_score > short_score:
        return long_score + amount, short_score
    if short_score > long_score:
        return long_score, short_score + amount
    return long_score, short_score


def _regime_direction(regime: object) -> str:
    value = getattr(regime, "direction", None)
    if value is None:
        value = getattr(regime, "label", None)
    if value is None:
        return "neutral"

    normalized = str(value).strip().lower()
    if normalized in {"bull", "bullish", "up", "uptrend", "rising", "long"}:
        return "bullish"
    if normalized in {"bear", "bearish", "down", "downtrend", "falling", "short"}:
        return "bearish"
    return "neutral"
