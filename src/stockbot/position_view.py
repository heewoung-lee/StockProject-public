from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Mapping

from stockbot.models import Position
from stockbot.symbols import SymbolDirectory


LEGEND_LABELS = (
    "실선: 최근 모의 가격 흐름",
    "점선: 평균 진입가/손절선/익절선/트레일링 기준",
    "진입: paper 포지션 시작",
    "현재: 최신 모의 가격",
)


@dataclass(frozen=True)
class PositionRow:
    symbol: str
    company_name: str
    label: str
    side_label: str
    quantity: int
    avg_price: str
    last_price: str
    unrealized_pnl: str


@dataclass(frozen=True)
class PositionDetail:
    symbol: str = ""
    company_name: str = ""
    label: str = ""
    summary: str = "보유 포지션을 선택하세요."
    side_label: str = ""
    quantity: int = 0
    avg_price: str = ""
    last_price: str = ""
    unrealized_pnl: str = ""
    price_points: tuple[tuple[datetime, Decimal], ...] = ()
    reference_lines: tuple[tuple[str, Decimal], ...] = ()
    legend_labels: tuple[str, ...] = LEGEND_LABELS

    @classmethod
    def empty(cls) -> "PositionDetail":
        return cls()


def build_position_rows(
    positions: Mapping[str, Position],
    symbols: SymbolDirectory,
) -> list[PositionRow]:
    return [_row_for(position, symbols) for position in positions.values()]


def build_position_detail(
    selected_symbol: str,
    positions: Mapping[str, Position],
    symbols: SymbolDirectory,
    *,
    stop_loss_pct: Decimal = Decimal("0.02"),
    take_profit_pct: Decimal = Decimal("0.03"),
    trailing_stop_pct: Decimal = Decimal("0.015"),
) -> PositionDetail:
    position = positions.get(selected_symbol)
    if position is None:
        return PositionDetail.empty()

    company_name = symbols.name_for(position.symbol)
    label = symbols.label_for(position.symbol)
    reference_lines = reference_lines_for(
        position,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        trailing_stop_pct=trailing_stop_pct,
    )
    return PositionDetail(
        symbol=position.symbol,
        company_name=company_name,
        label=label,
        summary=f"{label} {side_label_for(position)} {position.quantity}주",
        side_label=side_label_for(position),
        quantity=position.quantity,
        avg_price=format_krw(position.avg_price),
        last_price=format_krw(position.last_price),
        unrealized_pnl=format_krw(position.unrealized_pnl),
        price_points=price_points_for(position),
        reference_lines=reference_lines,
        legend_labels=LEGEND_LABELS,
    )


def _row_for(position: Position, symbols: SymbolDirectory) -> PositionRow:
    return PositionRow(
        symbol=position.symbol,
        company_name=symbols.name_for(position.symbol),
        label=symbols.label_for(position.symbol),
        side_label=side_label_for(position),
        quantity=position.quantity,
        avg_price=format_krw(position.avg_price),
        last_price=format_krw(position.last_price),
        unrealized_pnl=format_krw(position.unrealized_pnl),
    )


def side_label_for(position: Position) -> str:
    return "숏" if position.side == "SHORT" else "롱"


def format_krw(value: Decimal) -> str:
    whole = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    return f"{whole:,.0f}원" if whole == whole.to_integral_value() else f"{whole:,.2f}원"


def price_points_for(position: Position) -> tuple[tuple[datetime, Decimal], ...]:
    if position.price_history:
        return tuple(position.price_history)
    return ((position.opened_at, position.last_price),)


def reference_lines_for(
    position: Position,
    *,
    stop_loss_pct: Decimal = Decimal("0.02"),
    take_profit_pct: Decimal = Decimal("0.03"),
    trailing_stop_pct: Decimal = Decimal("0.015"),
) -> tuple[tuple[str, Decimal], ...]:
    avg = position.avg_price
    if position.side == "SHORT":
        lines: list[tuple[str, Decimal]] = [
            ("평균 진입가", avg),
            ("손절선", avg * (Decimal("1") + stop_loss_pct)),
            ("익절선", avg * (Decimal("1") - take_profit_pct)),
        ]
        low_water = position.lowest_price if position.lowest_price is not None else position.last_price
        if trailing_stop_pct > 0 and low_water < avg:
            lines.append(("트레일링선", low_water * (Decimal("1") + trailing_stop_pct)))
        return tuple(lines)

    lines = [
        ("평균 진입가", avg),
        ("손절선", avg * (Decimal("1") - stop_loss_pct)),
        ("익절선", avg * (Decimal("1") + take_profit_pct)),
    ]
    if trailing_stop_pct > 0 and position.highest_price > avg:
        lines.append(("트레일링선", position.highest_price * (Decimal("1") - trailing_stop_pct)))
    return tuple(lines)
