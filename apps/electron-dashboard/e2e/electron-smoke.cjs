const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const test = require("node:test");

const { _electron: electron } = require("playwright-core");

const DASHBOARD_ROOT = path.resolve(__dirname, "..");
const TEST_BRIDGE_TOKEN = "stockbot-electron-smoke-loopback-token-0001";
const PACKAGED_EXECUTABLE = String(process.env.STOCKBOT_E2E_EXECUTABLE || "").trim();

function sourceHostHasRegisteredWindowsService() {
  if (PACKAGED_EXECUTABLE || process.platform !== "win32") {
    return false;
  }
  const systemRoot = String(process.env.SystemRoot || process.env.SYSTEMROOT || "").trim();
  if (!systemRoot) {
    return false;
  }
  const result = spawnSync(
    path.join(systemRoot, "System32", "sc.exe"),
    ["query", "StockBotLive"],
    {
      windowsHide: true,
      timeout: 5000,
    },
  );
  return !result.error && result.status === 0;
}

function dashboardState() {
  return {
    stateRevision: 1,
    app: {
      title: "개미親주식",
      subtitle: "가상 자동매매",
      authorLabel: "MadeBy :heewoung-lee",
      authorUrl: "https://github.com/heewoung-lee",
      version: "0.1.0",
    },
    mode: {
      key: "virtual",
      label: "가상 모드",
      isReal: false,
    },
    runtime: {
      status: "정지",
      running: false,
      schedulerOwner: PACKAGED_EXECUTABLE ? "service" : "renderer",
      schedulerActive: false,
      cycleLabel: "예약 없음",
      lastUpdated: "09:00:00",
      dataSourceKind: "local",
      dataModeDescription: "E2E loopback stub",
      safetySummary: "실제 주문 없음",
      cleanupMode: false,
    },
    notice: {
      title: "E2E 안전 모드",
      description: "Loopback stub bridge에 연결되었습니다.",
      tone: "paper",
      orderEnabled: false,
      ready: false,
    },
    account: {
      title: "계좌 상태",
      metrics: [
        { label: "상태", value: "정지", emphasis: true },
        { label: "계좌", value: "E2E 가상계좌" },
        { label: "현금", value: "1,000,000원", emphasis: true },
        { label: "평가금", value: "1,000,000원", emphasis: true },
        { label: "보유 종목", value: "0개", emphasis: true },
        { label: "매수 가능", value: "1,000,000원", emphasis: true },
      ],
      summary: [],
    },
    positions: [],
    selectedPosition: null,
    logs: {
      trades: [],
      system: [],
    },
  };
}

function sendJson(response, statusCode, body) {
  const payload = Buffer.from(JSON.stringify(body), "utf8");
  response.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": payload.length,
    "Cache-Control": "no-store",
  });
  response.end(payload);
}

async function startLoopbackBridge() {
  const requests = [];
  const unexpectedRequests = [];
  const server = http.createServer((request, response) => {
    const method = request.method || "GET";
    const url = new URL(request.url || "/", "http://127.0.0.1");
    requests.push({ method, pathname: url.pathname });

    if (url.pathname === "/api/health" && method === "GET") {
      sendJson(response, 200, { ok: true });
      return;
    }

    if (
      url.pathname === "/api/state" &&
      method === "GET" &&
      request.headers["x-stockbot-bridge-token"] === TEST_BRIDGE_TOKEN
    ) {
      sendJson(response, 200, dashboardState());
      return;
    }

    unexpectedRequests.push({ method, pathname: url.pathname });
    sendJson(response, 405, { ok: false, message: "E2E stub rejects mutations and unknown routes." });
  });

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });

  const address = server.address();
  assert.ok(address && typeof address === "object");

  return {
    requests,
    unexpectedRequests,
    url: `http://127.0.0.1:${address.port}`,
    async close() {
      await new Promise((resolve, reject) => {
        server.close((error) => (error ? reject(error) : resolve()));
      });
    },
  };
}

