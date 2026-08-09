import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.models import MarketBar
from stockbot.timeframes import aggregate_bars


def make_bar(
    offset,
    open_price,
    high=None,
    low=None,
    close=None,
    volume=100,
    vwap=None,
    symbol="005930",
):
    close_price = open_price if close is None else close
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=offset),
        open=Decimal(str(open_price)),
        high=Decimal(str(high if high is not None else open_price)),
        low=Decimal(str(low if low is not None else open_price)),
        close=Decimal(str(close_price)),
        volume=volume,
        vwap=Decimal(str(vwap if vwap is not None else close_price)),
    )


class TimeframesTest(unittest.TestCase):
    def test_aggregates_unsorted_bars_into_three_minute_buckets(self):
        bars = [
            make_bar(2, 105, high=109, low=104, close=106, volume=200, vwap=106),
            make_bar(0, 100, high=102, low=99, close=101, volume=100, vwap=100),
            make_bar(3, 200, high=204, low=198, close=203, volume=50, vwap=202),
            make_bar(1, 101, high=107, low=100, close=105, volume=100, vwap=104),
        ]

        aggregated = aggregate_bars(bars, 3)

        self.assertEqual(2, len(aggregated))
        first = aggregated[0]
        self.assertEqual("005930", first.symbol)
        self.assertEqual(datetime(2026, 6, 8, 9, 0), first.timestamp)
        self.assertEqual(Decimal("100"), first.open)
        self.assertEqual(Decimal("109"), first.high)
        self.assertEqual(Decimal("99"), first.low)
        self.assertEqual(Decimal("106"), first.close)
        self.assertEqual(400, first.volume)
        self.assertEqual(Decimal("104"), first.vwap)

        partial = aggregated[1]
        self.assertEqual(datetime(2026, 6, 8, 9, 3), partial.timestamp)
        self.assertEqual(Decimal("200"), partial.open)
        self.assertEqual(Decimal("204"), partial.high)
        self.assertEqual(Decimal("198"), partial.low)
        self.assertEqual(Decimal("203"), partial.close)
        self.assertEqual(50, partial.volume)

    def test_floors_bucket_start_for_five_minute_timeframe(self):
        aggregated = aggregate_bars(
            [
                make_bar(7, 100, high=103, low=99, close=102),
                make_bar(5, 90, high=95, low=88, close=94),
            ],
            5,
        )

        self.assertEqual(1, len(aggregated))
        self.assertEqual(datetime(2026, 6, 8, 9, 5), aggregated[0].timestamp)
        self.assertEqual(Decimal("90"), aggregated[0].open)
        self.assertEqual(Decimal("102"), aggregated[0].close)

    def test_returns_empty_list_for_empty_input(self):
        self.assertEqual([], aggregate_bars([], 3))

    def test_rejects_non_positive_minutes(self):
        for minutes in (0, -1):
            with self.subTest(minutes=minutes):
                with self.assertRaisesRegex(ValueError, "minutes"):
                    aggregate_bars([], minutes)

    def test_rejects_mixed_symbols(self):
        with self.assertRaisesRegex(ValueError, "symbol"):
            aggregate_bars(
                [
                    make_bar(0, 100, symbol="005930"),
                    make_bar(1, 101, symbol="000660"),
                ],
                3,
            )

    def test_rejects_duplicate_symbol_timestamp_rows(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            aggregate_bars(
                [
                    make_bar(0, 100, symbol="005930"),
                    make_bar(0, 101, symbol="005930"),
                ],
                3,
            )


if __name__ == "__main__":
    unittest.main()
