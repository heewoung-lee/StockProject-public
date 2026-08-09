from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .scanner_snapshot import (
    SnapshotWriteOptions,
    build_scanner_snapshot_payload,
    write_scanner_snapshot_payload,
)


HttpOpener = Callable[..., Any]
NAVER_MARKET_CATEGORIES = {
    "kospi": "KOSPI",
    "kosdaq": "KOSDAQ",
}
NAVER_MARKET_ENDPOINT = "https://m.stock.naver.com/api/stocks/marketValue/{category}"
NAVER_MINUTE_CHART_ENDPOINT = "https://api.stock.naver.com/chart/domestic/item/{symbol}/minute"
NAVER_TRADING_VALUE_UNIT = Decimal("1000000")
NAVER_MINUTE_HISTORY_MAX_ROWS = 60
NAVER_MINUTE_HISTORY_MAX_WORKERS = 16
DEFAULT_NAVER_MINUTE_HISTORY_CANDIDATES = 128
DEFAULT_NAVER_MINUTE_HISTORY_WORKERS = 8
DEFAULT_NAVER_MINUTE_HISTORY_TIMEOUT_SECONDS = 2.0
KST = timezone(timedelta(hours=9), name="KST")


def collect_http_scanner_snapshot(
    url: str,
    output_path: str | Path,
    options: SnapshotWriteOptions | None = None,
    *,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    opener: HttpOpener | None = None,
) -> int:
    payload = fetch_http_scanner_payload(
        url,
        headers=headers,
        query=query,
        timeout=timeout,
        opener=opener,
    )
    return write_scanner_snapshot_payload(payload, output_path, options)


def fetch_http_scanner_payload(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    query: Mapping[str, str] | None = None,
    timeout: float = 10.0,
    opener: HttpOpener | None = None,
) -> Any:
    request = Request(_url_with_query(url, query), headers=dict(headers or {}), method="GET")
    response_opener = opener or urlopen
    with response_opener(request, timeout=_safe_timeout(timeout)) as response:
        raw_payload = response.read()
    return json.loads(raw_payload.decode("utf-8-sig"))


def collect_naver_market_scanner_snapshot(
    output_path: str | Path,
    options: SnapshotWriteOptions | None = None,
    *,
    markets: tuple[str, ...] | list[str] | None = None,
    pages: int = 0,
    page_size: int = 100,
    timeout: float = 10.0,
    opener: HttpOpener | None = None,
    minute_history_candidates: int = 0,
    minute_history_workers: int = 8,
    minute_history_timeout: float | None = None,
) -> int:
    resolved_options = options or SnapshotWriteOptions()
    payload = fetch_naver_market_payload(
        markets=markets,
        pages=pages,
        page_size=page_size,
        timeout=timeout,
        opener=opener,
    )
    canonical_payload = build_scanner_snapshot_payload(payload, resolved_options)
    _enrich_naver_candidates_with_minute_history(
        canonical_payload,
        candidate_limit=minute_history_candidates,
        worker_count=minute_history_workers,
        timeout=timeout if minute_history_timeout is None else minute_history_timeout,
        opener=opener,
    )
    return write_scanner_snapshot_payload(canonical_payload, output_path, resolved_options)


