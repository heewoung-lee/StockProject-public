import { afterEach, describe, expect, it, vi } from "vitest";

import { loadDashboardState, loadProfitReport, runAction } from "./api";
import type { ProfitReport } from "./types";

const profitReport: ProfitReport = {
  schemaVersion: 1,
  generatedAt: "2026-07-29T01:02:03.000Z",
  bridgeGeneration: 5,
  status: "complete",
  costInclusion: "unknown",
  query: {
    granularity: "day",
    scope: "account",
    anchor: "2026-07-29",
    timezone: "Asia/Seoul",
  },
  range: {
    label: "2026년 7월",
    startAt: "2026-07-01T00:00:00+09:00",
    endAt: "2026-08-01T00:00:00+09:00",
    anchor: "2026-07-29",
    previousAnchor: "2026-06-29",
    nextAnchor: null,
  },
  summary: {
    reportedRealizedPnlKrw: 12345,
    profitableBucketsTotalKrw: 15000,
    losingBucketsTotalKrw: -2655,
    tradingCostKrw: 500,
    profitableBucketCount: 2,
    losingBucketCount: 1,
    availableBucketCount: 3,
  },
  buckets: [
    {
      key: "2026-07-29",
      label: "07/29",
      startAt: "2026-07-29T00:00:00+09:00",
      endAt: "2026-07-30T00:00:00+09:00",
      reportedRealizedPnlKrw: 12345,
      feeKrw: 300,
      taxKrw: 200,
      interestKrw: 0,
      fillCount: 4,
      status: "confirmed",
      activityStatus: "trade",
      costInclusion: "unknown",
      issues: [],
    },
  ],
  issues: [],
  dataSource: "KIS_PERIOD_PROFIT_LOCAL",
  updatedAt: "2026-07-29T15:31:00+09:00",
};

describe("Electron bridge transport failures", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves the captured bridge generation for state and action failures", async () => {
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => ({
        bridgeTransportError: true,
        bridgeGeneration: 3,
        message: "state transport failed",
      })),
      runAction: vi.fn(async () => ({
        bridgeTransportError: true,
        bridgeGeneration: 4,
        message: "action transport failed",
      })),
    });

    const stateFailure = await loadDashboardState();
    const actionFailure = await runAction("cycle");

    expect(stateFailure.bridgeError).toBe(true);
    expect(stateFailure.bridgeGeneration).toBe(3);
    expect(stateFailure.notice.description).toContain("state transport failed");
    expect(actionFailure.bridgeError).toBe(true);
    expect(actionFailure.bridgeGeneration).toBe(4);
    expect(actionFailure.notice.description).toContain("action transport failed");
  });

  it("loads a profit report through its dedicated Electron IPC transport", async () => {
    const loadProfitReportBridge = vi.fn(async () => profitReport);
    vi.stubGlobal("stockbotBridge", {
      loadProfitReport: loadProfitReportBridge,
    });

    const result = await loadProfitReport({
      granularity: "day",
      scope: "account",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
    });

    expect(loadProfitReportBridge).toHaveBeenCalledWith({
      granularity: "day",
      scope: "account",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
    });
    expect(result).toEqual(profitReport);
    expect(result).not.toHaveProperty("stateRevision");
  });

  it("returns a dedicated profit report transport failure from Electron IPC", async () => {
    vi.stubGlobal("stockbotBridge", {
      loadProfitReport: vi.fn(async () => ({
        profitReportTransportError: true,
        bridgeGeneration: 7,
        message: "profit report transport failed",
      })),
    });

    const result = await loadProfitReport({
      granularity: "month",
      scope: "stockbot",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
    });

    expect(result).toEqual({
      profitReportTransportError: true,
      bridgeGeneration: 7,
      message: "Electron bridge profit report failed: profit report transport failed",
    });
  });

  it("uses the read-only HTTP endpoint when Electron IPC is unavailable", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => profitReport,
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await loadProfitReport({
      granularity: "hour",
      scope: "stockbot",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringMatching(
        /\/api\/profit-report\?granularity=hour&scope=stockbot&anchor=2026-07-29&timezone=Asia%2FSeoul$/,
      ),
      expect.objectContaining({ headers: expect.anything() }),
    );
    expect(result).toEqual(profitReport);
  });

  it("does not convert an HTTP profit report failure into DashboardState", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
    })));

    const result = await loadProfitReport({
      granularity: "year",
      scope: "account",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
    });

    expect(result).toEqual({
      profitReportTransportError: true,
      message: "HTTP bridge profit report failed: 503 Service Unavailable",
    });
    expect(result).not.toHaveProperty("stateRevision");
  });
});
