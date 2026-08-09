import type {
  DashboardState,
  ProfitReportQuery,
  ProfitReportResult,
  ProfitReportTransportFailure,
} from "./types";

interface BridgeTransportFailure {
  bridgeTransportError: true;
  bridgeGeneration?: number;
  message: string;
}

type BridgeRendererResponse = DashboardState | BridgeTransportFailure;
type ProfitReportRendererResponse = ProfitReportResult;

declare global {
  interface Window {
    stockbotBridge?: {
      loadState?: () => Promise<BridgeRendererResponse>;
      loadProfitReport?: (query: ProfitReportQuery) => Promise<ProfitReportRendererResponse>;
      runAction?: (action: string, payload?: Record<string, unknown>) => Promise<BridgeRendererResponse>;
    };
  }
}

const fallbackState: DashboardState = {
  stateRevision: 0,
  app: {
    title: "개미親주식",
    subtitle: "가상 자동매매",
    authorLabel: "MadeBy :heewoung-lee",
    authorUrl: "https://github.com/heewoung-lee",
    version: "0.1.0",
  },
  mode: { key: "virtual", label: "가상 모드", isReal: false },
  runtime: {
    status: "정지",
    running: false,
    cycleLabel: "예약 없음",
    lastUpdated: "-",
    dataSourceKind: "local",
    dataModeDescription: "샘플/로컬 데이터로 반복 검증합니다.",
    safetySummary: "실제 주문 없음 · 로컬 데이터 replay",
    cleanupMode: false,
  },
  notice: {
    title: "PAPER 안전 모드",
    description: "가상모드입니다. Python bridge 연결 후 paper runtime 상태를 표시합니다.",
    tone: "paper",
  },
  account: {
    title: "계좌 상태",
    metrics: [
      { label: "상태", value: "연결 전", emphasis: true },
      { label: "계좌", value: "가상계좌" },
      { label: "현금", value: "0원", emphasis: true },
      { label: "평가금", value: "0원", emphasis: true },
      { label: "보유 종목", value: "0개", emphasis: true },
      { label: "매수 가능", value: "0원", emphasis: true },
      { label: "조회 종목 현재가", value: "0원" },
      { label: "최근 갱신", value: "-" },
    ],
    summary: [],
  },
  positions: [],
  selectedPosition: null,
  logs: {
    trades: [],
    system: [{ timestamp: "-", level: "info", title: "Bridge", message: "Python bridge 연결 대기 중" }],
  },
};

function errorText(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return String(error || "unknown bridge error");
}

function bridgeFailureState(context: string, error: unknown, bridgeGeneration?: number): DashboardState {
  const message = `${context}: ${errorText(error)}`;
  return {
    ...fallbackState,
    ...(Number.isSafeInteger(bridgeGeneration) && Number(bridgeGeneration) > 0 ? { bridgeGeneration } : {}),
    bridgeError: true,
    runtime: { ...fallbackState.runtime, status: "브리지 오류", running: false },
    notice: {
      ...fallbackState.notice,
      title: "브리지 연결 실패",
      description: message,
      tone: "danger",
    },
    account: {
      ...fallbackState.account,
      metrics: fallbackState.account.metrics.map((metric, index) =>
        index === 0 ? { ...metric, value: "브리지 오류", emphasis: true } : metric,
      ),
    },
    logs: {
      ...fallbackState.logs,
      system: [{ timestamp: "-", level: "error", title: "Bridge", message }, ...fallbackState.logs.system],
    },
  };
}

function isBridgeTransportFailure(response: BridgeRendererResponse): response is BridgeTransportFailure {
  return "bridgeTransportError" in response && response.bridgeTransportError === true;
}

function dashboardStateFromBridgeResponse(context: string, response: BridgeRendererResponse): DashboardState {
  if (isBridgeTransportFailure(response)) {
    return bridgeFailureState(context, response.message, response.bridgeGeneration);
  }
  return response;
}

function profitReportFailure(
  context: string,
  error: unknown,
  bridgeGeneration?: number,
): ProfitReportTransportFailure {
  return {
    profitReportTransportError: true,
    ...(Number.isSafeInteger(bridgeGeneration) && Number(bridgeGeneration) > 0
      ? { bridgeGeneration }
      : {}),
    message: `${context}: ${errorText(error)}`,
  };
}

