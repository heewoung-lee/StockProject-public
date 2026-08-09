const PROFIT_GRANULARITIES = new Set(["hour", "day", "month", "year"]);
const PROFIT_SCOPES = new Set(["account", "stockbot"]);
const PROFIT_REPORT_STATUSES = new Set(["complete", "partial", "empty", "unavailable"]);
const PROFIT_BUCKET_STATUSES = new Set([
  "confirmed",
  "provisional",
  "no_trade",
  "market_closed",
  "partial",
  "unavailable",
]);
const PROFIT_ACTIVITY_STATUSES = new Set(["trade", "no_trade", "unknown"]);
const PROFIT_QUERY_KEYS = ["anchor", "granularity", "scope", "timezone"];

function invalidQuery() {
  return new Error("invalid profit report query");
}

function invalidResponse(field) {
  return new Error(`invalid profit report response: ${field}`);
}

function isRecord(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isIsoDate(value) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    return false;
  }
  const [year, month, day] = value.split("-").map(Number);
  const leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const daysInMonth = [
    31,
    leapYear ? 29 : 28,
    31,
    30,
    31,
    30,
    31,
    31,
    30,
    31,
    30,
    31,
  ];
  return month >= 1 && month <= 12 && day >= 1 && day <= daysInMonth[month - 1];
}

function validatedProfitReportQuery(query) {
  if (!isRecord(query)) {
    throw invalidQuery();
  }
  const keys = Object.keys(query).sort();
  if (
    keys.length !== PROFIT_QUERY_KEYS.length
    || keys.some((key, index) => key !== PROFIT_QUERY_KEYS[index])
    || !PROFIT_GRANULARITIES.has(query.granularity)
    || !PROFIT_SCOPES.has(query.scope)
    || !isIsoDate(query.anchor)
    || query.timezone !== "Asia/Seoul"
  ) {
    throw invalidQuery();
  }
  return {
    granularity: query.granularity,
    scope: query.scope,
    anchor: query.anchor,
    timezone: query.timezone,
  };
}

function profitReportEndpoint(query) {
  const validated = validatedProfitReportQuery(query);
  const params = new URLSearchParams(validated);
  return `/api/profit-report?${params.toString()}`;
}

function requiredString(value, field) {
  if (typeof value !== "string" || !value.trim()) {
    throw invalidResponse(field);
  }
  return value;
}

function requiredTimestamp(value, field) {
  const text = requiredString(value, field);
  if (Number.isNaN(Date.parse(text))) {
    throw invalidResponse(field);
  }
  return text;
}

function nullableTimestamp(value, field) {
  if (value === null) {
    return null;
  }
  return requiredTimestamp(value, field);
}

function nullableDate(value, field) {
  if (value === null) {
    return null;
  }
  if (!isIsoDate(value)) {
    throw invalidResponse(field);
  }
  return value;
}

function nullableFiniteNumber(value, field) {
  if (value === null) {
    return null;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw invalidResponse(field);
  }
  return value;
}

function nullableCount(value, field) {
  if (value === null) {
    return null;
  }
  if (!Number.isSafeInteger(value) || value < 0) {
    throw invalidResponse(field);
  }
  return value;
}

function requiredCount(value, field) {
  const count = nullableCount(value, field);
  if (count === null) {
    throw invalidResponse(field);
  }
  return count;
}

function sanitizedIssues(value, field, redact) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) {
    throw invalidResponse(field);
  }
  return value.map((issue) => String(redact(issue)));
}

function sanitizedRange(value, redact) {
  if (!isRecord(value)) {
    throw invalidResponse("range");
  }
  if (!isIsoDate(value.anchor)) {
    throw invalidResponse("range.anchor");
  }
  return {
    label: String(redact(requiredString(value.label, "range.label"))),
    startAt: requiredTimestamp(value.startAt, "range.startAt"),
    endAt: requiredTimestamp(value.endAt, "range.endAt"),
    anchor: value.anchor,
    previousAnchor: nullableDate(value.previousAnchor, "range.previousAnchor"),
    nextAnchor: nullableDate(value.nextAnchor, "range.nextAnchor"),
  };
}

