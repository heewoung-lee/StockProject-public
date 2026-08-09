import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import BotConfig
from stockbot.live_safety import (
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
    LIVE_ALLOW_ENV_KEY,
    LIVE_CONFIRMATION_PHRASE,
    LIVE_ENABLED_ENV_KEY,
    LiveOrderPreflightRequest,
    assess_live_order_preflight,
    assess_live_trading_readiness,
    live_credential_scope_fingerprint,
    live_order_gate_configured,
    load_live_kis_credentials,
    read_env_file,
)
from stockbot.models import AccountSnapshot, Order, Position


LIVE_ENV = {
    "KIS_LIVE_APP_KEY": "live-app-key",
    "KIS_LIVE_APP_SECRET": "live-app-secret",
    "KIS_LIVE_ACCOUNT_NO": "test-live-account",
    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
    "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
    "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "nt",
}
APPROVED_LIVE_ENV = {
    **LIVE_ENV,
    "KIS_LIVE_ACCOUNT_NO": "12345640",
    LIVE_ALLOW_ENV_KEY: "true",
    LIVE_ENABLED_ENV_KEY: "true",
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: "40",
}


class LiveTradingSafetyTest(unittest.TestCase):
    def test_live_credential_scope_fingerprint_is_stable_and_scope_bound(self):
        values = {
            "KIS_LIVE_APP_KEY": "app-key",
            "KIS_LIVE_APP_SECRET": "app-secret",
            "KIS_LIVE_ACCOUNT_NO": "12345678",
            "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
        }

        first = live_credential_scope_fingerprint(values)
        reordered = live_credential_scope_fingerprint(dict(reversed(tuple(values.items()))))
        changed = live_credential_scope_fingerprint({**values, "KIS_LIVE_ACCOUNT_NO": "87654321"})

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)
        self.assertEqual(64, len(first))

    def test_default_config_is_not_live_ready(self):
        readiness = assess_live_trading_readiness(BotConfig.default(), env={})

        self.assertFalse(readiness.ready)
        self.assertIn("trading_mode=live", readiness.blockers)
        self.assertIn("live broker is not implemented", readiness.blockers)
        self.assertIn("live fill reconciliation is not implemented", readiness.blockers)

    def test_live_readiness_requires_all_explicit_gates_and_credentials(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=False)

        readiness = assess_live_trading_readiness(config, env={"STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE})

        self.assertFalse(readiness.ready)
        self.assertIn("allow_live_trading=true and STOCKBOT_ALLOW_LIVE_TRADING=true", readiness.blockers)
        self.assertIn("live_trading_enabled=true and STOCKBOT_LIVE_TRADING_ENABLED=true", readiness.blockers)
        missing_blocker = next(blocker for blocker in readiness.blockers if blocker.startswith("missing KIS live credentials:"))
        self.assertIn("KIS_LIVE_APP_KEY", missing_blocker)
        self.assertIn("KIS_LIVE_APP_SECRET", missing_blocker)
        self.assertIn("live broker is not implemented", readiness.blockers)

    def test_live_order_gate_requires_config_and_local_env_approval(self):
        config = BotConfig(trading_mode="live", allow_live_trading=False, live_trading_enabled=False)
        env = {
            **LIVE_ENV,
            LIVE_ALLOW_ENV_KEY: "true",
            LIVE_ENABLED_ENV_KEY: "true",
            LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: "nt",
        }

        self.assertFalse(live_order_gate_configured(config, env))

        approved_config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)
        self.assertTrue(live_order_gate_configured(approved_config, env))

    def test_live_order_gate_requires_account_suffix_confirmation(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)
        env = {
            **LIVE_ENV,
            LIVE_ALLOW_ENV_KEY: "true",
            LIVE_ENABLED_ENV_KEY: "true",
            LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: "wrong",
        }

        self.assertFalse(live_order_gate_configured(config, env))

    def test_live_order_gate_requires_local_approval_env_flags_even_when_config_allows_live(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)
        env = {
            **LIVE_ENV,
            LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: "nt",
        }

        self.assertFalse(live_order_gate_configured(config, env))

    def test_live_readiness_requires_exact_true_local_approval_env_flags(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)
        env = {
            **APPROVED_LIVE_ENV,
            LIVE_ALLOW_ENV_KEY: "1",
            LIVE_ENABLED_ENV_KEY: "yes",
        }

        readiness = assess_live_trading_readiness(
            config,
            env=env,
            live_broker_available=True,
            fill_reconciliation_available=True,
            managed_position_ledger_available=True,
        )

        self.assertFalse(readiness.ready)
        self.assertIn("allow_live_trading=true and STOCKBOT_ALLOW_LIVE_TRADING=true", readiness.blockers)
        self.assertIn("live_trading_enabled=true and STOCKBOT_LIVE_TRADING_ENABLED=true", readiness.blockers)

    def test_live_readiness_still_blocks_until_live_broker_and_fill_reconciliation_are_available(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)

        readiness = assess_live_trading_readiness(config, env=APPROVED_LIVE_ENV)

        self.assertFalse(readiness.ready)
        self.assertEqual(
            (
                "live broker is not implemented",
                "live fill reconciliation is not implemented",
                "managed live position ledger is not available",
            ),
            readiness.blockers,
        )

    def test_live_readiness_blocks_when_fill_reconciliation_is_missing(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)

        readiness = assess_live_trading_readiness(
            config,
            env=APPROVED_LIVE_ENV,
            live_broker_available=True,
            managed_position_ledger_available=True,
        )

        self.assertFalse(readiness.ready)
        self.assertEqual(("live fill reconciliation is not implemented",), readiness.blockers)

    def test_live_readiness_can_pass_only_when_broker_implementation_is_explicitly_available(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)

        readiness = assess_live_trading_readiness(
            config,
            env=APPROVED_LIVE_ENV,
            live_broker_available=True,
            fill_reconciliation_available=True,
            managed_position_ledger_available=True,
        )

        self.assertTrue(readiness.ready)
        self.assertEqual((), readiness.blockers)

    def test_load_live_credentials_uses_separate_live_env_names(self):
        credentials = load_live_kis_credentials(LIVE_ENV)

        self.assertEqual("live-app-key", credentials.app_key)
        self.assertEqual("live-app-secret", credentials.app_secret)
        self.assertEqual("test-live-account", credentials.account_no)
        self.assertEqual("01", credentials.account_product_code)

    def test_read_env_file_loads_simple_key_values_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "# local only",
                        "KIS_LIVE_APP_KEY='abc'",
                        'KIS_LIVE_APP_SECRET="def"',
                        "KIS_LIVE_ACCOUNT_NO=test-live-account",
                    ]
                ),
                encoding="utf-8",
            )

            values = read_env_file(path)

        self.assertEqual("abc", values["KIS_LIVE_APP_KEY"])
        self.assertEqual("def", values["KIS_LIVE_APP_SECRET"])
        self.assertEqual("test-live-account", values["KIS_LIVE_ACCOUNT_NO"])

    def test_live_order_preflight_fails_closed_by_default(self):
        request = LiveOrderPreflightRequest(
            config=BotConfig.default(),
            env={},
            order=Order.buy("005930", 1, "entry"),
            account=AccountSnapshot(cash=Decimal("1000000")),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("trading_mode=live", decision.blockers)
        self.assertIn("live broker is not implemented", decision.blockers)

    def test_live_order_preflight_requires_market_session_and_account_confirmation(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("100000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 1, "entry"),
            account=AccountSnapshot(cash=Decimal("1000000")),
            estimated_price=Decimal("70000"),
            market_is_open=False,
            session_approved=False,
            account_confirmation="wrong",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("market_is_open=true", decision.blockers)
        self.assertIn("session_approved=true", decision.blockers)
        self.assertIn("account_confirmation=40", decision.blockers)

    def test_live_order_preflight_requires_audit_risk_and_entry_gates(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("100000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 1, "entry"),
            account=AccountSnapshot(cash=Decimal("1000000")),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("audit_log_ready=true", decision.blockers)
        self.assertIn("risk_limits_ok=true", decision.blockers)
        self.assertIn("new_entries_allowed=true", decision.blockers)

    def test_live_order_preflight_rejects_cleanup_mode_new_entry_even_when_runtime_risk_gate_passes(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            kill_switch=True,
            max_order_amount=Decimal("100000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 1, "entry"),
            account=AccountSnapshot(cash=Decimal("1000000")),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("kill_switch=false", decision.blockers)

    def test_live_order_preflight_allows_cleanup_mode_sell_when_other_gates_pass(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            kill_switch=True,
            max_order_amount=Decimal("100000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 1, "cleanup exit"),
            account=AccountSnapshot(
                cash=Decimal("1000000"),
                positions={
                    "005930": Position(
                        symbol="005930",
                        quantity=1,
                        avg_price=Decimal("70000"),
                        last_price=Decimal("70000"),
                        opened_at=opened_at,
                        highest_price=Decimal("70000"),
                        sellable_quantity=1,
                        managed_quantity=1,
                    )
                },
            ),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=False,
        )

        decision = assess_live_order_preflight(request)

        self.assertTrue(decision.approved)
        self.assertNotIn("kill_switch=false", decision.blockers)

    def test_live_order_preflight_can_approve_only_when_all_explicit_gates_pass(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("100000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 1, "entry"),
            account=AccountSnapshot(cash=Decimal("1000000")),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertTrue(decision.approved)
        self.assertEqual((), decision.blockers)

    def test_live_order_preflight_rejects_notional_and_cash_errors(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("50000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 2, "entry"),
            account=AccountSnapshot(cash=Decimal("100000")),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("max_order_amount", decision.blockers)
        self.assertIn("buying_power", decision.blockers)

    def test_live_order_preflight_requires_known_sellable_quantity_for_sell(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=2,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 1, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("sellable_position_known", decision.blockers)

    def test_live_order_preflight_rejects_sell_when_sellable_quantity_is_too_low(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=1,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 2, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("sellable_position", decision.blockers)

    def test_live_order_preflight_requires_known_managed_quantity_for_sell(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=3,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 1, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("managed_position_known", decision.blockers)

    def test_live_order_preflight_rejects_sell_above_managed_quantity(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=3,
            managed_quantity=1,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 2, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("managed_position", decision.blockers)

    def test_live_order_preflight_rejects_buy_when_manual_same_symbol_position_exists(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=3,
            managed_quantity=1,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.buy("005930", 1, "scale_in"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("manual_position_overlap", decision.blockers)

    def test_live_order_preflight_rejects_sell_when_manual_same_symbol_position_exists(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=3,
            managed_quantity=2,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 2, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertFalse(decision.approved)
        self.assertIn("manual_position_overlap", decision.blockers)

    def test_live_order_preflight_allows_adopted_partial_sell_when_order_stays_within_managed_quantity(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=3,
            sellable_quantity=3,
            managed_quantity=2,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 2, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
            allow_managed_partial_sell=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertTrue(decision.approved)
        self.assertNotIn("manual_position_overlap", decision.blockers)

    def test_live_order_preflight_allows_sell_when_sellable_quantity_is_sufficient(self):
        config = BotConfig(
            trading_mode="live",
            allow_live_trading=True,
            live_trading_enabled=True,
            allow_paper_short=False,
            max_order_amount=Decimal("200000"),
        )
        position = Position(
            symbol="005930",
            quantity=2,
            sellable_quantity=2,
            managed_quantity=2,
            avg_price=Decimal("70000"),
            last_price=Decimal("70000"),
            opened_at=datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc),
            highest_price=Decimal("70000"),
        )
        request = LiveOrderPreflightRequest(
            config=config,
            env=APPROVED_LIVE_ENV,
            order=Order.sell("005930", 2, "take_profit"),
            account=AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position}),
            estimated_price=Decimal("70000"),
            market_is_open=True,
            session_approved=True,
            account_confirmation="40",
            expected_account_suffix="40",
            live_broker_available=True,
            fill_reconciliation_available=True,
            audit_log_ready=True,
            managed_position_ledger_available=True,
            risk_limits_ok=True,
            new_entries_allowed=True,
        )

        decision = assess_live_order_preflight(request)

        self.assertTrue(decision.approved)
        self.assertEqual((), decision.blockers)


if __name__ == "__main__":
    unittest.main()
