import sys
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.models import Position
from stockbot.position_view import PositionDetail, build_position_detail, build_position_rows
from stockbot.symbols import SymbolDirectory


def make_position(symbol="005930", quantity=2, avg="70000", last="71000", side="LONG"):
    last_price = Decimal(last)
    return Position(
        symbol=symbol,
        quantity=quantity,
        avg_price=Decimal(avg),
        last_price=last_price,
        opened_at=datetime(2026, 6, 11, 9, 0),
        highest_price=max(Decimal(avg), last_price),
        side=side,
        lowest_price=min(Decimal(avg), last_price),
    )


def make_position_with_history(symbol="005930", side="LONG"):
    return Position(
        symbol=symbol,
        quantity=2,
        avg_price=Decimal("10000"),
        last_price=Decimal("10300"),
        opened_at=datetime(2026, 6, 11, 9, 0),
        highest_price=Decimal("10400"),
        side=side,
        lowest_price=Decimal("9900"),
        price_history=(
            (datetime(2026, 6, 11, 9, 0), Decimal("10000")),
            (datetime(2026, 6, 11, 9, 1), Decimal("10100")),
            (datetime(2026, 6, 11, 9, 2), Decimal("10300")),
        ),
    )


class PositionViewTest(unittest.TestCase):
    def test_position_rows_include_company_name_code_side_quantity_and_prices(self):
        rows = build_position_rows(
            positions={"005930": make_position(quantity=2, avg="70000", last="71000")},
            symbols=SymbolDirectory({"005930": "삼성전자"}),
        )

        self.assertEqual("삼성전자", rows[0].company_name)
        self.assertEqual("005930", rows[0].symbol)
        self.assertEqual("삼성전자 (005930)", rows[0].label)
        self.assertEqual("롱", rows[0].side_label)
        self.assertEqual(2, rows[0].quantity)
        self.assertEqual("70,000원", rows[0].avg_price)
        self.assertEqual("71,000원", rows[0].last_price)
        self.assertEqual("2,000원", rows[0].unrealized_pnl)

    def test_short_position_row_uses_short_side_label_and_pnl(self):
        rows = build_position_rows(
            positions={"005930": make_position(quantity=2, avg="70000", last="69000", side="SHORT")},
            symbols=SymbolDirectory({"005930": "삼성전자"}),
        )

        self.assertEqual("숏", rows[0].side_label)
        self.assertEqual("2,000원", rows[0].unrealized_pnl)

    def test_decimal_prices_and_loss_are_not_truncated_to_whole_won(self):
        rows = build_position_rows(
            positions={"005930": make_position(quantity=3, avg="100.75", last="100.50")},
            symbols=SymbolDirectory({"005930": "삼성전자"}),
        )

        self.assertEqual("100.75원", rows[0].avg_price)
        self.assertEqual("100.50원", rows[0].last_price)
        self.assertEqual("-0.75원", rows[0].unrealized_pnl)

    def test_empty_detail_tells_user_to_select_traded_symbol(self):
        detail = PositionDetail.empty()

        self.assertIn("보유 포지션을 선택", detail.summary)
        self.assertEqual((), detail.price_points)
        self.assertEqual((), detail.reference_lines)

    def test_selecting_symbol_returns_matching_detail_with_chart_and_legend(self):
        detail = build_position_detail(
            selected_symbol="000660",
            positions={
                "005930": make_position("005930", quantity=2, avg="70000", last="71000"),
                "000660": make_position("000660", quantity=3, avg="100000", last="99000", side="SHORT"),
            },
            symbols=SymbolDirectory({"000660": "SK하이닉스", "005930": "삼성전자"}),
        )

        self.assertEqual("000660", detail.symbol)
        self.assertEqual("SK하이닉스", detail.company_name)
        self.assertIn("SK하이닉스 (000660)", detail.summary)
        self.assertEqual("숏", detail.side_label)
        self.assertEqual(3, detail.quantity)
        self.assertEqual("100,000원", detail.avg_price)
        self.assertEqual("99,000원", detail.last_price)
        self.assertEqual("3,000원", detail.unrealized_pnl)
        self.assertEqual(((datetime(2026, 6, 11, 9, 0), Decimal("99000")),), detail.price_points)
        self.assertIn(("평균 진입가", Decimal("100000")), detail.reference_lines)
        self.assertIn(("손절선", Decimal("102000.00")), detail.reference_lines)
        self.assertIn(("익절선", Decimal("97000.00")), detail.reference_lines)
        self.assertIn("실선: 최근 모의 가격 흐름", detail.legend_labels)
        self.assertIn("점선: 평균 진입가/손절선/익절선/트레일링 기준", detail.legend_labels)
        self.assertIn("진입: paper 포지션 시작", detail.legend_labels)
        self.assertIn("현재: 최신 모의 가격", detail.legend_labels)


    def test_detail_exposes_price_flow_and_exit_reference_lines(self):
        detail = build_position_detail(
            selected_symbol="005930",
            positions={"005930": make_position_with_history()},
            symbols=SymbolDirectory({"005930": "Samsung Electronics"}),
            stop_loss_pct=Decimal("0.02"),
            take_profit_pct=Decimal("0.03"),
            trailing_stop_pct=Decimal("0.015"),
        )

        self.assertEqual(
            (
                (datetime(2026, 6, 11, 9, 0), Decimal("10000")),
                (datetime(2026, 6, 11, 9, 1), Decimal("10100")),
                (datetime(2026, 6, 11, 9, 2), Decimal("10300")),
            ),
            detail.price_points,
        )
        self.assertIn(("평균 진입가", Decimal("10000")), detail.reference_lines)
        self.assertIn(("손절선", Decimal("9800.00")), detail.reference_lines)
        self.assertIn(("익절선", Decimal("10300.00")), detail.reference_lines)
        self.assertIn(("트레일링선", Decimal("10244.000")), detail.reference_lines)
        self.assertIn("실선: 최근 모의 가격 흐름", detail.legend_labels)

    def test_short_detail_uses_inverse_exit_reference_lines(self):
        detail = build_position_detail(
            selected_symbol="005930",
            positions={"005930": make_position_with_history(side="SHORT")},
            symbols=SymbolDirectory({"005930": "Samsung Electronics"}),
            stop_loss_pct=Decimal("0.02"),
            take_profit_pct=Decimal("0.03"),
            trailing_stop_pct=Decimal("0.015"),
        )

        self.assertIn(("손절선", Decimal("10200.00")), detail.reference_lines)
        self.assertIn(("익절선", Decimal("9700.00")), detail.reference_lines)
        self.assertIn(("트레일링선", Decimal("10048.500")), detail.reference_lines)


if __name__ == "__main__":
    unittest.main()
