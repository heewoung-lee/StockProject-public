import unittest

from stockbot.watchlist import Watchlist, WatchlistEntry


class WatchlistEntryTest(unittest.TestCase):
    def test_entry_defaults_to_enabled_with_empty_tags_and_notes(self):
        entry = WatchlistEntry(symbol="005930", company_name="Samsung Electronics")

        self.assertEqual("005930", entry.symbol)
        self.assertEqual("Samsung Electronics", entry.company_name)
        self.assertTrue(entry.enabled)
        self.assertEqual((), entry.tags)
        self.assertEqual("", entry.notes)


class WatchlistTest(unittest.TestCase):
    def test_returns_enabled_symbols_and_entries_only(self):
        watchlist = Watchlist(
            (
                WatchlistEntry("005930", "Samsung Electronics", tags=("core",)),
                WatchlistEntry("000660", "SK Hynix", enabled=False),
                WatchlistEntry("035420", "Naver"),
            )
        )

        self.assertEqual(("005930", "035420"), watchlist.active_symbols())
        self.assertEqual(
            (
                WatchlistEntry("005930", "Samsung Electronics", tags=("core",)),
                WatchlistEntry("035420", "Naver"),
            ),
            watchlist.enabled_entries(),
        )

    def test_company_name_falls_back_to_symbol_for_unknown_symbols(self):
        watchlist = Watchlist((WatchlistEntry("005930", "Samsung Electronics"),))

        self.assertEqual("Samsung Electronics", watchlist.company_name("005930"))
        self.assertEqual("999999", watchlist.company_name("999999"))

    def test_company_name_falls_back_to_symbol_when_known_entry_has_blank_name(self):
        watchlist = Watchlist((WatchlistEntry("005930", ""),))

        self.assertEqual("005930", watchlist.company_name("005930"))

    def test_from_rows_normalizes_symbols_names_tags_and_enabled_values(self):
        watchlist = Watchlist.from_rows(
            (
                {
                    "symbol": " 005930 ",
                    "company": "Samsung Electronics",
                    "enabled": "false",
                    "tags": " core, semiconductor , ",
                    "notes": "market leader",
                },
                {
                    "symbol": "000660",
                    "name": "SK Hynix",
                    "enabled": "YES",
                    "tags": "memory",
                },
                {
                    "symbol": "035420",
                    "company_name": "Naver",
                    "enabled": "",
                },
            )
        )

        self.assertEqual(
            (
                WatchlistEntry(
                    symbol="005930",
                    company_name="Samsung Electronics",
                    enabled=False,
                    tags=("core", "semiconductor"),
                    notes="market leader",
                ),
                WatchlistEntry(symbol="000660", company_name="SK Hynix", tags=("memory",)),
                WatchlistEntry(symbol="035420", company_name="Naver"),
            ),
            watchlist.entries,
        )
        self.assertEqual(("000660", "035420"), watchlist.active_symbols())

    def test_from_rows_treats_false_like_enabled_values_as_disabled(self):
        rows = (
            {"symbol": "A", "company": "A Co", "enabled": "false"},
            {"symbol": "B", "company": "B Co", "enabled": "0"},
            {"symbol": "C", "company": "C Co", "enabled": "no"},
            {"symbol": "D", "company": "D Co", "enabled": "off"},
            {"symbol": "E", "company": "E Co", "enabled": "n"},
            {"symbol": "F", "company": "F Co", "enabled": "unexpected"},
        )

        watchlist = Watchlist.from_rows(rows)

        self.assertEqual(("F",), watchlist.active_symbols())

    def test_from_rows_uses_last_duplicate_symbol_row(self):
        watchlist = Watchlist.from_rows(
            (
                {"symbol": "005930", "company": "Old Name", "enabled": "no", "tags": "old"},
                {"symbol": "005930", "company": "Samsung Electronics", "enabled": "yes", "tags": "core"},
            )
        )

        self.assertEqual(
            (WatchlistEntry("005930", "Samsung Electronics", tags=("core",)),),
            watchlist.entries,
        )
        self.assertEqual(("005930",), watchlist.active_symbols())


if __name__ == "__main__":
    unittest.main()
