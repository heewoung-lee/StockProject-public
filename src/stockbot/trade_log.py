from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .runtime import RuntimeEvent


SIDE_LABELS = {
    "BUY": "매수",
    "SELL": "매도",
    "SHORT_ENTRY": "숏 진입",
    "SHORT_EXIT": "숏 청산",
    "HOLD": "관망",
}
SENSITIVE_TERMS = (
    "secret",
    "appsecret",
    "appkey",
    "apikey",
    "api_key",
    "bearer",
    "authorization",
    "token",
    "kisvtsapp",
)


@dataclass(frozen=True)
class TradeLogEntry:
    title: str
    detail: str
    timestamp: datetime | None = None
    symbol: str = ""
    company_name: str = ""
    side: str = ""
    side_label: str = ""
    quantity: int = 0
    price: Decimal = Decimal("0")
    result: str = ""
    reason: str = ""
    mode: str = ""
    realized_pnl: Decimal = Decimal("0")


def build_trade_log_entry(event: RuntimeEvent) -> TradeLogEntry:
    if event.kind != "trade":
        raise ValueError("trade log entries require trade events")

    side_label = _safe_text(SIDE_LABELS.get(event.side, event.side or "알 수 없음"))
    company_label = _company_label(event)
    result = _safe_text(event.result or "-")
    detail_parts = [
        f"{event.quantity:,}주",
        f"{_format_krw(event.price)}",
        f"결과 {result}",
        f"사유 {_safe_text(event.reason or '-')}",
        f"모드 {_safe_text(event.mode)}",
    ]
    if event.realized_pnl != 0:
        detail_parts.append(f"실현손익 {_format_krw(event.realized_pnl)}")

    return TradeLogEntry(
        title=f"[{event.timestamp:%H:%M:%S}] {side_label} {result} - {company_label}",
        detail=" / ".join(detail_parts),
        timestamp=event.timestamp,
        symbol=_safe_text(event.symbol),
        company_name=_safe_text(event.company_name),
        side=_safe_text(event.side),
        side_label=side_label,
        quantity=event.quantity,
        price=Decimal(str(event.price)),
        result=result,
        reason=_safe_text(event.reason or "-"),
        mode=_safe_text(event.mode),
        realized_pnl=Decimal(str(event.realized_pnl)),
    )


def _company_label(event: RuntimeEvent) -> str:
    safe_symbol = _safe_text(event.symbol)
    safe_company = _safe_text(event.company_name)
    if safe_company and safe_symbol:
        return f"{safe_company} ({safe_symbol})"
    if event.company_name:
        return safe_company
    return safe_symbol


def _format_krw(value: Decimal) -> str:
    whole = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    return f"{whole:,.0f}원" if whole == whole.to_integral_value() else f"{whole:,.2f}원"


def _safe_text(value: object) -> str:
    text = str(value)
    if _contains_sensitive_term(text) or re.search(r"\d{8,}", text):
        return "민감정보 숨김"
    return text


def _contains_sensitive_term(value: str) -> bool:
    normalized = re.sub(r"[\s_-]+", "", value.lower())
    return any(term.replace("_", "") in normalized for term in SENSITIVE_TERMS)
