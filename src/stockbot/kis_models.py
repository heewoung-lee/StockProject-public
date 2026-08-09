from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .models import AccountSnapshot, MarketBar, Position


KST = timezone(timedelta(hours=9), name="KST")


class KisQuoteUnavailableError(ValueError):
    pass


@dataclass(frozen=True)
class KisPeriodProfitRow:
    trading_date: date
    realized_pnl: Decimal
    fee: Decimal
    tax: Decimal
    loan_interest: Decimal
    has_activity: bool | None = None
    cost_inclusion: str = field(default="unknown", init=False)


def parse_kis_price_bar(
    response: Mapping[str, object],
    *,
    symbol: str,
    timestamp: datetime | None = None,
    orderbook_response: Mapping[str, object] | None = None,
) -> MarketBar:
    output = _required_mapping(response, "output", "KIS price")
    close = _required_decimal(output, "stck_prpr", "KIS price")
    volume = _optional_int(output, "acml_vol", 0)
    bid: Decimal | None = None
    ask: Decimal | None = None
    if orderbook_response is not None:
        bid, ask = _best_quotes(orderbook_response)
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp or _utc_now(),
        open=_optional_decimal(output, "stck_oprc", close),
        high=_optional_decimal(output, "stck_hgpr", close),
        low=_optional_decimal(output, "stck_lwpr", close),
        close=close,
        volume=volume,
        vwap=_price_vwap(output, close, volume),
        bid=bid,
        ask=ask,
        upper_limit=_first_optional_decimal_or_none(output, ("stck_mxpr",)),
        lower_limit=_first_optional_decimal_or_none(output, ("stck_llam",)),
        market=_normalize_kis_market_name(output.get("rprs_mrkt_kor_name")),
        temporary_stop=_optional_yn_flag(output, "temp_stop_yn"),
        vi_code=_optional_text(output, "vi_cls_code"),
        security_status_code=_optional_text(output, "iscd_stat_cls_code"),
        trading_state_source="KIS_CURRENT_PRICE",
    )


def parse_kis_minute_bars(
    response: Mapping[str, object],
    *,
    symbol: str,
    trading_date: date | datetime | str | None = None,
    completed_before: datetime | None = None,
) -> list[MarketBar]:
    rows = _required_sequence(response, "output2", "KIS minute bars")
    expected_date = _date_key(trading_date) if trading_date is not None else None
    bars: list[MarketBar] = []
    for raw_row in rows:
        if not isinstance(raw_row, Mapping):
            raise ValueError("invalid KIS minute bar row")
        row_date = _required_text(raw_row, "stck_bsop_date", "KIS minute bar")
        row_time = _required_text(raw_row, "stck_cntg_hour", "KIS minute bar")
        if expected_date is not None and row_date != expected_date:
            raise ValueError("unexpected KIS minute bar trading date")
        timestamp = _minute_timestamp(row_date, row_time)
        if completed_before is not None and timestamp >= _as_kst(completed_before):
            continue
        open_price = _required_decimal(raw_row, "stck_oprc", "KIS minute bar")
        high = _required_decimal(raw_row, "stck_hgpr", "KIS minute bar")
        low = _required_decimal(raw_row, "stck_lwpr", "KIS minute bar")
        close = _required_decimal(raw_row, "stck_prpr", "KIS minute bar")
        volume = _required_int(raw_row, "cntg_vol", "KIS minute bar")
        if volume < 0:
            raise ValueError("invalid KIS minute bar field: cntg_vol")
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=timestamp,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
                vwap=_minute_vwap(raw_row, high=high, low=low, close=close),
            )
        )
    return sorted(bars, key=lambda bar: bar.timestamp)


