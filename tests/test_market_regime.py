import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.models import MarketBar
from stockbot.market_regime import MarketRegimeDetector


def make_bar(offset, close, high=None, low=None):
    close_price = Decimal(str(close))
    high_price = Decimal(str(high if high is not None else close))
    low_price = Decimal(str(low if low is not None else close))
    return MarketBar(
        symbol="005930",
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=close_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=1000,
        vwap=close_price,
    )


class MarketRegimeDetectorTest(unittest.TestCase):
    def detector(self):
        return MarketRegimeDetector()

    def assert_normalized(self, regime):
        self.assertGreaterEqual(regime.volatility, 0.0)
        self.assertLessEqual(regime.volatility, 1.0)
        self.assertGreaterEqual(regime.confidence, 0.0)
        self.assertLessEqual(regime.confidence, 1.0)

    def test_insufficient_bars_returns_unknown_with_reason(self):
        regime = self.detector().detect([make_bar(0, 100), make_bar(1, 101)])

        self.assertEqual("unknown", regime.label)
        self.assertEqual("unknown", regime.direction)
        self.assertLess(regime.confidence, 0.5)
        self.assertIn("insufficient_data", regime.reasons)
        self.assertIsInstance(regime.reasons, tuple)
        self.assert_normalized(regime)

    def test_detects_uptrend_when_close_and_high_low_flow_rise(self):
        bars = [
            make_bar(0, 100, high=101, low=99),
            make_bar(1, 101, high=102, low=100),
            make_bar(2, 102, high=103, low=101),
            make_bar(3, 106, high=107, low=105),
            make_bar(4, 108, high=109, low=107),
            make_bar(5, 110, high=111, low=109),
        ]

        regime = self.detector().detect(bars)

        self.assertEqual("uptrend", regime.label)
        self.assertEqual("up", regime.direction)
        self.assertGreaterEqual(regime.confidence, 0.6)
        self.assertIn("close_above_early_average", regime.reasons)
        self.assertIn("higher_highs_and_lows", regime.reasons)
        self.assert_normalized(regime)

    def test_sorts_input_by_timestamp_before_detecting_latest_flow(self):
        bars = [
            make_bar(5, 110, high=111, low=109),
            make_bar(0, 100, high=101, low=99),
            make_bar(3, 106, high=107, low=105),
            make_bar(1, 101, high=102, low=100),
            make_bar(4, 108, high=109, low=107),
            make_bar(2, 102, high=103, low=101),
        ]

        regime = self.detector().detect(bars)

        self.assertEqual("uptrend", regime.label)
        self.assertEqual("up", regime.direction)

    def test_rejects_mixed_symbol_regime_inputs(self):
        bars = [
            make_bar(0, 100, high=101, low=99),
            make_bar(1, 101, high=102, low=100),
            make_bar(2, 102, high=103, low=101),
            make_bar(3, 106, high=107, low=105),
            make_bar(4, 108, high=109, low=107),
            MarketBar(
                symbol="000660",
                timestamp=datetime(2026, 6, 8, 9, 5),
                open=Decimal("110"),
                high=Decimal("111"),
                low=Decimal("109"),
                close=Decimal("110"),
                volume=1000,
                vwap=Decimal("110"),
            ),
        ]

        with self.assertRaisesRegex(ValueError, "symbol"):
            self.detector().detect(bars)

    def test_detects_downtrend_when_close_and_high_low_flow_fall(self):
        bars = [
            make_bar(0, 110, high=111, low=109),
            make_bar(1, 109, high=110, low=108),
            make_bar(2, 108, high=109, low=107),
            make_bar(3, 104, high=105, low=103),
            make_bar(4, 102, high=103, low=101),
            make_bar(5, 100, high=101, low=99),
        ]

        regime = self.detector().detect(bars)

        self.assertEqual("downtrend", regime.label)
        self.assertEqual("down", regime.direction)
        self.assertGreaterEqual(regime.confidence, 0.6)
        self.assertIn("close_below_early_average", regime.reasons)
        self.assertIn("lower_highs_and_lows", regime.reasons)
        self.assert_normalized(regime)

    def test_detects_range_when_direction_is_weak_and_volatility_is_low(self):
        bars = [
            make_bar(0, 100, high=101, low=99),
            make_bar(1, 100.5, high=101.2, low=99.7),
            make_bar(2, 99.8, high=100.8, low=99.2),
            make_bar(3, 100.2, high=101, low=99.5),
            make_bar(4, 100.4, high=101.1, low=99.6),
            make_bar(5, 100.1, high=100.9, low=99.4),
        ]

        regime = self.detector().detect(bars)

        self.assertEqual("range", regime.label)
        self.assertEqual("flat", regime.direction)
        self.assertLess(regime.volatility, 0.5)
        self.assertIn("weak_direction", regime.reasons)
        self.assertIn("low_volatility", regime.reasons)
        self.assert_normalized(regime)

    def test_clamps_extreme_volatility_and_confidence_to_normalized_bounds(self):
        bars = [
            make_bar(0, 100, high=500, low=1),
            make_bar(1, 105, high=520, low=2),
            make_bar(2, 110, high=540, low=3),
            make_bar(3, 500, high=900, low=4),
            make_bar(4, 600, high=1000, low=5),
            make_bar(5, 700, high=1100, low=6),
        ]

        regime = self.detector().detect(bars)

        self.assert_normalized(regime)


if __name__ == "__main__":
    unittest.main()
