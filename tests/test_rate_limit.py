import unittest
from dataclasses import is_dataclass
from threading import Barrier, Lock, Thread

from stockbot.rate_limit import KisRateLimiter, RateLimitDecision


class FakeClock:
    def __init__(self, now: float = 100.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RateLimitDecisionTest(unittest.TestCase):
    def test_rate_limit_decision_is_dataclass_with_required_fields(self):
        decision = RateLimitDecision(allowed=False, retry_after_seconds=1.5, reason="min_interval")

        self.assertTrue(is_dataclass(decision))
        self.assertFalse(decision.allowed)
        self.assertEqual(1.5, decision.retry_after_seconds)
        self.assertEqual("min_interval", decision.reason)


class KisRateLimiterTest(unittest.TestCase):
    def test_default_interval_leaves_margin_for_kis_per_second_limits(self):
        limiter = KisRateLimiter(clock=FakeClock())

        self.assertEqual(1.25, limiter.min_interval_seconds)

    def test_short_live_interval_does_not_shorten_token_cooldown_or_api_backoff(self):
        clock = FakeClock()
        token_limiter = KisRateLimiter(
            min_interval_seconds=0.15,
            token_cooldown_seconds=61.0,
            clock=clock,
        )
        api_limiter = KisRateLimiter(min_interval_seconds=0.15, clock=clock)

        token_limiter.record_token_issue()
        api_limiter.record_rate_limit_error(retry_after_seconds=1.5)

        token_decision = token_limiter.allow_request("kis_token")
        api_decision = api_limiter.allow_request("kis_live_read")
        self.assertFalse(token_decision.allowed)
        self.assertEqual("token_cooldown", token_decision.reason)
        self.assertEqual(61.0, token_decision.retry_after_seconds)
        self.assertFalse(api_decision.allowed)
        self.assertEqual("api_backoff", api_decision.reason)
        self.assertEqual(1.5, api_decision.retry_after_seconds)

    def test_run_request_serializes_transport_start_times_across_threads(self):
        clock = FakeClock()
        start_gate = Barrier(3)
        observed_lock = Lock()
        transport_starts = []

        def sleeper(seconds: float) -> None:
            clock.advance(seconds)

        limiter = KisRateLimiter(
            min_interval_seconds=1.25,
            clock=clock,
            sleeper=sleeper,
        )

        def worker() -> None:
            start_gate.wait()

            def transport():
                with observed_lock:
                    transport_starts.append(clock())
                return "ok"

            decision, result = limiter.run_request("kis_live_api", transport)
            self.assertTrue(decision.allowed)
            self.assertEqual("ok", result)

        threads = [Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        start_gate.wait()
        for thread in threads:
            thread.join()

        self.assertEqual([100.0, 101.25], transport_starts)

    def test_run_request_can_wait_through_short_api_backoff_for_get(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(
            min_interval_seconds=1.25,
            clock=clock,
            sleeper=sleeper,
        )
        limiter.record_rate_limit_error(retry_after_seconds=0.8)

        decision, result = limiter.run_request(
            "kis_live_api",
            lambda: "ok",
            wait_for_api_backoff=True,
            max_wait_seconds=3.0,
        )

        self.assertTrue(decision.allowed)
        self.assertEqual("ok", result)
        self.assertEqual(1, len(sleep_calls))
        self.assertAlmostEqual(0.8, sleep_calls[0])

    def test_allows_first_query_when_no_cooldown_is_active(self):
        limiter = KisRateLimiter(clock=FakeClock())

        decision = limiter.allow_request()

        self.assertTrue(decision.allowed)
        self.assertEqual(0.0, decision.retry_after_seconds)
        self.assertEqual("allowed", decision.reason)

    def test_rejects_query_until_min_interval_after_recorded_request(self):
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.05, clock=clock)

        limiter.record_request()
        clock.advance(0.25)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertAlmostEqual(0.8, decision.retry_after_seconds)
        self.assertEqual("min_interval", decision.reason)

        clock.advance(0.8)
        self.assertTrue(limiter.allow_request().allowed)

    def test_acquire_request_waits_for_min_interval_then_records_admission(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(min_interval_seconds=1.25, clock=clock, sleeper=sleeper)

        first = limiter.acquire_request("kis_live_api")
        second = limiter.acquire_request("kis_live_api")

        self.assertTrue(first.allowed)
        self.assertTrue(second.allowed)
        self.assertEqual([1.25], sleep_calls)
        decision = limiter.allow_request("kis_live_api")
        self.assertFalse(decision.allowed)
        self.assertEqual("min_interval", decision.reason)
        self.assertAlmostEqual(1.25, decision.retry_after_seconds)

    def test_acquire_request_rechecks_after_wait_before_recording(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)
            limiter.record_rate_limit_error(retry_after_seconds=2.0)

        limiter = KisRateLimiter(min_interval_seconds=5.0, clock=clock, sleeper=sleeper)
        self.assertTrue(limiter.acquire_request("kis_live_api").allowed)

        decision = limiter.acquire_request("kis_live_api")

        self.assertFalse(decision.allowed)
        self.assertEqual("api_backoff", decision.reason)
        self.assertAlmostEqual(2.0, decision.retry_after_seconds)
        self.assertEqual([5.0], sleep_calls)
        clock.advance(2.0)
        self.assertTrue(limiter.allow_request("kis_live_api").allowed)

    def test_acquire_request_returns_api_backoff_without_sleeping_or_recording(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(
            min_interval_seconds=5.0,
            clock=clock,
            sleeper=sleeper,
        )
        self.assertTrue(limiter.acquire_request("kis_live_api").allowed)
        clock.advance(1.0)
        limiter.record_rate_limit_error(retry_after_seconds=2.0)

        decision = limiter.acquire_request("kis_live_api")

        self.assertFalse(decision.allowed)
        self.assertEqual("api_backoff", decision.reason)
        self.assertAlmostEqual(2.0, decision.retry_after_seconds)
        self.assertEqual([], sleep_calls)
        clock.advance(2.0)
        remaining = limiter.allow_request("kis_live_api")
        self.assertEqual("min_interval", remaining.reason)
        self.assertAlmostEqual(2.0, remaining.retry_after_seconds)

    def test_acquire_request_returns_token_cooldown_without_sleeping(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(
            min_interval_seconds=5.0,
            token_cooldown_seconds=2.0,
            clock=clock,
            sleeper=sleeper,
        )
        self.assertTrue(limiter.acquire_request("kis_live_api").allowed)
        clock.advance(1.0)
        limiter.record_token_issue()

        decision = limiter.acquire_request("kis_live_api")

        self.assertFalse(decision.allowed)
        self.assertEqual("token_cooldown", decision.reason)
        self.assertAlmostEqual(2.0, decision.retry_after_seconds)
        self.assertEqual([], sleep_calls)

    def test_acquire_retry_request_waits_through_short_api_backoff(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(min_interval_seconds=1.25, clock=clock, sleeper=sleeper)
        self.assertTrue(limiter.acquire_request("kis_live_api").allowed)
        limiter.record_rate_limit_error(retry_after_seconds=2.0)

        decision = limiter.acquire_retry_request("kis_live_api", max_wait_seconds=3.0)

        self.assertTrue(decision.allowed)
        self.assertEqual([2.0], sleep_calls)
        remaining = limiter.allow_request("kis_live_api")
        self.assertEqual("min_interval", remaining.reason)
        self.assertAlmostEqual(1.25, remaining.retry_after_seconds)

    def test_acquire_retry_request_does_not_wait_beyond_bound_or_token_cooldown(self):
        clock = FakeClock()
        sleep_calls = []

        def sleeper(seconds: float) -> None:
            sleep_calls.append(seconds)
            clock.advance(seconds)

        limiter = KisRateLimiter(
            min_interval_seconds=1.25,
            token_cooldown_seconds=10.0,
            clock=clock,
            sleeper=sleeper,
        )
        self.assertTrue(limiter.acquire_request("kis_live_api").allowed)
        limiter.record_rate_limit_error(retry_after_seconds=5.0)

        bounded = limiter.acquire_retry_request("kis_live_api", max_wait_seconds=3.0)
        limiter.record_token_issue()
        token_blocked = limiter.acquire_retry_request("kis_live_api", max_wait_seconds=30.0)

        self.assertFalse(bounded.allowed)
        self.assertEqual("api_backoff", bounded.reason)
        self.assertFalse(token_blocked.allowed)
        self.assertEqual("token_cooldown", token_blocked.reason)
        self.assertEqual([], sleep_calls)

    def test_token_issue_blocks_until_token_cooldown_expires(self):
        clock = FakeClock()
        limiter = KisRateLimiter(token_cooldown_seconds=61.0, clock=clock)

        limiter.record_token_issue()
        clock.advance(1.0)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertEqual(60.0, decision.retry_after_seconds)
        self.assertEqual("token_cooldown", decision.reason)

        clock.advance(60.0)
        self.assertTrue(limiter.allow_request().allowed)

    def test_token_issue_does_not_block_market_data_cycle_kind(self):
        clock = FakeClock()
        limiter = KisRateLimiter(token_cooldown_seconds=61.0, clock=clock)

        limiter.record_token_issue()
        decision = limiter.allow_request("market_data_cycle")

        self.assertTrue(decision.allowed)

    def test_token_issue_does_not_block_kis_quote_kind(self):
        clock = FakeClock()
        limiter = KisRateLimiter(token_cooldown_seconds=61.0, clock=clock)

        limiter.record_token_issue()
        decision = limiter.allow_request("kis_quote")

        self.assertTrue(decision.allowed)

    def test_token_issue_does_not_block_kis_live_read_kind(self):
        clock = FakeClock()
        limiter = KisRateLimiter(token_cooldown_seconds=61.0, clock=clock)

        limiter.record_token_issue()
        decision = limiter.allow_request("kis_live_read")

        self.assertTrue(decision.allowed)

    def test_token_issue_does_not_block_kis_live_mutation_kind(self):
        clock = FakeClock()
        limiter = KisRateLimiter(token_cooldown_seconds=61.0, clock=clock)

        limiter.record_token_issue()
        decision = limiter.allow_request("kis_live_mutation")

        self.assertTrue(decision.allowed)

    def test_api_rate_limit_error_blocks_until_retry_after_expires(self):
        clock = FakeClock()
        limiter = KisRateLimiter(clock=clock)

        limiter.record_rate_limit_error(retry_after_seconds=2.5)
        clock.advance(0.5)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertEqual(2.0, decision.retry_after_seconds)
        self.assertEqual("api_backoff", decision.reason)

        clock.advance(2.0)
        self.assertTrue(limiter.allow_request().allowed)

    def test_api_rate_limit_error_without_retry_after_uses_min_interval(self):
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.05, clock=clock)

        limiter.record_rate_limit_error()
        clock.advance(0.05)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertAlmostEqual(1.0, decision.retry_after_seconds)
        self.assertEqual("api_backoff", decision.reason)

    def test_reports_longest_active_blocker_when_multiple_cooldowns_overlap(self):
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.05, token_cooldown_seconds=61.0, clock=clock)

        limiter.record_request()
        limiter.record_token_issue()
        clock.advance(0.25)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertEqual("token_cooldown", decision.reason)
        self.assertAlmostEqual(60.75, decision.retry_after_seconds)

    def test_api_backoff_can_take_priority_over_min_interval(self):
        clock = FakeClock()
        limiter = KisRateLimiter(min_interval_seconds=1.05, clock=clock)

        limiter.record_request()
        limiter.record_rate_limit_error(retry_after_seconds=5.0)
        clock.advance(0.25)
        decision = limiter.allow_request()

        self.assertFalse(decision.allowed)
        self.assertEqual("api_backoff", decision.reason)
        self.assertAlmostEqual(4.75, decision.retry_after_seconds)

    def test_diagnostic_snapshot_reports_each_active_blocker_without_mutating_state(self):
        clock = FakeClock()
        limiter = KisRateLimiter(
            min_interval_seconds=1.25,
            token_cooldown_seconds=61.0,
            clock=clock,
        )
        limiter.record_request("kis_live_api")
        limiter.record_token_issue()
        limiter.record_rate_limit_error(retry_after_seconds=2.5)
        clock.advance(0.25)

        snapshot = limiter.diagnostic_snapshot("kis_live_api")

        self.assertFalse(snapshot["allowed"])
        self.assertEqual("token_cooldown", snapshot["reason"])
        self.assertAlmostEqual(60.75, snapshot["retryAfterSeconds"])
        self.assertAlmostEqual(1.0, snapshot["requestSpacingRemainingSeconds"])
        self.assertAlmostEqual(60.75, snapshot["tokenCooldownRemainingSeconds"])
        self.assertAlmostEqual(2.25, snapshot["apiBackoffRemainingSeconds"])
        self.assertEqual("kis_live_api", snapshot["lastRequestKind"])
        self.assertEqual(1.25, snapshot["minIntervalSeconds"])
        self.assertEqual(snapshot, limiter.diagnostic_snapshot("kis_live_api"))


if __name__ == "__main__":
    unittest.main()
