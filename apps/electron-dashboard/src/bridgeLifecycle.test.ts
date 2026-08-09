import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import { describe, expect, it, vi } from "vitest";

const require = createRequire(import.meta.url);
const {
  bridgeSessionFromEnvironment,
  bridgeSessionFromServicePayload,
  bridgeFailurePayloadForRenderer,
  bridgeLaunchPolicy,
  bridgePayloadForRenderer,
  createActiveBridgeProcessRegistry,
  createBridgeSessionSequencer,
  isActiveBridgeProcess,
  rendererOwnedBridgeArgs,
  shouldForceWindowsServiceAbsentForE2E,
  shouldLoadDevelopmentRenderer,
  uniqueBridgeCandidates,
  windowsServiceBridgeState,
  windowsServiceIdentityMatchesSession,
  windowsServiceIsRegistered,
  windowsServiceQueryArguments,
  windowsServiceQueryMatchesSession,
  windowsServiceRegistrationState,
  windowsServiceRequestFailureError,
  windowsServiceRuntimeState,
} = require("../electron/bridge_lifecycle.cjs");
const {
  createDesktopQuitLifecycle,
  shouldHideMainWindowOnClose,
} = require("../electron/window_lifecycle.cjs");

describe("Electron bridge lifecycle", () => {
  it("hides a Windows dashboard only when a working tray can restore it", () => {
    expect(
      shouldHideMainWindowOnClose({
        platform: "win32",
        isQuitting: false,
        trayAvailable: true,
      }),
    ).toBe(true);
    expect(
      shouldHideMainWindowOnClose({
        platform: "win32",
        isQuitting: true,
        trayAvailable: true,
      }),
    ).toBe(false);
    expect(
      shouldHideMainWindowOnClose({
        platform: "win32",
        isQuitting: false,
        trayAvailable: false,
      }),
    ).toBe(false);
    expect(
      shouldHideMainWindowOnClose({
        platform: "linux",
        isQuitting: false,
        trayAvailable: true,
      }),
    ).toBe(false);
  });

  it("cleans up once and force exits when a graceful tray quit stalls", () => {
    const cleanup = vi.fn();
    const gracefulQuit = vi.fn();
    const forceExit = vi.fn();
    const cancelForceExit = vi.fn();
    const timer = { unref: vi.fn() };
    const scheduleForceExit = vi.fn((_callback: () => void, delayMs: number) => {
      expect(delayMs).toBe(1500);
      return timer;
    });
    const lifecycle = createDesktopQuitLifecycle({
      cleanup,
      gracefulQuit,
      forceExit,
      scheduleForceExit,
      cancelForceExit,
    });

    expect(lifecycle.requestQuit()).toBe(true);
    expect(cleanup).toHaveBeenCalledTimes(1);
    expect(gracefulQuit).toHaveBeenCalledTimes(1);
    expect(scheduleForceExit).toHaveBeenCalledTimes(1);
    expect(timer.unref).toHaveBeenCalledTimes(1);
    expect(lifecycle.requestQuit()).toBe(false);

    const forceExitCallback = scheduleForceExit.mock.calls[0][0];
    forceExitCallback();
    expect(forceExit).toHaveBeenCalledWith(0);

    lifecycle.completeQuit();
    expect(cancelForceExit).toHaveBeenCalledWith(timer);
  });

  it("requires the Windows service for packaged Electron builds", () => {
    expect(
      bridgeLaunchPolicy({
        isPackaged: true,
        serviceInstalled: true,
        environmentBridgeConfigured: true,
      }),
    ).toBe("windows-service");
    expect(
      bridgeLaunchPolicy({
        isPackaged: true,
        serviceInstalled: false,
        environmentBridgeConfigured: true,
      }),
    ).toBe("service-required");
    expect(
      bridgeLaunchPolicy({
        isPackaged: true,
        serviceInstalled: false,
        environmentBridgeConfigured: false,
      }),
    ).toBe("service-required");
  });

  it("allows only non-persistent bridge fallbacks from a development checkout", () => {
    expect(
      bridgeLaunchPolicy({
        isPackaged: false,
        serviceInstalled: true,
        environmentBridgeConfigured: false,
      }),
    ).toBe("windows-service");
    expect(
      bridgeLaunchPolicy({
        isPackaged: false,
        serviceInstalled: false,
        environmentBridgeConfigured: true,
      }),
    ).toBe("environment");
    expect(
      bridgeLaunchPolicy({
        isPackaged: false,
        serviceInstalled: false,
        environmentBridgeConfigured: false,
      }),
    ).toBe("renderer-owned");
  });

  it("starts a renderer-owned bridge without persistent scheduling arguments", () => {
    const args = rendererOwnedBridgeArgs({
      host: "127.0.0.1",
      token: "test-token",
      configPath: "C:\\StockBot\\config.live.example.yaml",
    });

    expect(args).toEqual([
      "-m",
      "stockbot.electron_bridge",
      "--host",
      "127.0.0.1",
      "--port",
      "0",
      "--token",
      "test-token",
      "--config",
      "C:\\StockBot\\config.live.example.yaml",
    ]);
    expect(args).not.toContain("--persistent-live");
    expect(args).not.toContain("--cycle-interval-seconds");
  });

  it("accepts only a versioned loopback Windows service bridge session", () => {
    expect(
      bridgeSessionFromServicePayload({
        schemaVersion: 1,
        url: "http://127.0.0.1:43123",
        token: "a".repeat(43),
        processId: 4321,
        createdAt: "2026-07-27T00:00:00.000Z",
      }),
    ).toEqual({
      url: "http://127.0.0.1:43123",
      token: "a".repeat(43),
      processId: 4321,
      createdAt: "2026-07-27T00:00:00.000Z",
    });

    expect(
      bridgeSessionFromServicePayload({
        schemaVersion: 1,
        url: "http://example.com:43123",
        token: "a".repeat(43),
        processId: 4321,
        createdAt: "2026-07-27T00:00:00.000Z",
      }),
    ).toBeNull();
    expect(
      bridgeSessionFromServicePayload({
        schemaVersion: 2,
        url: "http://127.0.0.1:43123",
        token: "a".repeat(43),
        processId: 4321,
        createdAt: "2026-07-27T00:00:00.000Z",
      }),
    ).toBeNull();
    expect(
      bridgeSessionFromServicePayload({
        schemaVersion: 1,
        url: "http://127.0.0.1:43123",
        token: "short",
        processId: 4321,
        createdAt: "2026-07-27T00:00:00.000Z",
      }),
    ).toBeNull();
    expect(
      bridgeSessionFromServicePayload({
        schemaVersion: 1,
        url: "http://127.0.0.1:43123",
        token: "a".repeat(43),
        processId: 0,
        createdAt: "2026-07-27T00:00:00.000Z",
      }),
    ).toBeNull();
  });

  it("accepts a service session only when SCM reports the same running process", () => {
    const session = {
      processId: 4321,
    };
    const runningService = `
SERVICE_NAME: StockBotLive
        STATE              : 4  RUNNING
        PID                : 4321
`;

    expect(windowsServiceIdentityMatchesSession(session, runningService)).toBe(true);
    expect(
      windowsServiceIdentityMatchesSession(
        session,
        runningService.replace("PID                : 4321", "PID                : 9876"),
      ),
    ).toBe(false);
    expect(
      windowsServiceIdentityMatchesSession(
        session,
        runningService.replace("STATE              : 4", "STATE              : 1"),
      ),
    ).toBe(false);
    expect(
      windowsServiceIdentityMatchesSession(
        session,
        runningService.replace("StockBotLive", "OtherService"),
      ),
    ).toBe(false);
    expect(
      windowsServiceQueryMatchesSession(session, {
        state: "registered",
        output: runningService,
      }),
    ).toBe(true);
    expect(
      windowsServiceQueryMatchesSession(session, {
        state: "unknown",
        output: runningService,
      }),
    ).toBe(false);
  });

  it("trusts service installation only when SCM reports the expected registration", () => {
    const stoppedService = `
SERVICE_NAME: StockBotLive
        STATE              : 1  STOPPED
        PID                : 0
`;

    expect(windowsServiceIsRegistered(stoppedService)).toBe(true);
    expect(windowsServiceIsRegistered("OpenService FAILED 1060")).toBe(false);
    expect(windowsServiceIsRegistered(stoppedService.replace("StockBotLive", "OtherService"))).toBe(false);
    expect(
      windowsServiceRegistrationState({
        queryOutput: stoppedService,
        exitStatus: 0,
      }),
    ).toBe("registered");
    expect(
      windowsServiceRegistrationState({
        queryOutput: "OpenService FAILED 1060",
        exitStatus: 1060,
      }),
    ).toBe("absent");
    expect(
      windowsServiceRegistrationState({
        queryOutput: "Access is denied.",
        exitStatus: 5,
      }),
    ).toBe("unknown");
    expect(
      windowsServiceRegistrationState({
        queryOutput: "",
        exitStatus: null,
        queryError: true,
      }),
    ).toBe("unknown");
    expect(windowsServiceQueryArguments()).toEqual(["queryex", "StockBotLive"]);
  });

  it("distinguishes a stopped service from a running or ambiguous registration", () => {
    const stoppedService = `
SERVICE_NAME: StockBotLive
        STATE              : 1  STOPPED
        PID                : 0
`;
    const runningService = stoppedService
      .replace("STATE              : 1  STOPPED", "STATE              : 4  RUNNING")
      .replace("PID                : 0", "PID                : 4321");

    expect(windowsServiceRuntimeState(stoppedService)).toBe("stopped");
    expect(windowsServiceRuntimeState(runningService)).toBe("running");
    expect(windowsServiceRuntimeState(stoppedService.replace("StockBotLive", "OtherService"))).toBe("unknown");
    expect(windowsServiceRuntimeState("Access is denied.")).toBe("unknown");
  });

  it("maps the first failed service request to an actionable stopped-service error", () => {
    const stoppedService = `
SERVICE_NAME: StockBotLive
        STATE              : 1  STOPPED
        PID                : 0
`;
    const runningService = stoppedService
      .replace("STATE              : 1  STOPPED", "STATE              : 4  RUNNING")
      .replace("PID                : 0", "PID                : 4321");
    const requestError = new Error("connect ECONNREFUSED 127.0.0.1");

    const stoppedError = windowsServiceRequestFailureError(requestError, stoppedService);
    expect(stoppedError).not.toBe(requestError);
    expect(stoppedError.message).toContain("StockBotLive 서비스가 중지되어 있습니다");
    expect(stoppedError.message).toContain("Start-Service StockBotLive");
    expect(stoppedError.message).not.toContain("ECONNREFUSED");
    expect(windowsServiceRequestFailureError(requestError, runningService)).toBe(requestError);
    expect(windowsServiceRequestFailureError(requestError, "Access is denied.")).toBe(requestError);
  });

  it("accepts the numeric RUNNING state when a localized SCM label is undecodable", () => {
    const localizedRunningService = `
SERVICE_NAME: StockBotLive
        ����               : 10  WIN32_OWN_PROCESS
        ����               : 4  RUNNING
        PID                : 4321
`;
    const latin1LocalizedRunningService = `
SERVICE_NAME: StockBotLive
        \xc0\xaf\xc7\xfc               : 10  WIN32_OWN_PROCESS
        \xbb\xf3\xc5\xc2               : 4  RUNNING
        PID                : 4321
`;

    expect(
      windowsServiceIdentityMatchesSession(
        { processId: 4321 },
        localizedRunningService,
      ),
    ).toBe(true);
    expect(
      windowsServiceIdentityMatchesSession(
        { processId: 4321 },
        localizedRunningService.replace("4  RUNNING", "1  STOPPED"),
      ),
    ).toBe(false);
    expect(
      windowsServiceIdentityMatchesSession(
        { processId: 9876 },
        localizedRunningService,
      ),
    ).toBe(false);
    expect(
      windowsServiceIdentityMatchesSession(
        { processId: 4321 },
        latin1LocalizedRunningService,
      ),
    ).toBe(true);
  });

  it("rejects ambiguous or contradictory SCM identity fields", () => {
    const runningService = `
SERVICE_NAME: StockBotLive
        STATE              : 4  RUNNING
        PID                : 4321
`;
    const ambiguousOutputs = [
      runningService.replace("4  RUNNING", "4  STOPPED"),
      runningService.replace("4  RUNNING", "1  RUNNING"),
      `${runningService}        STATE              : 1  STOPPED\n`,
      `${runningService}        STATE              : 4  RUNNING\n`,
      `${runningService}        PID                : 4321\n`,
      `${runningService}SERVICE_NAME: StockBotLive\n`,
    ];

    for (const output of ambiguousOutputs) {
      expect(
        windowsServiceIdentityMatchesSession({ processId: 4321 }, output),
      ).toBe(false);
    }
    expect(
      windowsServiceIsRegistered(
        "SERVICE_NAME: StockBotLive\nSERVICE_NAME: StockBotLive\n",
      ),
    ).toBe(false);
  });

  it("never loads a Vite development renderer from a packaged application", () => {
    expect(
      shouldLoadDevelopmentRenderer({
        isPackaged: true,
        developmentServerUrl: "http://127.0.0.1:5173",
      }),
    ).toBe(false);
    expect(
      shouldLoadDevelopmentRenderer({
        isPackaged: false,
        developmentServerUrl: "http://127.0.0.1:5173",
      }),
    ).toBe(true);
    expect(
      shouldLoadDevelopmentRenderer({
        isPackaged: false,
        developmentServerUrl: "",
      }),
    ).toBe(false);
  });

  it("allows the SCM absence override only in an explicit test environment", () => {
    expect(
      shouldForceWindowsServiceAbsentForE2E({
        isPackaged: true,
        nodeEnvironment: "test",
        forceAbsent: "1",
      }),
    ).toBe(true);
    expect(
      shouldForceWindowsServiceAbsentForE2E({
        isPackaged: true,
        nodeEnvironment: "production",
        forceAbsent: "1",
      }),
    ).toBe(false);
    expect(
      shouldForceWindowsServiceAbsentForE2E({
        isPackaged: true,
        nodeEnvironment: "test",
        forceAbsent: "",
      }),
    ).toBe(false);
    expect(
      shouldForceWindowsServiceAbsentForE2E({
        isPackaged: false,
        nodeEnvironment: "test",
        forceAbsent: "1",
      }),
    ).toBe(false);
  });

  it("treats an installed Windows service without a session as wait-only", () => {
    const existing = new Set(["C:\\ProgramData\\StockBot\\service-config.json"]);
    const state = windowsServiceBridgeState(
      { PROGRAMDATA: "C:\\ProgramData" },
      {
        existsSync: (candidate: string) => existing.has(candidate),
        readFileSync: vi.fn(),
      },
      "win32",
    );

    expect(state.installed).toBe(true);
    expect(state.session).toBeNull();
  });

  it("does not reserve bridge ownership when the Windows service is absent", () => {
    const state = windowsServiceBridgeState(
      { PROGRAMDATA: "C:\\ProgramData" },
      {
        existsSync: () => false,
        readFileSync: vi.fn(),
      },
      "win32",
    );

    expect(state.installed).toBe(false);
    expect(state.session).toBeNull();
  });

  it("ignores development-only service path overrides in packaged lookup", () => {
    const state = windowsServiceBridgeState(
      {
        PROGRAMDATA: "C:\\ProgramData",
        STOCKBOT_SERVICE_CONFIG_FILE: "C:\\Temp\\service-config.json",
        STOCKBOT_SERVICE_SESSION_FILE: "C:\\Temp\\bridge-session.json",
      },
      {
        existsSync: (candidate: string) => candidate.startsWith("C:\\Temp\\"),
        readFileSync: () =>
          JSON.stringify({
            schemaVersion: 1,
            url: "http://127.0.0.1:43123",
            token: "a".repeat(43),
            processId: 4321,
            createdAt: "2026-07-27T00:00:00.000Z",
          }),
      },
      "win32",
      false,
    );

    expect(state.installed).toBe(false);
    expect(state.session).toBeNull();
  });

  it("loads valid Windows service session metadata for main-process verification", () => {
    const sessionPath = "C:\\ProgramData\\StockBot\\bridge-session.json";
    const state = windowsServiceBridgeState(
      { PROGRAMDATA: "C:\\ProgramData" },
      {
        existsSync: (candidate: string) => candidate === sessionPath,
        readFileSync: () =>
          JSON.stringify({
            schemaVersion: 1,
            url: "http://127.0.0.1:43123",
            token: "a".repeat(43),
            processId: 4321,
            createdAt: "2026-07-27T00:00:00.000Z",
          }),
      },
      "win32",
    );

    expect(state.installed).toBe(true);
    expect(state.session).toEqual({
      url: "http://127.0.0.1:43123",
      token: "a".repeat(43),
      processId: 4321,
      createdAt: "2026-07-27T00:00:00.000Z",
    });
  });

  it("treats only startup environment values as an external bridge session", () => {
    expect(bridgeSessionFromEnvironment({})).toBeNull();
    expect(
      bridgeSessionFromEnvironment({
        STOCKBOT_BRIDGE_URL: "http://127.0.0.1:8765",
      }),
    ).toBeNull();
    expect(
      bridgeSessionFromEnvironment({
        STOCKBOT_BRIDGE_TOKEN: "test-token",
      }),
    ).toBeNull();
    expect(
      bridgeSessionFromEnvironment({
        STOCKBOT_BRIDGE_URL: "http://127.0.0.1:8765",
        STOCKBOT_BRIDGE_TOKEN: "test-token",
      }),
    ).toEqual({ url: "http://127.0.0.1:8765", token: "test-token" });
  });

  it("does not let an obsolete child exit clear the active bridge process", () => {
    const obsoleteChild = {};
    const activeChild = {};

    expect(isActiveBridgeProcess(activeChild, obsoleteChild)).toBe(false);
    expect(isActiveBridgeProcess(activeChild, activeChild)).toBe(true);
  });

  it("keeps a replacement bridge active when an obsolete child exits late", () => {
    const onActiveExit = vi.fn();
    const registry = createActiveBridgeProcessRegistry(onActiveExit);
    const obsoleteChild = new EventEmitter();
    const activeChild = new EventEmitter();

    registry.adopt(obsoleteChild);
    registry.adopt(activeChild);
    obsoleteChild.emit("exit");

    expect(registry.current()).toBe(activeChild);
    expect(onActiveExit).not.toHaveBeenCalled();

    activeChild.emit("exit");

    expect(registry.current()).toBeNull();
    expect(onActiveExit).toHaveBeenCalledTimes(1);
  });

  it("does not launch the same Python candidate twice", () => {
    expect(uniqueBridgeCandidates(["python.exe", "python.exe", "python", "py"])).toEqual([
      "python.exe",
      "python",
      "py",
    ]);
  });

  it("assigns monotonic generations without exposing the bridge token", () => {
    const sessions = createBridgeSessionSequencer();
    expect(sessions.currentGeneration()).toBe(0);
    const first = sessions.adopt({ url: "http://127.0.0.1:1001", token: "first-secret" });
    const second = sessions.adopt({ url: "http://127.0.0.1:1002", token: "second-secret" });

    expect(first.generation).toBe(1);
    expect(second.generation).toBe(2);
    expect(bridgePayloadForRenderer(second, { stateRevision: 0 })).toEqual({
      stateRevision: 0,
      bridgeGeneration: 2,
    });
    expect(JSON.stringify(bridgePayloadForRenderer(second, {}))).not.toContain("second-secret");
    expect(sessions.currentGeneration()).toBe(2);
  });

  it("keeps the captured generation on redacted bridge failures", () => {
    const failure = bridgeFailurePayloadForRenderer(
      { generation: 4, token: "bridge-secret" },
      new Error("request failed with bridge-secret"),
      (value: unknown) => String(value).replace("bridge-secret", "[REDACTED]"),
    );

    expect(failure).toEqual({
      bridgeTransportError: true,
      bridgeGeneration: 4,
      message: "request failed with [REDACTED]",
    });
    expect(JSON.stringify(failure)).not.toContain("bridge-secret");
  });
});