def parse_kis_period_profit_rows(
    response: Mapping[str, object],
    *,
    start_date: date | datetime | str,
    end_date: date | datetime | str,
) -> tuple[KisPeriodProfitRow, ...]:
    start_key = _date_key(start_date)
    end_key = _date_key(end_date)
    start = _parse_kis_date_key(start_key, "requested start date")
    end = _parse_kis_date_key(end_key, "requested end date")
    if start > end:
        raise ValueError("KIS period profit start date must not be after end date")

    output1 = response.get("output1")
    output1_present = "output1" in response
    valid_empty = False
    rows_by_date: dict[date, KisPeriodProfitRow] = {}

    if isinstance(output1, Sequence) and not isinstance(output1, (str, bytes, bytearray)):
        valid_empty = len(output1) == 0
        for raw_row in output1:
            if not isinstance(raw_row, Mapping):
                raise ValueError("invalid KIS period profit row")
            row_date_text = _required_text(raw_row, "trad_dt", "KIS period profit")
            row_date = _parse_kis_date_key(row_date_text, "KIS period profit field: trad_dt")
            if row_date < start or row_date > end:
                raise ValueError("KIS period profit row outside requested range")
            if row_date in rows_by_date:
                raise ValueError("duplicate trading date in KIS period profit rows")
            rows_by_date[row_date] = KisPeriodProfitRow(
                trading_date=row_date,
                realized_pnl=_required_signed_decimal(raw_row, "rlzt_pfls", "KIS period profit"),
                fee=_optional_nonnegative_decimal(raw_row, "fee", "KIS period profit"),
                tax=_optional_nonnegative_decimal(raw_row, "tl_tax", "KIS period profit"),
                loan_interest=_optional_nonnegative_decimal(raw_row, "loan_int", "KIS period profit"),
                has_activity=True,
            )
    elif output1 is not None:
        raise ValueError("invalid KIS period profit field: output1")

    if rows_by_date:
        return tuple(rows_by_date[row_date] for row_date in sorted(rows_by_date))
    if start != end:
        if output1_present and output1 is not None:
            return ()
        raise ValueError("missing KIS period profit daily rows")

    summary_values: set[tuple[Decimal, Decimal, Decimal]] = set()
    output2_rows = _mapping_rows(response.get("output2"), "KIS period profit", allow_empty=True)
    for summary in output2_rows:
        realized_value = summary.get("tot_rlzt_pfls")
        if realized_value is None or str(realized_value).strip() == "":
            if any(
                summary.get(key) is not None and str(summary.get(key)).strip() != ""
                for key in ("tot_fee", "tot_tltx")
            ):
                raise ValueError("missing KIS period profit field: tot_rlzt_pfls")
            continue
        summary_values.add(
            (
                _required_signed_decimal(summary, "tot_rlzt_pfls", "KIS period profit"),
                _optional_nonnegative_decimal(summary, "tot_fee", "KIS period profit"),
                _optional_nonnegative_decimal(summary, "tot_tltx", "KIS period profit"),
            )
        )
    if len(summary_values) > 1:
        raise ValueError("conflicting KIS period profit summaries")
    if summary_values:
        realized_pnl, fee, tax = next(iter(summary_values))
        return (
            KisPeriodProfitRow(
                trading_date=start,
                realized_pnl=realized_pnl,
                fee=fee,
                tax=tax,
                loan_interest=Decimal("0"),
            ),
        )
    if valid_empty:
        return (
            KisPeriodProfitRow(
                trading_date=start,
                realized_pnl=Decimal("0"),
                fee=Decimal("0"),
                tax=Decimal("0"),
                loan_interest=Decimal("0"),
                has_activity=False,
            ),
        )
    raise ValueError("missing KIS period profit data")


