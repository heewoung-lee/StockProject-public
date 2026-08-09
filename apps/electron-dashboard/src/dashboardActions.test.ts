import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const { DASHBOARD_ACTIONS } = require("../electron/dashboard_actions.cjs");

describe("Electron dashboard action allowlist", () => {
  it("allows automation start requests to reach the backend gate", () => {
    expect(DASHBOARD_ACTIONS.has("start")).toBe(true);
  });

  it("allows live manual reconciliation recovery from the renderer", () => {
    expect(DASHBOARD_ACTIONS.has("clear-manual-reconciliation")).toBe(true);
  });

  it("does not expose removed strategy and budget actions", () => {
    for (const action of ["ai-advisor", "profile", "custom-settings"]) {
      expect(DASHBOARD_ACTIONS.has(action)).toBe(false);
    }
  });
});
