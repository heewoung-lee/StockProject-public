import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class SimulateCliTest(unittest.TestCase):
    def test_simulate_cli_runs_local_replay_without_market_hours_gate(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "bars.csv"
            config_path = tmp_path / "config.yaml"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000,9990,10010",
                        "2026-06-08T09:01:00,005930,10100,10100,10100,10100,1000,10050,10090,10110",
                        "2026-06-08T09:02:00,005930,10300,10300,10300,10300,3000,10100,10290,10310",
                        "2026-06-08T09:03:00,005930,10600,10600,10600,10600,6000,10300,10590,10610",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "initial_cash: 1000000",
                        "order_cash_amount: 50000",
                        "max_order_amount: 100000",
                        "enforce_market_hours: true",
                        "allow_after_hours_simulation: true",
                        f"data_path: {data_path.as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.simulate_cli", "--config", str(config_path), "--cycles", "4"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("local-simulation", payload["mode"])
            self.assertEqual(4, payload["cycles_completed"])
            self.assertGreaterEqual(payload["filled_trades"], 1)
            self.assertIn("total_pnl", payload)

    def test_simulate_cli_keep_market_hours_respects_configured_closed_day(self):
        root = Path(__file__).resolve().parents[1]
        today_kst = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            data_path = tmp_path / "bars.csv"
            config_path = tmp_path / "config.yaml"
            data_path.write_text(
                "\n".join(
                    [
                        "timestamp,symbol,open,high,low,close,volume,vwap,bid,ask",
                        "2026-06-08T09:00:00,005930,10000,10000,10000,10000,1000,10000,9990,10010",
                        "2026-06-08T09:01:00,005930,10100,10100,10100,10100,1000,10050,10090,10110",
                        "2026-06-08T09:02:00,005930,10300,10300,10300,10300,3000,10100,10290,10310",
                        "2026-06-08T09:03:00,005930,10600,10600,10600,10600,6000,10300,10590,10610",
                    ]
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "initial_cash: 1000000",
                        "order_cash_amount: 50000",
                        "max_order_amount: 100000",
                        "enforce_market_hours: true",
                        "allow_after_hours_simulation: false",
                        f"market_closed_dates: {today_kst}",
                        f"data_path: {data_path.as_posix()}",
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
                    "stockbot.simulate_cli",
                    "--config",
                    str(config_path),
                    "--cycles",
                    "4",
                    "--keep-market-hours",
                ],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("local-simulation", payload["mode"])
            self.assertEqual(4, payload["cycles_completed"])
            self.assertEqual(0, payload["filled_trades"])
            self.assertEqual(0, payload["open_positions"])

    def test_simulate_cli_reports_non_paper_config_without_traceback(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("trading_mode: kis-vts\nallow_kis_vts_trading: true\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.simulate_cli", "--config", str(config_path), "--cycles", "1"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("local simulation only supports trading_mode=paper", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_simulate_cli_reports_invalid_cycles_without_traceback(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("trading_mode: paper\n", encoding="utf-8")
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.simulate_cli", "--config", str(config_path), "--cycles", "0"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("cycles must be greater than 0", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
