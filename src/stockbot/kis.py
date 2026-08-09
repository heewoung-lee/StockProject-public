from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from threading import Lock
from typing import Callable, Optional

from .kis_models import (
    KisPeriodProfitRow,
    parse_kis_account_snapshot,
    parse_kis_minute_bars,
    parse_kis_opening_day,
    parse_kis_period_profit_rows,
    parse_kis_price_bar,
    parse_kis_realized_profit_row_today,
)
from .models import AccountSnapshot, MarketBar, Order


KIS_VTS_BASE_URL = "https://openapivts.koreainvestment.com:29443"
KIS_LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"
KIS_READ_RATE_LIMIT_RETRY_SECONDS = 2.0
KIS_READ_RATE_LIMIT_MAX_WAIT_SECONDS = 3.0
KST = timezone(timedelta(hours=9), name="KST")
_BUDGETED_MARKET_READ_PATHS = {
    "/uapi/domestic-stock/v1/quotations/inquire-price",
    "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
    "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
    "/uapi/domestic-stock/v1/quotations/chk-holiday",
    "/uapi/domestic-stock/v1/trading/inquire-balance",
    "/uapi/domestic-stock/v1/trading/inquire-period-profit",
}


class KisApiError(RuntimeError):
    pass


class KisLocalRateLimitError(KisApiError):
    pass


class KisOrderSubmissionUncertain(KisApiError):
    """Raised when a live order POST may have reached KIS but no reliable response was received."""


@dataclass(frozen=True)
class KisCredentials:
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    account_no: str = field(repr=False)
    account_product_code: str

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("app_key", self.app_key),
                ("app_secret", self.app_secret),
                ("account_no", self.account_no),
                ("account_product_code", self.account_product_code),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"missing KIS credentials: {', '.join(missing)}")


@dataclass(frozen=True)
class KisRequest:
    method: str
    base_url: str
    path: str
    headers: dict[str, str] = field(repr=False)
    params: dict[str, str] = field(default_factory=dict, repr=False)
    json: Optional[dict[str, str]] = field(default=None, repr=False)
    timeout: float = 10.0


Transport = Callable[[KisRequest], dict]
ProfitObserver = Callable[
    [tuple[KisPeriodProfitRow, ...], datetime, date, date],
    None,
]


class KisVtsClient:
    def __init__(
        self,
        credentials: KisCredentials,
        *,
        base_url: str = KIS_VTS_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 10.0,
        allow_order_placement: bool = False,
        access_token: str | None = None,
        access_token_expires_at: datetime | None = None,
    ):
        credentials.validate()
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url != KIS_VTS_BASE_URL:
            raise ValueError(f"KIS VTS client only supports {KIS_VTS_BASE_URL}")
        self.credentials = credentials
        self.base_url = normalized_base_url
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self.allow_order_placement = allow_order_placement
        self._access_token: str | None = access_token
        self._access_token_expires_at: datetime | None = access_token_expires_at

    def issue_access_token(self) -> str:
        response = self.transport(
            KisRequest(
                method="POST",
                base_url=self.base_url,
                path="/oauth2/tokenP",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/plain",
                    "charset": "UTF-8",
                },
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.credentials.app_key,
                    "appsecret": self.credentials.app_secret,
                },
                timeout=self.timeout,
            )
        )
        token = response.get("access_token")
        if not token:
            raise KisApiError("KIS token response did not include access_token")
        self._access_token = str(token)
        self._access_token_expires_at = _parse_kis_datetime(response.get("access_token_token_expired")) or (
            datetime.now() + timedelta(hours=23)
        )
        return self._access_token

    def set_access_token(self, token: str, *, expires_at: datetime | None = None) -> None:
        if not token:
            raise ValueError("access token is required")
        self._access_token = str(token)
        self._access_token_expires_at = expires_at

    def access_token_expires_at(self) -> datetime | None:
        return self._access_token_expires_at

    def inquire_price(self, symbol: str) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )
        self._raise_if_error(response)
        return response

    def inquire_balance(self) -> dict:
        return _merge_kis_balance_pages(self._inquire_balance_pages("VTTC8434R"))

    def _inquire_balance_pages(self, tr_id: str) -> list[dict]:
        pages: list[dict] = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        for _ in range(10):
            response = self._send(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id,
                params={
                    "CANO": self.credentials.account_no,
                    "ACNT_PRDT_CD": self.credentials.account_product_code,
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "",
                    "INQR_DVSN": "02",
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": ctx_area_fk100,
                    "CTX_AREA_NK100": ctx_area_nk100,
                },
                tr_cont=tr_cont,
            )
            self._raise_if_error(response)
            pages.append(response)
            if _kis_tr_cont(response) not in {"M", "F"}:
                return pages
            ctx_area_fk100, ctx_area_nk100 = _kis_balance_continuation_keys(response)
            if not ctx_area_fk100 and not ctx_area_nk100:
                raise KisApiError("KIS balance continuation missing context keys")
            tr_cont = "N"
        raise KisApiError("KIS balance continuation exceeded max pages")

    def price_bar(self, symbol: str, *, timestamp: datetime | None = None) -> MarketBar:
        return parse_kis_price_bar(self.inquire_price(symbol), symbol=symbol, timestamp=timestamp)

    def account_snapshot(self, *, timestamp: datetime | None = None) -> AccountSnapshot:
        return parse_kis_account_snapshot(self.inquire_balance(), timestamp=timestamp)

    def place_cash_order(
        self,
        order: Order,
        *,
        order_price: Decimal,
        order_division: str = "00",
        exchange: str = "KRX",
    ) -> dict:
        if not self.allow_order_placement:
            raise ValueError("KIS VTS order placement requires allow_order_placement=True")
        if order.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if order.side not in {"BUY", "SELL"}:
            raise ValueError("order side must be BUY or SELL")
        tr_id = "VTTC0012U" if order.side == "BUY" else "VTTC0011U"
        response = self._send(
            "POST",
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id,
            json_body={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": order.symbol,
                "ORD_DVSN": order_division,
                "ORD_QTY": str(order.quantity),
                "ORD_UNPR": _decimal_to_api_string(order_price),
                "EXCG_ID_DVSN_CD": exchange,
                "SLL_TYPE": "01" if order.side == "SELL" else "",
                "CNDT_PRIC": "",
            },
        )
        self._raise_if_error(response)
        return response

    def _send(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
        tr_cont: str = "",
    ) -> dict:
        token = self._require_token()
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if tr_cont:
            headers["tr_cont"] = tr_cont
        return self.transport(
            KisRequest(
                method=method,
                base_url=self.base_url,
                path=path,
                headers=headers,
                params=params or {},
                json=json_body,
                timeout=self.timeout,
            )
        )

    def _require_token(self) -> str:
        if self._access_token is None:
            raise KisApiError("KIS access token is not issued; call issue_access_token() first")
        return self._access_token

    @staticmethod
    def _raise_if_error(response: dict) -> None:
        if response.get("rt_cd", "0") != "0":
            code = response.get("msg_cd", "unknown")
            message = response.get("msg1", "KIS API request failed")
            raise KisApiError(f"{code}: {message}")