def parse_kis_realized_profit_row_today(
    response: Mapping[str, object],
    *,
    trading_date: date | datetime | str,
) -> KisPeriodProfitRow:
    expected_date = _date_key(trading_date)
    normalized_response = dict(response)
    output1 = response.get("output1")
    blank_exact_row = False
    if isinstance(output1, Sequence) and not isinstance(output1, (str, bytes, bytearray)):
        matching_rows: list[Mapping[str, object]] = []
        for raw_row in output1:
            if not isinstance(raw_row, Mapping):
                raise ValueError("invalid KIS realized profit row")
            row_date = _required_text(raw_row, "trad_dt", "KIS realized profit")
            if row_date == expected_date:
                matching_rows.append(raw_row)
        if output1 and not matching_rows:
            raise ValueError("KIS realized profit response missing exact date")
        populated_rows = [
            row
            for row in matching_rows
            if row.get("rlzt_pfls") is not None and str(row.get("rlzt_pfls")).strip() != ""
        ]
        blank_exact_row = bool(matching_rows) and not populated_rows
        normalized_response["output1"] = populated_rows
    try:
        rows = parse_kis_period_profit_rows(
            normalized_response,
            start_date=trading_date,
            end_date=trading_date,
        )
    except ValueError as exc:
        raise ValueError(f"KIS realized profit response invalid: {exc}") from exc
    if not rows:
        raise ValueError("missing KIS realized profit data")
    if blank_exact_row and rows[0].has_activity is False:
        raise ValueError("KIS realized profit response missing field: rlzt_pfls")
    return rows[0]


def parse_kis_realized_pnl_today(
    response: Mapping[str, object],
    *,
    trading_date: date | datetime | str,
) -> Decimal:
    return parse_kis_realized_profit_row_today(
        response,
        trading_date=trading_date,
    ).realized_pnl


def parse_kis_opening_day(
    response: Mapping[str, object],
    *,
    trading_date: date | datetime | str,
) -> bool:
    expected_date = _date_key(trading_date)
    rows = _mapping_rows(response.get("output"), "KIS holiday", allow_empty=False)
    opening_values: list[bool] = []
    for row in rows:
        row_date = _required_text(row, "bass_dt", "KIS holiday")
        if row_date != expected_date:
            continue
        opening_flag = _required_text(row, "opnd_yn", "KIS holiday").upper()
        if opening_flag not in {"Y", "N"}:
            raise ValueError("invalid KIS holiday field: opnd_yn")
        opening_values.append(opening_flag == "Y")
    if not opening_values:
        raise ValueError("KIS holiday response missing exact date")
    if any(value != opening_values[0] for value in opening_values[1:]):
        raise ValueError("conflicting KIS holiday opening-day values")
    return opening_values[0]


def parse_kis_account_snapshot(
    response: Mapping[str, object],
    *,
    timestamp: datetime | None = None,
    allow_deposit_cash_fallback: bool = True,
    realized_pnl_today_known: bool = False,
) -> AccountSnapshot:
    opened_at = timestamp or _utc_now()
    positions: dict[str, Position] = {}
    for raw_position in _optional_sequence(response, "output1"):
        if not isinstance(raw_position, Mapping):
            raise ValueError("invalid KIS balance row")
        symbol = _required_text(raw_position, "pdno", "KIS balance")
        quantity = _required_int(raw_position, "hldg_qty", "KIS balance")
        if quantity == 0:
            continue
        avg_price = _required_decimal(raw_position, "pchs_avg_pric", "KIS balance")
        last_price = _required_decimal(raw_position, "prpr", "KIS balance")
        sellable_quantity = _optional_int_or_none(raw_position, "ord_psbl_qty")
        existing = positions.get(symbol)
        if existing is None:
            positions[symbol] = Position(
                symbol=symbol,
                quantity=quantity,
                avg_price=avg_price,
                last_price=last_price,
                opened_at=opened_at,
                highest_price=last_price,
                sellable_quantity=sellable_quantity,
            )
            continue

        total_quantity = existing.quantity + quantity
        weighted_avg_price = ((existing.avg_price * existing.quantity) + (avg_price * quantity)) / total_quantity
        combined_sellable_quantity = _combine_sellable_quantity(existing.sellable_quantity, sellable_quantity)
        positions[symbol] = Position(
            symbol=symbol,
            quantity=total_quantity,
            avg_price=weighted_avg_price,
            last_price=last_price,
            opened_at=existing.opened_at,
            highest_price=max(existing.highest_price, last_price),
            sellable_quantity=combined_sellable_quantity,
        )

    cash = Decimal("0")
    equity_override: Decimal | None = None
    buying_power_override: Decimal | None = None
    summaries = _optional_sequence(response, "output2")
    if summaries:
        first_summary = summaries[0]
        if not isinstance(first_summary, Mapping):
            raise ValueError("invalid KIS balance summary")
        deposit_cash = _first_optional_decimal_or_none(
            first_summary,
            (
                "dnca_tot_amt",
            ),
        )
        orderable_cash = _first_optional_decimal_or_none(
            first_summary,
            (
                "ord_psbl_cash",
                "ord_psbl_cash_amt",
            ),
        )
        total_equity = _first_optional_decimal_or_none(
            first_summary,
            (
                "tot_evlu_amt",
                "nass_amt",
            ),
        )
        if deposit_cash is not None:
            cash = deposit_cash
        elif orderable_cash is not None:
            cash = orderable_cash
        if total_equity is not None:
            equity_override = total_equity
        if orderable_cash is not None:
            buying_power_override = orderable_cash
        elif not allow_deposit_cash_fallback:
            buying_power_override = Decimal("0")

    return AccountSnapshot(
        cash=cash,
        positions=positions,
        realized_pnl_today=Decimal("0"),
        realized_pnl_today_known=bool(realized_pnl_today_known),
        equity_override=equity_override,
        buying_power_override=buying_power_override,
    )


