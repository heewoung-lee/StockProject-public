import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import BotConfig
from stockbot.live_order_rehearsal_cli import REHEARSAL_CONFIRM_PHRASE, main, run_live_order_rehearsal
from stockbot.live_safety import LIVE_CONFIRMATION_PHRASE
from stockbot.models import Fill, MarketBar, Order


def live_env() -> dict[str, str]:
    return {
        "KIS_LIVE_APP_KEY": "live-app-key",
        "KIS_LIVE_APP_SECRET": "live-secret-value",
        "KIS_LIVE_ACCOUNT_NO": "test-live-account21",
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
        "STOCKBOT_ALLOW_LIVE_TRADING": "true",
        "STOCKBOT_LIVE_TRADING_ENABLED": "true",
        "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
    }


def live_config() -> BotConfig:
    return BotConfig(
        trading_mode="live",
        allow_live_trading=True,
        live_trading_enabled=True,
        max_order_amount=Decimal("100000"),
    )


def bar(symbol: str = "005930", price: str = "70000") -> MarketBar:
    value = Decimal(price)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 7, 2, 9, 1, tzinfo=timezone.utc),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100,
        vwap=value,
        bid=value,
        ask=value,
    )


class FakeLiveOrderClient:
    def __init__(self, market_bar: MarketBar):
        self.market_bar = market_bar
        self.token_issued = False

    def issue_access_token(self):
        self.token_issued = True
        return "quote-token"

    def access_token_expires_at(self):
        return None

    def price_bar(self, symbol):
        return self.market_bar


class FakeBroker:
    def __init__(self):
        self.calls = []

    def place_order(self, order, market_bar):
        self.calls.append({"order": order, "bar": market_bar})
        return Fill(
            order=order,
            accepted=True,
            timestamp=market_bar.timestamp,
            price=market_bar.close,
            quantity=order.quantity,
        )