class KisLiveReadOnlyClient:
    def __init__(
        self,
        credentials: KisCredentials,
        *,
        base_url: str = KIS_LIVE_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 10.0,
        access_token: str | None = None,
        access_token_expires_at: datetime | None = None,
        rate_limiter: object | None = None,
        token_cache: object | None = None,
        profit_observer: ProfitObserver | None = None,
    ):
        credentials.validate()
        normalized_base_url = base_url.rstrip("/")
        if normalized_base_url != KIS_LIVE_BASE_URL:
            raise ValueError(f"KIS live read-only client only supports {KIS_LIVE_BASE_URL}")
        self.credentials = credentials
        self.base_url = normalized_base_url
        self.transport = transport or urllib_transport
        self.timeout = timeout
        self._access_token: str | None = access_token
        self._access_token_expires_at: datetime | None = access_token_expires_at
        self._token_refresh_required = False
        self._token_refresh_lock = Lock()
        self.rate_limiter = rate_limiter
        self.token_cache = token_cache
        self.profit_observer = profit_observer
        self._market_read_budget_limit: int | None = None
        self._market_read_budget_used = 0
        self._market_read_budget_lock = Lock()

    def begin_market_read_budget(self, limit: int | None) -> None:
        with self._market_read_budget_lock:
            self._market_read_budget_limit = None if limit is None else max(0, int(limit))
            self._market_read_budget_used = 0

    def ensure_market_read_budget(self, minimum_limit) -> None:
        if isinstance(minimum_limit, bool):
            return
        try:
            normalized_limit = int(minimum_limit)
        except (TypeError, ValueError, OverflowError):
            return
        if normalized_limit < 0:
            return
        with self._market_read_budget_lock:
            if self._market_read_budget_limit is None:
                return
            if normalized_limit > self._market_read_budget_limit:
                self._market_read_budget_limit = normalized_limit

    def market_read_budget_state(self) -> tuple[int, int] | None:
        with self._market_read_budget_lock:
            if self._market_read_budget_limit is None:
                return None
            return self._market_read_budget_used, self._market_read_budget_limit

    def end_market_read_budget(self) -> None:
        with self._market_read_budget_lock:
            self._market_read_budget_limit = None
            self._market_read_budget_used = 0

    def issue_access_token(self) -> str:
        response = self.transport(
            KisRequest(
                method="POST",
                base_url=self.base_url,
                path="/oauth2/tokenP",
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/plain",
                    "charset": "UTF-8",
                },
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.credentials.app_key,
                    "appsecret": self.credentials.app_secret,
                },
                timeout=self.timeout,
            )
        )
        self._raise_if_error(response)
        token = response.get("access_token")
        if not token:
            raise KisApiError("KIS token response did not include access_token")
        self._access_token = str(token)
        self._access_token_expires_at = _parse_kis_datetime(response.get("access_token_token_expired")) or (
            datetime.now() + timedelta(hours=23)
        )
        self._token_refresh_required = False
        return self._access_token

    def set_access_token(self, token: str, *, expires_at: datetime | None = None) -> None:
        if not token:
            raise ValueError("access token is required")
        self._access_token = str(token)
        self._access_token_expires_at = expires_at
        self._token_refresh_required = False

    def access_token_expires_at(self) -> datetime | None:
        return self._access_token_expires_at

    def inquire_price(self, symbol: str) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )
        self._raise_if_error(response)
        return response

    def inquire_asking_price_exp_ccn(self, symbol: str) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            "FHKST01010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
            },
        )
        self._raise_if_error(response)
        return response

    def inquire_time_itemchartprice(self, symbol: str, *, now: datetime | None = None) -> dict:
        current_kst = _as_kst(now or _kst_now())
        last_completed_second = current_kst.replace(second=0, microsecond=0) - timedelta(seconds=1)
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": last_completed_second.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
        )
        self._raise_if_error(response)
        return response

    def minute_bars(self, symbol: str, *, now: datetime | None = None) -> list[MarketBar]:
        current_kst = _as_kst(now or _kst_now())
        return parse_kis_minute_bars(
            self.inquire_time_itemchartprice(symbol, now=current_kst),
            symbol=symbol,
            trading_date=current_kst.date(),
            completed_before=current_kst.replace(second=0, microsecond=0),
        )

    def inquire_balance(self) -> dict:
        return _merge_kis_balance_pages(self._inquire_balance_pages("TTTC8434R"))

    def _inquire_balance_pages(self, tr_id: str) -> list[dict]:
        pages: list[dict] = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        for _ in range(10):
            response = self._send(
                "GET",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                tr_id,
                params={
                    "CANO": self.credentials.account_no,
                    "ACNT_PRDT_CD": self.credentials.account_product_code,
                    "AFHR_FLPR_YN": "N",
                    "OFL_YN": "",
                    "INQR_DVSN": "02",
                    "UNPR_DVSN": "01",
                    "FUND_STTL_ICLD_YN": "N",
                    "FNCG_AMT_AUTO_RDPT_YN": "N",
                    "PRCS_DVSN": "00",
                    "CTX_AREA_FK100": ctx_area_fk100,
                    "CTX_AREA_NK100": ctx_area_nk100,
                },
                tr_cont=tr_cont,
            )
            self._raise_if_error(response)
            pages.append(response)
            if _kis_tr_cont(response) not in {"M", "F"}:
                return pages
            ctx_area_fk100, ctx_area_nk100 = _kis_balance_continuation_keys(response)
            if not ctx_area_fk100 and not ctx_area_nk100:
                raise KisApiError("KIS balance continuation missing context keys")
            tr_cont = "N"
        raise KisApiError("KIS balance continuation exceeded max pages")

    def inquire_daily_orders(
        self,
        *,
        inquiry_start_date: date | datetime | str | None = None,
        inquiry_end_date: date | datetime | str | None = None,
        order_no: str = "",
        symbol: str = "",
        side_code: str = "00",
        execution_code: str = "00",
        exchange_code: str = "KRX",
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
        tr_cont: str = "",
    ) -> dict:
        """Read live domestic daily order/fill records.

        This is a read-only inquiry endpoint used for reconciliation after a
        submitted live order. It must stay on KisLiveReadOnlyClient so order
        status can be checked without widening the order placement surface.
        """
        start = _kis_date(inquiry_start_date)
        end = _kis_date(inquiry_end_date or inquiry_start_date)
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            "TTTC0081R",
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_STRT_DT": start,
                "INQR_END_DT": end,
                "SLL_BUY_DVSN_CD": side_code,
                "INQR_DVSN": "00",
                "PDNO": symbol,
                "CCLD_DVSN": execution_code,
                "ORD_GNO_BRNO": "",
                "ODNO": order_no,
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "INQR_DVSN_2": "",
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
                "EXCG_ID_DVSN_CD": exchange_code,
            },
            tr_cont=tr_cont,
        )
        self._raise_if_error(response)
        return response

    def inquire_period_profit(
        self,
        inquiry_start_date: date | datetime | str | None = None,
        inquiry_end_date: date | datetime | str | None = None,
        *,
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
        tr_cont: str = "",
    ) -> dict:
        start_date, end_date = _kis_period_date_keys(
            inquiry_start_date or _kst_now(),
            inquiry_end_date,
        )
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            "TTTC8708R",
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SORT_DVSN": "00",
                "INQR_DVSN": "00",
                "CBLC_DVSN": "00",
                "PDNO": "",
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
            },
            tr_cont=tr_cont,
        )
        self._raise_if_error(response)
        return response

    def period_profit_rows(
        self,
        inquiry_start_date: date | datetime | str,
        inquiry_end_date: date | datetime | str,
    ) -> tuple[KisPeriodProfitRow, ...]:
        start_key, end_key = _kis_period_date_keys(inquiry_start_date, inquiry_end_date)
        pages: list[dict] = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        seen_continuations: set[tuple[str, str]] = set()
        for _ in range(10):
            response = self.inquire_period_profit(
                start_key,
                end_key,
                ctx_area_fk100=ctx_area_fk100,
                ctx_area_nk100=ctx_area_nk100,
                tr_cont=tr_cont,
            )
            pages.append(response)
            if _kis_tr_cont(response) not in {"M", "F"}:
                return parse_kis_period_profit_rows(
                    _merge_kis_period_profit_pages(pages),
                    start_date=start_key,
                    end_date=end_key,
                )
            ctx_area_fk100, ctx_area_nk100 = _kis_balance_continuation_keys(response)
            if not ctx_area_fk100 and not ctx_area_nk100:
                raise KisApiError("KIS period profit continuation missing context keys")
            continuation = (ctx_area_fk100, ctx_area_nk100)
            if continuation in seen_continuations:
                raise KisApiError("KIS period profit continuation repeated context keys")
            seen_continuations.add(continuation)
            tr_cont = "N"
        raise KisApiError("KIS period profit continuation exceeded max pages")

    def realized_pnl_today(
        self,
        trading_date: date | datetime | str | None = None,
        *,
        _row_observer: Callable[[tuple[KisPeriodProfitRow, ...]], None] | None = None,
    ) -> Decimal:
        inquiry_date = _kis_date(trading_date or _kst_now())
        pages: list[dict] = []
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        seen_continuations: set[tuple[str, str]] = set()
        for _ in range(10):
            response = self.inquire_period_profit(
                inquiry_date,
                ctx_area_fk100=ctx_area_fk100,
                ctx_area_nk100=ctx_area_nk100,
                tr_cont=tr_cont,
            )
            pages.append(response)
            if _kis_profit_page_has_date(response, inquiry_date):
                row = parse_kis_realized_profit_row_today(response, trading_date=inquiry_date)
                if _row_observer is not None:
                    _row_observer((row,))
                return row.realized_pnl
            if _kis_tr_cont(response) not in {"M", "F"}:
                row = parse_kis_realized_profit_row_today(
                    _merge_kis_period_profit_pages(pages),
                    trading_date=inquiry_date,
                )
                if _row_observer is not None:
                    _row_observer((row,))
                return row.realized_pnl
            ctx_area_fk100, ctx_area_nk100 = _kis_balance_continuation_keys(response)
            if not ctx_area_fk100 and not ctx_area_nk100:
                raise KisApiError("KIS realized profit continuation missing context keys")
            continuation = (ctx_area_fk100, ctx_area_nk100)
            if continuation in seen_continuations:
                raise KisApiError("KIS realized profit continuation repeated context keys")
            seen_continuations.add(continuation)
            tr_cont = "N"
        raise KisApiError("KIS realized profit continuation exceeded max pages")

    def chk_holiday(
        self,
        trading_date: date | datetime | str,
        *,
        ctx_area_fk: str = "",
        ctx_area_nk: str = "",
        tr_cont: str = "",
    ) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/quotations/chk-holiday",
            "CTCA0903R",
            params={
                "BASS_DT": _kis_date(trading_date),
                "CTX_AREA_FK": ctx_area_fk,
                "CTX_AREA_NK": ctx_area_nk,
            },
            tr_cont=tr_cont,
        )
        self._raise_if_error(response)
        return response

    def is_opening_day(self, trading_date: date | datetime | str) -> bool:
        pages: list[dict] = []
        ctx_area_fk = ""
        ctx_area_nk = ""
        tr_cont = ""
        seen_continuations: set[tuple[str, str]] = set()
        for _ in range(10):
            response = self.chk_holiday(
                trading_date,
                ctx_area_fk=ctx_area_fk,
                ctx_area_nk=ctx_area_nk,
                tr_cont=tr_cont,
            )
            pages.append(response)
            try:
                return parse_kis_opening_day(response, trading_date=trading_date)
            except ValueError as exc:
                if "missing exact date" not in str(exc):
                    raise
            if _kis_tr_cont(response) not in {"M", "F"}:
                return parse_kis_opening_day(
                    _merge_kis_holiday_pages(pages),
                    trading_date=trading_date,
                )
            ctx_area_fk, ctx_area_nk = _kis_holiday_continuation_keys(response)
            if not ctx_area_fk and not ctx_area_nk:
                raise KisApiError("KIS holiday continuation missing context keys")
            continuation = (ctx_area_fk, ctx_area_nk)
            if continuation in seen_continuations:
                raise KisApiError("KIS holiday continuation repeated context keys")
            seen_continuations.add(continuation)
            tr_cont = "N"
        raise KisApiError("KIS holiday continuation exceeded max pages")

    def price_bar(self, symbol: str, *, timestamp: datetime | None = None) -> MarketBar:
        price_response = self.inquire_price(symbol)
        state_bar = parse_kis_price_bar(
            price_response,
            symbol=symbol,
            timestamp=timestamp,
        )
        if state_bar.temporary_stop is not False:
            return state_bar
        return parse_kis_price_bar(
            price_response,
            symbol=symbol,
            timestamp=timestamp,
            orderbook_response=self.inquire_asking_price_exp_ccn(symbol),
        )

    def account_snapshot(self, *, timestamp: datetime | None = None) -> AccountSnapshot:
        observed_at = _as_kst(timestamp or _kst_now())
        snapshot = parse_kis_account_snapshot(
            self.inquire_balance(),
            timestamp=timestamp,
            allow_deposit_cash_fallback=True,
            realized_pnl_today_known=False,
        )
        try:
            trading_date = observed_at.date()

            def observe_exact_rows(rows: tuple[KisPeriodProfitRow, ...]) -> None:
                self._notify_profit_observer(
                    rows,
                    observed_at=observed_at,
                    start_date=trading_date,
                    end_date=trading_date,
                )

            realized_pnl = self.realized_pnl_today(
                trading_date,
                _row_observer=observe_exact_rows if self.profit_observer is not None else None,
            )
        except Exception:
            return snapshot
        return replace(
            snapshot,
            realized_pnl_today=realized_pnl,
            realized_pnl_today_known=True,
        )

    def _notify_profit_observer(
        self,
        rows: tuple[KisPeriodProfitRow, ...],
        *,
        observed_at: datetime,
        start_date: date,
        end_date: date,
    ) -> bool:
        if self.profit_observer is None:
            return False
        try:
            self.profit_observer(
                rows,
                observed_at,
                start_date,
                end_date,
            )
        except Exception:
            return False
        return True

    def inquire_buyable_order(
        self,
        symbol: str,
        *,
        order_price: Decimal,
        order_division: str = "00",
    ) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            "TTTC8908R",
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": symbol,
                "ORD_UNPR": _decimal_to_api_string(order_price),
                "ORD_DVSN": order_division,
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        self._raise_if_error(response)
        return response

    def inquire_cancelable_orders(
        self,
        *,
        inquiry_division_1: str = "1",
        inquiry_division_2: str = "0",
        ctx_area_fk100: str = "",
        ctx_area_nk100: str = "",
        tr_cont: str = "",
    ) -> dict:
        response = self._send(
            "GET",
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            "TTTC0084R",
            params={
                "CANO": self.credentials.account_no,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_DVSN_1": inquiry_division_1,
                "INQR_DVSN_2": inquiry_division_2,
                "CTX_AREA_FK100": ctx_area_fk100,
                "CTX_AREA_NK100": ctx_area_nk100,
            },
            tr_cont=tr_cont,
        )
        self._raise_if_error(response)
        return response

    def _send(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, str] | None = None,
        tr_cont: str = "",
    ) -> dict:
        if method == "GET" and self._token_refresh_required:
            self._refresh_expired_access_token(self._access_token)

        expired_token_retried = False
        rate_limit_retried = False
        while True:
            token = self._require_token()
            headers = {
                "Content-Type": "application/json",
                "authorization": f"Bearer {token}",
                "appkey": self.credentials.app_key,
                "appsecret": self.credentials.app_secret,
                "tr_id": tr_id,
                "custtype": "P",
            }
            if tr_cont:
                headers["tr_cont"] = tr_cont
            request = KisRequest(
                method=method,
                base_url=self.base_url,
                path=path,
                headers=headers,
                params=params or {},
                json=json_body,
                timeout=self.timeout,
            )
            try:
                response = self._transport_request(request, retry=rate_limit_retried)
            except KisApiError as exc:
                error_text = str(exc)
                if method == "GET" and _is_kis_expired_access_token_error("", error_text):
                    if expired_token_retried:
                        self._mark_access_token_rejected()
                        raise
                    self._refresh_expired_access_token(token)
                    expired_token_retried = True
                    rate_limit_retried = False
                    continue
                if (
                    method == "GET"
                    and not rate_limit_retried
                    and self._acquire_read_rate_limit_retry(error_text)
                ):
                    rate_limit_retried = True
                    continue
                raise

            if method != "GET" or not isinstance(response, Mapping) or response.get("rt_cd", "0") == "0":
                return response

            code = str(response.get("msg_cd", ""))
            message = str(response.get("msg1", ""))
            if _is_kis_expired_access_token_error(code, message):
                if expired_token_retried:
                    self._mark_access_token_rejected()
                    return response
                self._refresh_expired_access_token(token)
                expired_token_retried = True
                rate_limit_retried = False
                continue
            if (
                not rate_limit_retried
                and _is_kis_per_second_rate_limit_code(code, message)
                and self._acquire_read_rate_limit_retry(f"{code}: {message}")
            ):
                rate_limit_retried = True
                continue
            return response

    def _refresh_expired_access_token(self, rejected_token: str | None) -> str:
        with self._token_refresh_lock:
            current_token = self._access_token
            if (
                current_token
                and rejected_token
                and current_token != rejected_token
                and not self._token_refresh_required
            ):
                return current_token

            cached = self._read_cached_access_token()
            cached_token = str(getattr(cached, "access_token", "") or "")
            if cached_token and cached_token != str(rejected_token or ""):
                self.set_access_token(
                    cached_token,
                    expires_at=getattr(cached, "expires_at", None),
                )
                return cached_token

            self._token_refresh_required = True
            self._invalidate_cached_access_token()
            access_token = self.issue_access_token_with_rate_limit()
            self._write_cached_access_token(access_token)
            return access_token

    def issue_access_token_with_rate_limit(self) -> str:
        def issue_access_token() -> str:
            try:
                return self.issue_access_token()
            finally:
                self._record_token_issue()

        run_request = getattr(self.rate_limiter, "run_request", None)
        if callable(run_request):
            decision, result = run_request("kis_token", issue_access_token)
            if not decision.allowed:
                raise KisLocalRateLimitError(
                    f"KIS local rate limit: {decision.reason} "
                    f"retry_after={decision.retry_after_seconds:.1f}s"
                )
            access_token = str(result or "")
        else:
            self._check_token_rate_limit()
            access_token = issue_access_token()
        if not access_token:
            raise KisApiError("KIS token response did not include access_token")
        return access_token

    def _record_token_issue(self) -> None:
        recorder = getattr(self.rate_limiter, "record_token_issue", None)
        if callable(recorder):
            recorder()

    def _check_token_rate_limit(self) -> None:
        if self.rate_limiter is None:
            return
        acquire_request = getattr(self.rate_limiter, "acquire_request", None)
        if callable(acquire_request):
            decision = acquire_request("kis_token")
        else:
            decision = self.rate_limiter.allow_request("kis_token")
            if decision.allowed:
                self.rate_limiter.record_request("kis_token")
        if not decision.allowed:
            raise KisLocalRateLimitError(
                f"KIS local rate limit: {decision.reason} "
                f"retry_after={decision.retry_after_seconds:.1f}s"
            )

    def _read_cached_access_token(self) -> object | None:
        reader = getattr(self.token_cache, "read", None)
        if not callable(reader):
            return None
        return reader(self.credentials)

    def _write_cached_access_token(self, access_token: str) -> None:
        writer = getattr(self.token_cache, "write", None)
        if callable(writer):
            writer(
                self.credentials,
                access_token,
                self.access_token_expires_at(),
            )

    def _invalidate_cached_access_token(self) -> None:
        invalidator = getattr(self.token_cache, "invalidate", None)
        if callable(invalidator):
            invalidator(self.credentials)

    def _mark_access_token_rejected(self) -> None:
        self._token_refresh_required = True
        self._invalidate_cached_access_token()

    def _transport_request(self, request: KisRequest, *, retry: bool = False) -> dict:
        request_kind = "kis_live_read" if request.method == "GET" else "kis_live_mutation"

        def invoke_transport():
            self._consume_market_read_budget(request)
            return self.transport(request)

        run_request = getattr(self.rate_limiter, "run_request", None)
        if callable(run_request):
            decision, response = run_request(
                request_kind,
                invoke_transport,
                retry=retry,
                wait_for_api_backoff=request.method == "GET",
                max_wait_seconds=KIS_READ_RATE_LIMIT_MAX_WAIT_SECONDS,
            )
            if not decision.allowed:
                raise KisLocalRateLimitError(
                    f"KIS local rate limit: {decision.reason} "
                    f"retry_after={decision.retry_after_seconds:.1f}s"
                )
            if not isinstance(response, Mapping):
                return response
            return dict(response)

        if retry:
            acquire_retry = getattr(self.rate_limiter, "acquire_retry_request", None)
            if callable(acquire_retry):
                decision = acquire_retry(
                    request_kind,
                    max_wait_seconds=KIS_READ_RATE_LIMIT_MAX_WAIT_SECONDS,
                )
                if not decision.allowed:
                    raise KisLocalRateLimitError(
                        f"KIS local rate limit: {decision.reason} "
                        f"retry_after={decision.retry_after_seconds:.1f}s"
                    )
            else:
                self._check_rate_limit(request_kind)
        else:
            self._check_rate_limit(request_kind)
        return invoke_transport()

    def _consume_market_read_budget(self, request: KisRequest) -> None:
        if request.method != "GET" or request.path not in _BUDGETED_MARKET_READ_PATHS:
            return
        with self._market_read_budget_lock:
            limit = self._market_read_budget_limit
            if limit is not None and self._market_read_budget_used >= limit:
                raise KisLocalRateLimitError("KIS physical market read budget exhausted")
            self._market_read_budget_used += 1

    def _check_rate_limit(self, kind: str = "kis_live_api") -> None:
        if self.rate_limiter is None:
            return

        acquire_request = getattr(self.rate_limiter, "acquire_request", None)
        if callable(acquire_request):
            decision = acquire_request(kind)
            if not decision.allowed:
                raise KisLocalRateLimitError(
                    f"KIS local rate limit: {decision.reason} retry_after={decision.retry_after_seconds:.1f}s"
                )
            return

        decision = self.rate_limiter.allow_request(kind)
        if not decision.allowed and decision.reason != "min_interval":
            raise KisLocalRateLimitError(
                f"KIS local rate limit: {decision.reason} retry_after={decision.retry_after_seconds:.1f}s"
            )
        self.rate_limiter.record_request(kind)

    def _record_rate_limit_error(self, retry_after_seconds: float | None = 1.5) -> None:
        recorder = getattr(self.rate_limiter, "record_rate_limit_error", None)
        if callable(recorder):
            recorder(retry_after_seconds)

    def _acquire_read_rate_limit_retry(self, error_text: str) -> bool:
        if not _is_kis_per_second_rate_limit_code("", error_text):
            return False
        if callable(getattr(self.rate_limiter, "run_request", None)):
            self._record_rate_limit_error(KIS_READ_RATE_LIMIT_RETRY_SECONDS)
            return True
        acquire_retry = getattr(self.rate_limiter, "acquire_retry_request", None)
        if not callable(acquire_retry):
            return False
        self._record_rate_limit_error(KIS_READ_RATE_LIMIT_RETRY_SECONDS)
        decision = acquire_retry(
            "kis_live_read",
            max_wait_seconds=KIS_READ_RATE_LIMIT_MAX_WAIT_SECONDS,
        )
        return bool(decision.allowed)

    def _raise_if_error(self, response: dict) -> None:
        if response.get("rt_cd", "0") == "0":
            return
        code = response.get("msg_cd", "unknown")
        message = response.get("msg1", "KIS API request failed")
        if _is_kis_per_second_rate_limit_code(str(code), str(message)):
            self._record_rate_limit_error(1.5)
        raise KisApiError(f"{code}: {message}")

    def _require_token(self) -> str:
        if self._access_token is None:
            raise KisApiError("KIS access token is not issued; call issue_access_token() first")
        return self._access_token


