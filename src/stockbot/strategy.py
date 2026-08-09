from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Callable

from .market_regime import MarketRegimeDetector
from .models import AccountSnapshot, MarketBar, Signal
from .signal_scoring import MarketFlowContext, SignalScore, SignalScorer
from .trend import TrendBoundary, TrendBoundaryAnalyzer


MarketFlowContextProvider = Callable[[str], MarketFlowContext | None]


@dataclass(frozen=True)
class FlowScalperConfig:
    momentum_window: int = 3
    min_momentum_pct: Decimal = Decimal("0.01")
    min_short_momentum_pct: Decimal = Decimal("-0.01")
    min_signal_confidence: Decimal = Decimal("0.70")
    volume_window: int = 3
    min_volume_ratio: Decimal = Decimal("2")
    max_spread_bps: Decimal = Decimal("30")
    stop_loss_pct: Decimal = Decimal("0.02")
    take_profit_pct: Decimal = Decimal("0.03")
    trailing_stop_pct: Decimal = Decimal("0.015")
    max_holding_minutes: int = 0
    daily_loss_exit_amount: Decimal = Decimal("100000")
    forced_exit_time: str = ""
    allow_paper_short: bool = False
    trend_boundary_window: int = 3
    min_trend_pct: Decimal = Decimal("0.005")
    trend_channel_pct: Decimal = Decimal("0.01")
    transaction_tax_pct: Decimal = Decimal("0.002")
    commission_pct: Decimal = Decimal("0")
    slippage_pct: Decimal = Decimal("0.001")
    min_net_profit_pct: Decimal = Decimal("0.001")
    require_vwap_alignment: bool = True

    def __post_init__(self) -> None:
        for name in (
            "min_signal_confidence",
            "transaction_tax_pct",
            "commission_pct",
            "slippage_pct",
            "min_net_profit_pct",
        ):
            value = Decimal(str(getattr(self, name)))
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
            object.__setattr__(self, name, value)
        if self.min_signal_confidence > Decimal("1"):
            raise ValueError("min_signal_confidence must be between 0 and 1")
        if not isinstance(self.allow_paper_short, bool):
            raise ValueError("allow_paper_short must be boolean")
        if not isinstance(self.require_vwap_alignment, bool):
            raise ValueError("require_vwap_alignment must be boolean")
        if int(self.trend_boundary_window) < 2:
            raise ValueError("trend_boundary_window must be at least 2")


