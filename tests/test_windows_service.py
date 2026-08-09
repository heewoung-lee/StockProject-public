from __future__ import annotations

import json
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.live_safety import (
    live_credential_scope_fingerprint,
    read_env_file,
)
from stockbot.windows_service import (
    DEFAULT_CYCLE_INTERVAL_SECONDS,
    SERVICE_NAME,
    StockBotServiceApplication,
    WindowsServiceConfig,
    create_service_config,
    main,
)


def write_live_env(path: Path) -> dict[str, str]:
    values = {
        "KIS_LIVE_APP_KEY": "service-app-key",
        "KIS_LIVE_APP_SECRET": "service-app-secret",
        "KIS_LIVE_ACCOUNT_NO": "12345678",
        "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
    }
    path.write_text(
        "".join(f"{key}={value}\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return values


class FakeController:
    def __init__(self, env_file: Path):
        self.env_file = str(env_file)
        self.state = SimpleNamespace(trading_mode="virtual")
        self._runtime_running = False
        self.services = SimpleNamespace(
            kis_market_status=lambda: SimpleNamespace(
                is_open=False,
                label="장 대기",
                message="정규장이 아닙니다.",
            )
        )
        self.mode_calls: list[str] = []
        self.live_probe_revision = 0

    def select_trading_mode(self, mode: str):
        self.mode_calls.append(mode)
        self.state.trading_mode = mode

    def run_kis_live_check(self, *, activate_real_mode: bool = True):
        self.state.trading_mode = "real"
        self.live_probe_revision += 1

    def live_account_scope_verified(self):
        return True

    def live_account_probe_revision(self):
        return self.live_probe_revision

    def run_persistent_live_account_probe(self):
        revision_before = self.live_probe_revision
        self.run_kis_live_check(activate_real_mode=True)
        return self.live_probe_revision > revision_before

    def start_paper_runtime(self):
        self._runtime_running = True
        return self.state

    def run_paper_cycle(self):
        return self.state

    def pause_paper_runtime(self):
        self._runtime_running = False
        return self.state


class FakeServer:
    def __init__(self):
        self.server_address = ("127.0.0.1", 43123)
        self.bridge_token = "t" * 43
        self.closed = False
        self.shutdown_called = False
        self.serve_calls = 0

    def serve_forever(self):
        self.serve_calls += 1
        return None

    def shutdown(self):
        self.shutdown_called = True

    def server_close(self):
        self.closed = True


class FakeProcessLock:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class SlowSchedulerThread:
    daemon = False

    def __init__(self):
        self.join_entered = threading.Event()
        self.release = threading.Event()

    def join(self, timeout=None):
        self.join_entered.set()
        self.release.wait(timeout=timeout)

    def is_alive(self):
        return not self.release.is_set()


class BlockingServer(FakeServer):
    def __init__(self):
        super().__init__()
        self.serving = threading.Event()
        self.stopped = threading.Event()

    def serve_forever(self):
        self.serve_calls += 1
        self.serving.set()
        self.stopped.wait(timeout=2)

    def shutdown(self):
        self.shutdown_called = True
        self.stopped.set()


class WindowsServiceConfigTest(unittest.TestCase):
    def setUp(self):
        process_lock_patch = patch(
            "stockbot.windows_service.acquire_persistent_live_process_lock",
            side_effect=FakeProcessLock,
        )
        process_lock_patch.start()
        self.addCleanup(process_lock_patch.stop)

    def test_default_cycle_interval_is_fifteen_seconds(self):
        self.assertEqual(15.0, DEFAULT_CYCLE_INTERVAL_SECONDS)

    def test_configure_stores_only_paths_policy_and_credential_fingerprint(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            secrets = write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            session_path = root / "bridge-session.json"

            config = create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=session_path,
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )

            raw = service_config_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
            self.assertEqual(2, payload["schemaVersion"])
            self.assertTrue(payload["liveOrdersAuthorized"])
            self.assertFalse(payload["credentialBindingPending"])
            self.assertEqual(64, len(payload["credentialScopeFingerprint"]))
            self.assertEqual(str(root.resolve()), payload["projectRoot"])
            self.assertEqual(str(session_path.resolve()), payload["sessionFile"])
            self.assertNotIn("token", raw.lower())
            for env_key in secrets:
                self.assertNotIn(env_key, raw)
            for secret_name in (
                "KIS_LIVE_APP_KEY",
                "KIS_LIVE_APP_SECRET",
                "KIS_LIVE_ACCOUNT_NO",
            ):
                self.assertNotIn(secrets[secret_name], raw)
            self.assertEqual(config, WindowsServiceConfig.load(service_config_path))
            payload["liveOrdersAuthorized"] = False
            service_config_path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "explicit live order authorization"):
                WindowsServiceConfig.load(service_config_path)

    def test_schema_one_config_loads_as_an_existing_bound_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            payload = {
                "schemaVersion": 1,
                "projectRoot": str(root.resolve()),
                "configPath": str(config_path.resolve()),
                "envFile": str(env_file.resolve()),
                "sessionFile": str((root / "bridge-session.json").resolve()),
                "cycleIntervalSeconds": 15,
                "credentialScopeFingerprint": "a" * 64,
                "liveOrdersAuthorized": True,
            }
            service_config_path.write_text(json.dumps(payload), encoding="utf-8")

            config = WindowsServiceConfig.load(service_config_path)

            self.assertFalse(config.credential_binding_pending)
            migrated = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertEqual(2, migrated["schemaVersion"])
            self.assertFalse(migrated["credentialBindingPending"])
            self.assertEqual("a" * 64, migrated["credentialScopeFingerprint"])
            restarted = WindowsServiceConfig.load(service_config_path)
            self.assertEqual(config, restarted)

    def test_schema_one_migration_failure_preserves_original_config_bytes(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            payload = {
                "schemaVersion": 1,
                "projectRoot": str(root.resolve()),
                "configPath": str(config_path.resolve()),
                "envFile": str(env_file.resolve()),
                "sessionFile": str((root / "bridge-session.json").resolve()),
                "cycleIntervalSeconds": 15,
                "credentialScopeFingerprint": "a" * 64,
                "liveOrdersAuthorized": True,
            }
            service_config_path.write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            original = service_config_path.read_bytes()

            with patch(
                "stockbot.windows_service.os.replace",
                side_effect=OSError("migration blocked"),
            ):
                with self.assertRaisesRegex(OSError, "migration blocked"):
                    WindowsServiceConfig.load(service_config_path)

            self.assertEqual(original, service_config_path.read_bytes())

    def test_configure_allows_opt_in_credential_bootstrap_with_an_empty_env(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"

            config = create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                authorize_live_orders=True,
                allow_credential_bootstrap=True,
            )

            self.assertTrue(env_file.is_file())
            self.assertEqual("", env_file.read_text(encoding="utf-8"))
            self.assertTrue(config.credential_binding_pending)
            self.assertEqual("", config.credential_scope_fingerprint)
            payload = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertEqual(2, payload["schemaVersion"])
            self.assertTrue(payload["credentialBindingPending"])
            self.assertEqual("", payload["credentialScopeFingerprint"])
            self.assertEqual(config, WindowsServiceConfig.load(service_config_path))

    def test_configure_does_not_create_a_missing_env_without_bootstrap(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "credential fields"):
                create_service_config(
                    service_config_path=root / "service.json",
                    project_root=root,
                    config_path=config_path,
                    env_file=env_file,
                    session_file=root / "bridge-session.json",
                    authorize_live_orders=True,
                )

            self.assertFalse(env_file.exists())

    def test_schema_two_requires_consistent_explicit_binding_state(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                authorize_live_orders=True,
            )
            payload = json.loads(service_config_path.read_text(encoding="utf-8"))

            without_state = dict(payload)
            without_state.pop("credentialBindingPending")
            service_config_path.write_text(
                json.dumps(without_state),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "binding state"):
                WindowsServiceConfig.load(service_config_path)

            pending_with_fingerprint = dict(payload)
            pending_with_fingerprint["credentialBindingPending"] = True
            service_config_path.write_text(
                json.dumps(pending_with_fingerprint),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "must be empty"):
                WindowsServiceConfig.load(service_config_path)

            bound_without_fingerprint = dict(payload)
            bound_without_fingerprint["credentialScopeFingerprint"] = ""
            service_config_path.write_text(
                json.dumps(bound_without_fingerprint),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "fingerprint is invalid"):
                WindowsServiceConfig.load(service_config_path)

    def test_configure_rejects_missing_live_credentials(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            env_file.write_text("KIS_LIVE_APP_KEY=only-key\n", encoding="utf-8")
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                create_service_config(
                    service_config_path=root / "service.json",
                    project_root=root,
                    config_path=config_path,
                    env_file=env_file,
                    session_file=root / "bridge-session.json",
                    cycle_interval_seconds=60,
                    authorize_live_orders=True,
                )

    def test_configure_requires_explicit_persistent_live_order_authorization(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "explicit live order authorization"):
                create_service_config(
                    service_config_path=root / "service.json",
                    project_root=root,
                    config_path=config_path,
                    env_file=env_file,
                    session_file=root / "bridge-session.json",
                    cycle_interval_seconds=60,
                )

    def test_service_application_uses_absolute_paths_and_writes_ephemeral_bridge_session(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            session_path = root / "bridge-session.json"
            config = create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=session_path,
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            controller = FakeController(env_file)
            server = FakeServer()

            with (
                patch("stockbot.windows_service.create_default_controller", return_value=controller) as controller_factory,
                patch("stockbot.windows_service.create_bridge_server", return_value=server) as server_factory,
            ):
                application = StockBotServiceApplication(config)
                application.prepare()

            controller_factory.assert_called_once_with(
                config_path=str(config_path.resolve()),
                env_file=str(env_file.resolve()),
            )
            self.assertEqual(["real"], controller.mode_calls)
            self.assertEqual("service", server_factory.call_args.kwargs["scheduler_owner"])
            self.assertTrue(server_factory.call_args.kwargs["persistent_real_mode"])
            self.assertFalse(application.scheduler_thread.daemon)
            payload = json.loads(session_path.read_text(encoding="utf-8"))
            self.assertEqual(1, payload["schemaVersion"])
            self.assertEqual("http://127.0.0.1:43123", payload["url"])
            self.assertEqual("t" * 43, payload["token"])

            application.close()

            self.assertFalse(session_path.exists())
            self.assertTrue(server.closed)

    def test_cli_configure_does_not_print_secrets(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            secrets = write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")

            with patch("sys.stdout") as stdout:
                exit_code = main(
                    [
                        "configure",
                        "--service-config",
                        str(root / "service.json"),
                        "--project-root",
                        str(root),
                        "--config",
                        str(config_path),
                        "--env-file",
                        str(env_file),
                        "--session-file",
                        str(root / "bridge-session.json"),
                        "--authorize-live-orders",
                    ]
                )

            self.assertEqual(0, exit_code)
            payload = json.loads((root / "service.json").read_text(encoding="utf-8"))
            self.assertEqual(15.0, payload["cycleIntervalSeconds"])
            rendered = "".join(str(call) for call in stdout.write.call_args_list)
            self.assertIn('"ok": true', rendered.lower())
            for secret in secrets.values():
                self.assertNotIn(secret, rendered)

    def test_cli_configure_accepts_explicit_cycle_interval(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"

            exit_code = main(
                [
                    "configure",
                    "--service-config",
                    str(service_config_path),
                    "--project-root",
                    str(root),
                    "--config",
                    str(config_path),
                    "--env-file",
                    str(env_file),
                    "--session-file",
                    str(root / "bridge-session.json"),
                    "--cycle-interval-seconds",
                    "30",
                    "--authorize-live-orders",
                ]
            )

            self.assertEqual(0, exit_code)
            payload = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertEqual(30.0, payload["cycleIntervalSeconds"])

    def test_cli_configure_accepts_opt_in_credential_bootstrap(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"

            exit_code = main(
                [
                    "configure",
                    "--service-config",
                    str(service_config_path),
                    "--project-root",
                    str(root),
                    "--config",
                    str(config_path),
                    "--env-file",
                    str(env_file),
                    "--session-file",
                    str(root / "bridge-session.json"),
                    "--authorize-live-orders",
                    "--allow-credential-bootstrap",
                ]
            )

            self.assertEqual(0, exit_code)
            payload = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["credentialBindingPending"])
            self.assertEqual("", payload["credentialScopeFingerprint"])
            self.assertTrue(env_file.is_file())

    def test_service_name_is_stable_for_scm_registration(self):
        self.assertEqual("StockBotLive", SERVICE_NAME)

    def test_prepare_failure_releases_the_single_scheduler_process_lock(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            config = create_service_config(
                service_config_path=root / "service.json",
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            process_lock = FakeProcessLock()
            config.session_file.write_text("active-service-session", encoding="utf-8")

            with (
                patch(
                    "stockbot.windows_service.acquire_persistent_live_process_lock",
                    return_value=process_lock,
                ),
                patch(
                    "stockbot.windows_service.create_default_controller",
                    side_effect=RuntimeError("startup failed"),
                ),
            ):
                application = StockBotServiceApplication(config)
                with self.assertRaisesRegex(RuntimeError, "startup failed"):
                    application.prepare()

            self.assertTrue(process_lock.closed)
            self.assertEqual(
                "active-service-session",
                config.session_file.read_text(encoding="utf-8"),
            )

    def test_pending_service_runs_bridge_without_market_or_kis_and_binds_first_save(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            config = create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                authorize_live_orders=True,
                allow_credential_bootstrap=True,
            )
            controller = FakeController(env_file)

            def forbidden_market_status():
                raise AssertionError("pending bootstrap must not query market status")

            def forbidden_live_probe():
                raise AssertionError("pending bootstrap must not call KIS")

            controller.services = SimpleNamespace(
                kis_market_status=forbidden_market_status
            )
            controller.run_persistent_live_account_probe = forbidden_live_probe

            with (
                patch(
                    "stockbot.windows_service.create_default_controller",
                    return_value=controller,
                ),
                patch(
                    "stockbot.windows_service.create_bridge_server",
                    return_value=FakeServer(),
                ),
            ):
                application = StockBotServiceApplication(config)
                application.prepare()
                self.assertIsNotNone(application.scheduler)
                pending = application.scheduler.run_once()
                with self.assertRaisesRegex(ValueError, "fingerprint is invalid"):
                    application.scheduler.bind_saved_credential_scope("a" * 64)
                write_live_env(env_file)
                candidate = live_credential_scope_fingerprint(
                    read_env_file(env_file)
                )
                application.scheduler.validate_candidate_credential_scope(
                    candidate
                )
                bound = application.scheduler.bind_saved_credential_scope(
                    candidate
                )

            self.assertEqual("credential_scope_pending", pending.action)
            self.assertTrue(bound)
            persisted = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertEqual(2, persisted["schemaVersion"])
            self.assertFalse(persisted["credentialBindingPending"])
            self.assertEqual(64, len(persisted["credentialScopeFingerprint"]))
            self.assertFalse(application.config.credential_binding_pending)
            application.close()

    def test_service_binding_callback_rechecks_disk_before_persisting_scope(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            service_config_path = root / "service.json"
            config = create_service_config(
                service_config_path=service_config_path,
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                authorize_live_orders=True,
                allow_credential_bootstrap=True,
            )
            controller = FakeController(env_file)

            with (
                patch(
                    "stockbot.windows_service.create_default_controller",
                    return_value=controller,
                ),
                patch(
                    "stockbot.windows_service.create_bridge_server",
                    return_value=FakeServer(),
                ),
            ):
                application = StockBotServiceApplication(config)
                application.prepare()

            candidate_values = write_live_env(env_file)
            candidate = live_credential_scope_fingerprint(candidate_values)
            different_values = dict(candidate_values)
            different_values["KIS_LIVE_ACCOUNT_NO"] = "87654321"

            def racing_provider():
                env_file.write_text(
                    "".join(
                        f"{key}={value}\n"
                        for key, value in different_values.items()
                    ),
                    encoding="utf-8",
                )
                return candidate

            application.scheduler.credential_fingerprint_provider = racing_provider
            with self.assertRaisesRegex(RuntimeError, "persistence failed"):
                application.scheduler.bind_saved_credential_scope(candidate)

            persisted = json.loads(service_config_path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["credentialBindingPending"])
            self.assertEqual("", persisted["credentialScopeFingerprint"])
            self.assertTrue(application.config.credential_binding_pending)
            application.close()

    def test_stop_requested_after_prepare_prevents_server_from_starting(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            config = create_service_config(
                service_config_path=root / "service.json",
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            controller = FakeController(env_file)
            server = FakeServer()

            with (
                patch("stockbot.windows_service.create_default_controller", return_value=controller),
                patch("stockbot.windows_service.create_bridge_server", return_value=server),
            ):
                application = StockBotServiceApplication(config)
                application.prepare()
                application.request_stop()
                application.run()

            self.assertEqual(0, server.serve_calls)
            self.assertTrue(server.closed)
            self.assertFalse(config.session_file.exists())

    def test_running_service_stop_request_unblocks_server_without_deadlock(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            config = create_service_config(
                service_config_path=root / "service.json",
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            controller = FakeController(env_file)
            server = BlockingServer()

            with (
                patch("stockbot.windows_service.create_default_controller", return_value=controller),
                patch("stockbot.windows_service.create_bridge_server", return_value=server),
            ):
                application = StockBotServiceApplication(config)
                worker = threading.Thread(target=application.run)
                worker.start()
                self.assertTrue(server.serving.wait(timeout=1))

                stopper = threading.Thread(target=application.request_stop)
                stopper.start()
                stopper.join(timeout=1)
                worker.join(timeout=1)

            self.assertFalse(stopper.is_alive())
            self.assertFalse(worker.is_alive())
            self.assertTrue(server.shutdown_called)
            self.assertFalse(config.session_file.exists())

    def test_close_pauses_runtime_after_scheduler_thread_has_finished(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            config = create_service_config(
                service_config_path=root / "service.json",
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            controller = FakeController(env_file)
            controller._runtime_running = True

            with (
                patch("stockbot.windows_service.create_default_controller", return_value=controller),
                patch("stockbot.windows_service.create_bridge_server", return_value=FakeServer()),
            ):
                application = StockBotServiceApplication(config)
                application.prepare()
                application.close()

            self.assertFalse(controller._runtime_running)

    def test_close_waits_for_scheduler_thread_before_releasing_process_lock(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_file = root / ".env"
            write_live_env(env_file)
            config_path = root / "config.live.yaml"
            config_path.write_text("trading_mode: live\n", encoding="utf-8")
            config = create_service_config(
                service_config_path=root / "service.json",
                project_root=root,
                config_path=config_path,
                env_file=env_file,
                session_file=root / "bridge-session.json",
                cycle_interval_seconds=60,
                authorize_live_orders=True,
            )
            process_lock = FakeProcessLock()
            shutdown_wait_reported = threading.Event()
            application = StockBotServiceApplication(
                config,
                shutdown_wait_callback=shutdown_wait_reported.set,
            )
            application.scheduler = SimpleNamespace(stop=lambda: None)
            scheduler_thread = SlowSchedulerThread()
            application.scheduler_thread = scheduler_thread
            application.server = FakeServer()
            application.process_lock = process_lock

            with patch("stockbot.windows_service.SERVICE_STOP_POLL_SECONDS", 0.01):
                worker = threading.Thread(target=application.close)
                worker.start()
                self.assertTrue(scheduler_thread.join_entered.wait(timeout=1))
                self.assertTrue(shutdown_wait_reported.wait(timeout=1))
                self.assertTrue(worker.is_alive())
                self.assertFalse(process_lock.closed)

                scheduler_thread.release.set()
                worker.join(timeout=1)

            self.assertFalse(worker.is_alive())
            self.assertTrue(process_lock.closed)
            self.assertIsNone(application.process_lock)


if __name__ == "__main__":
    unittest.main()