function isProfitReportTransportFailure(
  response: ProfitReportRendererResponse,
): response is ProfitReportTransportFailure {
  return (
    "profitReportTransportError" in response
    && response.profitReportTransportError === true
  );
}

function profitReportFromBridgeResponse(
  context: string,
  response: ProfitReportRendererResponse,
): ProfitReportResult {
  if (isProfitReportTransportFailure(response)) {
    return profitReportFailure(context, response.message, response.bridgeGeneration);
  }
  return response;
}

function profitReportQueryString(query: ProfitReportQuery): string {
  return new URLSearchParams({
    granularity: query.granularity,
    scope: query.scope,
    anchor: query.anchor,
    timezone: query.timezone,
  }).toString();
}

export function bridgeBaseUrl(): string {
  return import.meta.env.DEV ? import.meta.env.VITE_STOCKBOT_BRIDGE_URL || "http://127.0.0.1:8765" : "";
}

export function bridgeHeaders(): HeadersInit {
  const token = import.meta.env.DEV ? import.meta.env.VITE_STOCKBOT_BRIDGE_TOKEN || "" : "";
  return token ? { "X-StockBot-Bridge-Token": token } : {};
}

export async function loadDashboardState(): Promise<DashboardState> {
  if (window.stockbotBridge?.loadState) {
    try {
      const response = await window.stockbotBridge.loadState();
      return dashboardStateFromBridgeResponse("Electron bridge loadState failed", response);
    } catch (error) {
      return bridgeFailureState("Electron bridge loadState failed", error);
    }
  }
  if (typeof fetch === "undefined") {
    return fallbackState;
  }
  const baseUrl = bridgeBaseUrl();
  if (!baseUrl) {
    return fallbackState;
  }
  try {
    const response = await fetch(`${baseUrl}/api/state`, { headers: bridgeHeaders() });
    if (!response.ok) {
      return bridgeFailureState("HTTP bridge state request failed", new Error(`${response.status} ${response.statusText}`));
    }
    return (await response.json()) as DashboardState;
  } catch (error) {
    return bridgeFailureState("HTTP bridge state request failed", error);
  }
}

export async function runAction(action: string, payload: Record<string, unknown> = {}): Promise<DashboardState> {
  if (window.stockbotBridge?.runAction) {
    try {
      const response = await window.stockbotBridge.runAction(action, payload);
      return dashboardStateFromBridgeResponse(`Electron bridge action failed (${action})`, response);
    } catch (error) {
      return bridgeFailureState(`Electron bridge action failed (${action})`, error);
    }
  }
  const baseUrl = bridgeBaseUrl();
  if (!baseUrl) {
    return bridgeFailureState(`HTTP bridge action failed (${action})`, new Error("bridge URL is not configured"));
  }
  try {
    const response = await fetch(`${baseUrl}/api/actions/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...bridgeHeaders() },
      body: JSON.stringify(payload),
    });
    if (!response.ok) {
      return bridgeFailureState(`HTTP bridge action failed (${action})`, new Error(`${response.status} ${response.statusText}`));
    }
    return (await response.json()) as DashboardState;
  } catch (error) {
    return bridgeFailureState(`HTTP bridge action failed (${action})`, error);
  }
}

export async function loadProfitReport(query: ProfitReportQuery): Promise<ProfitReportResult> {
  const context = "Electron bridge profit report failed";
  if (window.stockbotBridge?.loadProfitReport) {
    try {
      const response = await window.stockbotBridge.loadProfitReport(query);
      return profitReportFromBridgeResponse(context, response);
    } catch (error) {
      return profitReportFailure(context, error);
    }
  }

  const baseUrl = bridgeBaseUrl();
  if (!baseUrl) {
    return profitReportFailure(
      "HTTP bridge profit report failed",
      new Error("bridge URL is not configured"),
    );
  }
  try {
    const endpoint =
      `${baseUrl.replace(/\/$/, "")}/api/profit-report?${profitReportQueryString(query)}`;
    const response = await fetch(endpoint, { headers: bridgeHeaders() });
    if (!response.ok) {
      return profitReportFailure(
        "HTTP bridge profit report failed",
        new Error(`${response.status} ${response.statusText}`),
      );
    }
    return (await response.json()) as ProfitReportResult;
  } catch (error) {
    return profitReportFailure("HTTP bridge profit report failed", error);
  }
}
