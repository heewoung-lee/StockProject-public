from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Callable

from .electron_bridge import create_bridge_server
from .live_safety import (
    LIVE_KIS_ENV_KEYS,
    live_credential_scope_fingerprint,
    read_env_file,
)
from .persistent_live import PersistentLiveScheduler
from .persistent_process_lock import (
    PersistentLiveProcessLock,
    acquire_persistent_live_process_lock,
)
from .runtime_factory import create_default_controller


SERVICE_NAME = "StockBotLive"
SERVICE_DISPLAY_NAME = "StockBot Live Trading Service"
SERVICE_CONFIG_SCHEMA_VERSION = 2
SERVICE_SESSION_SCHEMA_VERSION = 1
DEFAULT_CYCLE_INTERVAL_SECONDS = 15.0
SERVICE_STOP_POLL_SECONDS = 1.0
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class WindowsServiceConfig:
    project_root: Path
    config_path: Path
    env_file: Path
    session_file: Path
    cycle_interval_seconds: float
    credential_scope_fingerprint: str
    live_orders_authorized: bool
    credential_binding_pending: bool = False
    source_path: Path | None = field(default=None, compare=False, repr=False)

    @classmethod
    def load(cls, path: str | Path) -> WindowsServiceConfig:
        config_file = Path(path)
        payload = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unsupported StockBot Windows service config schema")
        schema_version = payload.get("schemaVersion")
        if type(schema_version) is not int or schema_version not in {1, 2}:
            raise ValueError("unsupported StockBot Windows service config schema")
        if schema_version == 2:
            credential_binding_pending = payload.get("credentialBindingPending")
            if not isinstance(credential_binding_pending, bool):
                raise ValueError(
                    "StockBot service credential binding state is invalid"
                )
        else:
            credential_binding_pending = False
        config = cls(
            project_root=_absolute_path(payload.get("projectRoot"), "projectRoot"),
            config_path=_absolute_path(payload.get("configPath"), "configPath"),
            env_file=_absolute_path(payload.get("envFile"), "envFile"),
            session_file=_absolute_path(payload.get("sessionFile"), "sessionFile"),
            cycle_interval_seconds=float(payload.get("cycleIntervalSeconds", 0)),
            credential_scope_fingerprint=str(payload.get("credentialScopeFingerprint") or "").strip(),
            live_orders_authorized=payload.get("liveOrdersAuthorized") is True,
            credential_binding_pending=credential_binding_pending,
            source_path=config_file.resolve(),
        )
        config.validate()
        if schema_version == 1:
            _write_private_json(config_file.resolve(), config.to_payload())
        return config

    def validate(self) -> None:
        if not self.project_root.is_dir():
            raise ValueError("StockBot service project root does not exist")
        if not self.config_path.is_file():
            raise ValueError("StockBot service live config does not exist")
        if not self.env_file.is_file():
            raise ValueError("StockBot service env file does not exist")
        if not 5 <= self.cycle_interval_seconds <= 3600:
            raise ValueError("StockBot service cycle interval must be between 5 and 3600 seconds")
        if not isinstance(self.credential_binding_pending, bool):
            raise ValueError("StockBot service credential binding state is invalid")
        if self.credential_binding_pending:
            if self.credential_scope_fingerprint:
                raise ValueError(
                    "pending StockBot service credential scope must be empty"
                )
        elif not _FINGERPRINT_PATTERN.fullmatch(
            self.credential_scope_fingerprint
        ):
            raise ValueError(
                "StockBot service credential scope fingerprint is invalid"
            )
        if self.live_orders_authorized is not True:
            raise ValueError("StockBot service requires explicit live order authorization")

    def to_payload(self) -> dict[str, object]:
        return {
            "schemaVersion": SERVICE_CONFIG_SCHEMA_VERSION,
            "projectRoot": str(self.project_root),
            "configPath": str(self.config_path),
            "envFile": str(self.env_file),
            "sessionFile": str(self.session_file),
            "cycleIntervalSeconds": self.cycle_interval_seconds,
            "credentialScopeFingerprint": self.credential_scope_fingerprint,
            "liveOrdersAuthorized": self.live_orders_authorized,
            "credentialBindingPending": self.credential_binding_pending,
        }


