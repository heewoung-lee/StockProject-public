import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.live_audit import JsonlLiveAuditLog


class LiveAuditLogTest(unittest.TestCase):
    def test_append_redacts_known_secrets_and_sensitive_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live-audit.jsonl"
            audit = JsonlLiveAuditLog(path, redact_values=["live-secret", "token-123", "87654321"])

            audit.record(
                "preflight_denied",
                {
                    "symbol": "005930",
                    "account_no": "87654321",
                    "app_secret": "live-secret",
                    "authorization": "Bearer token-123",
                    "reason": "market_closed",
                },
            )

            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual("preflight_denied", payload["event"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("live-secret", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("87654321", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertEqual("market_closed", payload["payload"]["reason"])

    def test_append_redacts_sensitive_patterns_inside_message_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live-audit.jsonl"
            audit = JsonlLiveAuditLog(path)

            audit.record(
                "broker_error",
                {
                    "message": (
                        "Authorization: Bearer token-123 "
                        "appsecret=live-secret appkey=live-key "
                        "account=87654321-01 access_token=abc.def"
                    )
                },
            )

            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("live-secret", rendered)
        self.assertNotIn("live-key", rendered)
        self.assertNotIn("87654321", rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_short_env_values_do_not_corrupt_structured_price_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live-audit.jsonl"
            audit = JsonlLiveAuditLog(
                path,
                redact_values=["01", "40", "true", "live-secret", "87654321"],
            )

            audit.record(
                "live_order_preflight_approved",
                {
                    "submitted_price": "10000000",
                    "reference_price": "10000000",
                    "app_secret": "live-secret",
                    "account_no": "87654321",
                },
            )

            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual("10000000", payload["payload"]["submitted_price"])
        self.assertEqual("10000000", payload["payload"]["reference_price"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("live-secret", rendered)
        self.assertNotIn("87654321", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
