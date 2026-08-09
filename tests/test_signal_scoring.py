import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from stockbot.models import MarketBar
from stockbot.signal_scoring import MarketFlowContext, SignalScorer


def make_bar(offset, close, volume=1000, vwap=None):
    price = Decimal(str(close))
    return MarketBar(
        symbol="005930",
        timestamp=datetime(2026, 6, 11, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        vwap=Decimal(str(vwap if vwap is not None else close)),
        bid=price,
        ask=price,
    )


class FakeRegime:
    def __init__(self, **attrs):
        self.__dict__.update(attrs)


class SignalScorerTest(unittest.TestCase):
    def test_returns_hold_with_low_confidence_when_data_is_insufficient(self):
        score = SignalScorer().score("005930", [make_bar(0, 100), make_bar(1, 101)], object())

        self.assertEqual("005930", score.symbol)
        self.assertEqual("hold", score.direction)
        self.assertLess(score.confidence, 0.55)
        self.assertIn("insufficient_data", score.reasons)

    def test_scores_bullish_momentum_volume_and_regime_as_long(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 102, volume=1200),
            make_bar(2, 105, volume=3000, vwap=103),
        ]

        score = SignalScorer().score("005930", bars, FakeRegime(direction="bullish"))

        self.assertEqual("long", score.direction)
        self.assertGreater(score.long_score, score.short_score)
        self.assertGreaterEqual(score.confidence, 0.55)
        self.assertLessEqual(score.confidence, 1.0)
        self.assertIn("upward_momentum", score.reasons)
        self.assertIn("close_strength", score.reasons)
        self.assertIn("volume_expansion", score.reasons)
        self.assertIn("bullish_regime", score.reasons)

    def test_volume_override_does_not_change_price_strength_inputs(self):
        price_bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1000),
            make_bar(2, 102, volume=10000000, vwap=101),
        ]
        completed_volume_bars = [
            make_bar(0, 98, volume=1000),
            make_bar(1, 99, volume=1000),
            make_bar(2, 100, volume=1000),
        ]

        score = SignalScorer().score(
            "005930",
            price_bars,
            FakeRegime(direction="neutral"),
            volume_bars=completed_volume_bars,
        )

        self.assertIn("close_strength", score.reasons)
        self.assertNotIn("volume_expansion", score.reasons)

    def test_scores_bearish_momentum_volume_and_regime_as_short(self):
        bars = [
            make_bar(0, 105, volume=1000),
            make_bar(1, 102, volume=1200),
            make_bar(2, 98, volume=3000, vwap=100),
        ]

        score = SignalScorer().score("005930", bars, FakeRegime(label="bearish"))

        self.assertEqual("short", score.direction)
        self.assertGreater(score.short_score, score.long_score)
        self.assertGreaterEqual(score.confidence, 0.55)
        self.assertLessEqual(score.confidence, 1.0)
        self.assertIn("downward_momentum", score.reasons)
        self.assertIn("close_weakness", score.reasons)
        self.assertIn("volume_expansion", score.reasons)
        self.assertIn("bearish_regime", score.reasons)

    def test_short_disabled_blocks_short_direction(self):
        bars = [
            make_bar(0, 105, volume=1000),
            make_bar(1, 102, volume=1200),
            make_bar(2, 98, volume=3000, vwap=100),
        ]

        score = SignalScorer(short_enabled=False).score("005930", bars, FakeRegime(label="bearish"))

        self.assertEqual("hold", score.direction)
        self.assertGreater(score.short_score, score.long_score)
        self.assertIn("short_disabled", score.reasons)
        self.assertNotIn("signal_confidence_below_minimum", score.reasons)

    def test_hold_names_signal_confidence_gate(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 102, volume=1000),
            make_bar(2, 101, volume=1000, vwap=101),
        ]

        score = SignalScorer(min_confidence=0.55).score(
            "005930",
            bars,
            FakeRegime(direction="neutral"),
        )

        self.assertEqual("hold", score.direction)
        self.assertLess(score.confidence, 0.55)
        self.assertIn("signal_confidence_below_minimum", score.reasons)

    def test_confidence_equal_to_threshold_is_not_reported_as_below_minimum(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 102, volume=1000),
            make_bar(2, 101, volume=3000, vwap=101),
        ]

        score = SignalScorer(min_confidence=0.55).score(
            "005930",
            bars,
            FakeRegime(direction="neutral"),
        )

        self.assertEqual("long", score.direction)
        self.assertEqual(0.55, score.confidence)
        self.assertNotIn("signal_confidence_below_minimum", score.reasons)

    def test_kis_flow_context_boosts_ranked_net_buying_candidates(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1100),
            make_bar(2, 102, volume=1300, vwap=101),
        ]

        score = SignalScorer(min_confidence=0.70).score(
            "005930",
            bars,
            FakeRegime(direction="neutral"),
            MarketFlowContext(
                volume_ratio=Decimal("2.1"),
                foreign_institution_net_amount=Decimal("250000000"),
                ranking_score=0.82,
            ),
        )

        self.assertEqual("long", score.direction)
        self.assertGreaterEqual(score.confidence, 0.70)
        self.assertIn("kis_volume_surge", score.reasons)
        self.assertIn("foreign_institution_net_buy", score.reasons)
        self.assertIn("kis_rank_strength", score.reasons)

    def test_kis_flow_context_can_confirm_short_pressure(self):
        bars = [
            make_bar(0, 105, volume=1000),
            make_bar(1, 103, volume=1100),
            make_bar(2, 101, volume=1300, vwap=102),
        ]

        score = SignalScorer(min_confidence=0.70, short_enabled=True).score(
            "005930",
            bars,
            FakeRegime(direction="neutral"),
            MarketFlowContext(
                volume_ratio=Decimal("2.0"),
                foreign_institution_net_amount=Decimal("-150000000"),
                short_pressure_score=0.85,
            ),
        )

        self.assertEqual("short", score.direction)
        self.assertGreaterEqual(score.confidence, 0.70)
        self.assertIn("foreign_institution_net_sell", score.reasons)
        self.assertIn("short_pressure", score.reasons)

    def test_wide_spread_blocks_otherwise_strong_flow_candidate(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 102, volume=1200),
            make_bar(2, 105, volume=4000, vwap=103),
        ]

        score = SignalScorer(min_confidence=0.70).score(
            "005930",
            bars,
            FakeRegime(direction="bullish"),
            MarketFlowContext(spread_bps=Decimal("75")),
        )

        self.assertEqual("hold", score.direction)
        self.assertLess(score.confidence, 0.70)
        self.assertIn("wide_spread", score.reasons)

    def test_overextended_move_blocks_chasing_candidate(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 107, volume=1800),
            make_bar(2, 113, volume=3500, vwap=110),
        ]

        score = SignalScorer(min_confidence=0.70).score(
            "005930",
            bars,
            FakeRegime(direction="bullish"),
            MarketFlowContext(overextension_pct=Decimal("0.12")),
        )

        self.assertEqual("hold", score.direction)
        self.assertLess(score.confidence, 0.70)
        self.assertIn("overextended_move", score.reasons)


if __name__ == "__main__":
    unittest.main()
