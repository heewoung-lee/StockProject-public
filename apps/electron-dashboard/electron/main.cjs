const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  ipcMain,
  nativeImage,
} = require("electron");
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const path = require("path");
const { DASHBOARD_ACTIONS } = require("./dashboard_actions.cjs");
const { requestBridgeJson, redactBridgeDiagnosticText } = require("./bridge_request.cjs");
const {
  profitReportEndpoint,
  sanitizeProfitReportForRenderer,
} = require("./profit_report_query.cjs");
const {
  bridgeLaunchPolicy,
  bridgeSessionFromEnvironment,
  bridgeFailurePayloadForRenderer,
  bridgePayloadForRenderer,
  createActiveBridgeProcessRegistry,
  createBridgeSessionSequencer,
  rendererOwnedBridgeArgs,
  shouldForceWindowsServiceAbsentForE2E,
  shouldLoadDevelopmentRenderer,
  uniqueBridgeCandidates,
  windowsServiceBridgeState,
  windowsServiceQueryArguments,
  windowsServiceQueryMatchesSession,
  windowsServiceRegistrationState,
  windowsServiceRequestFailureError,
  windowsServiceRuntimeState,
} = require("./bridge_lifecycle.cjs");
const {
  createDesktopQuitLifecycle,
  shouldHideMainWindowOnClose,
} = require("./window_lifecycle.cjs");

const BRIDGE_HOST = "127.0.0.1";
const BRIDGE_READY_TIMEOUT_MS = 10000;
const SERVICE_BRIDGE_READY_TIMEOUT_MS = 30000;
const SERVICE_BRIDGE_POLL_MS = 250;

let bridgeSessionPromise = null;
let mainWindow = null;
let tray = null;
let isQuitting = false;
const bridgeProcesses = createActiveBridgeProcessRegistry(() => {
  bridgeSessionPromise = null;
});
const bridgeSessions = createBridgeSessionSequencer();
const environmentBridgeConfig = bridgeSessionFromEnvironment(process.env);
const environmentBridgeSession = environmentBridgeConfig
  ? bridgeSessions.adopt(environmentBridgeConfig)
  : null;

function projectRoot() {
  return path.resolve(__dirname, "../../..");
}

function pythonCandidates() {
  const candidates = [];
  if (process.env.STOCKBOT_PYTHON) {
    candidates.push(process.env.STOCKBOT_PYTHON);
  }
  if (process.env.USERPROFILE) {
    candidates.push(
      path.join(
        process.env.USERPROFILE,
        ".cache",
        "codex-runtimes",
        "codex-primary-runtime",
        "dependencies",
        "python",
        "python.exe",
      ),
    );
  }
  candidates.push("python", "py");
  return uniqueBridgeCandidates(candidates);
}

function dashboardConfigPath(root) {
  const configured = process.env.STOCKBOT_CONFIG_PATH || process.env.STOCKBOT_CONFIG;
  if (configured) {
    return path.isAbsolute(configured) ? configured : path.resolve(root, configured);
  }

  const liveConfig = path.join(root, "config.live.example.yaml");
  if (fs.existsSync(liveConfig)) {
    return liveConfig;
  }
  return path.join(root, "config.example.yaml");
}

function bridgeArgs(root, bridgeToken) {
  return rendererOwnedBridgeArgs({
    host: BRIDGE_HOST,
    token: bridgeToken,
    configPath: dashboardConfigPath(root),
  });
}

function registerBridgeIpc() {
  ipcMain.handle("stockbot:load-state", async () => {
    return requestBridgeStateForRenderer("/api/state");
  });

  ipcMain.handle("stockbot:load-profit-report", async (_event, query) => {
    return requestBridgeProfitReportForRenderer(query);
  });

  ipcMain.handle("stockbot:run-action", async (_event, action, payload = {}) => {
    if (typeof action !== "string" || !DASHBOARD_ACTIONS.has(action)) {
      throw new Error("unknown dashboard action");
    }
    const body = payload && typeof payload === "object" && !Array.isArray(payload) ? payload : {};
    return requestBridgeStateForRenderer(`/api/actions/${encodeURIComponent(action)}`, {
      method: "POST",
      body,
    });
  });
}