class FlowScalperStrategy:
    supports_provisional_live_scanner_quotes = True

    def __init__(
        self,
        config: FlowScalperConfig,
        *,
        flow_context_provider: MarketFlowContextProvider | None = None,
    ):
        self.config = config
        self._flow_context_provider = flow_context_provider
        self._history: dict[str, list[MarketBar]] = defaultdict(list)
        self._last_entry_scores: dict[str, SignalScore] = {}
        self._last_live_quotes: dict[str, MarketBar] = {}

    def seed_history(self, symbol: str, bars: list[MarketBar]) -> int:
        ordered = sorted(
            (bar for bar in bars if bar.symbol == symbol),
            key=lambda item: item.timestamp,
        )
        latest_by_minute = {
            _minute_bucket_key(bar.timestamp): bar
            for bar in ordered
        }
        ordered = sorted(latest_by_minute.values(), key=lambda item: item.timestamp)
        if not ordered:
            self.reset_history(symbol)
            return 0
        previous = self._history.get(symbol)
        if previous and _trading_date_key(previous[-1].timestamp) != _trading_date_key(
            ordered[-1].timestamp
        ):
            self._last_live_quotes.pop(symbol, None)
        self._history[symbol] = list(ordered)
        return len(ordered)

    def reset_history(self, symbol: str) -> None:
        self._history.pop(symbol, None)
        self._last_entry_scores.pop(symbol, None)
        self._last_live_quotes.pop(symbol, None)

    def on_bar(self, bar: MarketBar, account: AccountSnapshot) -> list[Signal]:
        try:
            return self._signals_for_bar(bar, account)
        finally:
            self._history[bar.symbol].append(bar)

    def on_live_bar(self, bar: MarketBar, account: AccountSnapshot) -> list[Signal]:
        history = self._history[bar.symbol]
        if history and _trading_date_key(history[-1].timestamp) != _trading_date_key(
            bar.timestamp
        ):
            self.reset_history(bar.symbol)
            history = self._history[bar.symbol]

        previous_quote = self._last_live_quotes.get(bar.symbol)
        if previous_quote is not None:
            if _timestamp_key(bar.timestamp) < _timestamp_key(previous_quote.timestamp):
                return []
            if _trading_date_key(previous_quote.timestamp) != _trading_date_key(
                bar.timestamp
            ):
                self._last_live_quotes.pop(bar.symbol, None)

        self._last_live_quotes[bar.symbol] = bar
        position = account.positions.get(bar.symbol)
        history_is_fresh = _completed_history_is_fresh(history, bar)
        if position is not None:
            return self._exit_signals(
                bar,
                position,
                account,
                allow_trend_boundary=history_is_fresh,
            )
        if not history_is_fresh:
            self._last_entry_scores[bar.symbol] = _hold_score(
                bar.symbol,
                "stale_completed_minute_history",
            )
            return []
        return self._entry_signals(bar, completed_volume_bar=history[-1])

    def _signals_for_bar(self, bar: MarketBar, account: AccountSnapshot) -> list[Signal]:
        position = account.positions.get(bar.symbol)
        if position is not None:
            return self._exit_signals(bar, position, account)
        return self._entry_signals(bar)

    def revalidate_signal(
        self,
        provisional_signal: Signal,
        provisional_bar: MarketBar,
        final_bar: MarketBar,
        account: AccountSnapshot,
    ) -> Signal | None:
        symbol = provisional_signal.symbol
        if symbol != provisional_bar.symbol or symbol != final_bar.symbol:
            return None

        history = self._history.get(symbol)
        if not history or history[-1] != provisional_bar:
            return None

        history.pop()
        final_signals = self.on_bar(final_bar, account)
        return next(
            (
                signal
                for signal in final_signals
                if signal.symbol == symbol and signal.side == provisional_signal.side
            ),
            None,
        )

    def revalidate_live_signal(
        self,
        provisional_signal: Signal,
        provisional_bar: MarketBar,
        final_bar: MarketBar,
        account: AccountSnapshot,
    ) -> Signal | None:
        symbol = provisional_signal.symbol
        if symbol != provisional_bar.symbol or symbol != final_bar.symbol:
            return None

        if self._last_live_quotes.get(symbol) != provisional_bar:
            return None
        if _minute_bucket_key(provisional_bar.timestamp) != _minute_bucket_key(
            final_bar.timestamp
        ):
            return None
        if _timestamp_key(final_bar.timestamp) < _timestamp_key(
            provisional_bar.timestamp
        ):
            return None

        final_signals = self.on_live_bar(final_bar, account)
        return next(
            (
                signal
                for signal in final_signals
                if signal.symbol == symbol and signal.side == provisional_signal.side
            ),
            None,
        )

    def _entry_signals(
        self,
        bar: MarketBar,
        *,
        completed_volume_bar: MarketBar | None = None,
    ) -> list[Signal]:
        history = self._history[bar.symbol]
        score_required = max(self.config.momentum_window, self.config.volume_window, 2)
        if completed_volume_bar is not None:
            score_required = max(score_required, 3)
            if self.config.min_volume_ratio > 0:
                score_required = max(score_required, self.config.volume_window + 1)
        if len(history) < score_required:
            self._last_entry_scores[bar.symbol] = _hold_score(
                bar.symbol,
                "insufficient_data",
            )
            return []

        if len(history) < self.config.trend_boundary_window:
            self._last_entry_scores[bar.symbol] = _hold_score(
                bar.symbol,
                "insufficient_trend_boundary",
            )
            return []

        reference_bar = history[-self.config.momentum_window]
        if reference_bar.close <= 0:
            self._last_entry_scores[bar.symbol] = SignalScore(
                symbol=bar.symbol,
                long_score=0.0,
                short_score=0.0,
                confidence=0.0,
                direction="hold",
                reasons=("invalid_reference_price",),
            )
            return []
        momentum = (bar.close - reference_bar.close) / reference_bar.close

        volume_ratio = Decimal("0")
        if self.config.min_volume_ratio > 0:
            if completed_volume_bar is None:
                volume_bars = history[-self.config.volume_window :]
                signal_volume = bar.volume
            else:
                if completed_volume_bar != history[-1]:
                    self._last_entry_scores[bar.symbol] = _hold_score(
                        bar.symbol,
                        "invalid_completed_volume_bar",
                    )
                    return []
                volume_bars = history[-(self.config.volume_window + 1) : -1]
                signal_volume = completed_volume_bar.volume
            average_volume = sum(item.volume for item in volume_bars) / len(volume_bars)
            if average_volume <= 0:
                self._last_entry_scores[bar.symbol] = _hold_score(
                    bar.symbol,
                    "invalid_average_volume",
                )
                return []
            volume_ratio = Decimal(str(signal_volume)) / Decimal(str(average_volume))
        score = self._score_entry(
            bar,
            history,
            self._flow_context_for(bar.symbol),
            volume_bars=history if completed_volume_bar is not None else None,
        )
        self._last_entry_scores[bar.symbol] = score
        prior_boundary = self._trend_boundary_for(bar.symbol)
        current_boundary = self._trend_boundary_for(bar.symbol, current_bar=bar)
        if prior_boundary is None or current_boundary is None:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "invalid_trend_boundary",
                direction="hold",
            )
            return []

        if _score_blocks_entry(score):
            return []
        if volume_ratio < self.config.min_volume_ratio:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "volume_below_minimum",
                direction="hold",
            )
            return []
        if bar.spread_bps > self.config.max_spread_bps:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "wide_spread",
                direction="hold",
            )
            return []

        if score.direction == "long" and momentum < self.config.min_momentum_pct:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "long_momentum_below_minimum",
                direction="hold",
            )
            return []
        if self.config.require_vwap_alignment and score.direction == "long" and bar.close < bar.vwap:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "below_vwap",
                direction="hold",
            )
            return []

        if (
            score.direction == "long"
            and momentum >= self.config.min_momentum_pct
            and (not self.config.require_vwap_alignment or bar.close >= bar.vwap)
        ):
            boundary = prior_boundary if prior_boundary.direction == "bullish" else current_boundary
            if boundary is not None:
                if not _entry_allowed_by_boundary(boundary, bar.close, "long"):
                    self._last_entry_scores[bar.symbol] = _score_with_reasons(
                        score,
                        _long_boundary_rejection_reason(boundary, bar.close),
                        direction="hold",
                    )
                    return []
                score = _score_with_reasons(score, "bullish_trend_boundary")
                self._last_entry_scores[bar.symbol] = score
            if not self._expected_net_profit_covers_costs(bar, boundary, "long"):
                self._last_entry_scores[bar.symbol] = _score_with_reasons(
                    score,
                    "expected_net_profit_below_costs",
                    direction="hold",
                )
                return []
            return [Signal.buy(bar.symbol, _flow_score_reason(score.long_score))]

        if score.direction == "short" and momentum > self.config.min_short_momentum_pct:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "short_momentum_below_minimum",
                direction="hold",
            )
            return []
        if self.config.require_vwap_alignment and score.direction == "short" and bar.close > bar.vwap:
            self._last_entry_scores[bar.symbol] = _score_with_reasons(
                score,
                "above_vwap",
                direction="hold",
            )
            return []

        if (
            self.config.allow_paper_short
            and score.direction == "short"
            and momentum <= self.config.min_short_momentum_pct
            and (not self.config.require_vwap_alignment or bar.close <= bar.vwap)
        ):
            boundary = prior_boundary if prior_boundary.direction == "bearish" else current_boundary
            if boundary is not None:
                if not _entry_allowed_by_boundary(boundary, bar.close, "short"):
                    self._last_entry_scores[bar.symbol] = _score_with_reasons(
                        score,
                        _short_boundary_rejection_reason(boundary, bar.close),
                        direction="hold",
                    )
                    return []
                score = _score_with_reasons(score, "bearish_trend_boundary")
                self._last_entry_scores[bar.symbol] = score
            if not self._expected_net_profit_covers_costs(bar, boundary, "short"):
                self._last_entry_scores[bar.symbol] = _score_with_reasons(
                    score,
                    "expected_net_profit_below_costs",
                    direction="hold",
                )
                return []
            return [Signal.short(bar.symbol, _flow_score_reason(score.short_score))]

        return []

    def last_entry_score(self, symbol: str) -> SignalScore | None:
        return self._last_entry_scores.get(symbol)

    def _score_entry(
        self,
        bar: MarketBar,
        history: list[MarketBar],
        context: MarketFlowContext | None,
        *,
        volume_bars: list[MarketBar] | None = None,
    ) -> SignalScore:
        bars = [*history, bar]
        try:
            regime = MarketRegimeDetector(min_bars=3).detect(bars)
        except ValueError:
            regime = object()
        return SignalScorer(
            min_confidence=float(self.config.min_signal_confidence),
            short_enabled=self.config.allow_paper_short,
            max_spread_bps=self.config.max_spread_bps,
        ).score(
            bar.symbol,
            bars,
            regime,
            context,
            volume_bars=volume_bars,
        )

    def _flow_context_for(self, symbol: str) -> MarketFlowContext | None:
        if self._flow_context_provider is None:
            return None
        return self._flow_context_provider(symbol)

    def _trend_boundary_for(self, symbol: str, current_bar: MarketBar | None = None) -> TrendBoundary | None:
        try:
            bars = self._history[symbol]
            if current_bar is not None:
                bars = [*bars, current_bar]
            return TrendBoundaryAnalyzer(
                window=self.config.trend_boundary_window,
                min_trend_pct=self.config.min_trend_pct,
                min_channel_pct=self.config.trend_channel_pct,
                projection_steps=0 if current_bar is not None else 1,
            ).project(bars)
        except ValueError:
            return None

    def _expected_net_profit_covers_costs(self, bar: MarketBar, boundary: TrendBoundary, side: str) -> bool:
        required_gross_pct = _required_gross_profit_pct(self.config)
        if required_gross_pct <= 0:
            return True

        entry_price = bar.buy_price if side == "long" else bar.sell_price
        if entry_price <= 0:
            return False

        if side == "long":
            target_price = _long_expected_target_price(entry_price, boundary, self.config.take_profit_pct)
            expected_gross_pct = (target_price - entry_price) / entry_price
        else:
            target_price = _short_expected_target_price(entry_price, boundary, self.config.take_profit_pct)
            expected_gross_pct = (entry_price - target_price) / entry_price

        return expected_gross_pct >= required_gross_pct

    def _exit_signals(
        self,
        bar: MarketBar,
        position,
        account: AccountSnapshot,
        *,
        allow_trend_boundary: bool = True,
    ) -> list[Signal]:
        exit_signal = Signal.cover if position.side == "SHORT" else Signal.sell
        forced_exit_time = _parse_optional_time(self.config.forced_exit_time)

        if forced_exit_time is not None and bar.timestamp.time() >= forced_exit_time:
            return [exit_signal(bar.symbol, "forced_exit")]

        boundary = (
            self._trend_boundary_for(bar.symbol)
            if allow_trend_boundary
            else None
        )
        if position.side == "SHORT":
            if boundary is not None:
                if boundary.touches_lower(bar.buy_price) and _short_profit_exit_covers_costs(
                    bar, position, self.config
                ):
                    return [Signal.cover(bar.symbol, "lower_trend_boundary")]
                if boundary.touches_upper(bar.buy_price) and _short_defensive_exit_has_moved(bar, position):
                    return [Signal.cover(bar.symbol, "upper_trend_boundary")]

            if bar.close >= position.avg_price * (Decimal("1") + self.config.stop_loss_pct):
                return [Signal.cover(bar.symbol, "stop_loss")]
            if (
                bar.close <= position.avg_price * (Decimal("1") - self.config.take_profit_pct)
                and _short_profit_exit_covers_costs(bar, position, self.config)
            ):
                return [Signal.cover(bar.symbol, "take_profit")]

            low_water = min(position.lowest_price or position.last_price, bar.close)
            if (
                low_water < position.avg_price
                and bar.close >= low_water * (Decimal("1") + self.config.trailing_stop_pct)
                and _short_profit_exit_covers_costs(bar, position, self.config)
            ):
                return [Signal.cover(bar.symbol, "trailing_stop")]

            held_for = bar.timestamp - position.opened_at
            if _max_holding_enabled(self.config.max_holding_minutes) and (
                held_for.total_seconds() / 60 >= self.config.max_holding_minutes
            ):
                return [Signal.cover(bar.symbol, "max_holding_time")]

            return []

        if boundary is not None:
            if boundary.touches_upper(bar.sell_price) and _long_profit_exit_covers_costs(
                bar, position, self.config
            ):
                return [Signal.sell(bar.symbol, "upper_trend_boundary")]
            if boundary.touches_lower(bar.sell_price) and _long_defensive_exit_has_moved(bar, position):
                return [Signal.sell(bar.symbol, "lower_trend_boundary")]

        if bar.close <= position.avg_price * (Decimal("1") - self.config.stop_loss_pct):
            return [Signal.sell(bar.symbol, "stop_loss")]
        if (
            bar.close >= position.avg_price * (Decimal("1") + self.config.take_profit_pct)
            and _long_profit_exit_covers_costs(bar, position, self.config)
        ):
            return [Signal.sell(bar.symbol, "take_profit")]

        high_water = max(position.highest_price, bar.close)
        if (
            high_water > position.avg_price
            and bar.close <= high_water * (Decimal("1") - self.config.trailing_stop_pct)
            and _long_profit_exit_covers_costs(bar, position, self.config)
        ):
            return [Signal.sell(bar.symbol, "trailing_stop")]

        held_for = bar.timestamp - position.opened_at
        if _max_holding_enabled(self.config.max_holding_minutes) and (
            held_for.total_seconds() / 60 >= self.config.max_holding_minutes
        ):
            return [Signal.sell(bar.symbol, "max_holding_time")]

        return []