function isolatedElectronEnvironment(tempRoot, bridgeUrl) {
  const environment = {};
  for (const key of [
    "ComSpec",
    "COMSPEC",
    "Path",
    "PATH",
    "PATHEXT",
    "SystemDrive",
    "SYSTEMDRIVE",
    "SystemRoot",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "windir",
    "WINDIR",
  ]) {
    if (process.env[key]) {
      environment[key] = process.env[key];
    }
  }

  const isolatedEnvironment = {
    ...environment,
    APPDATA: path.join(tempRoot, "AppData", "Roaming"),
    LOCALAPPDATA: path.join(tempRoot, "AppData", "Local"),
    PROGRAMDATA: path.join(tempRoot, "ProgramData"),
    USERPROFILE: tempRoot,
    NODE_ENV: "test",
  };
  if (!PACKAGED_EXECUTABLE) {
    isolatedEnvironment.STOCKBOT_BRIDGE_URL = bridgeUrl;
    isolatedEnvironment.STOCKBOT_BRIDGE_TOKEN = TEST_BRIDGE_TOKEN;
  } else {
    isolatedEnvironment.STOCKBOT_E2E_FORCE_SCM_ABSENT = "1";
    isolatedEnvironment.VITE_DEV_SERVER_URL = bridgeUrl;
  }
  return isolatedEnvironment;
}