def fetch_naver_minute_history(
    symbol: str,
    *,
    timeout: float = 10.0,
    opener: HttpOpener | None = None,
) -> list[dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip()
    if len(normalized_symbol) != 6 or not normalized_symbol.isdigit():
        return []
    request = Request(
        NAVER_MINUTE_CHART_ENDPOINT.format(symbol=normalized_symbol),
        headers={
            "Accept": "application/json",
            "Referer": "https://m.stock.naver.com/",
            "User-Agent": "StockBot scanner snapshot/0.1",
        },
        method="GET",
    )
    response_opener = opener or urlopen
    with response_opener(request, timeout=_safe_timeout(timeout)) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    return _naver_minute_history_from_payload(payload, symbol=normalized_symbol)


def _enrich_naver_candidates_with_minute_history(
    payload: dict[str, Any],
    *,
    candidate_limit: int,
    worker_count: int,
    timeout: float,
    opener: HttpOpener | None,
) -> None:
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return
    try:
        parsed_limit = max(0, int(candidate_limit))
    except (TypeError, ValueError):
        parsed_limit = 0
    candidates = [candidate for candidate in raw_candidates[:parsed_limit] if isinstance(candidate, dict)]
    if not candidates:
        return
    try:
        parsed_workers = max(1, int(worker_count))
    except (TypeError, ValueError):
        parsed_workers = 1
    parsed_workers = min(NAVER_MINUTE_HISTORY_MAX_WORKERS, parsed_workers, len(candidates))

    with ThreadPoolExecutor(max_workers=parsed_workers, thread_name_prefix="stockbot-naver-minute") as executor:
        futures = {
            executor.submit(
                fetch_naver_minute_history,
                str(candidate.get("symbol") or ""),
                timeout=timeout,
                opener=opener,
            ): candidate
            for candidate in candidates
        }
        for future in as_completed(futures):
            candidate = futures[future]
            try:
                history = future.result()
            except Exception:
                continue
            if not history:
                continue
            market = str(candidate.get("market") or "").strip().upper()[:32]
            if market:
                for row in history:
                    row["market"] = market
            candidate["history"] = history


def _naver_minute_history_from_payload(payload: object, *, symbol: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows_by_timestamp: dict[str, dict[str, Any]] = {}
    for raw_row in payload:
        if not isinstance(raw_row, Mapping):
            continue
        timestamp = _naver_minute_timestamp(raw_row.get("localDateTime"))
        if timestamp is None:
            continue
        open_price = _decimal_from_text(raw_row.get("openPrice"))
        high = _decimal_from_text(raw_row.get("highPrice"))
        low = _decimal_from_text(raw_row.get("lowPrice"))
        close = _decimal_from_text(raw_row.get("currentPrice"))
        volume = _decimal_from_text(raw_row.get("accumulatedTradingVolume"))
        if any(
            value is None or not value.is_finite()
            for value in (open_price, high, low, close, volume)
        ):
            continue
        assert open_price is not None and high is not None and low is not None
        assert close is not None and volume is not None
        if min(open_price, high, low, close) <= 0 or volume < 0:
            continue
        if volume != volume.to_integral_value():
            continue
        if high < max(open_price, close) or low > min(open_price, close):
            continue
        # Naver's minute rows expose OHLC and a per-row volume field but no VWAP.
        # Use the same typical-price fallback as the KIS minute parser; live BUYs
        # still require a fresh KIS quote and order preflight before submission.
        typical_price = (high + low + close) / Decimal("3")
        rows_by_timestamp[timestamp] = {
            "symbol": symbol,
            "timestamp": timestamp,
            "open": _decimal_text(open_price),
            "high": _decimal_text(high),
            "low": _decimal_text(low),
            "close": _decimal_text(close),
            "volume": int(volume),
            "vwap": _decimal_text(typical_price),
        }
    return [
        rows_by_timestamp[timestamp]
        for timestamp in sorted(rows_by_timestamp)[-NAVER_MINUTE_HISTORY_MAX_ROWS:]
    ]


def _naver_minute_timestamp(value: object) -> str | None:
    raw_value = str(value or "").strip()
    if len(raw_value) != 14 or not raw_value.isdigit():
        return None
    try:
        parsed = datetime.strptime(raw_value, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError:
        return None
    if parsed.second != 0:
        return None
    return parsed.isoformat()


def fetch_naver_market_payload(
    *,
    markets: tuple[str, ...] | list[str] | None = None,
    pages: int = 0,
    page_size: int = 100,
    timeout: float = 10.0,
    opener: HttpOpener | None = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    resolved_page_size = _safe_naver_page_size(page_size)
    response_opener = opener or urlopen

    for market in _naver_markets(markets):
        page = 1
        while True:
            payload = _fetch_naver_market_page(
                market,
                page=page,
                page_size=resolved_page_size,
                timeout=timeout,
                opener=response_opener,
            )
            records.extend(
                _naver_records_from_payload(
                    payload,
                    market=NAVER_MARKET_CATEGORIES[market],
                )
            )

            if _naver_stop_after_page(payload, page=page, pages=pages, page_size=resolved_page_size):
                break
            page += 1

    return {
        "provider": "naver-mobile",
        "items": _dedupe_records_by_symbol(records),
    }


def parse_key_value_options(raw_values: list[str] | None, *, separator: str = "=") -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_value in raw_values or []:
        key, value = _split_pair(raw_value, separator=separator)
        parsed[key] = value
    return parsed


def parse_header_options(raw_values: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_value in raw_values or []:
        separator = ":" if ":" in raw_value else "="
        key, value = _split_pair(raw_value, separator=separator)
        parsed[key] = value
    return parsed


def _fetch_naver_market_page(
    market: str,
    *,
    page: int,
    page_size: int,
    timeout: float,
    opener: HttpOpener,
) -> Mapping[str, Any]:
    category = NAVER_MARKET_CATEGORIES[market]
    url = _url_with_query(
        NAVER_MARKET_ENDPOINT.format(category=category),
        {"page": str(page), "pageSize": str(page_size)},
    )
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "StockBot scanner snapshot/0.1",
        },
        method="GET",
    )
    with opener(request, timeout=_safe_timeout(timeout)) as response:
        payload = json.loads(response.read().decode("utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise ValueError("naver market response is not a JSON object")
    return payload


def _naver_records_from_payload(
    payload: Mapping[str, Any],
    *,
    market: str,
) -> list[dict[str, Any]]:
    raw_stocks = payload.get("stocks", [])
    if not isinstance(raw_stocks, list):
        return []

    records: list[dict[str, Any]] = []
    for raw_stock in raw_stocks:
        if not isinstance(raw_stock, Mapping):
            continue
        record = _naver_record_from_stock(raw_stock, market=market)
        if record is not None:
            records.append(record)
    return records


def _naver_record_from_stock(
    raw_stock: Mapping[str, Any],
    *,
    market: str,
) -> dict[str, Any] | None:
    if str(raw_stock.get("stockType") or "").lower() != "domestic":
        return None
    if str(raw_stock.get("stockEndType") or "").lower() != "stock":
        return None

    symbol = str(raw_stock.get("itemCode") or "").strip()
    price = _decimal_from_text(raw_stock.get("closePrice"))
    if not symbol or price is None:
        return None

    volume = _int_from_text(raw_stock.get("accumulatedTradingVolume"))
    trading_value = _decimal_from_text(raw_stock.get("accumulatedTradingValue"))
    trading_value_krw = trading_value * NAVER_TRADING_VALUE_UNIT if trading_value is not None else Decimal(volume)
    timestamp = str(raw_stock.get("localTradedAt") or raw_stock.get("lastUpdatedAt") or "").strip()

    record: dict[str, Any] = {
        "code": symbol,
        "name": str(raw_stock.get("stockName") or "").strip(),
        "current_price": _decimal_text(price),
        "volume": str(volume),
        "trading_value": _decimal_text(trading_value_krw),
        "priority": _decimal_text(trading_value_krw),
        "reason": "naver_mobile_market_value",
        "market": str(market).strip().upper(),
    }
    if timestamp:
        record["timestamp"] = timestamp

    change_rate = _decimal_from_text(
        raw_stock.get("fluctuationsRatio")
        or raw_stock.get("changeRate")
        or raw_stock.get("fluctuationsRatioText")
    )
    if change_rate is not None:
        record["change_rate"] = _decimal_text(change_rate)
    return record


def _dedupe_records_by_symbol(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_symbol: dict[str, dict[str, Any]] = {}
    for record in records:
        symbol = str(record.get("code") or "").strip()
        if not symbol:
            continue
        current_priority = _decimal_from_text(record.get("priority")) or Decimal("0")
        existing_priority = _decimal_from_text(by_symbol.get(symbol, {}).get("priority")) or Decimal("-1")
        if current_priority > existing_priority:
            by_symbol[symbol] = record
    return list(by_symbol.values())


def _naver_markets(markets: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    selected: list[str] = []
    for raw_market in markets or ("all",):
        key = str(raw_market or "").strip().lower()
        if key == "all":
            selected.extend(NAVER_MARKET_CATEGORIES)
            continue
        if key not in NAVER_MARKET_CATEGORIES:
            raise ValueError(f"unsupported naver market: {raw_market}")
        selected.append(key)
    return tuple(dict.fromkeys(selected))


def _naver_stop_after_page(
    payload: Mapping[str, Any],
    *,
    page: int,
    pages: int,
    page_size: int,
) -> bool:
    if pages > 0:
        return page >= pages
    stocks = payload.get("stocks", [])
    if not isinstance(stocks, list) or not stocks:
        return True
    total_count = _int_from_text(payload.get("totalCount"))
    payload_page_size = _int_from_text(payload.get("pageSize")) or page_size
    if total_count <= 0 or payload_page_size <= 0:
        return len(stocks) < page_size
    total_pages = (total_count + payload_page_size - 1) // payload_page_size
    return page >= total_pages


def _safe_naver_page_size(page_size: int) -> int:
    try:
        parsed = int(page_size)
    except (TypeError, ValueError):
        return 100
    return min(100, max(1, parsed))


def _url_with_query(url: str, query: Mapping[str, str] | None) -> str:
    if not query:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(query)}"


def _safe_timeout(timeout: float) -> float:
    try:
        parsed = float(timeout)
    except (TypeError, ValueError):
        return 10.0
    return max(1.0, parsed)


def _split_pair(raw_value: str, *, separator: str) -> tuple[str, str]:
    if separator not in raw_value:
        raise ValueError("expected KEY=VALUE or Header: Value")
    key, value = raw_value.split(separator, 1)
    key = key.strip()
    value = value.strip()
    if not key:
        raise ValueError("empty key")
    return key, value


def _decimal_from_text(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "").replace("%", "").replace("+", "")
    if not text or text in {"-", "N/A"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _int_from_text(value: object) -> int:
    parsed = _decimal_from_text(value)
    if parsed is None:
        return 0
    return max(0, int(parsed))


def _decimal_text(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f")
