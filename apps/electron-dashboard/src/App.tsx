import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  TouchEvent as ReactTouchEvent,
} from "react";

import { loadDashboardState, loadProfitReport, runAction } from "./api";
import type {
  ActionPopup,
  DashboardState,
  LogLevel,
  PositionDetail,
  PositionRow,
  ProfitBucket,
  ProfitBucketStatus,
  ProfitGranularity,
  ProfitReport,
  ProfitScope,
  ReferenceLine,
  SystemLog,
  TradeLog,
} from "./types";

import "./App.css";

const BRAND_ICON_SRC = `${import.meta.env.BASE_URL}stockbot-donghak-ant-icon.png`;
const FIRST_CYCLE_DELAY_MS = 250;
const LOCAL_CYCLE_INTERVAL_MS = 5000;
const KIS_CYCLE_INTERVAL_MS = 60000;
const LIVE_CYCLE_INTERVAL_MS = 15000;
const MASKED_CREDENTIAL = "**********";
const PROFIT_TIMEZONE = "Asia/Seoul" as const;
type ActiveView = "dashboard" | "trade-logs" | "profit-analysis" | "diagnostic-logs" | "environment-settings";
type TradingModeKey = DashboardState["mode"]["key"];
const ACCOUNT_OVERVIEW_METRIC_LABELS: Record<TradingModeKey, readonly string[]> = {
  virtual: ["현금", "평가금", "보유 종목", "매수 가능"],
  real: ["예수금", "평가금", "보유 종목", "매수 가능"],
};

type ModeSwitchPrompt = {
  targetMode: TradingModeKey;
  kind: "real-to-paper-holdings";
  holdingsLabel?: string;
};

type RealModeLoadingStage = "switching" | "refreshing";

type KisLiveCredentialDraft = {
  appKey: string;
  appSecret: string;
  accountNo: string;
  productCode: string;
};

type KisLiveCredentialField = keyof KisLiveCredentialDraft;

type DiagnosticStatus = {
  key: string;
  label: string;
  detail: string;
  tone: "info" | "warning" | "error" | "success";
};

type TradeTone = "buy" | "sell" | "short";

type ModeBadgeTone = "paper" | "real";
type PanelBadgeTone = ModeBadgeTone | "local" | "diag";

type ParsedTradeLog = {
  key: string;
  time: string;
  sideLabel: string;
  sideTone: TradeTone;
  resultRaw: string;
  resultLabel: string;
  target: string;
  companyName: string;
  symbol: string;
  quantity: string;
  quantityValue: number;
  price: string;
  priceValue: number;
  reasonLabel: string;
  realizedPnl: string;
  realizedPnlValue: number;
  returnRate: string;
  returnTone: "positive" | "negative" | "neutral";
  entryPriceValue: number;
  isShort: boolean;
};

const SENSITIVE_DIAGNOSTIC_MARKER = /(?:KIS_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|ACCOUNT)[A-Z0-9_]*|access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|app[_\s-]?secret|app[_\s-]?key|appkey|appsecret|api[_\s-]?key|account[_\s-]?no|account[_\s-]?number|accountno|acct|cano|acnt|authorization|bearer|token)/i;

function cycleIntervalForState(state: DashboardState | null): number {
  const serviceIntervalSeconds = state?.runtime.schedulerIntervalSeconds;
  if (
    state?.runtime.schedulerOwner === "service"
    && typeof serviceIntervalSeconds === "number"
    && Number.isFinite(serviceIntervalSeconds)
    && serviceIntervalSeconds > 0
  ) {
    return serviceIntervalSeconds * 1000;
  }
  const kind = state?.runtime.dataSourceKind;
  if (kind === "live") {
    return LIVE_CYCLE_INTERVAL_MS;
  }
  return kind === "kis-vts" || kind === "external-scan-kis"
    ? KIS_CYCLE_INTERVAL_MS
    : LOCAL_CYCLE_INTERVAL_MS;
}

function firstCycleDelayForState(state: DashboardState | null): number {
  return state?.runtime.dataSourceKind === "live" ? LIVE_CYCLE_INTERVAL_MS : FIRST_CYCLE_DELAY_MS;
}

function cycleIntervalLabelForState(state: DashboardState | null): string {
  const seconds = cycleIntervalForState(state) / 1000;
  return state?.runtime.schedulerOwner === "service"
    ? `완료 후 대기 ${seconds}초`
    : `주기 ${seconds}초`;
}

function cycleCountdownLabelForState(state: DashboardState | null, secondsRemaining: number | null): string {
  if (state?.runtime.schedulerOwner === "service" && state.runtime.schedulerCycleInProgress === true) {
    return "cycle 실행 중";
  }
  const serviceSecondsRemaining = state?.runtime.schedulerSecondsUntilNextCycle;
  const serviceCountdownAvailable = (
    state?.runtime.schedulerOwner === "service"
    && state.runtime.schedulerActive === true
    && typeof serviceSecondsRemaining === "number"
    && Number.isFinite(serviceSecondsRemaining)
    && serviceSecondsRemaining >= 0
  );
  if (serviceCountdownAvailable && secondsRemaining !== null) {
    return `다음 cycle까지 ${secondsRemaining}초`;
  }
  if (!state?.runtime.running) {
    return state?.runtime.cycleLabel ?? "예약 없음";
  }
  if (secondsRemaining === null) {
    return state.runtime.cycleLabel;
  }
  return `다음 cycle까지 ${secondsRemaining}초`;
}

function secondsUntil(timestampMs: number | null, nowMs: number): number | null {
  if (timestampMs === null) {
    return null;
  }
  return Math.max(0, Math.ceil((timestampMs - nowMs) / 1000));
}

export function diagnosticStatusesForLogs(system: SystemLog[]): DiagnosticStatus[] {
  const statuses: DiagnosticStatus[] = [];
  const perSecondLimitPatterns = ["EGW00201", "EGW00215", "초당 거래건수", "초당 요청 제한", "KIS 초당"];
  const perSecondLimitCount = countDiagnosticMatches(system, perSecondLimitPatterns);
  const latestPerSecondLimitIndex = system.findIndex((entry) => diagnosticEntryMatches(entry, perSecondLimitPatterns));
  const latestSuccessfulLiveCheckIndex = system.findIndex(
    (entry) => entry.level.toLowerCase() === "success" && entry.title === "KIS 실전 조회 확인",
  );
  const perSecondLimitRecovered =
    latestPerSecondLimitIndex >= 0 &&
    latestSuccessfulLiveCheckIndex >= 0 &&
    latestSuccessfulLiveCheckIndex < latestPerSecondLimitIndex;
  const expiredTokenCount = countDiagnosticMatches(system, ["EGW00123", "기간이 만료된"]);
  const tokenLimitCount = countDiagnosticMatches(system, ["EGW00133", "접근토큰", "1분당 1회"]);
  const timeoutCount = countDiagnosticMatches(system, ["network timeout", "timed out", "timeout", "시간 초과"]);
  const warmupCount = countDiagnosticMatches(system, ["insufficient_data", "데이터 누적", "가격 샘플 부족"]);
  const livePendingOrderCount = countDiagnosticMatches(system, [
    "pending live order requires reconciliation",
    "live pending orders unresolved",
    "live_pending_orders_unresolved",
    "live_order_pending",
  ]);
  const scannerSnapshotStaleCount = countDiagnosticMatches(system, [
    "stale scanner snapshot",
    "scanner snapshot refresh failed",
    "snapshot is stale",
  ]);
  const scannerSnapshotMissingCount = countDiagnosticMatches(system, [
    "scanner_snapshot.json 파일이 없습니다",
    "스캐너 스냅샷",
    "missing scanner snapshot",
    "scanner snapshot is missing",
    "scanner_snapshot.json is missing",
    "scanner_snapshot.json file does not exist",
  ]);

  if (scannerSnapshotStaleCount > 0) {
    statuses.push({
      key: "scanner-snapshot-stale",
      label: "스캐너 데이터 갱신 필요",
      detail: `${scannerSnapshotStaleCount}건 감지 - data/scanner_snapshot.json이 오래되었습니다. 외부 수집기를 다시 실행한 뒤 KIS 장중 테스트로 전환하세요.`,
      tone: "warning",
    });
  }
  if (scannerSnapshotMissingCount > 0 && scannerSnapshotStaleCount === 0) {
    statuses.push({
      key: "scanner-snapshot-missing",
      label: "스캐너 스냅샷 필요",
      detail: `${scannerSnapshotMissingCount}건 감지 - data/scanner_snapshot.json을 생성한 뒤 KIS 장중 테스트로 전환해야 합니다.`,
      tone: "warning",
    });
  }
  if (livePendingOrderCount > 0) {
    statuses.push({
      key: "live-pending-order",
      label: "실전 미체결 동기화",
      detail: `${livePendingOrderCount}건 감지 - KIS 미체결 주문이 있어 live runtime이 상태를 재확인해야 합니다. 신규 주문은 동기화 완료 전까지 차단됩니다.`,
      tone: "warning",
    });
  }
  if (perSecondLimitCount > 0) {
    statuses.push(
      perSecondLimitRecovered
        ? {
            key: "kis-rate-limit-recovered",
            label: "KIS 초당 제한 복구됨",
            detail: `${perSecondLimitCount}건 이력 - 이후 실전 계좌 조회 성공이 확인되었습니다.`,
            tone: "success",
          }
        : {
            key: "kis-rate-limit",
            label: "KIS 초당 제한",
            detail: `${perSecondLimitCount}건 감지 - 요청 간격을 늘리거나 조회 종목을 줄여야 합니다.`,
            tone: "warning",
          },
    );
  }
  if (expiredTokenCount > 0) {
    statuses.push({
      key: "kis-token-expired",
      label: "KIS 토큰 만료",
      detail: `${expiredTokenCount}건 감지 - 앱이 자동으로 토큰 캐시를 비우고 1회 재발급 후 같은 종목을 다시 조회합니다.`,
      tone: "warning",
    });
  }
  if (tokenLimitCount > 0) {
    statuses.push({
      key: "kis-token-limit",
      label: "토큰 발급 제한",
      detail: `${tokenLimitCount}건 감지 - 토큰 캐시를 사용하고 1분 내 재발급을 피해야 합니다.`,
      tone: "warning",
    });
  }
  if (timeoutCount > 0) {
    statuses.push({
      key: "quote-timeout",
      label: "시세 조회 지연",
      detail: `${timeoutCount}건 감지 - KIS 응답 지연으로 해당 종목은 건너뜁니다.`,
      tone: "warning",
    });
  }
  if (warmupCount > 0) {
    statuses.push({
      key: "warmup",
      label: "데이터 누적 중",
      detail: `${warmupCount}건 감지 - 전략 판단용 가격 샘플이 더 필요합니다.`,
      tone: "info",
    });
  }
  if (!statuses.length) {
    statuses.push({
      key: system.length ? "no-critical-error" : "empty",
      label: system.length ? "최근 치명 오류 없음" : "기록 없음",
      detail: system.length ? "현재 표시된 로그에서 KIS 제한/timeout 패턴은 감지되지 않았습니다." : "아직 진단 로그가 없습니다.",
      tone: system.length ? "success" : "info",
    });
  }
  return statuses;
}

function countDiagnosticMatches(system: SystemLog[], patterns: string[]): number {
  return system.reduce((count, entry) => count + (diagnosticEntryMatches(entry, patterns) ? 1 : 0), 0);
}

function diagnosticEntryMatches(entry: SystemLog, patterns: string[]): boolean {
  const text = `${entry.level} ${entry.title} ${entry.message}`.toLowerCase();
  return patterns.some((pattern) => text.includes(pattern.toLowerCase()));
}

export function buildDiagnosticExportText(state: DashboardState): string {
  const payload = {
    generatedAt: new Date().toISOString(),
    stateRevision: state.stateRevision,
    bridgeGeneration: state.bridgeGeneration ?? null,
    app: state.app,
    mode: state.mode,
    runtime: state.runtime,
    notice: state.notice,
    account: {
      title: state.account.title,
      metrics: state.account.metrics,
      summary: state.account.summary,
    },
    settings: diagnosticSettingsSummary(state.settings),
    diagnostics: diagnosticStatusesForLogs(state.logs.system),
    debug: {
      ...(state.debug ?? {}),
      exportState: {
        stateRevision: state.stateRevision,
        runtimeStatus: state.runtime.status,
        runtimeRunning: state.runtime.running,
        dataSourceKind: state.runtime.dataSourceKind,
        dataModeLabel: state.runtime.dataModeLabel ?? "",
        positionCount: state.positions.length,
        tradeLogCount: state.logs.trades.length,
        systemLogCount: state.logs.system.length,
      },
    },
    positions: state.positions.map((position) => ({
      symbol: position.symbol,
      companyName: position.companyName,
      side: position.side,
      quantity: position.quantity,
      avgPrice: position.avgPrice,
      lastPrice: position.lastPrice,
      unrealizedPnl: position.unrealizedPnl,
    })),
    logs: {
      trades: state.logs.trades.map((entry) => ({
        ...entry,
        title: redactDiagnosticText(entry.title),
        detail: redactDiagnosticText(entry.detail),
      })),
      system: state.logs.system.map((entry) => ({
        ...entry,
        title: redactDiagnosticText(entry.title),
        message: redactDiagnosticText(entry.message),
      })),
    },
  };
  return JSON.stringify(redactDiagnosticPayload(payload), null, 2);
}

