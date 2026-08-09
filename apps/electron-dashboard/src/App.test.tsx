import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";

import App, { buildDiagnosticExportText, diagnosticStatusesForLogs } from "./App";
import type {
  DashboardState,
  ProfitBucket,
  ProfitReport,
  ProfitReportQuery,
  ProfitReportResult,
} from "./types";

const fixture: DashboardState = {
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
    status: "실행 중",
    running: true,
    cycleLabel: "다음 cycle 예정됨",
    lastUpdated: "09:01:00",
    dataSourceKind: "local",
    safetySummary: "실제 주문 없음 · 로컬 데이터 replay",
    dataModeDescription: "샘플/로컬 데이터로 반복 검증합니다.",
    cleanupMode: false,
  },
  notice: {
    title: "PAPER 안전 모드",
    description: "가상모드입니다.",
    tone: "paper",
  },
  account: {
    title: "계좌 상태",
    metrics: [
      { label: "상태", value: "실행 중", emphasis: true },
      { label: "계좌", value: "가상계좌" },
      { label: "현금", value: "1,000,000원", emphasis: true },
      { label: "평가금", value: "1,000,000원", emphasis: true },
      { label: "보유 종목", value: "0개", emphasis: true },
      { label: "매수 가능", value: "1,000,000원", emphasis: true },
      { label: "조회 종목 현재가", value: "0원" },
      { label: "최근 갱신", value: "09:01:00" },
    ],
    summary: [],
  },
  positions: [],
  selectedPosition: null,
  logs: {
    trades: [],
    system: [
      { timestamp: "09:01:00", level: "warning", title: "KIS", message: "요청 제한 대기" },
      { timestamp: "09:02:00", level: "error", title: "Runtime", message: "cycle 실패" },
    ],
  },
};

function withRevision(state: DashboardState, stateRevision: number): DashboardState {
  return { ...state, stateRevision } as DashboardState;
}

function withLiveCredentialStatus(
  state: DashboardState,
  appKeySaved: boolean,
  appSecretSaved: boolean,
  accountNoSaved = appKeySaved && appSecretSaved,
  productCodeSaved = appKeySaved && appSecretSaved,
): DashboardState {
  return {
    ...state,
    settings: {
      kisLiveCredentials: {
        appKeySaved,
        appSecretSaved,
        accountNoSaved,
        productCodeSaved,
      },
    },
  } as DashboardState;
}

function withoutRevision(state: DashboardState): DashboardState {
  const { stateRevision: _stateRevision, ...rest } = state;
  return rest as DashboardState;
}

