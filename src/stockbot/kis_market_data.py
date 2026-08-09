from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Protocol

from .kis import KisApiError, KisCredentials, KisVtsClient
from .kis_smoke import load_kis_vts_credentials
from .models import MarketBar


class KisMarketDataRateLimitError(RuntimeError):
    def __init__(self, kind: str, retry_after_seconds: float, reason: str):
        self.kind = kind
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason
        super().__init__(f"KIS market data rate limited: {kind} {reason} retry_after={retry_after_seconds:.1f}s")


class KisPriceClient(Protocol):
    def issue_access_token(self) -> str:
        ...

    def set_access_token(self, token: str, *, expires_at: datetime | None = None) -> None:
        ...

    def access_token_expires_at(self) -> datetime | None:
        ...

    def price_bar(self, symbol: str) -> MarketBar:
        ...


class KisMarketDataRateLimiter(Protocol):
    def allow_request(self, kind: str = "query"):
        ...

    def record_request(self, kind: str = "query") -> None:
        ...

    def record_token_issue(self) -> None:
        ...

    def record_rate_limit_error(self, retry_after_seconds: float | None = None) -> None:
        ...


KisClientFactory = Callable[[KisCredentials], KisPriceClient]


@dataclass(frozen=True)
class CachedKisToken:
    access_token: str = field(repr=False)
    expires_at: datetime