def create_service_config(
    *,
    service_config_path: str | Path,
    project_root: str | Path,
    config_path: str | Path,
    env_file: str | Path,
    session_file: str | Path,
    cycle_interval_seconds: float = DEFAULT_CYCLE_INTERVAL_SECONDS,
    authorize_live_orders: bool = False,
    allow_credential_bootstrap: bool = False,
) -> WindowsServiceConfig:
    if authorize_live_orders is not True:
        raise ValueError("StockBot service requires explicit live order authorization")
    resolved_env_file = Path(env_file).resolve()
    env_values = read_env_file(resolved_env_file)
    missing = [
        env_key
        for env_key in LIVE_KIS_ENV_KEYS.values()
        if not str(env_values.get(env_key) or "").strip()
    ]
    credential_binding_pending = bool(missing)
    if credential_binding_pending and allow_credential_bootstrap is not True:
        raise ValueError("all KIS live credential fields must be saved before enabling the Windows service")
    if credential_binding_pending and not resolved_env_file.exists():
        _write_private_text(resolved_env_file, "")
    resolved_service_config_path = Path(service_config_path).resolve()
    config = WindowsServiceConfig(
        project_root=Path(project_root).resolve(),
        config_path=Path(config_path).resolve(),
        env_file=resolved_env_file,
        session_file=Path(session_file).resolve(),
        cycle_interval_seconds=float(cycle_interval_seconds),
        credential_scope_fingerprint=(
            ""
            if credential_binding_pending
            else live_credential_scope_fingerprint(env_values)
        ),
        live_orders_authorized=True,
        credential_binding_pending=credential_binding_pending,
        source_path=resolved_service_config_path,
    )
    config.validate()
    _write_private_json(resolved_service_config_path, config.to_payload())
    return config


