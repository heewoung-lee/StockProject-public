import { createRequire } from "node:module";
import { describe, expect, it } from "vitest";

const require = createRequire(import.meta.url);
const {
  profitReportEndpoint,
  sanitizeProfitReportForRenderer,
} = require("../electron/profit_report_query.cjs");

function reportPayload() {
  return {
    schemaVersion: 1,
    generatedAt: "2026-07-29T01:02:03.000Z",
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
        issues: ["accountNo=test-account-01"],
        orderNo: "test-order-ref",
      },
    ],
    issues: ["token=secret-token"],
    dataSource: "KIS_PERIOD_PROFIT_LOCAL",
    updatedAt: "2026-07-29T15:31:00+09:00",
    appKey: "secret-app-key",
    accountNo: "test-account-01",
  };
}

describe("profit report query contract", () => {
  it("builds the fixed read-only endpoint with URLSearchParams encoding", () => {
    expect(
      profitReportEndpoint({
        granularity: "hour",
        scope: "stockbot",
        anchor: "2026-07-29",
        timezone: "Asia/Seoul",
      }),
    ).toBe(
      "/api/profit-report?granularity=hour&scope=stockbot&anchor=2026-07-29&timezone=Asia%2FSeoul",
    );
  });

  it("accepts every supported granularity and scope", () => {
    for (const granularity of ["hour", "day", "month", "year"]) {
      for (const scope of ["account", "stockbot"]) {
        expect(
          profitReportEndpoint({
            granularity,
            scope,
            anchor: "2026-07-29",
            timezone: "Asia/Seoul",
          }),
        ).toContain(`granularity=${granularity}&scope=${scope}`);
      }
    }
  });

  it.each([
    [null],
    [[]],
    [{}],
    [{ granularity: "week", scope: "account", anchor: "2026-07-29", timezone: "Asia/Seoul" }],
    [{ granularity: "day", scope: "all", anchor: "2026-07-29", timezone: "Asia/Seoul" }],
    [{ granularity: "day", scope: "account", anchor: "2026-02-30", timezone: "Asia/Seoul" }],
    [{ granularity: "day", scope: "account", anchor: "20260729", timezone: "Asia/Seoul" }],
    [{ granularity: "day", scope: "account", anchor: "2026-07-29", timezone: "UTC" }],
    [{
      granularity: "day",
      scope: "account",
      anchor: "2026-07-29",
      timezone: "Asia/Seoul",
      token: "must-not-pass",
    }],
  ])("rejects an invalid or over-specified query: %j", (query) => {
    expect(() => profitReportEndpoint(query)).toThrow(/invalid profit report query/);
  });

  it("projects only the versioned report contract and redacts diagnostics", () => {
    const sanitized = sanitizeProfitReportForRenderer(
      reportPayload(),
      (value: unknown) =>
        String(value)
          .replace(/test-account-01/g, "[REDACTED]")
          .replace(/secret-token/g, "[REDACTED]"),
    );
    const serialized = JSON.stringify(sanitized);

    expect(sanitized).toMatchObject({
      schemaVersion: 1,
      status: "complete",
      costInclusion: "unknown",
      buckets: [{ status: "confirmed", activityStatus: "trade", costInclusion: "unknown" }],
    });
    expect(sanitized).not.toHaveProperty("appKey");
    expect(sanitized).not.toHaveProperty("accountNo");
    expect(sanitized.buckets[0]).not.toHaveProperty("orderNo");
    expect(serialized).not.toMatch(/secret-app-key|secret-token|test-account-01|test-order-ref/);
    expect(serialized).toContain("[REDACTED]");
  });

  it.each([
    [{ ...reportPayload(), schemaVersion: 2 }],
    [{ ...reportPayload(), costInclusion: "excluded" }],
    [{ ...reportPayload(), status: "ok" }],
    [{ ...reportPayload(), buckets: [{ ...reportPayload().buckets[0], status: "unknown" }] }],
    [{ ...reportPayload(), buckets: [{ ...reportPayload().buckets[0], activityStatus: "mixed" }] }],
    [{
      ...reportPayload(),
      buckets: [{ ...reportPayload().buckets[0], activityStatus: undefined }],
    }],
    [{
      ...reportPayload(),
      summary: { ...reportPayload().summary, reportedRealizedPnlKrw: Infinity },
    }],
    [{
      ...reportPayload(),
      buckets: [{ ...reportPayload().buckets[0], costInclusion: "included" }],
    }],
  ])("rejects malformed bridge report payloads", (payload) => {
    expect(() => sanitizeProfitReportForRenderer(payload)).toThrow(/invalid profit report response/);
  });
});
