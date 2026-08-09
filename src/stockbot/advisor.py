from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import BotConfig
from .models import MarketBar
from .profiles import get_profile_settings


@dataclass(frozen=True)
class AdvisorRecommendation:
    recommended_profile: str
    confidence: str
    reasons: list[str]
    suggested_changes: dict[str, str]
    metrics: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return {
            "recommended_profile": self.recommended_profile,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "suggested_changes": self.suggested_changes,
            "metrics": self.metrics,
        }


class StrategyAdvisor:
    def recommend(self, config: BotConfig, bars: list[MarketBar]) -> AdvisorRecommendation:
        if len(bars) < 2:
            return _recommendation(
                config=config,
                profile="balanced",
                confidence="low",
                reasons=["not enough market bars to judge current flow"],
                metrics={"bar_count": str(len(bars))},
            )

        metrics = _market_metrics(bars)
        reasons: list[str] = []

        if metrics["average_spread_bps"] > config.max_spread_bps:
            reasons.append("spread is wider than the configured threshold")
        if metrics["volatility"] >= Decimal("0.025"):
            reasons.append("volatility is elevated")

        if reasons:
            confidence = "high" if len(reasons) >= 2 else "medium"
            return _recommendation(config, "conservative", confidence, reasons, _string_metrics(metrics))

        strong_momentum = metrics["momentum_pct"] >= config.min_momentum_pct
        strong_volume = metrics["volume_ratio"] >= config.min_volume_ratio
        clean_spread = metrics["average_spread_bps"] <= config.max_spread_bps
        if strong_momentum and strong_volume and clean_spread:
            return _recommendation(
                config=config,
                profile="aggressive",
                confidence="medium",
                reasons=["momentum and volume confirm a clean short-term flow"],
                metrics=_string_metrics(metrics),
            )

        return _recommendation(
            config=config,
            profile="balanced",
            confidence="medium",
            reasons=["market flow is mixed, so balanced settings are preferred"],
            metrics=_string_metrics(metrics),
        )


def _market_metrics(bars: list[MarketBar]) -> dict[str, Decimal]:
    first = bars[0]
    last = bars[-1]
    momentum_pct = _safe_ratio(last.close - first.close, first.close)
    returns = [
        abs(_safe_ratio(current.close - previous.close, previous.close))
        for previous, current in zip(bars, bars[1:])
    ]
    volatility = sum(returns, Decimal("0")) / Decimal(str(len(returns))) if returns else Decimal("0")
    average_spread_bps = sum((bar.spread_bps for bar in bars), Decimal("0")) / Decimal(str(len(bars)))
    previous_volume = sum((bar.volume for bar in bars[:-1]), 0) / max(len(bars) - 1, 1)
    volume_ratio = Decimal(str(last.volume)) / Decimal(str(previous_volume)) if previous_volume > 0 else Decimal("0")
    return {
        "momentum_pct": momentum_pct,
        "volatility": volatility,
        "average_spread_bps": average_spread_bps,
        "volume_ratio": volume_ratio,
    }


def _safe_ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= 0:
        return Decimal("0")
    return numerator / denominator


def _recommendation(
    config: BotConfig,
    profile: str,
    confidence: str,
    reasons: list[str],
    metrics: dict[str, str],
) -> AdvisorRecommendation:
    return AdvisorRecommendation(
        recommended_profile=profile,
        confidence=confidence,
        reasons=reasons,
        suggested_changes=_suggested_changes(config, profile),
        metrics=metrics,
    )


def _suggested_changes(config: BotConfig, profile: str) -> dict[str, str]:
    changes: dict[str, str] = {}
    for key, value in get_profile_settings(profile).items():
        current = getattr(config, key)
        if current != value:
            changes[key] = _format_value(value)
    return changes


def _string_metrics(metrics: dict[str, Decimal]) -> dict[str, str]:
    return {key: _format_value(value) for key, value in metrics.items()}


def _format_value(value: object) -> str:
    return str(value)

