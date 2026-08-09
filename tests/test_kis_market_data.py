from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.kis import KisCredentials
from stockbot.kis import KisApiError
from stockbot.kis_market_data import KisMarketDataRateLimitError, KisPriceBarProvider, KisTokenFileCache
from stockbot.models import MarketBar
from stockbot.rate_limit import KisRateLimiter, RateLimitDecision


class FakeKisClient:
    def __init__(self):
        self.token_issues = 0
        self.price_symbols = []
        self.order_calls = 0
        self.applied_tokens = []
        self.expires_at = datetime(2026, 6, 16, 9, 0, 0)

    def issue_access_token(self):
        self.token_issues += 1
        return "token"

    def set_access_token(self, token, *, expires_at=None):
        self.applied_tokens.append((token, expires_at))

    def access_token_expires_at(self):
        return self.expires_at

    def price_bar(self, symbol):
        self.price_symbols.append(symbol)
        close = Decimal("70000") if symbol == "005930" else Decimal("120000")
        volume = 1000 if symbol == "005930" else 9000
        return MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 15, 9, 1),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            vwap=close,
        )

    def place_cash_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("KIS market data provider must not place orders")


class AccumulatedVolumeKisClient(FakeKisClient):
    def __init__(self, volumes):
        super().__init__()
        self.volumes = list(volumes)

    def price_bar(self, symbol):
        self.price_symbols.append(symbol)
        close = Decimal("70000")
        volume = self.volumes.pop(0)
        return MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 15, 9, 1),
            open=close,
            high=close,
            low=close,
            close=close,
            volume=volume,
            vwap=close,
        )


class OneTimeRateLimitedKisClient(FakeKisClient):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    def price_bar(self, symbol):
        if not self.failed_once:
            self.failed_once = True
            raise KisApiError('KIS HTTP 500: {"msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}')
        return super().price_bar(symbol)


class OneTimeLedgerRateLimitedKisClient(FakeKisClient):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    def price_bar(self, symbol):
        if not self.failed_once:
            self.failed_once = True
            raise KisApiError('KIS HTTP 500: {"rt_cd":"1","msg_cd":"EGW00215","msg1":"ledger request limit"}')
        return super().price_bar(symbol)


class AlwaysRateLimitedKisClient(FakeKisClient):
    def price_bar(self, symbol):
        raise KisApiError('KIS HTTP 500: {"msg_cd":"EGW00201","msg1":"초당 거래건수를 초과하였습니다."}')


class OneTimeExpiredTokenKisClient(FakeKisClient):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    def issue_access_token(self):
        self.token_issues += 1
        return "fresh-token"

    def price_bar(self, symbol):
        if not self.failed_once:
            self.failed_once = True
            self.price_symbols.append(symbol)
            raise KisApiError('KIS HTTP 500: {"rt_cd":"1","msg1":"기간이 만료된 token 입니다.","msg_cd":"EGW00123"}')
        return super().price_bar(symbol)


class AlwaysExpiredTokenKisClient(FakeKisClient):
    def issue_access_token(self):
        self.token_issues += 1
        return "fresh-token"

    def price_bar(self, symbol):
        self.price_symbols.append(symbol)
        raise KisApiError('KIS HTTP 500: {"rt_cd":"1","msg1":"기간이 만료된 token 입니다.","msg_cd":"EGW00123"}')


class NonRateLimitedKisErrorClient(FakeKisClient):
    def price_bar(self, symbol):
        raise KisApiError('KIS HTTP 500: {"msg_cd":"APBK0001","msg1":"other server error"}')


class OneTimeTimeoutKisClient(FakeKisClient):
    def __init__(self):
        super().__init__()
        self.failed_once = False

    def price_bar(self, symbol):
        if not self.failed_once:
            self.failed_once = True
            self.price_symbols.append(symbol)
            raise KisApiError("KIS network timeout: The read operation timed out")
        return super().price_bar(symbol)


class AlwaysTimeoutKisClient(FakeKisClient):
    def price_bar(self, symbol):
        self.price_symbols.append(symbol)
        raise KisApiError("KIS network timeout: The read operation timed out")


class TokenRateLimitedKisClient(FakeKisClient):
    def issue_access_token(self):
        self.token_issues += 1
        raise KisApiError(
            'KIS HTTP 403: {"error_code":"EGW00133","error_description":"접근토큰 발급 잠시 후 다시 시도하세요(1분당 1회)"}'
        )


class ExpiredThenTokenRateLimitedKisClient(TokenRateLimitedKisClient):
    def price_bar(self, symbol):
        self.price_symbols.append(symbol)
        raise KisApiError('KIS HTTP 500: {"rt_cd":"1","msg1":"기간이 만료된 token 입니다.","msg_cd":"EGW00123"}')