class KisLiveOrderClient(KisLiveReadOnlyClient):
    """KIS real-account order client.

    This class is intentionally separate from KisLiveReadOnlyClient so read-only
    probes cannot accidentally grow an order surface.
    """

    def __init__(
        self,
        credentials: KisCredentials,
        *,
        base_url: str = KIS_LIVE_BASE_URL,
        transport: Transport | None = None,
        timeout: float = 10.0,
        allow_order_placement: bool = False,
        access_token: str | None = None,
        access_token_expires_at: datetime | None = None,
        rate_limiter: object | None = None,
        token_cache: object | None = None,
        profit_observer: ProfitObserver | None = None,
    ):
        super().__init__(
            credentials,
            base_url=base_url,
            transport=transport,
            timeout=timeout,
            access_token=access_token,
            access_token_expires_at=access_token_expires_at,
            rate_limiter=rate_limiter,
            token_cache=token_cache,
            profit_observer=profit_observer,
        )
        self.allow_order_placement = allow_order_placement

    def issue_hashkey(self, payload: dict[str, str]) -> str:
        if not self.allow_order_placement:
            raise RuntimeError("KIS live order placement requires allow_order_placement=True")
        response = self._transport_request(
            KisRequest(
                method="POST",
                base_url=self.base_url,
                path="/uapi/hashkey",
                headers={
                    "Content-Type": "application/json",
                    "appkey": self.credentials.app_key,
                    "appsecret": self.credentials.app_secret,
                },
                json=payload,
                timeout=self.timeout,
            )
        )
        self._raise_if_error(response)
        hashkey = response.get("HASH") or response.get("hash")
        if not hashkey:
            raise KisApiError("KIS hashkey response did not include HASH")
        return str(hashkey)

    def place_cash_order(
        self,
        order: Order,
        *,
        order_price: Decimal,
        order_division: str = "00",
        exchange: str = "KRX",
    ) -> dict:
        if not self.allow_order_placement:
            raise RuntimeError("KIS live order placement requires allow_order_placement=True")
        if order.quantity <= 0:
            raise ValueError("order quantity must be positive")
        if order.side not in {"BUY", "SELL"}:
            raise ValueError("order side must be BUY or SELL")

        payload = {
            "CANO": self.credentials.account_no,
            "ACNT_PRDT_CD": self.credentials.account_product_code,
            "PDNO": order.symbol,
            "ORD_DVSN": order_division,
            "ORD_QTY": str(order.quantity),
            "ORD_UNPR": _decimal_to_api_string(order_price),
            "EXCG_ID_DVSN_CD": exchange,
            "SLL_TYPE": "01" if order.side == "SELL" else "",
            "CNDT_PRIC": "",
        }
        token = self._require_token()
        hashkey = self.issue_hashkey(payload)
        tr_id = "TTTC0012U" if order.side == "BUY" else "TTTC0011U"
        try:
            response = self._transport_request(
                KisRequest(
                    method="POST",
                    base_url=self.base_url,
                    path="/uapi/domestic-stock/v1/trading/order-cash",
                    headers={
                        "Content-Type": "application/json",
                        "authorization": f"Bearer {token}",
                        "appkey": self.credentials.app_key,
                        "appsecret": self.credentials.app_secret,
                        "tr_id": tr_id,
                        "custtype": "P",
                        "hashkey": hashkey,
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            )
        except KisLocalRateLimitError:
            raise
        except (KisApiError, RuntimeError, ValueError, OSError, TimeoutError) as exc:
            raise KisOrderSubmissionUncertain(f"KIS live order submission uncertain: {exc}") from exc
        if not isinstance(response, Mapping):
            raise KisOrderSubmissionUncertain(
                f"KIS live order submission uncertain: malformed response type {type(response).__name__}"
            )
        self._raise_if_error(response)
        return dict(response)

    def cancel_cash_order(
        self,
        *,
        order_no: str,
        order_org_no: str,
        quantity: int,
        order_price: Decimal,
        order_division: str = "00",
        exchange: str = "KRX",
    ) -> dict:
        if not self.allow_order_placement:
            raise RuntimeError("KIS live order placement requires allow_order_placement=True")
        if not order_no:
            raise ValueError("order_no is required")
        if not order_org_no:
            raise ValueError("order_org_no is required")
        if quantity <= 0:
            raise ValueError("cancel quantity must be positive")

        payload = {
            "CANO": self.credentials.account_no,
            "ACNT_PRDT_CD": self.credentials.account_product_code,
            "KRX_FWDG_ORD_ORGNO": str(order_org_no),
            "ORGN_ODNO": str(order_no),
            "ORD_DVSN": order_division,
            "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(quantity),
            "ORD_UNPR": _decimal_to_api_string(order_price),
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": exchange,
        }
        token = self._require_token()
        hashkey = self.issue_hashkey(payload)
        try:
            response = self._transport_request(
                KisRequest(
                    method="POST",
                    base_url=self.base_url,
                    path="/uapi/domestic-stock/v1/trading/order-rvsecncl",
                    headers={
                        "Content-Type": "application/json",
                        "authorization": f"Bearer {token}",
                        "appkey": self.credentials.app_key,
                        "appsecret": self.credentials.app_secret,
                        "tr_id": "TTTC0013U",
                        "custtype": "P",
                        "hashkey": hashkey,
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            )
        except KisLocalRateLimitError:
            raise
        except (KisApiError, RuntimeError, ValueError, OSError, TimeoutError) as exc:
            raise KisOrderSubmissionUncertain(f"KIS live cancel submission uncertain: {exc}") from exc
        if not isinstance(response, Mapping):
            raise KisOrderSubmissionUncertain(
                f"KIS live cancel submission uncertain: malformed response type {type(response).__name__}"
            )
        self._raise_if_error(response)
        return dict(response)


def _merge_kis_balance_pages(pages: list[dict]) -> dict:
    if not pages:
        return {"rt_cd": "0", "output1": [], "output2": []}
    merged = dict(pages[0])
    output1: list[object] = []
    output2: list[object] = []
    for page in pages:
        output1.extend(_kis_output_sequence(page, "output1"))
        output2.extend(_kis_output_sequence(page, "output2"))
    merged["output1"] = output1
    merged["output2"] = output2
    final_fk100, final_nk100 = _kis_balance_continuation_keys(pages[-1])
    merged["ctx_area_fk100"] = final_fk100
    merged["ctx_area_nk100"] = final_nk100
    merged["tr_cont"] = _kis_tr_cont(pages[-1])
    return merged


def _merge_kis_period_profit_pages(pages: list[dict]) -> dict:
    if not pages:
        return {"rt_cd": "0", "output1": [], "output2": {}}
    merged = dict(pages[-1])
    merged["output1"] = [row for page in pages for row in _kis_output_sequence(page, "output1")]
    merged["output2"] = [
        row
        for page in pages
        for row in _kis_output_sequence(page, "output2")
        if row not in (None, "", [], {})
    ]
    return merged


def _merge_kis_holiday_pages(pages: list[dict]) -> dict:
    if not pages:
        return {"rt_cd": "0", "output": []}
    merged = dict(pages[-1])
    merged["output"] = [row for page in pages for row in _kis_output_sequence(page, "output")]
    return merged


def _kis_output_sequence(response: Mapping[str, object], key: str) -> list[object]:
    value = response.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _kis_tr_cont(response: Mapping[str, object]) -> str:
    return _kis_text(response, "tr_cont").upper()


def _kis_balance_continuation_keys(response: Mapping[str, object]) -> tuple[str, str]:
    fk100 = _kis_text(response, "ctx_area_fk100") or _kis_text(response, "CTX_AREA_FK100")
    nk100 = _kis_text(response, "ctx_area_nk100") or _kis_text(response, "CTX_AREA_NK100")
    if fk100 or nk100:
        return fk100, nk100
    output2 = response.get("output2")
    if isinstance(output2, Mapping):
        return (
            _kis_text(output2, "ctx_area_fk100") or _kis_text(output2, "CTX_AREA_FK100"),
            _kis_text(output2, "ctx_area_nk100") or _kis_text(output2, "CTX_AREA_NK100"),
        )
    if isinstance(output2, (list, tuple)) and output2 and isinstance(output2[0], Mapping):
        first = output2[0]
        return (
            _kis_text(first, "ctx_area_fk100") or _kis_text(first, "CTX_AREA_FK100"),
            _kis_text(first, "ctx_area_nk100") or _kis_text(first, "CTX_AREA_NK100"),
        )
    return "", ""


def _kis_holiday_continuation_keys(response: Mapping[str, object]) -> tuple[str, str]:
    return (
        _kis_text(response, "ctx_area_fk") or _kis_text(response, "CTX_AREA_FK"),
        _kis_text(response, "ctx_area_nk") or _kis_text(response, "CTX_AREA_NK"),
    )


def _kis_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if value is None:
        return ""
    return str(value).strip()


def urllib_transport(request: KisRequest) -> dict:
    query = urllib.parse.urlencode(request.params)
    url = f"{request.base_url}{request.path}"
    if query:
        url = f"{url}?{query}"

    body = None
    if request.json is not None:
        body = json.dumps(request.json).encode("utf-8")

    http_request = urllib.request.Request(url=url, data=body, method=request.method, headers=request.headers)
    try:
        with urllib.request.urlopen(http_request, timeout=request.timeout) as response:
            payload = response.read().decode("utf-8")
            tr_cont = response.headers.get("tr_cont", "")
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        raise KisApiError(f"KIS HTTP {exc.code}: {payload}") from exc
    except urllib.error.URLError as exc:
        raise KisApiError(f"KIS network error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise KisApiError(f"KIS network timeout: {exc}") from exc

    parsed = json.loads(payload) if payload else {}
    if isinstance(parsed, dict) and tr_cont and "tr_cont" not in parsed:
        parsed["tr_cont"] = tr_cont
    return parsed


def _is_kis_per_second_rate_limit_code(code: str, message: str) -> bool:
    text = f"{code} {message}"
    return any(marker in text for marker in ("EGW00215", "EGW00201", "초당 거래건수"))


def _is_kis_expired_access_token_error(code: str, message: str) -> bool:
    text = f"{code} {message}".lower()
    return any(
        marker in text
        for marker in (
            "egw00123",
            "access token expired",
            "\uae30\uac04\uc774 \ub9cc\ub8cc",
        )
    )


def _decimal_to_api_string(value: Decimal) -> str:
    normalized = value.quantize(Decimal("1")) if value == value.to_integral_value() else value.normalize()
    return format(normalized, "f")


def _kis_date(value: date | datetime | str | None) -> str:
    if value is None:
        return _kst_now().strftime("%Y%m%d")
    if isinstance(value, datetime):
        return _as_kst(value).strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) != 8:
        raise ValueError("KIS inquiry date must be YYYYMMDD")
    return digits


def _kis_period_date_keys(
    inquiry_start_date: date | datetime | str,
    inquiry_end_date: date | datetime | str | None,
) -> tuple[str, str]:
    start_key = _kis_date(inquiry_start_date)
    end_key = _kis_date(inquiry_end_date or inquiry_start_date)
    try:
        start = datetime.strptime(start_key, "%Y%m%d").date()
        end = datetime.strptime(end_key, "%Y%m%d").date()
    except ValueError as exc:
        raise ValueError("KIS inquiry date must be YYYYMMDD") from exc
    if start > end:
        raise ValueError("KIS inquiry start date must not be after end date")
    return start_key, end_key


def _kis_profit_page_has_date(response: Mapping[str, object], inquiry_date: str) -> bool:
    rows = response.get("output1")
    if not isinstance(rows, (list, tuple)):
        return False
    return any(
        isinstance(row, Mapping) and str(row.get("trad_dt") or "").strip() == inquiry_date
        for row in rows
    )


def _kst_now() -> datetime:
    return datetime.now(KST)


def _as_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _parse_kis_datetime(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for parser in (datetime.fromisoformat, lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")):
        try:
            return parser(text)
        except ValueError:
            continue
    return None
