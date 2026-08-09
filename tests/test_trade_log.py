import unittest
from decimal import Decimal

from stockbot.runtime import RuntimeEvent
from stockbot.trade_log import SIDE_LABELS, build_trade_log_entry


class TradeLogTest(unittest.TestCase):
    def test_trade_log_contains_company_code_side_quantity_price_result_reason_and_mode(self):
        event = RuntimeEvent.trade(
            symbol="005930",
            company_name="삼성전자",
            side="BUY",
            quantity=3,
            price=Decimal("70000"),
            reason="flow_breakout",
            result="filled",
        )

        entry = build_trade_log_entry(event)

        self.assertIn("매수", entry.title)
        self.assertIn("삼성전자 (005930)", entry.title)
        self.assertIn("3주", entry.detail)
        self.assertIn("70,000원", entry.detail)
        self.assertIn("filled", entry.detail)
        self.assertIn("flow_breakout", entry.detail)
        self.assertIn("paper", entry.detail)

    def test_trade_log_maps_supported_sides_to_korean_labels(self):
        self.assertEqual(
            {
                "BUY": "매수",
                "SELL": "매도",
                "SHORT_ENTRY": "숏 진입",
                "SHORT_EXIT": "숏 청산",
                "HOLD": "관망",
            },
            SIDE_LABELS,
        )

    def test_trade_log_falls_back_to_symbol_when_company_name_is_missing(self):
        event = RuntimeEvent.trade(
            symbol="999999",
            side="HOLD",
            reason="no_signal",
            result="skipped",
        )

        entry = build_trade_log_entry(event)

        self.assertIn("999999", entry.title)
        self.assertIn("관망", entry.title)

    def test_trade_log_includes_realized_pnl_when_present(self):
        event = RuntimeEvent.trade(
            symbol="005930",
            company_name="삼성전자",
            side="SELL",
            quantity=2,
            price=Decimal("71000"),
            reason="take_profit",
            result="filled",
            realized_pnl=Decimal("2000"),
        )

        entry = build_trade_log_entry(event)

        self.assertIn("실현손익 2,000원", entry.detail)

    def test_trade_log_exposes_structured_fields_for_dashboard_details(self):
        event = RuntimeEvent.trade(
            symbol="005930",
            company_name="삼성전자",
            side="SELL",
            quantity=2,
            price=Decimal("71000"),
            reason="take_profit",
            result="filled",
            realized_pnl=Decimal("2000"),
        )

        entry = build_trade_log_entry(event)

        self.assertEqual("005930", entry.symbol)
        self.assertEqual("삼성전자", entry.company_name)
        self.assertEqual("SELL", entry.side)
        self.assertEqual("매도", entry.side_label)
        self.assertEqual(2, entry.quantity)
        self.assertEqual(Decimal("71000"), entry.price)
        self.assertEqual("take_profit", entry.reason)
        self.assertEqual("filled", entry.result)
        self.assertEqual(Decimal("2000"), entry.realized_pnl)

    def test_trade_log_redacts_sensitive_trade_fields(self):
        event = RuntimeEvent.trade(
            symbol="12345678",
            company_name="Authorization Bearer leaked-value",
            side="SELL",
            quantity=1,
            price=Decimal("70000"),
            reason="bad token-123 12345678",
            result="rejected appsecret leaked",
            mode="paper api_key leaked",
        )

        entry = build_trade_log_entry(event)
        rendered = f"{entry.title} {entry.detail}"

        self.assertNotIn("Authorization", rendered)
        self.assertNotIn("Bearer", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)
        self.assertNotIn("appsecret", rendered.lower())
        self.assertNotIn("api_key", rendered.lower())
        self.assertIn("민감정보", rendered)

    def test_trade_log_rejects_non_trade_events(self):
        with self.assertRaises(ValueError):
            build_trade_log_entry(RuntimeEvent.system("자동 모의투자 루프 시작"))


if __name__ == "__main__":
    unittest.main()