class RecordingRateLimiter:
    def __init__(self, blocked_kind: str = ""):
        self.blocked_kind = blocked_kind
        self.allowed_checks = []
        self.recorded_requests = []
        self.rate_limit_errors = []
        self.token_issues = 0

    def allow_request(self, kind="query"):
        self.allowed_checks.append(kind)
        if kind == self.blocked_kind:
            return RateLimitDecision(False, 1.5, "min_interval")
        return RateLimitDecision(True, 0.0, "allowed")

    def record_request(self, kind="query"):
        self.recorded_requests.append(kind)

    def record_token_issue(self):
        self.token_issues += 1

    def record_rate_limit_error(self, retry_after_seconds=None):
        self.rate_limit_errors.append(retry_after_seconds)


class FakeClock:
    def __init__(self, now: float | datetime = 100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class KisPriceBarProviderTest(unittest.TestCase):
    def test_provider_issues_token_once_and_reads_price_bars_only(self):
        fake_client = FakeKisClient()
        seen_credentials = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda credentials: seen_credentials.append(credentials) or fake_client,
            )

            first = provider("005930")
            second = provider("000660")

        self.assertIsInstance(seen_credentials[0], KisCredentials)
        self.assertEqual("005930", first.symbol)
        self.assertEqual("000660", second.symbol)
        self.assertEqual(1, fake_client.token_issues)
        self.assertEqual(["005930", "000660"], fake_client.price_symbols)
        self.assertEqual(0, fake_client.order_calls)
        self.assertEqual(9000.0, provider.priority("000660"))
        self.assertEqual(1000.0, provider.priority("005930"))

    def test_provider_converts_accumulated_volume_to_quote_interval_volume(self):
        fake_client = AccumulatedVolumeKisClient([1000, 1250, 1800])

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
            )

            first = provider("005930")
            second = provider("005930")
            third = provider("005930")

        self.assertEqual(0, first.volume)
        self.assertEqual(250, second.volume)
        self.assertEqual(550, third.volume)
        self.assertEqual(1800.0, provider.priority("005930"))

    def test_provider_tracks_accumulated_volume_baselines_per_symbol(self):
        fake_client = AccumulatedVolumeKisClient([1000, 5000, 1400, 5600])

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
            )

            samsung_first = provider("005930")
            hynix_first = provider("000660")
            samsung_second = provider("005930")
            hynix_second = provider("000660")

        self.assertEqual(0, samsung_first.volume)
        self.assertEqual(0, hynix_first.volume)
        self.assertEqual(400, samsung_second.volume)
        self.assertEqual(600, hynix_second.volume)

    def test_provider_clamps_interval_volume_when_accumulated_volume_resets(self):
        fake_client = AccumulatedVolumeKisClient([1000, 1250, 50, 200])

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
            )

            provider("005930")
            second = provider("005930")
            reset = provider("005930")
            after_reset = provider("005930")

        self.assertEqual(250, second.volume)
        self.assertEqual(0, reset.volume)
        self.assertEqual(150, after_reset.volume)

    def test_provider_records_token_and_each_price_query_for_rate_limiting(self):
        fake_client = FakeKisClient()
        limiter = RecordingRateLimiter()

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
            )

            provider("005930")
            provider("000660")

        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.allowed_checks)
        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(1, limiter.token_issues)
        self.assertEqual(["005930", "000660"], fake_client.price_symbols)

    def test_provider_reuses_valid_file_token_without_issuing_new_token(self):
        fake_client = FakeKisClient()
        limiter = RecordingRateLimiter()
        now = datetime(2026, 6, 15, 14, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            cache_path = Path(tmp) / "kis-token-cache.json"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            credentials = KisCredentials(
                app_key="key",
                app_secret="secret",
                account_no="12345678",
                account_product_code="01",
            )
            token_cache = KisTokenFileCache(cache_path, clock=lambda: now)
            token_cache.write(credentials, "cached-token", datetime(2026, 6, 16, 9, 0, 0))

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                token_cache=token_cache,
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual(0, fake_client.token_issues)
        self.assertEqual([("cached-token", datetime(2026, 6, 16, 9, 0, 0))], fake_client.applied_tokens)
        self.assertEqual(["kis_quote"], limiter.allowed_checks)
        self.assertEqual(["kis_quote"], limiter.recorded_requests)
        self.assertEqual(0, limiter.token_issues)

    def test_token_cache_does_not_reuse_token_when_app_secret_changes(self):
        now = datetime(2026, 6, 15, 14, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            token_cache = KisTokenFileCache(Path(tmp) / "kis-token-cache.json", clock=lambda: now)
            token_cache.write(
                KisCredentials(
                    app_key="key",
                    app_secret="old-secret",
                    account_no="12345678",
                    account_product_code="01",
                ),
                "old-secret-token",
                datetime(2026, 6, 16, 9, 0, 0),
            )

            cached = token_cache.read(
                KisCredentials(
                    app_key="key",
                    app_secret="new-secret",
                    account_no="12345678",
                    account_product_code="01",
                )
            )

        self.assertIsNone(cached)

    def test_provider_persists_new_token_for_next_provider_instance(self):
        first_client = FakeKisClient()
        second_client = FakeKisClient()
        now = datetime(2026, 6, 15, 14, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            cache_path = Path(tmp) / "kis-token-cache.json"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            token_cache = KisTokenFileCache(cache_path, clock=lambda: now)

            first_provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: first_client,
                token_cache=token_cache,
            )
            first_provider("005930")

            second_provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: second_client,
                token_cache=token_cache,
            )
            second_provider("000660")

        self.assertEqual(1, first_client.token_issues)
        self.assertEqual(0, second_client.token_issues)
        self.assertEqual([("token", datetime(2026, 6, 16, 9, 0, 0))], second_client.applied_tokens)
        self.assertEqual(["000660"], second_client.price_symbols)

    def test_provider_waits_for_short_min_interval_between_token_and_first_quote(self):
        fake_client = FakeKisClient()
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.05, token_cooldown_seconds=61.0, clock=clock)
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds) or clock.advance(seconds),
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual(1, len(waits))
        self.assertAlmostEqual(1.05, waits[0])
        self.assertEqual(["005930"], fake_client.price_symbols)

    def test_provider_does_not_call_price_api_when_quote_rate_limited(self):
        fake_client = FakeKisClient()
        limiter = RecordingRateLimiter(blocked_kind="kis_quote")

        def long_retry_after(kind="query"):
            limiter.allowed_checks.append(kind)
            if kind == "kis_quote":
                return RateLimitDecision(False, 99.0, "api_backoff")
            return RateLimitDecision(True, 0.0, "allowed")

        limiter.allow_request = long_retry_after

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
            )

            with self.assertRaises(KisMarketDataRateLimitError):
                provider("005930")

        self.assertEqual(["kis_token", "kis_quote"], limiter.allowed_checks)
        self.assertEqual(["kis_token"], limiter.recorded_requests)
        self.assertEqual(1, limiter.token_issues)
        self.assertEqual([], fake_client.price_symbols)

    def test_provider_retries_once_after_kis_per_second_rate_limit(self):
        fake_client = OneTimeRateLimitedKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual([1.5], waits)
        self.assertEqual([1.5], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(["005930"], fake_client.price_symbols)

    def test_provider_retries_once_after_kis_ledger_per_second_rate_limit(self):
        fake_client = OneTimeLedgerRateLimitedKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual([1.5], waits)
        self.assertEqual([1.5], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(["005930"], fake_client.price_symbols)

    def test_provider_refreshes_cached_token_once_after_expired_token_error(self):
        fake_client = OneTimeExpiredTokenKisClient()
        limiter = RecordingRateLimiter()
        now = datetime(2026, 6, 15, 14, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            cache_path = Path(tmp) / "kis-token-cache.json"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            credentials = KisCredentials(
                app_key="key",
                app_secret="secret",
                account_no="12345678",
                account_product_code="01",
            )
            token_cache = KisTokenFileCache(cache_path, clock=lambda: now)
            token_cache.write(credentials, "cached-token", datetime(2026, 6, 16, 9, 0, 0))

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                token_cache=token_cache,
            )

            bar = provider("005930")
            cached_token = token_cache.read(credentials)

        self.assertEqual("005930", bar.symbol)
        self.assertEqual(1, fake_client.token_issues)
        self.assertEqual(["005930", "005930"], fake_client.price_symbols)
        self.assertEqual(["kis_quote", "kis_token", "kis_quote"], limiter.recorded_requests)
        self.assertEqual("fresh-token", cached_token.access_token)

    def test_provider_does_not_loop_when_refreshed_token_is_also_expired(self):
        fake_client = AlwaysExpiredTokenKisClient()
        limiter = RecordingRateLimiter()
        now = datetime(2026, 6, 15, 14, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            cache_path = Path(tmp) / "kis-token-cache.json"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            credentials = KisCredentials(
                app_key="key",
                app_secret="secret",
                account_no="12345678",
                account_product_code="01",
            )
            token_cache = KisTokenFileCache(cache_path, clock=lambda: now)
            token_cache.write(credentials, "cached-token", datetime(2026, 6, 16, 9, 0, 0))

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                token_cache=token_cache,
            )

            with self.assertRaisesRegex(KisApiError, "EGW00123"):
                provider("005930")

            cached_token = token_cache.read(credentials)

        self.assertEqual(1, fake_client.token_issues)
        self.assertEqual(["005930", "005930"], fake_client.price_symbols)
        self.assertEqual(["kis_quote", "kis_token", "kis_quote"], limiter.recorded_requests)
        self.assertIsNone(cached_token)

    def test_provider_does_not_loop_when_kis_per_second_rate_limit_persists(self):
        fake_client = AlwaysRateLimitedKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            with self.assertRaises(KisApiError):
                provider("005930")

        self.assertEqual([1.5], waits)
        self.assertEqual([1.5], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.recorded_requests)

    def test_provider_does_not_retry_non_per_second_kis_errors(self):
        fake_client = NonRateLimitedKisErrorClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            with self.assertRaises(KisApiError):
                provider("005930")

        self.assertEqual([], waits)
        self.assertEqual([], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote"], limiter.recorded_requests)
        self.assertEqual([], fake_client.price_symbols)

    def test_provider_retries_once_after_kis_read_timeout(self):
        fake_client = OneTimeTimeoutKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
                retry_quote_timeouts=True,
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual([1.5], waits)
        self.assertEqual([1.5], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(["005930", "005930"], fake_client.price_symbols)

    def test_provider_returns_none_when_kis_read_timeout_persists(self):
        fake_client = AlwaysTimeoutKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            bar = provider("005930")

        self.assertIsNone(bar)
        self.assertEqual([], waits)
        self.assertEqual([], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(["005930"], fake_client.price_symbols)

    def test_provider_soft_fails_kis_read_timeout_without_retry_by_default(self):
        fake_client = OneTimeTimeoutKisClient()
        limiter = RecordingRateLimiter()
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds),
                per_second_retry_delay=1.5,
            )

            bar = provider("005930")

        self.assertIsNone(bar)
        self.assertEqual([], waits)
        self.assertEqual([], limiter.rate_limit_errors)
        self.assertEqual(["kis_token", "kis_quote"], limiter.recorded_requests)
        self.assertEqual(["005930"], fake_client.price_symbols)

    def test_provider_turns_token_rate_limit_into_local_cooldown(self):
        fake_client = TokenRateLimitedKisClient()
        limiter = RecordingRateLimiter()

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
            )

            with self.assertRaises(KisMarketDataRateLimitError):
                provider("005930")

        self.assertEqual(1, fake_client.token_issues)
        self.assertEqual(1, limiter.token_issues)
        self.assertEqual(["kis_token"], limiter.recorded_requests)

    def test_provider_clears_expired_client_when_token_refresh_hits_cooldown(self):
        fake_client = ExpiredThenTokenRateLimitedKisClient()
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.25, token_cooldown_seconds=61.0, clock=clock)
        waits = []
        now = datetime(2026, 6, 15, 14, 0, 0)

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            cache_path = Path(tmp) / "kis-token-cache.json"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            credentials = KisCredentials(
                app_key="key",
                app_secret="secret",
                account_no="12345678",
                account_product_code="01",
            )
            token_cache = KisTokenFileCache(cache_path, clock=lambda: now)
            token_cache.write(credentials, "cached-token", datetime(2026, 6, 16, 9, 0, 0))

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                token_cache=token_cache,
                sleeper=lambda seconds: waits.append(seconds) or clock.advance(seconds),
            )

            with self.assertRaises(KisMarketDataRateLimitError):
                provider("005930")

            with self.assertRaises(KisMarketDataRateLimitError):
                provider("000660")

            cached_token = token_cache.read(credentials)

        self.assertEqual(["005930"], fake_client.price_symbols)
        self.assertEqual(1, fake_client.token_issues)
        self.assertEqual([1.25], waits)
        self.assertIsNone(cached_token)

    def test_provider_retries_with_real_limiter_after_kis_per_second_rate_limit(self):
        fake_client = OneTimeRateLimitedKisClient()
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.25, token_cooldown_seconds=61.0, clock=clock)
        waits = []

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=key",
                        "KIS_VTS_APP_SECRET=secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            provider = KisPriceBarProvider(
                env_file=env_path,
                env={},
                client_factory=lambda _credentials: fake_client,
                rate_limiter=limiter,
                sleeper=lambda seconds: waits.append(seconds) or clock.advance(seconds),
                per_second_retry_delay=1.5,
            )

            bar = provider("005930")

        self.assertEqual("005930", bar.symbol)
        self.assertEqual([1.25, 1.5], waits)
        self.assertEqual(["005930"], fake_client.price_symbols)


if __name__ == "__main__":
    unittest.main()
