import unittest
from datetime import datetime

from stockbot.market_hours import KST, KoreanRegularMarketHours


class KoreanRegularMarketHoursTest(unittest.TestCase):
    def test_regular_session_is_open_between_0900_and_1530_kst(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 10, 0, tzinfo=KST))

        status = hours.status()

        self.assertTrue(status.is_open)
        self.assertEqual("정규장 진행 중", status.label)

    def test_regular_session_is_closed_after_1530_and_reports_next_open(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))

        status = hours.status()

        self.assertFalse(status.is_open)
        self.assertEqual("장 대기", status.label)
        self.assertIn("정규장", status.message)
        self.assertIn("2026-06-12 09:00 KST", status.message)

    def test_weekend_is_closed_until_next_monday(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 13, 10, 0, tzinfo=KST))

        status = hours.status()

        self.assertFalse(status.is_open)
        self.assertIn("2026-06-15 09:00 KST", status.message)

    def test_configured_weekday_closed_date_is_closed(self):
        hours = KoreanRegularMarketHours(
            clock=lambda: datetime(2026, 5, 1, 10, 0, tzinfo=KST),
            holidays={datetime(2026, 5, 1, tzinfo=KST).date()},
        )

        status = hours.status()

        self.assertFalse(status.is_open)
        self.assertEqual("장 대기", status.label)

    def test_default_krx_closed_dates_include_labor_day_and_year_end_closure(self):
        from stockbot.market_hours import default_krx_closed_dates

        closed_dates = default_krx_closed_dates(years=[2026])

        self.assertIn(datetime(2026, 5, 1, tzinfo=KST).date(), closed_dates)
        self.assertIn(datetime(2026, 12, 31, tzinfo=KST).date(), closed_dates)


if __name__ == "__main__":
    unittest.main()
