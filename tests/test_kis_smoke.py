import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.kis_smoke import load_kis_vts_credentials, main, run_read_only_smoke


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = [
            {"access_token": "token-123"},
            {"rt_cd": "0", "output": {"stck_prpr": "70000"}},
            {
                "rt_cd": "0",
                "output1": [
                    {"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "69000", "prpr": "70000"},
                    {"pdno": "000660", "hldg_qty": "0", "pchs_avg_pric": "180000", "prpr": "182000"},
                ],
                "output2": [{"dnca_tot_amt": "1000000"}],
            },
        ]

    def __call__(self, request):
        self.calls.append(request)
        return self.responses.pop(0)


class RateLimitedBalanceTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.responses = [
            {"access_token": "token-123"},
            {"rt_cd": "0", "output": {"stck_prpr": "70000"}},
            {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "초당 거래건수를 초과하였습니다."},
            {
                "rt_cd": "0",
                "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "69000", "prpr": "70000"}],
                "output2": [{"dnca_tot_amt": "1000000"}],
            },
        ]


class KisSmokeTest(unittest.TestCase):
    def test_loads_credentials_from_env_file_and_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_VTS_APP_KEY=file-key",
                        "KIS_VTS_APP_SECRET=file-secret",
                        "KIS_VTS_ACCOUNT_NO=12345678",
                        "KIS_VTS_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"KIS_VTS_APP_KEY": "env-key"}, clear=True):
                credentials = load_kis_vts_credentials(env_path)

        self.assertEqual("env-key", credentials.app_key)
        self.assertEqual("file-secret", credentials.app_secret)
        self.assertEqual("12345678", credentials.account_no)
        self.assertEqual("01", credentials.account_product_code)

    def test_missing_credentials_raise_without_leaking_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("KIS_VTS_APP_SECRET=secret-value\n", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "KIS_VTS_APP_KEY") as context:
                    load_kis_vts_credentials(env_path)

        self.assertNotIn("secret-value", str(context.exception))

    def test_read_only_smoke_calls_token_price_and_balance(self):
        transport = FakeTransport()

        result = run_read_only_smoke(
            env={
                "KIS_VTS_APP_KEY": "key",
                "KIS_VTS_APP_SECRET": "secret",
                "KIS_VTS_ACCOUNT_NO": "12345678",
                "KIS_VTS_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
        )

        self.assertTrue(result["token_issued"])
        self.assertEqual("005930", result["symbol"])
        self.assertEqual("70000", result["last_price"])
        self.assertEqual(1, result["balance_positions"])
        self.assertEqual("1000000", result["cash"])
        self.assertEqual("1210000", result["equity"])
        self.assertEqual(["/oauth2/tokenP", "/uapi/domestic-stock/v1/quotations/inquire-price", "/uapi/domestic-stock/v1/trading/inquire-balance"], [call.path for call in transport.calls])
        self.assertEqual(["POST", "GET", "GET"], [call.method for call in transport.calls])
        self.assertNotIn("/uapi/domestic-stock/v1/trading/order-cash", [call.path for call in transport.calls])

    def test_smoke_result_does_not_include_secret_values(self):
        transport = FakeTransport()

        result = run_read_only_smoke(
            env={
                "KIS_VTS_APP_KEY": "key",
                "KIS_VTS_APP_SECRET": "secret-value",
                "KIS_VTS_ACCOUNT_NO": "12345678",
                "KIS_VTS_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
        )

        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)

    def test_read_only_smoke_retries_once_after_per_second_rate_limit(self):
        transport = RateLimitedBalanceTransport()
        slept = []

        result = run_read_only_smoke(
            env={
                "KIS_VTS_APP_KEY": "key",
                "KIS_VTS_APP_SECRET": "secret",
                "KIS_VTS_ACCOUNT_NO": "12345678",
                "KIS_VTS_ACCOUNT_PRODUCT_CODE": "01",
            },
            symbol="005930",
            transport=transport,
            sleep=slept.append,
            rate_limit_retry_delay=1.1,
        )

        self.assertEqual("70000", result["last_price"])
        self.assertEqual(1, result["balance_positions"])
        self.assertEqual([1.1], slept)
        self.assertEqual(
            [
                "/oauth2/tokenP",
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
                "/uapi/domestic-stock/v1/trading/inquire-balance",
            ],
            [call.path for call in transport.calls],
        )

    def test_credentials_repr_does_not_include_app_key_secret_or_account(self):
        credentials = load_kis_vts_credentials(
            env={
                "KIS_VTS_APP_KEY": "app-key-value",
                "KIS_VTS_APP_SECRET": "secret-value",
                "KIS_VTS_ACCOUNT_NO": "12345678",
                "KIS_VTS_ACCOUNT_PRODUCT_CODE": "01",
            }
        )

        rendered = repr(credentials)

        self.assertNotIn("app-key-value", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("12345678", rendered)

    def test_cli_errors_do_not_print_secret_values(self):
        stderr = io.StringIO()

        with patch(
            "stockbot.kis_smoke.run_read_only_smoke",
            side_effect=RuntimeError("secret-value token-123 12345678"),
        ):
            with redirect_stderr(stderr):
                return_code = main(["--env-file", ".env", "--symbol", "005930"])

        rendered = stderr.getvalue()
        self.assertEqual(1, return_code)
        self.assertIn("KIS VTS smoke failed: RuntimeError", rendered)
        self.assertNotIn("secret-value", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("12345678", rendered)

    def test_module_cli_fails_cleanly_when_env_is_missing(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")
            for key in list(env):
                if key.startswith("KIS_VTS_"):
                    del env[key]

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "stockbot.kis_smoke",
                    "--env-file",
                    str(env_path),
                    "--symbol",
                    "005930",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertNotEqual(0, result.returncode)
        self.assertIn("missing KIS VTS credentials", result.stderr)


if __name__ == "__main__":
    unittest.main()
