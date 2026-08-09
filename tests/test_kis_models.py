import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.kis_models import (
    KST,
    parse_kis_account_snapshot,
    parse_kis_minute_bars,
    parse_kis_opening_day,
    parse_kis_period_profit_rows,
    parse_kis_price_bar,
    parse_kis_realized_profit_row_today,
    parse_kis_realized_pnl_today,
)


class KisModelMappingTest(unittest.TestCase):
    def test_parse_price_response_into_market_bar(self):
        timestamp = datetime(2026, 6, 10, 9, 1, tzinfo=timezone.utc)
        response = {
            "rt_cd": "0",
            "output": {
                "stck_prpr": "70000",
                "stck_oprc": "69500",
                "stck_hgpr": "70500",
                "stck_lwpr": "69000",
                "acml_vol": "123456",
            },
        }

        bar = parse_kis_price_bar(response, symbol="005930", timestamp=timestamp)

        self.assertEqual("005930", bar.symbol)
        self.assertEqual(timestamp, bar.timestamp)
        self.assertEqual(Decimal("69500"), bar.open)
        self.assertEqual(Decimal("70500"), bar.high)
        self.assertEqual(Decimal("69000"), bar.low)
        self.assertEqual(Decimal("70000"), bar.close)
        self.assertEqual(123456, bar.volume)
        self.assertEqual(Decimal("70000"), bar.vwap)

    def test_parse_price_response_uses_weighted_average_when_available(self):
        response = {
            "rt_cd": "0",
            "output": {
                "stck_prpr": "70000",
                "acml_vol": "10",
                "wghn_avrg_stck_prc": "69800",
            },
        }

        bar = parse_kis_price_bar(response, symbol="005930")

        self.assertEqual(Decimal("69800"), bar.vwap)

    def test_parse_price_response_derives_vwap_from_trade_amount_and_volume(self):
        response = {
            "rt_cd": "0",
            "output": {
                "stck_prpr": "70000",
                "acml_vol": "10",
                "acml_tr_pbmn": "698000",
            },
        }

        bar = parse_kis_price_bar(response, symbol="005930")

        self.assertEqual(Decimal("69800"), bar.vwap)

    def test_parse_price_response_combines_live_best_quotes(self):
        bar = parse_kis_price_bar(
            {"output": {"stck_prpr": "70000", "acml_vol": "10"}},
            symbol="005930",
            orderbook_response={"output1": {"askp1": "70,100", "bidp1": "69,900"}},
        )

        self.assertEqual(Decimal("70100"), bar.ask)
        self.assertEqual(Decimal("69900"), bar.bid)
        self.assertEqual(Decimal("70100"), bar.buy_price)
        self.assertEqual(Decimal("69900"), bar.sell_price)

    def test_parse_price_response_preserves_live_trading_state(self):
        bar = parse_kis_price_bar(
            {
                "output": {
                    "stck_prpr": "70000",
                    "rprs_mrkt_kor_name": "코스닥",
                    "temp_stop_yn": "Y",
                    "vi_cls_code": "2",
                    "iscd_stat_cls_code": "51",
                }
            },
            symbol="005930",
        )

        self.assertEqual("KOSDAQ", bar.market)
        self.assertTrue(bar.temporary_stop)
        self.assertEqual("2", bar.vi_code)
        self.assertEqual("51", bar.security_status_code)
        self.assertEqual("KIS_CURRENT_PRICE", bar.trading_state_source)

    def test_parse_price_response_marks_missing_trading_state_unknown(self):
        bar = parse_kis_price_bar(
            {"output": {"stck_prpr": "70000"}},
            symbol="005930",
        )

        self.assertIsNone(bar.temporary_stop)
        self.assertEqual("KIS_CURRENT_PRICE", bar.trading_state_source)

    def test_parse_price_response_marks_invalid_temporary_stop_flag_unknown(self):
        bar = parse_kis_price_bar(
            {
                "output": {
                    "stck_prpr": "70000",
                    "temp_stop_yn": "unknown",
                }
            },
            symbol="005930",
        )

        self.assertIsNone(bar.temporary_stop)

    def test_parse_price_response_rejects_missing_or_malformed_best_quotes(self):
        malformed_orderbooks = (
            {"output1": {"bidp1": "69900"}},
            {"output1": {"askp1": "70100", "bidp1": "not-a-number"}},
            {"output1": {"askp1": "0", "bidp1": "69900"}},
            {"output1": {"askp1": "69900", "bidp1": "70100"}},
        )

        for orderbook in malformed_orderbooks:
            with self.subTest(orderbook=orderbook):
                with self.assertRaisesRegex(ValueError, "KIS best quote") as context:
                    parse_kis_price_bar(
                        {"output": {"stck_prpr": "70000"}},
                        symbol="005930",
                        orderbook_response=orderbook,
                    )
                self.assertNotIn("005930", str(context.exception))

    def test_parse_minute_bars_uses_actual_rows_and_orders_them_chronologically(self):
        response = {
            "rt_cd": "0",
            "output2": [
                {
                    "stck_bsop_date": "20260710",
                    "stck_cntg_hour": "090200",
                    "stck_oprc": "720",
                    "stck_hgpr": "750",
                    "stck_lwpr": "720",
                    "stck_prpr": "750",
                    "cntg_vol": "20",
                },
                {
                    "stck_bsop_date": "20260710",
                    "stck_cntg_hour": "090100",
                    "stck_oprc": "700",
                    "stck_hgpr": "720",
                    "stck_lwpr": "690",
                    "stck_prpr": "720",
                    "cntg_vol": "10",
                },
            ],
        }

        bars = parse_kis_minute_bars(response, symbol="005930", trading_date=date(2026, 7, 10))

        self.assertEqual(2, len(bars))
        self.assertEqual(
            ["2026-07-10T09:01:00+09:00", "2026-07-10T09:02:00+09:00"],
            [bar.timestamp.isoformat() for bar in bars],
        )
        self.assertEqual(Decimal("700"), bars[0].open)
        self.assertEqual(Decimal("720"), bars[0].high)
        self.assertEqual(Decimal("690"), bars[0].low)
        self.assertEqual(Decimal("720"), bars[0].close)
        self.assertEqual(10, bars[0].volume)
        self.assertEqual(Decimal("710"), bars[0].vwap)

    def test_parse_minute_bars_keeps_valid_empty_response_empty(self):
        self.assertEqual(
            [],
            parse_kis_minute_bars(
                {"rt_cd": "0", "output2": []},
                symbol="005930",
                trading_date=date(2026, 7, 10),
            ),
        )

    def test_parse_minute_bars_excludes_the_in_progress_minute(self):
        response = {
            "output2": [
                {
                    "stck_bsop_date": "20260710",
                    "stck_cntg_hour": minute,
                    "stck_oprc": "700",
                    "stck_hgpr": "700",
                    "stck_lwpr": "700",
                    "stck_prpr": "700",
                    "cntg_vol": "10",
                }
                for minute in ("090400", "090500")
            ]
        }

        bars = parse_kis_minute_bars(
            response,
            symbol="005930",
            trading_date=date(2026, 7, 10),
            completed_before=datetime(2026, 7, 10, 9, 5, tzinfo=KST),
        )

        self.assertEqual(["2026-07-10T09:04:00+09:00"], [bar.timestamp.isoformat() for bar in bars])

    def test_parse_minute_bars_rejects_malformed_or_wrong_date_rows(self):
        malformed_responses = (
            {"output2": {}},
            {
                "output2": [
                    {
                        "stck_bsop_date": "20260709",
                        "stck_cntg_hour": "090100",
                        "stck_oprc": "700",
                        "stck_hgpr": "720",
                        "stck_lwpr": "690",
                        "stck_prpr": "710",
                        "cntg_vol": "10",
                    }
                ]
            },
        )

        for response in malformed_responses:
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "KIS minute"):
                    parse_kis_minute_bars(response, symbol="005930", trading_date=date(2026, 7, 10))

    def test_parse_realized_pnl_uses_exact_date_output1_and_preserves_sign(self):
        pnl = parse_kis_realized_pnl_today(
            {
                "output1": [
                    {"trad_dt": "20260709", "rlzt_pfls": "500"},
                    {"trad_dt": "20260710", "rlzt_pfls": "-1,250"},
                ],
                "output2": {"tot_rlzt_pfls": "-750"},
            },
            trading_date=date(2026, 7, 10),
        )

        self.assertEqual(Decimal("-1250"), pnl)

    def test_parse_realized_pnl_falls_back_to_signed_output2_total(self):
        pnl = parse_kis_realized_pnl_today(
            {"output1": None, "output2": {"tot_rlzt_pfls": "+875"}},
            trading_date=date(2026, 7, 10),
        )

        self.assertEqual(Decimal("875"), pnl)

    def test_parse_realized_pnl_keeps_summary_fallback_for_blank_exact_row(self):
        row = parse_kis_realized_profit_row_today(
            {
                "output1": [{"trad_dt": "20260710", "rlzt_pfls": ""}],
                "output2": {"tot_rlzt_pfls": "-125"},
            },
            trading_date=date(2026, 7, 10),
        )

        self.assertEqual(Decimal("-125"), row.realized_pnl)
        self.assertIsNone(row.has_activity)

    def test_parse_realized_pnl_rejects_blank_exact_row_without_realized_summary(self):
        for output2 in ({}, {"tot_fee": "10", "tot_tltx": "5"}):
            with self.subTest(output2=output2):
                with self.assertRaisesRegex(ValueError, "rlzt_pfls|tot_rlzt_pfls"):
                    parse_kis_realized_pnl_today(
                        {
                            "output1": [{"trad_dt": "20260710", "rlzt_pfls": ""}],
                            "output2": output2,
                        },
                        trading_date=date(2026, 7, 10),
                    )

    def test_parse_realized_pnl_distinguishes_valid_empty_from_malformed(self):
        empty_row = parse_kis_realized_profit_row_today(
            {"output1": [], "output2": {}},
            trading_date=date(2026, 7, 10),
        )
        self.assertEqual(Decimal("0"), empty_row.realized_pnl)
        self.assertFalse(empty_row.has_activity)

        for response in ({}, {"output1": "", "output2": []}, {"output1": [{"trad_dt": "20260709", "rlzt_pfls": "1"}]}):
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "KIS realized profit"):
                    parse_kis_realized_pnl_today(response, trading_date=date(2026, 7, 10))

    def test_parse_period_profit_rows_preserves_daily_values_without_deducting_costs(self):
        rows = parse_kis_period_profit_rows(
            {
                "output1": [
                    {
                        "trad_dt": "20260710",
                        "rlzt_pfls": "1,250",
                        "fee": "100",
                        "tl_tax": "75",
                        "loan_int": "25",
                    },
                    {
                        "trad_dt": "20260709",
                        "rlzt_pfls": "-300",
                        "fee": "30",
                        "tl_tax": "10",
                        "loan_int": "0",
                    },
                ],
                "output2": {"tot_rlzt_pfls": "950"},
            },
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 10),
        )

        self.assertEqual([date(2026, 7, 9), date(2026, 7, 10)], [row.trading_date for row in rows])
        self.assertEqual(Decimal("1250"), rows[1].realized_pnl)
        self.assertEqual(Decimal("100"), rows[1].fee)
        self.assertEqual(Decimal("75"), rows[1].tax)
        self.assertEqual(Decimal("25"), rows[1].loan_interest)
        self.assertTrue(rows[1].has_activity)
        self.assertEqual("unknown", rows[1].cost_inclusion)
        with self.assertRaises(FrozenInstanceError):
            rows[1].fee = Decimal("0")

    def test_parse_period_profit_rows_rejects_out_of_range_and_duplicate_dates(self):
        with self.assertRaisesRegex(ValueError, "outside requested range"):
            parse_kis_period_profit_rows(
                {
                    "output1": [
                        {
                            "trad_dt": "20260630",
                            "rlzt_pfls": "1",
                            "fee": "0",
                            "tl_tax": "0",
                            "loan_int": "0",
                        }
                    ]
                },
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 10),
            )

        with self.assertRaisesRegex(ValueError, "start date must not be after end date"):
            parse_kis_period_profit_rows(
                {"output1": []},
                start_date=date(2026, 7, 11),
                end_date=date(2026, 7, 10),
            )

        with self.assertRaisesRegex(ValueError, "duplicate trading date"):
            parse_kis_period_profit_rows(
                {
                    "output1": [
                        {"trad_dt": "20260710", "rlzt_pfls": "1"},
                        {"trad_dt": "20260710", "rlzt_pfls": "1"},
                    ]
                },
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 10),
            )

    def test_parse_period_profit_rows_rejects_invalid_or_non_finite_decimals(self):
        for field, value in (
            ("rlzt_pfls", "not-a-number"),
            ("fee", "NaN"),
            ("tl_tax", "Infinity"),
            ("loan_int", "-Infinity"),
        ):
            row = {
                "trad_dt": "20260710",
                "rlzt_pfls": "10",
                "fee": "0",
                "tl_tax": "0",
                "loan_int": "0",
            }
            row[field] = value
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(ValueError, field):
                    parse_kis_period_profit_rows(
                        {"output1": [row]},
                        start_date=date(2026, 7, 10),
                        end_date=date(2026, 7, 10),
                    )

    def test_parse_period_profit_rows_rejects_negative_daily_costs(self):
        for field in ("fee", "tl_tax", "loan_int"):
            row = {
                "trad_dt": "20260710",
                "rlzt_pfls": "-10",
                "fee": "0",
                "tl_tax": "0",
                "loan_int": "0",
            }
            row[field] = "-1"
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    parse_kis_period_profit_rows(
                        {"output1": [row]},
                        start_date=date(2026, 7, 10),
                        end_date=date(2026, 7, 10),
                    )

    def test_parse_period_profit_rows_uses_single_day_summary_once_as_fallback(self):
        rows = parse_kis_period_profit_rows(
            {
                "output1": [],
                "output2": [
                    {"tot_rlzt_pfls": "500", "tot_fee": "20", "tot_tltx": "10"},
                    {"tot_rlzt_pfls": "500", "tot_fee": "20", "tot_tltx": "10"},
                ],
            },
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 10),
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(Decimal("500"), rows[0].realized_pnl)
        self.assertEqual(Decimal("20"), rows[0].fee)
        self.assertEqual(Decimal("10"), rows[0].tax)
        self.assertEqual(Decimal("0"), rows[0].loan_interest)

        empty_range = parse_kis_period_profit_rows(
            {
                "output1": [],
                "output2": {"tot_rlzt_pfls": "500", "tot_fee": "20", "tot_tltx": "10"},
            },
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 10),
        )
        self.assertEqual((), empty_range)

    def test_parse_period_profit_rows_rejects_conflicting_single_day_summaries(self):
        with self.assertRaisesRegex(ValueError, "conflicting KIS period profit summaries"):
            parse_kis_period_profit_rows(
                {
                    "output1": [],
                    "output2": [
                        {"tot_rlzt_pfls": "500"},
                        {"tot_rlzt_pfls": "1000"},
                    ],
                },
                start_date=date(2026, 7, 10),
                end_date=date(2026, 7, 10),
            )

    def test_parse_period_profit_rows_rejects_negative_summary_costs(self):
        for field in ("tot_fee", "tot_tltx"):
            summary = {"tot_rlzt_pfls": "-10", "tot_fee": "0", "tot_tltx": "0"}
            summary[field] = "-1"
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    parse_kis_period_profit_rows(
                        {"output1": [], "output2": summary},
                        start_date=date(2026, 7, 10),
                        end_date=date(2026, 7, 10),
                    )

    def test_parse_opening_day_reads_exact_date(self):
        response = {
            "output": [
                {"bass_dt": "20260709", "opnd_yn": "Y"},
                {"bass_dt": "20260710", "opnd_yn": "N"},
            ]
        }

        self.assertFalse(parse_kis_opening_day(response, trading_date=date(2026, 7, 10)))

    def test_parse_opening_day_rejects_wrong_date_or_malformed_flag(self):
        malformed_responses = (
            {"output": [{"bass_dt": "20260709", "opnd_yn": "Y"}]},
            {"output": [{"bass_dt": "20260710", "opnd_yn": ""}]},
            {"output": [{"bass_dt": "20260710", "opnd_yn": "UNKNOWN"}]},
        )

        for response in malformed_responses:
            with self.subTest(response=response):
                with self.assertRaisesRegex(ValueError, "KIS holiday"):
                    parse_kis_opening_day(response, trading_date=date(2026, 7, 10))

    def test_parse_balance_response_into_account_snapshot_and_skips_zero_quantity_rows(self):
        timestamp = datetime(2026, 6, 10, 9, 2, tzinfo=timezone.utc)
        response = {
            "rt_cd": "0",
            "output1": [
                {
                    "pdno": "005930",
                    "hldg_qty": "3",
                    "ord_psbl_qty": "2",
                    "pchs_avg_pric": "69000",
                    "prpr": "70000",
                },
                {
                    "pdno": "000660",
                    "hldg_qty": "0",
                    "pchs_avg_pric": "180000",
                    "prpr": "182000",
                },
            ],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                }
            ],
        }

        snapshot = parse_kis_account_snapshot(response, timestamp=timestamp)

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(["005930"], list(snapshot.positions))
        position = snapshot.positions["005930"]
        self.assertEqual(3, position.quantity)
        self.assertEqual(2, position.sellable_quantity)
        self.assertEqual(Decimal("69000"), position.avg_price)
        self.assertEqual(Decimal("70000"), position.last_price)
        self.assertEqual(timestamp, position.opened_at)
        self.assertEqual(Decimal("70000"), position.highest_price)
        self.assertEqual(Decimal("1210000"), snapshot.equity)
        self.assertFalse(snapshot.realized_pnl_today_known)

    def test_parse_balance_response_aggregates_duplicate_symbol_rows(self):
        response = {
            "rt_cd": "0",
            "output1": [
                {"pdno": "005930", "hldg_qty": "2", "pchs_avg_pric": "69000", "prpr": "72000"},
                {"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "71000", "prpr": "72000"},
            ],
            "output2": [{"dnca_tot_amt": "1000000"}],
        }

        snapshot = parse_kis_account_snapshot(response)

        position = snapshot.positions["005930"]
        self.assertEqual(5, position.quantity)
        self.assertEqual(Decimal("70200"), position.avg_price)
        self.assertEqual(Decimal("72000"), position.last_price)
        self.assertEqual(Decimal("1360000"), snapshot.equity)

    def test_parse_balance_response_aggregates_duplicate_sellable_quantities(self):
        response = {
            "rt_cd": "0",
            "output1": [
                {"pdno": "005930", "hldg_qty": "2", "ord_psbl_qty": "1", "pchs_avg_pric": "69000", "prpr": "72000"},
                {"pdno": "005930", "hldg_qty": "3", "ord_psbl_qty": "2", "pchs_avg_pric": "71000", "prpr": "72000"},
            ],
            "output2": [{"dnca_tot_amt": "1000000"}],
        }

        snapshot = parse_kis_account_snapshot(response)

        self.assertEqual(3, snapshot.positions["005930"].sellable_quantity)

    def test_parse_balance_response_separates_deposit_cash_from_orderable_cash(self):
        response = {
            "rt_cd": "0",
            "output1": [],
            "output2": [
                {
                    "ord_psbl_cash": "750000",
                    "dnca_tot_amt": "1000000",
                }
            ],
        }

        snapshot = parse_kis_account_snapshot(response)

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(Decimal("750000"), snapshot.buying_power)

    def test_parse_balance_response_does_not_fallback_when_orderable_cash_is_negative(self):
        response = {
            "rt_cd": "0",
            "output1": [],
            "output2": [
                {
                    "ord_psbl_cash": "-1",
                    "dnca_tot_amt": "1000000",
                }
            ],
        }

        snapshot = parse_kis_account_snapshot(response)

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(Decimal("0"), snapshot.buying_power)

    def test_parse_balance_response_displays_live_cash_but_fails_closed_without_orderable_cash(self):
        response = {
            "rt_cd": "0",
            "output1": [],
            "output2": [
                {
                    "dnca_tot_amt": "1000000",
                    "tot_evlu_amt": "1250000",
                }
            ],
        }

        snapshot = parse_kis_account_snapshot(response, allow_deposit_cash_fallback=False)

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(Decimal("1250000"), snapshot.equity)
        self.assertEqual(Decimal("0"), snapshot.buying_power)

    def test_parse_balance_response_uses_cash_zero_when_summary_is_missing(self):
        snapshot = parse_kis_account_snapshot({"rt_cd": "0", "output1": [], "output2": []})

        self.assertEqual(Decimal("0"), snapshot.cash)
        self.assertEqual({}, snapshot.positions)

    def test_missing_required_price_field_raises_sanitized_error(self):
        with self.assertRaisesRegex(ValueError, "missing KIS price field: stck_prpr") as context:
            parse_kis_price_bar({"output": {"stck_oprc": "1"}}, symbol="005930")

        self.assertNotIn("005930", str(context.exception))


if __name__ == "__main__":
    unittest.main()