def _max_holding_enabled(value: int) -> bool:
    return int(value) > 0


def _minute_bucket_key(timestamp: datetime) -> int:
    return int(_timestamp_key(timestamp) // 60)


def _trading_date_key(timestamp: datetime) -> str:
    return timestamp.date().isoformat()


def _completed_history_is_fresh(
    history: list[MarketBar],
    current_bar: MarketBar,
) -> bool:
    if not history:
        return False
    latest_completed = history[-1]
    if not (
        _trading_date_key(latest_completed.timestamp)
        == _trading_date_key(current_bar.timestamp)
        and _minute_bucket_key(latest_completed.timestamp)
        == _minute_bucket_key(current_bar.timestamp) - 1
    ):
        return False
    buckets = [_minute_bucket_key(bar.timestamp) for bar in history]
    return all(
        current == previous + 1
        for previous, current in zip(buckets, buckets[1:])
    )


def _timestamp_key(timestamp: datetime) -> float:
    normalized = timestamp
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    return normalized.timestamp()


def _hold_score(symbol: str, reason: str) -> SignalScore:
    return SignalScore(
        symbol=symbol,
        long_score=0.0,
        short_score=0.0,
        confidence=0.0,
        direction="hold",
        reasons=(reason,),
    )


def _parse_optional_time(value: str) -> time | None:
    normalized = str(value).strip().lower()
    if normalized in {"", "0", "off", "none", "false"}:
        return None
    return _parse_time(normalized)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(hour=int(hour), minute=int(minute))


def _flow_score_reason(score: float) -> str:
    return f"flow_score_{round(score * 100)}"


def _score_blocks_entry(score: SignalScore) -> bool:
    return score.direction == "hold" or any(reason in {"wide_spread", "overextended_move"} for reason in score.reasons)


def _entry_allowed_by_boundary(boundary: TrendBoundary, price: Decimal, side: str) -> bool:
    breakout_buffer = _boundary_half_width(boundary)
    if side == "long":
        return boundary.direction == "bullish" and boundary.lower <= price <= boundary.upper + breakout_buffer
    return boundary.direction == "bearish" and boundary.lower - breakout_buffer <= price <= boundary.upper


def _long_boundary_rejection_reason(boundary: TrendBoundary, price: Decimal) -> str:
    if boundary.direction != "bullish":
        return "not_bullish_trend_boundary"
    if boundary.touches_upper(price):
        return "above_upper_trend_boundary"
    if boundary.touches_lower(price):
        return "below_lower_trend_boundary"
    return "outside_trend_boundary"


def _short_boundary_rejection_reason(boundary: TrendBoundary, price: Decimal) -> str:
    if boundary.direction != "bearish":
        return "not_bearish_trend_boundary"
    if boundary.touches_lower(price):
        return "below_lower_trend_boundary"
    if boundary.touches_upper(price):
        return "above_upper_trend_boundary"
    return "outside_trend_boundary"


def _score_with_reasons(score: SignalScore, *reasons: str, direction: str | None = None) -> SignalScore:
    existing = list(score.reasons)
    for reason in reasons:
        if reason and reason not in existing:
            existing.append(reason)
    confidence = score.confidence
    if direction == "hold":
        confidence = min(confidence, 0.69)
    return SignalScore(
        symbol=score.symbol,
        long_score=score.long_score,
        short_score=score.short_score,
        confidence=confidence,
        direction=direction or score.direction,
        reasons=tuple(existing),
    )


def _boundary_half_width(boundary: TrendBoundary) -> Decimal:
    return (boundary.upper - boundary.lower) / Decimal("2")


def _required_gross_profit_pct(config: FlowScalperConfig) -> Decimal:
    return (
        config.transaction_tax_pct
        + (config.commission_pct * Decimal("2"))
        + (config.slippage_pct * Decimal("2"))
        + config.min_net_profit_pct
    )


def _long_profit_exit_covers_costs(bar: MarketBar, position, config: FlowScalperConfig) -> bool:
    return _profit_exit_price_covers_costs(
        executable_price=bar.sell_price,
        entry_price=position.avg_price,
        config=config,
        side="long",
    )


def _short_profit_exit_covers_costs(bar: MarketBar, position, config: FlowScalperConfig) -> bool:
    return _profit_exit_price_covers_costs(
        executable_price=bar.buy_price,
        entry_price=position.avg_price,
        config=config,
        side="short",
    )


def _profit_exit_price_covers_costs(
    *,
    executable_price: Decimal,
    entry_price: Decimal,
    config: FlowScalperConfig,
    side: str,
) -> bool:
    if executable_price <= 0 or entry_price <= 0:
        return False
    required_gross_pct = _required_gross_profit_pct(config)
    if required_gross_pct <= 0:
        if side == "short":
            return executable_price < entry_price
        return executable_price > entry_price
    if side == "short":
        return executable_price <= entry_price * (Decimal("1") - required_gross_pct)
    return executable_price >= entry_price * (Decimal("1") + required_gross_pct)


def _long_defensive_exit_has_moved(bar: MarketBar, position) -> bool:
    return bar.sell_price != position.avg_price


def _short_defensive_exit_has_moved(bar: MarketBar, position) -> bool:
    return bar.buy_price != position.avg_price


def _long_expected_target_price(entry_price: Decimal, boundary: TrendBoundary, take_profit_pct: Decimal) -> Decimal:
    take_profit_target = entry_price * (Decimal("1") + take_profit_pct)
    if boundary.upper > entry_price:
        return min(boundary.upper, take_profit_target)
    return take_profit_target


def _short_expected_target_price(entry_price: Decimal, boundary: TrendBoundary, take_profit_pct: Decimal) -> Decimal:
    take_profit_target = entry_price * (Decimal("1") - take_profit_pct)
    if boundary.lower < entry_price:
        return max(boundary.lower, take_profit_target)
    return take_profit_target