async function requestBridgeStateForRenderer(endpoint, options = {}) {
  const generationAtRequestStart = bridgeSessions.currentGeneration();
  let session = null;
  try {
    session = await startBridge();
    const state = await requestBridgeJson(session, endpoint, options);
    return bridgePayloadForRenderer(session, state);
  } catch (error) {
    if (session && session.source === "service") {
      bridgeSessionPromise = null;
      error = windowsServiceRequestFailureError(
        error,
        queryStockBotWindowsService().output,
      );
    }
    return bridgeFailurePayloadForRenderer(
      session || { generation: generationAtRequestStart },
      error,
      redactBridgeDiagnosticText,
    );
  }
}

async function requestBridgeProfitReportForRenderer(query) {
  const generationAtRequestStart = bridgeSessions.currentGeneration();
  let session = null;
  try {
    const endpoint = profitReportEndpoint(query);
    session = await startBridge();
    const payload = await requestBridgeJson(session, endpoint);
    const report = sanitizeProfitReportForRenderer(payload, redactBridgeDiagnosticText);
    return bridgePayloadForRenderer(session, report);
  } catch (error) {
    if (session && session.source === "service") {
      bridgeSessionPromise = null;
      error = windowsServiceRequestFailureError(
        error,
        queryStockBotWindowsService().output,
      );
    }
    const failure = bridgeFailurePayloadForRenderer(
      session || { generation: generationAtRequestStart },
      error,
      redactBridgeDiagnosticText,
    );
    return {
      profitReportTransportError: true,
      ...(Number.isSafeInteger(failure.bridgeGeneration) && failure.bridgeGeneration > 0
        ? { bridgeGeneration: failure.bridgeGeneration }
        : {}),
      message: failure.message,
    };
  }
}

function startBridge() {
  if (!bridgeSessionPromise) {
    bridgeSessionPromise = startBridgeOnce().catch((error) => {
      bridgeSessionPromise = null;
      throw error;
    });
  }
  return bridgeSessionPromise;
}

function currentWindowsServiceBridgeState() {
  return windowsServiceBridgeState(
    process.env,
    fs,
    process.platform,
    !app.isPackaged,
  );
}

function queryStockBotWindowsService() {
  if (process.platform !== "win32") {
    return { state: "absent", output: "" };
  }
  if (
    shouldForceWindowsServiceAbsentForE2E({
      isPackaged: app.isPackaged,
      nodeEnvironment: process.env.NODE_ENV,
      forceAbsent: process.env.STOCKBOT_E2E_FORCE_SCM_ABSENT,
    })
  ) {
    return { state: "absent", output: "" };
  }
  const systemRoot = String(process.env.SystemRoot || process.env.SYSTEMROOT || "").trim();
  if (!systemRoot) {
    return { state: "unknown", output: "" };
  }
  const result = spawnSync(
    path.join(systemRoot, "System32", "sc.exe"),
    windowsServiceQueryArguments(),
    {
      encoding: "latin1",
      windowsHide: true,
      timeout: 5000,
    },
  );
  const output = [result.stdout, result.stderr].filter(Boolean).join("\n");
  return {
    state: windowsServiceRegistrationState({
      queryOutput: output,
      exitStatus: result.status,
      queryError: Boolean(result.error),
    }),
    output,
  };
}