function diagnosticSettingsSummary(settings: DashboardState["settings"] | undefined) {
  const credentials = settings?.kisLiveCredentials;
  const credentialValues = [
    credentials?.appKeySaved,
    credentials?.appSecretSaved,
    credentials?.accountNoSaved,
    credentials?.productCodeSaved,
  ].map(Boolean);
  const savedCount = credentialValues.filter(Boolean).length;
  const approval = settings?.liveOrderApproval;
  return {
    credentialFields: {
      requiredCount: credentialValues.length,
      savedCount,
      complete: savedCount === credentialValues.length,
    },
    liveOrderGate: {
      allowSaved: Boolean(approval?.allowSaved),
      enabledSaved: Boolean(approval?.enabledSaved),
      confirmationPhraseSaved: Boolean(approval?.confirmationSaved),
      scopeConfirmationSaved: Boolean(approval?.accountConfirmationSaved),
      sessionApproved: Boolean(approval?.sessionApproved),
      riskLimitsOk: Boolean(approval?.riskLimitsOk),
      newEntriesAllowed: Boolean(approval?.newEntriesAllowed),
    },
  };
}

function redactDiagnosticText(value: string): string {
  return value
    .replace(/\bAuthorization\b\s*[:=]?\s*Bearer\s+[^\s,;]+/gi, "[REDACTED]")
    .replace(/\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi, "[REDACTED]")
    .replace(/\bKIS_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|ACCOUNT)[A-Z0-9_]*\s*[:=]\s*[^\s,;]+/gi, "[REDACTED]")
    .replace(
      /\b(access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|app[_\s-]?secret|app[_\s-]?key|appkey|appsecret|api[_\s-]?key|authorization|bearer|token)\b(?:\s*[:=]\s*|\s+)[^\s,;]+/gi,
      "[REDACTED]",
    )
    .replace(/\b(access[_\s-]?token|refresh[_\s-]?token|id[_\s-]?token|app[_\s-]?secret|app[_\s-]?key|appkey|appsecret|api[_\s-]?key|authorization|bearer|token)\b/gi, "[REDACTED]")
    .replace(
      /\b(?:secret|token)[-_][A-Za-z0-9._-]+\b|\b[A-Za-z0-9._-]+[-_](?:secret|token)(?:[-_][A-Za-z0-9._-]+)?\b/gi,
      "[REDACTED]",
    )
    .replace(/\b(account|계좌)\s+[\d-]{8,}/gi, "$1 [REDACTED]")
    .replace(/\b\d{8}-?\d{0,2}\b/g, "[REDACTED]")
    .replace(/\b[A-Z]:\\[^\r\n,;]*/gi, "[REDACTED_PATH]");
}

function redactDiagnosticPayload(value: unknown): unknown {
  if (typeof value === "string") {
    return redactDiagnosticText(value);
  }
  if (Array.isArray(value)) {
    return value.map((item) => redactDiagnosticPayload(item));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [redactDiagnosticKey(key), redactDiagnosticPayload(item)]),
    );
  }
  return value;
}

function redactDiagnosticKey(key: string): string {
  if (SENSITIVE_DIAGNOSTIC_MARKER.test(key)) {
    return "[REDACTED_KEY]";
  }
  return key;
}

function diagnosticExportFileName(date: Date): string {
  const stamp = date
    .toISOString()
    .replace(/[-:]/g, "")
    .replace(/\.\d{3}Z$/, "Z");
  return `stockbot-diagnostics-${stamp}.json`;
}

