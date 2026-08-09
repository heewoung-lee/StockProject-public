from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Mapping, Protocol

from .models import Order


class LiveOrderInquiryClient(Protocol):
    def inquire_daily_orders(self, **kwargs) -> dict:
        ...


class LiveOrderReconciler(Protocol):
    def reconcile(
        self,
        order: Order,
        submission_response: Mapping[str, object],
        *,
        query_date: date | None = None,
    ) -> "LiveOrderReconciliation":
        ...


@dataclass(frozen=True)
class LiveOrderExecution:
    order_no: str
    order_org_no: str
    symbol: str
    side: str
    order_quantity: int
    filled_quantity: int
    unfilled_quantity: int
    order_price: Decimal
    average_fill_price: Decimal
    order_time: str = ""
    raw: Mapping[str, object] | None = None

    @property
    def status(self) -> str:
        if self._is_rejected():
            return "rejected"
        if self._is_canceled():
            return "canceled"
        if self.filled_quantity <= 0 and self.unfilled_quantity > 0:
            return "pending"
        if self.filled_quantity > 0 and self.unfilled_quantity > 0:
            return "partial"
        if self.filled_quantity >= self.order_quantity and self.order_quantity > 0:
            return "filled"
        if self.filled_quantity > 0:
            return "partial"
        return "unknown"

    def _is_canceled(self) -> bool:
        row = self.raw or {}
        if _truthy_value(row, "cncl_yn", "CNCL_YN", "ord_cncl_yn", "ORD_CNCL_YN"):
            return True
        status_text = _first_string(
            row,
            "ccld_dvsn_name",
            "CCLD_DVSN_NAME",
            "ord_dvsn_name",
            "ORD_DVSN_NAME",
            "ord_stat_name",
            "ORD_STAT_NAME",
        ).lower()
        return self.filled_quantity <= 0 and any(token in status_text for token in ("cancel", "cancelled", "canceled"))

    def _is_rejected(self) -> bool:
        row = self.raw or {}
        return _int_value(row, "rjct_qty", "RJCT_QTY") > 0


@dataclass(frozen=True)
class LiveOrderReconciliation:
    order_no: str
    status: str
    filled_quantity: int = 0
    unfilled_quantity: int = 0
    average_fill_price: Decimal = Decimal("0")
    execution: LiveOrderExecution | None = None

    @property
    def is_filled(self) -> bool:
        return self.status == "filled"

    @property
    def is_partial(self) -> bool:
        return self.status == "partial"

    @property
    def is_pending(self) -> bool:
        return self.status in {"pending", "not_found", "submitted_without_order_no", "unknown"}

    @property
    def is_terminal(self) -> bool:
        return self.status in {"filled", "canceled", "rejected", "expired"}


@dataclass(frozen=True)
class LiveEntryCountReconciliation:
    trading_day: date
    entry_counts: Mapping[str, int]