async function waitForMainWindowState(
  electronApplication,
  expected,
  timeoutMs = 5000,
) {
  const deadline = Date.now() + timeoutMs;
  let lastState;
  do {
    lastState = await electronApplication.evaluate(({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows();
      return {
        count: windows.length,
        id: windows[0]?.id ?? null,
        visible: windows[0]?.isVisible() ?? false,
      };
    });
    if (
      Object.entries(expected).every(
        ([key, value]) => lastState[key] === value,
      )
    ) {
      return lastState;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  } while (Date.now() < deadline);
  throw new Error(
    `Electron main window did not reach ${JSON.stringify(expected)}; last state was ${JSON.stringify(lastState)}`,
  );
}

test(
  "loads the production dashboard through Electron and enforces its bridge ownership boundary",
  { timeout: 45000 },
  async (context) => {
    if (sourceHostHasRegisteredWindowsService()) {
      context.skip("source Electron smoke requires a host without StockBotLive registration");
      return;
    }
    const tempRoot = await fs.mkdtemp(path.join(os.tmpdir(), "stockbot-electron-smoke-"));
    const loopbackBridge = await startLoopbackBridge();
    let electronApplication;

    try {
      await Promise.all([
        fs.mkdir(path.join(tempRoot, "AppData", "Roaming"), { recursive: true }),
        fs.mkdir(path.join(tempRoot, "AppData", "Local"), { recursive: true }),
        fs.mkdir(path.join(tempRoot, "ProgramData"), { recursive: true }),
      ]);
      if (PACKAGED_EXECUTABLE) {
        const staleServiceDirectory = path.join(tempRoot, "ProgramData", "StockBot");
        await fs.mkdir(staleServiceDirectory, { recursive: true });
        await fs.writeFile(path.join(staleServiceDirectory, "service-config.json"), "{}");
      }
      const executablePath = PACKAGED_EXECUTABLE || require("electron");
      const launchArgs = PACKAGED_EXECUTABLE ? [] : [DASHBOARD_ROOT];
      const electronEnvironment = isolatedElectronEnvironment(tempRoot, loopbackBridge.url);
      electronApplication = await electron.launch({
        executablePath,
        args: launchArgs,
        cwd: DASHBOARD_ROOT,
        env: electronEnvironment,
      });

      assert.equal(
        await electronApplication.evaluate(
          (_electron, expected) =>
            expected.packaged
              ? !process.env.STOCKBOT_BRIDGE_URL && !process.env.STOCKBOT_BRIDGE_TOKEN
              : process.env.STOCKBOT_BRIDGE_URL === expected.url &&
                process.env.STOCKBOT_BRIDGE_TOKEN === expected.token,
          {
            packaged: Boolean(PACKAGED_EXECUTABLE),
            url: loopbackBridge.url,
            token: TEST_BRIDGE_TOKEN,
          },
        ),
        true,
        "Electron main process must use the expected isolated bridge discovery path",
      );

      const page = await electronApplication.firstWindow();
      const pageErrors = [];
      page.on("pageerror", (error) => pageErrors.push(error.name));

      await page
        .getByRole("heading", { name: "개미親주식 대시보드" })
        .waitFor({ state: "visible", timeout: 20000 });
      await page.getByRole("heading", { name: "계좌 상태" }).waitFor({ state: "visible" });

      assert.match(page.url(), /^file:/, "smoke test must load the built renderer, not a Vite dev server");
      assert.equal(
        await page.evaluate(
          () =>
            typeof window.stockbotBridge?.loadState === "function" &&
            typeof window.stockbotBridge?.runAction === "function",
        ),
        true,
        "preload must expose the IPC bridge",
      );

      const ipcProbe = await page.evaluate(async () => window.stockbotBridge.loadState());
      if (PACKAGED_EXECUTABLE) {
        assert.equal(ipcProbe.bridgeTransportError, true);
        assert.match(String(ipcProbe.message || ""), /Windows service is required/i);
        await page.getByText("브리지 연결 실패", { exact: true }).waitFor({
          state: "visible",
          timeout: 20000,
        });
        assert.deepEqual(
          loopbackBridge.requests,
          [],
          "packaged Electron must not adopt a development loopback bridge",
        );
      } else {
        assert.equal(
          ipcProbe.bridgeTransportError,
          undefined,
          `read-only bridge state request failed: ${String(ipcProbe.message || "unknown transport error")}`,
        );
        assert.equal(ipcProbe.app?.title, "개미親주식");
        await page.getByText("E2E 가상계좌", { exact: true }).waitFor({ state: "visible", timeout: 20000 });
        assert.ok(
          loopbackBridge.requests.some(
            ({ method, pathname }) => method === "GET" && pathname === "/api/state",
          ),
          "renderer must load state through Electron IPC",
        );
      }
      assert.deepEqual(loopbackBridge.unexpectedRequests, [], "smoke test must not invoke bridge mutations");
      assert.deepEqual(pageErrors, [], "renderer must not emit uncaught page errors");

      if (process.platform === "win32") {
        const mainWindowId = await electronApplication.evaluate(({ BrowserWindow }) => {
          const [mainWindow] = BrowserWindow.getAllWindows();
          if (!mainWindow) {
            throw new Error("main window is missing");
          }
          const windowId = mainWindow.id;
          mainWindow.close();
          return windowId;
        });
        assert.deepEqual(
          await waitForMainWindowState(electronApplication, {
            count: 1,
            visible: false,
          }),
          { count: 1, id: mainWindowId, visible: false },
          "closing the Windows dashboard must hide its single window in the tray",
        );

        const secondInstance = spawnSync(executablePath, launchArgs, {
          cwd: DASHBOARD_ROOT,
          env: electronEnvironment,
          timeout: 10000,
          windowsHide: true,
        });
        assert.equal(secondInstance.error, undefined, "second Electron launch must exit cleanly");
        assert.equal(secondInstance.status, 0, "second Electron launch must hand off to the first instance");
        assert.deepEqual(
          await waitForMainWindowState(electronApplication, {
            count: 1,
            id: mainWindowId,
            visible: true,
          }),
          { count: 1, id: mainWindowId, visible: true },
          "launching the dashboard again must restore the existing tray window",
        );

        const restoredProbe = await page.evaluate(async () => window.stockbotBridge.loadState());
        assert.equal(
          Boolean(restoredProbe.bridgeTransportError),
          Boolean(PACKAGED_EXECUTABLE),
          "restoring the tray window must preserve its original bridge ownership",
        );
      }
    } finally {
      if (electronApplication) {
        await electronApplication.close();
      }
      await loopbackBridge.close();
      await fs.rm(tempRoot, { recursive: true, force: true });
    }
  },
);
