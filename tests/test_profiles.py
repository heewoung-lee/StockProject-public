import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import load_config
from stockbot.profiles import PROFILE_NAMES, get_profile_settings


class StrategyProfileTest(unittest.TestCase):
    def test_profile_names_are_stable(self):
        self.assertEqual(("conservative", "balanced", "aggressive"), PROFILE_NAMES)

    def test_conservative_profile_reduces_risk(self):
        settings = get_profile_settings("conservative")

        self.assertEqual("30000", str(settings["order_cash_amount"]))
        self.assertEqual("0.50", str(settings["cash_allocation_pct"]))
        self.assertEqual("0", str(settings["max_order_amount"]))
        self.assertEqual("60000", str(settings["max_daily_loss"]))
        self.assertEqual("0.003", str(settings["min_momentum_pct"]))
        self.assertEqual("2", str(settings["min_volume_ratio"]))
        self.assertEqual("0.70", str(settings["min_signal_confidence"]))
        self.assertEqual("20", str(settings["max_spread_bps"]))
        self.assertEqual("0.012", str(settings["stop_loss_pct"]))
        self.assertEqual(0, settings["max_positions"])
        self.assertEqual(1, settings["max_daily_entries_per_symbol"])

    def test_aggressive_profile_allows_more_risk(self):
        settings = get_profile_settings("aggressive")

        self.assertEqual("80000", str(settings["order_cash_amount"]))
        self.assertEqual("0.90", str(settings["cash_allocation_pct"]))
        self.assertEqual("0", str(settings["max_order_amount"]))
        self.assertEqual("150000", str(settings["max_daily_loss"]))
        self.assertEqual("0.0005", str(settings["min_momentum_pct"]))
        self.assertEqual("1", str(settings["min_volume_ratio"]))
        self.assertEqual("0.55", str(settings["min_signal_confidence"]))
        self.assertEqual("45", str(settings["max_spread_bps"]))
        self.assertEqual("0.03", str(settings["stop_loss_pct"]))
        self.assertEqual(0, settings["max_positions"])
        self.assertEqual(2, settings["max_daily_entries_per_symbol"])

    def test_balanced_profile_is_active_enough_for_intraday_rehearsal(self):
        settings = get_profile_settings("balanced")

        self.assertEqual("0.70", str(settings["cash_allocation_pct"]))
        self.assertEqual("0.55", str(settings["min_signal_confidence"]))
        self.assertEqual("0.001", str(settings["min_momentum_pct"]))
        self.assertEqual("1.2", str(settings["min_volume_ratio"]))
        self.assertEqual(1, settings["max_daily_entries_per_symbol"])

    def test_load_config_ignores_legacy_profile_and_budget_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "strategy_profile: conservative",
                        "order_cash_amount: 40000",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual("single", config.strategy_profile)
            self.assertEqual("50000", str(config.order_cash_amount))
            self.assertEqual("100000", str(config.max_daily_loss))
            self.assertEqual("0.001", str(config.min_momentum_pct))

    def test_load_config_ignores_unknown_legacy_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("strategy_profile: reckless\n", encoding="utf-8")

            config = load_config(path)

            self.assertEqual("single", config.strategy_profile)

    def test_example_config_uses_single_policy_without_profile_or_budget_keys(self):
        root = Path(__file__).resolve().parents[1]
        example = (root / "config.example.yaml").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(example, encoding="utf-8")

            config = load_config(path)

            self.assertNotIn("strategy_profile:", example)
            self.assertNotIn("cash_allocation_pct:", example)
            self.assertNotIn("order_cash_amount:", example)
            self.assertEqual("single", config.strategy_profile)
            self.assertEqual("1.0", str(config.cash_allocation_pct))


if __name__ == "__main__":
    unittest.main()