def _required_mapping(response: Mapping[str, object], key: str, label: str) -> Mapping[str, object]:
    value = response.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"missing {label} field: {key}")
    return value


def _price_vwap(output: Mapping[str, object], close: Decimal, volume: int) -> Decimal:
    weighted_average = _optional_decimal(output, "wghn_avrg_stck_prc", Decimal("0"))
    if weighted_average > 0:
        return weighted_average

    traded_amount = _optional_decimal(output, "acml_tr_pbmn", Decimal("0"))
    if traded_amount > 0 and volume > 0:
        return traded_amount / Decimal(volume)

    return close


def _best_quotes(response: Mapping[str, object]) -> tuple[Decimal, Decimal]:
    output = _required_mapping(response, "output1", "KIS best quote")
    unavailable_error: KisQuoteUnavailableError | None = None
    try:
        ask = _quote_decimal(output, "askp1")
    except KisQuoteUnavailableError as exc:
        ask = None
        unavailable_error = exc
    try:
        bid = _quote_decimal(output, "bidp1")
    except KisQuoteUnavailableError as exc:
        bid = None
        unavailable_error = unavailable_error or exc
    if unavailable_error is not None:
        raise unavailable_error
    if ask is None or bid is None:
        raise ValueError("invalid KIS best quote")
    if bid > ask:
        raise ValueError("invalid KIS best quote spread")
    return bid, ask


def _quote_decimal(row: Mapping[str, object], key: str) -> Decimal:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise KisQuoteUnavailableError(f"missing KIS best quote field: {key}")
    try:
        parsed = _to_decimal(value, key)
    except ValueError as exc:
        raise ValueError(f"invalid KIS best quote field: {key}") from exc
    if parsed < 0:
        raise ValueError(f"invalid KIS best quote field: {key}")
    if parsed == 0:
        raise KisQuoteUnavailableError(f"invalid KIS best quote field: {key}")
    return parsed


