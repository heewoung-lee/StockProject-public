const http = require("http");
const https = require("https");

const DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS = 5000;
const LONG_BRIDGE_REQUEST_TIMEOUT_MS = 60000;
const LONG_ACTION_ENDPOINTS = new Set([
  "/api/actions/start",
  "/api/actions/pause",
  "/api/actions/cycle",
  "/api/actions/kis-check",
  "/api/actions/kis-live-check",
  "/api/actions/live-readiness-check",
  "/api/actions/clear-manual-reconciliation",
  "/api/actions/mode",
  "/api/actions/data-source",
  "/api/actions/cleanup-mode",
]);
const SENSITIVE_VALUE_PATTERN = /([=:]\s*)[^\s,;"'}]+/;
const GENERIC_SECRET_PATTERN =
  /(["']?\b(?:app[_\s-]?secret|appsecret|app[_\s-]?key|appkey|api[_\s-]?key|apikey|authorization|token|account[_\s-]?no|accountno|account|acct)\b["']?\s*[:=]\s*["']?)[^,\s;"'}]+(["']?)/gi;
const MAX_ERROR_BODY_CHARS = 500;

function endpointPath(endpoint) {
  return new URL(endpoint, "http://stockbot.local").pathname;
}

function bridgeRequestTimeoutMs(endpoint, options = {}) {
  if (Number.isFinite(options.timeoutMs) && options.timeoutMs > 0) {
    return options.timeoutMs;
  }
  return LONG_ACTION_ENDPOINTS.has(endpointPath(endpoint))
    ? LONG_BRIDGE_REQUEST_TIMEOUT_MS
    : DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS;
}

function redactBridgeDiagnosticText(value) {
  let text = String(value ?? "");
  text = text.replace(/Authorization\s*:\s*Bearer\s+[^\s,"'}]+/gi, "Authorization: Bearer [REDACTED]");
  text = text.replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "Bearer [REDACTED]");
  text = text.replace(
    /\bKIS[_A-Z0-9]*(?:KEY|SECRET|TOKEN|ACCOUNT|APP)[_A-Z0-9]*\s*[=:]\s*[^\s,;"'}]+/gi,
    (match) => match.replace(SENSITIVE_VALUE_PATTERN, "$1[REDACTED]"),
  );
  text = text.replace(
    GENERIC_SECRET_PATTERN,
    "$1[REDACTED]$2",
  );
  text = text.replace(/[A-Za-z]:\\Users\\[^\\\s"'}]+(?:\\[^\s"'}]*)?/g, "[REDACTED_PATH]");
  text = text.replace(/\b\d{8,}(?:-\d{2})?\b/g, "[REDACTED]");
  if (text.length > MAX_ERROR_BODY_CHARS) {
    return `${text.slice(0, MAX_ERROR_BODY_CHARS)}...`;
  }
  return text;
}

function bridgeFailureMessage(statusCode, bodyText) {
  const redactedBody = redactBridgeDiagnosticText(bodyText).trim();
  if (!redactedBody) {
    return `bridge request failed: ${statusCode}`;
  }
  return `bridge request failed: ${statusCode}: ${redactedBody}`;
}

function requestBridgeJson(session, endpoint, options = {}) {
  const target = new URL(endpoint, session.url);
  const payload = options.body === undefined ? undefined : JSON.stringify(options.body);
  const headers = { Accept: "application/json" };
  if (payload !== undefined) {
    headers["Content-Type"] = "application/json";
    headers["Content-Length"] = Buffer.byteLength(payload);
  }
  if (options.authorize !== false && session.token) {
    headers["X-StockBot-Bridge-Token"] = session.token;
  }

  return new Promise((resolve, reject) => {
    const client = target.protocol === "https:" ? https : http;
    const request = client.request(
      target,
      {
        method: options.method || "GET",
        headers,
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf8");
          if ((response.statusCode || 500) >= 400) {
            reject(new Error(bridgeFailureMessage(response.statusCode || 500, text)));
            return;
          }
          try {
            resolve(JSON.parse(text));
          } catch (error) {
            reject(error);
          }
        });
      },
    );
    const timeoutMs = bridgeRequestTimeoutMs(endpoint, options);
    request.setTimeout(timeoutMs, () => request.destroy(new Error(`bridge request timed out after ${timeoutMs}ms`)));
    request.on("error", reject);
    if (payload !== undefined) {
      request.write(payload);
    }
    request.end();
  });
}

module.exports = {
  bridgeRequestTimeoutMs,
  redactBridgeDiagnosticText,
  requestBridgeJson,
  DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS,
  LONG_BRIDGE_REQUEST_TIMEOUT_MS,
};
