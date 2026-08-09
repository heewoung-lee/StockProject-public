import csv
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.cli import build_engine
from stockbot.config import BotConfig


class CliTest(unittest.TestCase):
    def test_build_engine_wires_cost_filter_to_strategy(self):
        config = BotConfig(
            trading_mode="paper",
            transaction_tax_pct=Decimal("0.0025"),
            commission_pct=Decimal("0.00015"),
            slippage_pct=Decimal("0.0015"),
            min_net_profit_pct=Decimal("0.002"),
        )

        engine = build_engine(config)

        strategy_config = engine.strategy.config
        self.assertEqual(Decimal("0.0025"), strategy_config.transaction_tax_pct)
        self.assertEqual(Decimal("0.00015"), strategy_config.commission_pct)
        self.assertEqual(Decimal("0.0015"), strategy_config.slippage_pct)
        self.assertEqual(Decimal("0.002"), strategy_config.min_net_profit_pct)

    def test_cli_runs_sample_data_and_writes_journal(self):
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
                    "stockbot.cli",
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
            with journal_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(1, len(rows))
            self.assertEqual("FILL", rows[0]["event"])
            self.assertEqual("paper", rows[0]["mode"])

    def test_cli_rejects_kis_vts_execution_loop_until_broker_is_wired(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: kis-vts",
                        "allow_kis_vts_trading: true",
                        "data_path: data/sample_bars.csv",
                        f"journal_path: {(tmp_path / 'trades.csv').as_posix()}",
                    ]
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(root / "src")

            result = subprocess.run(
                [sys.executable, "-m", "stockbot.cli", "--config", str(config_path), "--max-bars", "1"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("execution loop only supports paper mode", result.stderr)


if __name__ == "__main__":
    unittest.main()
