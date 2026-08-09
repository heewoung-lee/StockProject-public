import sys
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.kis import (
    KIS_LIVE_BASE_URL,
    KisApiError,
    KisCredentials,
    KisLocalRateLimitError,
    KisOrderSubmissionUncertain,
    KisLiveOrderClient,
    KisLiveReadOnlyClient,
)
from stockbot.models import Order
from stockbot.rate_limit import KisRateLimiter, RateLimitDecision


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def push(self, response):
        self.responses.append(response)

    def __call__(self, request):
        self.calls.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TimedTransport(FakeTransport):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.call_times = []

    def __call__(self, request):
        self.call_times.append(self.clock())
        return super().__call__(request)


class RecordingRateLimiter:
    def __init__(self, *, allowed=True, retry_after=1.5, reason="allowed"):
        self.allowed = allowed
        self.retry_after = retry_after
        self.reason = reason
        self.allow_calls = []
        self.recorded_requests = []
        self.rate_limit_errors = []

    def allow_request(self, kind="query"):
        self.allow_calls.append(kind)
        return RateLimitDecision(self.allowed, self.retry_after if not self.allowed else 0.0, self.reason)

    def record_request(self, kind="query"):
        self.recorded_requests.append(kind)

    def record_rate_limit_error(self, retry_after_seconds=None):
        self.rate_limit_errors.append(retry_after_seconds)


class SequenceRateLimiter:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.allow_calls = []
        self.recorded_requests = []

    def allow_request(self, kind="query"):
        self.allow_calls.append(kind)
        if not self.decisions:
            return RateLimitDecision(True, 0.0, "allowed")
        return self.decisions.pop(0)

    def record_request(self, kind="query"):
        self.recorded_requests.append(kind)

    def record_rate_limit_error(self, retry_after_seconds=None):
        return None


class AcquiringRateLimiter:
    def __init__(self, decision):
        self.decision = decision
        self.acquire_calls = []
        self.recorded_requests = []

    def acquire_request(self, kind="query"):
        self.acquire_calls.append(kind)
        return self.decision

    def allow_request(self, kind="query"):
        raise AssertionError("allow_request fallback should not run")

    def record_request(self, kind="query"):
        self.recorded_requests.append(kind)


class ImmediateRecordingRateLimiter:
    def __init__(self):
        self.request_kinds = []
        self.token_issue_count = 0

    def run_request(self, kind, operation, **kwargs):
        self.request_kinds.append(kind)
        return RateLimitDecision(True, 0.0, "allowed"), operation()

    def record_token_issue(self):
        self.token_issue_count += 1

    def record_rate_limit_error(self, retry_after_seconds=None):
        return None


class TokenDenyingRateLimiter(ImmediateRecordingRateLimiter):
    def run_request(self, kind, operation, **kwargs):
        self.request_kinds.append(kind)
        if kind == "kis_token":
            return RateLimitDecision(False, 30.0, "token_cooldown"), None
        return RateLimitDecision(True, 0.0, "allowed"), operation()


class MemoryTokenCache:
    def __init__(self, access_token, expires_at):
        self.cached = SimpleNamespace(access_token=access_token, expires_at=expires_at)
        self.invalidations = 0
        self.writes = []

    def read(self, credentials):
        return self.cached

    def write(self, credentials, access_token, expires_at):
        self.cached = SimpleNamespace(access_token=access_token, expires_at=expires_at)
        self.writes.append((access_token, expires_at))

    def invalidate(self, credentials):
        self.cached = None
        self.invalidations += 1


def credentials():
    return KisCredentials(
        app_key="live-app-key",
        app_secret="live-app-secret",
        account_no="87654321",
        account_product_code="01",
    )