class KisTokenFileCache:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        clock: Callable[[], object] | None = None,
        expiry_margin_seconds: float = 60.0,
        namespace: str = "kis-vts",
    ):
        self.path = Path(path) if path is not None else self.default_path()
        self.clock = clock or datetime.now
        self.expiry_margin = timedelta(seconds=float(expiry_margin_seconds))
        self.namespace = str(namespace or "kis-vts")
        self._memory_tokens: dict[str, CachedKisToken] = {}

    @staticmethod
    def default_path() -> Path:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return base / "StockBot" / "kis-token-cache.json"

    def read(self, credentials: KisCredentials) -> CachedKisToken | None:
        cache_key = self._cache_key(credentials)
        try:
            entry = self._read_payload().get("tokens", {}).get(cache_key, {})
            token = str(entry.get("access_token", "")).strip()
            expires_at = _parse_cache_datetime(entry.get("expires_at"))
        except Exception:
            return self._read_memory_token(cache_key)
        cached = self._valid_token_or_none(token, expires_at)
        return cached or self._read_memory_token(cache_key)

    def write(self, credentials: KisCredentials, access_token: str, expires_at: datetime | None) -> None:
        token = str(access_token or "").strip()
        if not token:
            return
        expires = expires_at or (self._now() + timedelta(hours=23))
        cache_key = self._cache_key(credentials)
        self._memory_tokens[cache_key] = CachedKisToken(token, expires)
        try:
            payload = self._read_payload()
            tokens = payload.setdefault("tokens", {})
            tokens[cache_key] = {
                "access_token": token,
                "expires_at": expires.isoformat(sep=" ", timespec="seconds"),
                "saved_at": self._now().isoformat(sep=" ", timespec="seconds"),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
        except (OSError, ValueError):
            return

    def invalidate(self, credentials: KisCredentials) -> None:
        cache_key = self._cache_key(credentials)
        self._memory_tokens.pop(cache_key, None)
        try:
            payload = self._read_payload()
            tokens = payload.get("tokens", {})
            if not isinstance(tokens, dict):
                return
            if cache_key not in tokens:
                return
            tokens.pop(cache_key, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
        except (OSError, ValueError):
            return

    def _read_memory_token(self, cache_key: str) -> CachedKisToken | None:
        cached = self._memory_tokens.get(cache_key)
        if cached is None:
            return None
        return self._valid_token_or_none(cached.access_token, cached.expires_at)

    def _valid_token_or_none(self, token: object, expires_at: datetime | None) -> CachedKisToken | None:
        normalized = str(token or "").strip()
        if not normalized or expires_at is None:
            return None
        if expires_at <= self._now() + self.expiry_margin:
            return None
        return CachedKisToken(normalized, expires_at)

    def _read_payload(self) -> dict:
        if not self.path.exists():
            return {"tokens": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {"tokens": {}}

    def _now(self) -> datetime:
        value = self.clock()
        if isinstance(value, datetime):
            return value
        return datetime.fromtimestamp(float(value))

    def _cache_key(self, credentials: KisCredentials) -> str:
        raw = "|".join(
            [
                self.namespace,
                credentials.app_key,
                credentials.app_secret,
                credentials.account_no,
                credentials.account_product_code,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class KisPriceBarProvider:
    data_source_label = "KIS VTS 현재가 / paper 체결"

    def __init__(
        self,
        *,
        env_file: str | Path = ".env",
        env: Mapping[str, str] | None = None,
        client_factory: KisClientFactory | None = None,
        rate_limiter: KisMarketDataRateLimiter | None = None,
        token_cache: KisTokenFileCache | None = None,
        timeout: float = 20.0,
        max_rate_limit_wait_seconds: float = 2.0,
        per_second_retry_delay: float = 1.5,
        token_retry_delay: float = 61.0,
        retry_quote_timeouts: bool = False,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.env_file = env_file
        self.env = env
        self.timeout = timeout
        self.client_factory = client_factory or self._default_client_factory
        if token_cache is not None:
            self.token_cache = token_cache
        elif client_factory is None:
            self.token_cache = KisTokenFileCache()
        else:
            self.token_cache = None
        self.rate_limiter = rate_limiter
        self.max_rate_limit_wait_seconds = float(max_rate_limit_wait_seconds)
        self.per_second_retry_delay = float(per_second_retry_delay)
        self.token_retry_delay = float(token_retry_delay)
        self.retry_quote_timeouts = bool(retry_quote_timeouts)
        self.sleeper = sleeper or time.sleep
        self._client: KisPriceClient | None = None
        self._credentials: KisCredentials | None = None
        self._latest_volume: dict[str, int] = {}
        self._latest_accumulated_volume: dict[str, int] = {}

    def __call__(self, symbol: str) -> MarketBar | None:
        client = self._client_or_create()
        self._require_allowed("kis_quote", record_request=True)
        try:
            bar = client.price_bar(symbol)
        except KisApiError as exc:
            if _is_expired_token_error(exc):
                bar = self._retry_after_expired_token(symbol)
                return self._with_interval_volume(symbol, bar)
            if _is_network_timeout(exc) and not self.retry_quote_timeouts:
                return None
            if not _is_retryable_quote_error(exc):
                raise
            self._record_rate_limit_error(self.per_second_retry_delay)
            self.sleeper(max(0.0, self.per_second_retry_delay))
            self._require_allowed("kis_quote", record_request=True)
            bar = client.price_bar(symbol)
        return self._with_interval_volume(symbol, bar)

    def priority(self, symbol: str) -> float:
        return float(self._latest_volume.get(symbol, 0))

    def _with_interval_volume(self, symbol: str, bar: MarketBar) -> MarketBar:
        accumulated_volume = max(0, int(bar.volume))
        previous = self._latest_accumulated_volume.get(symbol)
        self._latest_accumulated_volume[symbol] = accumulated_volume
        self._latest_volume[symbol] = accumulated_volume

        if previous is None or accumulated_volume < previous:
            interval_volume = 0
        else:
            interval_volume = accumulated_volume - previous
        return replace(bar, volume=interval_volume)

    def _client_or_create(self) -> KisPriceClient:
        if self._client is not None:
            return self._client
        credentials = load_kis_vts_credentials(self.env_file, self.env)
        self._credentials = credentials
        client = self.client_factory(credentials)
        cached_token = self._read_cached_token(credentials)
        if cached_token is not None:
            self._apply_access_token(client, cached_token)
            self._client = client
            return client

        self._issue_and_cache_token(credentials, client)
        self._client = client
        return client

    def _retry_after_expired_token(self, symbol: str) -> MarketBar:
        credentials = self._credentials or load_kis_vts_credentials(self.env_file, self.env)
        self._credentials = credentials
        self._client = None
        self._invalidate_cached_token(credentials)
        client = self.client_factory(credentials)
        self._issue_and_cache_token(credentials, client)
        self._client = client
        self._require_allowed("kis_quote", record_request=True)
        try:
            return client.price_bar(symbol)
        except KisApiError as exc:
            if _is_expired_token_error(exc):
                self._invalidate_cached_token(credentials)
                self._client = None
            raise

    def _issue_and_cache_token(self, credentials: KisCredentials, client: KisPriceClient) -> None:
        self._require_allowed("kis_token", record_request=True)
        try:
            access_token = client.issue_access_token()
        except KisApiError as exc:
            if not _is_token_rate_limit(exc):
                raise
            self._record_token_issue()
            self._record_rate_limit_error(self.token_retry_delay)
            raise KisMarketDataRateLimitError("kis_token", self.token_retry_delay, "token_cooldown") from exc
        self._record_token_issue()
        self._write_cached_token(credentials, client, access_token)

    def _default_client_factory(self, credentials: KisCredentials) -> KisPriceClient:
        return KisVtsClient(credentials, timeout=self.timeout, allow_order_placement=False)

    def _require_allowed(self, kind: str, *, record_request: bool) -> None:
        if self.rate_limiter is None:
            return
        decision = self.rate_limiter.allow_request(kind)
        if (
            decision is not None
            and not decision.allowed
            and decision.retry_after_seconds <= self.max_rate_limit_wait_seconds
        ):
            self.sleeper(max(0.0, decision.retry_after_seconds))
            decision = self.rate_limiter.allow_request(kind)
        if decision is not None and not decision.allowed:
            raise KisMarketDataRateLimitError(kind, decision.retry_after_seconds, decision.reason)
        if record_request:
            self.rate_limiter.record_request(kind)

    def _record_token_issue(self) -> None:
        if self.rate_limiter is not None:
            self.rate_limiter.record_token_issue()

    def _record_rate_limit_error(self, retry_after_seconds: float | None = None) -> None:
        recorder = getattr(self.rate_limiter, "record_rate_limit_error", None)
        if callable(recorder):
            recorder(retry_after_seconds)

    def _read_cached_token(self, credentials: KisCredentials) -> CachedKisToken | None:
        if self.token_cache is None:
            return None
        return self.token_cache.read(credentials)

    def _write_cached_token(self, credentials: KisCredentials, client: KisPriceClient, access_token: str) -> None:
        if self.token_cache is None:
            return
        self.token_cache.write(credentials, access_token, _client_token_expiry(client))

    def _invalidate_cached_token(self, credentials: KisCredentials) -> None:
        if self.token_cache is None:
            return
        invalidator = getattr(self.token_cache, "invalidate", None)
        if callable(invalidator):
            invalidator(credentials)

    @staticmethod
    def _apply_access_token(client: KisPriceClient, cached_token: CachedKisToken) -> None:
        setter = getattr(client, "set_access_token", None)
        if callable(setter):
            setter(cached_token.access_token, expires_at=cached_token.expires_at)
            return
        if hasattr(client, "_access_token"):
            setattr(client, "_access_token", cached_token.access_token)
        if hasattr(client, "_access_token_expires_at"):
            setattr(client, "_access_token_expires_at", cached_token.expires_at)


def _is_per_second_rate_limit(exc: KisApiError) -> bool:
    message = str(exc)
    if "EGW00215" in message:
        return True
    return "EGW00201" in message or "초당 거래건수" in message


def _is_retryable_quote_error(exc: KisApiError) -> bool:
    return _is_per_second_rate_limit(exc) or _is_network_timeout(exc)


def _is_expired_token_error(exc: KisApiError) -> bool:
    message = str(exc)
    return "EGW00123" in message or "기간이 만료된" in message


def _is_network_timeout(exc: KisApiError) -> bool:
    message = str(exc).lower()
    return "timed out" in message or "timeout" in message


def _is_token_rate_limit(exc: KisApiError) -> bool:
    message = str(exc)
    return "EGW00133" in message or "1분당 1회" in message or "접근토큰" in message


def _client_token_expiry(client: KisPriceClient) -> datetime | None:
    getter = getattr(client, "access_token_expires_at", None)
    if not callable(getter):
        return None
    value = getter()
    if isinstance(value, datetime):
        return value
    return _parse_cache_datetime(value)


def _parse_cache_datetime(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for parser in (datetime.fromisoformat, lambda raw: datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")):
        try:
            return parser(text)
        except ValueError:
            continue
    return None
