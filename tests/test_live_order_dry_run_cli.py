import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class LiveOrderDryRunCliTest(unittest.TestCase):
    def test_cli_builds_live_order_candidate_and_prints_redacted_json(self):
        from stockbot.live_order_dry_run_cli import main

        captured = {}

        def fake_dry_run(**kwargs):
            captured.update(kwargs)
            return {
                "mode": "kis-live-order-dry-run",
                "read_only": True,
                "dry_run": True,
                "order_submitted": False,
                "symbol": kwargs["order"].symbol,
                "side": kwargs["order"].side,
                "quantity": kwargs["order"].quantity,
                "note": "secret-value must not leak",
            }

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=file-key",
                        "KIS_LIVE_APP_SECRET=secret-value",
                        "KIS_LIVE_ACCOUNT_NO=test-live-account21",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )
            config_path = Path(tmp) / "live.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "max_order_amount: 100000",
                    ]
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("stockbot.live_order_dry_run_cli.run_live_order_dry_run", side_effect=fake_dry_run):
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
                            "3",
                            "--reason",
                            "operator dry-run",
                            "--market-open",
                            "--session-approved",
                            "--account-confirmation",
                            "21",
                            "--expected-account-suffix",
                            "21",
                            "--fill-reconciliation-available",
                            "--audit-log-ready",
                            "--managed-position-ledger-available",
                            "--risk-limits-ok",
                            "--new-entries-allowed",
                        ]
                    )

        self.assertEqual(0, rc)
        self.assertEqual("005930", captured["order"].symbol)
        self.assertEqual("BUY", captured["order"].side)
        self.assertEqual(3, captured["order"].quantity)
        self.assertEqual("operator dry-run", captured["order"].reason)
        self.assertTrue(captured["market_is_open"])
        self.assertTrue(captured["session_approved"])
        self.assertTrue(captured["fill_reconciliation_available"])
        self.assertTrue(captured["audit_log_ready"])
        self.assertTrue(captured["managed_position_ledger_available"])
        self.assertTrue(captured["risk_limits_ok"])
        self.assertTrue(captured["new_entries_allowed"])
        rendered = json.loads(stdout.getvalue())
        self.assertEqual("kis-live-order-dry-run", rendered["mode"])
        self.assertTrue(rendered["read_only"])
        self.assertFalse(rendered["order_submitted"])
        self.assertNotIn("secret-value", stdout.getvalue())

    def test_cli_reports_redacted_errors_to_stderr(self):
        from stockbot.live_order_dry_run_cli import main

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("KIS_LIVE_APP_SECRET=live-secret-value\n", encoding="utf-8")
            stderr = io.StringIO()
            with patch("stockbot.live_order_dry_run_cli.run_live_order_dry_run", side_effect=RuntimeError("failed with live-secret-value")):
                with patch("sys.stderr", stderr):
                    rc = main(["--env-file", str(env_path), "--symbol", "005930", "--quantity", "1"])

        self.assertEqual(1, rc)
        payload = json.loads(stderr.getvalue())
        self.assertFalse(payload["ready"])
        self.assertIn("failed", payload["error"])
        self.assertNotIn("live-secret-value", stderr.getvalue())

    def test_cli_redacts_process_env_secrets_when_env_file_is_missing(self):
        from stockbot.live_order_dry_run_cli import main

        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            missing_env = Path(tmp) / ".env"
            with patch.dict("os.environ", {"KIS_LIVE_APP_SECRET": "process-secret-value"}, clear=False):
                with patch("stockbot.live_order_dry_run_cli.run_live_order_dry_run", side_effect=RuntimeError("failed with process-secret-value")):
                    with patch("sys.stderr", stderr):
                        rc = main(["--env-file", str(missing_env), "--symbol", "005930", "--quantity", "1"])

        self.assertEqual(1, rc)
        payload = json.loads(stderr.getvalue())
        self.assertFalse(payload["ready"])
        self.assertIn("failed", payload["error"])
        self.assertNotIn("process-secret-value", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