class KisLiveReadOnlyClientTest(unittest.TestCase):
    def test_live_get_refreshes_expired_access_token_once_and_updates_cache(self):
        transport = FakeTransport()
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        transport.push(
            {
                "access_token": "fresh-token",
                "access_token_token_expired": "2026-07-24 13:00:00",
            }
        )
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        limiter = ImmediateRecordingRateLimiter()
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        response = client.inquire_price("005930")

        self.assertEqual("70000", response["output"]["stck_prpr"])
        self.assertEqual(
            [
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
            ],
            [request.path for request in transport.calls],
        )
        self.assertEqual("Bearer stale-token", transport.calls[0].headers["authorization"])
        self.assertEqual("Bearer fresh-token", transport.calls[2].headers["authorization"])
        self.assertEqual(["kis_live_read", "kis_token", "kis_live_read"], limiter.request_kinds)
        self.assertEqual(1, limiter.token_issue_count)
        self.assertEqual(1, cache.invalidations)
        self.assertEqual("fresh-token", cache.writes[-1][0])

    def test_live_get_refreshes_expired_access_token_from_error_response(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "expired"})
        transport.push(
            {
                "access_token": "fresh-token",
                "access_token_token_expired": "2026-07-24 13:00:00",
            }
        )
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=MemoryTokenCache(
                "stale-token",
                datetime(2026, 7, 24, 9, 0, 0),
            ),
            rate_limiter=ImmediateRecordingRateLimiter(),
        )

        response = client.inquire_price("005930")

        self.assertEqual("70000", response["output"]["stck_prpr"])
        self.assertEqual("Bearer fresh-token", transport.calls[-1].headers["authorization"])

    def test_live_get_stops_after_refreshed_access_token_is_also_expired(self):
        transport = FakeTransport()
        expired = KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}')
        transport.push(expired)
        transport.push(
            {
                "access_token": "fresh-token",
                "access_token_token_expired": "2026-07-24 13:00:00",
            }
        )
        transport.push(expired)
        limiter = ImmediateRecordingRateLimiter()
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "EGW00123"):
            client.inquire_price("005930")

        self.assertEqual(3, len(transport.calls))
        self.assertEqual(1, limiter.token_issue_count)
        self.assertEqual(2, cache.invalidations)

    def test_live_price_bar_uses_refreshed_token_during_token_cooldown(self):
        clock = FakeClock()
        transport = FakeTransport()
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        transport.push(
            {
                "access_token": "fresh-token",
                "access_token_token_expired": "2026-07-24 13:00:00",
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "output": {"stck_prpr": "70000", "temp_stop_yn": "N"},
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "output1": {"askp1": "70100", "bidp1": "69900"},
                "output2": {},
            }
        )
        limiter = KisRateLimiter(
            min_interval_seconds=0.0,
            token_cooldown_seconds=2.0,
            clock=clock,
            sleeper=lambda seconds: clock.advance(seconds),
        )
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        bar = client.price_bar("005930")

        self.assertEqual(Decimal("70000"), bar.close)
        self.assertEqual(Decimal("70100"), bar.ask)
        self.assertEqual(
            [
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            ],
            [request.path for request in transport.calls],
        )
        self.assertEqual("Bearer fresh-token", transport.calls[-1].headers["authorization"])
        self.assertEqual("token_cooldown", limiter.allow_request("kis_token").reason)

    def test_live_get_records_cooldown_when_token_refresh_is_rate_limited(self):
        transport = FakeTransport()
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        transport.push(
            {
                "rt_cd": "1",
                "msg_cd": "EGW00133",
                "msg1": "token issuance rate limited",
            }
        )
        limiter = ImmediateRecordingRateLimiter()
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "EGW00133"):
            client.inquire_price("005930")

        self.assertEqual(
            ["/uapi/domestic-stock/v1/quotations/inquire-price", "/oauth2/tokenP"],
            [request.path for request in transport.calls],
        )
        self.assertEqual(1, limiter.token_issue_count)
        self.assertEqual(1, cache.invalidations)
        self.assertEqual([], cache.writes)

    def test_live_get_records_cooldown_when_token_refresh_transport_is_uncertain(self):
        transport = FakeTransport()
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        transport.push(KisApiError("KIS network timeout during token issuance"))
        limiter = ImmediateRecordingRateLimiter()
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=MemoryTokenCache(
                "stale-token",
                datetime(2026, 7, 24, 9, 0, 0),
            ),
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "network timeout"):
            client.inquire_price("005930")

        self.assertEqual(1, limiter.token_issue_count)
        self.assertEqual(["kis_live_read", "kis_token"], limiter.request_kinds)

    def test_live_get_does_not_record_token_issue_when_refresh_is_locally_denied(self):
        transport = FakeTransport()
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        limiter = TokenDenyingRateLimiter()
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            token_cache=MemoryTokenCache(
                "stale-token",
                datetime(2026, 7, 24, 9, 0, 0),
            ),
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisLocalRateLimitError, "token_cooldown"):
            client.inquire_price("005930")

        self.assertEqual(0, limiter.token_issue_count)
        self.assertEqual(1, len(transport.calls))

    def test_uncertain_token_refresh_blocks_second_client_on_shared_limiter(self):
        clock = FakeClock()
        limiter = KisRateLimiter(
            min_interval_seconds=0.0,
            token_cooldown_seconds=61.0,
            clock=clock,
            sleeper=lambda seconds: clock.advance(seconds),
        )
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        first_transport = FakeTransport()
        first_transport.push(KisApiError("KIS HTTP 500: EGW00123 expired"))
        first_transport.push(KisApiError("KIS network timeout during token issuance"))
        first_client = KisLiveReadOnlyClient(
            credentials(),
            transport=first_transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "network timeout"):
            first_client.inquire_price("005930")

        second_transport = FakeTransport()
        second_transport.push(KisApiError("KIS HTTP 500: EGW00123 expired"))
        second_client = KisLiveReadOnlyClient(
            credentials(),
            transport=second_transport,
            access_token="stale-token",
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisLocalRateLimitError, "token_cooldown"):
            second_client.inquire_price("005930")

        self.assertEqual(
            ["/uapi/domestic-stock/v1/quotations/inquire-price"],
            [request.path for request in second_transport.calls],
        )

    def test_physical_market_read_budget_counts_requests_and_exempts_order_safety_reads(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        transport.push(
            {"rt_cd": "0", "output1": {"askp1": "70100", "bidp1": "69900"}, "output2": {}}
        )
        transport.push(
            {"rt_cd": "0", "output": {"ord_psbl_cash": "100000", "ord_psbl_qty": "1"}}
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")
        client.begin_market_read_budget(2)

        client.inquire_price("005930")
        client.inquire_asking_price_exp_ccn("005930")
        buyable = client.inquire_buyable_order("005930", order_price=Decimal("70000"))
        with self.assertRaisesRegex(KisLocalRateLimitError, "physical market read budget"):
            client.inquire_price("000660")

        self.assertEqual("100000", buyable["output"]["ord_psbl_cash"])
        self.assertEqual((2, 2), client.market_read_budget_state())
        self.assertEqual(3, len(transport.calls))

    def test_ensure_market_read_budget_preserves_used_and_only_raises_active_limit(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
        )

        client.ensure_market_read_budget(8)
        self.assertIsNone(client.market_read_budget_state())

        client.begin_market_read_budget(4)
        client.inquire_price("005930")
        client.inquire_price("000660")
        self.assertEqual((2, 4), client.market_read_budget_state())

        client.ensure_market_read_budget(3)
        client.ensure_market_read_budget(-1)
        client.ensure_market_read_budget(True)
        client.ensure_market_read_budget("invalid")
        self.assertEqual((2, 4), client.market_read_budget_state())

        client.ensure_market_read_budget(8)
        self.assertEqual((2, 8), client.market_read_budget_state())

        client.end_market_read_budget()
        client.ensure_market_read_budget(12)
        self.assertIsNone(client.market_read_budget_state())

    def test_live_client_uses_live_base_url_for_token_price_orderbook_and_balance(self):
        timestamp = datetime(2026, 6, 30, 9, 5, tzinfo=timezone.utc)
        transport = FakeTransport()
        transport.push({"access_token": "live-token"})
        transport.push(
            {
                "rt_cd": "0",
                "output": {
                    "stck_prpr": "70000",
                    "acml_vol": "100",
                    "temp_stop_yn": "N",
                },
            }
        )
        transport.push({"rt_cd": "0", "output1": {"askp1": "70100", "bidp1": "69900"}, "output2": {}})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [{"pdno": "005930", "hldg_qty": "1", "pchs_avg_pric": "69000", "prpr": "70000"}],
                "output2": [{"dnca_tot_amt": "100000", "ord_psbl_cash": "100000"}],
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "output1": [{"trad_dt": "20260630", "rlzt_pfls": "1234"}],
                "output2": [{"tot_rlzt_pfls": "1234"}],
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport)

        self.assertEqual("live-token", client.issue_access_token())
        bar = client.price_bar("005930", timestamp=timestamp)
        account = client.account_snapshot(timestamp=timestamp)

        self.assertEqual(Decimal("70000"), bar.close)
        self.assertEqual(Decimal("70100"), bar.ask)
        self.assertEqual(Decimal("69900"), bar.bid)
        self.assertEqual(Decimal("170000"), account.equity)
        self.assertEqual(Decimal("1234"), account.realized_pnl_today)
        self.assertTrue(account.realized_pnl_today_known)
        self.assertEqual([KIS_LIVE_BASE_URL] * 5, [call.base_url for call in transport.calls])
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-period-profit",
            ],
            [call.path for call in transport.calls],
        )
        self.assertEqual(["POST", "GET", "GET", "GET", "GET"], [call.method for call in transport.calls])
        self.assertEqual("FHKST01010200", transport.calls[2].headers["tr_id"])
        self.assertEqual("J", transport.calls[2].params["FID_COND_MRKT_DIV_CODE"])
        self.assertEqual("005930", transport.calls[2].params["FID_INPUT_ISCD"])
        self.assertEqual("TTTC8434R", transport.calls[3].headers["tr_id"])
        self.assertEqual("TTTC8708R", transport.calls[4].headers["tr_id"])
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", [call.path for call in transport.calls])

    def test_live_price_bar_keeps_unavailable_orderbook_fail_closed_by_default(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "output": {"stck_prpr": "70000", "temp_stop_yn": "N"},
            }
        )
        transport.push({"rt_cd": "0", "output1": {"askp1": "0", "bidp1": "0"}, "output2": {}})
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        with self.assertRaisesRegex(ValueError, "invalid KIS best quote field: askp1"):
            client.price_bar("005930")

    def test_live_price_bar_skips_orderbook_when_symbol_is_temporarily_stopped(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "output": {
                    "stck_prpr": "70000",
                    "temp_stop_yn": "Y",
                    "rprs_mrkt_kor_name": "코스닥",
                },
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        stopped_bar = client.price_bar("005930")

        self.assertTrue(stopped_bar.temporary_stop)
        self.assertIsNone(stopped_bar.ask)
        self.assertEqual(
            ["/uapi/domestic-stock/v1/quotations/inquire-price"],
            [request.path for request in transport.calls],
        )

    def test_live_price_bar_skips_orderbook_when_kis_trading_state_is_missing(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000"}})
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        unknown_bar = client.price_bar("005930")

        self.assertIsNone(unknown_bar.temporary_stop)
        self.assertIsNone(unknown_bar.ask)
        self.assertEqual(
            ["/uapi/domestic-stock/v1/quotations/inquire-price"],
            [request.path for request in transport.calls],
        )

    def test_live_account_snapshot_marks_daily_realized_pnl_unknown_when_profit_query_fails(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(KisApiError("profit unavailable"))
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        account = client.account_snapshot(timestamp=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc))

        self.assertEqual(Decimal("0"), account.realized_pnl_today)
        self.assertFalse(account.realized_pnl_today_known)

    def test_live_client_queries_and_parses_same_day_minute_bars(self):
        now = datetime(2026, 7, 10, 0, 5, 6, tzinfo=timezone.utc)
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "output1": {"stck_prpr": "70100"},
                "output2": [
                    {
                        "stck_bsop_date": "20260710",
                        "stck_cntg_hour": "090400",
                        "stck_oprc": "69900",
                        "stck_hgpr": "70100",
                        "stck_lwpr": "69800",
                        "stck_prpr": "70000",
                        "cntg_vol": "10",
                    },
                    {
                        "stck_bsop_date": "20260710",
                        "stck_cntg_hour": "090500",
                        "stck_oprc": "70000",
                        "stck_hgpr": "70200",
                        "stck_lwpr": "69900",
                        "stck_prpr": "70100",
                        "cntg_vol": "12",
                    }
                ],
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        bars = client.minute_bars("005930", now=now)

        self.assertEqual(1, len(bars))
        self.assertEqual("2026-07-10T09:04:00+09:00", bars[0].timestamp.isoformat())
        request = transport.calls[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", request.path)
        self.assertEqual("FHKST03010200", request.headers["tr_id"])
        self.assertEqual("J", request.params["FID_COND_MRKT_DIV_CODE"])
        self.assertEqual("005930", request.params["FID_INPUT_ISCD"])
        self.assertEqual("090459", request.params["FID_INPUT_HOUR_1"])
        self.assertEqual("Y", request.params["FID_PW_DATA_INCU_YN"])
        self.assertEqual("", request.params["FID_ETC_CLS_CODE"])

    def test_live_client_queries_exact_date_realized_pnl(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "output1": [{"trad_dt": "20260710", "rlzt_pfls": "-1,250"}],
                "output2": {"tot_rlzt_pfls": "-1250"},
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        pnl = client.realized_pnl_today(date(2026, 7, 10))

        self.assertEqual(Decimal("-1250"), pnl)
        request = transport.calls[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/inquire-period-profit", request.path)
        self.assertEqual("TTTC8708R", request.headers["tr_id"])
        self.assertEqual("87654321", request.params["CANO"])
        self.assertEqual("01", request.params["ACNT_PRDT_CD"])
        self.assertEqual("20260710", request.params["INQR_STRT_DT"])
        self.assertEqual("20260710", request.params["INQR_END_DT"])
        self.assertEqual("00", request.params["SORT_DVSN"])
        self.assertEqual("00", request.params["INQR_DVSN"])
        self.assertEqual("00", request.params["CBLC_DVSN"])

    def test_live_client_queries_period_profit_across_all_pages(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "M",
                "ctx_area_fk100": "next-fk",
                "ctx_area_nk100": "next-nk",
                "output1": [
                    {
                        "trad_dt": "20260710",
                        "rlzt_pfls": "100",
                        "fee": "5",
                        "tl_tax": "3",
                        "loan_int": "0",
                    }
                ],
                "output2": {"tot_rlzt_pfls": "150"},
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "",
                "ctx_area_fk100": "",
                "ctx_area_nk100": "",
                "output1": [
                    {
                        "trad_dt": "20260709",
                        "rlzt_pfls": "50",
                        "fee": "2",
                        "tl_tax": "1",
                        "loan_int": "0",
                    }
                ],
                "output2": {"tot_rlzt_pfls": "150"},
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        rows = client.period_profit_rows(date(2026, 7, 1), date(2026, 7, 10))

        self.assertEqual([date(2026, 7, 9), date(2026, 7, 10)], [row.trading_date for row in rows])
        self.assertEqual(Decimal("100"), rows[1].realized_pnl)
        first, second = transport.calls
        self.assertEqual("20260701", first.params["INQR_STRT_DT"])
        self.assertEqual("20260710", first.params["INQR_END_DT"])
        self.assertNotIn("tr_cont", first.headers)
        self.assertEqual("", first.params["CTX_AREA_FK100"])
        self.assertEqual("", first.params["CTX_AREA_NK100"])
        self.assertEqual("N", second.headers["tr_cont"])
        self.assertEqual("next-fk", second.params["CTX_AREA_FK100"])
        self.assertEqual("next-nk", second.params["CTX_AREA_NK100"])

    def test_live_client_period_profit_rejects_repeated_continuation_and_max_pages(self):
        repeated_transport = FakeTransport()
        for _ in range(2):
            repeated_transport.push(
                {
                    "rt_cd": "0",
                    "tr_cont": "M",
                    "ctx_area_fk100": "same-fk",
                    "ctx_area_nk100": "same-nk",
                    "output1": [],
                    "output2": {},
                }
            )
        repeated_client = KisLiveReadOnlyClient(
            credentials(),
            transport=repeated_transport,
            access_token="token",
        )

        with self.assertRaisesRegex(KisApiError, "repeated context keys"):
            repeated_client.period_profit_rows(date(2026, 7, 1), date(2026, 7, 10))

        max_transport = FakeTransport()
        for page in range(10):
            max_transport.push(
                {
                    "rt_cd": "0",
                    "tr_cont": "M",
                    "ctx_area_fk100": f"fk-{page}",
                    "ctx_area_nk100": f"nk-{page}",
                    "output1": [],
                    "output2": {},
                }
            )
        max_client = KisLiveReadOnlyClient(credentials(), transport=max_transport, access_token="token")

        with self.assertRaisesRegex(KisApiError, "exceeded max pages"):
            max_client.period_profit_rows(date(2026, 7, 1), date(2026, 7, 10))
        self.assertEqual(10, len(max_transport.calls))

    def test_live_client_period_profit_rejects_reversed_range_before_transport(self):
        transport = FakeTransport()
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        with self.assertRaisesRegex(ValueError, "start date must not be after end date"):
            client.period_profit_rows(date(2026, 7, 11), date(2026, 7, 10))

        self.assertEqual([], transport.calls)

    def test_account_snapshot_observes_exact_day_profit_without_account_identifiers(self):
        observed = []

        def profit_observer(rows, observed_at, start_date, end_date):
            observed.append((rows, observed_at, start_date, end_date))

        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [
                    {
                        "trad_dt": "20260710",
                        "rlzt_pfls": "125",
                        "fee": "7",
                        "tl_tax": "3",
                        "loan_int": "0",
                    },
                ],
                "output2": {"tot_rlzt_pfls": "75"},
            }
        )
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            profit_observer=profit_observer,
        )
        timestamp = datetime(2026, 7, 10, 1, 2, 3, tzinfo=timezone.utc)

        account = client.account_snapshot(timestamp=timestamp)

        self.assertEqual(Decimal("125"), account.realized_pnl_today)
        self.assertTrue(account.realized_pnl_today_known)
        self.assertEqual(1, len(observed))
        rows, observed_at, start_date, end_date = observed[0]
        self.assertEqual([date(2026, 7, 10)], [row.trading_date for row in rows])
        self.assertEqual("2026-07-10T10:02:03+09:00", observed_at.isoformat())
        self.assertEqual(date(2026, 7, 10), start_date)
        self.assertEqual(date(2026, 7, 10), end_date)
        self.assertNotIn(credentials().account_no, repr(observed[0]))
        profit_request = transport.calls[1]
        self.assertEqual("20260710", profit_request.params["INQR_STRT_DT"])
        self.assertEqual("20260710", profit_request.params["INQR_END_DT"])

    def test_account_snapshot_queries_exact_day_on_every_snapshot(self):
        observed = []

        def profit_observer(rows, observed_at, start_date, end_date):
            observed.append((rows, start_date, end_date))

        transport = FakeTransport()
        for realized_pnl in ("10", "-30"):
            transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
            transport.push(
                {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "trad_dt": "20260710",
                            "rlzt_pfls": realized_pnl,
                            "fee": "2",
                            "tl_tax": "1",
                            "loan_int": "0",
                        }
                    ],
                    "output2": {"tot_rlzt_pfls": realized_pnl},
                }
            )
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            profit_observer=profit_observer,
        )
        timestamp = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)

        first_account = client.account_snapshot(timestamp=timestamp)
        second_account = client.account_snapshot(timestamp=timestamp)

        self.assertTrue(first_account.realized_pnl_today_known)
        self.assertEqual(Decimal("10"), first_account.realized_pnl_today)
        self.assertTrue(second_account.realized_pnl_today_known)
        self.assertEqual(Decimal("-30"), second_account.realized_pnl_today)
        self.assertEqual(2, len(observed))
        self.assertEqual((date(2026, 7, 10), date(2026, 7, 10)), observed[0][1:])
        self.assertEqual((date(2026, 7, 10), date(2026, 7, 10)), observed[1][1:])
        self.assertEqual("20260710", transport.calls[1].params["INQR_STRT_DT"])
        self.assertEqual("20260710", transport.calls[3].params["INQR_STRT_DT"])

    def test_account_snapshot_keeps_today_unknown_after_exact_query_failure(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(KisApiError("today unavailable"))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            profit_observer=lambda *args: None,
        )

        account = client.account_snapshot(
            timestamp=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        )

        self.assertFalse(account.realized_pnl_today_known)
        self.assertEqual(Decimal("0"), account.realized_pnl_today)
        self.assertEqual("20260710", transport.calls[1].params["INQR_STRT_DT"])
        self.assertEqual("20260710", transport.calls[1].params["INQR_END_DT"])

    def test_account_snapshot_keeps_today_unknown_when_exact_row_omits_realized_pnl(self):
        observed = []
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [{"trad_dt": "20260710", "rlzt_pfls": ""}],
                "output2": {},
            }
        )
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            profit_observer=lambda *args: observed.append(args),
        )

        account = client.account_snapshot(
            timestamp=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)
        )

        self.assertFalse(account.realized_pnl_today_known)
        self.assertEqual(Decimal("0"), account.realized_pnl_today)
        self.assertEqual([], observed)

    def test_account_snapshot_isolates_profit_observer_failures(self):
        def failing_observer(rows, observed_at, start_date, end_date):
            raise RuntimeError("storage unavailable")

        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [
                    {
                        "trad_dt": "20260710",
                        "rlzt_pfls": "25",
                        "fee": "1",
                        "tl_tax": "0",
                        "loan_int": "0",
                    }
                ],
                "output2": {"tot_rlzt_pfls": "25"},
            }
        )
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"ord_psbl_cash": "100000"}]})
        transport.push(
            {
                "rt_cd": "0",
                "output1": [
                    {
                        "trad_dt": "20260710",
                        "rlzt_pfls": "25",
                        "fee": "1",
                        "tl_tax": "0",
                        "loan_int": "0",
                    }
                ],
                "output2": {"tot_rlzt_pfls": "25"},
            }
        )
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            profit_observer=failing_observer,
        )

        account = client.account_snapshot(timestamp=datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc))
        second_account = client.account_snapshot(timestamp=datetime(2026, 7, 10, 1, 1, tzinfo=timezone.utc))

        self.assertEqual(Decimal("25"), account.realized_pnl_today)
        self.assertTrue(account.realized_pnl_today_known)
        self.assertEqual(Decimal("25"), second_account.realized_pnl_today)
        self.assertEqual("20260710", transport.calls[1].params["INQR_STRT_DT"])
        self.assertEqual("20260710", transport.calls[3].params["INQR_STRT_DT"])

    def test_live_date_queries_normalize_aware_datetimes_to_kst(self):
        utc_value = datetime(2026, 7, 9, 16, 0, tzinfo=timezone.utc)
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "output1": [{"trad_dt": "20260710", "rlzt_pfls": "0"}],
                "output2": {},
            }
        )
        transport.push(
            {"rt_cd": "0", "output": [{"bass_dt": "20260710", "opnd_yn": "Y"}]}
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        client.realized_pnl_today(utc_value)
        client.is_opening_day(utc_value)

        self.assertEqual("20260710", transport.calls[0].params["INQR_STRT_DT"])
        self.assertEqual("20260710", transport.calls[1].params["BASS_DT"])

    def test_exact_date_queries_stop_on_first_page_even_with_continuation_header(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "M",
                "ctx_area_fk100": "unused-fk",
                "ctx_area_nk100": "unused-nk",
                "output1": [{"trad_dt": "20260710", "rlzt_pfls": "10"}],
                "output2": {},
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "M",
                "ctx_area_fk": "unused-fk",
                "ctx_area_nk": "unused-nk",
                "output": [{"bass_dt": "20260710", "opnd_yn": "Y"}],
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        self.assertEqual(Decimal("10"), client.realized_pnl_today(date(2026, 7, 10)))
        self.assertTrue(client.is_opening_day(date(2026, 7, 10)))

        self.assertEqual(2, len(transport.calls))

    def test_live_client_follows_holiday_continuation_and_reads_exact_opening_day(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "M",
                "ctx_area_fk": "next-fk",
                "ctx_area_nk": "next-nk",
                "output": [{"bass_dt": "20260709", "opnd_yn": "Y"}],
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "",
                "ctx_area_fk": "",
                "ctx_area_nk": "",
                "output": [{"bass_dt": "20260710", "opnd_yn": "N"}],
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        is_opening_day = client.is_opening_day(date(2026, 7, 10))

        self.assertFalse(is_opening_day)
        first, second = transport.calls
        self.assertEqual("GET", first.method)
        self.assertEqual("/uapi/domestic-stock/v1/quotations/chk-holiday", first.path)
        self.assertEqual("CTCA0903R", first.headers["tr_id"])
        self.assertEqual("20260710", first.params["BASS_DT"])
        self.assertEqual("", first.params["CTX_AREA_FK"])
        self.assertEqual("", first.params["CTX_AREA_NK"])
        self.assertEqual("next-fk", second.params["CTX_AREA_FK"])
        self.assertEqual("next-nk", second.params["CTX_AREA_NK"])
        self.assertEqual("N", second.headers["tr_cont"])

    def test_live_balance_inquiry_follows_kis_continuation_pages(self):
        transport = FakeTransport()
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "M",
                "ctx_area_fk100": "next-fk",
                "ctx_area_nk100": "next-nk",
                "output1": [{"pdno": "005930", "hldg_qty": "1", "pchs_avg_pric": "69000", "prpr": "70000"}],
                "output2": [{"dnca_tot_amt": "100000", "ord_psbl_cash": "100000"}],
            }
        )
        transport.push(
            {
                "rt_cd": "0",
                "tr_cont": "",
                "ctx_area_fk100": "",
                "ctx_area_nk100": "",
                "output1": [{"pdno": "000660", "hldg_qty": "2", "pchs_avg_pric": "120000", "prpr": "121000"}],
                "output2": [{"dnca_tot_amt": "100000", "ord_psbl_cash": "100000"}],
            }
        )
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        snapshot = client.account_snapshot()

        self.assertEqual(["005930", "000660"], list(snapshot.positions))
        self.assertEqual("next-fk", transport.calls[1].params["CTX_AREA_FK100"])
        self.assertEqual("next-nk", transport.calls[1].params["CTX_AREA_NK100"])
        self.assertEqual("N", transport.calls[1].headers["tr_cont"])

    def test_live_client_queries_daily_order_executions_with_live_tr_id(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": {"ctx_area_fk100": "", "ctx_area_nk100": ""}})
        client = KisLiveReadOnlyClient(credentials(), transport=transport, access_token="token")

        client.inquire_daily_orders(
            inquiry_start_date=date(2026, 7, 3),
            inquiry_end_date=date(2026, 7, 3),
            order_no="0000012345",
            symbol="005930",
            side_code="02",
            execution_code="01",
            ctx_area_fk100="fk",
            ctx_area_nk100="nk",
            tr_cont="N",
        )

        request = transport.calls[0]
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", request.path)
        self.assertEqual("TTTC0081R", request.headers["tr_id"])
        self.assertEqual("87654321", request.params["CANO"])
        self.assertEqual("01", request.params["ACNT_PRDT_CD"])
        self.assertEqual("20260703", request.params["INQR_STRT_DT"])
        self.assertEqual("20260703", request.params["INQR_END_DT"])
        self.assertEqual("02", request.params["SLL_BUY_DVSN_CD"])
        self.assertEqual("01", request.params["CCLD_DVSN"])
        self.assertEqual("005930", request.params["PDNO"])
        self.assertEqual("0000012345", request.params["ODNO"])
        self.assertEqual("KRX", request.params["EXCG_ID_DVSN_CD"])
        self.assertEqual("fk", request.params["CTX_AREA_FK100"])
        self.assertEqual("nk", request.params["CTX_AREA_NK100"])
        self.assertEqual("N", request.headers["tr_cont"])

    def test_live_client_respects_local_rate_limiter_before_transport(self):
        transport = FakeTransport()
        limiter = RecordingRateLimiter(allowed=False, retry_after=2.0, reason="api_backoff")
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            rate_limiter=limiter,
        )
        client.begin_market_read_budget(1)

        with self.assertRaisesRegex(KisApiError, "local rate limit"):
            client.inquire_price("005930")

        self.assertEqual([], transport.calls)
        self.assertEqual(["kis_live_read"], limiter.allow_calls)
        self.assertEqual([], limiter.recorded_requests)
        self.assertEqual((0, 1), client.market_read_budget_state())

    def test_run_request_rate_limit_rejection_does_not_consume_physical_read_budget(self):
        clock = FakeClock()
        transport = FakeTransport()
        limiter = KisRateLimiter(clock=clock, sleeper=clock.advance)
        limiter.record_rate_limit_error(retry_after_seconds=10.0)
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            rate_limiter=limiter,
        )
        client.begin_market_read_budget(1)

        with self.assertRaisesRegex(KisLocalRateLimitError, "local rate limit"):
            client.inquire_price("005930")

        self.assertEqual([], transport.calls)
        self.assertEqual((0, 1), client.market_read_budget_state())

    def test_live_client_uses_atomic_acquire_without_recording_twice(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000", "acml_vol": "100"}})
        limiter = AcquiringRateLimiter(RateLimitDecision(True, 0.0, "allowed"))
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            rate_limiter=limiter,
        )

        client.inquire_price("005930")

        self.assertEqual(["kis_live_read"], limiter.acquire_calls)
        self.assertEqual([], limiter.recorded_requests)

    def test_live_client_records_kis_per_second_rate_limit_response(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "ledger request limit"})
        limiter = RecordingRateLimiter()
        client = KisLiveReadOnlyClient(
            credentials(),
            transport=transport,
            access_token="token",
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "EGW00215"):
            client.inquire_balance()

        self.assertEqual(["kis_live_read"], limiter.allow_calls)
        self.assertEqual(["kis_live_read"], limiter.recorded_requests)
        self.assertEqual([1.5], limiter.rate_limit_errors)

    def test_live_client_retries_safe_get_once_after_kis_rate_limit(self):
        first_responses = (
            {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "ledger request limit"},
            KisApiError("KIS HTTP 500: EGW00215 초당 거래건수 초과"),
        )
        for first_response in first_responses:
            with self.subTest(first_response=type(first_response).__name__):
                clock = FakeClock()
                sleep_calls = []

                def sleeper(seconds):
                    sleep_calls.append(seconds)
                    clock.advance(seconds)

                transport = TimedTransport(clock)
                transport.push(first_response)
                transport.push({"rt_cd": "0", "output": {"stck_prpr": "70000", "acml_vol": "100"}})
                limiter = KisRateLimiter(
                    min_interval_seconds=1.25,
                    clock=clock,
                    sleeper=sleeper,
                )
                client = KisLiveReadOnlyClient(
                    credentials(),
                    transport=transport,
                    access_token="token",
                    rate_limiter=limiter,
                )
                client.begin_market_read_budget(2)

                response = client.inquire_price("005930")

                self.assertEqual("0", response["rt_cd"])
                self.assertEqual([100.0, 102.0], transport.call_times)
                self.assertEqual([2.0], sleep_calls)
                self.assertEqual((2, 2), client.market_read_budget_state())

    def test_live_read_only_client_has_no_order_method(self):
        client = KisLiveReadOnlyClient(credentials(), transport=FakeTransport())

        self.assertFalse(hasattr(client, "place_cash_order"))

    def test_live_client_rejects_vts_base_url(self):
        with self.assertRaisesRegex(ValueError, "KIS live read-only client only supports"):
            KisLiveReadOnlyClient(credentials(), base_url="https://openapivts.koreainvestment.com:29443")

    def test_live_order_client_rejects_vts_base_url(self):
        with self.assertRaisesRegex(ValueError, "KIS live read-only client only supports"):
            KisLiveOrderClient(credentials(), base_url="https://openapivts.koreainvestment.com:29443")

    def test_live_order_client_requires_explicit_order_gate(self):
        transport = FakeTransport()
        client = KisLiveOrderClient(credentials(), transport=transport, access_token="token")

        with self.assertRaisesRegex(RuntimeError, "allow_order_placement=True"):
            client.place_cash_order(Order.buy("005930", 1, "entry"), order_price=Decimal("70000"))

        self.assertEqual([], transport.calls)

    def test_live_order_client_requires_order_gate_before_hashkey_request(self):
        transport = FakeTransport()
        client = KisLiveOrderClient(credentials(), transport=transport, access_token="token")

        with self.assertRaisesRegex(RuntimeError, "allow_order_placement=True"):
            client.issue_hashkey({"PDNO": "005930"})

        self.assertEqual([], transport.calls)

    def test_live_order_client_requires_token_before_hashkey_request(self):
        transport = FakeTransport()
        client = KisLiveOrderClient(credentials(), transport=transport, allow_order_placement=True)

        with self.assertRaisesRegex(KisApiError, "access token"):
            client.place_cash_order(Order.buy("005930", 1, "entry"), order_price=Decimal("70000"))

        self.assertEqual([], transport.calls)

    def test_live_order_client_uses_deposit_cash_for_planning_when_balance_has_no_orderable_cash(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output1": [], "output2": [{"dnca_tot_amt": "100000"}]})
        client = KisLiveOrderClient(credentials(), transport=transport, access_token="token")

        account = client.account_snapshot()

        self.assertEqual(Decimal("100000"), account.cash)
        self.assertEqual(Decimal("100000"), account.buying_power)

    def test_live_order_client_queries_buyable_order_with_live_tr_id(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": {"ord_psbl_cash": "100000", "ord_psbl_qty": "3"}})
        client = KisLiveOrderClient(credentials(), transport=transport, access_token="token")

        response = client.inquire_buyable_order("005930", order_price=Decimal("70000"))

        request = transport.calls[0]
        self.assertEqual({"ord_psbl_cash": "100000", "ord_psbl_qty": "3"}, response["output"])
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/inquire-psbl-order", request.path)
        self.assertEqual("TTTC8908R", request.headers["tr_id"])
        self.assertEqual("87654321", request.params["CANO"])
        self.assertEqual("01", request.params["ACNT_PRDT_CD"])
        self.assertEqual("005930", request.params["PDNO"])
        self.assertEqual("70000", request.params["ORD_UNPR"])
        self.assertEqual("00", request.params["ORD_DVSN"])

    def test_live_order_client_maps_buy_and_sell_to_live_tr_ids(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-buy"})
        transport.push({"rt_cd": "0", "output": {"ODNO": "1"}})
        transport.push({"HASH": "hash-sell"})
        transport.push({"rt_cd": "0", "output": {"ODNO": "2"}})
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
        )

        client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))
        client.place_cash_order(Order.sell("005930", 1, "exit"), order_price=Decimal("70100"))

        self.assertEqual(4, len(transport.calls))
        buy_hash, buy_order, sell_hash, sell_order = transport.calls
        self.assertEqual(KIS_LIVE_BASE_URL, buy_hash.base_url)
        self.assertEqual("/uapi/hashkey", buy_hash.path)
        self.assertEqual("POST", buy_hash.method)
        self.assertEqual("005930", buy_hash.json["PDNO"])
        self.assertEqual("/uapi/domestic-stock/v1/trading/order-cash", buy_order.path)
        self.assertEqual("POST", buy_order.method)
        self.assertEqual("TTTC0012U", buy_order.headers["tr_id"])
        self.assertEqual("hash-buy", buy_order.headers["hashkey"])
        self.assertEqual("005930", buy_order.json["PDNO"])
        self.assertEqual("2", buy_order.json["ORD_QTY"])
        self.assertEqual("70000", buy_order.json["ORD_UNPR"])
        self.assertEqual("00", buy_order.json["ORD_DVSN"])
        self.assertNotIn("VTTC", buy_order.headers["tr_id"])

        self.assertEqual("/uapi/hashkey", sell_hash.path)
        self.assertEqual("TTTC0011U", sell_order.headers["tr_id"])
        self.assertEqual("hash-sell", sell_order.headers["hashkey"])
        self.assertEqual("01", sell_order.json["SLL_TYPE"])
        self.assertEqual("1", sell_order.json["ORD_QTY"])
        self.assertEqual("70100", sell_order.json["ORD_UNPR"])
        self.assertEqual("00", sell_order.json["ORD_DVSN"])

    def test_live_order_client_paces_hashkey_before_order_submission(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds):
            sleep_calls.append(seconds)
            clock.advance(seconds)

        transport = TimedTransport(clock)
        transport.push({"HASH": "hash-buy"})
        transport.push({"rt_cd": "0", "output": {}})
        limiter = KisRateLimiter(min_interval_seconds=1.25, clock=clock, sleeper=sleeper)
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
            rate_limiter=limiter,
        )

        client.place_cash_order(Order.buy("005930", 1, "entry"), order_price=Decimal("70000"))

        self.assertEqual([100.0, 101.25], transport.call_times)
        self.assertEqual([1.25], sleep_calls)

    def test_live_order_client_queries_cancelable_orders_with_live_tr_id(self):
        transport = FakeTransport()
        transport.push({"rt_cd": "0", "output": [{"odno": "0000012345", "psbl_qty": "3"}]})
        client = KisLiveOrderClient(credentials(), transport=transport, access_token="token")

        response = client.inquire_cancelable_orders(ctx_area_fk100="fk", ctx_area_nk100="nk", tr_cont="N")

        request = transport.calls[0]
        self.assertEqual([{"odno": "0000012345", "psbl_qty": "3"}], response["output"])
        self.assertEqual("GET", request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl", request.path)
        self.assertEqual("TTTC0084R", request.headers["tr_id"])
        self.assertEqual("87654321", request.params["CANO"])
        self.assertEqual("01", request.params["ACNT_PRDT_CD"])
        self.assertEqual("1", request.params["INQR_DVSN_1"])
        self.assertEqual("0", request.params["INQR_DVSN_2"])
        self.assertEqual("fk", request.params["CTX_AREA_FK100"])
        self.assertEqual("nk", request.params["CTX_AREA_NK100"])
        self.assertEqual("N", request.headers["tr_cont"])

    def test_live_order_client_cancels_remaining_order_with_live_tr_id(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-cancel"})
        transport.push({"rt_cd": "0", "output": {"ODNO": "0000012345"}})
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
        )

        response = client.cancel_cash_order(
            order_no="0000012345",
            order_org_no="12345",
            quantity=3,
            order_price=Decimal("2180"),
        )

        self.assertEqual("0", response["rt_cd"])
        hash_request, cancel_request = transport.calls
        self.assertEqual("/uapi/hashkey", hash_request.path)
        self.assertEqual("POST", cancel_request.method)
        self.assertEqual("/uapi/domestic-stock/v1/trading/order-rvsecncl", cancel_request.path)
        self.assertEqual("TTTC0013U", cancel_request.headers["tr_id"])
        self.assertEqual("hash-cancel", cancel_request.headers["hashkey"])
        self.assertEqual("87654321", cancel_request.json["CANO"])
        self.assertEqual("01", cancel_request.json["ACNT_PRDT_CD"])
        self.assertEqual("12345", cancel_request.json["KRX_FWDG_ORD_ORGNO"])
        self.assertEqual("0000012345", cancel_request.json["ORGN_ODNO"])
        self.assertEqual("00", cancel_request.json["ORD_DVSN"])
        self.assertEqual("02", cancel_request.json["RVSE_CNCL_DVSN_CD"])
        self.assertEqual("3", cancel_request.json["ORD_QTY"])
        self.assertEqual("2180", cancel_request.json["ORD_UNPR"])
        self.assertEqual("Y", cancel_request.json["QTY_ALL_ORD_YN"])
        self.assertEqual("KRX", cancel_request.json["EXCG_ID_DVSN_CD"])

    def test_live_order_client_marks_order_post_transport_error_as_uncertain(self):
        class FailingOrderTransport(FakeTransport):
            def __call__(self, request):
                self.calls.append(request)
                if request.path == "/uapi/hashkey":
                    return {"HASH": "hash-buy"}
                if request.path == "/uapi/domestic-stock/v1/trading/order-cash":
                    raise KisApiError("KIS network timeout after POST")
                raise AssertionError(f"unexpected request path: {request.path}")

        transport = FailingOrderTransport()
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
        )

        with self.assertRaisesRegex(KisOrderSubmissionUncertain, "submission uncertain"):
            client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))

        self.assertEqual(
            ["/uapi/hashkey", "/uapi/domestic-stock/v1/trading/order-cash"],
            [call.path for call in transport.calls],
        )

    def test_live_order_client_never_refreshes_or_retries_expired_order_post(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-buy"})
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        limiter = ImmediateRecordingRateLimiter()
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            allow_order_placement=True,
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisOrderSubmissionUncertain, "submission uncertain"):
            client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))

        self.assertEqual(
            ["/uapi/hashkey", "/uapi/domestic-stock/v1/trading/order-cash"],
            [call.path for call in transport.calls],
        )
        self.assertNotIn("kis_token", limiter.request_kinds)
        self.assertEqual(0, limiter.token_issue_count)
        self.assertEqual(0, cache.invalidations)

    def test_live_order_client_never_refreshes_or_retries_expired_cancel_post(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-cancel"})
        transport.push(KisApiError('KIS HTTP 500: {"msg_cd":"EGW00123","msg1":"expired"}'))
        limiter = ImmediateRecordingRateLimiter()
        cache = MemoryTokenCache("stale-token", datetime(2026, 7, 24, 9, 0, 0))
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="stale-token",
            allow_order_placement=True,
            token_cache=cache,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisOrderSubmissionUncertain, "cancel submission uncertain"):
            client.cancel_cash_order(
                order_no="0000012345",
                order_org_no="12345",
                quantity=1,
                order_price=Decimal("70000"),
            )

        self.assertEqual(
            ["/uapi/hashkey", "/uapi/domestic-stock/v1/trading/order-rvsecncl"],
            [call.path for call in transport.calls],
        )
        self.assertNotIn("kis_token", limiter.request_kinds)
        self.assertEqual(0, limiter.token_issue_count)
        self.assertEqual(0, cache.invalidations)

    def test_live_order_client_does_not_mark_pre_post_rate_limit_as_uncertain(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-buy"})
        limiter = SequenceRateLimiter(
            [
                RateLimitDecision(True, 0.0, "allowed"),
                RateLimitDecision(False, 2.0, "api_backoff"),
            ]
        )
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
            rate_limiter=limiter,
        )

        with self.assertRaisesRegex(KisApiError, "local rate limit"):
            client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))

        self.assertEqual(["/uapi/hashkey"], [call.path for call in transport.calls])
        self.assertEqual(["kis_live_mutation", "kis_live_mutation"], limiter.allow_calls)
        self.assertEqual(["kis_live_mutation"], limiter.recorded_requests)

    def test_live_order_client_marks_malformed_order_post_response_as_uncertain(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-buy"})
        transport.push([])
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
        )

        with self.assertRaisesRegex(KisOrderSubmissionUncertain, "malformed response type list"):
            client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))

        self.assertEqual(
            ["/uapi/hashkey", "/uapi/domestic-stock/v1/trading/order-cash"],
            [call.path for call in transport.calls],
        )

    def test_live_order_client_preserves_explicit_kis_order_rejection_as_api_error(self):
        transport = FakeTransport()
        transport.push({"HASH": "hash-buy"})
        transport.push({"rt_cd": "1", "msg_cd": "EGW00001", "msg1": "주문 거절"})
        client = KisLiveOrderClient(
            credentials(),
            transport=transport,
            access_token="token",
            allow_order_placement=True,
        )

        with self.assertRaisesRegex(KisApiError, "EGW00001"):
            client.place_cash_order(Order.buy("005930", 2, "entry"), order_price=Decimal("70000"))


if __name__ == "__main__":
    unittest.main()
