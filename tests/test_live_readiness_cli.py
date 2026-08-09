import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.live_order_state import (
    JsonManualReconciliationStore,
    JsonPendingLiveOrderStore,
    ManualReconciliationBlocker,
    PendingLiveOrder,
)
from stockbot.live_position_ledger import managed_live_position_ledger_scope
from stockbot.config import load_config
from stockbot.live_readiness_cli import (
    MANUAL_RECONCILIATION_CLEAR_PHRASE,
    dashboard_live_readiness_config_values,
    main,
    run_live_readiness_check,
)
from stockbot.live_safety import (
    LIVE_ACCOUNT_CONFIRMATION_ENV_KEY,
    LIVE_ALLOW_ENV_KEY,
    LIVE_CONFIRMATION_PHRASE,
    LIVE_ENABLED_ENV_KEY,
)

ROOT = Path(__file__).resolve().parents[1]


def _approved_live_readiness_env_lines() -> list[str]:
    return [
        "KIS_LIVE_APP_KEY=live-app-key",
        "KIS_LIVE_APP_SECRET=live-app-secret",
        "KIS_LIVE_ACCOUNT_NO=test-live-account",
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
        f"{LIVE_ALLOW_ENV_KEY}=true",
        f"{LIVE_ENABLED_ENV_KEY}=true",
        f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}",
        f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=nt",
    ]


def _write_default_scanner_snapshot(root: Path) -> Path:
    scanner_path = root / "data" / "scanner_snapshot.json"
    scanner_path.parent.mkdir(parents=True, exist_ok=True)
    scanner_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}],
            }
        ),
        encoding="utf-8",
    )
    return scanner_path


