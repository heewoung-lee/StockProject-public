import { createRequire } from "node:module";
import http from "node:http";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const {
  bridgeRequestTimeoutMs,
  DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS,
  LONG_BRIDGE_REQUEST_TIMEOUT_MS,
  requestBridgeJson,
  redactBridgeDiagnosticText,
} = require("../electron/bridge_request.cjs");

describe("Electron bridge request timeout policy", () => {
  it("keeps quick state requests on the short timeout", () => {
    expect(bridgeRequestTimeoutMs("/api/state")).toBe(DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS);
  });

  it("uses a longer timeout for KIS-backed trading actions", () => {
    for (const endpoint of [
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
    ]) {
      expect(bridgeRequestTimeoutMs(endpoint)).toBe(LONG_BRIDGE_REQUEST_TIMEOUT_MS);
      expect(bridgeRequestTimeoutMs(endpoint)).toBeGreaterThan(DEFAULT_BRIDGE_REQUEST_TIMEOUT_MS);
    }
  });

  it("redacts sensitive bridge response text before rejecting", async () => {
    const server = http.createServer((_request, response) => {
      response.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
      response.end(
        "Authorization: Bearer secret-token-123 KIS_APPSECRET=very-secret 12345678-01 C:\\Users\\alice\\StockProject\\.env",
      );
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const address = server.address();
    if (!address || typeof address === "string") {
      server.close();
      throw new Error("test server did not bind to a TCP port");
    }

    await expect(
      requestBridgeJson({ url: `http://127.0.0.1:${address.port}`, token: "" }, "/boom"),
    ).rejects.toThrow(/bridge request failed: 500/);
    await expect(
      requestBridgeJson({ url: `http://127.0.0.1:${address.port}`, token: "" }, "/boom"),
    ).rejects.not.toThrow(/secret-token-123|very-secret|12345678|alice|StockProject/);

    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  it("redacts bridge diagnostic snippets", () => {
    const redacted = redactBridgeDiagnosticText(
      "Authorization: Bearer secret-token-123 KIS_APP_KEY=abc KIS_ACCOUNT_NO=12345678 "
        + '{"appSecret":"json-secret","accountNo":"87654321","token":"json-token"} '
        + "C:\\Users\\alice\\StockProject\\.env",
    );

    expect(redacted).not.toMatch(/secret-token-123|abc|12345678|87654321|json-secret|json-token|alice|StockProject/);
    expect(redacted).toContain("[REDACTED]");
  });
});