async function startBridgeOnce() {
  const serviceQuery = queryStockBotWindowsService();
  if (serviceQuery.state === "unknown") {
    throw new Error(
      "StockBot Windows service registration could not be verified. " +
        "Refusing to start a fallback bridge.",
    );
  }
  const initialServiceState = {
    ...currentWindowsServiceBridgeState(),
    installed: serviceQuery.state === "registered",
  };
  const launchPolicy = bridgeLaunchPolicy({
    isPackaged: app.isPackaged,
    serviceInstalled: initialServiceState.installed,
    environmentBridgeConfigured: Boolean(environmentBridgeSession),
  });

  if (launchPolicy === "windows-service") {
    return waitForWindowsServiceBridge(initialServiceState);
  }
  if (launchPolicy === "service-required") {
    throw new Error(
      "StockBot Windows service is required but is not installed. " +
        "The packaged dashboard will not start a local Python bridge.",
    );
  }
  if (launchPolicy === "environment") {
    return environmentBridgeSession;
  }

  const root = projectRoot();
  const bridgeToken = process.env.STOCKBOT_BRIDGE_TOKEN || crypto.randomBytes(24).toString("base64url");
  const errors = [];

  for (const python of pythonCandidates()) {
    if (python !== "python" && python !== "py" && !fs.existsSync(python)) {
      continue;
    }
    try {
      const session = await launchBridgeWithPython(python, root, bridgeToken);
      return session;
    } catch (error) {
      const pythonLabel = redactBridgeDiagnosticText(python);
      const errorMessage = redactBridgeDiagnosticText(error && error.message ? error.message : String(error));
      errors.push(`${pythonLabel}: ${errorMessage}`);
    }
  }

  throw new Error(
    errors.length
      ? `Python bridge failed. Tried ${errors.join("; ")}`
      : "Python runtime was not found. Set STOCKBOT_PYTHON or STOCKBOT_BRIDGE_URL.",
  );
}

async function waitForWindowsServiceBridge(initialServiceState) {
  let serviceState =
    initialServiceState || windowsServiceBridgeState(process.env, fs);
  let serviceRuntimeState = "unknown";
  if (!serviceState.installed) {
    return null;
  }
  const deadline = Date.now() + SERVICE_BRIDGE_READY_TIMEOUT_MS;
  do {
    const serviceQuery = queryStockBotWindowsService();
    serviceRuntimeState = windowsServiceRuntimeState(serviceQuery.output);
    if (serviceRuntimeState === "stopped") {
      throw windowsServiceRequestFailureError(
        new Error("StockBot Windows service bridge is unavailable."),
        serviceQuery.output,
      );
    }
    if (
      serviceState.session &&
      windowsServiceQueryMatchesSession(serviceState.session, serviceQuery)
    ) {
      try {
        await requestBridgeJson(serviceState.session, "/api/health", { authorize: false });
        return bridgeSessions.adopt({
          ...serviceState.session,
          source: "service",
        });
      } catch {
        // The service may be replacing a stale session during automatic recovery.
      }
    }
    await new Promise((resolve) => setTimeout(resolve, SERVICE_BRIDGE_POLL_MS));
    serviceState = currentWindowsServiceBridgeState();
  } while (Date.now() < deadline);

  throw new Error("StockBot Windows service is installed but its local bridge is not ready.");
}

async function launchBridgeWithPython(python, root, bridgeToken) {
  const child = spawn(python, bridgeArgs(root, bridgeToken), {
    cwd: root,
    env: {
      ...process.env,
      PYTHONPATH: [path.join(root, "src"), process.env.PYTHONPATH || ""].filter(Boolean).join(path.delimiter),
    },
    windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });

  child.stderr.on("data", (chunk) => {
    console.error(`[stockbot-bridge] ${redactBridgeDiagnosticText(chunk)}`);
  });

  try {
    const session = await waitForBridgeReady(child, bridgeToken);
    await requestBridgeJson(session, "/api/health", { authorize: false });
    if (child.exitCode !== null || child.signalCode !== null) {
      throw new Error("bridge exited after health check");
    }
    bridgeProcesses.adopt(child);
    return bridgeSessions.adopt(session);
  } catch (error) {
    if (child.exitCode === null && !child.killed) {
      child.kill();
    }
    throw error;
  }
}

