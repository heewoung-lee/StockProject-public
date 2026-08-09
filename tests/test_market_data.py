import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.market_data import read_csv_bars


class MarketDataTest(unittest.TestCase):
    def test_reads_bars_sorted_by_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:02:00,005930,101,102,100,101,2000,100.5,100.9,101.1",
                        "2026-06-08T09:01:00,005930,100,101,99,100,1000,100,99.9,100.1",
                    ]
                ),
                encoding="utf-8",
            )

            bars = list(read_csv_bars(path))

            self.assertEqual(2, len(bars))
            self.assertLess(bars[0].timestamp, bars[1].timestamp)
            self.assertEqual("005930", bars[0].symbol)
            self.assertEqual("100", str(bars[0].close))

    def test_missing_required_column_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text("timestamp,symbol,close\n2026-06-08T09:01:00,005930,100\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "missing required columns"):
                list(read_csv_bars(path))


if __name__ == "__main__":
    unittest.main()