class KisLiveOrderReconciler:
    def __init__(self, client: LiveOrderInquiryClient):
        self.client = client

    def reconcile(
        self,
        order: Order,
        submission_response: Mapping[str, object],
        *,
        query_date: date | None = None,
    ) -> LiveOrderReconciliation:
        order_no = extract_live_order_number(submission_response)
        if not order_no:
            return LiveOrderReconciliation(order_no="", status="submitted_without_order_no")

        pages = self._inquire_daily_order_pages(
            trading_day=query_date,
            order_no=order_no,
            symbol=order.symbol,
            side_code=_side_code_for_order(order),
            execution_code="00",
        )
        executions = tuple(
            execution
            for page in pages
            for execution in parse_kis_daily_order_executions(page)
        )
        match = _matching_execution(order, order_no, executions)
        if match is None:
            return LiveOrderReconciliation(order_no=order_no, status="not_found")
        return LiveOrderReconciliation(
            order_no=match.order_no,
            status=match.status,
            filled_quantity=match.filled_quantity,
            unfilled_quantity=match.unfilled_quantity,
            average_fill_price=match.average_fill_price,
            execution=match,
        )

    def reconcile_entry_counts(self, trading_day: date) -> LiveEntryCountReconciliation:
        filled_orders: set[tuple[str, str]] = set()
        pages = self._inquire_daily_order_pages(
            trading_day=trading_day,
            order_no="",
            symbol="",
            side_code="02",
            execution_code="01",
        )
        for response in pages:
            rows = _strict_daily_order_rows(response)
            for row in rows:
                side = _parse_side(row)
                if side != "BUY":
                    raise ValueError("KIS daily buy entry reconciliation returned an unexpected side")
                symbol = _first_string(row, "pdno", "PDNO")
                order_no = _first_string(row, "odno", "ODNO", "ord_no", "ORD_NO")
                if not symbol or not order_no:
                    raise ValueError("KIS daily buy entry reconciliation returned an incomplete order identity")
                filled_quantity = _required_nonnegative_int(
                    row,
                    "tot_ccld_qty",
                    "TOT_CCLD_QTY",
                    "ccld_qty",
                    "CCLD_QTY",
                    field_name="filled quantity",
                )
                if filled_quantity > 0:
                    filled_orders.add((order_no, symbol))
        counts: dict[str, int] = {}
        for _, symbol in filled_orders:
            counts[symbol] = counts.get(symbol, 0) + 1
        return LiveEntryCountReconciliation(trading_day, counts)

    def _inquire_daily_order_pages(
        self,
        *,
        trading_day: date | None,
        order_no: str,
        symbol: str,
        side_code: str,
        execution_code: str,
    ) -> tuple[Mapping[str, object], ...]:
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        seen_continuations: set[tuple[str, str]] = set()
        pages: list[Mapping[str, object]] = []
        for _ in range(10):
            response = self.client.inquire_daily_orders(
                inquiry_start_date=trading_day,
                inquiry_end_date=trading_day,
                order_no=order_no,
                symbol=symbol,
                side_code=side_code,
                execution_code=execution_code,
                ctx_area_fk100=ctx_area_fk100,
                ctx_area_nk100=ctx_area_nk100,
                tr_cont=tr_cont,
            )
            if not isinstance(response, Mapping):
                raise ValueError("KIS daily order reconciliation response is invalid")
            pages.append(response)
            continuation = _first_string(response, "tr_cont", "TR_CONT").upper()
            if continuation not in {"M", "F"}:
                return tuple(pages)
            ctx_area_fk100, ctx_area_nk100 = _daily_order_continuation_keys(response)
            if not ctx_area_fk100 or not ctx_area_nk100:
                raise ValueError("KIS daily order reconciliation continuation keys are missing")
            continuation_key = (ctx_area_fk100, ctx_area_nk100)
            if continuation_key in seen_continuations:
                raise ValueError("KIS daily order reconciliation continuation keys repeated")
            seen_continuations.add(continuation_key)
            tr_cont = "N"
        raise ValueError("KIS daily order reconciliation exceeded the continuation page limit")


def extract_live_order_number(response: Mapping[str, object]) -> str:
    output = response.get("output")
    if isinstance(output, Mapping):
        return _first_string(output, "ODNO", "odno", "ord_no", "ORD_NO")
    return _first_string(response, "ODNO", "odno", "ord_no", "ORD_NO")


def extract_live_order_org_number(response: Mapping[str, object]) -> str:
    output = response.get("output")
    keys = ("KRX_FWDG_ORD_ORGNO", "krx_fwdg_ord_orgno", "ORD_GNO_BRNO", "ord_gno_brno")
    if isinstance(output, Mapping):
        return _first_string(output, *keys)
    return _first_string(response, *keys)


def parse_kis_daily_order_executions(response: Mapping[str, object]) -> tuple[LiveOrderExecution, ...]:
    rows = response.get("output1")
    if not isinstance(rows, list):
        return ()
    executions: list[LiveOrderExecution] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        executions.append(
            LiveOrderExecution(
                order_no=_first_string(row, "odno", "ODNO", "ord_no", "ORD_NO"),
                order_org_no=_first_string(
                    row,
                    "ord_gno_brno",
                    "ORD_GNO_BRNO",
                    "krx_fwdg_ord_orgno",
                    "KRX_FWDG_ORD_ORGNO",
                ),
                symbol=_first_string(row, "pdno", "PDNO"),
                side=_parse_side(row),
                order_quantity=_int_value(row, "ord_qty", "ORD_QTY"),
                filled_quantity=_int_value(row, "tot_ccld_qty", "TOT_CCLD_QTY", "ccld_qty", "CCLD_QTY"),
                unfilled_quantity=_int_value(row, "rmn_qty", "RMN_QTY"),
                order_price=_decimal_value(row, "ord_unpr", "ORD_UNPR"),
                average_fill_price=_decimal_value(row, "avg_prvs", "AVG_PRVS", "avg_ccld_pric", "AVG_CCLD_PRIC"),
                order_time=_first_string(row, "ord_tmd", "ORD_TMD"),
                raw=dict(row),
            )
        )
    return tuple(executions)


