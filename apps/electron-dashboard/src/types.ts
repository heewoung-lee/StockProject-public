export type Tone = "paper" | "danger" | "neutral" | "real";

export type ProfitGranularity = "hour" | "day" | "month" | "year";
export type ProfitScope = "account" | "stockbot";
export type ProfitReportStatus = "complete" | "partial" | "empty" | "unavailable";
export type ProfitBucketStatus =
  | "confirmed"
  | "provisional"
  | "no_trade"
  | "market_closed"
  | "partial"
  | "unavailable";
export type ProfitActivityStatus = "trade" | "no_trade" | "unknown";

export interface ProfitReportQuery {
  granularity: ProfitGranularity;
  scope: ProfitScope;
  anchor: string;
  timezone: "Asia/Seoul";
}

export interface ProfitReportRange {
  label: string;
  startAt: string;
  endAt: string;
  anchor: string;
  previousAnchor: string | null;
  nextAnchor: string | null;
}

export interface ProfitReportSummary {
  reportedRealizedPnlKrw: number | null;
  profitableBucketsTotalKrw: number | null;
  losingBucketsTotalKrw: number | null;
  tradingCostKrw: number | null;
  profitableBucketCount: number;
  losingBucketCount: number;
  availableBucketCount: number;
}

export interface ProfitBucket {
  key: string;
  label: string;
  startAt: string;
  endAt: string;
  reportedRealizedPnlKrw: number | null;
  feeKrw: number | null;
  taxKrw: number | null;
  interestKrw: number | null;
  fillCount: number | null;
  status: ProfitBucketStatus;
  activityStatus: ProfitActivityStatus;
  costInclusion: "unknown";
  issues: string[];
}

export interface ProfitReport {
  schemaVersion: 1;
  generatedAt: string;
  bridgeGeneration?: number;
  status: ProfitReportStatus;
  costInclusion: "unknown";
  query: ProfitReportQuery;
  range: ProfitReportRange;
  summary: ProfitReportSummary;
  buckets: ProfitBucket[];
  issues: string[];
  dataSource: string;
  updatedAt: string | null;
}

export interface ProfitReportTransportFailure {
  profitReportTransportError: true;
  bridgeGeneration?: number;
  message: string;
}

export type ProfitReportResult = ProfitReport | ProfitReportTransportFailure;

export interface DashboardState {
  stateRevision: number;
  bridgeGeneration?: number;
  bridgeError?: boolean;
  actionPopup?: ActionPopup;
  app: {
    title: string;
    subtitle: string;
    authorLabel: string;
    authorUrl: string;
    version: string;
  };
  mode: {
    key: "virtual" | "real";
    label: string;
    isReal: boolean;
  };
  runtime: {
    status: string;
    running: boolean;
    schedulerOwner?: "renderer" | "service";
    schedulerActive?: boolean;
    schedulerCycleInProgress?: boolean;
    schedulerIntervalSeconds?: number | null;
    schedulerSecondsUntilNextCycle?: number | null;
    schedulerFailureCount?: number;
    schedulerErrorStage?: string;
    schedulerErrorCode?: string;
    cycleLabel: string;
    lastUpdated: string;
    dataSource?: string;
    dataSourceKind: "local" | "kis-vts" | "external-scan-kis" | "real-prep" | "real-read-only" | "live" | "unknown";
    dataModeLabel?: string;
    dataModeDescription: string;
    safetySummary: string;
    cleanupMode: boolean;
  };
  notice: {
    title: string;
    description: string;
    tone: Tone;
    locked?: boolean;
    orderEnabled?: boolean;
    ready?: boolean;
  };
  account: {
    title: string;
    metrics: Metric[];
    summary: Metric[];
  };
  positions: PositionRow[];
  selectedPosition: PositionDetail | null;
  logs: {
    trades: TradeLog[];
    system: SystemLog[];
  };
  settings?: {
    kisLiveCredentials?: {
      appKeySaved: boolean;
      appSecretSaved: boolean;
      accountNoSaved: boolean;
      productCodeSaved: boolean;
    };
    liveOrderApproval?: {
      allowSaved: boolean;
      enabledSaved: boolean;
      confirmationSaved: boolean;
      accountConfirmationSaved: boolean;
      sessionApproved?: boolean;
      riskLimitsOk?: boolean;
      newEntriesAllowed?: boolean;
    };
  };
  debug?: Record<string, unknown>;
}

export interface ActionPopup {
  title: string;
  message: string;
  tone: "warning" | "danger" | "paper" | "neutral";
}

export interface Metric {
  label: string;
  value: string;
  emphasis?: boolean;
  hint?: string;
}

export interface PositionRow {
  symbol: string;
  companyName: string;
  label: string;
  side: "롱" | "숏";
  quantity: number;
  avgPrice: string;
  lastPrice: string;
  unrealizedPnl: string;
  pnlTone: "positive" | "negative" | "neutral";
}

export interface PositionDetail {
  symbol: string;
  companyName: string;
  label: string;
  side: "롱" | "숏";
  quantity: number;
  summary: string;
  avgPrice: string;
  lastPrice: string;
  unrealizedPnl: string;
  pricePoints: PricePoint[];
  referenceLines: ReferenceLine[];
}

export interface PricePoint {
  time: string;
  value: number;
}

export interface ReferenceLine {
  label: string;
  value: number;
}

export type LogLevel = "buy" | "sell" | "short" | "rejected" | "info" | "warning" | "error";

export interface TradeLog {
  title: string;
  detail: string;
  level: LogLevel;
  timestamp?: string;
  symbol?: string;
  companyName?: string;
  side?: string;
  sideLabel?: string;
  quantity?: number;
  price?: number;
  priceText?: string;
  result?: string;
  reason?: string;
  mode?: string;
  realizedPnl?: number;
  realizedPnlText?: string;
}

export interface SystemLog {
  timestamp: string;
  level: string;
  title: string;
  message: string;
}
