const path = require("path");

function bridgeSessionFromEnvironment(environment) {
  const url = String(environment.STOCKBOT_BRIDGE_URL || "").trim();
  const token = String(environment.STOCKBOT_BRIDGE_TOKEN || "").trim();
  if (!url || !token) {
    return null;
  }
  return {
    url,
    token,
  };
}

function bridgeLaunchPolicy({
  isPackaged,
  serviceInstalled,
  environmentBridgeConfigured,
}) {
  if (serviceInstalled) {
    return "windows-service";
  }
  if (isPackaged) {
    return "service-required";
  }
  if (environmentBridgeConfigured) {
    return "environment";
  }
  return "renderer-owned";
}

function rendererOwnedBridgeArgs({ host, token, configPath }) {
  return [
    "-m",
    "stockbot.electron_bridge",
    "--host",
    host,
    "--port",
    "0",
    "--token",
    token,
    "--config",
    configPath,
  ];
}

function windowsServiceBridgeState(
  environment,
  fileSystem,
  platform = process.platform,
  allowPathOverrides = true,
) {
  if (platform !== "win32") {
    return { installed: false, session: null };
  }
  const configuredSessionPath = allowPathOverrides
    ? String(environment.STOCKBOT_SERVICE_SESSION_FILE || "").trim()
    : "";
  const configuredConfigPath = allowPathOverrides
    ? String(environment.STOCKBOT_SERVICE_CONFIG_FILE || "").trim()
    : "";
  const programData = String(environment.PROGRAMDATA || "").trim();
  const sessionPath =
    configuredSessionPath || (programData ? path.join(programData, "StockBot", "bridge-session.json") : "");
  if (!sessionPath) {
    return { installed: false, session: null };
  }
  const configPath =
    configuredConfigPath || path.join(path.dirname(sessionPath), "service-config.json");
  const explicitlyConfigured = Boolean(configuredSessionPath || configuredConfigPath);
  const configExists = fileSystem.existsSync(configPath);
  const sessionExists = fileSystem.existsSync(sessionPath);
  if (!sessionExists) {
    return {
      installed: explicitlyConfigured || configExists,
      session: null,
    };
  }
  try {
    return {
      installed: true,
      session: bridgeSessionFromServicePayload(
        JSON.parse(fileSystem.readFileSync(sessionPath, "utf8")),
      ),
    };
  } catch {
    return { installed: true, session: null };
  }
}

function bridgeSessionFromServicePayload(payload) {
  if (!payload || typeof payload !== "object" || payload.schemaVersion !== 1) {
    return null;
  }
  const token = String(payload.token || "").trim();
  const processId = Number(payload.processId);
  const createdAt = String(payload.createdAt || "").trim();
  if (
    token.length < 32 ||
    !Number.isSafeInteger(processId) ||
    processId <= 0 ||
    !createdAt ||
    Number.isNaN(Date.parse(createdAt))
  ) {
    return null;
  }
  try {
    const parsed = new URL(String(payload.url || "").trim());
    if (
      parsed.protocol !== "http:" ||
      parsed.hostname !== "127.0.0.1" ||
      !parsed.port ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }
    return {
      url: parsed.origin,
      token,
      processId,
      createdAt,
    };
  } catch {
    return null;
  }
}

function windowsServiceIdentityMatchesSession(
  session,
  queryOutput,
  serviceName = "StockBotLive",
) {
  if (!session || !Number.isSafeInteger(session.processId) || session.processId <= 0) {
    return false;
  }
  const output = String(queryOutput || "");
  const lines = output.split(/\r?\n/);
  const serviceNames = lines
    .map((line) => line.match(/^[ \t]*SERVICE_NAME[ \t]*:[ \t]*(\S+)[ \t]*$/i))
    .filter(Boolean);
  const states = lines
    .map((line) =>
      line.match(
        /^[ \t]*[^:]+[ \t]*:[ \t]*(\d+)[ \t]+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)[ \t]*$/i,
      ),
    )
    .filter(Boolean);
  const processIds = lines
    .map((line) => line.match(/^[ \t]*PID[ \t]*:[ \t]*(\d+)[ \t]*$/i))
    .filter(Boolean);
  if (serviceNames.length !== 1 || states.length !== 1 || processIds.length !== 1) {
    return false;
  }
  const reportedName = serviceNames[0][1];
  const state = Number(states[0][1]);
  const stateLabel = states[0][2].toUpperCase();
  const processId = Number(processIds[0][1]);
  return (
    reportedName.toLowerCase() === String(serviceName).toLowerCase() &&
    state === 4 &&
    stateLabel === "RUNNING" &&
    processId === session.processId
  );
}