def _minute_timestamp(day: str, time_text: str) -> datetime:
    if len(day) != 8 or not day.isdigit() or len(time_text) != 6 or not time_text.isdigit():
        raise ValueError("invalid KIS minute bar timestamp")
    try:
        return datetime.strptime(f"{day}{time_text}", "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError as exc:
        raise ValueError("invalid KIS minute bar timestamp") from exc


def _minute_vwap(row: Mapping[str, object], *, high: Decimal, low: Decimal, close: Decimal) -> Decimal:
    exact_vwap = _optional_decimal(row, "wghn_avrg_stck_prc", Decimal("0"))
    if exact_vwap > 0:
        return exact_vwap
    return (high + low + close) / Decimal("3")


def _required_sequence(response: Mapping[str, object], key: str, label: str) -> Sequence[object]:
    if key not in response:
        raise ValueError(f"missing {label} field: {key}")
    value = response[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"invalid {label} field: {key}")
    return value


def _mapping_rows(value: object, label: str, *, allow_empty: bool) -> list[Mapping[str, object]]:
    if isinstance(value, Mapping):
        if not value:
            return []
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows: list[Mapping[str, object]] = []
        for row in value:
            if not isinstance(row, Mapping):
                raise ValueError(f"invalid {label} row")
            rows.append(row)
        if rows or allow_empty:
            return rows
    if value is None and allow_empty:
        return []
    raise ValueError(f"invalid {label} response")


def _signed_decimal(value: object, key: str, label: str) -> Decimal:
    try:
        return _to_decimal(value, key)
    except ValueError as exc:
        raise ValueError(f"invalid {label} field: {key}") from exc


def _required_signed_decimal(row: Mapping[str, object], key: str, label: str) -> Decimal:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {label} field: {key}")
    return _signed_decimal(value, key, label)


def _optional_signed_decimal(row: Mapping[str, object], key: str, label: str) -> Decimal:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return Decimal("0")
    return _signed_decimal(value, key, label)


def _optional_nonnegative_decimal(row: Mapping[str, object], key: str, label: str) -> Decimal:
    value = _optional_signed_decimal(row, key, label)
    if value < 0:
        raise ValueError(f"invalid {label} field: {key} must not be negative")
    return value


def _date_key(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return _as_kst(value).strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    digits = "".join(character for character in str(value) if character.isdigit())
    if len(digits) != 8:
        raise ValueError("KIS date must be YYYYMMDD")
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("KIS date must be YYYYMMDD") from exc
    return digits


def _parse_kis_date_key(value: str, label: str) -> date:
    if len(value) != 8 or not value.isdigit():
        raise ValueError(f"invalid {label}")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError(f"invalid {label}") from exc


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _optional_sequence(response: Mapping[str, object], key: str) -> Sequence[object]:
    value = response.get(key, [])
    if value in (None, ""):
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"invalid KIS response field: {key}")
    return value


def _required_text(row: Mapping[str, object], key: str, label: str) -> str:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {label} field: {key}")
    return str(value).strip()


def _optional_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _optional_yn_flag(row: Mapping[str, object], key: str) -> bool | None:
    value = _optional_text(row, key).upper()
    if not value:
        return None
    if value not in {"Y", "N"}:
        return None
    return value == "Y"


def _normalize_kis_market_name(value: object) -> str:
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return ""
    if "코스닥" in text or "KOSDAQ" in text:
        return "KOSDAQ"
    if "코스피" in text or "KOSPI" in text:
        return "KOSPI"
    if "코넥스" in text or "KONEX" in text:
        return "KONEX"
    return text[:32]


def _required_decimal(row: Mapping[str, object], key: str, label: str) -> Decimal:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {label} field: {key}")
    return _to_decimal(value, key)


def _optional_decimal(row: Mapping[str, object], key: str, default: Decimal) -> Decimal:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return default
    return _to_decimal(value, key)


def _required_int(row: Mapping[str, object], key: str, label: str) -> int:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        raise ValueError(f"missing {label} field: {key}")
    return _to_int(value, key)


def _optional_int(row: Mapping[str, object], key: str, default: int) -> int:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return default
    return _to_int(value, key)


def _optional_int_or_none(row: Mapping[str, object], key: str) -> int | None:
    value = row.get(key)
    if value is None or str(value).strip() == "":
        return None
    return _to_int(value, key)


def _first_optional_decimal_or_none(row: Mapping[str, object], keys: Sequence[str]) -> Decimal | None:
    for key in keys:
        value = row.get(key)
        if value is None or str(value).strip() == "":
            continue
        parsed = _to_decimal(value, key)
        return max(Decimal("0"), parsed)
    return None


def _combine_sellable_quantity(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return left + right


def _to_decimal(value: object, key: str) -> Decimal:
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"invalid KIS numeric field: {key}") from exc
    if not number.is_finite():
        raise ValueError(f"invalid KIS numeric field: {key}")
    return number


def _to_int(value: object, key: str) -> int:
    number = _to_decimal(value, key)
    if number != number.to_integral_value():
        raise ValueError(f"invalid KIS integer field: {key}")
    return int(number)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