function withAccountMetric(state: DashboardState, label: string, value: string): DashboardState {
  return {
    ...state,
    account: {
      ...state.account,
      metrics: state.account.metrics.map((metric) => (metric.label === label ? { ...metric, value } : metric)),
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((innerResolve) => {
    resolve = innerResolve;
  });
  return { promise, resolve };
}

function currentKstDateForTest(): string {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const value = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((part) => part.type === type)?.value ?? "";
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function profitReportFixture(
  query: ProfitReportQuery,
  options: {
    value?: number | null;
    label?: string;
    buckets?: ProfitBucket[];
    status?: ProfitReport["status"];
    bridgeGeneration?: number;
    previousAnchor?: string | null;
    nextAnchor?: string | null;
  } = {},
): ProfitReport {
  const value = options.value === undefined ? 42000 : options.value;
  const buckets = options.buckets ?? [
    {
      key: "2026-07-01",
      label: options.label ?? "07/01",
      startAt: "2026-07-01T00:00:00+09:00",
      endAt: "2026-07-01T23:59:59+09:00",
      reportedRealizedPnlKrw: value,
      feeKrw: 1000,
      taxKrw: 600,
      interestKrw: 100,
      fillCount: 4,
      status: "confirmed",
      activityStatus: value === null ? "unknown" : "trade",
      costInclusion: "unknown",
      issues: [],
    },
  ];
  return {
    schemaVersion: 1,
    generatedAt: "2026-07-29T09:30:00+09:00",
    bridgeGeneration: options.bridgeGeneration ?? 1,
    status: options.status ?? "complete",
    costInclusion: "unknown",
    query,
    range: {
      label: options.label ?? "2026년 7월",
      startAt: "2026-07-01T00:00:00+09:00",
      endAt: "2026-07-31T23:59:59+09:00",
      anchor: query.anchor,
      previousAnchor: options.previousAnchor === undefined ? "2026-06-01" : options.previousAnchor,
      nextAnchor: options.nextAnchor === undefined ? null : options.nextAnchor,
    },
    summary: {
      reportedRealizedPnlKrw: value,
      profitableBucketsTotalKrw: value !== null && value > 0 ? value : 0,
      losingBucketsTotalKrw: value !== null && value < 0 ? value : 0,
      tradingCostKrw: 1700,
      profitableBucketCount: value !== null && value > 0 ? 1 : 0,
      losingBucketCount: value !== null && value < 0 ? 1 : 0,
      availableBucketCount: buckets.filter((bucket) => bucket.reportedRealizedPnlKrw !== null).length,
    },
    buckets,
    issues: [],
    dataSource: query.scope === "account" ? "kis_period_profit" : "managed_fill_ledger",
    updatedAt: "2026-07-29T09:29:30+09:00",
  };
}

function stubProfitBridge(
  loadProfitReport: (query: ProfitReportQuery) => Promise<ProfitReportResult>,
  dashboardState: DashboardState = {
    ...fixture,
    bridgeGeneration: 1,
    runtime: { ...fixture.runtime, running: false },
  },
) {
  const loadState = vi.fn(async () => dashboardState);
  const profitLoader = vi.fn(loadProfitReport);
  vi.stubGlobal("stockbotBridge", {
    loadState,
    loadProfitReport: profitLoader,
    runAction: vi.fn(async () => dashboardState),
  });
  return {
    loadState,
    loadProfitReport: profitLoader,
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.localStorage.clear();
});

describe("Electron dashboard app shell", () => {
  it("renders the Korean professional trading dashboard contract", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => fixture,
      })),
    );

    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    const logo = screen.getByRole("img", { name: "개미親주식 로고" });
    expect(logo.getAttribute("src")).toContain("stockbot-donghak-ant-icon.png");
    expect(screen.queryByText("PAPER 안전 모드")).toBeNull();
    const runtimeModeGroup = screen.getByRole("group", { name: "현재 테스트 구분" });
    expect(runtimeModeGroup).toBeTruthy();
    expect(screen.queryByText("거래 계좌")).toBeNull();
    expect(screen.queryByText("가상 계좌")).toBeNull();
    expect(screen.queryByText("리얼 계좌 잠금")).toBeNull();
    expect(screen.getByText("시세 출처")).toBeTruthy();
    expect(screen.getByText("로컬 가상 테스트")).toBeTruthy();
    expect(screen.getByText("장외 로컬 검증")).toBeTruthy();
    expect(screen.getByText("자동매매 cycle 주기 5초")).toBeTruthy();
    expect(within(runtimeModeGroup).getByText("최근 갱신 09:01:00")).toBeTruthy();
    expect(screen.queryByText("샘플/CSV 데이터 replay")).toBeNull();
    expect(screen.queryByText("원본")).toBeNull();
    expect(container.querySelector(".runtime-mode-card.local")).toBeTruthy();
    expect(container.querySelector(".runtime-mode-card.kis")).toBeNull();
    const runtimeActions = container.querySelector(".runtime-mode-actions");
    expect(runtimeActions).toBeTruthy();
    expect(runtimeModeGroup.contains(runtimeActions)).toBe(true);
    expect(within(runtimeActions as HTMLElement).getByRole("button", { name: /^▶ 자동매매 시작$/ })).toBeTruthy();
    expect(within(runtimeActions as HTMLElement).queryByRole("button", { name: /가상 자동매매 시작/ })).toBeNull();
    const modeButtons = within(screen.getByRole("group", { name: "거래 모드" })).getAllByRole("button");
    expect(modeButtons.map((button) => button.textContent)).toEqual(["리얼모드 REAL", "가상모드 PAPER"]);
    const pauseButton = within(runtimeActions as HTMLElement).getByRole("button", { name: /일시정지/ });
    expect(pauseButton.getAttribute("title")).toContain("현재 보유 상태");
    const cleanupButton = within(runtimeActions as HTMLElement).getByRole("button", { name: "정리모드" });
    expect(cleanupButton.getAttribute("aria-pressed")).toBe("false");
    expect(cleanupButton.getAttribute("title")).toContain("신규 매수");
    expect(within(runtimeActions as HTMLElement).queryByRole("button", { name: /AI 추천 실행/ })).toBeNull();
    const accountPanel = screen.getByRole("heading", { name: "계좌 상태" }).closest("section") as HTMLElement;
    expect(within(accountPanel).getByText("계좌")).toBeTruthy();
    expect(within(accountPanel).getByText("가상계좌")).toBeTruthy();
    expect(within(accountPanel).getByText("현금")).toBeTruthy();
    expect(within(accountPanel).queryByText("예수금")).toBeNull();
    expect(within(accountPanel).getByText("평가금")).toBeTruthy();
    expect(within(accountPanel).getByText("보유 종목")).toBeTruthy();
    expect(within(accountPanel).getByText("매수 가능")).toBeTruthy();
    expect(within(accountPanel).queryByText("최근 갱신")).toBeNull();
    expect(within(accountPanel).queryByText("상태")).toBeNull();
    expect(within(accountPanel).queryByText("조회 종목 현재가")).toBeNull();
    expect(accountPanel.querySelectorAll(".account-metric-grid .metric")).toHaveLength(4);
    expect(screen.queryByRole("heading", { name: "투자 전략" })).toBeNull();
    expect(container.querySelector(".strategy-card")).toBeNull();
    expect(screen.queryByText("보수형")).toBeNull();
    expect(screen.queryByText("균형형")).toBeNull();
    expect(screen.queryByText("공격형")).toBeNull();
    expect(screen.queryByText("커스텀")).toBeNull();
    expect(screen.queryByText("현금 사용 비율")).toBeNull();
    expect(screen.queryByText("종목 안전 상한")).toBeNull();
    expect(screen.queryByText("최대 보유 종목")).toBeNull();
    expect(screen.getByRole("table", { name: "보유 포지션" })).toBeTruthy();
    const emptyPositionRow = screen.getByText("보유 포지션이 없습니다.").closest("tr");
    expect(emptyPositionRow?.className).toContain("positions-empty-row");
    expect(screen.getByText("MadeBy :heewoung-lee")).toBeTruthy();
    expect(screen.queryByText("warning")).toBeNull();
    expect(screen.queryByText("error")).toBeNull();
    const dashboardLogPanel = screen.getByRole("complementary", { name: "매도/매수 로그" });
    expect(within(dashboardLogPanel).getByRole("heading", { name: "매도/매수 로그" })).toBeTruthy();
    expect(within(dashboardLogPanel).queryByText("실행 로그")).toBeNull();
    expect(screen.queryByRole("region", { name: "환경설정" })).toBeNull();
    expect(screen.queryByRole("button", { name: /KIS 연결 확인/ })).toBeNull();
    const sidebarItems = [...container.querySelectorAll(".side-nav button")].map((button) => button.textContent);
    expect(sidebarItems).toEqual(["대시보드", "매도/매수 로그", "손익 분석", "오류/진단 로그", "환경설정"]);
    expect(screen.getByRole("button", { name: "매도/매수 로그" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "손익 분석" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "오류/진단 로그" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "환경설정" })).toBeTruthy();
    expect(container.querySelector(".side-nav button.active")?.textContent).toBe("대시보드");
  });

  it("renders real deposit cash separately from buying power without duplicate cash cards", async () => {
    const realState: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      account: {
        ...fixture.account,
        metrics: [
          ...fixture.account.metrics.map((metric) =>
            metric.label === "매수 가능" ? { ...metric, value: "750,000원" } : metric,
          ),
          { label: "예수금", value: "1,200,000원", emphasis: true },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => realState,
      })),
    );

    render(<App />);

    const accountPanel = (await screen.findByRole("heading", { name: "계좌 상태" })).closest("section") as HTMLElement;
    expect(within(accountPanel).getByText("예수금")).toBeTruthy();
    expect(within(accountPanel).getByText("1,200,000원")).toBeTruthy();
    expect(within(accountPanel).getByText("매수 가능")).toBeTruthy();
    expect(within(accountPanel).getByText("750,000원")).toBeTruthy();
    expect(within(accountPanel).queryByText("현금")).toBeNull();
    expect(accountPanel.querySelectorAll(".account-metric-grid .metric")).toHaveLength(4);
  });

  it("switches into real mode directly from paper mode", async () => {
    const realState: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        status: "실전 잠금",
        dataSourceKind: "real-prep",
        dataModeDescription: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
      },
      notice: {
        title: "REAL 주문 잠금",
        description: "실전 주문은 잠금 상태입니다.",
        tone: "danger",
      },
    };
    const runAction = vi.fn(async () => realState);
    vi.stubGlobal("stockbotBridge", { loadState: vi.fn(async () => fixture), runAction });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /리얼모드/ }));

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("mode", { mode: "real" }));
    expect(screen.queryByRole("dialog", { name: "리얼모드 전환 확인" })).toBeNull();
  });

  it("shows a loading dialog while the real account is being loaded", async () => {
    const realState: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        status: "실전 준비",
        dataSourceKind: "real-prep",
        dataModeDescription: "KIS 실전 계좌 정보를 확인합니다.",
      },
    };
    const modeRequest = deferred<DashboardState>();
    const runAction = vi.fn(async () => modeRequest.promise);
    vi.stubGlobal("stockbotBridge", { loadState: vi.fn(async () => fixture), runAction });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /리얼모드/ }));

    const dialog = await screen.findByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" });
    expect(dialog.getAttribute("aria-busy")).toBe("true");
    expect(within(dialog).getByText("리얼모드로 전환 중")).toBeTruthy();
    expect(within(dialog).getByText("KIS 계좌 잔고와 보유 종목을 확인하고 있습니다.")).toBeTruthy();
    expect(screen.getByRole("button", { name: /리얼모드/ }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: /가상모드/ }).hasAttribute("disabled")).toBe(true);

    await act(async () => {
      modeRequest.resolve(realState);
      await modeRequest.promise;
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" })).toBeNull();
    });
  });

  it("shows the loading dialog when refreshing the selected real account", async () => {
    const realInitial: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        status: "실전 준비",
        dataSourceKind: "real-prep",
        dataModeDescription: "KIS 실전 계좌 정보를 확인합니다.",
      },
    };
    const accountRequest = deferred<DashboardState>();
    const runAction = vi.fn(async () => accountRequest.promise);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realInitial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /리얼모드/ }));

    const dialog = await screen.findByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" });
    expect(within(dialog).getByText("실전 계좌 새로고침 중")).toBeTruthy();

    await act(async () => {
      accountRequest.resolve(realInitial);
      await accountRequest.promise;
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" })).toBeNull();
    });
  });

  it("refreshes the live account when the selected real mode button is clicked", async () => {
    const realInitial: DashboardState = withLiveCredentialStatus(
      {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          status: "실전 잠금",
          running: false,
          dataSourceKind: "real-prep",
          dataModeLabel: "KIS 실전 계좌",
          dataModeDescription: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
        },
        notice: {
          title: "REAL 주문 잠금",
          description: "실전 주문은 잠금 상태입니다.",
          tone: "danger",
        },
      },
      true,
      true,
    );
    const refreshedState = withAccountMetric(
      withAccountMetric(
        withAccountMetric(
          withAccountMetric(realInitial, "계좌", "******78-01"),
          "현금",
          "1,200,000원",
        ),
        "평가금",
        "1,250,000원",
      ),
      "매수 가능",
      "750,000원",
    );
    const runAction = vi.fn(async () => refreshedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realInitial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /리얼모드/ }));

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("kis-live-check", {}));
    const accountPanel = screen.getByRole("heading", { name: "계좌 상태" }).closest("section") as HTMLElement;
    expect(within(accountPanel).getByText("******78-01")).toBeTruthy();
    expect(within(accountPanel).getByText("1,200,000원")).toBeTruthy();
    expect(within(accountPanel).getByText("1,250,000원")).toBeTruthy();
    expect(within(accountPanel).getByText("750,000원")).toBeTruthy();
  });

  it("asks before switching from real mode to paper when the real account has holdings", async () => {
    const realFixture: DashboardState = withRevision(
      withAccountMetric(
        {
          ...fixture,
          mode: { key: "real", label: "리얼 모드", isReal: true },
          runtime: {
            ...fixture.runtime,
            status: "실전 잠금",
            running: false,
            cycleLabel: "예약 없음",
            dataSourceKind: "real-prep",
            dataModeDescription: "실전 계좌 연결",
          },
          notice: {
            ...fixture.notice,
            title: "REAL 주문 잠금",
            description: "실전 주문은 잠금 상태입니다.",
            tone: "danger",
          },
          positions: [],
        },
        "보유 종목",
        "2개",
      ),
      1,
    );
    const paperState = withRevision(fixture, 2);
    const runAction = vi.fn(async () => paperState);
    vi.stubGlobal("stockbotBridge", { loadState: vi.fn(async () => realFixture), runAction });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: /가상모드/ }));

    expect(runAction).not.toHaveBeenCalled();
    expect(await screen.findByRole("dialog", { name: "실전 보유 종목 확인" })).toBeTruthy();
    expect(screen.getByText(/실전 계좌에 보유 종목 2개가 있습니다/)).toBeTruthy();
    expect(screen.getByText(/앱 정리모드로 실제 매도하지 않습니다/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "즉시 가상모드 전환" }));

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("mode", { mode: "virtual" }));
    expect(screen.queryByRole("dialog", { name: "실전 보유 종목 확인" })).toBeNull();
  });

  it("shows the account freshness timestamp in the runtime strip even when runtime freshness is blank", async () => {
    const accountFreshnessOnlyState = withAccountMetric(
      {
        ...fixture,
        runtime: {
          ...fixture.runtime,
          lastUpdated: "-",
        },
      },
      "최근 갱신",
      "09:44:00",
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => accountFreshnessOnlyState,
      })),
    );

    render(<App />);

    const runtimeModeGroup = await screen.findByRole("group", { name: "현재 테스트 구분" });
    expect(within(runtimeModeGroup).getByText("최근 갱신 09:44:00")).toBeTruthy();
    const accountPanel = screen.getByRole("heading", { name: "계좌 상태" }).closest("section") as HTMLElement;
    expect(within(accountPanel).queryByText("최근 갱신")).toBeNull();
    expect(accountPanel.querySelectorAll(".account-metric-grid .metric")).toHaveLength(4);
  });

  it("renders real trading prep dashboard surfaces without read-only badges", async () => {
    const realFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        status: "실전 잠금",
        running: false,
        cycleLabel: "예약 없음",
        lastUpdated: "10:10:00",
        dataSourceKind: "real-prep",
        dataModeLabel: "KIS 실전 계좌",
        dataModeDescription: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
        safetySummary: "실전 거래 준비 · 시작 시 안전 게이트 확인",
      },
      notice: {
        title: "REAL 주문 준비",
        description: "자동매매 시작 시 실전 계좌 주문 게이트를 확인합니다.",
        tone: "neutral",
      },
      account: {
        ...fixture.account,
        metrics: fixture.account.metrics.map((metric) => {
          if (metric.label === "계좌") return { ...metric, value: "******06-01" };
          if (metric.label === "현금") return { ...metric, value: "100,202원" };
          if (metric.label === "평가금") return { ...metric, value: "100,202원" };
          if (metric.label === "보유 종목") return { ...metric, value: "1개" };
          if (metric.label === "최근 갱신") return { ...metric, value: "10:10:00" };
          return metric;
        }),
        summary: [],
      },
      positions: [
        {
          symbol: "005930",
          companyName: "삼성전자",
          label: "삼성전자 (005930)",
          side: "롱",
          quantity: 1,
          avgPrice: "70,000원",
          lastPrice: "70,500원",
          unrealizedPnl: "500원",
          pnlTone: "positive",
        },
      ],
      selectedPosition: {
        symbol: "005930",
        companyName: "삼성전자",
        label: "삼성전자 (005930)",
        side: "롱",
        quantity: 1,
        summary: "실전 계좌 보유 종목",
        avgPrice: "70,000원",
        lastPrice: "70,500원",
        unrealizedPnl: "500원",
        pricePoints: [
          { time: "10:00", value: 70000 },
          { time: "10:05", value: 70500 },
        ],
        referenceLines: [
          { label: "진입", value: 70000 },
          { label: "현재", value: 70500 },
          { label: "익절", value: 72100 },
          { label: "손절", value: 68600 },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => realFixture,
      })),
    );

    const { container } = render(<App />);
    await act(async () => {});

    expect(await screen.findByText("KIS 실전 계좌")).toBeTruthy();
    expect(container.textContent).not.toContain("READ-ONLY");
    expect(container.textContent).not.toContain("읽기 전용");
    expect(screen.queryByText("시세 출처 확인 필요")).toBeNull();
    expect(container.querySelector(".status-badge.danger")?.textContent).toBe("리얼 모드");
    expect(container.textContent).not.toContain("REAL 준비");
    const titleLine = container.querySelector(".title-line") as HTMLElement;
    expect(titleLine.textContent).not.toContain("실전 잠금");
    expect(within(screen.getByRole("group", { name: "현재 테스트 구분" })).getByText("최근 갱신 10:10:00")).toBeTruthy();

    const accountPanel = screen.getByRole("heading", { name: "계좌 상태" }).closest("section") as HTMLElement;
    expect(within(accountPanel).queryByText("최근 갱신")).toBeNull();
    expect(within(accountPanel).queryByText(/Paper 총손익/)).toBeNull();
    expect(within(accountPanel).getByText("총손익 0원 · 실현손익 0원 · 평가손익 0원")).toBeTruthy();

    expect(screen.queryByRole("heading", { name: "투자 전략" })).toBeNull();

    const positionsPanel = screen.getByRole("heading", { name: "보유 포지션" }).closest("section") as HTMLElement;
    expect(within(positionsPanel).getByText("평균진입 / 현재가")).toBeTruthy();
    expect(within(positionsPanel).queryByText("평균진입 / 모의현재")).toBeNull();
    expect(within(positionsPanel).getByText(/실선은 최근 가격 흐름/)).toBeTruthy();

    const panelBadges = [...container.querySelectorAll(".dashboard-view .panel-badge")].map((badge) => badge.textContent);
    expect(panelBadges).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "매도/매수 로그" }));
    const tradeArchive = await screen.findByRole("region", { name: "매도/매수 로그 상세" });
    expect(within(tradeArchive).queryByText("REAL 준비")).toBeNull();
  });

  it("normalizes legacy real read-only payloads to real trading prep copy", async () => {
    const legacyRealFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "실전 잠금",
        cycleLabel: "예약 없음",
        dataSourceKind: "real-read-only",
        dataModeLabel: "KIS 실전 읽기 전용",
        dataModeDescription: "실전 계좌 잔고와 현재가를 읽기 전용으로만 확인합니다.",
        safetySummary: "실전 계좌 읽기 전용",
      },
      notice: {
        title: "REAL 주문 준비",
        description: "자동매매 시작 시 실전 계좌 주문 게이트를 확인합니다.",
        tone: "neutral",
        orderEnabled: false,
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => legacyRealFixture,
      })),
    );

    const { container } = render(<App />);

    const runtimeModeGroup = await screen.findByRole("group", { name: "현재 테스트 구분" });
    expect(within(runtimeModeGroup).getByText("KIS 실전 계좌")).toBeTruthy();
    expect(within(runtimeModeGroup).getByText("실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.")).toBeTruthy();
    expect(within(runtimeModeGroup).getByText("실전 거래 준비 · 시작 시 안전 게이트 확인")).toBeTruthy();
    expect(container.textContent).not.toContain("READ-ONLY");
    expect(container.textContent).not.toContain("읽기 전용");
  });

  it("does not render a persistent real-mode notice banner over dashboard panels", async () => {
    const realLockedState: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        status: "실전 잠금",
        running: false,
        cycleLabel: "예약 없음",
        dataSourceKind: "real-prep",
        dataModeLabel: "KIS 실전 계좌",
        dataModeDescription: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
        safetySummary: "실전 주문 잠금 · 주문 전송 없음",
      },
      notice: {
        title: "REAL 주문 잠금",
        description: "실계좌 주문은 안전장치로 비활성화되어 있습니다.",
        tone: "danger",
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => realLockedState,
      })),
    );

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelector(".top-stack")).toBeTruthy());
    const workspace = container.querySelector(".workspace") as HTMLElement;
    const directChildren = Array.from(workspace.children);

    expect(directChildren[0].classList.contains("top-stack")).toBe(true);
    expect(directChildren[1].classList.contains("dashboard-view")).toBe(true);
    expect(directChildren.some((child) => child.classList.contains("notice"))).toBe(false);
    expect(directChildren[0].querySelector(".notice")).toBeNull();
  });

  it("opens a dedicated diagnostic log view from the left menu", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            system: [
              {
                timestamp: "10:48:57",
                level: "info",
                title: "Paper runtime",
                message:
                  "오류 - LG화학 (051910): 데이터 조회 실패 - KIS HTTP 500: EGW00201 Authorization: Bearer secret-token-123 account 12345678 C:\\Users\\example-user\\Documents\\StockProject\\.env",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "오류/진단 로그" }));

    const panel = await screen.findByRole("region", { name: "오류/진단 로그" });
    expect(within(panel).getByText(/KIS HTTP 500/)).toBeTruthy();
    expect(within(panel).getByText(/EGW00201/)).toBeTruthy();
    expect(panel.textContent).not.toContain("secret-token-123");
    expect(panel.textContent).not.toContain("12345678");
    expect(panel.textContent).not.toContain("C:\\Users\\example-user");
    expect(panel.textContent).toContain("[REDACTED]");
    expect(panel.textContent).toContain("[REDACTED_PATH]");
    expect(within(panel).getByText("KIS 초당 제한")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "진단 로그 내보내기" })).toBeTruthy();
  });

  it("classifies diagnostic logs into user-readable status chips", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "10:48:57",
        level: "info",
        title: "Paper runtime",
        message: "오류 - 삼성전자 (005930): 데이터 조회 실패 - KIS HTTP 500: EGW00201 초당 거래건수를 초과하였습니다.",
      },
      {
        timestamp: "10:49:01",
        level: "info",
        title: "Paper runtime",
        message: "오류 - NAVER (035420): 데이터 조회 실패 - KIS network timeout: The read operation timed out",
      },
      {
        timestamp: "10:49:02",
        level: "info",
        title: "Paper runtime",
        message: "관망 - LG화학 (051910): 거래 조건 미충족 (direction=hold, confidence=0.00, reasons=insufficient_data)",
      },
    ]);

    expect(statuses.map((status) => status.label)).toEqual(["KIS 초당 제한", "시세 조회 지연", "데이터 누적 중"]);
    expect(statuses[0].detail).toContain("1건");
  });

  it("classifies live pending order readiness blockers instead of reporting no critical error", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "10:49:20",
        level: "warning",
        title: "Live readiness",
        message:
          "Live readiness blocked: pending live order requires reconciliation before live readiness: count=1 symbols=005930",
      },
    ]);

    expect(statuses).toHaveLength(1);
    expect(statuses[0].key).toBe("live-pending-order");
    expect(statuses[0].tone).toBe("warning");
  });

  it("classifies sanitized KIS limit messages into the rate-limit status", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "10:49:03",
        level: "warning",
        title: "KIS 모의투자 연결 확인",
        message: "KIS 초당 요청 제한을 초과했습니다. 잠시 후 다시 시도하세요.",
      },
    ]);

    expect(statuses[0].label).toBe("KIS 초당 제한");
  });

  it("marks an earlier live-check rate limit as recovered after a newer successful check", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "09:39:20",
        level: "success",
        title: "KIS 실전 조회 확인",
        message: "실전 계좌 잔고와 기준 종목 현재가를 확인했습니다.",
      },
      {
        timestamp: "09:39:14",
        level: "error",
        title: "KIS 실전 조회 확인",
        message: "KIS 초당 요청 제한을 초과했습니다. 잠시 후 다시 시도하세요.",
      },
    ]);

    expect(statuses[0].key).toBe("kis-rate-limit-recovered");
    expect(statuses[0].label).toBe("KIS 초당 제한 복구됨");
    expect(statuses[0].detail).toContain("이후 실전 계좌 조회 성공");
    expect(statuses[0].tone).toBe("success");
  });

  it("classifies the KIS ledger rate-limit code used by live cycles", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "09:40:20",
        level: "error",
        title: "Paper runtime",
        message: "cycle_exception - KIS HTTP 500: EGW00215 ledger rate limit exceeded",
      },
    ]);

    expect(statuses[0].key).toBe("kis-rate-limit");
    expect(statuses[0].label).toBe("KIS 초당 제한");
  });

  it("classifies missing scanner snapshots as a setup blocker", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "14:06:59",
        level: "error",
        title: "테스트 데이터 전환",
        message:
          "새 테스트 데이터 runtime을 준비하는 중 오류가 발생했습니다: scanner_snapshot.json 파일이 없습니다. 외부 수집기로 data 폴더에 scanner_snapshot.json을 먼저 생성하세요",
      },
    ]);

    expect(statuses).toHaveLength(1);
    expect(statuses[0].label).toBe("스캐너 스냅샷 필요");
    expect(statuses[0].tone).toBe("warning");
  });

  it("classifies stale scanner snapshots as a refresh blocker", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "14:07:00",
        level: "error",
        title: "Live runtime",
        message:
          "실전 runtime 구성 중 오류가 발생했습니다. 원인: scanner snapshot refresh failed: stale scanner snapshot age_seconds=239035 path=data/scanner_snapshot.json",
      },
    ]);

    expect(statuses).toHaveLength(1);
    expect(statuses[0].key).toBe("scanner-snapshot-stale");
    expect(statuses[0].label).toBe("스캐너 데이터 갱신 필요");
    expect(statuses[0].detail).toContain("외부 수집기");
  });

  it("classifies expired KIS access tokens as a token refresh issue", () => {
    const statuses = diagnosticStatusesForLogs([
      {
        timestamp: "10:15:54",
        level: "info",
        title: "Paper runtime",
        message:
          '오류 - 현대자동차 (005380): 데이터 조회 실패 - KIS HTTP 500: {"rt_cd":"1","msg1":"기간이 만료된 [REDACTED]","msg_cd":"EGW00123"}',
      },
    ]);

    expect(statuses).toHaveLength(1);
    expect(statuses[0].label).toBe("KIS 토큰 만료");
    expect(statuses[0].detail).toContain("자동으로 토큰 캐시를 비우고 1회 재발급");
  });

  it("exports diagnostic logs as redacted UTF-8 JSON text", () => {
    const runtimeWithSensitiveKey = {
      ...fixture.runtime,
      status: "Authorization: Bearer runtime-token",
      dataSource: "KIS VTS quote / paper",
      dataModeLabel: "KIS 장중 테스트",
      access_token: "runtime-object-token",
      authorizationHeader: "runtime-auth-header",
      api_key_id: "runtime-api-key-id",
      user_access_token: "runtime-user-token",
    } as typeof fixture.runtime & Record<string, unknown>;
    const exportState: DashboardState = {
      ...fixture,
      bridgeGeneration: 7,
      notice: {
        ...fixture.notice,
        description:
          "bridge failed: api_key leaked-value token=raw-token appkey visible-key authorization access_token=jwt-secret-value refresh_token=refresh-secret-value C:\\Temp\\stockbot\\bridge.log",
      },
      runtime: runtimeWithSensitiveKey,
      debug: {
        cycle: {
          id: 12,
          durationMs: 4200,
          scannedSymbols: ["005930", "000660"],
        },
        effectivePolicy: {
          policy: "single-built-in",
          minSignalConfidence: "0.25",
          minMomentumPct: "0",
          minVolumeRatio: "0",
          requireVwapAlignment: false,
        },
        secretDump: {
          access_token: "debug-access-token",
          api_key: "debug-api-key",
        },
        accountNo: "12345678",
        acct: "12345678",
        fullTradeLogs: [
          {
            title: "[09:00:00] buy filled - Samsung Electronics (005930)",
            detail: "1 share / 70000 / result filled / reason flow",
            level: "buy",
            symbol: "005930",
            companyName: "Samsung Electronics",
            side: "BUY",
            quantity: 1,
            price: 70000,
            reason: "Authorization: Bearer full-trade-token",
            result: "filled",
            realizedPnl: 0,
          },
        ],
      },
      settings: {
        kisLiveCredentials: {
          appKeySaved: true,
          appSecretSaved: true,
          accountNoSaved: true,
          productCodeSaved: true,
        },
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: false,
          riskLimitsOk: true,
          newEntriesAllowed: false,
        },
      },
      logs: {
        trades: [
          {
            title: "[09:00:00] 매수 rejected - 삼성전자 (005930)",
            detail: "1주 / 70000원 / 결과 rejected / 사유 insufficient_cash / 모드 paper",
            level: "rejected",
          },
        ],
        system: [
          {
            timestamp: "09:00:01",
            level: "error",
            title: "Bridge",
            message:
              "Authorization: Bearer secret-token-123 KIS_VTS_APP_SECRET=secret-value account 12345678 C:\\Users\\example-user\\Documents\\StockProject\\.env",
          },
        ],
      },
    };

    const exported = JSON.parse(buildDiagnosticExportText(exportState));

    expect(exported.app.title).toBe(fixture.app.title);
    expect(exported.stateRevision).toBe(exportState.stateRevision);
    expect(exported.bridgeGeneration).toBe(7);
    expect(exported).not.toHaveProperty("strategy");
    expect(exported.debug.cycle.id).toBe(12);
    expect(exported.debug.cycle.scannedSymbols).toEqual(["005930", "000660"]);
    expect(exported.debug.effectivePolicy.minSignalConfidence).toBe("0.25");
    expect(exported.debug.fullTradeLogs).toHaveLength(1);
    expect(exported.debug.fullTradeLogs[0].symbol).toBe("005930");
    expect(exported.debug.fullTradeLogs[0].reason).toContain("[REDACTED]");
    expect(exported.debug.exportState.positionCount).toBe(0);
    expect(exported.debug.exportState.systemLogCount).toBe(1);
    expect(exported.settings.credentialFields.savedCount).toBe(4);
    expect(exported.settings.credentialFields.complete).toBe(true);
    expect(exported.settings.liveOrderGate.sessionApproved).toBe(false);
    expect(exported.settings.liveOrderGate.newEntriesAllowed).toBe(false);
    expect(exported.logs.system[0].message).toContain("[REDACTED]");
    expect(JSON.stringify(exported)).not.toContain("secret-token-123");
    expect(JSON.stringify(exported)).not.toContain("secret-value");
    expect(JSON.stringify(exported)).not.toContain("12345678");
    expect(JSON.stringify(exported)).not.toContain("C:\\Users\\example-user");
    expect(JSON.stringify(exported)).not.toContain("leaked-value");
    expect(JSON.stringify(exported)).not.toContain("raw-token");
    expect(JSON.stringify(exported)).not.toContain("visible-key");
    expect(JSON.stringify(exported)).not.toContain("jwt-secret-value");
    expect(JSON.stringify(exported)).not.toContain("refresh-secret-value");
    expect(JSON.stringify(exported)).not.toContain("runtime-token");
    expect(JSON.stringify(exported)).not.toContain("runtime-object-token");
    expect(JSON.stringify(exported)).not.toContain("runtime-auth-header");
    expect(JSON.stringify(exported)).not.toContain("runtime-api-key-id");
    expect(JSON.stringify(exported)).not.toContain("runtime-user-token");
    expect(JSON.stringify(exported)).not.toContain("debug-access-token");
    expect(JSON.stringify(exported)).not.toContain("debug-api-key");
    expect(JSON.stringify(exported)).not.toContain("accountNo");
    expect(JSON.stringify(exported)).not.toContain("acct");
    expect(JSON.stringify(exported)).not.toContain("12345678");
    expect(JSON.stringify(exported)).not.toMatch(/api[_\s-]?key/i);
    expect(JSON.stringify(exported)).not.toMatch(/\bappkey\b/i);
    expect(JSON.stringify(exported)).not.toMatch(/access[_\s-]?token/i);
    expect(JSON.stringify(exported)).not.toMatch(/refresh[_\s-]?token/i);
    expect(JSON.stringify(exported)).not.toMatch(/authorization/i);
    expect(JSON.stringify(exported)).not.toContain("C:\\Temp\\stockbot");
    expect(exported.logs.trades).toHaveLength(1);
  });

  it("distinguishes KIS quote-backed paper mode from local paper mode", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          runtime: {
            ...fixture.runtime,
            dataSourceKind: "kis-vts",
            safetySummary: "실제 주문 없음 · 후보군 20종목 · cycle당 조회 최대 10종목 · 최대 보유 10종목",
            dataModeDescription: "KIS 현재가를 받아 paper 계좌로만 체결합니다.",
          },
        }),
      })),
    );

    const { container } = render(<App />);

    await screen.findByText("KIS 장중 시세 테스트");
    expect(screen.getByRole("group", { name: "현재 테스트 구분" })).toBeTruthy();
    expect(screen.getByText("KIS 장중 시세 테스트")).toBeTruthy();
    expect(screen.getByText("정규장 현재가 조회 + paper 체결")).toBeTruthy();
    expect(screen.getByText("실제 주문 없음 · 후보군 20종목 · cycle당 조회 최대 10종목 · 최대 보유 10종목")).toBeTruthy();
    expect(screen.getByText("자동매매 cycle 주기 60초")).toBeTruthy();
    expect(screen.getByText("가상 모드")).toBeTruthy();
    expect(screen.queryByText("가상 계좌")).toBeNull();
    expect(container.querySelector(".runtime-mode-card.kis")).toBeTruthy();
    expect(container.querySelector(".runtime-mode-card.local")).toBeNull();
    expect((screen.getByRole("button", { name: /^▶ 자동매매 시작$/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("describes hybrid KIS paper mode as wide scanner plus final KIS quote confirmation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          runtime: {
            ...fixture.runtime,
            dataSourceKind: "external-scan-kis",
            safetySummary: "실제 주문 없음 · 후보군 2606종목 · 스캐너 선별 · KIS 현재가 최종 확인",
            dataModeDescription: "넓은 후보군을 먼저 선별하고, 주문 직전 KIS 현재가로 최종 확인합니다.",
          },
        }),
      })),
    );

    const { container } = render(<App />);

    await screen.findByText("KIS 장중 하이브리드 테스트");
    expect(screen.getByText("넓은 후보군을 먼저 선별하고, 주문 직전 KIS 현재가로 최종 확인합니다.")).toBeTruthy();
    expect(screen.getByText("실제 주문 없음 · 후보군 2606종목 · 스캐너 선별 · KIS 현재가 최종 확인")).toBeTruthy();
    expect(screen.getByText("자동매매 cycle 주기 60초")).toBeTruthy();
    expect(container.querySelector(".runtime-mode-card.kis")).toBeTruthy();
    expect(container.querySelector(".runtime-mode-card.local")).toBeNull();
  });

  it("hides the native Electron application menu from the desktop window", () => {
    const mainProcess = readFileSync(resolve(__dirname, "../electron/main.cjs"), "utf8");

    expect(mainProcess).toContain("Menu.setApplicationMenu(null)");
    expect(mainProcess).toContain("autoHideMenuBar: true");
  });

  it("lets the user request scanner-backed KIS final quote paper mode from the KIS market test button", async () => {
    const runAction = vi.fn(async () => ({
      ...fixture,
      runtime: {
        ...fixture.runtime,
        dataSourceKind: "external-scan-kis",
        dataModeDescription: "KIS 현재가를 받아 paper 계좌로만 체결합니다.",
      },
    }));
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "KIS 장중 테스트" }));

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("data-source", { source: "external-scan-kis" }));
  });

  it("lets an existing direct KIS quote paper state migrate to scanner-backed KIS paper mode", async () => {
    const directKisState: DashboardState = {
      ...fixture,
      runtime: {
        ...fixture.runtime,
        dataSourceKind: "kis-vts",
        safetySummary: "실제 주문 없음 · 후보군 20종목 · cycle당 조회 최대 10종목 · 최대 보유 10종목",
        dataModeDescription: "KIS 현재가를 받아 paper 계좌로만 체결합니다.",
      },
    };
    const runAction = vi.fn(async () => ({
      ...directKisState,
      runtime: {
        ...directKisState.runtime,
        dataSourceKind: "external-scan-kis",
        safetySummary: "실제 주문 없음 · 스캐너 선별 · KIS 현재가 최종 확인",
      },
    }));
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => directKisState),
      runAction,
    });

    render(<App />);

    const switchButton = (await screen.findByRole("button", { name: "KIS 장중 테스트" })) as HTMLButtonElement;
    expect(switchButton.disabled).toBe(false);
    fireEvent.click(switchButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("data-source", { source: "external-scan-kis" }));
  });

  it("shows a market-hours popup when KIS market-hours paper switch is blocked", async () => {
    const blockedState: DashboardState = {
      ...fixture,
      actionPopup: {
        title: "장중 테스트 불가",
        message: "장 대기 - 정규장 시간이 아닙니다. paper 자동매매는 정규장(09:00-15:30 KST)에만 실행합니다.",
        tone: "warning",
      },
      logs: {
        ...fixture.logs,
        system: [
          {
            timestamp: "20:00:00",
            level: "warning",
            title: "장중 테스트 전환 차단",
            message: "장 대기 - 정규장 시간이 아닙니다. paper 자동매매는 정규장(09:00-15:30 KST)에만 실행합니다.",
          },
          ...fixture.logs.system,
        ],
      },
    };
    const runAction = vi.fn(async () => blockedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "KIS 장중 테스트" }));

    const dialog = await screen.findByRole("dialog", { name: "장중 테스트 불가" });
    expect(dialog).toBeTruthy();
    expect(within(dialog).getByText(/정규장 시간이 아닙니다/)).toBeTruthy();
    expect(runAction).toHaveBeenCalledWith("data-source", { source: "external-scan-kis" });
  });

  it("opens a dedicated buy/sell log view from the left menu", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[21:23:00] 매수 rejected - 신라섬유 (001000)",
                detail: "19주 / 4,105원 / 결과 rejected / 사유 insufficient_cash / 모드 paper",
                level: "rejected",
              },
              {
                title: "[21:23:00] 매도 filled - 우리로 (046970)",
                detail: "4주 / 16,550원 / 결과 filled / 사유 take_profit / 모드 paper / 실현손익 1,306원",
                level: "sell",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "매도/매수 로그" }));

    const workspace = await screen.findByRole("region", { name: "매도/매수 로그 상세" });
    expect(within(workspace).getByRole("heading", { name: "매도/매수 로그" })).toBeTruthy();
    expect(within(workspace).getByText("신라섬유 (001000)")).toBeTruthy();
    expect(within(workspace).getByText("우리로 (046970)")).toBeTruthy();
    expect(within(workspace).getByText(/사유: 현금 부족/)).toBeTruthy();
    expect(within(workspace).getByText(/사유: 익절 기준 도달/)).toBeTruthy();
    expect(screen.queryByRole("table", { name: "보유 포지션" })).toBeNull();
  });

  it("shows only live KIS settings in the environment view", async () => {
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction: vi.fn(async () => fixture),
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });

    expect(within(panel).queryByText("모의투자 / 장중 테스트 설정")).toBeNull();
    expect(within(panel).queryByLabelText("KIS API Key")).toBeNull();
    expect(within(panel).queryByLabelText("KIS API Secret")).toBeNull();
    expect(within(panel).queryByLabelText("모의투자 계좌번호")).toBeNull();
    expect(within(panel).getByLabelText("실전 계좌번호")).toBeTruthy();
    expect(within(panel).getByLabelText("상품 코드")).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: "KIS 설정 저장" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "저장된 설정으로 연결 확인" })).toBeNull();
    expect(within(panel).getByRole("heading", { name: "실전 계좌 설정" })).toBeTruthy();
    expect(within(panel).queryByRole("heading", { name: "실전 주문 잠금 해제" })).toBeNull();
    expect(within(panel).queryByRole("heading", { name: "수동 대조 차단 해제" })).toBeNull();
    expect(panel.querySelector('input[name="confirmationPhrase"]')).toBeNull();
    expect(panel.querySelector('input[name="accountConfirmation"]')).toBeNull();
    expect(panel.querySelector('input[name="manualReconciliationConfirmation"]')).toBeNull();
  });

  it("keeps the live KIS settings form mode-independent in real mode", async () => {
    const realLockedState: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "REAL", isReal: true },
      runtime: { ...fixture.runtime, running: false, status: "REAL LOCKED" },
      notice: {
        title: "REAL_LOCK_NOTICE_SHOULD_NOT_RENDER_IN_ENVIRONMENT_SETTINGS",
        description: "Real orders stay locked.",
        tone: "danger",
      },
    };
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realLockedState),
      runAction: vi.fn(async () => realLockedState),
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelectorAll(".side-nav button").length).toBeGreaterThan(0));
    const settingsButton = [...container.querySelectorAll(".side-nav button")].at(-1) as HTMLButtonElement;
    fireEvent.click(settingsButton);

    await waitFor(() => expect(container.querySelector("#environment-settings")).toBeTruthy());
    const panel = container.querySelector("#environment-settings") as HTMLElement;

    expect(panel.querySelector('input[name="appKey"]')).toBeTruthy();
    expect(panel.querySelector('input[name="appSecret"]')).toBeTruthy();
    expect(panel.querySelector('input[name="accountNo"]')).toBeTruthy();
    expect(panel.querySelector('input[name="productCode"]')).toBeTruthy();
    expect(document.body.textContent).not.toContain("REAL_LOCK_NOTICE_SHOULD_NOT_RENDER_IN_ENVIRONMENT_SETTINGS");
  });

  it("reveals live KIS credential values only while the hint button is held", async () => {
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction: vi.fn(async () => fixture),
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelectorAll(".side-nav button").length).toBeGreaterThan(0));
    const settingsButton = [...container.querySelectorAll(".side-nav button")].at(-1) as HTMLButtonElement;
    fireEvent.click(settingsButton);

    await waitFor(() => expect(container.querySelector("#environment-settings")).toBeTruthy());
    const panel = container.querySelector("#environment-settings") as HTMLElement;
    const appKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const appSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    fireEvent.change(appKey, { target: { value: "visible-live-key" } });
    fireEvent.change(appSecret, { target: { value: "visible-live-secret" } });

    const appKeyHint = within(panel).getByRole("button", { name: "입력값 보기: 실전 API Key" });
    const appSecretHint = within(panel).getByRole("button", { name: "입력값 보기: 실전 API Secret" });
    expect(appKeyHint.textContent?.trim()).toBe("");
    expect(appKeyHint.querySelector(".eye-icon")).toBeTruthy();
    expect(appSecretHint.querySelector(".eye-icon")).toBeTruthy();

    expect(appKey.type).toBe("password");
    expect(appSecret.type).toBe("password");

    fireEvent.mouseDown(appKeyHint);
    expect(appKey.type).toBe("text");
    expect(appKey.value).toBe("visible-live-key");
    expect(appSecret.type).toBe("password");

    fireEvent.mouseUp(appKeyHint);
    expect(appKey.type).toBe("password");

    fireEvent.mouseDown(appSecretHint);
    expect(appSecret.type).toBe("text");
    expect(appSecret.value).toBe("visible-live-secret");
    fireEvent.mouseLeave(appSecretHint);
    expect(appSecret.type).toBe("password");
  });

  it("saves live KIS settings and masks them after the bridge reports saved status", async () => {
    const savedState = withLiveCredentialStatus(
      {
        ...fixture,
        logs: {
          ...fixture.logs,
          system: [
            { timestamp: "09:05:00", level: "success", title: "KIS 실전 조회 설정 저장", message: "저장됨" },
            ...fixture.logs.system,
          ],
        },
      },
      true,
      true,
    );
    const runAction = vi.fn(async () => savedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    const appKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const appSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const accountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const productCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;

    expect(appKey.type).toBe("password");
    expect(appSecret.type).toBe("password");
    expect(accountNo.type).toBe("password");
    expect(productCode.type).toBe("password");

    fireEvent.change(appKey, { target: { value: "live-renderer-key" } });
    fireEvent.change(appSecret, { target: { value: "live-renderer-secret" } });
    fireEvent.change(accountNo, { target: { value: "12345678" } });
    fireEvent.change(productCode, { target: { value: "01" } });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 조회 설정 저장" }));

    await waitFor(() =>
      expect(runAction).toHaveBeenCalledWith("kis-live-credentials", {
        appKey: "live-renderer-key",
        appSecret: "live-renderer-secret",
        accountNo: "12345678",
        productCode: "01",
      }),
    );
    expect(within(panel).getByText("실전 설정 저장됨")).toBeTruthy();
    const maskedAppKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const maskedAppSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const maskedAccountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const maskedProductCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;
    expect(maskedAppKey.value).toBe("**********");
    expect(maskedAppSecret.value).toBe("**********");
    expect(maskedAccountNo.value).toBe("**********");
    expect(maskedProductCode.value).toBe("**********");
    expect(maskedAppKey.disabled).toBe(true);
    expect(maskedAppSecret.disabled).toBe(true);
    expect(maskedAccountNo.disabled).toBe(true);
    expect(maskedProductCode.disabled).toBe(true);
    expect(document.body.textContent).not.toContain("live-renderer-key");
    expect(document.body.textContent).not.toContain("live-renderer-secret");
    expect(document.body.textContent).not.toContain("12345678");
  });

  it("renders saved live KIS settings as disabled masks until re-entry is requested", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const savedState = withLiveCredentialStatus(
      {
        ...fixture,
        logs: {
          ...fixture.logs,
          system: [
            { timestamp: "09:05:00", level: "success", title: "KIS 실전 조회 설정 저장", message: "저장됨" },
            ...fixture.logs.system,
          ],
        },
      },
      true,
      true,
    );
    const runAction = vi.fn(async () => savedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    const appKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const appSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const accountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const productCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;

    expect(appKey.value).toBe("**********");
    expect(appSecret.value).toBe("**********");
    expect(accountNo.value).toBe("**********");
    expect(productCode.value).toBe("**********");
    expect(appKey.disabled).toBe(true);
    expect(appSecret.disabled).toBe(true);
    expect(accountNo.disabled).toBe(true);
    expect(productCode.disabled).toBe(true);
    expect(within(panel).queryByRole("button", { name: "실전 조회 설정 저장" })).toBeNull();

    fireEvent.click(within(panel).getByRole("button", { name: "실전 키 재입력" }));

    const editableAppKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const editableAppSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const editableAccountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const editableProductCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;
    expect(editableAppKey.disabled).toBe(false);
    expect(editableAppSecret.disabled).toBe(false);
    expect(editableAccountNo.disabled).toBe(false);
    expect(editableProductCode.disabled).toBe(false);
    expect(editableAppKey.type).toBe("password");
    expect(editableAppSecret.type).toBe("password");
    expect(editableAccountNo.type).toBe("password");
    expect(editableProductCode.type).toBe("password");
    expect(editableAppKey.value).toBe("");
    expect(editableAppSecret.value).toBe("");
    expect(editableAccountNo.value).toBe("");
    expect(editableProductCode.value).toBe("");

    fireEvent.change(editableAppKey, { target: { value: "replacement-live-key" } });
    fireEvent.change(editableAppSecret, { target: { value: "replacement-live-secret" } });
    fireEvent.change(editableAccountNo, { target: { value: "87654321" } });
    fireEvent.change(editableProductCode, { target: { value: "01" } });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 조회 설정 저장" }));

    await waitFor(() =>
      expect(runAction).toHaveBeenCalledWith("kis-live-credentials", {
        appKey: "replacement-live-key",
        appSecret: "replacement-live-secret",
        accountNo: "87654321",
        productCode: "01",
      }),
    );
    expect(document.body.textContent).not.toContain("replacement-live-key");
    expect(document.body.textContent).not.toContain("replacement-live-secret");
    expect(document.body.textContent).not.toContain("87654321");
  });

  it("keeps live KIS settings editable when a replacement save is rejected", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const rejectedState: DashboardState = {
      ...initial,
      logs: {
        ...initial.logs,
        system: [
          { timestamp: "09:06:00", level: "error", title: "KIS live settings save blocked", message: "runtime is running" },
          ...initial.logs.system,
        ],
      },
    };
    const runAction = vi.fn(async () => rejectedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelectorAll(".side-nav button").length).toBeGreaterThan(0));
    const settingsButton = [...container.querySelectorAll(".side-nav button")].at(-1) as HTMLButtonElement;
    fireEvent.click(settingsButton);

    await waitFor(() => expect(container.querySelector("#environment-settings")).toBeTruthy());
    const panel = container.querySelector("#environment-settings") as HTMLElement;
    fireEvent.click(panel.querySelector(".credential-action-buttons button") as HTMLButtonElement);

    const appKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const appSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const accountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const productCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;
    fireEvent.change(appKey, { target: { value: "blocked-live-key" } });
    fireEvent.change(appSecret, { target: { value: "blocked-live-secret" } });
    fireEvent.change(accountNo, { target: { value: "87654321" } });
    fireEvent.change(productCode, { target: { value: "01" } });
    fireEvent.submit(appKey.closest("form") as HTMLFormElement);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("kis-live-credentials", expect.any(Object)));
    const stillEditableAppKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    expect(stillEditableAppKey.disabled).toBe(false);
    expect(stillEditableAppKey.value).not.toBe("**********");
  });

  it("does not render live order approval controls in environment settings", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const runAction = vi.fn(async () => initial);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelectorAll(".side-nav button").length).toBeGreaterThan(0));
    const settingsButton = [...container.querySelectorAll(".side-nav button")].at(-1) as HTMLButtonElement;
    fireEvent.click(settingsButton);

    await waitFor(() => expect(container.querySelector("#environment-settings")).toBeTruthy());
    const panel = container.querySelector("#environment-settings") as HTMLElement;
    expect(within(panel).queryByRole("heading", { name: "실전 주문 잠금 해제" })).toBeNull();
    expect(panel.querySelector('input[name="confirmationPhrase"]')).toBeNull();
    expect(panel.querySelector('input[name="accountConfirmation"]')).toBeNull();
    expect(within(panel).queryByRole("button", { name: "실전 주문 잠금 해제" })).toBeNull();
    expect(within(panel).queryByRole("button", { name: "실전 주문 세션 승인" })).toBeNull();
    expect(runAction).not.toHaveBeenCalled();
  });

  it("does not surface saved live order approval status in environment settings", async () => {
    const initial: DashboardState = {
      ...withLiveCredentialStatus(fixture, true, true),
      settings: {
        ...withLiveCredentialStatus(fixture, true, true).settings,
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: false,
          riskLimitsOk: false,
          newEntriesAllowed: false,
        },
      },
    };
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction: vi.fn(async () => initial),
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelectorAll(".side-nav button").length).toBeGreaterThan(0));
    const settingsButton = [...container.querySelectorAll(".side-nav button")].at(-1) as HTMLButtonElement;
    fireEvent.click(settingsButton);

    await waitFor(() => expect(container.querySelector("#environment-settings")).toBeTruthy());
    const panel = container.querySelector("#environment-settings") as HTMLElement;
    expect(panel.textContent).not.toContain("현재 세션 재승인 필요");
    expect(panel.textContent).not.toContain("승인값 저장됨");
    expect(within(panel).queryByRole("button", { name: "실전 주문 세션 승인" })).toBeNull();
  });

  it("runs the live account check from saved live KIS settings", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const checkedState: DashboardState = {
      ...initial,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...initial.runtime,
        dataSourceKind: "real-prep",
        dataSource: "KIS live account",
        dataModeLabel: "KIS 실전 계좌",
        dataModeDescription: "실전 계좌를 연결하고 자동매매 시작 시 주문 안전 게이트를 확인합니다.",
      },
    };
    const runAction = vi.fn(async (_action: string) => checkedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 계좌 조회 확인" }));

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("kis-live-check", {}));
  });

  it("shows the real account loading dialog for a settings account check", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const accountRequest = deferred<DashboardState>();
    const runAction = vi.fn(async () => accountRequest.promise);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 계좌 조회 확인" }));

    const dialog = await screen.findByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" });
    expect(within(dialog).getByText("실전 계좌 새로고침 중")).toBeTruthy();
    expect(within(panel).getByRole("button", { name: "실전 계좌 조회 확인" }).hasAttribute("disabled")).toBe(true);

    await act(async () => {
      accountRequest.resolve(initial);
      await accountRequest.promise;
    });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "실전 계좌 정보를 불러오는 중" })).toBeNull();
    });
  });

  it("shows a market-hours popup when the live account check runs outside regular hours", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const checkedState: DashboardState = {
      ...initial,
      actionPopup: {
        title: "장중 시간이 아닙니다",
        message: "실전 계좌 조회는 완료했습니다. 실전 자동매매는 정규장(09:00-15:30 KST)에만 실행됩니다.",
        tone: "warning",
      },
    };
    const runAction = vi.fn(async () => checkedState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 계좌 조회 확인" }));

    const dialog = await screen.findByRole("dialog", { name: "장중 시간이 아닙니다" });
    expect(within(dialog).getByText(/정규장\(09:00-15:30 KST\)/)).toBeTruthy();
  });

  it("runs the live readiness check from saved live KIS settings", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const runAction = vi.fn(async (_action: string) => initial);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 준비도 점검" }));

    await waitFor(() =>
      expect(runAction).toHaveBeenCalledWith("live-readiness-check", {
        refreshScannerSnapshot: true,
      }),
    );
  });

  it("does not render manual reconciliation clear controls in environment settings", async () => {
    const initial = withLiveCredentialStatus(fixture, true, true);
    const runAction = vi.fn(async (_action: string) => initial);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    const { container } = render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    expect(within(panel).queryByRole("heading", { name: "수동 대조 차단 해제" })).toBeNull();
    expect(panel.querySelector('input[name="manualReconciliationConfirmation"]')).toBeNull();
    expect(within(panel).queryByRole("button", { name: "수동 대조 차단 해제" })).toBeNull();
    expect(container.textContent).not.toContain("I_CONFIRMED_LIVE_ACCOUNT_RECONCILED");
    expect(runAction).not.toHaveBeenCalled();
  });

  it("shows bridge action failures even after a newer versioned state was accepted", async () => {
    const initial = withRevision(
      {
        ...fixture,
        positions: [
          {
            symbol: "005930",
            companyName: "삼성전자",
            label: "삼성전자 (005930)",
            side: "롱",
            quantity: 4,
            avgPrice: "10,610원",
            lastPrice: "10,400원",
            unrealizedPnl: "-840원",
            pnlTone: "negative",
          },
        ],
      },
      5,
    );
    const runAction = vi.fn(async () => {
      throw new Error("bridge boom");
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "정리모드" }));

    expect((await screen.findAllByText(/bridge boom/)).length).toBeGreaterThan(0);
    expect(screen.getByText("삼성전자 (005930)")).toBeTruthy();
    expect((screen.getByRole("button", { name: /일시정지/ }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("ignores backend strategy payloads and exposes no strategy actions", async () => {
    const stoppedFixture: DashboardState = {
      ...fixture,
      runtime: { ...fixture.runtime, status: "정지", running: false, cycleLabel: "예약 없음" },
    };
    const runAction = vi.fn(async () => stoppedFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => stoppedFixture),
      runAction,
    });

    const { container } = render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    const controls = container.querySelector(".runtime-mode-actions") as HTMLElement;
    expect(controls).toBeTruthy();
    expect(within(controls).getAllByRole("button")).toHaveLength(3);
    expect(within(controls).queryByRole("button", { name: /AI|추천|전략/ })).toBeNull();
    expect(screen.queryByRole("heading", { name: "투자 전략" })).toBeNull();
    expect(screen.queryByText("균형형 설정입니다.")).toBeNull();
    expect(runAction).not.toHaveBeenCalled();
  });

  it("keeps live KIS settings editable when the bridge save fails", async () => {
    const initial = fixture;
    const runAction = vi.fn(async () => {
      throw new Error("save boom");
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "환경설정" }));
    const panel = await screen.findByRole("region", { name: "환경설정" });
    const appKey = panel.querySelector('input[name="appKey"]') as HTMLInputElement;
    const appSecret = panel.querySelector('input[name="appSecret"]') as HTMLInputElement;
    const accountNo = panel.querySelector('input[name="accountNo"]') as HTMLInputElement;
    const productCode = panel.querySelector('input[name="productCode"]') as HTMLInputElement;

    fireEvent.change(appKey, { target: { value: "live-renderer-key" } });
    fireEvent.change(appSecret, { target: { value: "live-renderer-secret" } });
    fireEvent.change(accountNo, { target: { value: "12345678" } });
    fireEvent.change(productCode, { target: { value: "01" } });
    fireEvent.click(within(panel).getByRole("button", { name: "실전 조회 설정 저장" }));

    expect(await screen.findByText(/save boom/)).toBeTruthy();
    expect(within(panel).getByText("실전 설정 저장 전")).toBeTruthy();
    expect(appKey.value).toBe("live-renderer-key");
    expect(appSecret.value).toBe("live-renderer-secret");
    expect(accountNo.value).toBe("12345678");
    expect(productCode.value).toBe("01");
  });

  it("runs scheduled paper cycles while the runtime is active", async () => {
    const loadState = vi.fn(async () => fixture);
    const runAction = vi.fn(async () => fixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
  });

  it("shows a live countdown until the next scheduled paper cycle", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-06-11T09:00:00.000Z"));
    const loadState = vi.fn(async () => fixture);
    const runAction = vi.fn(async () => fixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    expect(screen.getByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    expect(screen.getAllByText("다음 cycle까지 1초").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledWith("cycle", {});
    expect(screen.getAllByText("다음 cycle까지 5초").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(1000);
    });
    expect(screen.getAllByText("다음 cycle까지 4초").length).toBeGreaterThan(0);
  });

  it("stops stale cycle scheduling when a scheduled cycle returns a bridge failure state", async () => {
    vi.useFakeTimers();
    const failedCycleState: DashboardState = {
      ...fixture,
      bridgeError: true,
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "cycle failed",
        cycleLabel: "?덉빟 ?놁쓬",
      },
      notice: {
        title: "cycle failed",
        description: "bridge request timed out",
        tone: "danger",
      },
      logs: {
        ...fixture.logs,
        system: [{ timestamp: "09:01:01", level: "error", title: "Bridge", message: "bridge request timed out" }],
      },
    };
    const loadState = vi.fn(async () => fixture);
    const runAction = vi.fn(async () => failedCycleState);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(20000);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
  });

  it("runs scheduled live cycles while the real runtime is active", async () => {
    const realLiveFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: true,
        status: "실행 중",
        dataSourceKind: "live",
        dataModeLabel: "KIS 실전 주문",
        dataModeDescription: "실전 주문 runtime입니다. 주문 전 안전 게이트를 확인합니다.",
        safetySummary: "실전 주문 runtime · live preflight 통과 시에만 주문 전송",
      },
    };
    const loadState = vi.fn(async () => realLiveFixture);
    const runAction = vi.fn(async () => realLiveFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    const { container } = render(<App />);
    await act(async () => {});

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    expect(screen.getByText("KIS 실전 주문")).toBeTruthy();
    expect(screen.getByText("실전 주문 runtime입니다. 주문 전 안전 게이트를 확인합니다.")).toBeTruthy();
    expect(container.querySelector(".runtime-mode-card.real")).toBeTruthy();
  });

  describe("service-owned scheduler", () => {
    it("counts down from the service scheduler timing without posting renderer cycles", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-06-11T09:00:00.000Z"));
      const serviceFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: true,
          dataSourceKind: "live",
          schedulerOwner: "service",
          schedulerActive: true,
          schedulerIntervalSeconds: 45,
          schedulerSecondsUntilNextCycle: 45,
          cycleLabel: "Windows 서비스가 다음 cycle 예약",
        },
      };
      const loadState = vi.fn(async () => serviceFixture);
      const runAction = vi.fn(async () => serviceFixture);
      vi.stubGlobal("stockbotBridge", {
        loadState,
        runAction,
      });

      render(<App />);
      await act(async () => {});

      expect(screen.getAllByText("자동매매 cycle 완료 후 대기 45초").length).toBeGreaterThan(0);
      expect(screen.getAllByText("다음 cycle까지 45초").length).toBeGreaterThan(0);

      await act(async () => {
        vi.advanceTimersByTime(1000);
      });

      expect(screen.getAllByText("다음 cycle까지 44초").length).toBeGreaterThan(0);
      expect(runAction).not.toHaveBeenCalledWith("cycle", {});
    });

    it("shows when the service scheduler is executing a cycle without posting renderer cycles", async () => {
      vi.useFakeTimers();
      const serviceFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: true,
          dataSourceKind: "live",
          schedulerOwner: "service",
          schedulerActive: true,
          schedulerIntervalSeconds: 45,
          schedulerSecondsUntilNextCycle: 12,
          schedulerCycleInProgress: true,
          cycleLabel: "Windows 서비스 cycle 실행 중",
        },
      };
      const loadState = vi.fn(async () => serviceFixture);
      const runAction = vi.fn(async () => serviceFixture);
      vi.stubGlobal("stockbotBridge", {
        loadState,
        runAction,
      });

      render(<App />);
      await act(async () => {});

      expect(screen.getAllByText("cycle 실행 중").length).toBeGreaterThan(0);
      expect(screen.queryByText("다음 cycle까지 12초")).toBeNull();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(65000);
      });

      expect(runAction).not.toHaveBeenCalledWith("cycle", {});
    });

    it("keeps the service countdown ticking while the runtime waits outside market hours", async () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date("2026-06-11T21:00:00.000Z"));
      const serviceFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: false,
          status: "장 대기",
          dataSourceKind: "live",
          schedulerOwner: "service",
          schedulerActive: true,
          schedulerIntervalSeconds: 45,
          schedulerSecondsUntilNextCycle: 30,
          cycleLabel: "장외 시장 상태 확인 대기",
        },
      };
      const loadState = vi.fn(async () => serviceFixture);
      const runAction = vi.fn(async () => serviceFixture);
      vi.stubGlobal("stockbotBridge", {
        loadState,
        runAction,
      });

      render(<App />);
      await act(async () => {});

      expect(screen.getAllByText("다음 cycle까지 30초").length).toBeGreaterThan(0);

      await act(async () => {
        vi.advanceTimersByTime(1000);
      });

      expect(screen.getAllByText("다음 cycle까지 29초").length).toBeGreaterThan(0);
      expect(runAction).not.toHaveBeenCalledWith("cycle", {});
    });

    it("does not schedule renderer cycles and keeps the backend cycle label", async () => {
      vi.useFakeTimers();
      const backendCycleLabel = "Windows 서비스가 다음 cycle 예약";
      const serviceFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: true,
          dataSourceKind: "live",
          schedulerOwner: "service",
          schedulerActive: true,
          cycleLabel: backendCycleLabel,
        },
      };
      const loadState = vi.fn(async () => serviceFixture);
      const runAction = vi.fn(async () => serviceFixture);
      vi.stubGlobal("stockbotBridge", {
        loadState,
        runAction,
      });

      render(<App />);
      await act(async () => {});

      expect(screen.getAllByText(backendCycleLabel).length).toBeGreaterThan(0);
      expect(screen.getAllByText("자동매매 cycle 완료 후 대기 15초").length).toBeGreaterThan(0);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(125000);
      });

      expect(runAction).not.toHaveBeenCalledWith("cycle", {});
    });

    it("keeps start disabled and pause enabled while the service scheduler waits outside market hours", async () => {
      const serviceWaitingFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: false,
          status: "장 대기",
          schedulerOwner: "service",
          schedulerActive: true,
          cycleLabel: "장 대기 - 백그라운드 대기",
          dataSourceKind: "live",
        },
      };
      vi.stubGlobal("stockbotBridge", {
        loadState: vi.fn(async () => serviceWaitingFixture),
        runAction: vi.fn(async () => serviceWaitingFixture),
      });

      render(<App />);

      const startButton = await screen.findByRole("button", { name: /^▶ 자동매매 시작$/ });
      const pauseButton = screen.getByRole("button", { name: /일시정지/ });
      expect((startButton as HTMLButtonElement).disabled).toBe(true);
      expect((pauseButton as HTMLButtonElement).disabled).toBe(false);
    });

    it("enables start after the service scheduler has been manually paused", async () => {
      const servicePausedFixture: DashboardState = {
        ...fixture,
        mode: { key: "real", label: "리얼 모드", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: false,
          status: "일시정지",
          schedulerOwner: "service",
          schedulerActive: false,
          cycleLabel: "백그라운드 자동매매 수동 일시정지",
          dataSourceKind: "live",
        },
      };
      vi.stubGlobal("stockbotBridge", {
        loadState: vi.fn(async () => servicePausedFixture),
        runAction: vi.fn(async () => servicePausedFixture),
      });

      render(<App />);

      const startButton = await screen.findByRole("button", { name: /^▶ 자동매매 시작$/ });
      const pauseButton = screen.getByRole("button", { name: /일시정지/ });
      expect((startButton as HTMLButtonElement).disabled).toBe(false);
      expect((pauseButton as HTMLButtonElement).disabled).toBe(true);
    });
  });

  it("sends real mode start requests to the bridge so the backend live gate can decide", async () => {
    const realLockedFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "실전 잠금",
        cycleLabel: "예약 없음",
        dataSourceKind: "real-prep",
        safetySummary: "실전 거래 준비 · 시작 시 안전 게이트 확인",
      },
    };
    realLockedFixture.notice = {
      title: "REAL order locked",
      description: "Live orders are disabled by safety gates.",
      tone: "danger",
      locked: true,
      orderEnabled: false,
    };
    const runAction = vi.fn(async () => realLockedFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realLockedFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect((startButton as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("does not render persistent real start blocker shortcut buttons", async () => {
    const realLockedFixture: DashboardState = {
      ...withLiveCredentialStatus(fixture, true, true),
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "실전 잠금",
        cycleLabel: "예약 없음",
        dataSourceKind: "real-read-only",
        safetySummary: "실전 거래 준비 · 시작 시 안전 게이트 확인",
      },
      notice: {
        title: "REAL order locked",
        description: "Live orders are disabled by safety gates.",
        tone: "danger",
        locked: true,
        orderEnabled: false,
      },
    };
    const runAction = vi.fn(async () => realLockedFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realLockedFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    expect(document.querySelector(".real-start-blockers")).toBeNull();
    expect(screen.queryByRole("button", { name: "실전 주문 승인하기" })).toBeNull();
    expect(screen.queryByRole("button", { name: "실전 계좌 설정하기" })).toBeNull();
    expect(screen.queryByRole("button", { name: "실전 준비도 점검하기" })).toBeNull();
  });

  it("allows approved real mode automation start to reach the bridge live gate", async () => {
    const realReadyFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-read-only",
        safetySummary: "Live orders approved",
      },
      notice: {
        title: "REAL order approved",
        description: "Live order safety gates are approved.",
        tone: "neutral",
        locked: false,
        orderEnabled: true,
        ready: true,
      },
      settings: {
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: true,
          riskLimitsOk: true,
          newEntriesAllowed: true,
        },
      },
    };
    const realRunningFixture: DashboardState = {
      ...realReadyFixture,
      runtime: {
        ...realReadyFixture.runtime,
        running: true,
        status: "Running",
        dataSourceKind: "live",
      },
    };
    const runAction = vi.fn(async (action: string) => (action === "start" ? realRunningFixture : realReadyFixture));
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realReadyFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect((startButton as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("keeps the real start button disabled after the backend reports a running live runtime", async () => {
    const realReadyFixture: DashboardState = withRevision(
      {
        ...fixture,
        mode: { key: "real", label: "Real mode", isReal: true },
        runtime: {
          ...fixture.runtime,
          running: false,
          status: "Ready",
          cycleLabel: "No schedule",
          dataSourceKind: "real-read-only",
          safetySummary: "Live orders approved",
        },
        notice: {
          title: "REAL order approved",
          description: "Live order safety gates are approved.",
          tone: "neutral",
          locked: false,
          orderEnabled: true,
          ready: true,
        },
      },
      1,
    );
    const realRunningFixture: DashboardState = withRevision(
      {
        ...realReadyFixture,
        runtime: {
          ...realReadyFixture.runtime,
          running: true,
          status: "Running",
          dataSourceKind: "live",
        },
      },
      2,
    );
    const startResponse = deferred<DashboardState>();
    const runAction = vi.fn((action: string) =>
      action === "start" ? startResponse.promise : Promise.resolve(realReadyFixture),
    );
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realReadyFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(startButton.disabled).toBe(true));
    await act(async () => {
      startResponse.resolve(realRunningFixture);
      await startResponse.promise;
    });

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
    await waitFor(() => expect(startButton.disabled).toBe(true));
  });

  it("sends stale real start requests to the backend gate instead of disabling the button", async () => {
    const staleReadyFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-read-only",
        safetySummary: "Stale order flag only",
      },
      notice: {
        title: "REAL stale order flag",
        description: "Order enabled without readiness must not start.",
        tone: "neutral",
        locked: false,
        orderEnabled: true,
      },
    };
    const runAction = vi.fn(async () => staleReadyFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => staleReadyFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("sends real start requests without an approval session to the backend gate", async () => {
    const noSessionFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-read-only",
        safetySummary: "Readiness passed without an active live order session",
      },
      notice: {
        title: "REAL readiness passed",
        description: "Readiness alone must not start live orders.",
        tone: "neutral",
        locked: true,
        orderEnabled: false,
        ready: true,
      },
      settings: {
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: false,
          riskLimitsOk: false,
          newEntriesAllowed: false,
        },
      },
    };
    const runAction = vi.fn(async () => noSessionFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => noSessionFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("does not point real start guidance to removed environment approval controls", async () => {
    const realFixture: DashboardState = {
      ...withLiveCredentialStatus(fixture, true, true),
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-read-only",
      },
      notice: {
        title: "REAL readiness pending",
        description: "Readiness still needs a check.",
        tone: "neutral",
        ready: false,
        orderEnabled: false,
      },
    };
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realFixture),
      runAction: vi.fn(async () => realFixture),
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    const title = startButton.getAttribute("title") ?? "";
    expect(title).not.toContain("주문 승인 문구");
    expect(title).not.toContain("계좌 끝 2자리");
    expect(title).not.toContain("실전 준비도 점검");
  });

  it("allows real mode start when live order approval has an active session", async () => {
    const realReadyFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-prep",
        safetySummary: "Live order approval session active",
      },
      notice: {
        title: "REAL order session ready",
        description: "Live order approval session is active.",
        tone: "neutral",
        locked: true,
        orderEnabled: false,
        ready: true,
      },
      settings: {
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: true,
          riskLimitsOk: true,
          newEntriesAllowed: true,
        },
      },
    };
    const realRunningFixture: DashboardState = {
      ...realReadyFixture,
      runtime: {
        ...realReadyFixture.runtime,
        running: true,
        status: "Running",
        dataSourceKind: "live",
      },
    };
    const runAction = vi.fn(async (action: string) => (action === "start" ? realRunningFixture : realReadyFixture));
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realReadyFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("sends real start requests with missing readiness to the backend gate", async () => {
    const realNotReadyFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Locked",
        cycleLabel: "No schedule",
        dataSourceKind: "real-prep",
        safetySummary: "Live order approval session active",
      },
      notice: {
        title: "REAL order locked",
        description: "Live readiness has not passed.",
        tone: "danger",
        locked: true,
        orderEnabled: false,
        ready: false,
      },
      settings: {
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: true,
          riskLimitsOk: true,
          newEntriesAllowed: true,
        },
      },
    };
    const runAction = vi.fn(async () => realNotReadyFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realNotReadyFixture),
      runAction,
    });

    render(<App />);

    await waitFor(() => expect(document.querySelector(".runtime-mode-actions")).toBeTruthy());
    const runtimeActions = document.querySelector(".runtime-mode-actions") as HTMLElement;
    const startButton = within(runtimeActions).getAllByRole("button")[0] as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);

    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });

  it("hides real-ready badges until orders are actually enabled", async () => {
    const realReadyFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "Real mode", isReal: true },
      runtime: {
        ...fixture.runtime,
        running: false,
        status: "Ready",
        cycleLabel: "No schedule",
        dataSourceKind: "real-prep",
        safetySummary: "Live order approval session active",
      },
      notice: {
        title: "REAL order session ready",
        description: "Live order approval session is active.",
        tone: "neutral",
        locked: true,
        orderEnabled: false,
        ready: true,
      },
    };
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realReadyFixture),
      runAction: vi.fn(async () => realReadyFixture),
    });

    const { container } = render(<App />);

    await waitFor(() => expect(document.querySelector(".dashboard-view")).toBeTruthy());
    expect(container.querySelector(".mini-badge.real")).toBeNull();
    expect(container.textContent).not.toContain("REAL 준비");

    const panelBadges = [...container.querySelectorAll(".dashboard-view .panel-badge")].map((badge) => badge.textContent);
    expect(panelBadges).toEqual([]);
    expect(panelBadges).not.toContain("REAL");
  });

  it("stops scheduling paper cycles when cleanup mode finishes and runtime stops", async () => {
    vi.useFakeTimers();
    const stoppedAfterCleanup: DashboardState = {
      ...fixture,
      stateRevision: 1,
      runtime: {
        ...fixture.runtime,
        status: "일시정지",
        running: false,
        cycleLabel: "예약 없음",
        cleanupMode: true,
      },
    };
    const loadState = vi.fn(async () => fixture);
    const runAction = vi.fn(async () => stoppedAfterCleanup);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(runAction).toHaveBeenCalledWith("cycle", {});

    await act(async () => {
      vi.advanceTimersByTime(20000);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(screen.getAllByText("예약 없음").length).toBeGreaterThan(0);
  });

  it("uses a slower scheduled cycle interval for KIS quote-backed paper mode", async () => {
    vi.useFakeTimers();
    const kisFixture: DashboardState = {
      ...fixture,
      runtime: {
        ...fixture.runtime,
        dataSourceKind: "kis-vts",
      },
    };
    const loadState = vi.fn(async () => kisFixture);
    const runAction = vi.fn(async () => kisFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(runAction).toHaveBeenCalledWith("cycle", {});

    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(runAction).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(55000);
    });
    expect(runAction).toHaveBeenCalledTimes(2);
  });

  it("uses the slower scheduled cycle interval for hybrid KIS paper mode", async () => {
    vi.useFakeTimers();
    const hybridFixture: DashboardState = {
      ...fixture,
      runtime: {
        ...fixture.runtime,
        dataSourceKind: "external-scan-kis",
      },
    };
    const loadState = vi.fn(async () => hybridFixture);
    const runAction = vi.fn(async () => hybridFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(runAction).toHaveBeenCalledWith("cycle", {});

    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    expect(runAction).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(55000);
    });
    expect(runAction).toHaveBeenCalledTimes(2);
  });

  it("uses a 15-second scheduled cycle interval for live mode", async () => {
    vi.useFakeTimers();
    const liveFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "由ъ뼹 紐⑤뱶", isReal: true },
      runtime: {
        ...fixture.runtime,
        dataSourceKind: "live",
      },
    };
    const loadState = vi.fn(async () => liveFixture);
    const runAction = vi.fn(async () => liveFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).not.toHaveBeenCalled();

    await act(async () => {
      vi.advanceTimersByTime(14750);
    });
    expect(runAction).toHaveBeenCalledTimes(1);
    expect(runAction).toHaveBeenCalledWith("cycle", {});

    await act(async () => {
      vi.advanceTimersByTime(15000);
    });
    expect(runAction).toHaveBeenCalledTimes(2);
  });

  it("does not poll bridge state while a scheduled cycle is still running", async () => {
    vi.useFakeTimers();
    const cycle = deferred<DashboardState>();
    const loadState = vi.fn(async () => fixture);
    const runAction = vi.fn(async (action: string) => {
      if (action === "cycle") {
        return cycle.promise;
      }
      return fixture;
    });
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction,
    });

    render(<App />);

    await act(async () => {});
    expect(loadState).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(250);
    });
    expect(runAction).toHaveBeenCalledWith("cycle", {});

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });

    expect(loadState).toHaveBeenCalledTimes(1);

    await act(async () => {
      cycle.resolve(fixture);
      await cycle.promise;
    });
    await act(async () => {
      vi.advanceTimersByTime(2500);
    });

    expect(loadState).toHaveBeenCalledTimes(2);
  });

  it("ignores stale cycle responses after a newer runtime action state", async () => {
    const initial = withRevision(fixture, 1);
    const staleCycleState = withRevision(withAccountMetric(fixture, "현금", "500,000원"), 2);
    const latestActionState = withRevision(withAccountMetric(fixture, "현금", "2,000,000원"), 3);
    const cycle = deferred<DashboardState>();
    const runAction = vi.fn(async (action: string) => {
      if (action === "cycle") {
        return cycle.promise;
      }
      if (action === "cleanup-mode") {
        return latestActionState;
      }
      return initial;
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    await waitFor(() => expect(runAction).toHaveBeenCalledWith("cycle", {}));

    fireEvent.click(screen.getByRole("button", { name: "정리모드" }));

    await waitFor(() => expect(screen.getAllByText("2,000,000원").length).toBeGreaterThan(0));
    await act(async () => {
      cycle.resolve(staleCycleState);
      await cycle.promise;
    });

    expect(screen.queryByText("500,000원")).toBeNull();
    expect(screen.getAllByText("2,000,000원").length).toBeGreaterThan(0);
  });

  it("ignores unversioned cycle responses after a versioned runtime action state", async () => {
    const initial = withRevision(fixture, 1);
    const staleCycleState = withoutRevision(withAccountMetric(fixture, "현금", "750,000원"));
    const latestActionState = withRevision(withAccountMetric(fixture, "현금", "2,500,000원"), 3);
    const cycle = deferred<DashboardState>();
    const runAction = vi.fn(async (action: string) => {
      if (action === "cycle") {
        return cycle.promise;
      }
      if (action === "cleanup-mode") {
        return latestActionState;
      }
      return initial;
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);

    expect(await screen.findByRole("heading", { name: "개미親주식 대시보드" })).toBeTruthy();
    await waitFor(() => expect(runAction).toHaveBeenCalledWith("cycle", {}));

    fireEvent.click(screen.getByRole("button", { name: "정리모드" }));

    await waitFor(() => expect(screen.getAllByText("2,500,000원").length).toBeGreaterThan(0));
    await act(async () => {
      cycle.resolve(staleCycleState);
      await cycle.promise;
    });

    expect(screen.queryByText("750,000원")).toBeNull();
    expect(screen.getAllByText("2,500,000원").length).toBeGreaterThan(0);
  });

  it("rebases revisions for a newer bridge generation without accepting an older generation", async () => {
    vi.useFakeTimers();
    const initial: DashboardState = {
      ...withRevision(fixture, 8),
      bridgeGeneration: 1,
      runtime: { ...fixture.runtime, running: false, status: "old-instance" },
    };
    const replacement: DashboardState = {
      ...withRevision(fixture, 0),
      bridgeGeneration: 3,
      runtime: { ...fixture.runtime, running: false, status: "replacement-instance" },
    };
    const staleOldInstance: DashboardState = {
      ...withRevision(fixture, 9),
      bridgeGeneration: 2,
      runtime: { ...fixture.runtime, running: false, status: "stale-old-instance" },
    };
    const loadState = vi
      .fn<() => Promise<DashboardState>>()
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(replacement)
      .mockResolvedValue(staleOldInstance);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction: vi.fn(async () => replacement),
    });

    render(<App />);
    await act(async () => {});
    expect(screen.getAllByText("old-instance").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});
    expect(screen.getAllByText("replacement-instance").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});
    expect(screen.queryByText("stale-old-instance")).toBeNull();
    expect(screen.getAllByText("replacement-instance").length).toBeGreaterThan(0);
  });

  it("does not show an action popup from an older bridge generation", async () => {
    const initial: DashboardState = {
      ...withRevision(fixture, 4),
      bridgeGeneration: 1,
      runtime: { ...fixture.runtime, running: false, status: "initial-generation" },
    };
    const replacement: DashboardState = {
      ...withRevision(fixture, 0),
      bridgeGeneration: 2,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: { ...fixture.runtime, running: false, status: "new-generation" },
    };
    const staleAction = deferred<DashboardState>();
    const runAction = vi.fn(async (action: string) => {
      if (action === "cleanup-mode") {
        return staleAction.promise;
      }
      if (action === "mode") {
        return replacement;
      }
      return initial;
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "정리모드" }));
    fireEvent.click(screen.getByRole("button", { name: /리얼모드/ }));
    await waitFor(() => expect(screen.getAllByText("new-generation").length).toBeGreaterThan(0));

    await act(async () => {
      staleAction.resolve({
        ...initial,
        stateRevision: 99,
        actionPopup: { title: "stale-popup", message: "stale-message", tone: "warning" },
      });
      await staleAction.promise;
    });

    expect(screen.queryByText("stale-popup")).toBeNull();
    expect(screen.getAllByText("new-generation").length).toBeGreaterThan(0);
  });

  it("does not merge a bridge failure from an older bridge generation", async () => {
    const initial: DashboardState = {
      ...withRevision(fixture, 4),
      bridgeGeneration: 1,
      runtime: { ...fixture.runtime, running: false, status: "initial-generation" },
    };
    const replacement: DashboardState = {
      ...withRevision(fixture, 0),
      bridgeGeneration: 2,
      mode: { key: "real", label: "리얼 모드", isReal: true },
      runtime: { ...fixture.runtime, running: false, status: "new-generation" },
    };
    const staleFailure = deferred<DashboardState>();
    const runAction = vi.fn(async (action: string) => {
      if (action === "cleanup-mode") {
        return staleFailure.promise;
      }
      if (action === "mode") {
        return replacement;
      }
      return initial;
    });
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => initial),
      runAction,
    });

    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "정리모드" }));
    fireEvent.click(screen.getByRole("button", { name: /리얼모드/ }));
    await waitFor(() => expect(screen.getAllByText("new-generation").length).toBeGreaterThan(0));

    await act(async () => {
      staleFailure.resolve({
        ...initial,
        bridgeError: true,
        notice: { title: "stale-bridge-failure", description: "stale-message", tone: "danger" },
      });
      await staleFailure.promise;
    });

    expect(screen.queryByText("stale-bridge-failure")).toBeNull();
    expect(screen.getAllByText("new-generation").length).toBeGreaterThan(0);
  });
  it("toggles cleanup mode from the global runtime controls", async () => {
    const cleanupState = withRevision(
      {
        ...fixture,
        runtime: { ...fixture.runtime, cleanupMode: true },
      },
      2,
    );
    const runAction = vi.fn(async () => cleanupState);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => fixture),
      runAction,
    });

    render(<App />);

    const cleanupButton = await screen.findByRole("button", { name: "정리모드" });
    fireEvent.click(cleanupButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("cleanup-mode", { enabled: true }));
    expect(await screen.findByRole("button", { name: "정리모드 중" })).toBeTruthy();
  });

  it("does not dispatch cleanup disable from real mode", async () => {
    const realCleanupFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "由ъ뼹 紐⑤뱶", isReal: true },
      runtime: { ...fixture.runtime, cleanupMode: true },
    };
    const runAction = vi.fn(async () => realCleanupFixture);
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realCleanupFixture),
      runAction,
    });

    render(<App />);

    const cleanupButton = await screen.findByRole("button", { name: "정리모드 중" });
    expect((cleanupButton as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(cleanupButton);

    expect(runAction).not.toHaveBeenCalled();
  });

  it("allows real cleanup-only runtime start without new entry approval", async () => {
    const realCleanupFixture: DashboardState = {
      ...fixture,
      mode: { key: "real", label: "REAL", isReal: true },
      runtime: { ...fixture.runtime, running: false, cleanupMode: true },
      notice: { ...fixture.notice, orderEnabled: false, ready: true },
      settings: {
        ...fixture.settings,
        liveOrderApproval: {
          allowSaved: true,
          enabledSaved: true,
          confirmationSaved: true,
          accountConfirmationSaved: true,
          sessionApproved: true,
          riskLimitsOk: true,
          newEntriesAllowed: false,
        },
      },
    };
    const runAction = vi.fn(async () => ({ ...realCleanupFixture, runtime: { ...realCleanupFixture.runtime, running: true } }));
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => realCleanupFixture),
      runAction,
    });

    const { container } = render(<App />);

    await waitFor(() => expect(container.querySelector(".primary-action")).toBeTruthy());
    const startButton = container.querySelector(".primary-action") as HTMLButtonElement;
    expect(startButton.disabled).toBe(false);
    fireEvent.click(startButton);

    await waitFor(() => expect(runAction).toHaveBeenCalledWith("start", {}));
  });
  it("surfaces bridge failures instead of silently rendering fallback data", async () => {
    vi.stubGlobal("stockbotBridge", {
      loadState: vi.fn(async () => {
        throw new Error("python bridge unavailable");
      }),
    });

    render(<App />);

    expect((await screen.findAllByText(/Electron bridge loadState failed/)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/python bridge unavailable/).length).toBeGreaterThan(0);
  });

  it("renders trade logs as compact order rows with Korean reasons", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[21:23:00] 매수 rejected - 신라섬유 (001000)",
                detail: "19주 / 4,105원 / 결과 rejected / 사유 insufficient_cash / 모드 paper",
                level: "rejected",
              },
              {
                title: "[21:23:00] 매도 filled - 우리로 (046970)",
                detail: "4주 / 16,550원 / 결과 filled / 사유 take_profit / 모드 paper / 실현손익 1,306원",
                level: "sell",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const tradeSection = (await screen.findByRole("heading", { name: "매도/매수 로그" })).closest("section");

    expect(tradeSection).toBeTruthy();
    expect(tradeSection?.id).toBe("trade-logs");
    expect(within(tradeSection as HTMLElement).getAllByText("21:23:00").length).toBe(2);
    expect(within(tradeSection as HTMLElement).getByText("신라섬유 (001000)")).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText("우리로 (046970)")).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/19주 \/ 4,105원 \/ 결과: 거절 \/ 사유: 현금 부족/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/4주 \/ 16,550원 \/ 결과: 체결 \/ 사유: 익절 기준 도달/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/실현손익 1,306원/)).toBeTruthy();
    expect(tradeSection?.textContent).not.toContain("insufficient_cash");
    expect(tradeSection?.textContent).not.toContain("take_profit");
  });

  it("labels insufficient_data trade reasons as warmup instead of a generic failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[09:00:00] 매수 rejected - 테스트 (000001)",
                detail: "1주 / 1원 / 결과 rejected / 사유 insufficient_data / 모드 paper",
                level: "rejected",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const tradeSection = (await screen.findByRole("heading", { name: "매도/매수 로그" })).closest("section");

    expect(within(tradeSection as HTMLElement).getByText(/가격 샘플 부족 - 몇 cycle 더 누적 후 판단/)).toBeTruthy();
    expect(tradeSection?.textContent).not.toContain("insufficient_data");
  });

  it("labels position safety cap rejections with the current strategy wording", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[09:00:00] 매수 rejected - 테스트 (000001)",
                detail: "1주 / 1,000원 / 결과 rejected / 사유 max_position_amount_exceeded / 모드 paper",
                level: "rejected",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const tradeSection = (await screen.findByRole("heading", { name: "매도/매수 로그" })).closest("section");

    expect(within(tradeSection as HTMLElement).getByText(/사유: 종목 안전 상한 초과/)).toBeTruthy();
    expect(tradeSection?.textContent).not.toContain("종목별 최대 비중 초과");
  });

  it("explains trend-boundary sell reasons in Korean instead of raw reason codes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[00:51:26] 매도 filled - 카카오 (035720)",
                detail: "1주 / 40,150원 / 결과 filled / 사유 lower_trend_boundary / 모드 paper / 실현손익 0원",
                level: "sell",
                timestamp: "00:51:26",
                symbol: "035720",
                companyName: "카카오",
                side: "SELL",
                sideLabel: "매도",
                quantity: 1,
                price: 40150,
                priceText: "40,150원",
                reason: "lower_trend_boundary",
                result: "filled",
                mode: "paper",
                realizedPnl: 0,
                realizedPnlText: "0원",
              },
              {
                title: "[00:52:10] 숏 청산 filled - 테스트숏 (123456)",
                detail: "2주 / 12,000원 / 결과 filled / 사유 upper_trend_boundary / 모드 paper / 실현손익 0원",
                level: "short",
                timestamp: "00:52:10",
                symbol: "123456",
                companyName: "테스트숏",
                side: "SHORT_EXIT",
                sideLabel: "숏 청산",
                quantity: 2,
                price: 12000,
                priceText: "12,000원",
                reason: "upper_trend_boundary",
                result: "filled",
                mode: "paper",
                realizedPnl: 0,
                realizedPnlText: "0원",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const tradeSection = (await screen.findByRole("heading", { name: "매도/매수 로그" })).closest("section");

    expect(within(tradeSection as HTMLElement).getByText(/하단 추세 경계선 이탈/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/손실 방어 기준으로 매도/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/상단 추세 경계선 돌파/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getByText(/숏 손실 방어 기준으로 청산/)).toBeTruthy();
    expect(within(tradeSection as HTMLElement).getAllByText(/실현손익 0원/)).toHaveLength(2);
    expect(tradeSection?.textContent).not.toContain("lower_trend_boundary");
    expect(tradeSection?.textContent).not.toContain("upper_trend_boundary");
    expect(tradeSection?.textContent).not.toContain("lower trend boundary");
    expect(tradeSection?.textContent).not.toContain("upper trend boundary");
  });

  it("opens a trade detail chart from the buy/sell log and colors realized return", async () => {
    const tradeState: DashboardState = {
      ...fixture,
      logs: {
        ...fixture.logs,
        trades: [
          {
            title: "[21:23:00] 매도 filled - 우리로 (046970)",
            detail: "4주 / 16,550원 / 결과 filled / 사유 take_profit / 모드 paper / 실현손익 1,306원",
            level: "sell",
            timestamp: "21:23:00",
            symbol: "046970",
            companyName: "우리로",
            side: "SELL",
            sideLabel: "매도",
            quantity: 4,
            price: 16550,
            priceText: "16,550원",
            reason: "take_profit",
            result: "filled",
            mode: "paper",
            realizedPnl: 1306,
            realizedPnlText: "1,306원",
          },
          {
            title: "[21:24:00] 매도 filled - 손실종목 (111111)",
            detail: "2주 / 9,000원 / 결과 filled / 사유 stop_loss / 모드 paper / 실현손익 -1,000원",
            level: "sell",
            timestamp: "21:24:00",
            symbol: "111111",
            companyName: "손실종목",
            side: "SELL",
            sideLabel: "매도",
            quantity: 2,
            price: 9000,
            priceText: "9,000원",
            reason: "stop_loss",
            result: "filled",
            mode: "paper",
            realizedPnl: -1000,
            realizedPnlText: "-1,000원",
          },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => tradeState,
      })),
    );

    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "매도/매수 로그" }));
    const archive = await screen.findByRole("region", { name: "매도/매수 로그 상세" });
    fireEvent.click(within(archive).getByRole("button", { name: /우리로 \(046970\)/ }));

    expect(await within(archive).findByRole("img", { name: "우리로 거래 지점 그래프" })).toBeTruthy();
    expect(within(archive).getByText("매수 지점")).toBeTruthy();
    expect(within(archive).getByText("매도 지점")).toBeTruthy();
    const positiveReturn = within(archive).getByText("+2.01%");
    expect(positiveReturn.className).toContain("positive");

    fireEvent.click(within(archive).getByRole("button", { name: /손실종목 \(111111\)/ }));
    const negativeReturn = await within(archive).findByText("-5.26%");
    expect(negativeReturn.className).toContain("negative");
  });

  it("keeps the selected trade detail stable when newer logs are prepended", async () => {
    vi.useFakeTimers();
    const selectedTrade = {
      title: "[21:23:00] 매도 filled - 우리로 (046970)",
      detail: "4주 / 16,550원 / 결과 filled / 사유 take_profit / 모드 paper / 실현손익 1,306원",
      level: "sell" as const,
      timestamp: "2026-06-15T21:23:00",
      symbol: "046970",
      companyName: "우리로",
      side: "SELL",
      sideLabel: "매도",
      quantity: 4,
      price: 16550,
      priceText: "16,550원",
      reason: "take_profit",
      result: "filled",
      mode: "paper",
      realizedPnl: 1306,
      realizedPnlText: "1,306원",
    };
    const newerTrade = {
      title: "[21:24:00] 매수 filled - 신규종목 (222222)",
      detail: "1주 / 1,000원 / 결과 filled / 사유 flow_score_80 / 모드 paper",
      level: "buy" as const,
      timestamp: "2026-06-15T21:24:00",
      symbol: "222222",
      companyName: "신규종목",
      side: "BUY",
      sideLabel: "매수",
      quantity: 1,
      price: 1000,
      priceText: "1,000원",
      reason: "flow_score_80",
      result: "filled",
      mode: "paper",
      realizedPnl: 0,
      realizedPnlText: "0원",
    };
    const baseState: DashboardState = {
      ...fixture,
      runtime: { ...fixture.runtime, running: false },
      logs: { ...fixture.logs, trades: [selectedTrade] },
    };
    const prependedState: DashboardState = {
      ...baseState,
      stateRevision: 1,
      logs: { ...fixture.logs, trades: [newerTrade, selectedTrade] },
    };
    const loadState = vi.fn()
      .mockResolvedValueOnce(baseState)
      .mockResolvedValue(prependedState);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      runAction: vi.fn(async () => prependedState),
    });

    render(<App />);
    await act(async () => {});

    fireEvent.click(screen.getByRole("button", { name: "매도/매수 로그" }));
    const archive = screen.getByRole("region", { name: "매도/매수 로그 상세" });
    fireEvent.click(within(archive).getByRole("button", { name: /우리로 \(046970\)/ }));
    expect(within(archive).getByRole("img", { name: "우리로 거래 지점 그래프" })).toBeTruthy();

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});

    expect(within(archive).getByRole("img", { name: "우리로 거래 지점 그래프" })).toBeTruthy();
    expect(within(archive).queryByRole("img", { name: "신규종목 거래 지점 그래프" })).toBeNull();
  });

  it("normalizes bridge ISO trade timestamps to compact log time", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          logs: {
            ...fixture.logs,
            trades: [
              {
                title: "[21:23:00] 매도 filled - 우리로 (046970)",
                detail: "4주 / 16,550원 / 결과 filled / 사유 take_profit / 모드 paper / 실현손익 1,306원",
                level: "sell",
                timestamp: "2026-06-15T21:23:00",
                symbol: "046970",
                companyName: "우리로",
                side: "SELL",
                sideLabel: "매도",
                quantity: 4,
                price: 16550,
                priceText: "16,550원",
                reason: "take_profit",
                result: "filled",
                mode: "paper",
                realizedPnl: 1306,
                realizedPnlText: "1,306원",
              },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const tradeSection = (await screen.findByRole("heading", { name: "매도/매수 로그" })).closest("section");

    expect(within(tradeSection as HTMLElement).getByText("21:23:00")).toBeTruthy();
    expect(tradeSection?.textContent).not.toContain("2026-06-15T21:23:00");
  });

  it("opens profit analysis with the current KST daily account query and source metadata", async () => {
    const { loadProfitReport } = stubProfitBridge(async (query) => profitReportFixture(query));
    const { container } = render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));

    const region = await screen.findByRole("region", { name: "손익 분석" });
    await waitFor(() =>
      expect(loadProfitReport).toHaveBeenCalledWith({
        granularity: "day",
        scope: "account",
        anchor: currentKstDateForTest(),
        timezone: "Asia/Seoul",
      }),
    );
    expect(within(region).getByRole("heading", { name: "손익 분석" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "손익 분석" }).getAttribute("aria-current")).toBe("page");
    expect(within(region).getAllByText("KIS 보고 실현손익").length).toBeGreaterThan(0);
    expect(within(region).getAllByText("수수료").length).toBeGreaterThan(0);
    expect(within(region).getAllByText("제세금").length).toBeGreaterThan(0);
    expect(within(region).getAllByText("대출이자").length).toBeGreaterThan(0);
    expect(within(region).getByText(/비용 포함 여부가 확인되지 않아 다시 차감하지 않습니다/)).toBeTruthy();
    expect(within(region).getByText(/데이터 기준 .*09:29:30 KST/)).toBeTruthy();
    expect(within(region).getByText(/KIS 기간별손익/)).toBeTruthy();
    expect(container.querySelector(".side-nav button.active")?.textContent).toBe("손익 분석");
  });

  it("refreshes the visible profit report when the accepted dashboard revision advances", async () => {
    vi.useFakeTimers();
    const initialState: DashboardState = {
      ...fixture,
      stateRevision: 1,
      bridgeGeneration: 1,
      runtime: { ...fixture.runtime, running: false },
    };
    const advancedState: DashboardState = {
      ...initialState,
      stateRevision: 2,
    };
    const loadState = vi.fn()
      .mockResolvedValueOnce(initialState)
      .mockResolvedValue(advancedState);
    const loadProfitReport = vi.fn(async (query: ProfitReportQuery) =>
      profitReportFixture(query, {
        label: loadProfitReport.mock.calls.length === 1 ? "첫 손익" : "갱신 손익",
      }),
    );
    vi.stubGlobal("stockbotBridge", {
      loadState,
      loadProfitReport,
      runAction: vi.fn(async () => advancedState),
    });

    render(<App />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "손익 분석" }));
    await act(async () => {});
    expect(screen.getAllByText("첫 손익").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});

    expect(loadProfitReport).toHaveBeenCalledTimes(2);
    expect(screen.getAllByText("갱신 손익").length).toBeGreaterThan(0);
  });

  it("moves a current-period profit view to the next KST date after midnight", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-29T14:59:50Z"));
    const { loadProfitReport } = stubProfitBridge(async (query) => profitReportFixture(query));
    render(<App />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "손익 분석" }));
    await act(async () => {});
    expect(loadProfitReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ anchor: "2026-07-29" }),
    );

    vi.setSystemTime(new Date("2026-07-29T15:00:01Z"));
    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});

    expect(loadProfitReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ anchor: "2026-07-30" }),
    );
  });

  it("changes profit granularity and scope, then navigates with report anchors", async () => {
    const { loadProfitReport } = stubProfitBridge(async (query) =>
      profitReportFixture(query, {
        label: `${query.granularity}-${query.scope}`,
        previousAnchor: "2026-06-01",
        nextAnchor: null,
      }),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));
    await waitFor(() => expect(loadProfitReport).toHaveBeenCalledTimes(1));

    for (const [label, granularity] of [
      ["시간별", "hour"],
      ["월별", "month"],
      ["연도별", "year"],
      ["일별", "day"],
    ] as const) {
      fireEvent.click(screen.getByRole("button", { name: label }));
      await waitFor(() =>
        expect(loadProfitReport).toHaveBeenLastCalledWith(
          expect.objectContaining({ granularity }),
        ),
      );
    }

    fireEvent.click(screen.getByRole("button", { name: "자동매매" }));
    await waitFor(() =>
      expect(loadProfitReport).toHaveBeenLastCalledWith(
        expect.objectContaining({ granularity: "day", scope: "stockbot" }),
      ),
    );
    expect((await screen.findAllByText("StockBot 기록 실현손익")).length).toBeGreaterThan(0);
    expect(screen.getByText(/StockBot 체결 원장/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "이전 기간" }));
    await waitFor(() =>
      expect(loadProfitReport).toHaveBeenLastCalledWith(
        expect.objectContaining({ anchor: "2026-06-01", scope: "stockbot" }),
      ),
    );
    expect(screen.getByRole("button", { name: "다음 기간" }).hasAttribute("disabled")).toBe(true);
  });

  it("resumes following KST today after navigating back to the current month", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-07-29T03:00:00Z"));
    const { loadProfitReport } = stubProfitBridge(async (query) =>
      profitReportFixture(query, {
        previousAnchor: query.anchor.startsWith("2026-07") ? "2026-06-01" : "2026-05-01",
        nextAnchor: query.anchor.startsWith("2026-06") ? "2026-07-01" : null,
      }),
    );
    render(<App />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "손익 분석" }));
    await act(async () => {});

    fireEvent.click(screen.getByRole("button", { name: "이전 기간" }));
    await act(async () => {});
    expect(loadProfitReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ anchor: "2026-06-01" }),
    );

    fireEvent.click(screen.getByRole("button", { name: "다음 기간" }));
    await act(async () => {});
    expect(loadProfitReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ anchor: "2026-07-29" }),
    );

    vi.setSystemTime(new Date("2026-07-31T15:00:01Z"));
    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});

    expect(loadProfitReport).toHaveBeenLastCalledWith(
      expect.objectContaining({ anchor: "2026-08-01" }),
    );
  });

  it("keeps the latest profit query when an older response arrives late", async () => {
    const first = deferred<ProfitReportResult>();
    const { loadProfitReport } = stubProfitBridge(
      vi.fn()
        .mockReturnValueOnce(first.promise)
        .mockImplementation(async (query: ProfitReportQuery) =>
          profitReportFixture(query, { label: "시간별 최신", value: 22000 }),
        ),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));
    await waitFor(() => expect(loadProfitReport).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "시간별" }));
    expect((await screen.findAllByText("시간별 최신")).length).toBeGreaterThan(0);

    await act(async () => {
      first.resolve(
        profitReportFixture(
          {
            granularity: "day",
            scope: "account",
            anchor: currentKstDateForTest(),
            timezone: "Asia/Seoul",
          },
          { label: "늦은 일별", value: 11000 },
        ),
      );
    });

    expect(screen.getAllByText("시간별 최신").length).toBeGreaterThan(0);
    expect(screen.queryAllByText("늦은 일별")).toHaveLength(0);
  });

  it("shows profit-specific loading and error states and retries without blocking dashboard actions", async () => {
    const first = deferred<ProfitReportResult>();
    stubProfitBridge(
      vi.fn()
        .mockReturnValueOnce(first.promise)
        .mockImplementation(async (query: ProfitReportQuery) =>
          profitReportFixture(query, { label: "재시도 성공" }),
        ),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));

    expect(await screen.findByRole("status", { name: "손익 분석 조회 중" })).toBeTruthy();
    await act(async () => {
      first.resolve({
        profitReportTransportError: true,
        bridgeGeneration: 1,
        message: "기간 손익 저장소를 읽지 못했습니다.",
      });
    });

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/기간 손익 저장소를 읽지 못했습니다/)).toBeTruthy();
    fireEvent.click(within(alert).getByRole("button", { name: "다시 조회" }));
    expect((await screen.findAllByText("재시도 성공")).length).toBeGreaterThan(0);
  });

  it("renders positive, negative, null, and every profit bucket status without converting missing values to zero", async () => {
    const buckets: ProfitBucket[] = [
      {
        key: "confirmed",
        label: "수익일",
        startAt: "2026-07-01T00:00:00+09:00",
        endAt: "2026-07-01T23:59:59+09:00",
        reportedRealizedPnlKrw: 12000,
        feeKrw: 300,
        taxKrw: 200,
        interestKrw: 0,
        fillCount: 2,
        status: "confirmed",
        activityStatus: "trade",
        costInclusion: "unknown",
        issues: [],
      },
      {
        key: "provisional",
        label: "손실일",
        startAt: "2026-07-02T00:00:00+09:00",
        endAt: "2026-07-02T23:59:59+09:00",
        reportedRealizedPnlKrw: -6000,
        feeKrw: 200,
        taxKrw: 100,
        interestKrw: 0,
        fillCount: 1,
        status: "provisional",
        activityStatus: "trade",
        costInclusion: "unknown",
        issues: [],
      },
      ...(["no_trade", "market_closed", "partial", "unavailable"] as const).map((status, index) => ({
        key: status,
        label: ["무거래일", "휴장일", "일부 누락일", "조회 불가일"][index],
        startAt: `2026-07-0${index + 3}T00:00:00+09:00`,
        endAt: `2026-07-0${index + 3}T23:59:59+09:00`,
        reportedRealizedPnlKrw: status === "no_trade" ? 0 : null,
        feeKrw: status === "no_trade" ? 0 : null,
        taxKrw: status === "no_trade" ? 0 : null,
        interestKrw: status === "no_trade" ? 0 : null,
        fillCount: status === "no_trade" ? 0 : null,
        status,
        activityStatus: status === "no_trade" ? "no_trade" as const : "unknown" as const,
        costInclusion: "unknown" as const,
        issues: status === "partial" ? ["일부 체결 자료 누락"] : [],
      })),
    ];
    const { loadProfitReport } = stubProfitBridge(async (query) => {
      const report = profitReportFixture(query, { buckets, status: "partial", value: null });
      return {
        ...report,
        summary: {
          ...report.summary,
          reportedRealizedPnlKrw: null,
          tradingCostKrw: null,
          availableBucketCount: 3,
        },
      };
    });
    const { container } = render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));
    await waitFor(() => expect(loadProfitReport).toHaveBeenCalledTimes(1));

    const chart = await screen.findByRole("img", { name: "기간별 보고 실현손익 그래프" });
    expect(chart.querySelectorAll(".profit-bar.positive")).toHaveLength(1);
    expect(chart.querySelectorAll(".profit-bar.negative")).toHaveLength(1);
    expect(chart.querySelectorAll(".profit-missing-marker")).toHaveLength(3);
    expect(chart.querySelector(".profit-zero-line")).toBeTruthy();
    for (const statusLabel of ["확정", "잠정", "거래 없음", "휴장", "일부 누락", "조회 불가"]) {
      expect(screen.getAllByText(statusLabel).length).toBeGreaterThan(0);
    }
    const unavailableRow = screen.getByRole("row", { name: /조회 불가일/ });
    expect(within(unavailableRow).getAllByText("-").length).toBeGreaterThan(0);
    expect(container.querySelector(".profit-summary-value")?.textContent).toBe("-");
  });

  it("distinguishes a provisional no-trade zero from a provisional traded zero", async () => {
    const baseBucket: ProfitBucket = {
      key: "no-trade-zero",
      label: "무거래 0원",
      startAt: "2026-07-29T00:00:00+09:00",
      endAt: "2026-07-30T00:00:00+09:00",
      reportedRealizedPnlKrw: 0,
      feeKrw: 0,
      taxKrw: 0,
      interestKrw: 0,
      fillCount: 0,
      status: "provisional",
      activityStatus: "no_trade",
      costInclusion: "unknown",
      issues: [],
    };
    stubProfitBridge(async (query) =>
      profitReportFixture(query, {
        buckets: [
          baseBucket,
          {
            ...baseBucket,
            key: "traded-zero",
            label: "체결 0원",
            fillCount: 2,
            activityStatus: "trade",
          },
        ],
        value: 0,
      }),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));

    expect(await screen.findByText("잠정 · 거래 없음")).toBeTruthy();
    expect(screen.getByText("잠정 · 거래 있음")).toBeTruthy();
  });

  it("does not invent one-won axis labels when every profit bucket is unavailable", async () => {
    const unavailableBucket: ProfitBucket = {
      key: "missing",
      label: "자료 없음",
      startAt: "2026-07-01T00:00:00+09:00",
      endAt: "2026-07-01T23:59:59+09:00",
      reportedRealizedPnlKrw: null,
      feeKrw: null,
      taxKrw: null,
      interestKrw: null,
      fillCount: null,
      status: "unavailable",
      activityStatus: "unknown",
      costInclusion: "unknown",
      issues: ["수집되지 않음"],
    };
    stubProfitBridge(async (query) =>
      profitReportFixture(query, {
        buckets: [unavailableBucket],
        status: "unavailable",
        value: null,
      }),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));

    const chart = await screen.findByRole("img", { name: "기간별 보고 실현손익 그래프" });
    expect(chart.textContent).not.toContain("+1원");
    expect(chart.textContent).not.toContain("-1원");
    expect(chart.querySelectorAll(".profit-missing-marker")).toHaveLength(1);
  });

  it("rejects a profit report from a bridge generation newer than the accepted dashboard", async () => {
    stubProfitBridge(async (query) =>
      profitReportFixture(query, { bridgeGeneration: 2 }),
    );
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "손익 분석" }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/현재 브리지 상태와 다른 손익 보고서/)).toBeTruthy();
    expect(screen.queryByRole("img", { name: "기간별 보고 실현손익 그래프" })).toBeNull();
  });

  it("invalidates a displayed profit report when the bridge generation changes", async () => {
    vi.useFakeTimers();
    const generationOne: DashboardState = {
      ...fixture,
      bridgeGeneration: 1,
      runtime: { ...fixture.runtime, running: false },
    };
    const generationTwo: DashboardState = {
      ...generationOne,
      stateRevision: 0,
      bridgeGeneration: 2,
    };
    const loadState = vi.fn()
      .mockResolvedValueOnce(generationOne)
      .mockResolvedValue(generationTwo);
    const refreshed = deferred<ProfitReportResult>();
    const loadProfitReport = vi.fn()
      .mockImplementationOnce(async (query: ProfitReportQuery) =>
        profitReportFixture(query, { label: "이전 브리지", bridgeGeneration: 1 }),
      )
      .mockReturnValueOnce(refreshed.promise);
    vi.stubGlobal("stockbotBridge", {
      loadState,
      loadProfitReport,
      runAction: vi.fn(async () => generationOne),
    });

    render(<App />);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: "손익 분석" }));
    await act(async () => {});
    expect(screen.getAllByText("이전 브리지").length).toBeGreaterThan(0);

    await act(async () => {
      vi.advanceTimersByTime(2500);
    });
    await act(async () => {});

    expect(screen.getByRole("status", { name: "손익 분석 조회 중" })).toBeTruthy();
    expect(screen.queryAllByText("이전 브리지")).toHaveLength(0);
    await act(async () => {
      refreshed.resolve(
        profitReportFixture(
          {
            granularity: "day",
            scope: "account",
            anchor: currentKstDateForTest(),
            timezone: "Asia/Seoul",
          },
          { label: "새 브리지", bridgeGeneration: 2 },
        ),
      );
    });
    expect(screen.getAllByText("새 브리지").length).toBeGreaterThan(0);
  });

  it("keeps profit analysis chart and table in internal scroll regions at the minimum viewport", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const viewRule = css.match(/\.profit-analysis-view\s*\{[^}]+\}/)?.[0] || "";
    const contentRule = css.match(/\.profit-analysis-content\s*\{[^}]+\}/)?.[0] || "";
    const chartRule = css.match(/\.profit-chart-scroll\s*\{[^}]+\}/)?.[0] || "";
    const tableRule = css.match(/\.profit-table-scroll\s*\{[^}]+\}/)?.[0] || "";
    const issueTextRule = css.match(/\.profit-report-issues span\s*\{[^}]+\}/)?.[0] || "";

    expect(viewRule).toContain("overflow: hidden");
    expect(viewRule).toContain("min-height: 0");
    expect(contentRule).toContain("overflow-y: auto");
    expect(contentRule).toContain("min-height: 0");
    expect(chartRule).toContain("overflow-x: auto");
    expect(tableRule).toContain("overflow: auto");
    expect(issueTextRule).toContain("min-width: 0");
    expect(issueTextRule).toContain("overflow-wrap: anywhere");
  });

  it("keeps trade log side tags on one line", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const tradeTagRule = css.match(/\.trade-entry \.log-tag\s*\{[^}]+\}/)?.[0] || "";

    expect(tradeTagRule).toContain("white-space: nowrap");
    expect(tradeTagRule).toContain("min-width: 34px");
  });

  it("keeps the top runtime controls readable after removing the account status card", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const appCss = readFileSync(resolve(__dirname, "App.css"), "utf-8");
    const runtimeStripRule = css.match(/\.runtime-mode-strip\s*\{[^}]+\}/)?.[0] || "";
    const runtimeActionsRule = css.match(/\.runtime-mode-actions\s*\{[^}]+\}/)?.[0] || "";
    const runtimeActionButtonRule = css.match(/\.runtime-mode-actions button\s*\{[^}]+\}/)?.[0] || "";
    const runtimeTooltipRule = css.match(/\.runtime-mode-actions button\[data-tooltip\]::after\s*\{[^}]+\}/)?.[0] || "";
    const compactActionsRule = appCss.match(/\.runtime-mode-actions\.runtime-mode-actions-compact\s*\{[^}]+\}/)?.[0] || "";
    const singleSummaryRule = appCss.match(/\.summary-grid\.summary-grid-single\s*\{[^}]+\}/)?.[0] || "";

    expect(runtimeStripRule).toContain("grid-template-columns: minmax(360px, 1fr) minmax(480px, 1.05fr)");
    expect(runtimeStripRule).toContain("max-width: none");
    expect(runtimeActionsRule).toContain("grid-column: auto");
    expect(runtimeActionsRule).toContain("display: grid");
    expect(runtimeActionsRule).toContain("grid-template-columns: repeat(4, minmax(0, 1fr))");
    expect(runtimeActionsRule).toContain("overflow: visible");
    expect(runtimeActionButtonRule).toContain("white-space: normal");
    expect(runtimeActionButtonRule).toContain("text-align: center");
    expect(runtimeActionButtonRule).toContain("justify-content: center");
    expect(runtimeTooltipRule).toContain("top: calc(100% + 8px)");
    expect(runtimeTooltipRule).not.toContain("bottom: calc(100% + 8px)");
    expect(compactActionsRule).toContain("grid-template-columns: repeat(3, minmax(0, 1fr))");
    expect(singleSummaryRule).toContain("grid-template-columns: minmax(0, 1fr)");
  });

  it("renders credential reveal controls as borderless eye icon buttons", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const revealButtonRule = css.match(/\.credential-reveal-button\s*\{[^}]+\}/)?.[0] || "";
    const eyeIconRule = css.match(/\.credential-reveal-button \.eye-icon\s*\{[^}]+\}/)?.[0] || "";

    expect(revealButtonRule).toContain("border: 0");
    expect(revealButtonRule).toContain("background: transparent");
    expect(revealButtonRule).toContain("border-radius: 999px");
    expect(eyeIconRule).toContain("stroke: currentColor");
  });

  it("uses one dashboard buy/sell log rail and suppresses empty-position divider lines", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const logsPanelRule = css.match(/\.logs-panel\s*\{[^}]+\}/)?.[0] || "";
    const narrowMediaRule = css.match(/@media \(max-width: 1180px\)\s*\{[\s\S]*?\.trade-log-detail-layout/)?.[0] || "";
    const emptyRowRule = css.match(/\.positions-empty-row td\s*\{[^}]+\}/)?.[0] || "";
    const rowHoverRule = css.match(/tbody tr:not\(\.positions-empty-row\):hover,\s*\.selected-row\s*\{[^}]+\}/)?.[0] || "";
    const rowCursorRule = css.match(/tbody tr:not\(\.positions-empty-row\)\s*\{[^}]+\}/)?.[0] || "";

    expect(logsPanelRule).toContain("grid-template-rows: minmax(0, 1fr)");
    expect(logsPanelRule).not.toContain("minmax(0, 1fr) minmax(0, 1fr)");
    expect(narrowMediaRule).not.toContain("grid-template-columns: 1fr 1fr");
    expect(emptyRowRule).toContain("border-bottom: 0");
    expect(emptyRowRule).toContain("background: transparent");
    expect(rowHoverRule).toContain("background: rgb(47 211 238 / 0.12)");
    expect(rowCursorRule).toContain("cursor: pointer");
  });

  it("keeps the buy/sell log archive readable across resized windows", () => {
    const css = readFileSync(resolve(__dirname, "styles.css"), "utf-8");
    const workspaceRule = css.match(/\.trade-log-workspace\s*\{[^}]+\}/)?.[0] || "";
    const detailLayoutRule = css.match(/\.trade-log-detail-layout\s*\{[^}]+\}/)?.[0] || "";
    const archiveScrollRule = css.match(/\.trade-log-archive-scroll\s*\{[^}]+\}/)?.[0] || "";
    const detailCardRule = css.match(/\.trade-log-detail-card\s*\{[^}]+\}/)?.[0] || "";
    const tradeTitleRule = css.match(/\.trade-line strong\s*\{[^}]+\}/)?.[0] || "";
    const tradeDetailRule = css.match(/\.trade-detail\s*\{[^}]+\}/)?.[0] || "";
    const mediumMediaRule = css.match(/@media \(max-width: 1500px\)\s*\{[\s\S]*?\.trade-log-detail-layout\s*\{[^}]+\}/)?.[0] || "";

    expect(workspaceRule).toContain("overflow: hidden");
    expect(detailLayoutRule).toContain("grid-template-columns: minmax(300px, 0.9fr) minmax(360px, 1.1fr)");
    expect(detailLayoutRule).toContain("overflow: hidden");
    expect(archiveScrollRule).toContain("overflow: auto");
    expect(detailCardRule).toContain("overflow: auto");
    expect(tradeTitleRule).toContain("white-space: normal");
    expect(tradeTitleRule).toContain("overflow-wrap: anywhere");
    expect(tradeDetailRule).toContain("overflow-wrap: anywhere");
    expect(mediumMediaRule).toContain("grid-template-columns: minmax(0, 1fr)");
  });

  it("keeps the selected position chart readable when only one price sample exists", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({
          ...fixture,
          selectedPosition: {
            symbol: "100120",
            companyName: "부윅스",
            label: "부윅스 (100120)",
            side: "롱",
            quantity: 2,
            summary: "단일 샘플 포지션",
            avgPrice: "18,737원",
            lastPrice: "18,737원",
            unrealizedPnl: "0원",
            pnlTone: "neutral",
            pricePoints: [{ time: "09:08", value: 18737 }],
            referenceLines: [
              { label: "평균 진입가", value: 18737 },
              { label: "익절선", value: 19300 },
              { label: "손절선", value: 18174 },
            ],
          },
        }),
      })),
    );

    render(<App />);

    const chart = await screen.findByRole("img", { name: "부윅스 가격 흐름" });
    expect(screen.getAllByText("가격 샘플 1개").length).toBeGreaterThan(0);
    expect(screen.getByText(/익절까지/)).toBeTruthy();
    expect(screen.getByText(/손절까지/)).toBeTruthy();
    const path = chart.querySelector(".price-path");
    expect(path?.getAttribute("d")).toContain(" L ");
    const pointXValues = [...chart.querySelectorAll(".price-point")].map((point) => point.getAttribute("cx"));
    expect(new Set(pointXValues).size).toBeGreaterThan(1);
    const referenceList = screen.getByLabelText("차트 기준선");
    expect(within(referenceList).getByText("평균 18,737원")).toBeTruthy();
    expect(chart.textContent).not.toContain("평균 18,737원");
    expect(screen.queryByText("평균 진입가 18,737원")).toBeNull();
    expect(document.querySelector(".chart-decision")).toBeNull();
  });
});