function windowsServiceIsRegistered(queryOutput, serviceName = "StockBotLive") {
  const serviceNames = String(queryOutput || "")
    .split(/\r?\n/)
    .map((line) => line.match(/^[ \t]*SERVICE_NAME[ \t]*:[ \t]*(\S+)[ \t]*$/i))
    .filter(Boolean);
  return (
    serviceNames.length === 1 &&
    serviceNames[0][1].toLowerCase() === String(serviceName).toLowerCase()
  );
}

function windowsServiceRegistrationState({
  queryOutput,
  exitStatus,
  queryError = false,
}) {
  const output = String(queryOutput || "");
  if (queryError) {
    return "unknown";
  }
  if (exitStatus === 0) {
    return windowsServiceIsRegistered(output) ? "registered" : "unknown";
  }
  if (exitStatus === 1060 || /\b1060\b/.test(output)) {
    return "absent";
  }
  return "unknown";
}

function windowsServiceRuntimeState(queryOutput, serviceName = "StockBotLive") {
  const output = String(queryOutput || "");
  const serviceNames = output
    .split(/\r?\n/)
    .map((line) => line.match(/^[ \t]*SERVICE_NAME[ \t]*:[ \t]*(\S+)[ \t]*$/i))
    .filter(Boolean);
  const states = output
    .split(/\r?\n/)
    .map((line) =>
      line.match(
        /^[ \t]*[^:]+[ \t]*:[ \t]*(\d+)[ \t]+(STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|PAUSE_PENDING|PAUSED)[ \t]*$/i,
      ),
    )
    .filter(Boolean);
  if (
    serviceNames.length !== 1
    || serviceNames[0][1].toLowerCase() !== String(serviceName).toLowerCase()
    || states.length !== 1
  ) {
    return "unknown";
  }
  const stateCode = Number(states[0][1]);
  const labels = {
    1: "stopped",
    2: "start-pending",
    3: "stop-pending",
    4: "running",
    5: "continue-pending",
    6: "pause-pending",
    7: "paused",
  };
  return labels[stateCode] || "unknown";
}

const WINDOWS_SERVICE_STOPPED_MESSAGE =
  "StockBotLive 서비스가 중지되어 있습니다. 관리자 PowerShell에서 Start-Service StockBotLive를 실행한 뒤 다시 시도하세요.";

function windowsServiceRequestFailureError(
  requestError,
  queryOutput,
  serviceName = "StockBotLive",
) {
  if (windowsServiceRuntimeState(queryOutput, serviceName) !== "stopped") {
    return requestError;
  }
  return new Error(WINDOWS_SERVICE_STOPPED_MESSAGE);
}

function windowsServiceQueryArguments(serviceName = "StockBotLive") {
  return ["queryex", serviceName];
}

function shouldLoadDevelopmentRenderer({ isPackaged, developmentServerUrl }) {
  return !isPackaged && Boolean(String(developmentServerUrl || "").trim());
}

function shouldForceWindowsServiceAbsentForE2E({
  isPackaged,
  nodeEnvironment,
  forceAbsent,
}) {
  return Boolean(isPackaged) && nodeEnvironment === "test" && forceAbsent === "1";
}

function windowsServiceQueryMatchesSession(session, serviceQuery) {
  return (
    serviceQuery?.state === "registered" &&
    windowsServiceIdentityMatchesSession(session, serviceQuery.output)
  );
}

function isActiveBridgeProcess(activeProcess, candidateProcess) {
  return activeProcess === candidateProcess;
}

function createActiveBridgeProcessRegistry(onActiveExit) {
  let activeProcess = null;
  return {
    adopt(child) {
      activeProcess = child;
      child.once("exit", () => {
        if (!isActiveBridgeProcess(activeProcess, child)) {
          return;
        }
        activeProcess = null;
        onActiveExit();
      });
    },
    current() {
      return activeProcess;
    },
    stop() {
      const child = activeProcess;
      if (!child) {
        return;
      }
      activeProcess = null;
      onActiveExit();
      child.kill();
    },
  };
}

function uniqueBridgeCandidates(candidates) {
  return [...new Set(candidates)];
}

function createBridgeSessionSequencer() {
  let generation = 0;
  return {
    adopt(session) {
      generation += 1;
      return {
        ...session,
        generation,
      };
    },
    currentGeneration() {
      return generation;
    },
  };
}

function bridgePayloadForRenderer(session, payload) {
  return {
    ...payload,
    bridgeGeneration: session.generation,
  };
}

function bridgeFailurePayloadForRenderer(session, error, redact) {
  const errorMessage = error && error.message ? error.message : String(error);
  const payload = {
    bridgeTransportError: true,
    message: redact(errorMessage),
  };
  if (!Number.isSafeInteger(session && session.generation) || session.generation <= 0) {
    return payload;
  }
  return bridgePayloadForRenderer(session, payload);
}

module.exports = {
  bridgeLaunchPolicy,
  bridgeSessionFromEnvironment,
  bridgeSessionFromServicePayload,
  bridgeFailurePayloadForRenderer,
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
};