def _matching_execution(
    order: Order,
    order_no: str,
    executions: tuple[LiveOrderExecution, ...],
) -> LiveOrderExecution | None:
    for execution in executions:
        if execution.order_no == order_no and execution.symbol == order.symbol and execution.side == order.side:
            return execution
    return None


def _side_code_for_order(order: Order) -> str:
    if order.side == "BUY":
        return "02"
    if order.side == "SELL":
        return "01"
    return "00"


def _parse_side(row: Mapping[str, object]) -> str:
    side_code = _first_string(row, "sll_buy_dvsn_cd", "SLL_BUY_DVSN_CD")
    if side_code == "01":
        return "SELL"
    if side_code == "02":
        return "BUY"
    side_name = _first_string(row, "sll_buy_dvsn_cd_name", "SLL_BUY_DVSN_CD_NAME")
    if "매도" in side_name:
        return "SELL"
    if "매수" in side_name:
        return "BUY"
    return "UNKNOWN"


def _first_string(row: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None:
            return str(value).strip()
    return ""


def _strict_daily_order_rows(response: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(response, Mapping):
        raise ValueError("KIS daily buy entry reconciliation response is invalid")
    rows = response.get("output1")
    if not isinstance(rows, list):
        raise ValueError("KIS daily buy entry reconciliation rows are invalid")
    if not all(isinstance(row, Mapping) for row in rows):
        raise ValueError("KIS daily buy entry reconciliation row is invalid")
    return tuple(rows)


def _daily_order_continuation_keys(response: Mapping[str, object]) -> tuple[str, str]:
    direct = (
        _first_string(response, "ctx_area_fk100", "CTX_AREA_FK100"),
        _first_string(response, "ctx_area_nk100", "CTX_AREA_NK100"),
    )
    if direct != ("", ""):
        return direct
    output2 = response.get("output2")
    if isinstance(output2, Mapping):
        return (
            _first_string(output2, "ctx_area_fk100", "CTX_AREA_FK100"),
            _first_string(output2, "ctx_area_nk100", "CTX_AREA_NK100"),
        )
    if isinstance(output2, list) and output2 and isinstance(output2[0], Mapping):
        return (
            _first_string(output2[0], "ctx_area_fk100", "CTX_AREA_FK100"),
            _first_string(output2[0], "ctx_area_nk100", "CTX_AREA_NK100"),
        )
    return "", ""


def _required_nonnegative_int(
    row: Mapping[str, object],
    *keys: str,
    field_name: str,
) -> int:
    text = _first_string(row, *keys).replace(",", "")
    if not text:
        raise ValueError(f"KIS daily buy entry reconciliation {field_name} is missing")
    try:
        parsed_decimal = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"KIS daily buy entry reconciliation {field_name} is invalid") from exc
    if not parsed_decimal.is_finite() or parsed_decimal < 0 or parsed_decimal != parsed_decimal.to_integral_value():
        raise ValueError(f"KIS daily buy entry reconciliation {field_name} is invalid")
    return int(parsed_decimal)


def _int_value(row: Mapping[str, object], *keys: str) -> int:
    text = _first_string(row, *keys).replace(",", "")
    if not text:
        return 0
    try:
        return int(Decimal(text))
    except (InvalidOperation, ValueError):
        return 0


def _decimal_value(row: Mapping[str, object], *keys: str) -> Decimal:
    text = _first_string(row, *keys).replace(",", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _truthy_value(row: Mapping[str, object], *keys: str) -> bool:
    text = _first_string(row, *keys).strip().lower()
    return text in {"y", "yes", "true", "1"}