function downloadTextFile(filename: string, text: string): void {
  const blob = new Blob([text], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function App() {
  const [state, setState] = useState<DashboardState | null>(null);
  const [busyAction, setBusyAction] = useState<string>("");
  const [kisLiveEditing, setKisLiveEditing] = useState(false);
  const [activeView, setActiveView] = useState<ActiveView>("dashboard");
  const [profitGranularity, setProfitGranularity] = useState<ProfitGranularity>("day");
  const [profitScope, setProfitScope] = useState<ProfitScope>("account");
  const [profitToday, setProfitToday] = useState(currentKstDate);
  const [profitFollowsToday, setProfitFollowsToday] = useState(true);
  const [profitAnchor, setProfitAnchor] = useState(currentKstDate);
  const [profitReport, setProfitReport] = useState<ProfitReport | null>(null);
  const [profitError, setProfitError] = useState("");
  const [profitLoading, setProfitLoading] = useState(false);
  const [profitRetryNonce, setProfitRetryNonce] = useState(0);
  const [actionPopup, setActionPopup] = useState<ActionPopup | null>(null);
  const [modeSwitchPrompt, setModeSwitchPrompt] = useState<ModeSwitchPrompt | null>(null);
  const [realModeLoading, setRealModeLoading] = useState<RealModeLoadingStage | null>(null);
  const [nextCycleDueAtMs, setNextCycleDueAtMs] = useState<number | null>(null);
  const [countdownNowMs, setCountdownNowMs] = useState(() => Date.now());
  const cycleInFlightRef = useRef(false);
  const bridgeActionInFlightCountRef = useRef(0);
  const stateRefreshInFlightRef = useRef(false);
  const stateRevisionRef = useRef<number | null>(null);
  const bridgeGenerationRef = useRef<number | null>(null);
  const acceptedStateRef = useRef<DashboardState | null>(null);
  const profitRequestSequenceRef = useRef(0);
  const cycleIntervalMs = useMemo(
    () => cycleIntervalForState(state),
    [
      state?.runtime.dataSourceKind,
      state?.runtime.schedulerIntervalSeconds,
      state?.runtime.schedulerOwner,
    ],
  );
  const firstCycleDelayMs = useMemo(() => firstCycleDelayForState(state), [state?.runtime.dataSourceKind]);
  const acceptedBridgeGeneration = state?.bridgeGeneration ?? null;
  const acceptedStateRevision = state?.stateRevision ?? null;

  const beginBridgeAction = useCallback(() => {
    bridgeActionInFlightCountRef.current += 1;
  }, []);

  const finishBridgeAction = useCallback(() => {
    bridgeActionInFlightCountRef.current = Math.max(0, bridgeActionInFlightCountRef.current - 1);
  }, []);

  const acceptDashboardState = useCallback(
    (
      next: DashboardState,
      options: { preserveRuntimeOnBridgeError?: boolean } = {},
    ): DashboardState | null => {
      const nextBridgeGeneration =
        typeof next.bridgeGeneration === "number" &&
        Number.isSafeInteger(next.bridgeGeneration) &&
        next.bridgeGeneration > 0
          ? next.bridgeGeneration
          : null;
      const currentBridgeGeneration = bridgeGenerationRef.current;
      if (currentBridgeGeneration !== null) {
        if (nextBridgeGeneration === null || nextBridgeGeneration < currentBridgeGeneration) {
          return null;
        }
        if (nextBridgeGeneration > currentBridgeGeneration) {
          bridgeGenerationRef.current = nextBridgeGeneration;
          stateRevisionRef.current = null;
        }
      } else if (nextBridgeGeneration !== null) {
        bridgeGenerationRef.current = nextBridgeGeneration;
      }
      if (next.bridgeError) {
        const merged = mergeBridgeFailureState(acceptedStateRef.current, next, {
          preserveRuntime: options.preserveRuntimeOnBridgeError !== false,
        });
        acceptedStateRef.current = merged;
        setState(merged);
        return merged;
      }
      const nextRevision = typeof next.stateRevision === "number" ? next.stateRevision : null;
      const currentRevision = stateRevisionRef.current;
      if (nextRevision === null && currentRevision !== null) {
        return null;
      }
      if (nextRevision !== null && currentRevision !== null && nextRevision < currentRevision) {
        return null;
      }
      if (nextRevision !== null) {
        stateRevisionRef.current = currentRevision === null ? nextRevision : Math.max(currentRevision, nextRevision);
      }
      acceptedStateRef.current = next;
      setState(next);
      return next;
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    const refresh = async () => {
      if (bridgeActionInFlightCountRef.current > 0 || stateRefreshInFlightRef.current) {
        return;
      }
      stateRefreshInFlightRef.current = true;
      try {
        const next = await loadDashboardState();
        if (!cancelled) {
          setProfitToday(currentKstDate());
          acceptDashboardState(next);
        }
      } finally {
        stateRefreshInFlightRef.current = false;
      }
    };
    void refresh();
    const timer = window.setInterval(refresh, 2500);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [acceptDashboardState]);

  useEffect(() => {
    if (profitFollowsToday && profitAnchor !== profitToday) {
      setProfitAnchor(profitToday);
    }
  }, [profitAnchor, profitFollowsToday, profitToday]);

  useEffect(() => {
    if (activeView !== "profit-analysis") {
      profitRequestSequenceRef.current += 1;
      setProfitLoading(false);
      return;
    }

    const requestSequence = profitRequestSequenceRef.current + 1;
    profitRequestSequenceRef.current = requestSequence;
    const query = {
      granularity: profitGranularity,
      scope: profitScope,
      anchor: profitAnchor,
      timezone: PROFIT_TIMEZONE,
    } as const;
    setProfitReport(null);
    setProfitError("");
    setProfitLoading(true);

    void loadProfitReport(query)
      .then((result) => {
        if (profitRequestSequenceRef.current !== requestSequence) {
          return;
        }
        if ("profitReportTransportError" in result) {
          setProfitError(result.message);
          return;
        }
        if (
          acceptedBridgeGeneration !== null
          && result.bridgeGeneration !== acceptedBridgeGeneration
        ) {
          setProfitError("현재 브리지 상태와 다른 손익 보고서입니다. 다시 조회해 주세요.");
          return;
        }
        if (
          result.query.granularity !== query.granularity
          || result.query.scope !== query.scope
          || result.query.anchor !== query.anchor
          || result.query.timezone !== query.timezone
        ) {
          setProfitError("요청한 기간과 다른 손익 보고서가 반환되었습니다. 다시 조회해 주세요.");
          return;
        }
        setProfitReport(result);
      })
      .catch((error: unknown) => {
        if (profitRequestSequenceRef.current !== requestSequence) {
          return;
        }
        setProfitError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (profitRequestSequenceRef.current === requestSequence) {
          setProfitLoading(false);
        }
      });

    return () => {
      if (profitRequestSequenceRef.current === requestSequence) {
        profitRequestSequenceRef.current += 1;
      }
    };
  }, [
    acceptedBridgeGeneration,
    acceptedStateRevision,
    activeView,
    profitAnchor,
    profitGranularity,
    profitRetryNonce,
    profitScope,
  ]);

  useEffect(() => {
    if (state?.runtime.schedulerOwner !== "service") {
      return;
    }
    const secondsRemaining = state.runtime.schedulerSecondsUntilNextCycle;
    if (
      state.runtime.schedulerActive !== true
      || state.runtime.schedulerCycleInProgress === true
      || typeof secondsRemaining !== "number"
      || !Number.isFinite(secondsRemaining)
      || secondsRemaining < 0
    ) {
      setNextCycleDueAtMs(null);
      return;
    }
    const nowMs = Date.now();
    setNextCycleDueAtMs(nowMs + secondsRemaining * 1000);
    setCountdownNowMs(nowMs);
  }, [
    state?.runtime.schedulerActive,
    state?.runtime.schedulerCycleInProgress,
    state?.runtime.schedulerOwner,
    state?.runtime.schedulerSecondsUntilNextCycle,
  ]);

  useEffect(() => {
    if (state?.runtime.schedulerOwner === "service") {
      return;
    }
    if (!state?.runtime.running) {
      setNextCycleDueAtMs(null);
      return;
    }
    let cancelled = false;
    const schedulerStartedAtMs = Date.now();
    let intervalCycleCount = 0;
    const setNextCycleDue = (dueAtMs: number) => {
      if (cancelled) {
        return;
      }
      setNextCycleDueAtMs(dueAtMs);
      setCountdownNowMs(Date.now());
    };
    const runScheduledCycle = async () => {
      if (cycleInFlightRef.current) {
        return;
      }
      cycleInFlightRef.current = true;
      beginBridgeAction();
      setBusyAction((current) => current || "cycle");
      try {
        const next = await runAction("cycle");
        if (!cancelled) {
          acceptDashboardState(next, { preserveRuntimeOnBridgeError: false });
        }
      } catch {
        // The next state poll keeps the UI recoverable if one cycle request fails.
      } finally {
        finishBridgeAction();
        cycleInFlightRef.current = false;
        if (!cancelled) {
          setBusyAction((current) => (current === "cycle" ? "" : current));
        }
      }
    };
    let timer: number | null = null;
    setNextCycleDue(schedulerStartedAtMs + firstCycleDelayMs);
    const firstCycle = window.setTimeout(() => {
      void runScheduledCycle();
      setNextCycleDue(schedulerStartedAtMs + firstCycleDelayMs + cycleIntervalMs);
      timer = window.setInterval(() => {
        intervalCycleCount += 1;
        void runScheduledCycle();
        setNextCycleDue(schedulerStartedAtMs + firstCycleDelayMs + (intervalCycleCount + 1) * cycleIntervalMs);
      }, cycleIntervalMs);
    }, firstCycleDelayMs);
    return () => {
      cancelled = true;
      window.clearTimeout(firstCycle);
      if (timer !== null) {
        window.clearInterval(timer);
      }
    };
  }, [
    acceptDashboardState,
    beginBridgeAction,
    cycleIntervalMs,
    finishBridgeAction,
    firstCycleDelayMs,
    state?.runtime.running,
    state?.runtime.schedulerOwner,
  ]);

  useEffect(() => {
    if (nextCycleDueAtMs === null) {
      return;
    }
    setCountdownNowMs(Date.now());
    const timer = window.setInterval(() => setCountdownNowMs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [nextCycleDueAtMs]);

  const dashboard = state;
  if (!dashboard) {
    return <div className="boot-screen">개미親주식 대시보드 로딩 중</div>;
  }

  const cycleCountdownSeconds = secondsUntil(nextCycleDueAtMs, countdownNowMs);
  const cycleCountdownLabel = cycleCountdownLabelForState(dashboard, cycleCountdownSeconds);

  const dispatch = async (action: string, payload: Record<string, unknown> = {}): Promise<DashboardState> => {
    setBusyAction(action);
    beginBridgeAction();
    try {
      const next = await runAction(action, payload);
      const accepted = acceptDashboardState(next);
      if (accepted?.actionPopup) {
        setActionPopup(accepted.actionPopup);
      }
      return accepted || acceptedStateRef.current || next;
    } finally {
      finishBridgeAction();
      setBusyAction("");
    }
  };

  const saveKisLiveSettings = async (draft: KisLiveCredentialDraft) => {
    const next = await dispatch("kis-live-credentials", {
      appKey: draft.appKey.trim(),
      appSecret: draft.appSecret.trim(),
      accountNo: draft.accountNo.trim(),
      productCode: draft.productCode.trim(),
    });
    if (next.bridgeError || !kisLiveCredentialSaveSucceeded(next)) {
      return;
    }
    setKisLiveEditing(false);
  };

  const runKisLiveCheck = async () => {
    setRealModeLoading("refreshing");
    try {
      await dispatch("kis-live-check", {});
    } finally {
      setRealModeLoading(null);
    }
  };

  const runLiveReadinessCheck = async () => {
    await dispatch("live-readiness-check", {
      refreshScannerSnapshot: true,
    });
  };

  const requestModeChange = async (mode: TradingModeKey) => {
    if (busyAction === "mode") {
      return;
    }
    if (mode === dashboard.mode.key) {
      if (mode === "real" && busyAction !== "kis-live-check") {
        await runKisLiveCheck();
      }
      return;
    }
    if (dashboard.mode.key === "real" && mode === "virtual" && accountHasHoldings(dashboard)) {
      setModeSwitchPrompt({
        targetMode: mode,
        kind: "real-to-paper-holdings",
        holdingsLabel: accountHoldingsLabel(dashboard),
      });
      return;
    }
    if (mode === "real") {
      setRealModeLoading("switching");
      try {
        await dispatch("mode", { mode });
      } finally {
        setRealModeLoading(null);
      }
      return;
    }
    await dispatch("mode", { mode });
  };

  const confirmModeSwitch = async () => {
    if (!modeSwitchPrompt) {
      return;
    }
    const next = await dispatch("mode", { mode: modeSwitchPrompt.targetMode });
    if (!next.bridgeError) {
      setModeSwitchPrompt(null);
    }
  };

  const liveKisCredentialsSaved = hasSavedLiveKisCredentials(dashboard);
  const showGlobalNotice =
    Boolean(dashboard.bridgeError) ||
    (!dashboard.mode.isReal && dashboard.notice.tone === "danger" && activeView !== "environment-settings");
  const selectView = (view: ActiveView) => {
    if (view === "profit-analysis" && activeView !== "profit-analysis") {
      const today = currentKstDate();
      setProfitToday(today);
      setProfitFollowsToday(true);
      setProfitAnchor(today);
    }
    setActiveView(view);
  };

  return (
    <div className="app-shell">
      <Sidebar state={dashboard} activeView={activeView} onViewChange={selectView} />
      <main className="workspace">
        <div className="top-stack">
          <Header
            state={dashboard}
            busyAction={busyAction}
            cycleCountdownLabel={cycleCountdownLabel}
            onAction={dispatch}
            onModeChange={requestModeChange}
          />
          {showGlobalNotice ? <Notice state={dashboard} /> : null}
        </div>
        {activeView === "dashboard" ? (
          <DashboardView state={dashboard} onAction={dispatch} />
        ) : null}
        {activeView === "trade-logs" ? <TradeLogArchivePanel state={dashboard} trades={dashboard.logs.trades} /> : null}
        {activeView === "profit-analysis" ? (
          <ProfitAnalysisPanel
            granularity={profitGranularity}
            scope={profitScope}
            report={profitReport}
            loading={profitLoading}
            error={profitError}
            onGranularityChange={setProfitGranularity}
            onScopeChange={setProfitScope}
            onAnchorChange={(anchor) => {
              setProfitFollowsToday(
                isProfitAnchorInCurrentPeriod(anchor, profitGranularity, profitToday),
              );
              setProfitAnchor(anchor);
            }}
            onRetry={() => setProfitRetryNonce((current) => current + 1)}
          />
        ) : null}
        {activeView === "diagnostic-logs" ? <DiagnosticLogPanel state={dashboard} /> : null}
        {activeView === "environment-settings" ? (
          <EnvironmentSettingsPanel
            liveSaved={liveKisCredentialsSaved}
            liveEditing={kisLiveEditing}
            liveBusy={busyAction === "kis-live-credentials"}
            liveCheckBusy={busyAction === "kis-live-check"}
            liveReadinessBusy={busyAction === "live-readiness-check"}
            onLiveEdit={() => setKisLiveEditing(true)}
            onLiveCancelEdit={() => setKisLiveEditing(false)}
            onLiveSave={saveKisLiveSettings}
            onLiveCheck={runKisLiveCheck}
            onLiveReadinessCheck={() => runLiveReadinessCheck()}
          />
        ) : null}
      </main>
      {modeSwitchPrompt ? (
        <ModeSwitchConfirmDialog
          prompt={modeSwitchPrompt}
          busy={busyAction === "mode"}
          onClose={() => setModeSwitchPrompt(null)}
          onConfirm={confirmModeSwitch}
        />
      ) : null}
      {realModeLoading ? <RealModeLoadingDialog stage={realModeLoading} /> : null}
      {actionPopup ? <ActionPopupDialog popup={actionPopup} onClose={() => setActionPopup(null)} /> : null}
    </div>
  );
}

function RealModeLoadingDialog({ stage }: { stage: RealModeLoadingStage }) {
  return (
    <div className="modal-backdrop">
      <section
        className="action-popup real-mode-loading-popup"
        role="dialog"
        aria-modal="true"
        aria-busy="true"
        aria-label="실전 계좌 정보를 불러오는 중"
      >
        <header>
          <span>KIS REAL</span>
          <h2>{stage === "switching" ? "리얼모드로 전환 중" : "실전 계좌 새로고침 중"}</h2>
        </header>
        <div className="real-mode-loading-content" role="status" aria-live="polite">
          <span className="real-mode-loading-indicator" aria-hidden="true" />
          <p>KIS 계좌 잔고와 보유 종목을 확인하고 있습니다.</p>
        </div>
      </section>
    </div>
  );
}

function mergeBridgeFailureState(
  current: DashboardState | null,
  failure: DashboardState,
  options: { preserveRuntime?: boolean } = {},
): DashboardState {
  if (!current) {
    return failure;
  }
  const preserveRuntime = options.preserveRuntime !== false;
  return {
    ...current,
    bridgeGeneration: failure.bridgeGeneration ?? current.bridgeGeneration,
    bridgeError: true,
    runtime: preserveRuntime ? current.runtime : failure.runtime,
    mode: preserveRuntime ? current.mode : failure.mode,
    account: preserveRuntime ? current.account : failure.account,
    notice: failure.notice,
    logs: {
      ...current.logs,
      system: [...failure.logs.system, ...current.logs.system],
    },
  };
}

function ActionPopupDialog({ popup, onClose }: { popup: ActionPopup; onClose: () => void }) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="modal-backdrop">
      <section className={`action-popup ${popup.tone}`} role="dialog" aria-modal="true" aria-label={popup.title}>
        <header>
          <span>{popup.tone === "warning" ? "확인 필요" : "알림"}</span>
          <h2>{popup.title}</h2>
        </header>
        <p>{popup.message}</p>
        <footer>
          <button type="button" className="blue-action" onClick={onClose}>
            확인
          </button>
        </footer>
      </section>
    </div>
  );
}

function ModeSwitchConfirmDialog({
  prompt,
  busy,
  onClose,
  onConfirm,
}: {
  prompt: ModeSwitchPrompt;
  busy: boolean;
  onClose: () => void;
  onConfirm: () => void;
}) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  return (
    <div className="modal-backdrop">
      <section className="action-popup mode-switch-confirm" role="dialog" aria-modal="true" aria-label="실전 보유 종목 확인">
        <header>
          <span>REAL 보유 확인</span>
          <h2>실전 보유 종목 확인</h2>
        </header>
        <p>
          실전 계좌에 보유 종목 {prompt.holdingsLabel}가 있습니다. 가상모드로 전환해도 실전 계좌의 보유 종목은
          매도되지 않습니다.
        </p>
        <p className="mode-switch-confirm-note">
          현재 실전 주문은 잠금 상태라 앱 정리모드로 실제 매도하지 않습니다. 정리가 필요하면 HTS/MTS에서 직접 처리한 뒤
          실전 계좌 조회를 다시 확인하세요.
        </p>
        <footer>
          <button type="button" onClick={onClose} disabled={busy}>
            취소
          </button>
          <button
            type="button"
            disabled
            title="실전 주문 잠금 상태에서는 앱 정리모드로 실제 매도하지 않습니다."
          >
            정리모드로 정리
          </button>
          <button type="button" className="blue-action" onClick={onConfirm} disabled={busy}>
            즉시 가상모드 전환
          </button>
        </footer>
      </section>
    </div>
  );
}

function kisLiveCredentialSaveSucceeded(state: DashboardState): boolean {
  const latestSettingsLog = state.logs.system[0];
  if (latestSettingsLog?.title !== "KIS 실전 조회 설정 저장") {
    return false;
  }
  return latestSettingsLog?.level.toLowerCase() === "success";
}

function hasSavedLiveKisCredentials(state: DashboardState): boolean {
  const status = state.settings?.kisLiveCredentials;
  return Boolean(status?.appKeySaved && status?.appSecretSaved && status?.accountNoSaved && status?.productCodeSaved);
}

function liveStartBlockerMessages(state: DashboardState): string[] {
  if (!state.mode.isReal) {
    return [];
  }
  const blockers: string[] = [];
  if (!hasSavedLiveKisCredentials(state)) {
    blockers.push("환경설정에서 실전 API 키와 계좌를 저장하세요.");
  }
  const seen = new Set<string>();
  return blockers.filter((blocker) => {
    if (seen.has(blocker)) {
      return false;
    }
    seen.add(blocker);
    return true;
  });
}

function accountMetricValue(state: DashboardState, label: string): string {
  return state.account.metrics.find((metric) => metric.label === label)?.value ?? "";
}

function accountHasHoldings(state: DashboardState): boolean {
  const match = accountMetricValue(state, "보유 종목").replace(/,/g, "").match(/\d+/);
  return match ? Number(match[0]) > 0 : false;
}

function accountHoldingsLabel(state: DashboardState): string {
  return accountMetricValue(state, "보유 종목") || "보유 종목 있음";
}

function modeBadgeForState(state: DashboardState): string | null {
  return noticeBadgeForState(state);
}

function modeBadgeToneForState(state: DashboardState): ModeBadgeTone {
  return state.mode.isReal ? "real" : "paper";
}

function noticeBadgeForState(state: DashboardState): string | null {
  if (!state.mode.isReal) {
    return "PAPER";
  }
  if (state.notice.orderEnabled === true) {
    return "REAL";
  }
  return null;
}

function showHeaderRuntimeStatus(state: DashboardState): boolean {
  return !(state.mode.isReal && !state.runtime.running && state.notice.orderEnabled !== true);
}

const PROFIT_GRANULARITY_OPTIONS: readonly { key: ProfitGranularity; label: string }[] = [
  { key: "hour", label: "시간별" },
  { key: "day", label: "일별" },
  { key: "month", label: "월별" },
  { key: "year", label: "연도별" },
];

const PROFIT_SCOPE_OPTIONS: readonly { key: ProfitScope; label: string }[] = [
  { key: "account", label: "계좌 전체" },
  { key: "stockbot", label: "자동매매" },
];

const PROFIT_BUCKET_STATUS_LABELS: Record<ProfitBucketStatus, string> = {
  confirmed: "확정",
  provisional: "잠정",
  no_trade: "거래 없음",
  market_closed: "휴장",
  partial: "일부 누락",
  unavailable: "조회 불가",
};

const PROFIT_REPORT_STATUS_LABELS: Record<ProfitReport["status"], string> = {
  complete: "조회 완료",
  partial: "일부 누락",
  empty: "거래 없음",
  unavailable: "조회 불가",
};

function currentKstDate(): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: PROFIT_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function isProfitAnchorInCurrentPeriod(
  anchor: string,
  granularity: ProfitGranularity,
  today: string,
): boolean {
  if (granularity === "hour") {
    return anchor === today;
  }
  if (granularity === "day") {
    return anchor.slice(0, 7) === today.slice(0, 7);
  }
  if (granularity === "month") {
    return anchor.slice(0, 4) === today.slice(0, 4);
  }
  const anchorYear = Number(anchor.slice(0, 4));
  const todayYear = Number(today.slice(0, 4));
  return (
    Number.isInteger(anchorYear)
    && Number.isInteger(todayYear)
    && Math.floor(anchorYear / 10) === Math.floor(todayYear / 10)
  );
}

function formatProfitWon(value: number | null, signed = false): string {
  if (value === null || !Number.isFinite(value)) {
    return "-";
  }
  const rounded = Math.round(value);
  if (signed && rounded > 0) {
    return `+${rounded.toLocaleString("ko-KR")}원`;
  }
  return `${rounded.toLocaleString("ko-KR")}원`;
}

function profitValueTone(value: number | null): "positive" | "negative" | "neutral" {
  if (value === null || value === 0) {
    return "neutral";
  }
  return value > 0 ? "positive" : "negative";
}

function formatProfitSource(source: string): string {
  const normalized = source.trim().toUpperCase();
  if (normalized === "KIS_PERIOD_PROFIT_LOCAL" || normalized === "KIS_PERIOD_PROFIT") {
    return "KIS 기간별손익";
  }
  if (
    normalized === "STOCKBOT_MANAGED_LEDGER"
    || normalized === "MANAGED_FILL_LEDGER"
    || normalized === "LIVE_POSITION_LEDGER"
  ) {
    return "StockBot 체결 원장";
  }
  if (normalized === "KIS_ACCOUNT_SNAPSHOT") {
    return "KIS 계좌 손익 스냅샷";
  }
  return "손익 분석 저장소";
}

function formatProfitUpdatedAt(value: string | null): string {
  if (!value) {
    return "기준 시각 없음";
  }
  const timestamp = new Date(value);
  if (Number.isNaN(timestamp.getTime())) {
    return "기준 시각 미확인";
  }
  const formatted = new Intl.DateTimeFormat("ko-KR", {
    timeZone: PROFIT_TIMEZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
    hourCycle: "h23",
  }).format(timestamp);
  return `${formatted.replace(/\s+/g, " ")} KST`;
}

function sumProfitBucketCost(
  buckets: ProfitBucket[],
  field: "feeKrw" | "taxKrw" | "interestKrw",
): number | null {
  if (buckets.length === 0 || buckets.some((bucket) => bucket[field] === null)) {
    return null;
  }
  return buckets.reduce((total, bucket) => total + (bucket[field] ?? 0), 0);
}

function profitBucketStatusLabel(bucket: ProfitBucket): string {
  const statusLabel = PROFIT_BUCKET_STATUS_LABELS[bucket.status];
  if (bucket.activityStatus === "no_trade" && bucket.status !== "no_trade") {
    return `${statusLabel} · 거래 없음`;
  }
  if (bucket.activityStatus === "trade" && bucket.reportedRealizedPnlKrw === 0) {
    return `${statusLabel} · 거래 있음`;
  }
  return statusLabel;
}

function ProfitAnalysisPanel({
  granularity,
  scope,
  report,
  loading,
  error,
  onGranularityChange,
  onScopeChange,
  onAnchorChange,
  onRetry,
}: {
  granularity: ProfitGranularity;
  scope: ProfitScope;
  report: ProfitReport | null;
  loading: boolean;
  error: string;
  onGranularityChange: (granularity: ProfitGranularity) => void;
  onScopeChange: (scope: ProfitScope) => void;
  onAnchorChange: (anchor: string) => void;
  onRetry: () => void;
}) {
  return (
    <section className="panel profit-analysis-view" role="region" aria-label="손익 분석">
      <header className="profit-analysis-header">
        <div className="panel-title profit-title">
          <h2>손익 분석</h2>
          {report ? (
            <span className={`profit-report-status ${report.status}`}>
              {PROFIT_REPORT_STATUS_LABELS[report.status]}
            </span>
          ) : null}
        </div>
        {report ? (
          <div className="profit-data-basis">
            <span>
              데이터 기준 {formatProfitUpdatedAt(report.updatedAt)} · {formatProfitSource(report.dataSource)}
            </span>
            <span>
              가용 구간 {report.summary.availableBucketCount.toLocaleString("ko-KR")} /{" "}
              {report.buckets.length.toLocaleString("ko-KR")}
            </span>
          </div>
        ) : null}
      </header>

      <div className="profit-toolbar">
        <div className="profit-segmented" role="group" aria-label="손익 집계 단위">
          {PROFIT_GRANULARITY_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.key}
              className={granularity === option.key ? "selected" : ""}
              aria-pressed={granularity === option.key}
              onClick={() => onGranularityChange(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="profit-segmented scope" role="group" aria-label="손익 집계 범위">
          {PROFIT_SCOPE_OPTIONS.map((option) => (
            <button
              type="button"
              key={option.key}
              className={scope === option.key ? "selected" : ""}
              aria-pressed={scope === option.key}
              onClick={() => onScopeChange(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <div className="profit-period-nav" aria-label="손익 조회 기간">
          <button
            type="button"
            className="profit-period-button"
            aria-label="이전 기간"
            title="이전 기간"
            disabled={!report?.range.previousAnchor || loading}
            onClick={() => {
              if (report?.range.previousAnchor) {
                onAnchorChange(report.range.previousAnchor);
              }
            }}
          >
            ←
          </button>
          <strong aria-live="polite">{report?.range.label ?? (loading ? "조회 중" : "기간 미선택")}</strong>
          <button
            type="button"
            className="profit-period-button"
            aria-label="다음 기간"
            title="다음 기간"
            disabled={!report?.range.nextAnchor || loading}
            onClick={() => {
              if (report?.range.nextAnchor) {
                onAnchorChange(report.range.nextAnchor);
              }
            }}
          >
            →
          </button>
        </div>
      </div>

      <div className="profit-analysis-content">
        {loading ? (
          <div className="profit-load-state" role="status" aria-label="손익 분석 조회 중">
            <span className="profit-loading-indicator" aria-hidden="true" />
            <strong>기간 손익을 조회하고 있습니다.</strong>
            <p>선택한 기간의 저장된 손익 기록을 확인 중입니다.</p>
          </div>
        ) : null}
        {!loading && error ? (
          <div className="profit-load-state error" role="alert">
            <strong>손익 분석을 불러오지 못했습니다.</strong>
            <p>{error}</p>
            <button type="button" onClick={onRetry}>다시 조회</button>
          </div>
        ) : null}
        {!loading && !error && report ? <ProfitReportContent report={report} scope={scope} /> : null}
      </div>
    </section>
  );
}

function ProfitReportContent({ report, scope }: { report: ProfitReport; scope: ProfitScope }) {
  const feeTotal = sumProfitBucketCost(report.buckets, "feeKrw");
  const taxTotal = sumProfitBucketCost(report.buckets, "taxKrw");
  const interestTotal = sumProfitBucketCost(report.buckets, "interestKrw");
  const reportLabel = scope === "account" ? "KIS 보고 실현손익" : "StockBot 기록 실현손익";

  return (
    <div className="profit-report-content">
      <section className="profit-summary-grid" aria-label="기간 손익 요약">
        <article className="profit-summary-item primary">
          <span>{reportLabel}</span>
          <strong
            className={`profit-summary-value ${profitValueTone(report.summary.reportedRealizedPnlKrw)}`}
          >
            {formatProfitWon(report.summary.reportedRealizedPnlKrw, true)}
          </strong>
        </article>
        <article className="profit-summary-item">
          <span>수익 구간 합계</span>
          <strong className={`profit-summary-value ${profitValueTone(report.summary.profitableBucketsTotalKrw)}`}>
            {formatProfitWon(report.summary.profitableBucketsTotalKrw, true)}
          </strong>
          <small>{report.summary.profitableBucketCount.toLocaleString("ko-KR")}개 구간</small>
        </article>
        <article className="profit-summary-item">
          <span>손실 구간 합계</span>
          <strong className={`profit-summary-value ${profitValueTone(report.summary.losingBucketsTotalKrw)}`}>
            {formatProfitWon(report.summary.losingBucketsTotalKrw, true)}
          </strong>
          <small>{report.summary.losingBucketCount.toLocaleString("ko-KR")}개 구간</small>
        </article>
        <article className="profit-summary-item">
          <span>확인된 거래비용</span>
          <strong className="profit-summary-value neutral">
            {formatProfitWon(report.summary.tradingCostKrw)}
          </strong>
          <small>별도 보고값</small>
        </article>
      </section>

      <section className="profit-cost-strip" aria-label="거래비용 세부 내역">
        <span>수수료 <strong>{formatProfitWon(feeTotal)}</strong></span>
        <span>제세금 <strong>{formatProfitWon(taxTotal)}</strong></span>
        <span>대출이자 <strong>{formatProfitWon(interestTotal)}</strong></span>
        <p>비용 포함 여부가 확인되지 않아 다시 차감하지 않습니다. 수수료·제세금·대출이자는 참고값으로 별도 표시합니다.</p>
      </section>

      {report.issues.length > 0 ? (
        <div className="profit-report-issues" role="note">
          <strong>데이터 확인 사항</strong>
          <span>{report.issues.join(" · ")}</span>
        </div>
      ) : null}

      {report.buckets.length > 0 ? (
        <>
          <section className="profit-chart-section" aria-labelledby="profit-chart-heading">
            <div className="profit-section-heading">
              <h3 id="profit-chart-heading">기간별 흐름</h3>
              <span>0원 기준 · 보고 실현손익</span>
            </div>
            <div className="profit-chart-scroll" tabIndex={0} role="region" aria-label="손익 그래프 스크롤 영역">
              <ProfitBarChart buckets={report.buckets} />
            </div>
          </section>

          <section className="profit-table-section" aria-labelledby="profit-table-heading">
            <div className="profit-section-heading">
              <h3 id="profit-table-heading">구간별 내역</h3>
              <span>금액 단위 원 · 시간 기준 KST</span>
            </div>
            <div className="profit-table-scroll" tabIndex={0} role="region" aria-label="손익 내역 표 스크롤 영역">
              <table className="profit-table" aria-label="기간별 손익 내역">
                <thead>
                  <tr>
                    <th>기간</th>
                    <th>데이터 상태</th>
                    <th className="numeric">{reportLabel}</th>
                    <th className="numeric">수수료</th>
                    <th className="numeric">제세금</th>
                    <th className="numeric">대출이자</th>
                    <th className="numeric">체결</th>
                    <th>확인 사항</th>
                  </tr>
                </thead>
                <tbody>
                  {report.buckets.map((bucket) => (
                    <tr key={bucket.key} className={`profit-row ${bucket.status}`}>
                      <th scope="row">{bucket.label}</th>
                      <td>
                        <span className={`profit-bucket-status ${bucket.status}`}>
                          {profitBucketStatusLabel(bucket)}
                        </span>
                      </td>
                      <td className={`numeric profit-amount ${profitValueTone(bucket.reportedRealizedPnlKrw)}`}>
                        {formatProfitWon(bucket.reportedRealizedPnlKrw, true)}
                      </td>
                      <td className="numeric">{formatProfitWon(bucket.feeKrw)}</td>
                      <td className="numeric">{formatProfitWon(bucket.taxKrw)}</td>
                      <td className="numeric">{formatProfitWon(bucket.interestKrw)}</td>
                      <td className="numeric">
                        {bucket.fillCount === null ? "-" : `${bucket.fillCount.toLocaleString("ko-KR")}건`}
                      </td>
                      <td className="profit-issue-cell">{bucket.issues.length > 0 ? bucket.issues.join(" · ") : "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : (
        <div className={`profit-empty-state ${report.status}`}>
          <strong>{report.status === "empty" ? "선택한 기간에 거래 기록이 없습니다." : "표시할 손익 구간이 없습니다."}</strong>
          <p>수집된 손익 데이터가 없습니다.</p>
        </div>
      )}
    </div>
  );
}

function ProfitBarChart({ buckets }: { buckets: ProfitBucket[] }) {
  const chartHeight = 270;
  const plotTop = 28;
  const baseline = 118;
  const plotHalfHeight = 82;
  const labelY = 252;
  const left = 72;
  const right = 26;
  const slotWidth = 82;
  const chartWidth = Math.max(760, left + right + buckets.length * slotWidth);
  const values = buckets
    .map((bucket) => bucket.reportedRealizedPnlKrw)
    .filter((value): value is number => value !== null && Number.isFinite(value));
  const hasNumericValues = values.length > 0;
  const maxAbsolute = hasNumericValues
    ? Math.max(1, ...values.map((value) => Math.abs(value)))
    : 1;
  const barWidth = 30;

  return (
    <svg
      className="profit-chart"
      viewBox={`0 0 ${chartWidth} ${chartHeight}`}
      width={chartWidth}
      height={chartHeight}
      role="img"
      aria-label="기간별 보고 실현손익 그래프"
    >
      <title>기간별 보고 실현손익 그래프</title>
      <desc>0원 기준선 위는 수익, 아래는 손실이며 X 표시는 해당 구간 데이터가 없음을 뜻합니다.</desc>
      {hasNumericValues ? (
        <>
          <text className="profit-axis-label" x={left - 10} y={plotTop + 4} textAnchor="end">
            {formatProfitWon(maxAbsolute, true)}
          </text>
          <text className="profit-axis-label" x={left - 10} y={baseline + 4} textAnchor="end">0원</text>
          <text className="profit-axis-label" x={left - 10} y={baseline + plotHalfHeight + 4} textAnchor="end">
            {formatProfitWon(-maxAbsolute, true)}
          </text>
        </>
      ) : null}
      <line
        className="profit-grid-line"
        x1={left}
        y1={plotTop}
        x2={chartWidth - right}
        y2={plotTop}
      />
      <line
        className="profit-zero-line"
        x1={left}
        y1={baseline}
        x2={chartWidth - right}
        y2={baseline}
      />
      <line
        className="profit-grid-line"
        x1={left}
        y1={baseline + plotHalfHeight}
        x2={chartWidth - right}
        y2={baseline + plotHalfHeight}
      />
      {buckets.map((bucket, index) => {
        const centerX = left + slotWidth * index + slotWidth / 2;
        const value = bucket.reportedRealizedPnlKrw;
        if (value === null || !Number.isFinite(value)) {
          return (
            <g className="profit-missing-marker" key={bucket.key}>
              <title>{bucket.label} 데이터 없음</title>
              <line x1={centerX - 6} y1={baseline - 6} x2={centerX + 6} y2={baseline + 6} />
              <line x1={centerX + 6} y1={baseline - 6} x2={centerX - 6} y2={baseline + 6} />
              <text className="profit-chart-value missing" x={centerX} y={baseline - 12} textAnchor="middle">-</text>
              <text className="profit-chart-label" x={centerX} y={labelY} textAnchor="middle">{bucket.label}</text>
            </g>
          );
        }
        if (value === 0) {
          return (
            <g className="profit-zero-marker" key={bucket.key}>
              <title>{bucket.label} 0원</title>
              <line x1={centerX - barWidth / 2} y1={baseline} x2={centerX + barWidth / 2} y2={baseline} />
              <text className="profit-chart-value neutral" x={centerX} y={baseline - 10} textAnchor="middle">0원</text>
              <text className="profit-chart-label" x={centerX} y={labelY} textAnchor="middle">{bucket.label}</text>
            </g>
          );
        }
        const height = Math.max(2, (Math.abs(value) / maxAbsolute) * plotHalfHeight);
        const isPositive = value > 0;
        return (
          <g key={bucket.key}>
            <title>{bucket.label} {formatProfitWon(value, true)}</title>
            <rect
              className={`profit-bar ${isPositive ? "positive" : "negative"}`}
              x={centerX - barWidth / 2}
              y={isPositive ? baseline - height : baseline}
              width={barWidth}
              height={height}
            />
            <text
              className={`profit-chart-value ${isPositive ? "positive" : "negative"}`}
              x={centerX}
              y={isPositive ? Math.max(16, baseline - height - 8) : baseline + height + 16}
              textAnchor="middle"
            >
              {formatProfitWon(value, true)}
            </text>
            <text className="profit-chart-label" x={centerX} y={labelY} textAnchor="middle">{bucket.label}</text>
          </g>
        );
      })}
    </svg>
  );
}

function Sidebar({
  state,
  activeView,
  onViewChange,
}: {
  state: DashboardState;
  activeView: ActiveView;
  onViewChange: (view: ActiveView) => void;
}) {
  const menu: { key: ActiveView; label: string }[] = [
    { key: "dashboard", label: "대시보드" },
    { key: "trade-logs", label: "매도/매수 로그" },
    { key: "profit-analysis", label: "손익 분석" },
    { key: "diagnostic-logs", label: "오류/진단 로그" },
    { key: "environment-settings", label: "환경설정" },
  ];
  const modeBadge = modeBadgeForState(state);
  return (
    <aside className="sidebar">
      <div className="brand">
        <img src={BRAND_ICON_SRC} alt={`${state.app.title} 로고`} className="brand-icon" />
        <h1>{state.app.title}</h1>
        <p>{state.app.subtitle}</p>
        {modeBadge ? <span className={`mini-badge ${modeBadgeToneForState(state)}`}>{modeBadge}</span> : null}
      </div>
      <nav className="side-nav" aria-label="주요 메뉴">
        {menu.map((item) => (
          <button
            type="button"
            className={activeView === item.key ? "active" : ""}
            aria-current={activeView === item.key ? "page" : undefined}
            key={item.key}
            onClick={() => onViewChange(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>
      <div className="sidebar-status">
        <span className={`status-dot ${state.runtime.running ? "on" : ""}`} />
        <span>연결 상태</span>
        <strong>{state.runtime.status}</strong>
        <small>버전 {state.app.version}</small>
        <a href={state.app.authorUrl} target="_blank" rel="noreferrer">
          {state.app.authorLabel}
        </a>
      </div>
    </aside>
  );
}

function Header({
  state,
  busyAction,
  cycleCountdownLabel,
  onAction,
  onModeChange,
}: {
  state: DashboardState;
  busyAction: string;
  cycleCountdownLabel: string;
  onAction: (action: string, payload?: Record<string, unknown>) => Promise<DashboardState>;
  onModeChange: (mode: TradingModeKey) => void | Promise<void>;
}) {
  const renderRuntimeStatus = showHeaderRuntimeStatus(state);
  return (
    <header className="topbar">
      <div>
        <div className="title-line">
          <h2>{state.app.title} 대시보드</h2>
          <StatusBadge tone={state.mode.isReal ? "danger" : "paper"}>{state.mode.label}</StatusBadge>
          {renderRuntimeStatus ? (
            <StatusBadge tone={state.runtime.running ? "success" : "warning"}>{state.runtime.status}</StatusBadge>
          ) : null}
          <StatusBadge tone="neutral">{cycleCountdownLabel}</StatusBadge>
        </div>
        <RuntimeModeStrip
          state={state}
          busyAction={busyAction}
          cycleCountdownLabel={cycleCountdownLabel}
          onAction={onAction}
        />
        <div className="mode-switch" role="group" aria-label="거래 모드">
          <button
            className={state.mode.key === "real" ? "selected danger" : ""}
            disabled={busyAction === "mode" || busyAction === "kis-live-check"}
            onClick={() => void onModeChange("real")}
          >
            리얼모드 <span>REAL</span>
          </button>
          <button
            className={state.mode.key === "virtual" ? "selected" : ""}
            disabled={busyAction === "mode" || busyAction === "kis-live-check"}
            onClick={() => void onModeChange("virtual")}
          >
            가상모드 <span>PAPER</span>
          </button>
        </div>
      </div>
    </header>
  );
}

function RuntimeModeStrip({
  state,
  busyAction,
  cycleCountdownLabel,
  onAction,
}: {
  state: DashboardState;
  busyAction: string;
  cycleCountdownLabel: string;
  onAction: (action: string, payload?: Record<string, unknown>) => Promise<DashboardState>;
}) {
  const dataMode = runtimeDataModeCopy(state);
  const switching = busyAction === "data-source";
  const isScannerBackedKisMarketTest = state.runtime.dataSourceKind === "external-scan-kis";
  const automationActive = state.runtime.running || state.runtime.schedulerActive === true;
  const startDisabled = automationActive || busyAction === "start";
  const realStartBlockers = liveStartBlockerMessages(state);
  const accountLastUpdated = accountMetricValue(state, "최근 갱신");
  const lastUpdated = accountLastUpdated && accountLastUpdated !== "-" ? accountLastUpdated : state.runtime.lastUpdated;
  const lastUpdatedLabel = lastUpdated && lastUpdated !== "-" ? `최근 갱신 ${lastUpdated}` : "";
  const safetySummary = runtimeSafetySummaryForState(state);

  return (
    <div className="runtime-mode-strip" role="group" aria-label="현재 테스트 구분">
      <div className={`runtime-mode-card ${dataMode.tone}`}>
        <span>시세 출처</span>
        <strong>{dataMode.title}</strong>
        <small>{dataMode.description}</small>
        <small className="mode-safety">{safetySummary}</small>
        <div className="cycle-status-row" aria-live="polite">
          <small className="cycle-interval-label">자동매매 cycle {cycleIntervalLabelForState(state)}</small>
          <small className="cycle-countdown-label">{cycleCountdownLabel}</small>
          {lastUpdatedLabel ? <small className="cycle-updated-label">{lastUpdatedLabel}</small> : null}
        </div>
        <div className="data-source-switch" role="group" aria-label="테스트 데이터 전환">
          <button
            type="button"
            className={state.runtime.dataSourceKind === "local" ? "selected" : ""}
            disabled={state.mode.isReal || switching || state.runtime.dataSourceKind === "local"}
            onClick={() => onAction("data-source", { source: "local" })}
          >
            로컬 테스트 전환
          </button>
          <button
            type="button"
            className={isScannerBackedKisMarketTest ? "selected" : ""}
            disabled={state.mode.isReal || switching || isScannerBackedKisMarketTest}
            onClick={() => onAction("data-source", { source: "external-scan-kis" })}
          >
            KIS 장중 테스트
          </button>
        </div>
      </div>
      <div className="runtime-mode-actions runtime-mode-actions-compact actions" aria-label="자동매매 실행 제어">
        <button
          className="primary-action"
          disabled={startDisabled}
          onClick={() => onAction("start")}
          title={realStartBlockers.join("\n") || undefined}
          data-tooltip={realStartBlockers.length > 0 ? realStartBlockers[0] : undefined}
        >
          ▶ 자동매매 시작
        </button>
        <button
          disabled={!automationActive || busyAction === "pause"}
          onClick={() => onAction("pause")}
          title="자동매매 cycle을 멈추고 현재 보유 상태를 그대로 유지합니다."
          data-tooltip="자동매매 cycle을 멈추고 현재 보유 상태를 그대로 유지합니다."
        >
          Ⅱ 일시정지
        </button>
        <button
          className={state.runtime.cleanupMode ? "cleanup-action active" : "cleanup-action"}
          disabled={busyAction === "cleanup-mode" || (state.mode.isReal && state.runtime.cleanupMode)}
          onClick={() => onAction("cleanup-mode", { enabled: state.mode.isReal ? true : !state.runtime.cleanupMode })}
          aria-pressed={state.runtime.cleanupMode}
          title="신규 매수/숏 진입을 차단하고 보유 종목은 익절/손절 기준에 따라 정리합니다."
          data-tooltip="신규 매수/숏 진입을 차단하고 보유 종목은 익절/손절 기준에 따라 정리합니다."
        >
          {state.runtime.cleanupMode ? "정리모드 중" : "정리모드"}
        </button>
      </div>
    </div>
  );
}

function isRealPrepDataSource(kind: DashboardState["runtime"]["dataSourceKind"]): boolean {
  return kind === "real-prep" || kind === "real-read-only";
}

function runtimeSafetySummaryForState(state: DashboardState): string {
  if (state.mode.isReal && isRealPrepDataSource(state.runtime.dataSourceKind) && state.notice.orderEnabled !== true) {
    return "실전 거래 준비 · 시작 시 안전 게이트 확인";
  }
  return state.runtime.safetySummary;
}

function runtimeDataModeCopy(state: DashboardState): { title: string; description: string; tone: string } {
  if (state.runtime.dataSourceKind === "external-scan-kis") {
    return {
      title: "KIS 장중 하이브리드 테스트",
      description: "넓은 후보군을 먼저 선별하고, 주문 직전 KIS 현재가로 최종 확인합니다.",
      tone: "kis",
    };
  }
  if (state.runtime.dataSourceKind === "kis-vts") {
    return {
      title: "KIS 장중 시세 테스트",
      description: "정규장 현재가 조회 + paper 체결",
      tone: "kis",
    };
  }
  if (isRealPrepDataSource(state.runtime.dataSourceKind)) {
    return {
      title: "KIS 실전 계좌",
      description: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
      tone: "real",
    };
  }
  if (state.runtime.dataSourceKind === "live") {
    return {
      title: state.runtime.dataModeLabel || "KIS 실전 주문",
      description: state.runtime.dataModeDescription || "실전 주문 runtime입니다. 주문 전 안전 게이트를 확인합니다.",
      tone: "real",
    };
  }
  if (state.runtime.dataSourceKind === "local") {
    return {
      title: "로컬 가상 테스트",
      description: "장외 로컬 검증",
      tone: "local",
    };
  }
  return {
    title: "시세 출처 확인 필요",
    description: state.runtime.dataModeDescription,
    tone: "unknown",
  };
}

function Notice({ state }: { state: DashboardState }) {
  const badge = noticeBadgeForState(state);
  return (
    <section className={`notice ${state.notice.tone}`}>
      <strong>{state.notice.title}</strong>
      <span>{state.notice.description}</span>
      {badge ? <em className={modeBadgeToneForState(state)}>{badge}</em> : null}
    </section>
  );
}

function AccountPanel({ state }: { state: DashboardState }) {
  const accountMetric = state.account.metrics.find((metric) => metric.label === "계좌");
  const overviewMetrics = ACCOUNT_OVERVIEW_METRIC_LABELS[state.mode.key].flatMap((label) => {
    const metric = state.account.metrics.find((candidate) => candidate.label === label)
      ?? (state.mode.key === "real" && label === "예수금"
        ? state.account.metrics.find((candidate) => candidate.label === "현금")
        : undefined);
    if (metric && state.mode.key === "real" && label === "예수금" && metric.label === "현금") {
      return [{ ...metric, label: "예수금" }];
    }
    return metric ? [metric] : [];
  });

  return (
    <section className="panel" id="dashboard">
      <PanelTitle title={state.account.title} badge={modeBadgeForState(state)} tone={modeBadgeToneForState(state)} />
      {accountMetric ? (
        <div className="account-identity">
          <span>{accountMetric.label}</span>
          <strong>{accountMetric.value}</strong>
        </div>
      ) : null}
      <div className="metric-grid account-metric-grid">
        {overviewMetrics.map((metric) => (
          <div className={metric.emphasis ? "metric emphasis" : "metric"} key={metric.label}>
            <span>{metric.label}</span>
            <strong>{metric.value}</strong>
          </div>
        ))}
      </div>
      <div className="summary-strip">
        {state.account.summary.length ? (
          state.account.summary.map((metric) => (
            <span key={metric.label}>
              {metric.label} <strong>{metric.value}</strong>
            </span>
          ))
        ) : (
          <span>총손익 0원 · 실현손익 0원 · 평가손익 0원</span>
        )}
      </div>
    </section>
  );
}

function DashboardView({
  state,
  onAction,
}: {
  state: DashboardState;
  onAction: (action: string, payload?: Record<string, unknown>) => Promise<DashboardState>;
}) {
  return (
    <div className="dashboard-view">
      <section className="summary-grid summary-grid-single">
        <AccountPanel state={state} />
      </section>
      <section className="main-grid">
        <PositionsPanel state={state} onAction={onAction} />
        <LogsPanel trades={state.logs.trades} />
      </section>
    </div>
  );
}
function LiveCredentialInputField({
  label,
  name,
  placeholder,
  liveEditable,
  liveBusy,
  revealActive,
  onRevealStart,
  onRevealEnd,
}: {
  label: string;
  name: KisLiveCredentialField;
  placeholder: string;
  liveEditable: boolean;
  liveBusy: boolean;
  revealActive: boolean;
  onRevealStart: (name: KisLiveCredentialField) => void;
  onRevealEnd: (name: KisLiveCredentialField) => void;
}) {
  const inputId = `live-credential-${name}`;
  const canReveal = liveEditable && !liveBusy;
  const inputType = canReveal && revealActive ? "text" : liveEditable ? "password" : "text";
  const releaseReveal = () => onRevealEnd(name);
  const startReveal = (event: ReactMouseEvent<HTMLButtonElement> | ReactTouchEvent<HTMLButtonElement>) => {
    event.preventDefault();
    if (canReveal) {
      onRevealStart(name);
    }
  };
  const handleKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if ((event.key === " " || event.key === "Enter") && canReveal) {
      event.preventDefault();
      onRevealStart(name);
    }
  };
  const handleKeyUp = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      releaseReveal();
    }
  };

  return (
    <div className="credential-field">
      <label htmlFor={inputId}>{label}</label>
      <div className="credential-input-shell">
        <input
          id={inputId}
          key={`live-${name}-${liveEditable ? "edit" : "masked"}`}
          name={name}
          type={inputType}
          autoComplete="off"
          spellCheck={false}
          placeholder={placeholder}
          required={liveEditable}
          disabled={!liveEditable || liveBusy}
          readOnly={!liveEditable}
          value={liveEditable ? undefined : MASKED_CREDENTIAL}
          defaultValue={liveEditable ? "" : undefined}
        />
        {liveEditable ? (
          <button
            type="button"
            className="credential-reveal-button"
            aria-label={`입력값 보기: ${label}`}
            aria-controls={inputId}
            aria-pressed={revealActive}
            title="누르고 있는 동안 입력값 보기"
            disabled={liveBusy}
            onMouseDown={startReveal}
            onMouseUp={releaseReveal}
            onMouseLeave={releaseReveal}
            onTouchStart={startReveal}
            onTouchEnd={releaseReveal}
            onTouchCancel={releaseReveal}
            onKeyDown={handleKeyDown}
            onKeyUp={handleKeyUp}
            onBlur={releaseReveal}
          >
            <EyeIcon />
          </button>
        ) : null}
      </div>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg className="eye-icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M2.5 12s3.5-6 9.5-6 9.5 6 9.5 6-3.5 6-9.5 6-9.5-6-9.5-6Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function EnvironmentSettingsPanel({
  liveSaved,
  liveEditing,
  liveBusy,
  liveCheckBusy,
  liveReadinessBusy,
  onLiveEdit,
  onLiveCancelEdit,
  onLiveSave,
  onLiveCheck,
  onLiveReadinessCheck,
}: {
  liveSaved: boolean;
  liveEditing: boolean;
  liveBusy: boolean;
  liveCheckBusy: boolean;
  liveReadinessBusy: boolean;
  onLiveEdit: () => void;
  onLiveCancelEdit: () => void;
  onLiveSave: (draft: KisLiveCredentialDraft) => Promise<void>;
  onLiveCheck: () => Promise<void>;
  onLiveReadinessCheck: () => Promise<void>;
}) {
  const liveEditable = liveEditing || !liveSaved;
  const [revealedCredential, setRevealedCredential] = useState<KisLiveCredentialField | null>(null);
  useEffect(() => {
    if (!liveEditable) {
      setRevealedCredential(null);
    }
  }, [liveEditable]);
  const clearReveal = (name: KisLiveCredentialField) => {
    setRevealedCredential((current) => (current === name ? null : current));
  };
  const submitLive = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!liveEditable) {
      return;
    }
    const formData = new FormData(event.currentTarget);
    void onLiveSave({
      appKey: String(formData.get("appKey") ?? ""),
      appSecret: String(formData.get("appSecret") ?? ""),
      accountNo: String(formData.get("accountNo") ?? ""),
      productCode: String(formData.get("productCode") ?? ""),
    });
  };
  return (
    <section className="panel environment-panel" id="environment-settings" role="region" aria-label="환경설정">
      <PanelTitle title="환경설정" badge="LOCAL" tone="local" />
      <form id="live-credentials-section" className="credential-form" onSubmit={submitLive}>
        <h3 className="credential-heading">실전 계좌 설정</h3>
        <p className="credential-copy">
          실전 APP Key와 Secret은 이 컴퓨터의 로컬 .env 파일에 KIS_LIVE_* 값으로만 저장됩니다. 화면과 로그에는 평문으로 표시하지 않습니다.
        </p>
        <div className="credential-grid">
          <LiveCredentialInputField
            label="실전 API Key"
            name="appKey"
            placeholder="실전투자 APP Key"
            liveEditable={liveEditable}
            liveBusy={liveBusy}
            revealActive={revealedCredential === "appKey"}
            onRevealStart={setRevealedCredential}
            onRevealEnd={clearReveal}
          />
          <LiveCredentialInputField
            label="실전 API Secret"
            name="appSecret"
            placeholder="실전투자 APP Secret"
            liveEditable={liveEditable}
            liveBusy={liveBusy}
            revealActive={revealedCredential === "appSecret"}
            onRevealStart={setRevealedCredential}
            onRevealEnd={clearReveal}
          />
          <LiveCredentialInputField
            label="실전 계좌번호"
            name="accountNo"
            placeholder="실전 계좌 8자리"
            liveEditable={liveEditable}
            liveBusy={liveBusy}
            revealActive={revealedCredential === "accountNo"}
            onRevealStart={setRevealedCredential}
            onRevealEnd={clearReveal}
          />
          <LiveCredentialInputField
            label="상품 코드"
            name="productCode"
            placeholder="예: 01"
            liveEditable={liveEditable}
            liveBusy={liveBusy}
            revealActive={revealedCredential === "productCode"}
            onRevealStart={setRevealedCredential}
            onRevealEnd={clearReveal}
          />
        </div>
        <footer className="credential-actions">
          <span className={liveSaved ? "credential-status saved" : "credential-status"}>
            {liveSaved ? "실전 설정 저장됨" : "실전 설정 저장 전"}
          </span>
          <div className="credential-action-buttons">
            {liveSaved && !liveEditing ? (
              <button type="button" onClick={onLiveEdit}>
                실전 키 재입력
              </button>
            ) : null}
            {liveSaved && !liveEditing ? (
              <button type="button" className="blue-action" disabled={liveCheckBusy} onClick={() => void onLiveCheck()}>
                실전 계좌 조회 확인
              </button>
            ) : null}
            {liveSaved && !liveEditing ? (
              <button
                type="button"
                className="blue-action"
                aria-label="실전 준비도 점검"
                id="live-readiness-check-button"
                disabled={liveReadinessBusy}
                onClick={() => void onLiveReadinessCheck()}
              >
                실전 준비도 점검
              </button>
            ) : null}
            {liveSaved && liveEditing ? (
              <button type="button" disabled={liveBusy} onClick={onLiveCancelEdit}>
                재입력 취소
              </button>
            ) : null}
            {liveEditable ? (
              <button type="submit" className="blue-action" disabled={liveBusy}>
                실전 조회 설정 저장
              </button>
            ) : null}
          </div>
        </footer>
      </form>
    </section>
  );
}

function PositionsPanel({
  state,
  onAction,
}: {
  state: DashboardState;
  onAction: (action: string, payload?: Record<string, unknown>) => Promise<DashboardState>;
}) {
  const currentPriceColumn = state.mode.isReal ? "평균진입 / 현재가" : "평균진입 / 모의현재";
  return (
    <section className="panel positions-panel" id="positions">
      <PanelTitle title="보유 포지션" badge={modeBadgeForState(state)} tone={modeBadgeToneForState(state)} />
      <div className="positions-layout">
        <div className="table-card">
          <div className="table-scroll" role="region" aria-label="보유 포지션 표 스크롤 영역" tabIndex={0}>
            <table aria-label="보유 포지션">
              <thead>
                <tr>
                  <th>회사명 (코드)</th>
                  <th>롱/숏</th>
                  <th>수량</th>
                  <th>{currentPriceColumn}</th>
                  <th>손익</th>
                </tr>
              </thead>
              <tbody>
                {state.positions.length ? (
                  state.positions.map((position) => (
                    <PositionRowView
                      key={position.symbol}
                      position={position}
                      selected={state.selectedPosition?.symbol === position.symbol}
                      onSelect={() => onAction("position", { symbol: position.symbol })}
                    />
                  ))
                ) : (
                  <tr className="positions-empty-row">
                    <td colSpan={5} className="empty-cell">
                      보유 포지션이 없습니다.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <footer>
            <span>총 보유 {state.positions.length}개 종목</span>
            <strong>{state.account.summary.find((metric) => metric.label.includes("평가"))?.value || "총 평가손익 0원"}</strong>
          </footer>
        </div>
        <PositionDetailCard detail={state.selectedPosition} isReal={state.mode.isReal} />
      </div>
    </section>
  );
}

function PositionRowView({ position, selected, onSelect }: { position: PositionRow; selected: boolean; onSelect: () => void }) {
  return (
    <tr className={selected ? "selected-row" : ""} onClick={onSelect}>
      <td>{position.label || `${position.companyName} (${position.symbol})`}</td>
      <td className={position.side === "숏" ? "short" : "long"}>{position.side}</td>
      <td className="numeric">{position.quantity}</td>
      <td className="numeric">
        {position.avgPrice} / {position.lastPrice}
      </td>
      <td className={`numeric pnl ${position.pnlTone}`}>{position.unrealizedPnl}</td>
    </tr>
  );
}

function PositionDetailCard({ detail, isReal }: { detail: PositionDetail | null; isReal: boolean }) {
  if (!detail) {
    return (
      <article className="detail-card empty-detail">
        <h3>보유 포지션을 선택하세요.</h3>
        <p>종목을 선택하면 가격 흐름, 진입가, 현재가, 익절선, 손절선이 표시됩니다.</p>
      </article>
    );
  }
  return (
    <article className="detail-card">
      <div className="detail-header">
        <div>
          <h3>
            {detail.companyName} ({detail.symbol})
          </h3>
          <span>
            {detail.side} {detail.quantity}주 보유
          </span>
        </div>
        <strong className={detail.unrealizedPnl.startsWith("-") ? "loss" : "profit"}>{detail.unrealizedPnl}</strong>
      </div>
      <div className="price-cards">
        <span>
          현재가 <strong>{detail.lastPrice}</strong>
        </span>
        <span>
          평균 진입가 <strong>{detail.avgPrice}</strong>
        </span>
      </div>
      <PriceChart detail={detail} />
      <p className="detail-note">
        실선은 최근 {isReal ? "" : "모의 "}가격 흐름이며, 점선은 진입가/익절선/손절선 기준입니다.
      </p>
    </article>
  );
}

function PriceChart({
  detail,
  ariaLabel,
  caption = "가격 흐름",
}: {
  detail: PositionDetail;
  ariaLabel?: string;
  caption?: string;
}) {
  const chart = useMemo(() => buildChart(detail.pricePoints, detail.referenceLines), [detail.pricePoints, detail.referenceLines]);
  return (
    <div className="chart-block">
      <svg className="price-chart" viewBox="0 0 620 260" role="img" aria-label={ariaLabel || `${detail.companyName} 가격 흐름`}>
        <defs>
          <linearGradient id="priceGlow" x1="0" x2="1" y1="0" y2="0">
            <stop offset="0%" stopColor="#2fd3ee" />
            <stop offset="100%" stopColor="#5ee37d" />
          </linearGradient>
        </defs>
        <text x="56" y="22" className="chart-caption">
          {caption}
        </text>
        <text x="500" y="22" textAnchor="end" className="chart-sample-label">
          {chart.sampleSummary}
        </text>
        <rect className="plot-area" x="56" y="32" width="444" height="166" />
        {chart.axis.map((tick) => (
          <g key={tick.label}>
            <line className="chart-grid-line" x1="56" x2="500" y1={tick.y} y2={tick.y} />
            <text x="48" y={tick.y + 4} textAnchor="end" className="chart-axis-price">
              {tick.label}
            </text>
          </g>
        ))}
        {chart.references.map((line) => (
          <g key={line.label}>
            <line className={`reference ${line.kind}`} x1="56" x2="500" y1={line.y} y2={line.y} />
          </g>
        ))}
        <path className="price-path" d={chart.path} />
        {chart.points.map((point, index) => (
          <circle
            cx={point.x}
            cy={point.y}
            r={index === chart.points.length - 1 ? 5 : 3.5}
            className={`price-point ${index === 0 ? "entry-dot" : ""} ${index === chart.points.length - 1 ? "current-dot" : ""}`}
            key={`${point.time}-${index}`}
          />
        ))}
        <text x={chart.current.labelX} y={chart.current.labelY} textAnchor={chart.current.labelAnchor} className="current-label">
          현재 {formatPrice(chart.current.value)}
        </text>
        <text x="56" y="226" className="axis-label">
          {chart.startLabel}
        </text>
        <text x="500" y="226" textAnchor="end" className="axis-label">
          {chart.endLabel}
        </text>
      </svg>
      <div className="chart-reference-list" aria-label="차트 기준선">
        {chart.references.map((line) => (
          <span className={`reference-chip ${line.kind}`} key={line.label}>
            <span className="reference-chip-combined">{formatReferenceLabel(line)} {formatPrice(line.value)}</span>
            <span aria-hidden="true">{formatReferenceLabel(line)}</span>
            {" "}
            <strong aria-hidden="true">{formatPrice(line.value)}</strong>
          </span>
        ))}
      </div>
      <div className="chart-readout">
        <span className="chart-pill sample">{chart.sampleSummary}</span>
        {chart.distanceLabels.map((label) => (
          <span className="chart-pill" key={label}>
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}

function LogsPanel({ trades }: { trades: TradeLog[] }) {
  return (
    <aside className="panel logs-panel" id="logs" aria-label="매도/매수 로그">
      <TradeLogList id="trade-logs" title="매도/매수 로그" items={trades} />
    </aside>
  );
}

function TradeLogArchivePanel({ state, trades }: { state: DashboardState; trades: TradeLog[] }) {
  const parsedTrades = useMemo(() => trades.map(parseTradeLog), [trades]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  useEffect(() => {
    if (selectedKey !== null && !parsedTrades.some((trade) => trade.key === selectedKey)) {
      setSelectedKey(null);
    }
  }, [parsedTrades, selectedKey]);
  const selectedTrade = selectedKey === null ? null : parsedTrades.find((trade) => trade.key === selectedKey) || null;

  return (
    <section className="panel trade-log-workspace" role="region" aria-label="매도/매수 로그 상세">
      <PanelTitle title="매도/매수 로그" badge={modeBadgeForState(state)} tone={modeBadgeToneForState(state)} />
      <p className="panel-copy">이전까지 기록된 매수, 매도, 숏 진입, 거절 로그를 자세히 확인합니다.</p>
      <div className="trade-log-detail-layout">
        <div className="trade-log-archive-scroll" aria-label="매도/매수 로그 목록">
          {trades.length ? (
            trades.map((item, index) => (
              <TradeLogEntryView
                item={item}
                key={`${item.title}-${index}`}
                selected={parsedTrades[index]?.key === selectedKey}
                onSelect={() => setSelectedKey(parsedTrades[index]?.key || null)}
              />
            ))
          ) : (
            <p className="empty-log">아직 기록이 없습니다.</p>
          )}
        </div>
        <TradeLogDetailCard trade={selectedTrade} />
      </div>
    </section>
  );
}

function DiagnosticLogPanel({ state }: { state: DashboardState }) {
  const system = state.logs.system;
  const statuses = useMemo(() => diagnosticStatusesForLogs(system), [system]);
  const [exportStatus, setExportStatus] = useState("");
  const exportDiagnostics = () => {
    const filename = diagnosticExportFileName(new Date());
    downloadTextFile(filename, buildDiagnosticExportText(state));
    setExportStatus(`${filename} 저장을 요청했습니다.`);
  };

  return (
    <section className="panel diagnostic-log-workspace" role="region" aria-label="오류/진단 로그">
      <PanelTitle title="오류/진단 로그" badge="DIAG" tone="diag" />
      <div className="diagnostic-toolbar">
        <p className="panel-copy">
          KIS 조회, 브리지, 자동매매 cycle 오류를 시간순으로 확인합니다. API 키와 계좌번호 같은 민감정보는 표시하지 않습니다.
        </p>
        <div className="diagnostic-actions">
          <button type="button" className="blue-action" onClick={exportDiagnostics}>
            진단 로그 내보내기
          </button>
          {exportStatus ? <p className="diagnostic-export-status" role="status">{exportStatus}</p> : null}
        </div>
      </div>
      <div className="diagnostic-status-grid" aria-label="진단 상태 요약">
        {statuses.map((status) => (
          <article className={`diagnostic-status ${status.tone}`} key={status.key}>
            <strong>{status.label}</strong>
            <span>{status.detail}</span>
          </article>
        ))}
      </div>
      <div className="diagnostic-log-scroll">
        {system.length ? (
          system.map((entry, index) => {
            const level = normalizeSystemLogLevel(entry.level);
            const safeLevel = redactDiagnosticText(entry.level);
            const safeTitle = redactDiagnosticText(entry.title);
            const safeMessage = redactDiagnosticText(entry.message);
            return (
              <article className={`diagnostic-entry ${level}`} key={`${entry.timestamp}-${entry.title}-${index}`}>
                <header>
                  <span className={`log-tag ${level}`}>{safeLevel}</span>
                  <strong>
                    [{entry.timestamp}] {safeTitle}
                  </strong>
                </header>
                <pre>{safeMessage}</pre>
              </article>
            );
          })
        ) : (
          <p className="empty-log">아직 진단 로그가 없습니다.</p>
        )}
      </div>
    </section>
  );
}

function TradeLogList({ id, title, items }: { id?: string; title: string; items: TradeLog[] }) {
  return (
    <section className="log-section" id={id}>
      <h3>{title}</h3>
      <div className="log-scroll">
        {items.length ? (
          items.map((item, index) => <TradeLogEntryView item={item} key={`${item.title}-${index}`} />)
        ) : (
          <p className="empty-log">아직 기록이 없습니다.</p>
        )}
      </div>
    </section>
  );
}

function TradeLogEntryView({
  item,
  selected = false,
  onSelect,
}: {
  item: TradeLog;
  selected?: boolean;
  onSelect?: () => void;
}) {
  const trade = parseTradeLog(item);
  const content = (
    <>
      <time className="trade-time">{trade.time}</time>
      <div className="trade-main">
        <div className="trade-line">
          <span className={`log-tag ${trade.sideTone}`}>{trade.sideLabel}</span>
          {trade.resultRaw === "rejected" ? <span className="log-tag rejected">{trade.resultRaw}</span> : null}
          <strong>{trade.target}</strong>
          {trade.returnRate ? <span className={`trade-return ${trade.returnTone}`}>{trade.returnRate}</span> : null}
        </div>
        <p className="trade-detail">
          {trade.quantity} / {trade.price} / 결과: {trade.resultLabel} / 사유: {trade.reasonLabel}
          {trade.realizedPnl ? ` / ${trade.realizedPnl}` : ""}
        </p>
      </div>
    </>
  );

  if (onSelect) {
    return (
      <button
        type="button"
        className={`trade-entry trade-entry-button ${selected ? "selected" : ""}`}
        onClick={onSelect}
        aria-pressed={selected}
      >
        {content}
      </button>
    );
  }

  return (
    <article className="trade-entry">
      {content}
    </article>
  );
}

function TradeLogDetailCard({ trade }: { trade: ParsedTradeLog | null }) {
  if (!trade) {
    return (
      <article className="trade-log-detail-card empty-detail">
        <h3>기록을 선택하세요.</h3>
        <p>매수/매도 기록을 클릭하면 체결 가격, 실현손익, 수익률과 거래 지점 그래프가 표시됩니다.</p>
      </article>
    );
  }

  const detail = buildTradeChartDetail(trade);
  return (
    <article className="trade-log-detail-card">
      <div className="detail-header">
        <div>
          <h3>
            {trade.companyName} ({trade.symbol})
          </h3>
          <span>
            {trade.sideLabel} {trade.quantity} · {trade.resultLabel}
          </span>
        </div>
        {trade.returnRate ? <strong className={`trade-return large ${trade.returnTone}`}>수익률 {trade.returnRate}</strong> : null}
      </div>
      <div className="trade-detail-metrics">
        <span>
          체결가 <strong>{trade.price}</strong>
        </span>
        <span>
          추정 진입가 <strong>{formatPrice(trade.entryPriceValue)}</strong>
        </span>
        <span>
          실현손익 <strong className={trade.returnTone}>{formatRealizedPnlAmount(trade.realizedPnl) || "0원"}</strong>
        </span>
        <span>
          매매 기준 <strong>{trade.reasonLabel}</strong>
        </span>
      </div>
      <PriceChart detail={detail} ariaLabel={`${trade.companyName} 거래 지점 그래프`} caption="거래 지점" />
      <p className="detail-note">
        이 그래프는 선택한 로그의 체결가와 실현손익을 기준으로 매수 지점과 매도 지점을 설명합니다. 전체 시장
        분봉 차트가 아니라 로그 해석용 기준선입니다.
      </p>
    </article>
  );
}

function normalizeSystemLogLevel(level: string): LogLevel {
  const normalized = level.toLowerCase();
  if (normalized.includes("error")) return "error";
  if (normalized.includes("warning") || normalized.includes("warn")) return "warning";
  return "info";
}

function PanelTitle({ title, badge, tone = "paper" }: { title: string; badge?: string | null; tone?: PanelBadgeTone }) {
  return (
    <div className="panel-title">
      <h2>{title}</h2>
      {badge ? <span className={`panel-badge ${tone}`}>{badge}</span> : null}
    </div>
  );
}

function StatusBadge({
  children,
  tone,
}: {
  children: string;
  tone: "paper" | "success" | "warning" | "neutral" | "danger";
}) {
  return <span className={`status-badge ${tone}`}>{children}</span>;
}

function parseTradeLog(item: TradeLog): ParsedTradeLog {
  const title = item.title.match(/^\[(?<time>[^\]]+)\]\s+(?<side>.+)\s+(?<result>\S+)\s+-\s+(?<target>.+)$/);
  const parts = item.detail.split(" / ").map((part) => part.trim()).filter(Boolean);
  const resultRaw = item.result || title?.groups?.result || extractDetailValue(parts, "결과") || item.level;
  const reasonRaw = item.reason || extractDetailValue(parts, "사유") || "-";
  const target = buildTradeTarget(item, title?.groups?.target || item.title);
  const targetParts = splitTradeTarget(target);
  const sideLabel = item.sideLabel || title?.groups?.side || sideLabelFromSide(item.side || item.level, item.level);
  const tone = sideTone(sideLabel || item.side || item.level);
  const quantityValue = finiteNumber(item.quantity) ?? parseInteger(parts[0]);
  const priceValue = finiteNumber(item.price) ?? parseMoney(parts[1]);
  const realizedPnlValue = finiteNumber(item.realizedPnl) ?? parseMoney(parts.find((part) => part.startsWith("실현손익")) || "");
  const realizedPnlAmount = normalizeRealizedPnlAmount(item.realizedPnlText, realizedPnlValue);
  const isShort = sideLabel.includes("숏") || (item.side || "").toUpperCase().includes("SHORT");
  const entryPriceValue = inferTradeEntryPrice({
    exitPrice: priceValue,
    quantity: quantityValue,
    realizedPnl: realizedPnlValue,
    isShort,
  });
  const returnRateValue = inferTradeReturnRate({
    entryPrice: entryPriceValue,
    quantity: quantityValue,
    realizedPnl: realizedPnlValue,
  });
  const returnTone = realizedPnlValue > 0 ? "positive" : realizedPnlValue < 0 ? "negative" : "neutral";
  const rawTimestamp = item.timestamp || title?.groups?.time || "-";
  const symbol = item.symbol || targetParts.symbol || "-";
  return {
    key: tradeLogKey({
      rawTimestamp,
      symbol,
      side: item.side || sideLabel,
      result: resultRaw,
      quantity: quantityValue,
      price: priceValue,
      title: item.title,
    }),
    time: formatTradeTimestamp(rawTimestamp),
    sideLabel,
    sideTone: tone,
    resultRaw,
    resultLabel: translateTradeResult(resultRaw),
    target,
    companyName: item.companyName || targetParts.companyName || target,
    symbol,
    quantity: quantityValue > 0 ? `${quantityValue.toLocaleString("ko-KR")}주` : parts[0] || "-",
    quantityValue,
    price: item.priceText || (priceValue > 0 ? formatPrice(priceValue) : parts[1] || "-"),
    priceValue,
    reasonLabel: translateTradeReason(reasonRaw, sideLabel || item.side || item.level),
    realizedPnl: realizedPnlAmount ? `실현손익 ${realizedPnlAmount}` : "",
    realizedPnlValue,
    returnRate: returnRateValue === null ? "" : formatSignedPct(returnRateValue),
    returnTone,
    entryPriceValue,
    isShort,
  };
}

function tradeLogKey({
  rawTimestamp,
  symbol,
  side,
  result,
  quantity,
  price,
  title,
}: {
  rawTimestamp: string;
  symbol: string;
  side: string;
  result: string;
  quantity: number;
  price: number;
  title: string;
}) {
  return [rawTimestamp, symbol, side, result, quantity, price, title].join("|");
}

function formatTradeTimestamp(value: string) {
  const trimmed = value.trim();
  const compact = trimmed.match(/^\d{2}:\d{2}:\d{2}$/);
  if (compact) {
    return trimmed;
  }
  const isoTime = trimmed.match(/[T\s](\d{2}:\d{2}:\d{2})(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?$/);
  if (isoTime) {
    return isoTime[1];
  }
  return trimmed || "-";
}

function buildTradeTarget(item: TradeLog, fallback: string) {
  if (item.companyName && item.symbol) {
    return `${item.companyName} (${item.symbol})`;
  }
  return fallback;
}

function splitTradeTarget(target: string) {
  const match = target.match(/^(?<companyName>.+?)\s+\((?<symbol>[^)]+)\)$/);
  return {
    companyName: match?.groups?.companyName || "",
    symbol: match?.groups?.symbol || "",
  };
}

function sideLabelFromSide(side: string, level: LogLevel) {
  const normalized = side.toUpperCase();
  if (normalized.includes("SELL")) return "매도";
  if (normalized.includes("SHORT")) return normalized.includes("EXIT") ? "숏 청산" : "숏";
  if (normalized.includes("BUY")) return "매수";
  return sideLabelFromLevel(level);
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function parseInteger(value: string | undefined): number {
  const parsed = Number((value || "").replace(/[^\d-]/g, ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function parseMoney(value: string | undefined): number {
  const parsed = Number((value || "").replace(/[^\d.-]/g, ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function normalizeRealizedPnlAmount(text: string | undefined, value: number): string {
  if (text) {
    return formatRealizedPnlAmount(text);
  }
  if (value === 0) {
    return "";
  }
  return value < 0 ? formatSignedPrice(value) : formatPrice(value);
}

function formatRealizedPnlAmount(text: string): string {
  return text.replace(/^실현손익\s+/, "").replace(/^\+/, "").trim();
}

function inferTradeEntryPrice({
  exitPrice,
  quantity,
  realizedPnl,
  isShort,
}: {
  exitPrice: number;
  quantity: number;
  realizedPnl: number;
  isShort: boolean;
}) {
  if (exitPrice <= 0 || quantity <= 0 || realizedPnl === 0) {
    return exitPrice;
  }
  const pnlPerShare = realizedPnl / quantity;
  return Math.max(0, isShort ? exitPrice + pnlPerShare : exitPrice - pnlPerShare);
}

function inferTradeReturnRate({
  entryPrice,
  quantity,
  realizedPnl,
}: {
  entryPrice: number;
  quantity: number;
  realizedPnl: number;
}): number | null {
  const invested = Math.abs(entryPrice * quantity);
  if (invested <= 0 || realizedPnl === 0) {
    return null;
  }
  return (realizedPnl / invested) * 100;
}

function buildTradeChartDetail(trade: ParsedTradeLog): PositionDetail {
  const entryPrice = trade.entryPriceValue || trade.priceValue;
  const exitPrice = trade.priceValue || entryPrice;
  const isExitTrade = trade.sideTone === "sell" || trade.realizedPnlValue !== 0;
  const points = isExitTrade
    ? [
        { time: "매수", value: entryPrice },
        { time: trade.time, value: exitPrice },
      ]
    : [
        { time: trade.time, value: exitPrice },
        { time: "보유", value: exitPrice },
      ];
  const referenceLines = isExitTrade
    ? [
        { label: "매수 지점", value: entryPrice },
        { label: "매도 지점", value: exitPrice },
      ]
    : [{ label: "매수 지점", value: exitPrice }];

  return {
    symbol: trade.symbol,
    companyName: trade.companyName,
    label: trade.target,
    side: trade.isShort ? "숏" : "롱",
    quantity: trade.quantityValue,
    summary: trade.reasonLabel,
    avgPrice: formatPrice(entryPrice),
    lastPrice: formatPrice(exitPrice),
    unrealizedPnl: trade.realizedPnl || "0원",
    pricePoints: points,
    referenceLines,
  };
}

function extractDetailValue(parts: string[], label: string) {
  const found = parts.find((part) => part === label || part.startsWith(`${label} `) || part.startsWith(`${label}:`));
  if (!found) return "";
  return found.replace(label, "").replace(/^[:\s]+/, "").trim();
}

function sideLabelFromLevel(level: LogLevel) {
  if (level === "sell") return "매도";
  if (level === "short") return "숏";
  return "매수";
}

function sideTone(side: string) {
  if (side.includes("매도") || side.includes("청산") || side === "sell") return "sell";
  if (side.includes("숏") || side === "short") return "short";
  return "buy";
}

function translateTradeResult(result: string) {
  const normalized = result.toLowerCase();
  if (normalized === "filled") return "체결";
  if (normalized === "rejected") return "거절";
  if (normalized === "skipped") return "건너뜀";
  return result;
}

function translateTradeReason(reason: string, side = "") {
  const normalized = reason.trim().toLowerCase();
  const flowScore = normalized.match(/^flow_score_(\d+)$/);
  if (flowScore) {
    return `흐름 점수 ${flowScore[1]}`;
  }
  const normalizedSide = side.trim().toUpperCase();
  const isShortExit = normalizedSide.includes("SHORT_EXIT") || normalizedSide.includes("숏 청산");
  const isLongSell = normalizedSide.includes("SELL") || normalizedSide.includes("매도");
  if (normalized === "upper_trend_boundary") {
    if (isShortExit) {
      return "상단 추세 경계선 돌파 - 숏 손실 방어 기준으로 청산";
    }
    if (isLongSell) {
      return "상단 추세 경계선 도달 - 목표 가격권 기준으로 매도";
    }
    return "상단 추세 경계선 도달 - 전략 기준선에 닿아 청산";
  }
  if (normalized === "lower_trend_boundary") {
    if (isShortExit) {
      return "하단 추세 경계선 도달 - 숏 목표 가격권 기준으로 청산";
    }
    if (isLongSell) {
      return "하단 추세 경계선 이탈 - 손실 방어 기준으로 매도";
    }
    return "하단 추세 경계선 도달 - 전략 기준선에 닿아 청산";
  }
  const translations: Record<string, string> = {
    insufficient_cash: "현금 부족",
    insufficient_position: "보유 수량 부족",
    invalid_quantity: "주문 수량 오류",
    max_order_amount_exceeded: "이전 주문 상한 초과",
    max_position_amount_exceeded: "종목 안전 상한 초과",
    max_positions_reached: "최대 보유 종목 수 도달",
    daily_loss_limit_reached: "일일 손실 한도 도달",
    kill_switch_active: "정리 모드 활성화",
    cleanup_mode_active: "정리 모드 활성화",
    max_daily_entries_reached: "일일 진입 횟수 초과",
    order_failure_limit_reached: "주문 실패 한도 도달",
    paper_short_disabled: "숏 비활성화",
    position_side_conflict: "포지션 방향 충돌",
    flow_breakout: "흐름 돌파 조건 충족",
    replacement_entry: "보충 진입 조건 충족",
    top_up_entry: "보충 매수 조건 충족",
    take_profit: "익절 기준 도달",
    stop_loss: "손절 기준 도달",
    trailing_stop: "트레일링 스탑 도달",
    max_holding_time: "최대 보유 시간 도달",
    forced_exit: "강제 청산",
    no_signal: "매매 신호 없음",
    short_disabled: "숏 비활성화",
    wide_spread: "호가 차이 과다",
    overextended_move: "과열 구간",
    insufficient_data: "가격 샘플 부족 - 몇 cycle 더 누적 후 판단",
  };
  return translations[normalized] || reason.replaceAll("_", " ");
}

function buildChart(points: { time: string; value: number }[], references: ReferenceLine[]) {
  const safePoints = points.length ? points : [{ time: "09:00", value: 0 }];
  const displayPoints =
    safePoints.length === 1
      ? [
          { ...safePoints[0], time: "진입" },
          { time: "현재", value: safePoints[0].value },
        ]
      : safePoints;
  const values = [...displayPoints.map((point) => point.value), ...references.map((line) => line.value)].filter(Number.isFinite);
  const rawMin = Math.min(...values);
  const rawMax = Math.max(...values);
  const rawSpread = Math.max(rawMax - rawMin, 1);
  const padding = Math.max(rawSpread * 0.12, Math.abs(rawMax) * 0.002, 10);
  const min = rawMin - padding;
  const max = rawMax + padding;
  const spread = Math.max(max - min, 1);
  const plot = { left: 56, right: 500, top: 32, bottom: 198 };
  const x = (index: number) => plot.left + (index / Math.max(displayPoints.length - 1, 1)) * (plot.right - plot.left);
  const y = (value: number) => plot.bottom - ((value - min) / spread) * (plot.bottom - plot.top);
  const chartPoints = displayPoints.map((point, index) => ({ ...point, x: x(index), y: y(point.value) }));
  const path = chartPoints.map((point, index) => `${index === 0 ? "M" : "L"} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(" ");
  const latest = chartPoints[chartPoints.length - 1];
  const referencesWithY = staggerReferenceLabels(
    references.map((line) => ({
      ...line,
      kind: referenceClass(line),
      y: y(line.value),
      labelY: y(line.value),
    })),
    plot.top,
    plot.bottom,
  );
  const axisValues = [max, max - spread * 0.25, max - spread * 0.5, max - spread * 0.75, min];
  const sampleSummary = `가격 샘플 ${points.length}개`;
  return {
    path,
    axis: axisValues.map((value) => ({ y: y(value), label: formatPrice(value) })),
    references: referencesWithY,
    points: chartPoints,
    current: {
      x: latest.x,
      y: latest.y,
      value: latest.value,
      labelX: latest.x > 430 ? latest.x - 10 : latest.x + 10,
      labelY: Math.max(plot.top + 18, latest.y - 10),
      labelAnchor: latest.x > 430 ? ("end" as const) : ("start" as const),
    },
    startLabel: points.length <= 1 ? "진입" : safePoints[0].time,
    endLabel: points.length <= 1 ? "현재" : safePoints.at(-1)?.time || "현재",
    sampleSummary,
    distanceLabels: buildDistanceLabels(latest.value, references),
  };
}

function staggerReferenceLabels<T extends { y: number; labelY: number }>(lines: T[], top: number, bottom: number) {
  const sorted = lines.map((line, index) => ({ ...line, index })).sort((a, b) => a.y - b.y);
  const minimumGap = 18;
  let previous = top - minimumGap;
  for (const line of sorted) {
    line.labelY = Math.min(bottom - 6, Math.max(top + 10, line.y, previous + minimumGap));
    previous = line.labelY;
  }
  for (let index = sorted.length - 2; index >= 0; index -= 1) {
    const next = sorted[index + 1];
    const line = sorted[index];
    if (next.labelY - line.labelY < minimumGap) {
      line.labelY = Math.max(top + 10, next.labelY - minimumGap);
    }
  }
  return sorted.sort((a, b) => a.index - b.index);
}

function buildDistanceLabels(current: number, references: ReferenceLine[]) {
  return references
    .filter((line) => ["take", "stop"].includes(referenceClass(line)))
    .map((line) => `${line.label.replace("선", "")}까지 ${formatDistance(current, line.value)}`);
}

function referenceClass(line: { label: string }) {
  if (line.label.includes("매수")) return "entry";
  if (line.label.includes("매도") || line.label.includes("청산")) return "take";
  if (line.label.includes("익절")) return "take";
  if (line.label.includes("손절")) return "stop";
  if (line.label.includes("진입")) return "entry";
  return "trail";
}

function formatReferenceLabel(line: ReferenceLine) {
  const compactLabel = line.label
    .replace("평균 진입가", "평균")
    .replace("익절선", "익절")
    .replace("손절선", "손절")
    .replace("트레일링선", "트레일");
  return compactLabel;
}

function formatPrice(value: number) {
  return `${Math.round(value).toLocaleString("ko-KR")}원`;
}

function formatDistance(current: number, target: number) {
  const diff = target - current;
  const pct = current === 0 ? 0 : (diff / current) * 100;
  return `${formatSignedPrice(diff)} (${formatSignedPct(pct)})`;
}

function formatSignedPrice(value: number) {
  if (value === 0) return "0원";
  const sign = value > 0 ? "+" : "-";
  return `${sign}${formatPrice(Math.abs(value))}`;
}

function formatSignedPct(value: number) {
  if (value === 0) return "0.00%";
  const sign = value > 0 ? "+" : "-";
  return `${sign}${Math.abs(value).toFixed(2)}%`;
}

export default App;