class LiveReadinessCliTest(unittest.TestCase):
    def test_live_example_config_can_report_ready_with_local_live_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "provider": "external-file",
                        "candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "config.live.yaml"
            config_path.write_text(
                (ROOT / "config.live.example.yaml")
                .read_text(encoding="utf-8")
                .replace("scanner_snapshot_path: data/scanner_snapshot.json", f"scanner_snapshot_path: {scanner_path}"),
                encoding="utf-8",
            )
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["live_order_enabled"])
        self.assertFalse(payload["manual_reconciliation_cleared"])

    def test_reusable_readiness_check_never_enables_live_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            payload = run_live_readiness_check(config_path=config_path, env_file=env_path)

        self.assertTrue(payload["ready"])
        self.assertFalse(payload["live_order_enabled"])
        self.assertEqual([], payload["blockers"])

    def test_dashboard_live_readiness_values_allow_paper_safe_dashboard_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = _write_default_scanner_snapshot(tmp_path)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: paper",
                        "allow_live_trading: false",
                        "live_trading_enabled: false",
                        "allow_paper_short: true",
                        "market_data_source: local",
                        "scanner_source: local",
                        f"scanner_snapshot_path: {scanner_path}",
                        "journal_path: logs/trades.csv",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")

            payload = run_live_readiness_check(
                config_path=config_path,
                env_file=env_path,
                config_values=dashboard_live_readiness_config_values(load_config(config_path)),
            )

        self.assertTrue(payload["ready"])
        self.assertEqual([], payload["blockers"])
        self.assertFalse(payload["live_order_enabled"])

    def test_live_readiness_requires_full_live_order_gate_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            env_path.write_text(
                "\n".join(
                    [
                        line
                        for line in _approved_live_readiness_env_lines()
                        if not line.startswith(f"{LIVE_ACCOUNT_CONFIRMATION_ENV_KEY}=")
                    ]
                ),
                encoding="utf-8",
            )

            payload = run_live_readiness_check(config_path=config_path, env_file=env_path)

        self.assertFalse(payload["ready"])
        self.assertTrue(any(LIVE_ACCOUNT_CONFIRMATION_ENV_KEY in blocker for blocker in payload["blockers"]))

    def test_live_readiness_does_not_fallback_to_process_environment_when_env_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            env_path.write_text("", encoding="utf-8")
            env_updates = {
                "KIS_LIVE_APP_KEY": "process-live-key",
                "KIS_LIVE_APP_SECRET": "process-live-secret",
                "KIS_LIVE_ACCOUNT_NO": "12345678",
                "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
                LIVE_ALLOW_ENV_KEY: "true",
                LIVE_ENABLED_ENV_KEY: "true",
                "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
                LIVE_ACCOUNT_CONFIRMATION_ENV_KEY: "78",
            }
            previous_env = {key: os.environ.get(key) for key in env_updates}
            os.environ.update(env_updates)
            try:
                payload = run_live_readiness_check(config_path=config_path, env_file=env_path)
            finally:
                for key, value in previous_env.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

        self.assertFalse(payload["ready"])
        self.assertTrue(any("missing KIS live credentials" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_blocks_when_manual_reconciliation_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            scope = managed_live_position_ledger_scope("test-live-account", "01")
            store = JsonManualReconciliationStore(
                tmp_path / "logs" / f"live_manual_reconciliation_required_{scope}.json",
                scope=scope,
            )
            store.latch(
                ManualReconciliationBlocker(
                    reason="pending_order_store_update_failed",
                    symbol="005930",
                    side="BUY",
                    quantity=1,
                    order_no="KIS123",
                    created_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertFalse(payload["manual_reconciliation_cleared"])
        self.assertTrue(
            any("manual live account reconciliation is required" in blocker for blocker in payload["blockers"])
        )

    def test_live_readiness_blocks_when_manual_pending_order_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            scope = managed_live_position_ledger_scope("test-live-account", "01")
            pending_store = JsonPendingLiveOrderStore(
                tmp_path / "logs" / f"pending_live_orders_{scope}.json",
                scope=scope,
            )
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="manual:20260703T090100:005930:BUY",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                    estimated_price=Decimal("70000"),
                    reason="submitted_without_order_no",
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(
            any("manual pending live order requires reconciliation" in blocker for blocker in payload["blockers"])
        )

    def test_live_readiness_blocks_when_broker_pending_order_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            scope = managed_live_position_ledger_scope("test-live-account", "01")
            pending_store = JsonPendingLiveOrderStore(
                tmp_path / "logs" / f"pending_live_orders_{scope}.json",
                scope=scope,
            )
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="KIS123456789",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=2,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                    estimated_price=Decimal("70000"),
                    reason="submitted",
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(
            any("pending live order requires reconciliation before live readiness" in blocker for blocker in payload["blockers"])
        )

    def test_live_readiness_can_clear_manual_reconciliation_with_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            scope = managed_live_position_ledger_scope("test-live-account", "01")
            store = JsonManualReconciliationStore(
                tmp_path / "logs" / f"live_manual_reconciliation_required_{scope}.json",
                scope=scope,
            )
            store.latch(
                ManualReconciliationBlocker(
                    reason="pending_order_store_update_failed",
                    symbol="005930",
                    side="BUY",
                    quantity=1,
                    order_no="KIS123",
                    created_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                )
            )
            pending_store = JsonPendingLiveOrderStore(
                tmp_path / "logs" / f"pending_live_orders_{scope}.json",
                scope=scope,
            )
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="manual:20260703T090100:005930:BUY",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                    estimated_price=Decimal("70000"),
                    reason="submitted_without_order_no",
                )
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--config",
                        str(config_path),
                        "--env-file",
                        str(env_path),
                        "--clear-manual-reconciliation",
                        MANUAL_RECONCILIATION_CLEAR_PHRASE,
                    ]
                )
            pending_after_clear = pending_store.all()
            audit_rows = [
                json.loads(line)
                for line in (tmp_path / "logs" / "live_orders.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["manual_reconciliation_cleared"])
        self.assertEqual([], payload["blockers"])
        self.assertEqual((), pending_after_clear)
        audit_events = [row["event"] for row in audit_rows]
        self.assertIn("live_manual_reconciliation_clear_requested", audit_events)
        self.assertIn("live_manual_reconciliation_cleared_by_operator", audit_events)
        rendered_audit = json.dumps(audit_rows, ensure_ascii=False)
        self.assertNotIn("live-app-key", rendered_audit)
        self.assertNotIn("live-app-secret", rendered_audit)
        self.assertNotIn("test-live-account", rendered_audit)

    def test_manual_reconciliation_clear_restores_fail_closed_state_when_final_audit_fails(self):
        class FailingFinalAuditLog:
            def __init__(self):
                self.events: list[str] = []

            def record(self, event, payload):
                self.events.append(event)
                if event == "live_manual_reconciliation_cleared_by_operator":
                    raise RuntimeError("audit write failed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            scope = managed_live_position_ledger_scope("test-live-account", "01")
            store = JsonManualReconciliationStore(
                tmp_path / "logs" / f"live_manual_reconciliation_required_{scope}.json",
                scope=scope,
            )
            blocker = ManualReconciliationBlocker(
                reason="pending_order_store_update_failed",
                symbol="005930",
                side="BUY",
                quantity=1,
                order_no="KIS123",
                created_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
            )
            store.latch(blocker)
            pending_store = JsonPendingLiveOrderStore(
                tmp_path / "logs" / f"pending_live_orders_{scope}.json",
                scope=scope,
            )
            pending_order = PendingLiveOrder(
                order_no="manual:20260703T090100:005930:BUY",
                symbol="005930",
                side="BUY",
                requested_quantity=1,
                remaining_quantity=1,
                submitted_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
                estimated_price=Decimal("70000"),
                reason="submitted_without_order_no",
            )
            pending_store.upsert(pending_order)
            audit_log = FailingFinalAuditLog()

            with patch("stockbot.live_readiness_cli._live_readiness_audit_log", return_value=audit_log):
                with self.assertRaises(RuntimeError):
                    run_live_readiness_check(
                        config_path=config_path,
                        env_file=env_path,
                        clear_manual_reconciliation=MANUAL_RECONCILIATION_CLEAR_PHRASE,
                    )

            self.assertEqual(blocker, store.blocker())
            self.assertEqual((pending_order,), pending_store.all())
            self.assertEqual(
                [
                    "live_manual_reconciliation_clear_requested",
                    "live_manual_reconciliation_cleared_by_operator",
                ],
                audit_log.events,
            )

    def test_live_readiness_rejects_wrong_manual_reconciliation_clear_confirmation(self):
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            exit_code = main(
                [
                    "--config",
                    "config.example.yaml",
                    "--env-file",
                    ".env",
                    "--clear-manual-reconciliation",
                    "wrong",
                ]
            )

        payload = json.loads(stderr.getvalue())
        self.assertEqual(2, exit_code)
        self.assertFalse(payload["ready"])
        self.assertIn("manual reconciliation clear confirmation is invalid", payload["error"])

    def test_live_readiness_requires_configured_scanner_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "scanner_snapshot_path: data/scanner_snapshot.json",
                        "scanner_snapshot_max_age_seconds: 300",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(any("scanner_snapshot_path is usable by runtime" in blocker for blocker in payload["blockers"]))
        self.assertTrue(any("FileNotFoundError" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_checks_default_external_scan_snapshot_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: external-scan-kis",
                        "scanner_source: json",
                        "scanner_snapshot_max_age_seconds: 300",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(any("data/scanner_snapshot.json" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_checks_default_live_runtime_snapshot_path_for_minimal_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")

            payload = run_live_readiness_check(config_path=config_path, env_file=env_path)

        self.assertFalse(payload["ready"])
        self.assertTrue(any("data/scanner_snapshot.json" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_checks_default_snapshot_for_local_scanner_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        "scanner_source: local",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")

            payload = run_live_readiness_check(config_path=config_path, env_file=env_path)

        self.assertFalse(payload["ready"])
        self.assertTrue(any("data/scanner_snapshot.json" in blocker for blocker in payload["blockers"]))

    def test_refresh_scanner_snapshot_uses_default_path_for_local_scanner_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: local",
                        "scanner_source: local",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")
            refresh_calls = []

            def fake_refresh(output_path, options=None, **kwargs):
                refresh_calls.append((Path(output_path), options, kwargs))
                _write_default_scanner_snapshot(tmp_path)
                return 1

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", side_effect=fake_refresh):
                payload = run_live_readiness_check(
                    config_path=config_path,
                    env_file=env_path,
                    refresh_scanner_snapshot=True,
                )

        self.assertTrue(payload["ready"])
        self.assertTrue(payload["scanner_snapshot_refreshed"])
        self.assertEqual(1, len(refresh_calls))
        self.assertEqual(tmp_path / "data" / "scanner_snapshot.json", refresh_calls[0][0])
        self.assertEqual(128, refresh_calls[0][2].get("minute_history_candidates"))
        self.assertEqual(8, refresh_calls[0][2].get("minute_history_workers"))
        self.assertEqual(2.0, refresh_calls[0][2].get("minute_history_timeout"))

    def test_live_readiness_rejects_stale_scanner_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps({"candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}]}),
                encoding="utf-8",
            )
            old_time = time.time() - 600
            os.utime(scanner_path, (old_time, old_time))
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                    ]
                ),
                encoding="utf-8",
            )
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(any("stale scanner snapshot" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_rejects_stale_scanner_snapshot_metadata_even_when_file_mtime_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2000-01-01T00:00:00+00:00",
                        "candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}],
                    }
                ),
                encoding="utf-8",
            )
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertTrue(any("stale scanner snapshot" in blocker for blocker in payload["blockers"]))

    def test_live_readiness_uses_snapshot_metadata_before_file_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "scanner_snapshot.json"
            scanner_path.write_text(
                json.dumps(
                    {
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}],
                    }
                ),
                encoding="utf-8",
            )
            old_time = time.time() - 600
            os.utime(scanner_path, (old_time, old_time))
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ready"])

    def test_live_readiness_can_refresh_scanner_snapshot_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "data" / "scanner_snapshot.json"
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                        "initial_cash: 1000000",
                        "max_positions: 10",
                        "kis_market_data_scan_limit: 10",
                        "max_position_amount: 300000",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def fake_refresh(output_path, options=None, **kwargs):
                refresh_calls.append((Path(output_path), options, kwargs))
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_text(
                    json.dumps(
                        {
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "candidates": [{"symbol": "005930", "price": "70000", "volume": 1000000}],
                        }
                    ),
                    encoding="utf-8",
                )
                return 1

            stdout = io.StringIO()
            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", side_effect=fake_refresh):
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(
                        [
                            "--config",
                            str(config_path),
                            "--env-file",
                            str(env_path),
                            "--refresh-scanner-snapshot",
                        ]
                    )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ready"])
        self.assertTrue(payload["scanner_snapshot_refreshed"])
        self.assertEqual(1, len(refresh_calls))
        refreshed_path, options, kwargs = refresh_calls[0]
        self.assertEqual(scanner_path, refreshed_path)
        self.assertEqual(Decimal("300000"), options.max_price)
        self.assertEqual(("all",), kwargs["markets"])

    def test_refresh_scanner_snapshot_uses_position_risk_cap_without_slot_division(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "data" / "scanner_snapshot.json"
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: external-scan-kis",
                        "scanner_source: json",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                        "initial_cash: 1000000",
                        "max_positions: 0",
                        "scan_limit_per_cycle: 20",
                        "kis_market_data_scan_limit: 2",
                        "max_position_amount: 300000",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")
            refresh_calls = []

            def fake_refresh(output_path, options=None, **kwargs):
                refresh_calls.append((Path(output_path), options, kwargs))
                _write_default_scanner_snapshot(tmp_path)
                return 1

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", side_effect=fake_refresh):
                payload = run_live_readiness_check(
                    config_path=config_path,
                    env_file=env_path,
                    refresh_scanner_snapshot=True,
                )

        self.assertTrue(payload["scanner_snapshot_refreshed"])
        self.assertEqual(1, len(refresh_calls))
        self.assertEqual(Decimal("300000"), refresh_calls[0][1].max_price)

    def test_refresh_scanner_snapshot_ignores_legacy_strategy_and_budget_yaml_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            scanner_path = tmp_path / "data" / "scanner_snapshot.json"
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                        "market_data_source: external-scan-kis",
                        "scanner_source: json",
                        "strategy_profile: aggressive",
                        "cash_allocation_pct: 0.01",
                        "order_cash_amount: 1",
                        f"scanner_snapshot_path: {scanner_path}",
                        "scanner_snapshot_max_age_seconds: 300",
                        "initial_cash: 1000000",
                        "max_position_amount: 300000",
                        "max_positions: 0",
                        "scan_limit_per_cycle: 20",
                    ]
                ),
                encoding="utf-8",
            )
            env_path.write_text("\n".join(_approved_live_readiness_env_lines()), encoding="utf-8")
            refresh_calls = []

            def fake_refresh(output_path, options=None, **kwargs):
                refresh_calls.append((Path(output_path), options, kwargs))
                _write_default_scanner_snapshot(tmp_path)
                return 1

            with patch("stockbot.live_readiness_cli.collect_naver_market_scanner_snapshot", side_effect=fake_refresh):
                payload = run_live_readiness_check(
                    config_path=config_path,
                    env_file=env_path,
                    refresh_scanner_snapshot=True,
                )

        self.assertTrue(payload["scanner_snapshot_refreshed"])
        self.assertEqual(1, len(refresh_calls))
        self.assertEqual(Decimal("300000"), refresh_calls[0][1].max_price)

    def test_cli_reports_static_live_readiness_without_placing_orders(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
            config_path.write_text(
                "\n".join(
                    [
                        "trading_mode: live",
                        "allow_live_trading: true",
                        "live_trading_enabled: true",
                        "allow_paper_short: false",
                    ]
                ),
                encoding="utf-8",
            )
            _write_default_scanner_snapshot(tmp_path)
            env_path.write_text(
                "\n".join(
                    [
                        *_approved_live_readiness_env_lines(),
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ready"])
        self.assertFalse(payload["live_order_enabled"])
        self.assertEqual([], payload["blockers"])
        self.assertIn("This command never places orders", payload["note"])

    def test_cli_prefers_saved_env_file_over_process_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.yaml"
            env_path = tmp_path / ".env"
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
            env_path.write_text(
                "\n".join(
                    [
                        "KIS_LIVE_APP_KEY=file-live-key",
                        "KIS_LIVE_APP_SECRET=file-live-secret",
                        "KIS_LIVE_ACCOUNT_NO=file-live-account",
                        "KIS_LIVE_ACCOUNT_PRODUCT_CODE=01",
                    ]
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with patch.dict(os.environ, {"STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE}):
                with contextlib.redirect_stdout(stdout):
                    exit_code = main(["--config", str(config_path), "--env-file", str(env_path)])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertIn(f"STOCKBOT_LIVE_TRADING_CONFIRM={LIVE_CONFIRMATION_PHRASE}", payload["blockers"])

    def test_cli_errors_are_redacted(self):
        stderr = io.StringIO()

        with patch(
            "stockbot.live_readiness_cli.read_env_file",
            side_effect=RuntimeError(
                "KIS_LIVE_APP_SECRET=sekret Bearer token-123 "
                "appsecret=secret-456 appkey=key-789 access_token=abc.def account=87654321-01"
            ),
        ):
            with contextlib.redirect_stderr(stderr):
                exit_code = main(["--config", "config.example.yaml", "--env-file", ".env"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(2, exit_code)
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("sekret", rendered)
        self.assertNotIn("token-123", rendered)
        self.assertNotIn("secret-456", rendered)
        self.assertNotIn("key-789", rendered)
        self.assertNotIn("abc.def", rendered)
        self.assertNotIn("87654321", rendered)
        self.assertIn("[REDACTED]", rendered)

if __name__ == "__main__":
    unittest.main()
