import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.live_reconciliation import (
    KisLiveOrderReconciler,
    extract_live_order_number,
    extract_live_order_org_number,
    parse_kis_daily_order_executions,
)
from stockbot.models import Order


class FakeExecutionClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def inquire_daily_orders(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class LiveReconciliationTest(unittest.TestCase):
    def test_extracts_order_number_from_live_submission_response(self):
        self.assertEqual("12345", extract_live_order_number({"output": {"ODNO": "12345"}}))
        self.assertEqual("67890", extract_live_order_number({"output": {"odno": "67890"}}))
        self.assertEqual("", extract_live_order_number({"output": {}}))

    def test_extracts_order_org_number_from_live_submission_response(self):
        self.assertEqual("54321", extract_live_order_org_number({"output": {"KRX_FWDG_ORD_ORGNO": "54321"}}))
        self.assertEqual("12345", extract_live_order_org_number({"output": {"ord_gno_brno": "12345"}}))
        self.assertEqual("", extract_live_order_org_number({"output": {}}))

    def test_parses_kis_daily_order_execution_statuses(self):
        response = {
            "output1": [
                {
                    "odno": "0000012345",
                    "ord_gno_brno": "54321",
                    "pdno": "005930",
                    "prdt_name": "삼성전자",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "tot_ccld_qty": "2",
                    "rmn_qty": "1",
                    "ord_unpr": "70000",
                    "avg_prvs": "70100",
                    "ord_tmd": "090101",
                },
                {
                    "odno": "0000012346",
                    "pdno": "000660",
                    "sll_buy_dvsn_cd_name": "매도",
                    "ord_qty": "4",
                    "tot_ccld_qty": "4",
                    "ord_unpr": "100000",
                    "avg_prvs": "99900",
                },
            ]
        }

        executions = parse_kis_daily_order_executions(response)

        self.assertEqual(2, len(executions))
        self.assertEqual("0000012345", executions[0].order_no)
        self.assertEqual("54321", executions[0].order_org_no)
        self.assertEqual("BUY", executions[0].side)
        self.assertEqual(3, executions[0].order_quantity)
        self.assertEqual(2, executions[0].filled_quantity)
        self.assertEqual(1, executions[0].unfilled_quantity)
        self.assertEqual(Decimal("70100"), executions[0].average_fill_price)
        self.assertEqual("partial", executions[0].status)
        self.assertEqual("SELL", executions[1].side)
        self.assertEqual("filled", executions[1].status)

    def test_parses_rejected_and_canceled_order_statuses(self):
        response = {
            "output1": [
                {
                    "odno": "0000012347",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "tot_ccld_qty": "0",
                    "rmn_qty": "0",
                    "rjct_qty": "3",
                },
                {
                    "odno": "0000012348",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "tot_ccld_qty": "0",
                    "rmn_qty": "0",
                    "cncl_yn": "Y",
                },
            ]
        }

        executions = parse_kis_daily_order_executions(response)

        self.assertEqual("rejected", executions[0].status)
        self.assertEqual("canceled", executions[1].status)

    def test_reconciler_queries_today_order_and_returns_matching_execution(self):
        response = {
            "output1": [
                {
                    "odno": "0000012345",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "3",
                    "tot_ccld_qty": "3",
                    "rmn_qty": "0",
                    "ord_unpr": "70000",
                    "avg_prvs": "70100",
                }
            ]
        }
        client = FakeExecutionClient(response)
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile(
            Order.buy("005930", 3, "entry"),
            {"output": {"ODNO": "0000012345"}},
            query_date=date(2026, 7, 3),
        )

        self.assertEqual("filled", result.status)
        self.assertEqual(3, result.filled_quantity)
        self.assertEqual(Decimal("70100"), result.average_fill_price)
        self.assertEqual(
            {
                "inquiry_start_date": date(2026, 7, 3),
                "inquiry_end_date": date(2026, 7, 3),
                "order_no": "0000012345",
                "symbol": "005930",
                "side_code": "02",
                "execution_code": "00",
                "ctx_area_fk100": "",
                "ctx_area_nk100": "",
                "tr_cont": "",
            },
            client.calls[0],
        )

    def test_reconciler_follows_continuation_to_matching_execution(self):
        client = FakeExecutionClient(
            [
                {
                    "tr_cont": "M",
                    "output1": [],
                    "output2": {"ctx_area_fk100": "next-fk", "ctx_area_nk100": "next-nk"},
                },
                {
                    "tr_cont": "",
                    "output1": [
                        {
                            "odno": "0000012345",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "3",
                            "tot_ccld_qty": "3",
                            "rmn_qty": "0",
                            "avg_prvs": "70100",
                        }
                    ],
                },
            ]
        )
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile(
            Order.buy("005930", 3, "entry"),
            {"output": {"ODNO": "0000012345"}},
            query_date=date(2026, 7, 3),
        )

        self.assertEqual("filled", result.status)
        self.assertEqual(2, len(client.calls))
        self.assertEqual("next-fk", client.calls[1]["ctx_area_fk100"])
        self.assertEqual("next-nk", client.calls[1]["ctx_area_nk100"])
        self.assertEqual("N", client.calls[1]["tr_cont"])

    def test_reconciler_treats_non_continuation_header_as_terminal(self):
        client = FakeExecutionClient(
            {
                "tr_cont": "D",
                "output1": [
                    {
                        "odno": "0000012345",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_qty": "3",
                        "tot_ccld_qty": "3",
                        "rmn_qty": "0",
                        "avg_prvs": "70100",
                    }
                ],
            }
        )
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile(
            Order.buy("005930", 3, "entry"),
            {"output": {"ODNO": "0000012345"}},
            query_date=date(2026, 7, 3),
        )

        self.assertEqual("filled", result.status)
        self.assertEqual(3, result.filled_quantity)
        self.assertEqual(1, len(client.calls))

    def test_reconciler_reports_missing_order_number_without_querying(self):
        client = FakeExecutionClient({"output1": []})
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile(Order.buy("005930", 3, "entry"), {"output": {}})

        self.assertEqual("submitted_without_order_no", result.status)
        self.assertEqual([], client.calls)

    def test_reconciler_does_not_match_wrong_side_execution(self):
        response = {
            "output1": [
                {
                    "odno": "0000012345",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "01",
                    "ord_qty": "3",
                    "tot_ccld_qty": "3",
                    "rmn_qty": "0",
                    "ord_unpr": "70000",
                    "avg_prvs": "70100",
                }
            ]
        }
        client = FakeExecutionClient(response)
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile(
            Order.buy("005930", 3, "entry"),
            {"output": {"ODNO": "0000012345"}},
            query_date=date(2026, 7, 3),
        )

        self.assertEqual("not_found", result.status)
        self.assertEqual(0, result.filled_quantity)

    def test_reconciles_daily_buy_entry_counts_across_complete_pages(self):
        client = FakeExecutionClient(
            [
                {
                    "tr_cont": "M",
                    "output1": [
                        {
                            "odno": "1001",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "3",
                            "tot_ccld_qty": "1",
                            "rmn_qty": "2",
                        },
                        {
                            "odno": "1002",
                            "ord_gno_brno": "A",
                            "pdno": "035420",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "1",
                            "tot_ccld_qty": "0",
                            "rmn_qty": "1",
                        },
                    ],
                    "output2": {"ctx_area_fk100": "next-fk", "ctx_area_nk100": "next-nk"},
                },
                {
                    "tr_cont": "",
                    "output1": [
                        {
                            "odno": "1001",
                            "ord_gno_brno": "A",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "3",
                            "tot_ccld_qty": "3",
                            "rmn_qty": "0",
                        },
                        {
                            "odno": "1003",
                            "ord_gno_brno": "A",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "1",
                            "tot_ccld_qty": "1",
                            "rmn_qty": "0",
                        },
                    ],
                    "output2": {"ctx_area_fk100": "", "ctx_area_nk100": ""},
                },
            ]
        )
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile_entry_counts(date(2026, 7, 10))

        self.assertEqual(date(2026, 7, 10), result.trading_day)
        self.assertEqual({"005930": 2}, result.entry_counts)
        self.assertEqual(2, len(client.calls))
        self.assertEqual("02", client.calls[0]["side_code"])
        self.assertEqual("01", client.calls[0]["execution_code"])
        self.assertEqual("", client.calls[0]["tr_cont"])
        self.assertEqual("next-fk", client.calls[1]["ctx_area_fk100"])
        self.assertEqual("next-nk", client.calls[1]["ctx_area_nk100"])
        self.assertEqual("N", client.calls[1]["tr_cont"])

    def test_daily_entry_count_reconciliation_rejects_incomplete_continuation(self):
        client = FakeExecutionClient(
            {
                "tr_cont": "M",
                "output1": [],
                "output2": {"ctx_area_fk100": "", "ctx_area_nk100": ""},
            }
        )
        reconciler = KisLiveOrderReconciler(client)

        with self.assertRaisesRegex(ValueError, "continuation"):
            reconciler.reconcile_entry_counts(date(2026, 7, 10))
        self.assertEqual(1, len(client.calls))

    def test_daily_entry_count_reconciliation_requires_both_continuation_keys(self):
        client = FakeExecutionClient(
            {
                "tr_cont": "M",
                "output1": [],
                "output2": {"ctx_area_fk100": "next-fk", "ctx_area_nk100": ""},
            }
        )
        reconciler = KisLiveOrderReconciler(client)

        with self.assertRaisesRegex(ValueError, "continuation"):
            reconciler.reconcile_entry_counts(date(2026, 7, 10))

    def test_daily_entry_count_reconciliation_treats_non_continuation_header_as_terminal(self):
        client = FakeExecutionClient(
            {
                "tr_cont": "D",
                "output1": [
                    {
                        "odno": "1001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_qty": "1",
                        "tot_ccld_qty": "1",
                        "rmn_qty": "0",
                    }
                ],
            }
        )
        reconciler = KisLiveOrderReconciler(client)

        result = reconciler.reconcile_entry_counts(date(2026, 7, 10))

        self.assertEqual({"005930": 1}, result.entry_counts)
        self.assertEqual(1, len(client.calls))

    def test_daily_entry_count_reconciliation_rejects_ambiguous_fill_quantity(self):
        client = FakeExecutionClient(
            {
                "tr_cont": "",
                "output1": [
                    {
                        "odno": "1001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_qty": "1",
                        "tot_ccld_qty": "not-a-number",
                        "rmn_qty": "0",
                    }
                ],
            }
        )
        reconciler = KisLiveOrderReconciler(client)

        with self.assertRaisesRegex(ValueError, "filled quantity"):
            reconciler.reconcile_entry_counts(date(2026, 7, 10))


if __name__ == "__main__":
    unittest.main()
