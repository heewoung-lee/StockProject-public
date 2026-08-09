from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock


DEFAULT_KIS_REST_MIN_INTERVAL_SECONDS = 1.25
LIVE_KIS_REST_MIN_INTERVAL_SECONDS = 0.15


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after_seconds: float
    reason: str


class KisRateLimiter:
    def __init__(
        self,
        min_interval_seconds: float = DEFAULT_KIS_REST_MIN_INTERVAL_SECONDS,
        token_cooldown_seconds: float = 61.0,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.min_interval_seconds = float(min_interval_seconds)
        self.token_cooldown_seconds = float(token_cooldown_seconds)
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or time.sleep
        self._lock = Lock()
        self._request_lock = Lock()
        self._last_request_at: float | None = None
        self._last_request_kind: str | None = None
        self._token_cooldown_until: float | None = None
        self._api_backoff_until: float | None = None

    def allow_request(self, kind: str = "query") -> RateLimitDecision:
        with self._lock:
            return self._allow_request(kind, now=self._clock())

    def acquire_request(self, kind: str = "query") -> RateLimitDecision:
        """Atomically wait for pacing or return an active cooldown blocker."""
        while True:
            with self._lock:
                now = self._clock()
                blockers = self._active_blockers(kind, now=now)
                cooldown_blockers = [
                    decision for decision in blockers if decision.reason != "min_interval"
                ]
                if cooldown_blockers:
                    return max(cooldown_blockers, key=lambda decision: decision.retry_after_seconds)
                if not blockers:
                    decision = RateLimitDecision(True, 0.0, "allowed")
                    self._record_request(kind, now=now)
                    return decision
                retry_after = blockers[0].retry_after_seconds

            self._sleeper(retry_after)

    def acquire_retry_request(
        self,
        kind: str = "query",
        *,
        max_wait_seconds: float = 3.0,
    ) -> RateLimitDecision:
        """Wait through a short API backoff for one explicitly safe retry."""
        deadline = self._clock() + max(0.0, float(max_wait_seconds))
        while True:
            with self._lock:
                now = self._clock()
                blockers = self._active_blockers(kind, now=now)
                token_blockers = [
                    decision for decision in blockers if decision.reason == "token_cooldown"
                ]
                if token_blockers:
                    return max(token_blockers, key=lambda decision: decision.retry_after_seconds)
                if not blockers:
                    decision = RateLimitDecision(True, 0.0, "allowed")
                    self._record_request(kind, now=now)
                    return decision
                blocker = max(blockers, key=lambda decision: decision.retry_after_seconds)
                if now + blocker.retry_after_seconds > deadline:
                    return blocker

            self._sleeper(blocker.retry_after_seconds)

    def run_request(
        self,
        kind: str,
        operation: Callable[[], object],
        *,
        retry: bool = False,
        wait_for_api_backoff: bool = False,
        max_wait_seconds: float = 3.0,
    ) -> tuple[RateLimitDecision, object | None]:
        """Pace and execute one transport call without reservation reordering."""
        with self._request_lock:
            decision = (
                self.acquire_retry_request(kind, max_wait_seconds=max_wait_seconds)
                if retry or wait_for_api_backoff
                else self.acquire_request(kind)
            )
            if not decision.allowed:
                return decision, None
            return decision, operation()

    def record_request(self, kind: str = "query") -> None:
        with self._lock:
            self._record_request(kind, now=self._clock())

    def record_token_issue(self) -> None:
        with self._lock:
            self._token_cooldown_until = self._clock() + self.token_cooldown_seconds

    def record_rate_limit_error(self, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            retry_after = self.min_interval_seconds if retry_after_seconds is None else float(retry_after_seconds)
            backoff_until = self._clock() + max(0.0, retry_after)
            self._api_backoff_until = max(self._api_backoff_until or backoff_until, backoff_until)

    def diagnostic_snapshot(self, kind: str = "kis_live_api") -> dict[str, object]:
        """Return side-effect-free limiter state for redacted diagnostics."""
        with self._lock:
            now = self._clock()
            decision = self._allow_request(kind, now=now)
            spacing_until = (
                None
                if self._last_request_at is None
                else self._last_request_at + self.min_interval_seconds
            )
            return {
                "allowed": decision.allowed,
                "retryAfterSeconds": decision.retry_after_seconds,
                "reason": decision.reason,
                "requestSpacingRemainingSeconds": self._remaining(now, spacing_until),
                "tokenCooldownRemainingSeconds": self._remaining(now, self._token_cooldown_until),
                "apiBackoffRemainingSeconds": self._remaining(now, self._api_backoff_until),
                "lastRequestKind": self._last_request_kind or "",
                "minIntervalSeconds": self.min_interval_seconds,
            }

    def _allow_request(self, kind: str, *, now: float) -> RateLimitDecision:
        blockers = self._active_blockers(kind, now=now)
        if blockers:
            return max(blockers, key=lambda decision: decision.retry_after_seconds)

        return RateLimitDecision(allowed=True, retry_after_seconds=0.0, reason="allowed")

    def _active_blockers(self, kind: str, *, now: float) -> list[RateLimitDecision]:
        blockers: list[RateLimitDecision] = []

        if self._last_request_at is not None:
            retry_after = self._remaining(now, self._last_request_at + self.min_interval_seconds)
            if retry_after > 0.0:
                blockers.append(RateLimitDecision(False, retry_after, "min_interval"))

        if kind not in {"market_data_cycle", "kis_quote", "kis_live_read", "kis_live_mutation"}:
            retry_after = self._remaining(now, self._token_cooldown_until)
            if retry_after > 0.0:
                blockers.append(RateLimitDecision(False, retry_after, "token_cooldown"))

        retry_after = self._remaining(now, self._api_backoff_until)
        if retry_after > 0.0:
            blockers.append(RateLimitDecision(False, retry_after, "api_backoff"))

        return blockers

    def _record_request(self, kind: str, *, now: float) -> None:
        self._last_request_kind = kind
        self._last_request_at = now

    @staticmethod
    def _remaining(now: float, until: float | None) -> float:
        if until is None:
            return 0.0
        return max(0.0, until - now)