function waitForBridgeReady(child, expectedToken) {
  return new Promise((resolve, reject) => {
    let buffer = "";
    let settled = false;

    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      child.off("exit", onExit);
      child.off("error", onError);
      callback(value);
    };

    const onExit = (code) => {
      finish(reject, new Error(`bridge exited before ready: ${code}`));
    };

    const onError = (error) => {
      finish(reject, error);
    };

    const timer = setTimeout(() => {
      finish(reject, new Error("bridge did not report readiness in time"));
    }, BRIDGE_READY_TIMEOUT_MS);

    child.once("exit", onExit);
    child.once("error", onError);
    child.stdout.on("data", (chunk) => {
      buffer += chunk.toString("utf8");
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const ready = JSON.parse(line);
          if (!ready.ok || !ready.port || ready.token !== expectedToken) {
            finish(reject, new Error("bridge readiness payload was invalid"));
            return;
          }
          finish(resolve, {
            url: `http://${ready.host || BRIDGE_HOST}:${ready.port}`,
            token: expectedToken,
          });
          return;
        } catch (error) {
          finish(reject, error);
          return;
        }
      }
    });
  });
}

function hasUsableTray() {
  return Boolean(tray && !tray.isDestroyed());
}

function showMainWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function createTray() {
  if (process.platform !== "win32" || hasUsableTray()) {
    return tray;
  }
  try {
    const iconPath = path.join(
      __dirname,
      "..",
      "dist",
      "stockbot-donghak-ant-icon.png",
    );
    const icon = nativeImage.createFromPath(iconPath);
    if (icon.isEmpty()) {
      throw new Error("tray icon is empty");
    }
    tray = new Tray(icon.resize({ width: 16, height: 16, quality: "best" }));
    tray.setToolTip("개미親주식");
    tray.setContextMenu(
      Menu.buildFromTemplate([
        {
          label: "대시보드 열기",
          click: showMainWindow,
        },
        { type: "separator" },
        {
          label: "대시보드 종료",
          click: quitDesktopApp,
        },
      ]),
    );
    tray.on("click", showMainWindow);
  } catch {
    tray = null;
    console.error("[stockbot-tray] Windows tray could not be created");
  }
  return tray;
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    showMainWindow();
    return mainWindow;
  }

  void startBridge().catch((error) => {
    console.error(`[stockbot-bridge] ${redactBridgeDiagnosticText(error.message)}`);
  });

  Menu.setApplicationMenu(null);

  const win = new BrowserWindow({
    width: 1600,
    height: 900,
    minWidth: 1180,
    minHeight: 820,
    title: "개미親주식",
    autoHideMenuBar: true,
    backgroundColor: "#07111a",
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow = win;

  win.on("close", (event) => {
    if (
      shouldHideMainWindowOnClose({
        platform: process.platform,
        isQuitting,
        trayAvailable: hasUsableTray(),
      })
    ) {
      event.preventDefault();
      win.hide();
    }
  });
  win.on("closed", () => {
    if (mainWindow === win) {
      mainWindow = null;
    }
  });

  const developmentServerUrl = String(process.env.VITE_DEV_SERVER_URL || "").trim();
  if (
    shouldLoadDevelopmentRenderer({
      isPackaged: app.isPackaged,
      developmentServerUrl,
    })
  ) {
    win.loadURL(developmentServerUrl);
  } else {
    win.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  }
  return win;
}

function stopDashboardProcesses() {
  bridgeProcesses.stop();
  if (hasUsableTray()) {
    tray.destroy();
  }
  tray = null;
}

const desktopQuitLifecycle = createDesktopQuitLifecycle({
  cleanup: () => {
    isQuitting = true;
    stopDashboardProcesses();
  },
  gracefulQuit: () => app.quit(),
  forceExit: (exitCode) => app.exit(exitCode),
});

function quitDesktopApp() {
  desktopQuitLifecycle.requestQuit();
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  registerBridgeIpc();

  app.on("second-instance", showMainWindow);
  app.on("before-quit", () => {
    isQuitting = true;
  });
  app.on("will-quit", stopDashboardProcesses);
  app.on("quit", () => desktopQuitLifecycle.completeQuit());

  app.whenReady().then(() => {
    createTray();
    createWindow();
  });
}

app.on("window-all-closed", () => {
  if (process.platform === "win32" && hasUsableTray()) {
    return;
  }
  bridgeProcesses.stop();
  if (process.platform !== "darwin") {
    app.quit();
  }
});

app.on("activate", () => {
  showMainWindow();
});
