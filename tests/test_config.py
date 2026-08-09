import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import BotConfig, KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, load_config


class ConfigTest(unittest.TestCase):
    def test_default_config_is_safe_paper_mode(self):
        config = BotConfig.default()

        self.assertEqual("paper", config.trading_mode)
        self.assertFalse(config.allow_live_trading)
        self.assertFalse(config.live_trading_enabled)
        self.assertEqual(0, config.max_holding_minutes)
        self.assertEqual("", config.forced_exit_time)
        self.assertTrue(config.allow_after_hours_simulation)
        self.assertTrue(config.enforce_market_hours)
        config.validate_safety()

    def test_live_example_config_is_loader_safe(self):
        config = load_config(Path(__file__).resolve().parents[1] / "config.live.example.yaml")

        self.assertEqual("live", config.trading_mode)
        self.assertTrue(config.allow_live_trading)
        self.assertTrue(config.live_trading_enabled)
        self.assertFalse(config.allow_paper_short)
        self.assertEqual("local", config.market_data_source)
        self.assertEqual("local", config.scanner_source)
        self.assertEqual("data/scanner_snapshot.json", config.scanner_snapshot_path)

    def test_live_mode_fails_closed_without_all_gates(self):
        config = BotConfig(trading_mode="live", allow_live_trading=False, live_trading_enabled=True)

        with self.assertRaisesRegex(ValueError, "live trading requires"):
            config.validate_safety()

    def test_live_mode_is_allowed_only_when_all_config_gates_are_present(self):
        config = BotConfig(trading_mode="live", allow_live_trading=True, live_trading_enabled=True)

        config.validate_safety()

    def test_kis_vts_mode_requires_explicit_gate(self):
        config = BotConfig(trading_mode="kis-vts")

        with self.assertRaisesRegex(ValueError, "kis-vts trading requires"):
            config.validate_safety()

    def test_kis_vts_mode_is_allowed_with_explicit_gate(self):
        config = BotConfig(trading_mode="kis-vts", allow_kis_vts_trading=True)

        config.validate_safety()

    def test_paper_mode_rejects_broker_trading_gates(self):
        for kwargs in (
            {"allow_kis_vts_trading": True},
            {"allow_live_trading": True},
            {"live_trading_enabled": True},
        ):
            with self.subTest(kwargs=kwargs):
                config = BotConfig(trading_mode="paper", **kwargs)

                with self.assertRaisesRegex(ValueError, "paper mode"):
                    config.validate_safety()

    def test_allow_paper_short_is_rejected_outside_paper_mode(self):
        config = BotConfig(trading_mode="kis-vts", allow_kis_vts_trading=True, allow_paper_short=True)

        with self.assertRaisesRegex(ValueError, "allow_paper_short"):
            config.validate_safety()

    def test_direct_boolean_safety_gates_must_be_real_booleans(self):
        config = BotConfig(trading_mode="kis-vts", allow_kis_vts_trading="false")

        with self.assertRaisesRegex(ValueError, "allow_kis_vts_trading must be boolean"):
            config.validate_safety()

    def test_load_config_reads_simple_yaml_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                (
                    "trading_mode: paper\n"
                    "initial_cash: 500000\n"
                    "strategy_profile: aggressive\n"
                    "order_cash_amount: 30000\n"
                    "cash_allocation_pct: 0.50\n"
                    "allow_after_hours_simulation: false\n"
                    "enforce_market_hours: false\n"
                    "market_closed_dates: 2026-05-01,2026-12-31\n"
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual("paper", config.trading_mode)
            self.assertEqual("500000", str(config.initial_cash))
            self.assertEqual("single", config.strategy_profile)
            self.assertEqual("50000", str(config.order_cash_amount))
            self.assertEqual("1.0", str(config.cash_allocation_pct))
            self.assertFalse(config.allow_after_hours_simulation)
            self.assertFalse(config.enforce_market_hours)
            self.assertEqual("2026-05-01,2026-12-31", config.market_closed_dates)

    def test_load_config_reads_strategy_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "momentum_window: 2",
                        "min_momentum_pct: 0.015",
                        "min_signal_confidence: 0.55",
                        "volume_window: 2",
                        "min_volume_ratio: 2.5",
                        "forced_exit_time: 15:15",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual(2, config.momentum_window)
            self.assertEqual("0.015", str(config.min_momentum_pct))
            self.assertEqual("0.55", str(config.min_signal_confidence))
            self.assertEqual(2, config.volume_window)
            self.assertEqual("2.5", str(config.min_volume_ratio))
            self.assertEqual("15:15", config.forced_exit_time)

    def test_load_config_reads_cost_filter_parameters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "transaction_tax_pct: 0.002",
                        "commission_pct: 0.00015",
                        "slippage_pct: 0.001",
                        "min_net_profit_pct: 0.0025",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual("0.002", str(config.transaction_tax_pct))
            self.assertEqual("0.00015", str(config.commission_pct))
            self.assertEqual("0.001", str(config.slippage_pct))
            self.assertEqual("0.0025", str(config.min_net_profit_pct))

    def test_load_config_reads_kis_market_data_settings_for_paper_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "market_data_source: kis-vts",
                        "kis_market_data_symbols: 005930,000660",
                        "kis_market_data_scan_limit: 2",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual("kis-vts", config.market_data_source)
            self.assertEqual("005930,000660", config.kis_market_data_symbols)
            self.assertEqual(2, config.kis_market_data_scan_limit)

    def test_load_config_reads_external_scanner_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "market_data_source: external-scan-kis",
                        "scanner_source: json",
                        "scanner_snapshot_path: data/scanner_snapshot.json",
                        "scanner_snapshot_max_age_seconds: 45",
                    ]
                ),
                encoding="utf-8",
            )

            config = load_config(path)

            self.assertEqual("external-scan-kis", config.market_data_source)
            self.assertEqual("json", config.scanner_source)
            self.assertEqual("data/scanner_snapshot.json", config.scanner_snapshot_path)
            self.assertEqual(45, config.scanner_snapshot_max_age_seconds)

    def test_external_scanner_snapshot_max_age_must_not_be_negative(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="external-scan-kis",
            scanner_source="json",
            scanner_snapshot_path="data/scanner_snapshot.json",
            scanner_snapshot_max_age_seconds=-1,
        )

        with self.assertRaisesRegex(ValueError, "scanner_snapshot_max_age_seconds"):
            config.validate_safety()

    def test_external_scanner_source_requires_external_scan_mode(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="local",
            scanner_source="json",
            scanner_snapshot_path="data/scanner_snapshot.json",
        )

        with self.assertRaisesRegex(ValueError, "scanner_source=json requires market_data_source=external-scan-kis"):
            config.validate_safety()

    def test_json_scanner_source_requires_snapshot_path(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="external-scan-kis",
            scanner_source="json",
            scanner_snapshot_path="",
        )

        with self.assertRaisesRegex(ValueError, "scanner_snapshot_path is required"):
            config.validate_safety()

    def test_external_scan_kis_requires_external_scanner_source(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="external-scan-kis",
            scanner_source="local",
        )

        with self.assertRaisesRegex(ValueError, "external-scan-kis requires scanner_source=json"):
            config.validate_safety()

    def test_load_config_allows_external_scan_kis_only_for_paper_mode(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="external-scan-kis",
            scanner_source="json",
            scanner_snapshot_path="data/scanner_snapshot.json",
            enforce_market_hours=True,
        )

        config.validate_safety()

        live_config = BotConfig(
            trading_mode="kis-vts",
            allow_kis_vts_trading=True,
            market_data_source="external-scan-kis",
            scanner_source="json",
            scanner_snapshot_path="data/scanner_snapshot.json",
        )
        with self.assertRaisesRegex(ValueError, "KIS market data source"):
            live_config.validate_safety()

    def test_default_kis_market_data_settings_are_bounded_for_intraday_rehearsal(self):
        config = BotConfig.default()
        symbols = [symbol.strip() for symbol in config.kis_market_data_symbols.split(",") if symbol.strip()]

        self.assertEqual(20, len(symbols))
        self.assertIn("005930", symbols)
        self.assertIn("086520", symbols)
        self.assertIn("247540", symbols)
        self.assertEqual(KIS_INTRADAY_REHEARSAL_SCAN_LIMIT, config.kis_market_data_scan_limit)

    def test_load_config_rejects_kis_market_data_outside_paper_mode(self):
        config = BotConfig(
            trading_mode="kis-vts",
            allow_kis_vts_trading=True,
            market_data_source="kis-vts",
        )

        with self.assertRaisesRegex(ValueError, "KIS market data source"):
            config.validate_safety()

    def test_load_config_rejects_kis_market_data_without_market_hours_gate(self):
        config = BotConfig(
            trading_mode="paper",
            market_data_source="kis-vts",
            enforce_market_hours=False,
        )

        with self.assertRaisesRegex(ValueError, "KIS market data source requires enforce_market_hours=true"):
            config.validate_safety()

    def test_load_config_exposes_paper_short_without_live_order_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nallow_paper_short: true\n", encoding="utf-8")

            config = load_config(path)

            self.assertTrue(config.allow_paper_short)
            self.assertFalse(hasattr(config, "allow_live_orders"))

    def test_load_config_rejects_invalid_boolean_tokens_for_safety_gates(self):
        for key in (
            "kill_switch",
            "allow_kis_vts_trading",
            "allow_live_trading",
            "allow_paper_short",
            "allow_after_hours_simulation",
            "enforce_market_hours",
        ):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "config.yaml"
                path.write_text(f"trading_mode: paper\n{key}: maybe\n", encoding="utf-8")

                with self.assertRaisesRegex(ValueError, key):
                    load_config(path)

    def test_load_config_rejects_negative_max_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nmax_positions: -1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "max_positions must be 0 or greater"):
                load_config(path)

    def test_load_config_rejects_negative_max_holding_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nmax_holding_minutes: -1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "max_holding_minutes must be 0 or greater"):
                load_config(path)

    def test_load_config_rejects_signal_confidence_above_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nmin_signal_confidence: 1.1\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "min_signal_confidence"):
                load_config(path)

    def test_load_config_allows_zero_max_holding_minutes_as_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.yaml"
            path.write_text("trading_mode: paper\nmax_holding_minutes: 0\nforced_exit_time:\n", encoding="utf-8")

            config = load_config(path)

            self.assertEqual(0, config.max_holding_minutes)
            self.assertEqual("", config.forced_exit_time)


if __name__ == "__main__":
    unittest.main()