function sanitizedSummary(value) {
  if (!isRecord(value)) {
    throw invalidResponse("summary");
  }
  return {
    reportedRealizedPnlKrw: nullableFiniteNumber(
      value.reportedRealizedPnlKrw,
      "summary.reportedRealizedPnlKrw",
    ),
    profitableBucketsTotalKrw: nullableFiniteNumber(
      value.profitableBucketsTotalKrw,
      "summary.profitableBucketsTotalKrw",
    ),
    losingBucketsTotalKrw: nullableFiniteNumber(
      value.losingBucketsTotalKrw,
      "summary.losingBucketsTotalKrw",
    ),
    tradingCostKrw: nullableFiniteNumber(value.tradingCostKrw, "summary.tradingCostKrw"),
    profitableBucketCount: requiredCount(
      value.profitableBucketCount,
      "summary.profitableBucketCount",
    ),
    losingBucketCount: requiredCount(value.losingBucketCount, "summary.losingBucketCount"),
    availableBucketCount: requiredCount(
      value.availableBucketCount,
      "summary.availableBucketCount",
    ),
  };
}

function sanitizedBucket(value, index, redact) {
  const prefix = `buckets[${index}]`;
  if (!isRecord(value)) {
    throw invalidResponse(prefix);
  }
  if (!PROFIT_BUCKET_STATUSES.has(value.status)) {
    throw invalidResponse(`${prefix}.status`);
  }
  if (!PROFIT_ACTIVITY_STATUSES.has(value.activityStatus)) {
    throw invalidResponse(`${prefix}.activityStatus`);
  }
  if (value.costInclusion !== "unknown") {
    throw invalidResponse(`${prefix}.costInclusion`);
  }
  return {
    key: String(redact(requiredString(value.key, `${prefix}.key`))),
    label: String(redact(requiredString(value.label, `${prefix}.label`))),
    startAt: requiredTimestamp(value.startAt, `${prefix}.startAt`),
    endAt: requiredTimestamp(value.endAt, `${prefix}.endAt`),
    reportedRealizedPnlKrw: nullableFiniteNumber(
      value.reportedRealizedPnlKrw,
      `${prefix}.reportedRealizedPnlKrw`,
    ),
    feeKrw: nullableFiniteNumber(value.feeKrw, `${prefix}.feeKrw`),
    taxKrw: nullableFiniteNumber(value.taxKrw, `${prefix}.taxKrw`),
    interestKrw: nullableFiniteNumber(value.interestKrw, `${prefix}.interestKrw`),
    fillCount: nullableCount(value.fillCount, `${prefix}.fillCount`),
    status: value.status,
    activityStatus: value.activityStatus,
    costInclusion: "unknown",
    issues: sanitizedIssues(value.issues, `${prefix}.issues`, redact),
  };
}

function sanitizeProfitReportForRenderer(payload, redact = (value) => String(value)) {
  if (!isRecord(payload) || payload.schemaVersion !== 1) {
    throw invalidResponse("schemaVersion");
  }
  if (!PROFIT_REPORT_STATUSES.has(payload.status)) {
    throw invalidResponse("status");
  }
  if (payload.costInclusion !== "unknown") {
    throw invalidResponse("costInclusion");
  }
  if (!Array.isArray(payload.buckets)) {
    throw invalidResponse("buckets");
  }

  const query = validatedProfitReportQuery(payload.query);
  return {
    schemaVersion: 1,
    generatedAt: requiredTimestamp(payload.generatedAt, "generatedAt"),
    status: payload.status,
    costInclusion: "unknown",
    query,
    range: sanitizedRange(payload.range, redact),
    summary: sanitizedSummary(payload.summary),
    buckets: payload.buckets.map((bucket, index) => sanitizedBucket(bucket, index, redact)),
    issues: sanitizedIssues(payload.issues, "issues", redact),
    dataSource: String(redact(requiredString(payload.dataSource, "dataSource"))),
    updatedAt: nullableTimestamp(payload.updatedAt, "updatedAt"),
  };
}

module.exports = {
  profitReportEndpoint,
  sanitizeProfitReportForRenderer,
  validatedProfitReportQuery,
};
