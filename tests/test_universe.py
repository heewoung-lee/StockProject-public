import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from stockbot.models import MarketBar
from stockbot.universe import VolumePriorityRanker


def make_bar(symbol, offset, close="10000", volume=1000):
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=volume,
        vwap=price,
        bid=price,
        ask=price,
    )


class VolumePriorityRankerTest(unittest.TestCase):
    def test_prioritizes_symbols_with_larger_recent_volume(self):
        ranker = VolumePriorityRanker.from_bars(
            ["LOW001", "HIGH01"],
            [
                make_bar("LOW001", 0, volume=1000),
                make_bar("LOW001", 1, volume=1100),
                make_bar("HIGH01", 0, volume=8000),
                make_bar("HIGH01", 1, volume=9000),
            ],
        )

        self.assertGreater(ranker.priority("HIGH01"), ranker.priority("LOW001"))
        self.assertEqual(["HIGH01", "LOW001"], ranker.rank(["LOW001", "HIGH01"]))

    def test_ranking_uses_stable_symbol_order_when_volume_matches(self):
        ranker = VolumePriorityRanker.from_bars(
            ["BBB002", "AAA001"],
            [make_bar("BBB002", 0, volume=1000), make_bar("AAA001", 0, volume=1000)],
        )

        self.assertEqual(["BBB002", "AAA001"], ranker.rank(["BBB002", "AAA001"]))


if __name__ == "__main__":
    unittest.main()