class StockBotServiceApplication:
    def __init__(
        self,
        config: WindowsServiceConfig,
        *,
        shutdown_wait_callback: Callable[[], object] | None = None,
    ) -> None:
        self.config = config
        self.controller = None
        self.scheduler: PersistentLiveScheduler | None = None
        self.server = None
        self.scheduler_thread: Thread | None = None
        self.process_lock: PersistentLiveProcessLock | None = None
        self._prepared = False
        self._closed = False
        self._session_written = False
        self._stop_requested = Event()
        self._serve_started = Event()
        self._serve_lifecycle_lock = Lock()
        self._shutdown_wait_callback = shutdown_wait_callback

    def prepare(self) -> None:
        if self._prepared:
            return
        self.config.validate()
        self.process_lock = acquire_persistent_live_process_lock()
        try:
            controller = create_default_controller(
                config_path=str(self.config.config_path),
                env_file=str(self.config.env_file),
            )
            controller.select_trading_mode("real")
            scheduler = PersistentLiveScheduler(
                controller,
                interval_seconds=self.config.cycle_interval_seconds,
                expected_credential_fingerprint=self.config.credential_scope_fingerprint,
                credential_fingerprint_provider=lambda: (
                    _saved_live_credential_scope_fingerprint(
                        self.config.env_file
                    )
                ),
                credential_binding_pending=self.config.credential_binding_pending,
                credential_scope_persistence_callback=(
                    self._persist_credential_scope_binding
                ),
            )
            server = create_bridge_server(
                controller,
                host="127.0.0.1",
                port=0,
                token=secrets.token_urlsafe(32),
                allow_real_actions=False,
                scheduler_owner="service",
                scheduler_control=scheduler,
                persistent_real_mode=True,
            )
            self.controller = controller
            self.scheduler = scheduler
            self.server = server
            self.scheduler_thread = Thread(
                target=scheduler.run_forever,
                name="stockbot-windows-service-cycle",
                daemon=False,
            )
            self._write_bridge_session()
            self._prepared = True
        except Exception:
            self.close()
            raise

    def _persist_credential_scope_binding(self, fingerprint: str) -> None:
        if not _FINGERPRINT_PATTERN.fullmatch(fingerprint):
            raise ValueError("StockBot service credential scope fingerprint is invalid")
        current = self.config
        if not current.credential_binding_pending:
            if current.credential_scope_fingerprint == fingerprint:
                return
            raise PermissionError(
                "StockBot service credential scope changed; reinstall is required"
            )
        if current.source_path is None:
            raise RuntimeError("StockBot service config path is unavailable")
        if _saved_live_credential_scope_fingerprint(current.env_file) != fingerprint:
            raise PermissionError(
                "saved credential scope does not match the verified candidate"
            )
        bound = replace(
            current,
            credential_scope_fingerprint=fingerprint,
            credential_binding_pending=False,
        )
        bound.validate()
        _write_private_json(current.source_path, bound.to_payload())
        self.config = bound

    def run(self) -> None:
        try:
            if self._stop_requested.is_set():
                return
            self.prepare()
            assert self.scheduler_thread is not None
            assert self.server is not None
            if self._stop_requested.is_set():
                return
            self.scheduler_thread.start()
            with self._serve_lifecycle_lock:
                if self._stop_requested.is_set():
                    return
                self._serve_started.set()
            try:
                self.server.serve_forever()
            finally:
                with self._serve_lifecycle_lock:
                    self._serve_started.clear()
        finally:
            self.close()

    def request_stop(self) -> None:
        self._stop_requested.set()
        scheduler = self.scheduler
        server = self.server
        if scheduler is not None:
            scheduler.stop()
        with self._serve_lifecycle_lock:
            should_shutdown = server is not None and self._serve_started.is_set()
        if should_shutdown:
            server.shutdown()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.scheduler is not None:
            self.scheduler.stop()
        if self.scheduler_thread is not None and self.scheduler_thread.is_alive():
            while self.scheduler_thread.is_alive():
                self.scheduler_thread.join(timeout=SERVICE_STOP_POLL_SECONDS)
                if (
                    self.scheduler_thread.is_alive()
                    and self._shutdown_wait_callback is not None
                ):
                    try:
                        self._shutdown_wait_callback()
                    except Exception:
                        pass
        if self.controller is not None:
            pause_runtime = getattr(self.controller, "pause_paper_runtime", None)
            if callable(pause_runtime) and bool(
                getattr(self.controller, "_runtime_running", False)
            ):
                pause_runtime()
        if self.server is not None:
            self.server.server_close()
        if self._session_written:
            self.config.session_file.unlink(missing_ok=True)
            self._session_written = False
        if self.process_lock is not None:
            self.process_lock.close()
            self.process_lock = None

    def _write_bridge_session(self) -> None:
        if self.server is None:
            raise RuntimeError("StockBot service bridge is not prepared")
        host, port = self.server.server_address
        if str(host) != "127.0.0.1" or int(port) <= 0:
            raise RuntimeError("StockBot service bridge must bind to an ephemeral loopback port")
        payload = {
            "schemaVersion": SERVICE_SESSION_SCHEMA_VERSION,
            "url": f"http://127.0.0.1:{int(port)}",
            "token": str(self.server.bridge_token),
            "processId": os.getpid(),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_private_json(self.config.session_file, payload)
        self._session_written = True


def run_windows_service(service_config_path: str | Path) -> None:
    try:
        import servicemanager
        import win32service
        import win32serviceutil
    except ImportError as exc:
        raise RuntimeError("pywin32 is required to run StockBot as a Windows service") from exc

    resolved_config_path = str(Path(service_config_path).resolve())

    class StockBotWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = (
            "Runs the StockBot real-mode scheduler and waits outside Korean regular market hours."
        )

        def __init__(self, args):
            super().__init__(args)
            self.application: StockBotServiceApplication | None = None
            self._stop_requested = Event()

        def SvcStop(self):
            self._stop_requested.set()
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self.application is not None:
                self.application.request_stop()

        def SvcDoRun(self):
            self.application = StockBotServiceApplication(
                WindowsServiceConfig.load(resolved_config_path),
                shutdown_wait_callback=lambda: self.ReportServiceStatus(
                    win32service.SERVICE_STOP_PENDING,
                    waitHint=5000,
                ),
            )
            if self._stop_requested.is_set():
                self.application.request_stop()
            try:
                self.application.run()
            except Exception:
                servicemanager.LogErrorMsg(
                    f"{SERVICE_NAME} stopped after a sanitized startup or runtime failure."
                )
                raise

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(StockBotWindowsService)
    servicemanager.StartServiceCtrlDispatcher()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Configure or run the StockBot Windows service.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    configure = subparsers.add_parser("configure")
    configure.add_argument("--service-config", required=True)
    configure.add_argument("--project-root", required=True)
    configure.add_argument("--config", dest="config_path", required=True)
    configure.add_argument("--env-file", required=True)
    configure.add_argument("--session-file", required=True)
    configure.add_argument(
        "--cycle-interval-seconds",
        type=float,
        default=DEFAULT_CYCLE_INTERVAL_SECONDS,
    )
    configure.add_argument("--authorize-live-orders", action="store_true")
    configure.add_argument("--allow-credential-bootstrap", action="store_true")

    run_console = subparsers.add_parser("run-console")
    run_console.add_argument("--service-config", required=True)

    run_service = subparsers.add_parser("run-service")
    run_service.add_argument("--service-config", required=True)

    args = parser.parse_args(argv)
    if args.command == "configure":
        create_service_config(
            service_config_path=args.service_config,
            project_root=args.project_root,
            config_path=args.config_path,
            env_file=args.env_file,
            session_file=args.session_file,
            cycle_interval_seconds=args.cycle_interval_seconds,
            authorize_live_orders=args.authorize_live_orders,
            allow_credential_bootstrap=args.allow_credential_bootstrap,
        )
        print(json.dumps({"ok": True, "service": SERVICE_NAME}))
        return 0
    if args.command == "run-console":
        application = StockBotServiceApplication(
            WindowsServiceConfig.load(args.service_config)
        )
        try:
            application.run()
        except KeyboardInterrupt:
            application.request_stop()
        finally:
            application.close()
        return 0
    if args.command == "run-service":
        if os.name != "nt":
            raise RuntimeError("StockBot Windows service can only run on Windows")
        run_windows_service(args.service_config)
        return 0
    raise ValueError("unknown StockBot Windows service command")


def _absolute_path(value: Any, field_name: str) -> Path:
    path = Path(str(value or ""))
    if not path.is_absolute():
        raise ValueError(f"StockBot service {field_name} must be an absolute path")
    return path.resolve()


def _write_private_json(path: Path, payload: dict[str, object]) -> None:
    _write_private_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _saved_live_credential_scope_fingerprint(env_file: Path) -> str:
    env_values = read_env_file(env_file)
    if any(
        not str(env_values.get(env_key) or "").strip()
        for env_key in LIVE_KIS_ENV_KEYS.values()
    ):
        return ""
    return live_credential_scope_fingerprint(env_values)


if __name__ == "__main__":
    raise SystemExit(main())
