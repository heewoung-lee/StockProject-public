import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.models import AccountSnapshot, MarketBar, Position
from stockbot.signal_scoring import MarketFlowContext, SignalScore
from stockbot.strategy import FlowScalperConfig, FlowScalperStrategy


def make_bar(offset, close, volume=1000, vwap=None, bid=None, ask=None):
    price = Decimal(str(close))
    return MarketBar(
        symbol="005930",
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        vwap=Decimal(str(vwap if vwap is not None else close)),
        bid=Decimal(str(bid if bid is not None else close)),
        ask=Decimal(str(ask if ask is not None else close)),
    )


class FlowScalperStrategyTest(unittest.TestCase):
    def test_position_rejects_invalid_side_values(self):
        with self.assertRaisesRegex(ValueError, "position side"):
            Position(
                symbol="005930",
                quantity=1,
                avg_price=Decimal("10000"),
                last_price=Decimal("10000"),
                opened_at=datetime(2026, 6, 8, 9, 0),
                highest_price=Decimal("10000"),
                side="SHORT_ENTRY",
            )

    def test_flow_scalper_config_rejects_non_bool_short_gate(self):
        with self.assertRaisesRegex(ValueError, "allow_paper_short must be boolean"):
            FlowScalperConfig(allow_paper_short="false")

    def test_flow_scalper_config_rejects_invalid_trend_boundary_window(self):
        with self.assertRaisesRegex(ValueError, "trend_boundary_window must be at least 2"):
            FlowScalperConfig(trend_boundary_window=1)

    def test_flow_scalper_config_enables_default_cost_filter(self):
        config = FlowScalperConfig()

        self.assertEqual(Decimal("0.002"), config.transaction_tax_pct)
        self.assertEqual(Decimal("0"), config.commission_pct)
        self.assertEqual(Decimal("0.001"), config.slippage_pct)
        self.assertEqual(Decimal("0.001"), config.min_net_profit_pct)

    def test_generates_buy_signal_when_flow_conditions_match(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        self.assertEqual([], strategy.on_bar(make_bar(0, 100, volume=1000), account))
        self.assertEqual([], strategy.on_bar(make_bar(1, 101, volume=1000), account))
        self.assertEqual([], strategy.on_bar(make_bar(2, 102, volume=1000), account))
        signals = strategy.on_bar(make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)
        self.assertRegex(signals[0].reason, r"^flow_score_\d+$")
        score = strategy.last_entry_score("005930")
        self.assertIsInstance(score, SignalScore)
        self.assertEqual("long", score.direction)

    def test_seed_history_lets_first_live_bar_use_prior_samples(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.seed_history(
            "005930",
            [
                make_bar(0, 100, volume=1000),
                make_bar(1, 101, volume=1000),
                make_bar(2, 102, volume=1000),
            ],
        )
        signals = strategy.on_bar(make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)

    def test_live_quotes_never_mutate_completed_minute_history(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        account = AccountSnapshot(cash=Decimal("1000000"))
        completed = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1200),
            make_bar(2, 102, volume=1400),
        ]
        strategy.seed_history("005930", completed)
        first = replace(
            make_bar(3, 103, volume=10000),
            timestamp=datetime(2026, 6, 8, 9, 3, 5),
        )
        latest = replace(first, timestamp=datetime(2026, 6, 8, 9, 3, 50), close=Decimal("104"))
        next_minute = replace(first, timestamp=datetime(2026, 6, 8, 9, 4, 2), close=Decimal("105"))

        strategy.on_live_bar(first, account)
        strategy.on_live_bar(latest, account)
        strategy.on_live_bar(next_minute, account)

        self.assertEqual(completed, strategy._history["005930"])

    def test_live_entry_uses_completed_minute_volume_not_quote_cumulative_volume(self):
        config = FlowScalperConfig(
            momentum_window=2,
            min_momentum_pct=Decimal("0.01"),
            volume_window=2,
            min_volume_ratio=Decimal("2"),
            max_spread_bps=Decimal("30"),
            transaction_tax_pct=Decimal("0"),
            slippage_pct=Decimal("0"),
            min_net_profit_pct=Decimal("0"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        completed = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=3000),
        ]
        low_cumulative = replace(
            make_bar(3, 104, volume=1, vwap=103, bid=103.9, ask=104.1),
            timestamp=datetime(2026, 6, 8, 9, 3, 20),
        )
        high_cumulative = replace(low_cumulative, volume=10000000)
        low_strategy = FlowScalperStrategy(config)
        high_strategy = FlowScalperStrategy(config)
        low_strategy.seed_history("005930", completed)
        high_strategy.seed_history("005930", completed)

        low_signals = low_strategy.on_live_bar(low_cumulative, account)
        high_signals = high_strategy.on_live_bar(high_cumulative, account)

        self.assertEqual(["BUY"], [signal.side for signal in low_signals])
        self.assertEqual(["BUY"], [signal.side for signal in high_signals])
        self.assertEqual(completed, low_strategy._history["005930"])
        self.assertEqual(completed, high_strategy._history["005930"])
        self.assertEqual(
            low_strategy.last_entry_score("005930"),
            high_strategy.last_entry_score("005930"),
        )

    def test_live_volume_override_preserves_latest_completed_price_for_scoring(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0"),
                min_signal_confidence=Decimal("0.70"),
                volume_window=2,
                min_volume_ratio=Decimal("0"),
                min_trend_pct=Decimal("0"),
                require_vwap_alignment=False,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        completed = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 100, volume=1000),
            make_bar(2, 101.8, volume=1000),
        ]
        strategy.seed_history("005930", completed)
        current = replace(
            make_bar(3, 101.5, volume=10000000, vwap=101),
            timestamp=datetime(2026, 6, 8, 9, 3, 10),
        )

        signals = strategy.on_live_bar(current, account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertEqual("hold", score.direction)
        self.assertNotIn("close_strength", score.reasons)
        self.assertEqual(completed, strategy._history["005930"])

    def test_live_entry_requires_the_immediately_previous_completed_minute(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0"),
                volume_window=2,
                min_volume_ratio=Decimal("0"),
                min_trend_pct=Decimal("0"),
                require_vwap_alignment=False,
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        completed = [
            make_bar(0, 100),
            make_bar(1, 101),
        ]
        strategy.seed_history("005930", completed)
        quote_after_gap = replace(
            make_bar(3, 104),
            timestamp=datetime(2026, 6, 8, 9, 3, 10),
        )

        signals = strategy.on_live_bar(quote_after_gap, account)

        self.assertEqual([], signals)
        self.assertEqual(
            ("stale_completed_minute_history",),
            strategy.last_entry_score("005930").reasons,
        )
        self.assertEqual(completed, strategy._history["005930"])

    def test_live_entry_rejects_a_gap_inside_completed_minute_history(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        completed_with_gap = [
            make_bar(0, 100, volume=1000),
            make_bar(2, 101, volume=1000),
            make_bar(3, 102, volume=3000),
        ]
        strategy.seed_history("005930", completed_with_gap)
        current = replace(
            make_bar(4, 104, volume=10000000, vwap=103, bid=103.9, ask=104.1),
            timestamp=datetime(2026, 6, 8, 9, 4, 10),
        )

        signals = strategy.on_live_bar(current, account)

        self.assertEqual([], signals)
        self.assertEqual(
            ("stale_completed_minute_history",),
            strategy.last_entry_score("005930").reasons,
        )
        self.assertEqual(completed_with_gap, strategy._history["005930"])

    def test_live_quote_on_new_trading_day_clears_prior_day_history(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        account = AccountSnapshot(cash=Decimal("1000000"))
        strategy.seed_history(
            "005930",
            [
                make_bar(0, 100),
                make_bar(1, 101),
                make_bar(2, 102),
            ],
        )
        next_day = replace(
            make_bar(3, 103),
            timestamp=datetime(2026, 6, 9, 9, 3, 10),
        )

        signals = strategy.on_live_bar(next_day, account)

        self.assertEqual([], signals)
        self.assertEqual([], strategy._history["005930"])

    def test_live_same_minute_quotes_recheck_every_price_and_time_exit(self):
        cases = (
            (
                "stop_loss",
                FlowScalperConfig(stop_loss_pct=Decimal("0.02")),
                Decimal("10000"),
                Decimal("9900"),
                Decimal("9800"),
                Decimal("10000"),
                datetime(2026, 6, 8, 9, 0),
            ),
            (
                "take_profit",
                FlowScalperConfig(take_profit_pct=Decimal("0.03")),
                Decimal("10000"),
                Decimal("10200"),
                Decimal("10300"),
                Decimal("10300"),
                datetime(2026, 6, 8, 9, 0),
            ),
            (
                "trailing_stop",
                FlowScalperConfig(
                    take_profit_pct=Decimal("0.10"),
                    trailing_stop_pct=Decimal("0.015"),
                ),
                Decimal("10000"),
                Decimal("10400"),
                Decimal("10300"),
                Decimal("10500"),
                datetime(2026, 6, 8, 9, 0),
            ),
            (
                "max_holding_time",
                FlowScalperConfig(max_holding_minutes=5),
                Decimal("10000"),
                Decimal("10050"),
                Decimal("10050"),
                Decimal("10050"),
                datetime(2026, 6, 8, 9, 0, 10),
            ),
        )

        for reason, config, average, first_price, latest_price, high_water, opened_at in cases:
            with self.subTest(reason=reason):
                strategy = FlowScalperStrategy(config)
                position = Position(
                    symbol="005930",
                    quantity=10,
                    avg_price=average,
                    last_price=average,
                    opened_at=opened_at,
                    highest_price=high_water,
                )
                account = AccountSnapshot(
                    cash=Decimal("900000"),
                    positions={"005930": position},
                    realized_pnl_today=Decimal("0"),
                )
                first = replace(
                    make_bar(5, first_price),
                    timestamp=datetime(2026, 6, 8, 9, 5, 5),
                )
                latest = replace(
                    make_bar(5, latest_price),
                    timestamp=datetime(2026, 6, 8, 9, 5, 20),
                )

                self.assertEqual([], strategy.on_live_bar(first, account))
                signals = strategy.on_live_bar(latest, account)

                self.assertEqual(1, len(signals))
                self.assertEqual("SELL", signals[0].side)
                self.assertEqual(reason, signals[0].reason)
                self.assertEqual([], strategy._history["005930"])

    def test_live_final_quote_revalidation_keeps_completed_history_immutable(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=3000),
        ]
        strategy.seed_history("005930", prior_bars)
        provisional_bar = replace(
            make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1),
            timestamp=datetime(2026, 6, 8, 9, 3, 20),
        )
        final_bar = replace(
            make_bar(3, 104, volume=3200, vwap=103, bid=103.8, ask=104),
            timestamp=datetime(2026, 6, 8, 9, 3, 25),
        )

        provisional_signal = strategy.on_live_bar(provisional_bar, account)[0]
        final_signal = strategy.revalidate_live_signal(
            provisional_signal,
            provisional_bar,
            final_bar,
            account,
        )

        self.assertIsNotNone(final_signal)
        self.assertEqual(provisional_signal.side, final_signal.side)
        self.assertEqual(prior_bars, strategy._history["005930"])

    def test_live_final_quote_revalidation_defers_across_minute_boundary(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=3000),
        ]
        strategy.seed_history("005930", prior_bars)
        provisional_bar = replace(
            make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1),
            timestamp=datetime(2026, 6, 8, 9, 3, 59),
        )
        final_bar = replace(
            make_bar(4, 104, volume=3200, vwap=103, bid=103.8, ask=104),
            timestamp=datetime(2026, 6, 8, 9, 4, 1),
        )

        provisional_signal = strategy.on_live_bar(provisional_bar, account)[0]
        final_signal = strategy.revalidate_live_signal(
            provisional_signal,
            provisional_bar,
            final_bar,
            account,
        )

        self.assertIsNone(final_signal)
        self.assertEqual(prior_bars, strategy._history["005930"])

    def test_live_final_quote_revalidation_rejects_older_quote(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=3000),
        ]
        strategy.seed_history("005930", prior_bars)
        provisional_bar = replace(
            make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1),
            timestamp=datetime(2026, 6, 8, 9, 3, 20),
        )
        older_final_bar = replace(
            provisional_bar,
            timestamp=datetime(2026, 6, 8, 9, 3, 15),
        )

        provisional_signal = strategy.on_live_bar(provisional_bar, account)[0]
        final_signal = strategy.revalidate_live_signal(
            provisional_signal,
            provisional_bar,
            older_final_bar,
            account,
        )
        equal_timestamp_bar = replace(
            provisional_bar,
            bid=Decimal("103.8"),
            ask=Decimal("104"),
        )
        equal_timestamp_signal = strategy.revalidate_live_signal(
            provisional_signal,
            provisional_bar,
            equal_timestamp_bar,
            account,
        )

        self.assertIsNone(final_signal)
        self.assertIsNotNone(equal_timestamp_signal)
        self.assertEqual(prior_bars, strategy._history["005930"])

    def test_live_out_of_order_bar_does_not_roll_history_back(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100),
            make_bar(1, 101),
        ]
        strategy.seed_history("005930", list(reversed(prior_bars)))
        latest = replace(
            make_bar(2, 102),
            timestamp=datetime(2026, 6, 8, 9, 2, 40),
        )
        stale = replace(
            make_bar(2, 99),
            timestamp=datetime(2026, 6, 8, 9, 2, 20),
        )
        strategy.on_live_bar(latest, account)
        history_before_stale = list(strategy._history["005930"])

        signals = strategy.on_live_bar(stale, account)

        self.assertEqual([], signals)
        self.assertEqual(history_before_stale, strategy._history["005930"])

    def test_paper_on_bar_keeps_each_quote_in_the_same_minute(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        account = AccountSnapshot(cash=Decimal("1000000"))
        first = replace(
            make_bar(0, 100),
            timestamp=datetime(2026, 6, 8, 9, 0, 5),
        )
        latest = replace(
            make_bar(0, 101),
            timestamp=datetime(2026, 6, 8, 9, 0, 50),
        )

        strategy.on_bar(first, account)
        strategy.on_bar(latest, account)

        self.assertEqual([first, latest], strategy._history["005930"])

    def test_revalidate_signal_replaces_provisional_tail_with_valid_final_bar(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=1000),
        ]
        strategy.seed_history("005930", prior_bars)
        provisional_bar = make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1)
        provisional_signal = strategy.on_bar(provisional_bar, account)[0]
        final_bar = make_bar(3, 104, volume=3200, vwap=103, bid=103.8, ask=104)

        final_signal = strategy.revalidate_signal(
            provisional_signal,
            provisional_bar,
            final_bar,
            account,
        )

        self.assertIsNotNone(final_signal)
        self.assertEqual(provisional_signal.side, final_signal.side)
        self.assertEqual([*prior_bars, final_bar], strategy._history["005930"])

    def test_revalidate_signal_keeps_final_bar_when_quote_flips_entry_side(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        prior_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=1000),
        ]
        strategy.seed_history("005930", prior_bars)
        provisional_bar = make_bar(3, 104, volume=3000, vwap=103, bid=103.9, ask=104.1)
        provisional_signal = strategy.on_bar(provisional_bar, account)[0]
        final_bar = make_bar(3, 98, volume=3000, vwap=99, bid=97.9, ask=98.1)

        final_signal = strategy.revalidate_signal(
            provisional_signal,
            provisional_bar,
            final_bar,
            account,
        )

        self.assertIsNone(final_signal)
        self.assertEqual("short", strategy.last_entry_score("005930").direction)
        self.assertEqual([*prior_bars, final_bar], strategy._history["005930"])

    def test_revalidate_signal_fails_closed_on_symbol_or_tail_mismatch(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"))
        strategy.seed_history(
            "005930",
            [
                make_bar(0, 100, volume=1000),
                make_bar(1, 101, volume=1000),
                make_bar(2, 102, volume=1000),
            ],
        )
        provisional_bar = make_bar(3, 104, volume=3000, vwap=103)
        provisional_signal = strategy.on_bar(provisional_bar, account)[0]
        final_bar = make_bar(3, 104, volume=3200, vwap=103)
        mismatches = (
            (replace(provisional_signal, symbol="000660"), provisional_bar, final_bar),
            (provisional_signal, replace(provisional_bar, symbol="000660"), final_bar),
            (provisional_signal, provisional_bar, replace(final_bar, symbol="000660")),
        )

        for signal, recorded_bar, confirmed_bar in mismatches:
            with self.subTest(signal=signal, recorded_bar=recorded_bar, confirmed_bar=confirmed_bar):
                history_before = {symbol: list(bars) for symbol, bars in strategy._history.items()}

                result = strategy.revalidate_signal(signal, recorded_bar, confirmed_bar, account)

                self.assertIsNone(result)
                self.assertEqual(history_before, strategy._history)

        strategy.on_bar(make_bar(4, 105, volume=1000), account)
        history_before = {symbol: list(bars) for symbol, bars in strategy._history.items()}

        result = strategy.revalidate_signal(provisional_signal, provisional_bar, final_bar, account)

        self.assertIsNone(result)
        self.assertEqual(history_before, strategy._history)

    def test_records_volume_rejection_reason_when_flow_score_is_strong_but_volume_ratio_low(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.01"),
                volume_window=2,
                min_volume_ratio=Decimal("2"),
                max_spread_bps=Decimal("30"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 104, volume=1000, vwap=103, bid=103.9, ask=104.1), account)

        self.assertEqual([], signals)
        score = strategy.last_entry_score("005930")
        self.assertIsInstance(score, SignalScore)
        self.assertEqual("hold", score.direction)
        self.assertIn("volume_below_minimum", score.reasons)

    def test_entry_score_uses_kis_flow_context_provider(self):
        def context_for(symbol):
            self.assertEqual("005930", symbol)
            return MarketFlowContext(
                volume_ratio=Decimal("2.2"),
                foreign_institution_net_amount=Decimal("100000000"),
                ranking_score=0.9,
            )

        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            ),
            flow_context_provider=context_for,
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1100), account)
        strategy.on_bar(make_bar(2, 102, volume=1200), account)
        strategy.on_bar(make_bar(3, 104, volume=3000, vwap=103), account)
        score = strategy.last_entry_score("005930")

        self.assertIsInstance(score, SignalScore)
        self.assertEqual("long", score.direction)
        self.assertIn("kis_volume_surge", score.reasons)
        self.assertIn("foreign_institution_net_buy", score.reasons)
        self.assertIn("kis_rank_strength", score.reasons)

    def test_kis_flow_context_risk_reasons_block_entry_signal(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(momentum_window=1, volume_window=1, min_volume_ratio=Decimal("1")),
            flow_context_provider=lambda symbol: MarketFlowContext(overextension_pct=Decimal("0.12")),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 104, volume=1200), account)
        strategy.on_bar(make_bar(2, 108, volume=1800), account)
        signals = strategy.on_bar(make_bar(3, 113, volume=3500, vwap=110), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("overextended_move", score.reasons)

    def test_kis_context_spread_uses_strategy_configured_threshold(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                max_spread_bps=Decimal("80"),
            ),
            flow_context_provider=lambda symbol: MarketFlowContext(spread_bps=Decimal("75")),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1100), account)
        strategy.on_bar(make_bar(2, 103, volume=3000, vwap=101), account)
        signals = strategy.on_bar(make_bar(3, 105, volume=4000, vwap=102), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual(1, len(signals))
        self.assertIsInstance(score, SignalScore)
        self.assertNotIn("wide_spread", score.reasons)

    def test_insufficient_score_data_blocks_entry_signal(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(momentum_window=1, volume_window=1, min_volume_ratio=Decimal("1"))
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(1, 102, volume=2000, vwap=101), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("insufficient_data", score.reasons)

    def test_insufficient_trend_boundary_history_blocks_long_entry(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                trend_boundary_window=4,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 104, volume=3000, vwap=103), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("insufficient_trend_boundary", score.reasons)

    def test_insufficient_trend_boundary_history_blocks_short_entry(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                trend_boundary_window=4,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 102, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 98, volume=3000, vwap=99), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("insufficient_trend_boundary", score.reasons)

    def test_invalid_trend_boundary_data_blocks_entry_signal(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(momentum_window=1, volume_window=1, min_volume_ratio=Decimal("1"))
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 0, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 104, volume=3000, vwap=103), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("invalid_trend_boundary", score.reasons)

    def test_does_not_buy_below_vwap(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(momentum_window=1, volume_window=1))
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(1, 103, volume=3000, vwap=104), account)

        self.assertEqual([], signals)

    def test_records_momentum_rejection_reason_after_score_passes(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_momentum_pct=Decimal("0.01"),
                min_signal_confidence=Decimal("0.55"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 100, volume=1000), account)
        strategy.on_bar(make_bar(2, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 100.5, volume=3000, vwap=100), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("long_momentum_below_minimum", score.reasons)

    def test_zero_volume_gate_does_not_block_when_volume_data_is_unavailable(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_momentum_pct=Decimal("0"),
                min_signal_confidence=Decimal("0.25"),
                volume_window=1,
                min_volume_ratio=Decimal("0"),
                min_trend_pct=Decimal("0"),
                require_vwap_alignment=False,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=0), account)
        strategy.on_bar(make_bar(1, 100, volume=0), account)
        strategy.on_bar(make_bar(2, 100, volume=0), account)
        signals = strategy.on_bar(make_bar(3, 101, volume=0, vwap=102), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)

    def test_configurable_signal_confidence_controls_rehearsal_entry_gate(self):
        strict = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_short_momentum_pct=Decimal("-0.001"),
                min_signal_confidence=Decimal("0.85"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        active = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_short_momentum_pct=Decimal("-0.001"),
                min_signal_confidence=Decimal("0.55"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        bars = [
            make_bar(0, 102, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 100, volume=1000),
            make_bar(3, 98, volume=1000, vwap=99),
        ]

        for bar in bars[:3]:
            strict.on_bar(bar, account)
            active.on_bar(bar, account)

        self.assertEqual([], strict.on_bar(bars[3], account))
        signals = active.on_bar(bars[3], account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_ENTRY", signals[0].side)

    def test_allows_buy_at_exact_momentum_and_volume_thresholds(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_momentum_pct=Decimal("0.01"),
                volume_window=1,
                min_volume_ratio=Decimal("2"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 98, volume=1000), account)
        strategy.on_bar(make_bar(1, 99, volume=1000), account)
        strategy.on_bar(make_bar(2, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 101, volume=2000, vwap=100), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)

    def test_does_not_buy_when_spread_is_too_wide(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                max_spread_bps=Decimal("10"),
            ),
            flow_context_provider=lambda symbol: MarketFlowContext(
                spread_bps=Decimal("5")
            ),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1100), account)
        strategy.on_bar(make_bar(2, 103, volume=3000, vwap=101), account)
        signals = strategy.on_bar(
            make_bar(3, 105, volume=4000, vwap=102, bid=103, ask=107),
            account,
        )

        self.assertEqual([], signals)
        score = strategy.last_entry_score("005930")
        self.assertIsInstance(score, SignalScore)
        self.assertEqual("hold", score.direction)
        self.assertIn("wide_spread", score.reasons)

    def test_does_not_buy_when_price_is_above_upper_trend_boundary(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 120, volume=5000, vwap=110), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("above_upper_trend_boundary", score.reasons)

    def test_generates_buy_when_bullish_trend_boundary_and_volume_match(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 102, volume=1000), account)
        strategy.on_bar(make_bar(2, 104, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 106, volume=3000, vwap=105), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("bullish_trend_boundary", score.reasons)

    def test_entry_trend_boundary_includes_current_breakout_bar(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_momentum_pct=Decimal("0.001"),
                min_signal_confidence=Decimal("0.55"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 100, volume=1000), account)
        strategy.on_bar(make_bar(2, 100, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 101, volume=2500, vwap=100), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("bullish_trend_boundary", score.reasons)

    def test_blocks_long_entry_when_expected_net_profit_is_below_costs(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0.010"),
                commission_pct=Decimal("0.001"),
                slippage_pct=Decimal("0.001"),
                min_net_profit_pct=Decimal("0.005"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 102, volume=1000), account)
        strategy.on_bar(make_bar(2, 104, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 106, volume=3000, vwap=105), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("expected_net_profit_below_costs", score.reasons)
        self.assertEqual("hold", score.direction)

    def test_allows_long_entry_when_expected_net_profit_covers_costs(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0.002"),
                commission_pct=Decimal("0.0001"),
                slippage_pct=Decimal("0.0001"),
                min_net_profit_pct=Decimal("0.001"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 102, volume=1000), account)
        strategy.on_bar(make_bar(2, 104, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 106, volume=3000, vwap=105), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)

    def test_blocks_short_entry_when_expected_net_profit_is_below_costs(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0.010"),
                commission_pct=Decimal("0.001"),
                slippage_pct=Decimal("0.001"),
                min_net_profit_pct=Decimal("0.005"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 106, volume=1000), account)
        strategy.on_bar(make_bar(1, 104, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 100, volume=3000, vwap=101), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual([], signals)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("expected_net_profit_below_costs", score.reasons)
        self.assertEqual("hold", score.direction)

    def test_allows_short_entry_when_expected_net_profit_covers_costs(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0.002"),
                commission_pct=Decimal("0.0001"),
                slippage_pct=Decimal("0.0001"),
                min_net_profit_pct=Decimal("0.001"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 106, volume=1000), account)
        strategy.on_bar(make_bar(1, 104, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 100, volume=3000, vwap=101), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_ENTRY", signals[0].side)

    def test_allows_buy_on_small_volume_breakout_above_upper_boundary(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 100, volume=1000), account)
        strategy.on_bar(make_bar(1, 101, volume=1000), account)
        strategy.on_bar(make_bar(2, 102, volume=1000), account)
        signals = strategy.on_bar(make_bar(3, 104.5, volume=5000, vwap=103), account)
        score = strategy.last_entry_score("005930")

        self.assertEqual(1, len(signals))
        self.assertEqual("BUY", signals[0].side)
        self.assertIsInstance(score, SignalScore)
        self.assertIn("bullish_trend_boundary", score.reasons)

    def test_generates_sell_signal_at_stop_loss(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(stop_loss_pct=Decimal("0.02")))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 9800), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("stop_loss", signals[0].reason)

    def test_generates_sell_signal_at_take_profit(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(take_profit_pct=Decimal("0.03")))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 10300), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("take_profit", signals[0].reason)

    def test_long_take_profit_does_not_sell_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(take_profit_pct=Decimal("0.03")))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10300"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        take_profit_bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 5),
            open=Decimal("10300"),
            high=Decimal("10300"),
            low=Decimal("10300"),
            close=Decimal("10300"),
            volume=1000,
            vwap=Decimal("10300"),
            bid=Decimal("9900"),
            ask=Decimal("10310"),
        )

        signals = strategy.on_bar(take_profit_bar, account)

        self.assertLess(take_profit_bar.sell_price, position.avg_price)
        self.assertEqual([], signals)

    def test_generates_sell_signal_at_upper_trend_boundary(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 100), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 104), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("106"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("106"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, 108), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("upper_trend_boundary", signals[0].reason)

    def test_long_upper_trend_boundary_does_not_sell_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 100), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 104), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("107"),
            last_price=Decimal("106"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("106"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        exit_bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 3),
            open=Decimal("106"),
            high=Decimal("108"),
            low=Decimal("106"),
            close=Decimal("106"),
            volume=1000,
            vwap=Decimal("106"),
            bid=Decimal("106"),
            ask=Decimal("106.1"),
        )

        signals = strategy.on_bar(exit_bar, account)

        self.assertLess(exit_bar.sell_price, position.avg_price)
        self.assertEqual([], signals)

    def test_generates_sell_signal_at_lower_trend_boundary(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 100), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 104), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("106"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("106"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, 103), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("lower_trend_boundary", signals[0].reason)

    def test_long_lower_trend_boundary_does_not_sell_flat_at_entry_price(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 100), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 104), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("104.94"),
            last_price=Decimal("104.94"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("104.94"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, Decimal("104.94")), account)

        self.assertEqual([], signals)

    def test_long_exit_uses_lower_trend_boundary_even_when_recent_direction_is_bearish(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 104), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 100), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("98"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("104"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, 97), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("lower_trend_boundary", signals[0].reason)

    def test_long_trend_boundaries_ignore_session_extremes_when_sell_price_is_inside(self):
        account_without_position = AccountSnapshot(cash=Decimal("1000000"))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("106"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("106"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position})
        extremes = (
            (Decimal("108"), Decimal("105")),
            (Decimal("106"), Decimal("104")),
        )

        for high, low in extremes:
            with self.subTest(high=high, low=low):
                strategy = FlowScalperStrategy(
                    FlowScalperConfig(
                        take_profit_pct=Decimal("0.50"),
                        stop_loss_pct=Decimal("0.50"),
                        trailing_stop_pct=Decimal("0.50"),
                    )
                )
                strategy.on_bar(make_bar(0, 100), account_without_position)
                strategy.on_bar(make_bar(1, 102), account_without_position)
                strategy.on_bar(make_bar(2, 104), account_without_position)
                bar = MarketBar(
                    symbol="005930",
                    timestamp=datetime(2026, 6, 8, 9, 3),
                    open=Decimal("106"),
                    high=high,
                    low=low,
                    close=Decimal("106"),
                    volume=1000,
                    vwap=Decimal("106"),
                    bid=Decimal("106"),
                    ask=Decimal("106.1"),
                )

                signals = strategy.on_bar(bar, account)

                self.assertEqual([], signals)

    def test_generates_sell_signal_after_max_holding_minutes(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(max_holding_minutes=5))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(6, 10050), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("max_holding_time", signals[0].reason)

    def test_default_strategy_keeps_long_between_price_exit_zones_after_time_passes(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(30, 10050), account)

        self.assertEqual([], signals)

    def test_default_strategy_keeps_long_after_forced_exit_time_when_price_is_between_exit_zones(self):
        strategy = FlowScalperStrategy(FlowScalperConfig())
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 14, 50),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(
            MarketBar(
                symbol="005930",
                timestamp=datetime(2026, 6, 8, 15, 16),
                open=Decimal("10050"),
                high=Decimal("10050"),
                low=Decimal("10050"),
                close=Decimal("10050"),
                volume=1000,
                vwap=Decimal("10050"),
            ),
            account,
        )

        self.assertEqual([], signals)

    def test_generates_sell_signal_at_trailing_stop(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(trailing_stop_pct=Decimal("0.05"), take_profit_pct=Decimal("0.20"))
        )
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("11000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("11000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 10450), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SELL", signals[0].side)
        self.assertEqual("trailing_stop", signals[0].reason)

    def test_long_trailing_stop_does_not_sell_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                trailing_stop_pct=Decimal("0.05"),
                take_profit_pct=Decimal("0.20"),
                stop_loss_pct=Decimal("0.20"),
            )
        )
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10100"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10100"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 5),
            open=Decimal("9590"),
            high=Decimal("9590"),
            low=Decimal("9590"),
            close=Decimal("9590"),
            volume=1000,
            vwap=Decimal("9590"),
            bid=Decimal("9590"),
            ask=Decimal("9591"),
        )

        signals = strategy.on_bar(bar, account)

        self.assertLess(bar.sell_price, position.avg_price)
        self.assertEqual([], signals)

    def test_generates_sell_signal_at_forced_exit_time(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(forced_exit_time="15:15"))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 14, 50),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("900000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(
            MarketBar(
                symbol="005930",
                timestamp=datetime(2026, 6, 8, 15, 16),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=1000,
                vwap=Decimal("10000"),
            ),
            account,
        )

        self.assertEqual(1, len(signals))
        self.assertEqual("forced_exit", signals[0].reason)

    def test_account_daily_loss_does_not_force_long_exit_without_symbol_exit_zone(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(daily_loss_exit_amount=Decimal("100")))
        position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(cash=Decimal("990000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 9900), account)

        self.assertEqual([], signals)

    def test_realized_daily_loss_does_not_force_long_exit_without_symbol_exit_zone(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(daily_loss_exit_amount=Decimal("1000")))
        position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(
            cash=Decimal("990000"),
            positions={"005930": position},
            realized_pnl_today=Decimal("-1000"),
        )

        signals = strategy.on_bar(make_bar(5, 10000), account)

        self.assertEqual([], signals)

    def test_account_wide_open_loss_does_not_force_unrelated_symbol_exit(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(daily_loss_exit_amount=Decimal("1000")))
        current_position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        losing_position = Position(
            symbol="000660",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("9000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
        )
        account = AccountSnapshot(
            cash=Decimal("980000"),
            positions={"005930": current_position, "000660": losing_position},
            realized_pnl_today=Decimal("0"),
        )

        signals = strategy.on_bar(make_bar(5, 10000), account)

        self.assertEqual([], signals)

    def test_does_not_generate_short_signal_when_paper_short_disabled(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_short_momentum_pct=Decimal("-0.01"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        self.assertEqual([], strategy.on_bar(make_bar(0, 101, volume=1000), account))
        self.assertEqual([], strategy.on_bar(make_bar(1, 100, volume=1000), account))
        signals = strategy.on_bar(make_bar(2, 98, volume=1000, vwap=99), account)

        self.assertEqual([], signals)

    def test_generates_short_signal_when_paper_short_enabled_and_downtrend_matches(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                min_short_momentum_pct=Decimal("-0.01"),
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                allow_paper_short=True,
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))

        self.assertEqual([], strategy.on_bar(make_bar(0, 103, volume=1000), account))
        self.assertEqual([], strategy.on_bar(make_bar(1, 102, volume=1000), account))
        self.assertEqual([], strategy.on_bar(make_bar(2, 100, volume=1000), account))
        signals = strategy.on_bar(make_bar(3, 98, volume=3000, vwap=99), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_ENTRY", signals[0].side)
        self.assertRegex(signals[0].reason, r"^flow_score_\d+$")
        score = strategy.last_entry_score("005930")
        self.assertIsInstance(score, SignalScore)
        self.assertEqual("short", score.direction)

    def test_existing_short_position_exits_before_new_entry(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=1,
                volume_window=1,
                min_volume_ratio=Decimal("1"),
                stop_loss_pct=Decimal("0.02"),
                allow_paper_short=True,
            )
        )
        position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        strategy.on_bar(make_bar(0, 10000, volume=1000), AccountSnapshot(cash=Decimal("1000000")))
        signals = strategy.on_bar(make_bar(1, 10200, volume=1000, vwap=10100), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("stop_loss", signals[0].reason)

    def test_short_take_profit_exit_uses_cover_signal(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(take_profit_pct=Decimal("0.03")))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 9700), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("take_profit", signals[0].reason)

    def test_short_take_profit_does_not_cover_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(take_profit_pct=Decimal("0.03"), allow_paper_short=True))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
            lowest_price=Decimal("9700"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        take_profit_bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 5),
            open=Decimal("9700"),
            high=Decimal("9700"),
            low=Decimal("9700"),
            close=Decimal("9700"),
            volume=1000,
            vwap=Decimal("9700"),
            bid=Decimal("9690"),
            ask=Decimal("10100"),
        )

        signals = strategy.on_bar(take_profit_bar, account)

        self.assertGreater(take_profit_bar.buy_price, position.avg_price)
        self.assertEqual([], signals)

    def test_short_exit_uses_lower_trend_boundary_for_profit(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
                allow_paper_short=True,
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 104), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 100), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("98"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("100"),
            side="SHORT",
            lowest_price=Decimal("98"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, 96), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("lower_trend_boundary", signals[0].reason)

    def test_short_lower_trend_boundary_does_not_cover_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
                allow_paper_short=True,
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 104), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 100), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("97.5"),
            last_price=Decimal("98"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("100"),
            side="SHORT",
            lowest_price=Decimal("98"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        exit_bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 3),
            open=Decimal("98"),
            high=Decimal("98"),
            low=Decimal("96"),
            close=Decimal("98"),
            volume=1000,
            vwap=Decimal("98"),
            bid=Decimal("97.9"),
            ask=Decimal("98"),
        )

        signals = strategy.on_bar(exit_bar, account)

        self.assertGreater(exit_bar.buy_price, position.avg_price)
        self.assertEqual([], signals)

    def test_short_exit_uses_upper_trend_boundary_even_when_recent_direction_is_bullish(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
                allow_paper_short=True,
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 96), flat_account)
        strategy.on_bar(make_bar(1, 98), flat_account)
        strategy.on_bar(make_bar(2, 100), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("102"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("104"),
            side="SHORT",
            lowest_price=Decimal("96"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, 104), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("upper_trend_boundary", signals[0].reason)

    def test_short_upper_trend_boundary_does_not_cover_flat_at_entry_price(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                take_profit_pct=Decimal("0.50"),
                stop_loss_pct=Decimal("0.50"),
                trailing_stop_pct=Decimal("0.50"),
                allow_paper_short=True,
            )
        )
        flat_account = AccountSnapshot(cash=Decimal("1000000"), positions={}, realized_pnl_today=Decimal("0"))
        strategy.on_bar(make_bar(0, 104), flat_account)
        strategy.on_bar(make_bar(1, 102), flat_account)
        strategy.on_bar(make_bar(2, 100), flat_account)
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("98.98"),
            last_price=Decimal("98.98"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("98.98"),
            side="SHORT",
            lowest_price=Decimal("98.98"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(3, Decimal("98.98")), account)

        self.assertEqual([], signals)

    def test_short_trend_boundaries_ignore_session_extremes_when_buy_price_is_inside(self):
        account_without_position = AccountSnapshot(cash=Decimal("1000000"))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("100"),
            last_price=Decimal("98"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("100"),
            side="SHORT",
            lowest_price=Decimal("98"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position})
        extremes = (
            (Decimal("98"), Decimal("96")),
            (Decimal("100"), Decimal("98")),
        )

        for high, low in extremes:
            with self.subTest(high=high, low=low):
                strategy = FlowScalperStrategy(
                    FlowScalperConfig(
                        take_profit_pct=Decimal("0.50"),
                        stop_loss_pct=Decimal("0.50"),
                        trailing_stop_pct=Decimal("0.50"),
                        allow_paper_short=True,
                    )
                )
                strategy.on_bar(make_bar(0, 104), account_without_position)
                strategy.on_bar(make_bar(1, 102), account_without_position)
                strategy.on_bar(make_bar(2, 100), account_without_position)
                bar = MarketBar(
                    symbol="005930",
                    timestamp=datetime(2026, 6, 8, 9, 3),
                    open=Decimal("98"),
                    high=high,
                    low=low,
                    close=Decimal("98"),
                    volume=1000,
                    vwap=Decimal("98"),
                    bid=Decimal("97.9"),
                    ask=Decimal("98"),
                )

                signals = strategy.on_bar(bar, account)

                self.assertEqual([], signals)

    def test_short_forced_exit_uses_cover_signal(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(forced_exit_time="15:15"))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 14, 50),
            highest_price=Decimal("10000"),
            side="SHORT",
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(
            MarketBar(
                symbol="005930",
                timestamp=datetime(2026, 6, 8, 15, 16),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=1000,
                vwap=Decimal("10000"),
            ),
            account,
        )

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("forced_exit", signals[0].reason)

    def test_short_max_holding_exit_uses_cover_signal(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(max_holding_minutes=5))
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(6, 9950), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("max_holding_time", signals[0].reason)

    def test_short_trailing_stop_uses_low_water_mark(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(trailing_stop_pct=Decimal("0.015"), take_profit_pct=Decimal("0.20"))
        )
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("9000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
            lowest_price=Decimal("9000"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 9140), account)

        self.assertEqual(1, len(signals))
        self.assertEqual("SHORT_EXIT", signals[0].side)
        self.assertEqual("trailing_stop", signals[0].reason)

    def test_short_trailing_stop_does_not_cover_when_executable_price_is_losing(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                trailing_stop_pct=Decimal("0.05"),
                take_profit_pct=Decimal("0.20"),
                stop_loss_pct=Decimal("0.20"),
                allow_paper_short=True,
            )
        )
        position = Position(
            symbol="005930",
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("9900"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
            lowest_price=Decimal("9900"),
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))
        bar = MarketBar(
            symbol="005930",
            timestamp=datetime(2026, 6, 8, 9, 5),
            open=Decimal("10400"),
            high=Decimal("10400"),
            low=Decimal("10400"),
            close=Decimal("10400"),
            volume=1000,
            vwap=Decimal("10400"),
            bid=Decimal("10399"),
            ask=Decimal("10400"),
        )

        signals = strategy.on_bar(bar, account)

        self.assertGreater(bar.buy_price, position.avg_price)
        self.assertEqual([], signals)

    def test_account_daily_loss_does_not_force_short_exit_without_symbol_exit_zone(self):
        strategy = FlowScalperStrategy(FlowScalperConfig(daily_loss_exit_amount=Decimal("100")))
        position = Position(
            symbol="005930",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 8, 9, 0),
            highest_price=Decimal("10000"),
            side="SHORT",
        )
        account = AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}, realized_pnl_today=Decimal("0"))

        signals = strategy.on_bar(make_bar(5, 10100), account)

        self.assertEqual([], signals)


if __name__ == "__main__":
    unittest.main()
