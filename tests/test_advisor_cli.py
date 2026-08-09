import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class AdvisorCliTest(unittest.TestCase):
    def test_advisor_cli_outputs_json_without_writing_journal(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "bars.csv"
            journal_path = tmp_path / "trades.csv"
            config_path = tmp_path / "config.yaml"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-10T09:00:00,005930,100,100,100,100,1000,100,99.95,100.05",
                        "2026-06-10T09:01:00,005930,101,101,101,101,1100,101,100.95,101.05",
                        "2026-06-10T09:02:00,005930,102,102,102,102,1200,102,101.95,102.05",
                        "2026-06-10T09:03:00,005930,104,104,104,104,4000,104,103.95,104.05",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "strategy_profile: balanced",
                        f"data_path: {data_path.as_posix()}",
                        f"journal_path: {journal_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "stockbot.advisor_cli",
                    "--config",
                    str(config_path),
                    "--max-bars",
                    "4",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("aggressive", payload["recommended_profile"])
            self.assertFalse(journal_path.exists())

    def test_advisor_cli_analyzes_latest_bars_when_limited(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "bars.csv"
            config_path = tmp_path / "config.yaml"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-10T09:00:00,005930,100,100,100,100,1000,100,99.95,100.05",
                        "2026-06-10T09:01:00,005930,101,101,101,101,1100,101,100.95,101.05",
                        "2026-06-10T09:02:00,005930,102,102,102,102,1200,102,101.95,102.05",
                        "2026-06-10T09:03:00,005930,110,110,110,110,1000,110,106,114",
                        "2026-06-10T09:04:00,005930,95,95,95,95,1000,95,91,99",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.advisor_cli", "--config", str(config_path), "--max-bars", "2"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("conservative", payload["recommended_profile"])

    def test_advisor_cli_sanitizes_unexpected_errors(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            missing_config_path = Path(tmp) / "private-config.yaml"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.advisor_cli", "--config", str(missing_config_path)],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(1, result.returncode)
            self.assertIn("advisor failed: FileNotFoundError", result.stderr)
            self.assertNotIn(str(missing_config_path), result.stderr)


if __name__ == "__main__":
    unittest.main()
