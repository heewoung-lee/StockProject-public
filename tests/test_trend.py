import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from stockbot.models import MarketBar
from stockbot.trend import TrendBoundaryAnalyzer


def make_bar(offset, close):
    price = Decimal(str(close))
    return MarketBar(
        symbol="005930",
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        vwap=price,
        bid=price,
        ask=price,
    )


class TrendBoundaryAnalyzerTest(unittest.TestCase):
    def test_projects_bullish_channel_boundaries_from_recent_closes(self):
        analyzer = TrendBoundaryAnalyzer(window=3, min_trend_pct=Decimal("0.005"))

        boundary = analyzer.project([make_bar(0, 100), make_bar(1, 102), make_bar(2, 104)])

        self.assertEqual("bullish", boundary.direction)
        self.assertEqual(Decimal("106"), boundary.center)
        self.assertLess(boundary.lower, boundary.center)
        self.assertGreater(boundary.upper, boundary.center)
        self.assertTrue(boundary.contains(Decimal("106")))
        self.assertTrue(boundary.touches_upper(boundary.upper))

    def test_projection_steps_zero_keeps_boundary_on_current_bar(self):
        analyzer = TrendBoundaryAnalyzer(window=3, min_trend_pct=Decimal("0.005"), projection_steps=0)

        boundary = analyzer.project([make_bar(0, 100), make_bar(1, 102), make_bar(2, 104)])

        self.assertEqual("bullish", boundary.direction)
        self.assertEqual(Decimal("104"), boundary.center)

    def test_projects_bearish_channel_boundaries_from_recent_closes(self):
        analyzer = TrendBoundaryAnalyzer(window=3, min_trend_pct=Decimal("0.005"))

        boundary = analyzer.project([make_bar(0, 104), make_bar(1, 102), make_bar(2, 100)])

        self.assertEqual("bearish", boundary.direction)
        self.assertEqual(Decimal("98"), boundary.center)
        self.assertLess(boundary.lower, boundary.center)
        self.assertGreater(boundary.upper, boundary.center)
        self.assertTrue(boundary.contains(Decimal("98")))
        self.assertTrue(boundary.touches_lower(boundary.lower))

    def test_flat_prices_do_not_create_tradeable_boundary(self):
        analyzer = TrendBoundaryAnalyzer(window=3, min_trend_pct=Decimal("0.005"))

        boundary = analyzer.project([make_bar(0, 100), make_bar(1, 100), make_bar(2, 100)])

        self.assertEqual("flat", boundary.direction)


if __name__ == "__main__":
    unittest.main()