class LiveOrderRehearsalCliTest(unittest.TestCase):
    def test_rehearsal_places_single_broker_attempt_and_clears_session_approval(self):
        captured = {}
        fake_quote_client = FakeLiveOrderClient(bar())
        fake_order_client = object()
        fake_broker = FakeBroker()

        def quote_client_factory(**kwargs):
            captured["quote_client_kwargs"] = kwargs
            return fake_quote_client

        def client_factory(**kwargs):
            captured["allow_order_placement"] = kwargs["allow_order_placement"]
            captured["order_access_token"] = kwargs["access_token"]
            return fake_order_client

        def broker_factory(config, env_values, *, client, env_file, live_order_safety_context):
            captured["account_confirmation"] = env_values["STOCKBOT_LIVE_ACCOUNT_CONFIRMATION"]
            captured["broker_client"] = client
            captured["safety_context"] = live_order_safety_context
            self.assertTrue(live_order_safety_context.session_approved is False)
            return fake_broker

        result = run_live_order_rehearsal(
            order=Order.buy("005930", 1, "operator rehearsal"),
            config=live_config(),
            env=live_env(),
            confirm=REHEARSAL_CONFIRM_PHRASE,
            account_confirmation="21",
            expected_account_suffix="21",
            quote_client_factory=quote_client_factory,
            client_factory=client_factory,
            broker_factory=broker_factory,
        )

        self.assertTrue(captured["allow_order_placement"])
        self.assertEqual("quote-token", captured["order_access_token"])
        self.assertEqual("21", captured["account_confirmation"])
        self.assertIs(fake_order_client, captured["broker_client"])
        self.assertTrue(fake_quote_client.token_issued)
        self.assertEqual(1, len(fake_broker.calls))
        self.assertFalse(captured["safety_context"].session_approved)
        self.assertEqual("kis-live-one-shot-rehearsal", result["mode"])
        self.assertTrue(result["accepted"])
        self.assertTrue(result["order_submitted_confirmed"])
        self.assertTrue(result["session_approval_cleared"])
        self.assertFalse(result["live_order_enabled"])
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("live-secret-value", rendered)
        self.assertNotIn("test-live-account21", rendered)

    def test_rehearsal_requires_explicit_confirmation_before_client_creation(self):
        def quote_client_factory(**kwargs):
            raise AssertionError("quote client must not be created without explicit one-shot confirmation")

        def client_factory(**kwargs):
            raise AssertionError("client must not be created without explicit one-shot confirmation")

        with self.assertRaisesRegex(ValueError, "I_UNDERSTAND_ONE_REAL_ORDER"):
            run_live_order_rehearsal(
                order=Order.buy("005930", 1, "operator rehearsal"),
                config=live_config(),
                env=live_env(),
                confirm="wrong",
                account_confirmation="21",
                quote_client_factory=quote_client_factory,
                client_factory=client_factory,
            )

    def test_rehearsal_requires_operator_typed_account_confirmation_even_if_env_has_stale_suffix(self):
        env = live_env()
        env["STOCKBOT_LIVE_ACCOUNT_CONFIRMATION"] = "21"

        def quote_client_factory(**kwargs):
            raise AssertionError("quote client must not be created with stale saved confirmation only")

        def client_factory(**kwargs):
            raise AssertionError("order client must not be created with stale saved confirmation only")

        with self.assertRaisesRegex(ValueError, "account confirmation is required"):
            run_live_order_rehearsal(
                order=Order.buy("005930", 1, "operator rehearsal"),
                config=live_config(),
                env=env,
                confirm=REHEARSAL_CONFIRM_PHRASE,
                account_confirmation="",
                quote_client_factory=quote_client_factory,
                client_factory=client_factory,
            )

    def test_rehearsal_rejects_quantity_above_one_shot_cap_before_client_creation(self):
        def quote_client_factory(**kwargs):
            raise AssertionError("quote client must not be created when quantity exceeds cap")

        def client_factory(**kwargs):
            raise AssertionError("client must not be created when quantity exceeds cap")

        with self.assertRaisesRegex(ValueError, "quantity exceeds one-shot rehearsal cap"):
            run_live_order_rehearsal(
                order=Order.buy("005930", 2, "operator rehearsal"),
                config=live_config(),
                env=live_env(),
                confirm=REHEARSAL_CONFIRM_PHRASE,
                account_confirmation="21",
                max_quantity=1,
                quote_client_factory=quote_client_factory,
                client_factory=client_factory,
            )

    def test_rehearsal_rejects_notional_above_cap_before_broker_attempt(self):
        fake_quote_client = FakeLiveOrderClient(bar(price="70000"))
        order_client_called = False
        broker_called = False

        def quote_client_factory(**kwargs):
            return fake_quote_client

        def client_factory(**kwargs):
            nonlocal order_client_called
            order_client_called = True
            raise AssertionError("order-capable client must not be created when notional exceeds cap")

        def broker_factory(*args, **kwargs):
            nonlocal broker_called
            broker_called = True
            raise AssertionError("broker must not be created when notional exceeds cap")

        with self.assertRaisesRegex(ValueError, "notional exceeds one-shot rehearsal cap"):
            run_live_order_rehearsal(
                order=Order.buy("005930", 1, "operator rehearsal"),
                config=live_config(),
                env=live_env(),
                confirm=REHEARSAL_CONFIRM_PHRASE,
                account_confirmation="21",
                max_notional="50000",
                quote_client_factory=quote_client_factory,
                client_factory=client_factory,
                broker_factory=broker_factory,
            )

        self.assertTrue(fake_quote_client.token_issued)
        self.assertFalse(order_client_called)
        self.assertFalse(broker_called)

    def test_cli_builds_rehearsal_order_and_redacts_output(self):
        captured = {}

        def fake_rehearsal(**kwargs):
            captured.update(kwargs)
            return {
                "mode": "kis-live-one-shot-rehearsal",
                "accepted": False,
                "reject_reason": "live-secret-value must not leak",
            }

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("KIS_LIVE_APP_SECRET=live-secret-value\n", encoding="utf-8")
            config_path = Path(tmp) / "live.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                    ]
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("stockbot.live_order_rehearsal_cli.run_live_order_rehearsal", side_effect=fake_rehearsal):
                with patch("sys.stdout", stdout):
                    rc = main(
                        [
                            "--config",
                            str(config_path),
                            "--env-file",
                            str(env_path),
                            "--symbol",
                            "005930",
                            "--side",
                            "BUY",
                            "--quantity",
                            "1",
                            "--reason",
                            "operator rehearsal",
                            "--confirm",
                            REHEARSAL_CONFIRM_PHRASE,
                            "--account-confirmation",
                            "21",
                            "--expected-account-suffix",
                            "21",
                        ]
                    )

        self.assertEqual(0, rc)
        self.assertEqual("005930", captured["order"].symbol)
        self.assertEqual("operator rehearsal", captured["order"].reason)
        self.assertEqual(REHEARSAL_CONFIRM_PHRASE, captured["confirm"])
        self.assertNotIn("live-secret-value", stdout.getvalue())
        self.assertEqual("kis-live-one-shot-rehearsal", json.loads(stdout.getvalue())["mode"])

    def test_cli_reports_redacted_errors_to_stderr(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("KIS_LIVE_APP_SECRET=process-secret-value\n", encoding="utf-8")
            config_path = Path(tmp) / "live.yaml"
            config_path.write_text("trading_mode: live\nallow_live_trading: true\nlive_trading_enabled: true\n", encoding="utf-8")
            with patch("stockbot.live_order_rehearsal_cli.run_live_order_rehearsal", side_effect=RuntimeError("failed with process-secret-value")):
                with patch("sys.stderr", stderr):
                    rc = main(
                        [
                            "--config",
                            str(config_path),
                            "--env-file",
                            str(env_path),
                            "--symbol",
                            "005930",
                            "--quantity",
                            "1",
                            "--confirm",
                            REHEARSAL_CONFIRM_PHRASE,
                            "--account-confirmation",
                            "21",
                        ]
                    )

        self.assertEqual(1, rc)
        payload = json.loads(stderr.getvalue())
        self.assertFalse(payload["ready"])
        self.assertIn("failed", payload["error"])
        self.assertNotIn("process-secret-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
