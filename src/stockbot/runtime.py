from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_FLOOR
import re
from typing import Callable, Iterable, Protocol

from .broker import PaperBroker
from .candidate_selection import CandidateSelector, entry_reference_price
from .execution import ExecutionSettings, estimated_order_price, order_from_signal
from .market_hours import MarketSessionStatus
from .live_order_state import (
    pending_buy_is_safely_scopable,
    pending_order_is_safely_scopable,
)
from .metrics import PaperMetricsTracker
from .models import AccountSnapshot, Fill, MarketBar, Order, Position, Signal
from .risk import RiskConfig, RiskManager
from .scanner import ScannerCandidate, ScannerProvider, ScannerSnapshot
from .symbols import SymbolDirectory


_LIVE_PLANNER_PHASE_ENTRY_RESERVED = "entry_reserved"
_LIVE_PLANNER_PHASE_MONITORING = "monitoring"
_LIVE_PLANNER_PHASE_NOT_STARTED = "not_started"
_LIVE_ZERO_BUYING_POWER_RECHECK_INTERVAL = timedelta(minutes=5)
_LIVE_OPENING_DAY_MAX_BUDGETED_READS = 10
LIVE_ORDER_CYCLE_PAUSE_REASONS = {
    "live_fill_reconciliation_unavailable",
    "live_manual_reconciliation_required",
    "live_order_not_found",
    "live_order_partial",
    "live_order_pending",
    "live_order_reconciliation_failed",
    "live_order_submission_uncertain",
    "live_order_submitted_without_order_no",
    "live_order_unknown",
    "live_pending_order_store_unavailable",
    "live_pending_orders_synced",
    "live_pending_orders_unresolved",
    "live_submission_guard_unavailable",
}
_LIVE_ORDER_FAILURE_LOCK_EXEMPT_REASONS = frozenset(
    {
        "live_audit_log_unavailable",
        "live_buyable_cash",
        "live_buyable_cash_unknown",
        "live_buyable_inquiry_malformed",
        "live_buyable_inquiry_unavailable",
        "live_buyable_quantity",
        "live_daily_loss_limit_reached",
        "live_daily_realized_pnl_unknown",
        "live_entry_count_state_unavailable",
        "live_entry_count_unknown",
        "live_fill_reconciliation_unavailable",
        "live_managed_position_ledger_unavailable",
        "live_market_temporarily_stopped",
        "live_market_state_rejected",
        "live_market_state_unknown",
        "live_manual_reconciliation_required",
        "live_manual_reconciliation_store_unavailable",
        "live_order_not_found",
        "live_order_partial",
        "live_order_pending",
        "live_order_reconciliation_failed",
        "live_order_submission_uncertain",
        "live_order_submitted_without_order_no",
        "live_order_unknown",
        "live_pending_order_store_unavailable",
        "live_pending_order_sync_unavailable",
        "live_pending_orders_synced",
        "live_pending_orders_unresolved",
        "live_position_lifecycle_state_unavailable",
        "live_position_lifecycle_unknown",
        "live_quote_above_daily_upper_limit",
        "live_quote_below_daily_lower_limit",
        "live_submission_guard_unavailable",
    }
)
_LIVE_ORDER_FAILURE_LOCK_EXEMPT_PREFIXES = (
    "live_buyable_inquiry_failed:",
    "live_market_state_rejected:",
    "live_order_reconciliation_failed:",
    "live_preflight_denied:",
    "live_snapshot_failed:",
)


@dataclass(frozen=True)
class CustomStrategySettings:
    order_cash_amount: Decimal
    max_order_amount: Decimal
    max_position_amount: Decimal
    max_symbol_exposure: Decimal
    max_positions: int
    max_daily_entries_per_symbol: int
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    trailing_stop_pct: Decimal
    max_holding_minutes: int
    daily_loss_limit: Decimal
    cash_allocation_pct: Decimal = Decimal("1.0")
    allow_paper_short: bool = False
    kill_switch: bool = False

    @classmethod
    def default(cls) -> "CustomStrategySettings":
        return cls(
            order_cash_amount=Decimal("50000"),
            cash_allocation_pct=Decimal("1.0"),
            max_order_amount=Decimal("0"),
            max_position_amount=Decimal("300000"),
            max_symbol_exposure=Decimal("0.30"),
            max_positions=0,
            max_daily_entries_per_symbol=1,
            stop_loss_pct=Decimal("0.02"),
            take_profit_pct=Decimal("0.03"),
            trailing_stop_pct=Decimal("0.015"),
            max_holding_minutes=0,
            daily_loss_limit=Decimal("100000"),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_cash_amount", Decimal(str(self.order_cash_amount)))
        object.__setattr__(self, "cash_allocation_pct", Decimal(str(self.cash_allocation_pct)))
        object.__setattr__(self, "max_order_amount", Decimal(str(self.max_order_amount)))
        object.__setattr__(self, "max_position_amount", Decimal(str(self.max_position_amount)))
        object.__setattr__(self, "max_symbol_exposure", Decimal(str(self.max_symbol_exposure)))
        object.__setattr__(self, "stop_loss_pct", Decimal(str(self.stop_loss_pct)))
        object.__setattr__(self, "take_profit_pct", Decimal(str(self.take_profit_pct)))
        object.__setattr__(self, "trailing_stop_pct", Decimal(str(self.trailing_stop_pct)))
        object.__setattr__(self, "daily_loss_limit", Decimal(str(self.daily_loss_limit)))
        object.__setattr__(self, "max_positions", int(self.max_positions))
        object.__setattr__(self, "max_daily_entries_per_symbol", int(self.max_daily_entries_per_symbol))
        object.__setattr__(self, "max_holding_minutes", int(self.max_holding_minutes))

        self._validate_bool("allow_paper_short", self.allow_paper_short)
        self._validate_bool("kill_switch", self.kill_switch)
        self._validate_decimal_range("order_cash_amount", self.order_cash_amount, Decimal("10000"), Decimal("1000000"))
        self._validate_decimal_range("cash_allocation_pct", self.cash_allocation_pct, Decimal("0.01"), Decimal("1.0"))
        self._validate_decimal_range("max_order_amount", self.max_order_amount, Decimal("0"), Decimal("1000000"))
        self._validate_decimal_range("max_position_amount", self.max_position_amount, Decimal("10000"), Decimal("10000000"))
        self._validate_decimal_range("max_symbol_exposure", self.max_symbol_exposure, Decimal("0.01"), Decimal("10"))
        self._validate_int_range("max_positions", self.max_positions, 0, 200)
        self._validate_int_range("max_daily_entries_per_symbol", self.max_daily_entries_per_symbol, 1, 50)
        self._validate_decimal_range("stop_loss_pct", self.stop_loss_pct, Decimal("0.002"), Decimal("0.05"))
        self._validate_decimal_range("take_profit_pct", self.take_profit_pct, Decimal("0.002"), Decimal("0.10"))
        self._validate_decimal_range("trailing_stop_pct", self.trailing_stop_pct, Decimal("0"), Decimal("0.05"))
        self._validate_int_range("max_holding_minutes", self.max_holding_minutes, 0, 120)
        if self.daily_loss_limit < Decimal("10000"):
            raise ValueError("daily_loss_limit must be at least 10000")

    def with_updates(self, **updates: object) -> "CustomStrategySettings":
        return replace(self, **updates)

    @staticmethod
    def _validate_decimal_range(name: str, value: Decimal, minimum: Decimal, maximum: Decimal) -> None:
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")

    @staticmethod
    def _validate_int_range(name: str, value: int, minimum: int, maximum: int) -> None:
        if value < minimum or value > maximum:
            raise ValueError(f"{name} must be between {minimum} and {maximum}")

    @staticmethod
    def _validate_bool(name: str, value: object) -> None:
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be boolean")


@dataclass(frozen=True)
class RuntimeEvent:
    kind: str
    timestamp: datetime
    message: str
    symbol: str = ""
    company_name: str = ""
    side: str = ""
    quantity: int = 0
    price: Decimal = Decimal("0")
    reason: str = ""
    result: str = ""
    mode: str = "paper"
    realized_pnl: Decimal = Decimal("0")

    @classmethod
    def trade(
        cls,
        *,
        symbol: str,
        company_name: str = "",
        side: str,
        quantity: int = 0,
        price: Decimal = Decimal("0"),
        reason: str = "",
        result: str = "",
        mode: str = "paper",
        realized_pnl: Decimal = Decimal("0"),
        timestamp: datetime | None = None,
        message: str = "",
    ) -> "RuntimeEvent":
        return cls(
            kind="trade",
            timestamp=timestamp or datetime.now(),
            message=message,
            symbol=symbol,
            company_name=company_name,
            side=side,
            quantity=quantity,
            price=price,
            reason=reason,
            result=result,
            mode=mode,
            realized_pnl=realized_pnl,
        )

    @classmethod
    def system(
        cls,
        message: str,
        *,
        timestamp: datetime | None = None,
        mode: str = "paper",
    ) -> "RuntimeEvent":
        return cls(
            kind="system",
            timestamp=timestamp or datetime.now(),
            message=message,
            mode=mode,
        )


@dataclass(frozen=True)
class RuntimeStatus:
    label: str = "정지"
    running: bool = False


class BarProvider(Protocol):
    def __call__(self, symbol: str) -> MarketBar | None:
        ...


class RateLimiter(Protocol):
    def allow_request(self, kind: str = "query"):
        ...

    def record_request(self, kind: str = "query") -> None:
        ...


class MarketHours(Protocol):
    def status(self) -> MarketSessionStatus:
        ...


class SymbolPriorityProvider(Protocol):
    def __call__(self, symbol: str) -> float:
        ...


class TradingBroker(Protocol):
    def snapshot(self, *, timestamp: datetime | None = None) -> AccountSnapshot:
        ...

    def update_market(self, bar: MarketBar) -> None:
        ...

    def place_order(self, order: Order, bar: MarketBar) -> Fill:
        ...


@dataclass(frozen=True)
class _EntryCandidate:
    signal: Signal
    bar: MarketBar
    priority: float
    sequence: int


class PaperTradingRuntime:
    def __init__(
        self,
        *,
        symbols: Iterable[str],
        broker: TradingBroker,
        strategy,
        risk_manager: RiskManager,
        bar_provider: BarProvider | Callable[[str], MarketBar | None],
        final_quote_provider: BarProvider | Callable[[str], MarketBar | None] | None = None,
        entry_history_provider: Callable[[str], Iterable[MarketBar] | None] | None = None,
        symbol_directory: SymbolDirectory | None = None,
        settings: CustomStrategySettings | None = None,
        rate_limiter: RateLimiter | None = None,
        market_hours: MarketHours | None = None,
        data_source_label: str = "paper",
        data_source_kind: str = "local",
        scan_limit_per_cycle: int | None = None,
        max_bar_requests_per_cycle: int | None = None,
        max_final_quote_requests_per_cycle: int | None = None,
        max_physical_market_reads_per_cycle: int | None = None,
        symbol_priority_provider: SymbolPriorityProvider | Callable[[str], float] | None = None,
        scanner_provider: ScannerProvider | None = None,
        execution_mode: str = "paper",
    ):
        if execution_mode not in {"paper", "live"}:
            raise ValueError("execution_mode must be paper or live")
        self.symbols = list(symbols)
        self.broker = broker
        self.strategy = strategy
        self.risk_manager = risk_manager
        self.bar_provider = bar_provider
        self.final_quote_provider = final_quote_provider
        self.entry_history_provider = entry_history_provider
        self.scanner_provider = scanner_provider
        self._scanner_snapshot = ScannerSnapshot()
        self.symbol_directory = symbol_directory or SymbolDirectory({})
        self.settings = settings or CustomStrategySettings.default()
        self.rate_limiter = rate_limiter
        self.market_hours = market_hours
        self.data_source_label = data_source_label
        self.data_source_kind = data_source_kind
        self.execution_mode = execution_mode
        self.symbol_priority_provider = symbol_priority_provider
        if scan_limit_per_cycle is None or int(scan_limit_per_cycle) <= 0:
            self.scan_limit_per_cycle = None
        else:
            self.scan_limit_per_cycle = max(1, int(scan_limit_per_cycle))
        if max_bar_requests_per_cycle is None or int(max_bar_requests_per_cycle) <= 0:
            self.max_bar_requests_per_cycle = None
        else:
            self.max_bar_requests_per_cycle = max(1, int(max_bar_requests_per_cycle))
        if max_final_quote_requests_per_cycle is None or int(max_final_quote_requests_per_cycle) <= 0:
            self.max_final_quote_requests_per_cycle = None
        else:
            self.max_final_quote_requests_per_cycle = max(1, int(max_final_quote_requests_per_cycle))
        if max_physical_market_reads_per_cycle is None or int(max_physical_market_reads_per_cycle) <= 0:
            self.max_physical_market_reads_per_cycle = None
        else:
            self.max_physical_market_reads_per_cycle = max(1, int(max_physical_market_reads_per_cycle))
        self._final_quote_requests_this_cycle = 0
        self._scan_cursor = 0
        self._scan_cursor_anchor = ""
        self._open_position_monitor_queue: list[str] = []
        self._open_position_monitor_failures: dict[str, int] = {}
        self._next_live_planner_phase = _LIVE_PLANNER_PHASE_ENTRY_RESERVED
        self._cycle_live_planner_phase: str | None = None
        self._last_live_planning_buying_power: Decimal | None = None
        self._last_live_planning_buying_power_at: datetime | None = None
        self._scan_batch_symbols: list[str] = []
        self._bar_request_attempts: dict[str, int] = {}
        self._successful_bar_samples: dict[str, int] = {}
        self._live_history_refresh_buckets: dict[str, int] = {}
        self._live_history_refresh_days: dict[str, str] = {}
        self._live_history_ready_buckets: dict[str, int] = {}
        self._latest_entry_prices: dict[str, Decimal] = {}
        self._latest_short_entry_prices: dict[str, Decimal] = {}
        self._pending_scanner_rank_error: Exception | None = None
        self._ranked_symbols_cache: dict[tuple[str, ...], list[str]] = {}
        self._scanner_price_prime_keys: set[tuple[str, ...]] = set()
        self._cycle_entry_spent = Decimal("0")
        self._cycle_entry_spend_by_symbol: dict[str, Decimal] = {}
        self._cycle_entry_positions: dict[str, Position] = {}
        self._cycle_entry_symbols: set[str] = set()
        self._cycle_entry_slot_target: int | None = None
        self._cycle_entry_slot_capacity: int | None = None
        self._cycle_entry_sizing_slots: int | None = None
        self._cycle_exit_symbols: set[str] = set()
        self._cycle_exit_quantities: dict[str, int] = {}
        self._cycle_prescan_rejection_reasons: dict[str, set[str]] = {}
        self._cycle_history_failure_reasons: dict[str, set[str]] = {}
        self._cycle_start_buying_power: Decimal | None = None
        self._cycle_account_snapshot: AccountSnapshot | None = None
        self._cycle_live_account_read_cost: int | None = None
        self._latest_cycle_account_snapshot: AccountSnapshot | None = None
        # Re-adoption can revive a just-sold quantity from a stale KIS balance snapshot.
        self._live_start_positions_adopted = False
        self._live_existing_positions_snapshot_refresh_needed = False
        self._cycle_paused_for_live_pending_order = False
        self._cycle_new_entries_blocked_for_live_pending_order = False
        self._cycle_new_entries_blocked_for_live_entry_count = False
        self._last_live_entry_count_sync_ready = True
        self._cycle_pending_order_symbols: set[str] = set()
        self._cycle_pending_sell_symbols: set[str] = set()
        self._cycle_blocked_symbols: set[str] = set()
        self._cycle_symbol_trading_block_reasons: dict[str, str] = {}
        self._cycle_market_trading_block_keys: set[tuple[str, str, str]] = set()
        self._last_market_trading_block_reason = ""
        self._last_market_trading_block_market = ""
        self._last_market_trading_block_symbol = ""
        self._last_market_trading_block_source = ""
        self._last_market_trading_block_at: datetime | None = None
        self._market_trading_block_count = 0
        self._last_pending_live_order_sync_summary: dict[str, object] = {
            "outcome": "not_run",
            "remainingCount": 0,
            "entryBlockingCount": 0,
            "isolatedSellCount": 0,
            "fillCount": 0,
            "storeUnavailable": False,
            "syncUnavailable": False,
        }
        self._cycle_physical_entry_capacity_exhausted = False
        self._last_order_failure_class = "none"
        self._last_order_failure_reason = ""
        if hasattr(self.broker, "set_allow_short"):
            self.broker.set_allow_short(self.settings.allow_paper_short)
        self.status = RuntimeStatus()
        self.cycle_count = 0
        self.last_update: datetime | None = None
        self.events: list[RuntimeEvent] = []
        self.metrics_tracker = PaperMetricsTracker()
        self.performance_metrics = self.metrics_tracker.snapshot(self.broker.snapshot())
        self._confirmed_scan_bars_this_cycle: dict[str, MarketBar] = {}

    @property
    def latest_cycle_account_snapshot(self) -> AccountSnapshot | None:
        return self._latest_cycle_account_snapshot

    def prewarm_strategy_history(self, bars_by_symbol: dict[str, list[MarketBar]]) -> None:
        if self.execution_mode == "live":
            return
        seed_history = getattr(self.strategy, "seed_history", None)
        for symbol, bars in bars_by_symbol.items():
            if bars:
                price = entry_reference_price(bars[-1])
                if price is not None:
                    self._latest_entry_prices[symbol] = price
                short_price = entry_reference_price(bars[-1], "SHORT_ENTRY")
                if short_price is not None:
                    self._latest_short_entry_prices[symbol] = short_price
            if not callable(seed_history):
                continue
            seeded_count = int(seed_history(symbol, list(bars)) or 0)
            if seeded_count > 0:
                self._successful_bar_samples[symbol] = max(
                    self._successful_bar_samples.get(symbol, 0),
                    seeded_count,
                )

    def seed_entry_prices(
        self,
        prices_by_symbol: dict[str, Decimal],
        short_prices_by_symbol: dict[str, Decimal] | None = None,
    ) -> None:
        for symbol, price in prices_by_symbol.items():
            parsed_price = Decimal(str(price))
            if parsed_price > 0:
                self._latest_entry_prices[symbol] = parsed_price
        for symbol, price in (short_prices_by_symbol or {}).items():
            parsed_price = Decimal(str(price))
            if parsed_price > 0:
                self._latest_short_entry_prices[symbol] = parsed_price

    def start(self) -> RuntimeEvent:
        if self.execution_mode == "live":
            self._live_start_positions_adopted = False
            self._next_live_planner_phase = _LIVE_PLANNER_PHASE_ENTRY_RESERVED
            self._clear_live_planning_buying_power_observation()
        market_status = self._market_session_status()
        if market_status is not None and not market_status.is_open:
            self.status = RuntimeStatus(label=market_status.label, running=True)
            return self._emit(
                RuntimeEvent.system(
                    f"자동 모의투자 루프 시작 - 데이터 출처: {self.data_source_label}. {market_status.message}"
                )
            )
        self.status = RuntimeStatus(label="실행 중", running=True)
        return self._emit(RuntimeEvent.system(f"자동 모의투자 루프 시작 - 데이터 출처: {self.data_source_label}"))

    def pause(self) -> RuntimeEvent:
        self.status = RuntimeStatus(label="일시정지", running=False)
        return self._emit(RuntimeEvent.system("자동 모의투자 루프 일시정지"))

    def apply_strategy_settings(
        self,
        *,
        settings: CustomStrategySettings,
        strategy_config: object | None = None,
        risk_config: RiskConfig | None = None,
        profile_label: str = "커스텀",
    ) -> RuntimeEvent:
        self.settings = settings
        if strategy_config is not None and hasattr(self.strategy, "config"):
            self.strategy.config = strategy_config
        if risk_config is not None:
            self._sync_live_broker_risk_config(risk_config)
            self.risk_manager.config = risk_config
        reset_order_failures = getattr(self.risk_manager, "reset_order_failures", None)
        if callable(reset_order_failures):
            reset_order_failures()
        if hasattr(self.broker, "set_allow_short"):
            self.broker.set_allow_short(settings.allow_paper_short)
        return self._emit(RuntimeEvent.system(f"전략 설정 적용 - {profile_label}"))

    def _sync_live_broker_risk_config(self, risk_config: RiskConfig) -> None:
        if self.execution_mode != "live":
            return
        broker_config = getattr(self.broker, "config", None)
        if broker_config is None:
            return
        if not hasattr(broker_config, "max_daily_loss") or not hasattr(broker_config, "kill_switch"):
            return
        updates = {
            "max_daily_loss": risk_config.max_daily_loss,
            "kill_switch": risk_config.kill_switch,
        }
        if hasattr(broker_config, "max_order_amount"):
            updates["max_order_amount"] = risk_config.max_order_amount
        if hasattr(broker_config, "max_position_amount"):
            updates["max_position_amount"] = risk_config.max_position_amount
        try:
            self.broker.config = replace(broker_config, **updates)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("live broker risk config synchronization failed") from exc

    def run_cycle(self) -> list[RuntimeEvent]:
        if not self.status.running:
            return []

        self._end_pending_order_batch()
        self._cycle_entry_spent = Decimal("0")
        self._cycle_entry_spend_by_symbol = {}
        self._cycle_entry_positions = {}
        self._cycle_entry_symbols = set()
        self._cycle_entry_slot_target = None
        self._cycle_entry_slot_capacity = None
        self._cycle_entry_sizing_slots = None
        self._cycle_live_planner_phase = None
        self._cycle_exit_symbols = set()
        self._cycle_exit_quantities = {}
        self._cycle_prescan_rejection_reasons = {}
        self._cycle_history_failure_reasons = {}
        self._cycle_account_snapshot = None
        self._cycle_live_account_read_cost = None
        self._live_existing_positions_snapshot_refresh_needed = False
        self._cycle_paused_for_live_pending_order = False
        self._cycle_new_entries_blocked_for_live_pending_order = False
        self._cycle_new_entries_blocked_for_live_entry_count = False
        self._cycle_pending_order_symbols = set()
        self._cycle_pending_sell_symbols = set()
        self._cycle_blocked_symbols = set()
        self._cycle_symbol_trading_block_reasons = {}
        self._cycle_market_trading_block_keys = set()
        self._cycle_physical_entry_capacity_exhausted = False
        self._cycle_planning_buying_power_refreshed = False
        self._cycle_planning_buying_power_ready = None

        market_status = self._market_session_status()
        if market_status is not None and not market_status.is_open:
            self.status = RuntimeStatus(label=market_status.label, running=True)
            cycle_events = [self._emit(RuntimeEvent.system(market_status.message))]
            self._finish_cycle(refresh_metrics=False)
            return cycle_events
        if market_status is not None:
            self.status = RuntimeStatus(label="실행 중", running=True)

        cycle_events: list[RuntimeEvent] = []
        opening_day_read_cost = self._begin_physical_market_read_budget()
        if not self._warm_live_opening_day_gate(
            cycle_events,
            opening_day_read_cost,
        ):
            self._finish_cycle(refresh_metrics=False)
            return cycle_events
        self._last_live_entry_count_sync_ready = self._sync_live_entry_count_state(cycle_events)
        self._cycle_new_entries_blocked_for_live_entry_count = (
            not self._last_live_entry_count_sync_ready
        )
        pre_sync_read_state = self._physical_market_read_budget_state()
        pre_sync_account = self._live_account_snapshot_or_rate_limit_skip(
            cycle_events,
            stage="pre_sync",
        )
        if pre_sync_account is None:
            self._finish_cycle(refresh_metrics=False)
            return cycle_events
        pre_sync_buying_power = Decimal(str(getattr(pre_sync_account, "buying_power", Decimal("0"))))
        if self.execution_mode == "live":
            physical_read_state = self._physical_market_read_budget_state()
            if physical_read_state is not None:
                reads_before_account = pre_sync_read_state[0] if pre_sync_read_state is not None else 0
                account_read_delta = physical_read_state[0] - reads_before_account
                self._cycle_live_account_read_cost = max(
                    2,
                    account_read_delta if account_read_delta > 0 else physical_read_state[0],
                )
            if not self._ensure_live_decision_market_read_budget():
                cycle_events.append(
                    self._emit(
                        RuntimeEvent.system(
                            "live_market_read_budget_extension_failed - cycle skipped"
                        )
                    )
                )
                self._finish_cycle(refresh_metrics=False)
                return cycle_events
        if self.execution_mode == "live" and pre_sync_buying_power > 0:
            self._clear_live_planning_buying_power_observation()
        self._cycle_account_snapshot = pre_sync_account
        self._cycle_start_buying_power = pre_sync_buying_power
        exited_symbols: set[str] = set()
        if self._sync_pending_live_orders(cycle_events, exited_symbols=exited_symbols):
            self._finish_cycle()
            return cycle_events
        if self._adopt_existing_live_positions(cycle_events, account=pre_sync_account):
            self._finish_cycle()
            return cycle_events

        if self._live_existing_positions_snapshot_refresh_needed:
            start_account = self._live_account_snapshot_or_rate_limit_skip(
                cycle_events,
                stage="post_adoption",
            )
            if start_account is None:
                self._finish_cycle(refresh_metrics=False)
                return cycle_events
        else:
            start_account = self._cycle_account_snapshot or pre_sync_account
        self._cycle_account_snapshot = start_account
        self._cycle_start_buying_power = pre_sync_buying_power
        had_positions_at_cycle_start = bool(start_account.positions)
        if self.settings.kill_switch:
            cycle_events.append(self._emit(RuntimeEvent.system("정리 모드 활성화 - 신규 진입 없이 보유 종목 청산 조건만 확인합니다.")))

        rate_limit = self._rate_limit_decision()
        if rate_limit is not None and not rate_limit.allowed and self._rate_limit_blocks_cycle(rate_limit):
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"rate_limit_skip - {rate_limit.reason}: retry_after={rate_limit.retry_after_seconds:.1f}s"
                    )
                )
            )
            self._finish_cycle()
            return cycle_events
        self._record_rate_limited_request()
        self._scanner_snapshot = ScannerSnapshot()
        self._final_quote_requests_this_cycle = 0
        self._confirmed_scan_bars_this_cycle = {}
        self._ranked_symbols_cache = {}
        self._scanner_price_prime_keys = set()
        history_candidate_states: dict[str, str] = {}

        def prioritize_scanner_history(symbols: Iterable[str]) -> list[str]:
            ordered, ready_symbols, fallback_symbols = (
                self._history_prioritized_scanner_symbols(
                    symbols,
                    open_symbols=self._runtime_account_snapshot().positions,
                )
            )
            for symbol in ready_symbols:
                history_candidate_states[symbol] = "ready"
            for symbol in fallback_symbols:
                history_candidate_states[symbol] = "fallback"
            return ordered

        if self._cycle_blocks_new_entries():
            cycle_symbols = self._rotated_open_position_symbols(
                list(self._runtime_account_snapshot().positions)
            )
        else:
            self._refresh_authoritative_symbol_universe()
            self._initialize_cycle_entry_slot_target()
            cycle_symbols = self._symbols_for_cycle()
        main_scan_cursor_after_selection = (self._scan_cursor, self._scan_cursor_anchor)
        candidate_universe_size = len(self.symbols) if self._uses_authoritative_scanner() else len(cycle_symbols)
        if not self._cycle_blocks_new_entries():
            self._emit_pending_scanner_rank_diagnostic(cycle_events)
            self._refresh_scanner_snapshot(cycle_symbols, cycle_events)
            original_cycle_symbols = list(cycle_symbols)
            cycle_symbols = prioritize_scanner_history(cycle_symbols)
            main_scan_reordered = cycle_symbols != original_cycle_symbols
            self._expand_cycle_entry_capacity_for_scanner_history(cycle_symbols)
        else:
            main_scan_reordered = False
        log_individual_holds = len(cycle_symbols) <= 50
        summarized_holds = 0
        hold_reason_counts: Counter[str] = Counter()
        entry_candidates: list[_EntryCandidate] = []
        visited_symbols: set[str] = set()
        processed_symbols: set[str] = set()
        entry_scan_physical_budget_exhausted = False
        entry_scan_quote_budget_exhausted = False
        sequence = 0
        bar_requests_this_cycle = 0
        sparse_scan_candidates_this_cycle = 0
        scan_confirmation_reason_counts: Counter[str] = Counter()
        entry_candidates_queued_this_cycle = 0
        entry_fills_this_cycle = 0
        entry_deferred_this_cycle = 0
        entry_capacity_stop_this_cycle = 0

        def entry_candidate_queue_capacity_reached(symbol: str | None = None) -> bool:
            # External scanner bars are cheap to evaluate and the breadth planner
            # needs the full scanned set before choosing the best feasible subset.
            # Keep the old early stop when KIS history reads would consume the
            # order-preflight lane before queued candidates can be submitted.
            if self._uses_authoritative_scanner() and symbol:
                scanner_bar = self._scanner_snapshot.bars.get(symbol)
                if self._scanner_history_is_ready_for_bar(symbol, scanner_bar):
                    return False
            if self._cycle_entry_slot_capacity is None:
                return False
            remaining_capacity = max(
                0,
                self._cycle_entry_slot_capacity - len(self._cycle_entry_symbols),
            )
            return len(entry_candidates) >= remaining_capacity

        def record_known_deferred_prescan_rejections() -> None:
            account_positions = self._runtime_account_snapshot().positions
            for symbol in self._entry_scan_order(self.symbols):
                if symbol in visited_symbols or symbol in account_positions:
                    continue
                self._known_entry_unavailable_after_current_scan_prime(symbol)

        def execute_entry_candidates(*, entry_fill_limit: int | None = None) -> None:
            nonlocal entry_scan_physical_budget_exhausted, entry_scan_quote_budget_exhausted
            nonlocal entry_fills_this_cycle, entry_deferred_this_cycle, entry_capacity_stop_this_cycle
            filled_entries_this_call = 0
            if self.scanner_provider is not None:
                entry_candidates.sort(key=lambda item: item.sequence)
            else:
                entry_candidates.sort(key=lambda item: (-item.priority, item.sequence))
            attempt_candidates = [
                candidate
                for candidate in entry_candidates
                if candidate.signal.symbol not in exited_symbols
            ]
            if attempt_candidates and not self._refresh_live_planning_account(
                cycle_events,
                attempt_candidates[0].bar,
            ):
                entry_deferred_this_cycle += len(attempt_candidates)
                entry_candidates.clear()
                return
            attempt_limit = len(attempt_candidates)
            if entry_fill_limit is not None:
                attempt_limit = min(attempt_limit, max(0, entry_fill_limit))
            if self._uses_final_quote_budget_for_entries():
                confirmed_attempts = sum(
                    1
                    for candidate in attempt_candidates
                    if candidate.signal.symbol in self._confirmed_scan_bars_this_cycle
                )
                unconfirmed_attempts = max(0, attempt_limit - confirmed_attempts)
                attempt_limit = confirmed_attempts + min(
                    unconfirmed_attempts,
                    self._remaining_final_quote_capacity(),
                )
            attempt_limit = min(
                attempt_limit,
                self._remaining_entry_slots(self._runtime_account_snapshot()),
            )
            attempt_candidates = self._maximum_distinct_entry_candidates(
                attempt_candidates,
                available_cash=self._remaining_cycle_allocation_cash(
                    self._runtime_account_snapshot()
                ),
                limit=attempt_limit,
            )
            attempt_limit = len(attempt_candidates)
            attempted_entries = 0
            last_attempted_entry_symbol = ""
            for candidate_index, candidate in enumerate(attempt_candidates):
                if self._cycle_blocks_new_entries():
                    break
                if entry_fill_limit is not None and filled_entries_this_call >= entry_fill_limit:
                    entry_capacity_stop_this_cycle += 1
                    break
                if not self._has_entry_capacity():
                    entry_capacity_stop_this_cycle += 1
                    break
                if self._entry_candidate_deferred_by_final_quote_limit(candidate.signal):
                    entry_deferred_this_cycle += 1
                    summarize_hold_reasons("final_quote_limit_reached")
                    continue
                if self._position_limit() <= 0:
                    open_positions = self._position_count_with_cycle_entries(
                        self._runtime_account_snapshot()
                    )
                    remaining_attempts = max(1, attempt_limit - attempted_entries)
                    if self._cycle_entry_slot_capacity is not None:
                        remaining_capacity = max(
                            0,
                            self._cycle_entry_slot_capacity - len(self._cycle_entry_symbols),
                        )
                        remaining_attempts = min(remaining_attempts, remaining_capacity)
                    if remaining_attempts <= 0:
                        entry_capacity_stop_this_cycle += 1
                        break
                    if self._cycle_entry_sizing_slots is None:
                        sizing_slots = remaining_attempts
                    else:
                        sizing_slots = self._entry_sizing_slots_for_candidate(
                            candidate.signal,
                            candidate.bar,
                            self._runtime_account_snapshot(),
                        )
                        if sizing_slots <= 0:
                            summarize_hold_reasons("entry_unaffordable")
                            continue
                    self._cycle_entry_slot_target = open_positions + sizing_slots
                    if self._cycle_entry_sizing_slots is not None:
                        execution_issue = self._entry_candidate_issue(
                            candidate.signal,
                            candidate.bar,
                            self._account_with_cycle_overlays(self._runtime_account_snapshot()),
                        )
                        if execution_issue is not None:
                            summarize_hold_reasons(execution_issue)
                            continue
                event_count_before_execution = len(cycle_events)
                reserved_entry_cash = sum(
                    (
                        entry_reference_price(later.bar, later.signal.side)
                        or Decimal("0")
                    )
                    for later in attempt_candidates[candidate_index + 1 :]
                )
                self._execute_signal_safely(
                    cycle_events,
                    candidate.signal,
                    candidate.bar,
                    reserved_entry_cash=reserved_entry_cash,
                )
                attempted_entries += 1
                last_attempted_entry_symbol = candidate.signal.symbol
                if self._cycle_physical_entry_capacity_exhausted:
                    entry_scan_physical_budget_exhausted = True
                    entry_deferred_this_cycle += max(1, len(attempt_candidates) - attempted_entries + 1)
                    self._move_scan_cursor_to_symbol(
                        self._entry_scan_order(self.symbols),
                        candidate.signal.symbol,
                    )
                    last_attempted_entry_symbol = ""
                    break
                for event in cycle_events[event_count_before_execution:]:
                    if (
                        event.kind == "trade"
                        and event.result == "filled"
                        and event.side in {"BUY", "SHORT_ENTRY"}
                    ):
                        entry_fills_this_cycle += 1
                        filled_entries_this_call += 1
            if last_attempted_entry_symbol and self._uses_authoritative_final_quote_scan_window():
                self._move_scan_cursor_after_symbol(
                    self._entry_scan_order(self.symbols),
                    last_attempted_entry_symbol,
                )
            entry_candidates.clear()
            self._cycle_entry_slot_target = None
            if (
                self._uses_final_quote_budget_for_entries()
                and self._remaining_final_quote_capacity() <= 0
            ):
                record_known_deferred_prescan_rejections()
                entry_scan_quote_budget_exhausted = True

        def summarize_hold_reasons(*reasons: str) -> None:
            nonlocal summarized_holds
            summarized_holds += 1
            for reason in reasons or ("hold",):
                hold_reason_counts[_summary_reason_key(reason)] += 1

        def process_symbol(symbol: str, *, log_hold: bool, summarize_hold: bool = True) -> None:
            nonlocal sequence, summarized_holds, bar_requests_this_cycle
            nonlocal sparse_scan_candidates_this_cycle, entry_candidates_queued_this_cycle
            nonlocal entry_scan_physical_budget_exhausted, entry_scan_quote_budget_exhausted
            if (
                self._cycle_paused_for_live_pending_order
                or (
                    self._cycle_blocks_new_entries()
                    and symbol not in self._runtime_account_snapshot().positions
                )
                or entry_scan_physical_budget_exhausted
                or entry_scan_quote_budget_exhausted
            ):
                return
            if (
                self.execution_mode == "live"
                and symbol not in self._runtime_account_snapshot().positions
            ):
                exact_zero_retry_after = self._live_exact_zero_buying_power_retry_after_seconds()
                if exact_zero_retry_after > 0:
                    reason = "exact_zero_buying_power_cooldown"
                    visited_symbols.add(symbol)
                    self._record_prescan_rejection(symbol, reason)
                    if log_hold:
                        cycle_events.append(
                            self._emit(
                                RuntimeEvent.system(
                                    "scanner_hold_summary - "
                                    f"{self._label_for(symbol)} reasons={reason}, "
                                    f"retry_after_seconds={exact_zero_retry_after:.1f}"
                                )
                            )
                        )
                    elif summarize_hold:
                        summarize_hold_reasons(reason)
                    return
            if self.max_bar_requests_per_cycle is not None and bar_requests_this_cycle >= self.max_bar_requests_per_cycle:
                return
            visited_symbols.add(symbol)
            bar_requests_this_cycle += 1
            self._record_bar_request_attempt(symbol)
            uses_open_position_quote = self._uses_final_quote_for_open_position(symbol)
            try:
                bar = self._bar_for(symbol)
            except Exception as exc:
                if symbol in start_account.positions:
                    self._record_open_position_deferred(symbol)
                if uses_open_position_quote:
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                "scanner_diagnostic - open_position_final_quote_error: "
                                f"{self._label_for(symbol)} {_market_data_error_message(exc)}"
                            )
                        )
                    )
                    return
                cycle_events.append(
                    self._emit(RuntimeEvent.system(f"오류 - {self._label_for(symbol)}: {_market_data_error_message(exc)}"))
                )
                return
            if bar is None:
                if symbol in start_account.positions:
                    self._record_open_position_deferred(symbol)
                if uses_open_position_quote:
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                "scanner_diagnostic - open_position_final_quote_unavailable: "
                                f"{self._label_for(symbol)} deferred until the next cycle"
                            )
                        )
                    )
                    return
                cycle_events.append(self._emit(RuntimeEvent.system(f"관망 - {self._label_for(symbol)}: 데이터 없음")))
                return

            market_trading_issue = self._live_market_trading_issue(bar)
            if market_trading_issue is not None:
                if symbol in start_account.positions:
                    self._record_open_position_deferred(symbol)
                self._emit_market_trading_deferred(
                    cycle_events,
                    bar,
                    market_trading_issue,
                )
                if not log_hold and summarize_hold:
                    summarize_hold_reasons(market_trading_issue)
                return

            self._record_latest_entry_price(symbol, bar)
            account_before_strategy = self._account_with_cycle_overlays(self._runtime_account_snapshot())
            if symbol not in account_before_strategy.positions:
                if not self._refresh_live_planning_account(cycle_events, bar):
                    self._record_prescan_rejection(symbol, "live_buying_power_unavailable")
                    if log_hold:
                        cycle_events.append(
                            self._emit(
                                RuntimeEvent.system(
                                    f"관망 - {self._label_for(symbol)}: 거래 조건 미충족 "
                                    "(direction=hold, confidence=0.00, reasons=live_buying_power_unavailable)"
                                )
                            )
                        )
                    elif summarize_hold:
                        summarize_hold_reasons("live_buying_power_unavailable")
                    return
                account_before_strategy = self._account_with_cycle_overlays(
                    self._runtime_account_snapshot()
                )
            scan_issue = self._entry_scan_issue(symbol, bar, account_before_strategy)
            if scan_issue is not None:
                if log_hold:
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                f"관망 - {self._label_for(symbol)}: 거래 조건 미충족 "
                                f"(direction=hold, confidence=0.00, reasons={scan_issue})"
                            )
                        )
                    )
                elif summarize_hold:
                    summarize_hold_reasons(scan_issue)
                return
            scan_confirmation_reason = self._scan_confirmation_reason(symbol, bar)
            needs_scan_confirmation = scan_confirmation_reason is not None
            if scan_confirmation_reason is not None:
                scan_confirmation_reason_counts[scan_confirmation_reason] += 1
                if self._scanner_bar_is_sparse(bar):
                    sparse_scan_candidates_this_cycle += 1
                if any(
                    candidate.signal.symbol not in self._confirmed_scan_bars_this_cycle
                    for candidate in entry_candidates
                ):
                    execute_entry_candidates()
            scan_confirmation_limit_reached = self._scan_confirmation_limit_reached()
            if needs_scan_confirmation and not scan_confirmation_limit_reached:
                physical_budget_issue = self._live_entry_market_read_budget_issue(
                    symbol,
                    bar,
                    include_final_quote=True,
                )
                if physical_budget_issue is not None:
                    entry_scan_physical_budget_exhausted = True
                    self._move_scan_cursor_to_symbol(authoritative_scan_order, symbol)
                    if log_hold:
                        cycle_events.append(
                            self._emit(
                                RuntimeEvent.system(
                                    "scanner_hold_summary - physical market read budget reached: "
                                    f"{self._label_for(symbol)}"
                                )
                            )
                        )
                    elif summarize_hold:
                        summarize_hold_reasons(physical_budget_issue)
                    return
            confirmed_scan_bar = self._scan_confirmation_bar(symbol, bar, cycle_events)
            if needs_scan_confirmation and confirmed_scan_bar is None:
                reason = self._scan_confirmation_unavailable_reason(scan_confirmation_limit_reached)
                if scan_confirmation_limit_reached:
                    entry_scan_quote_budget_exhausted = True
                    self._move_scan_cursor_to_symbol(authoritative_scan_order, symbol)
                else:
                    self._move_scan_cursor_after_symbol(authoritative_scan_order, symbol)
                if log_hold:
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                f"관망 - {self._label_for(symbol)}: 거래 조건 미충족 "
                                f"(direction=hold, confidence=0.00, reasons={reason})"
                            )
                        )
                    )
                elif summarize_hold:
                    summarize_hold_reasons(reason)
                return
            if confirmed_scan_bar is not None:
                bar = confirmed_scan_bar
                self._confirmed_scan_bars_this_cycle[symbol] = confirmed_scan_bar
                market_trading_issue = self._live_market_trading_issue(
                    bar,
                    require_verified_state=True,
                )
                if market_trading_issue is not None:
                    self._emit_market_trading_deferred(
                        cycle_events,
                        bar,
                        market_trading_issue,
                    )
                    if not log_hold and summarize_hold:
                        summarize_hold_reasons(market_trading_issue)
                    return
                self._record_latest_entry_price(symbol, bar)
                confirmed_scan_issue = self._entry_scan_issue(
                    symbol,
                    bar,
                    self._account_with_cycle_overlays(self._runtime_account_snapshot()),
                )
                if confirmed_scan_issue is not None:
                    if log_hold:
                        cycle_events.append(
                            self._emit(
                                RuntimeEvent.system(
                                    f"관망 - {self._label_for(symbol)}: 거래 조건 미충족 "
                                    f"(direction=hold, confidence=0.00, reasons={confirmed_scan_issue})"
                                )
                            )
                        )
                    elif summarize_hold:
                        summarize_hold_reasons(confirmed_scan_issue)
                    return

            physical_budget_issue = self._live_entry_market_read_budget_issue(
                symbol,
                bar,
            )
            if physical_budget_issue is not None:
                entry_scan_physical_budget_exhausted = True
                self._move_scan_cursor_to_symbol(authoritative_scan_order, symbol)
                self._record_prescan_rejection(symbol, physical_budget_issue)
                if log_hold:
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                "scanner_hold_summary - physical market read budget reached: "
                                f"{self._label_for(symbol)}"
                            )
                        )
                    )
                elif summarize_hold:
                    summarize_hold_reasons(physical_budget_issue)
                return

            self._prewarm_entry_history(symbol, bar)
            try:
                self.broker.update_market(bar)
                account = self._account_with_cycle_overlays(self._runtime_account_snapshot())
                signals = self._strategy_signals_for_bar(bar, account)
                self._record_successful_bar_sample(symbol, bar)
                processed_symbols.add(symbol)
                self._move_scan_cursor_after_symbol(authoritative_scan_order, symbol)
            except Exception:
                if symbol in start_account.positions:
                    self._record_open_position_deferred(symbol)
                cycle_events.append(self._symbol_processing_error(symbol, "전략 처리 실패"))
                return
            if not signals:
                if symbol in start_account.positions:
                    self._record_open_position_processed(symbol)
                if not log_hold:
                    if summarize_hold:
                        summarize_hold_reasons(*self._hold_reason_codes(symbol))
                    return
                hold_detail = self._hold_detail(symbol)
                cycle_events.append(
                    self._emit(
                        RuntimeEvent.system(
                            f"관망 - {self._label_for(symbol)}: 거래 조건 미충족 {hold_detail}"
                        )
                    )
                )
                return

            for signal in signals:
                if signal.symbol in self._cycle_pending_order_symbols:
                    pending_side = "SELL" if signal.symbol in self._cycle_pending_sell_symbols else "BUY"
                    cycle_events.append(
                        self._emit(
                            RuntimeEvent.system(
                                "live_pending_symbol_isolated - "
                                f"symbol={signal.symbol}, pending_side={pending_side}, mutation_skipped=true"
                            )
                        )
                    )
                    continue
                if signal.side in {"BUY", "SHORT_ENTRY"}:
                    if self._cycle_blocks_new_entries():
                        summarize_hold_reasons(
                            "live_entry_count_reconciliation_pending"
                            if self._cycle_new_entries_blocked_for_live_entry_count
                            else "live_pending_buy_blocks_new_entries"
                        )
                        continue
                    entry_issue = self._entry_candidate_issue(
                        signal,
                        bar,
                        self._account_with_cycle_overlays(self._runtime_account_snapshot()),
                        prescan=True,
                    )
                    if entry_issue is not None:
                        if log_hold:
                            cycle_events.append(
                                self._emit(
                                    RuntimeEvent.system(
                                        f"관망 - {self._label_for(signal.symbol)}: 거래 조건 미충족 "
                                        f"(direction=hold, confidence=0.00, reasons={entry_issue})"
                                    )
                                )
                        )
                        elif summarize_hold:
                            summarize_hold_reasons(entry_issue)
                        continue
                    try:
                        priority = self._entry_priority(signal)
                    except Exception:
                        cycle_events.append(self._symbol_processing_error(signal.symbol, "진입 점수 처리 실패"))
                        continue
                    entry_candidates.append(
                        _EntryCandidate(
                            signal=signal,
                            bar=bar,
                            priority=priority,
                            sequence=sequence,
                        )
                    )
                    entry_candidates_queued_this_cycle += 1
                    sequence += 1
                else:
                    exit_budget_issue = self._live_exit_market_read_budget_issue()
                    if exit_budget_issue is not None:
                        self._record_open_position_deferred(signal.symbol)
                        cycle_events.append(
                            self._emit(
                                RuntimeEvent.system(
                                    "live_exit_deferred_physical_read_budget - "
                                    f"symbol={signal.symbol}, {exit_budget_issue}"
                                )
                            )
                        )
                        return
                    event_count_before_execution = len(cycle_events)
                    self._execute_signal_safely(cycle_events, signal, bar)
                    if self._cycle_paused_for_live_pending_order:
                        if signal.symbol in self._cycle_pending_order_symbols:
                            self._record_open_position_processed(signal.symbol)
                        else:
                            self._record_open_position_deferred(signal.symbol)
                        return
                    exit_filled = False
                    for event in cycle_events[event_count_before_execution:]:
                        if (
                            event.kind == "trade"
                            and event.result == "filled"
                            and event.side in {"SELL", "SHORT_EXIT"}
                            and event.symbol == signal.symbol
                        ):
                            exit_filled = True
                            exited_symbols.add(event.symbol)
                    if signal.symbol in self._cycle_pending_order_symbols:
                        continue
                    if not exit_filled:
                        self._record_open_position_deferred(signal.symbol)
                        return

            if symbol in start_account.positions:
                self._record_open_position_processed(symbol)

        authoritative_scan_order = (
            self._entry_scan_order(self.symbols)
            if self._uses_authoritative_final_quote_scan_window()
            and not self._cycle_blocks_new_entries()
            else []
        )
        for symbol in cycle_symbols:
            if self._cycle_paused_for_live_pending_order:
                break
            if symbol not in start_account.positions and entry_candidate_queue_capacity_reached(symbol):
                entry_capacity_stop_this_cycle += 1
                break
            process_symbol(symbol, log_hold=log_individual_holds)
            if entry_scan_physical_budget_exhausted or entry_scan_quote_budget_exhausted:
                break
        else:
            if main_scan_reordered:
                self._scan_cursor, self._scan_cursor_anchor = main_scan_cursor_after_selection

        if not self._cycle_blocks_new_entries():
            execute_entry_candidates()

        top_up_scan_limit = self._top_up_scan_limit(
            had_positions_at_cycle_start=had_positions_at_cycle_start,
            exited_symbols=exited_symbols,
        )
        top_up_entry_fill_limit = self._top_up_entry_fill_limit(
            had_positions_at_cycle_start=had_positions_at_cycle_start,
            exited_symbols=exited_symbols,
        )
        if (
            not self._cycle_blocks_new_entries()
            and not entry_scan_physical_budget_exhausted
            and not entry_scan_quote_budget_exhausted
            and top_up_scan_limit > 0
        ):
            top_up_symbols = self._replacement_scan_symbols(visited_symbols, limit=top_up_scan_limit)
            top_up_cursor_after_selection = (self._scan_cursor, self._scan_cursor_anchor)
            self._refresh_scanner_snapshot(top_up_symbols, cycle_events, merge=True)
            original_top_up_symbols = list(top_up_symbols)
            top_up_symbols = prioritize_scanner_history(top_up_symbols)
            top_up_reordered = top_up_symbols != original_top_up_symbols
            for symbol in top_up_symbols:
                if self._cycle_paused_for_live_pending_order:
                    break
                if entry_candidate_queue_capacity_reached(symbol):
                    entry_capacity_stop_this_cycle += 1
                    break
                process_symbol(symbol, log_hold=False)
            else:
                if (
                    top_up_reordered
                    and not entry_scan_physical_budget_exhausted
                    and not entry_scan_quote_budget_exhausted
                ):
                    self._scan_cursor, self._scan_cursor_anchor = top_up_cursor_after_selection

        if not self._cycle_blocks_new_entries():
            execute_entry_candidates(entry_fill_limit=top_up_entry_fill_limit)

        refill_pass = 0
        while (
            had_positions_at_cycle_start
            and not self._cycle_blocks_new_entries()
            and not entry_scan_physical_budget_exhausted
            and not entry_scan_quote_budget_exhausted
            and not self._account_with_cycle_overlays(self._runtime_account_snapshot()).positions
            and self._has_entry_capacity()
            and refill_pass < self._empty_portfolio_refill_pass_limit()
        ):
            refill_symbols = self._empty_portfolio_refill_symbols(exited_symbols)
            if not refill_symbols:
                break
            refill_cursor_after_selection = (self._scan_cursor, self._scan_cursor_anchor)
            self._refresh_scanner_snapshot(refill_symbols, cycle_events, merge=True)
            original_refill_symbols = list(refill_symbols)
            refill_symbols = prioritize_scanner_history(refill_symbols)
            refill_reordered = refill_symbols != original_refill_symbols
            refill_pass += 1
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"보충 스캔 {refill_pass}회 - 모든 보유 포지션이 청산되어 다음 paper 샘플을 다시 확인합니다."
                    )
                )
            )
            for symbol in refill_symbols:
                if self._cycle_paused_for_live_pending_order:
                    break
                if entry_candidate_queue_capacity_reached(symbol):
                    entry_capacity_stop_this_cycle += 1
                    break
                process_symbol(symbol, log_hold=False, summarize_hold=False)
            else:
                if (
                    refill_reordered
                    and not entry_scan_physical_budget_exhausted
                    and not entry_scan_quote_budget_exhausted
                ):
                    self._scan_cursor, self._scan_cursor_anchor = refill_cursor_after_selection
            if not self._cycle_blocks_new_entries():
                execute_entry_candidates(
                    entry_fill_limit=self._top_up_entry_fill_limit(
                        had_positions_at_cycle_start=had_positions_at_cycle_start,
                        exited_symbols=exited_symbols,
                    )
                )

        if summarized_holds:
            reason_summary = self._hold_summary_detail(hold_reason_counts)
            suffix = f" ({reason_summary})" if reason_summary else ""
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"scanner_hold_summary - {summarized_holds} symbols without entry signal{suffix}"
                    )
                )
            )
        if self._uses_authoritative_scanner():
            cap = self.max_final_quote_requests_per_cycle if self.max_final_quote_requests_per_cycle is not None else "unlimited"
            prescan_rejection_detail = self._diagnostic_reason_counts_detail(
                Counter(
                    reason
                    for reasons in self._cycle_prescan_rejection_reasons.values()
                    for reason in reasons
                )
            )
            hold_reason_detail = self._diagnostic_reason_counts_detail(hold_reason_counts)
            confirmation_reason_detail = self._diagnostic_reason_counts_detail(
                scan_confirmation_reason_counts
            )
            confirmation_candidates = sum(scan_confirmation_reason_counts.values())
            physical_read_detail = self._physical_market_read_diagnostic()
            history_failure_detail = self._diagnostic_reason_counts_detail(
                Counter(
                    reason
                    for reasons in self._cycle_history_failure_reasons.values()
                    for reason in reasons
                )
            )
            history_ready_candidates = sum(
                state == "ready" for state in history_candidate_states.values()
            )
            history_fallback_candidates = sum(
                state == "fallback" for state in history_candidate_states.values()
            )
            exact_zero_retry_after = self._live_exact_zero_buying_power_retry_after_seconds()
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - external_scan_cycle: "
                        f"candidates={candidate_universe_size}, selected={len(cycle_symbols)}, "
                        f"processed={len(processed_symbols)}, "
                        f"history_ready_candidates={history_ready_candidates}, "
                        f"history_fallback_candidates={history_fallback_candidates}, "
                        f"history_failures={history_failure_detail}, "
                        f"sparse_candidates={sparse_scan_candidates_this_cycle}, "
                        f"confirmation_candidates={confirmation_candidates}, "
                        f"confirmation_reasons={confirmation_reason_detail}, "
                        f"final_quotes={self._final_quote_requests_this_cycle}/{cap}, "
                        f"physical_reads={physical_read_detail}, "
                        f"confirmed={len(self._confirmed_scan_bars_this_cycle)}, holds={summarized_holds}, "
                        f"entry_candidates={entry_candidates_queued_this_cycle}, "
                        f"entry_fills={entry_fills_this_cycle}, "
                        f"entry_deferred={entry_deferred_this_cycle}, "
                        f"entry_capacity_stop={entry_capacity_stop_this_cycle}, "
                        f"entry_blocked_by_pending={int(self._cycle_new_entries_blocked_for_live_pending_order)}, "
                        f"entry_blocked_by_entry_count={int(self._cycle_new_entries_blocked_for_live_entry_count)}, "
                        f"planner_phase={self._cycle_live_planner_phase or _LIVE_PLANNER_PHASE_NOT_STARTED}, "
                        f"entry_slot_capacity={self._cycle_entry_slot_capacity}, "
                        f"open_target_slots={self._open_entry_slots_for_practical_target()}, "
                        f"exact_zero_cooldown_active={int(exact_zero_retry_after > 0)}, "
                        f"exact_zero_cooldown_retry_after_seconds={exact_zero_retry_after:.1f}, "
                        f"prescan_rejections={prescan_rejection_detail}, "
                        f"hold_reasons={hold_reason_detail}"
                    )
                )
            )
        if not processed_symbols:
            self._emit_no_scannable_candidates(cycle_events)

        self._finish_cycle()
        return cycle_events

    def _bar_for(self, symbol: str) -> MarketBar | None:
        if self._uses_final_quote_for_open_position(symbol):
            return self._open_position_final_quote(symbol)
        scanner_bar = self._scanner_snapshot.bars.get(symbol)
        if scanner_bar is not None:
            return scanner_bar
        return self.bar_provider(symbol)

    def _uses_final_quote_for_open_position(self, symbol: str) -> bool:
        if not self._uses_authoritative_scanner():
            return False
        if self.final_quote_provider is None:
            return False
        return symbol in self._runtime_account_snapshot().positions

    def _open_position_final_quote(self, symbol: str) -> MarketBar | None:
        bar = self.final_quote_provider(symbol)
        if bar is not None:
            self._confirmed_scan_bars_this_cycle[symbol] = bar
        return bar

    def _refresh_scanner_snapshot(
        self,
        symbols: Iterable[str],
        cycle_events: list[RuntimeEvent],
        *,
        merge: bool = False,
    ) -> None:
        requested_symbols = list(dict.fromkeys(symbols))
        if not requested_symbols:
            return
        if self.scanner_provider is None:
            if not merge:
                self._scanner_snapshot = ScannerSnapshot()
            return
        cached_snapshot = self._cached_authoritative_scanner_snapshot(requested_symbols)
        if cached_snapshot is not None:
            self._scanner_snapshot = (
                self._merge_scanner_snapshots(self._scanner_snapshot, cached_snapshot, requested_symbols)
                if merge
                else cached_snapshot
            )
            return
        try:
            snapshot = self.scanner_provider.snapshot(requested_symbols)
        except Exception as exc:
            if not merge:
                self._scanner_snapshot = ScannerSnapshot()
            self._emit_scanner_diagnostic(cycle_events, exc)
            return

        self._scanner_snapshot = (
            self._merge_scanner_snapshots(self._scanner_snapshot, snapshot, requested_symbols)
            if merge
            else snapshot
        )
        for message in snapshot.diagnostics.messages:
            self._emit_scanner_diagnostic(cycle_events, RuntimeError(message))

    def _merge_scanner_snapshots(
        self,
        base: ScannerSnapshot,
        update: ScannerSnapshot,
        requested_symbols: Iterable[str],
    ) -> ScannerSnapshot:
        requested = set(requested_symbols)
        bars = {
            symbol: bar
            for symbol, bar in base.bars.items()
            if symbol not in requested
        }
        bars.update(update.bars)

        candidates_by_symbol: dict[str, ScannerCandidate] = {
            candidate.symbol: candidate
            for candidate in base.candidates
            if candidate.symbol not in requested
        }
        for candidate in update.candidates:
            candidates_by_symbol[candidate.symbol] = candidate
        histories = {
            symbol: history
            for symbol, history in base.histories.items()
            if symbol not in requested
        }
        histories.update(update.histories)
        return ScannerSnapshot(
            bars=bars,
            candidates=tuple(candidates_by_symbol.values()),
            diagnostics=update.diagnostics,
            histories=histories,
        )

    def _emit_scanner_diagnostic(self, cycle_events: list[RuntimeEvent], exc: Exception) -> None:
        detail = _safe_error_detail(exc) or "scanner unavailable"
        cycle_events.append(self._emit(RuntimeEvent.system(f"scanner_diagnostic - {detail}")))

    def _emit_pending_scanner_rank_diagnostic(self, cycle_events: list[RuntimeEvent]) -> None:
        if self._pending_scanner_rank_error is None:
            return
        self._emit_scanner_diagnostic(cycle_events, self._pending_scanner_rank_error)
        self._pending_scanner_rank_error = None

    def _emit_no_scannable_candidates(self, cycle_events: list[RuntimeEvent]) -> None:
        if self.settings.kill_switch or not self.symbols or not self._skips_known_unaffordable_before_scan():
            return
        known_unaffordable = sum(1 for symbol in self.symbols if self._known_entry_unaffordable(symbol))
        known_unavailable = sum(
            1
            for symbol in self.symbols
            if self._known_entry_unavailable_before_scan(symbol, self._scanner_bar_date(symbol))
        )
        if known_unavailable <= 0:
            return
        budget = self._entry_budget_for_account(self._runtime_account_snapshot()).quantize(Decimal("1"))
        cycle_events.append(
            self._emit(
                RuntimeEvent.system(
                    "scanner_hold_summary - no_scannable_candidates: "
                    f"known_unaffordable={known_unaffordable}/{len(self.symbols)}, "
                    f"known_unavailable={known_unavailable}/{len(self.symbols)}, "
                    f"entry_budget={budget}"
                )
            )
        )

    def _refresh_authoritative_symbol_universe(self) -> None:
        if not self._uses_authoritative_scanner() or self.scanner_provider is None:
            return

        cursor_anchor = self._scan_cursor_anchor
        if not cursor_anchor and self._scan_cursor and self.symbols:
            cursor_anchor = self.symbols[self._scan_cursor % len(self.symbols)]
        try:
            ranked_symbols = self.scanner_provider.rank_symbols([])
        except Exception as exc:
            self.symbols = []
            self._scan_cursor = 0
            self._scan_cursor_anchor = ""
            self._pending_scanner_rank_error = exc
            return

        refreshed = [
            symbol
            for symbol in dict.fromkeys(str(symbol).strip() for symbol in ranked_symbols)
            if symbol
        ]
        self.symbols = refreshed
        if cursor_anchor and cursor_anchor in refreshed:
            self._scan_cursor = 0
            self._scan_cursor_anchor = cursor_anchor
        else:
            self._scan_cursor = 0
            self._scan_cursor_anchor = ""
        self._ranked_symbols_cache[tuple(refreshed)] = list(refreshed)

    def _initialize_cycle_entry_slot_target(self) -> None:
        if self._position_limit() > 0 or not self._uses_authoritative_scanner():
            return
        account = self._runtime_account_snapshot()
        open_positions = self._position_count_with_cycle_entries(account)
        entry_capacity = min(len(self.symbols), self._unlimited_entry_slot_target())
        physical_entry_capacity = self._live_physical_entry_slot_capacity(open_positions)
        if physical_entry_capacity is not None:
            if physical_entry_capacity < entry_capacity:
                self._cycle_entry_sizing_slots = max(0, entry_capacity)
            entry_capacity = min(entry_capacity, physical_entry_capacity)
        if self.max_final_quote_requests_per_cycle is not None:
            entry_capacity = min(
                entry_capacity,
                self._remaining_final_quote_capacity(),
            )
        self._cycle_entry_slot_capacity = max(0, entry_capacity)

    def _live_physical_entry_slot_capacity(
        self,
        open_positions: int,
        *,
        scanner_history_ready: bool = False,
    ) -> int | None:
        budget = self.max_physical_market_reads_per_cycle
        if self.execution_mode != "live" or budget is None:
            return None
        monitored_positions = self._open_position_monitor_limit(open_positions)
        return self._live_physical_entry_capacity_for_monitored_positions(
            monitored_positions,
            scanner_history_ready=scanner_history_ready,
        )

    def _live_physical_entry_capacity_for_monitored_positions(
        self,
        monitored_positions: int,
        *,
        scanner_history_ready: bool = False,
    ) -> int:
        state = self._physical_market_read_budget_state()
        budget = (
            state[1]
            if state is not None
            else self.max_physical_market_reads_per_cycle
        )
        if budget is None:
            return 0
        pre_sync_account_reads = self._observed_live_account_read_cost()
        reads_already_used = state[0] if state is not None else pre_sync_account_reads
        remaining = max(0, budget - reads_already_used)
        remaining -= (
            monitored_positions * self._live_monitor_market_reads_per_position()
        )
        if monitored_positions > 0:
            remaining -= pre_sync_account_reads
        entry_history_reads = int(
            callable(self.entry_history_provider) and not scanner_history_ready
        )
        first_entry_reads = (
            pre_sync_account_reads
            + self._pending_live_opening_day_read_cost()
            + 2
            + entry_history_reads
        )
        additional_entry_reads = pre_sync_account_reads + 2 + entry_history_reads
        if remaining < first_entry_reads:
            return 0
        return 1 + max(0, (remaining - first_entry_reads) // additional_entry_reads)

    def _expand_cycle_entry_capacity_for_scanner_history(
        self,
        symbols: Iterable[str],
    ) -> None:
        if (
            self._position_limit() > 0
            or not self._uses_authoritative_scanner()
            or not callable(self.entry_history_provider)
        ):
            return
        account = self._runtime_account_snapshot()
        ready_entry_symbols = [
            symbol
            for symbol in dict.fromkeys(symbols)
            if symbol not in account.positions
            and self._scanner_history_is_ready_for_bar(
                symbol,
                self._scanner_snapshot.bars.get(symbol),
            )
        ]
        if not ready_entry_symbols:
            return

        open_positions = self._position_count_with_cycle_entries(account)
        expanded_capacity = min(
            len(ready_entry_symbols),
            len(self.symbols),
            self._unlimited_entry_slot_target(),
        )
        physical_capacity = self._live_physical_entry_slot_capacity(
            open_positions,
            scanner_history_ready=True,
        )
        if physical_capacity is not None:
            expanded_capacity = min(expanded_capacity, physical_capacity)
        if self.max_final_quote_requests_per_cycle is not None:
            expanded_capacity = min(
                expanded_capacity,
                self._remaining_final_quote_capacity(),
            )
        self._cycle_entry_slot_capacity = max(
            int(self._cycle_entry_slot_capacity or 0),
            max(0, expanded_capacity),
        )

    def _reserve_live_entry_lane(self, position_count: int) -> bool:
        if not self._live_entry_lane_eligible(position_count):
            return False
        if self._cycle_live_planner_phase is None:
            self._cycle_live_planner_phase = self._next_live_planner_phase
        return self._cycle_live_planner_phase == _LIVE_PLANNER_PHASE_ENTRY_RESERVED

    def _live_monitor_market_reads_per_position(self) -> int:
        return 3 if callable(self.entry_history_provider) else 2

    def _live_entry_lane_eligible(self, position_count: int) -> bool:
        if self.execution_mode != "live" or self.max_physical_market_reads_per_cycle is None:
            return False
        if self.settings.kill_switch or self._cycle_blocks_new_entries():
            return False
        if not self.symbols:
            return False
        configured_limit = self._position_limit()
        if configured_limit > 0 and position_count >= configured_limit:
            return False
        try:
            snapshot_buying_power = Decimal(
                str(self._runtime_account_snapshot().buying_power)
            )
            if snapshot_buying_power > 0:
                self._clear_live_planning_buying_power_observation()
                return True
            # Live balance snapshots may omit orderable cash; the reserved lane
            # lets the broker query the exact amount before any BUY is considered.
            if not callable(getattr(self.broker, "refresh_planning_account", None)):
                return False
            if self._recent_live_exact_zero_buying_power():
                return False
        except Exception:
            return False
        return True

    def _recent_live_exact_zero_buying_power(self) -> bool:
        return self._live_exact_zero_buying_power_retry_after_seconds() > 0

    def _live_exact_zero_buying_power_retry_after_seconds(self) -> float:
        if self._last_live_planning_buying_power != Decimal("0"):
            return 0.0
        observed_at = self._last_live_planning_buying_power_at
        if observed_at is None:
            return 0.0
        elapsed_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - observed_at).total_seconds(),
        )
        return max(
            0.0,
            _LIVE_ZERO_BUYING_POWER_RECHECK_INTERVAL.total_seconds() - elapsed_seconds,
        )

    def _clear_live_planning_buying_power_observation(self) -> None:
        self._last_live_planning_buying_power = None
        self._last_live_planning_buying_power_at = None

    def _advance_live_planner_phase(self) -> None:
        if self._recent_live_exact_zero_buying_power():
            self._next_live_planner_phase = _LIVE_PLANNER_PHASE_ENTRY_RESERVED
            return
        if self._cycle_live_planner_phase is None:
            return
        self._next_live_planner_phase = (
            _LIVE_PLANNER_PHASE_MONITORING
            if self._cycle_live_planner_phase == _LIVE_PLANNER_PHASE_ENTRY_RESERVED
            else _LIVE_PLANNER_PHASE_ENTRY_RESERVED
        )

    def _observed_live_account_read_cost(self) -> int:
        observed = self._cycle_live_account_read_cost
        if observed is not None:
            return max(2, int(observed))
        state = self._physical_market_read_budget_state()
        if state is None:
            return 2
        used, _limit = state
        return max(2, used)

    def _estimated_live_opening_day_read_cost(self) -> int | None:
        market_is_open = getattr(self.broker, "market_is_open", None)
        if not callable(market_is_open):
            return None
        estimator = getattr(market_is_open, "pending_market_read_cost", None)
        if not callable(estimator):
            return None
        try:
            estimate = estimator()
        except Exception:
            return _LIVE_OPENING_DAY_MAX_BUDGETED_READS
        if not isinstance(estimate, int) or isinstance(estimate, bool):
            return _LIVE_OPENING_DAY_MAX_BUDGETED_READS
        if estimate < 0 or estimate > _LIVE_OPENING_DAY_MAX_BUDGETED_READS:
            return _LIVE_OPENING_DAY_MAX_BUDGETED_READS
        return estimate

    def _production_live_opening_day_read_cost(self) -> int | None:
        estimate = self._estimated_live_opening_day_read_cost()
        if estimate is None:
            return None
        ensure_budget = getattr(
            self._live_kis_client(),
            "ensure_market_read_budget",
            None,
        )
        if not callable(ensure_budget):
            return None
        return estimate

    def _pending_live_opening_day_read_cost(self) -> int:
        estimate = self._estimated_live_opening_day_read_cost()
        return 1 if estimate is None else estimate

    def _live_order_preflight_market_read_cost(self) -> int:
        return (
            self._observed_live_account_read_cost()
            + self._pending_live_opening_day_read_cost()
        )

    def _live_buy_preflight_market_read_cost(self) -> int:
        return self._live_order_preflight_market_read_cost()

    def _live_kis_client(self):
        client = getattr(self.broker, "client", None)
        if client is not None:
            return client
        return getattr(self.final_quote_provider, "__self__", None)

    def _begin_physical_market_read_budget(self) -> int | None:
        if self.execution_mode != "live":
            return None
        opening_day_read_cost = self._production_live_opening_day_read_cost()
        budget_limit = self.max_physical_market_reads_per_cycle
        if budget_limit is not None and opening_day_read_cost is not None:
            budget_limit = max(0, int(budget_limit)) + opening_day_read_cost
        begin_budget = getattr(self._live_kis_client(), "begin_market_read_budget", None)
        if callable(begin_budget):
            begin_budget(budget_limit)
        return opening_day_read_cost

    def _warm_live_opening_day_gate(
        self,
        cycle_events: list[RuntimeEvent],
        pending_read_cost: int | None,
    ) -> bool:
        if self.execution_mode != "live" or pending_read_cost is None:
            return True
        market_is_open = getattr(self.broker, "market_is_open", None)
        if not callable(market_is_open):
            return True
        try:
            opening_day_ready = market_is_open()
        except Exception:
            opening_day_ready = False
        if opening_day_ready is True:
            return True
        cycle_events.append(
            self._emit(
                RuntimeEvent.system(
                    "live_opening_day_gate_failed - opening-day status unavailable; cycle skipped"
                )
            )
        )
        return False

    def _ensure_live_decision_market_read_budget(self) -> bool:
        if self.execution_mode != "live":
            return True
        client = self._live_kis_client()
        ensure_budget = getattr(client, "ensure_market_read_budget", None)
        state = self._physical_market_read_budget_state()
        if not callable(ensure_budget) or state is None:
            return True
        used, _limit = state
        per_symbol_reads = 0
        if callable(self.entry_history_provider):
            per_symbol_reads += 1
        if self.final_quote_provider is not None:
            per_symbol_reads += 2
        minimum_limit = (
            used
            + self._observed_live_account_read_cost()
            + per_symbol_reads
            + self._pending_live_opening_day_read_cost()
        )
        try:
            ensure_budget(minimum_limit)
        except Exception:
            return False
        return True

    def _physical_market_read_budget_state(self) -> tuple[int, int] | None:
        state_reader = getattr(self._live_kis_client(), "market_read_budget_state", None)
        if not callable(state_reader):
            return None
        state = state_reader()
        if not isinstance(state, tuple) or len(state) != 2:
            return None
        return max(0, int(state[0])), max(0, int(state[1]))

    def _end_physical_market_read_budget(self) -> None:
        end_budget = getattr(self._live_kis_client(), "end_market_read_budget", None)
        if callable(end_budget):
            end_budget()

    def _physical_market_read_diagnostic(self) -> str:
        state = self._physical_market_read_budget_state()
        if state is None:
            return "unlimited"
        return f"{state[0]}/{state[1]}"

    def _live_entry_market_read_budget_issue(
        self,
        symbol: str,
        bar: MarketBar | None = None,
        *,
        include_final_quote: bool = False,
        include_order_preflight: bool = False,
    ) -> str | None:
        if self.execution_mode != "live":
            return None
        state = self._physical_market_read_budget_state()
        if state is None:
            return None
        account = self._account_with_cycle_overlays(self._runtime_account_snapshot())
        if symbol in account.positions:
            return None
        required_reads = 0
        history_refresh_required = (
            bar is None
            and symbol not in self._live_history_refresh_buckets
        ) or (
            bar is not None
            and self._live_history_refresh_required(symbol, bar)
        )
        if (
            callable(self.entry_history_provider)
            and history_refresh_required
            and not self._scanner_history_is_ready_for_bar(symbol, bar)
        ):
            required_reads += 1
        if (
            include_final_quote
            and self.final_quote_provider is not None
            and symbol not in self._confirmed_scan_bars_this_cycle
        ):
            required_reads += 2
        if include_order_preflight:
            required_reads += self._live_buy_preflight_market_read_cost()
        used, limit = state
        if limit - used < required_reads:
            return "physical_market_read_budget_reached"
        return None

    def _live_exit_market_read_budget_issue(self) -> str | None:
        if self.execution_mode != "live":
            return None
        state = self._physical_market_read_budget_state()
        if state is None:
            return None
        used, limit = state
        required_reads = self._live_order_preflight_market_read_cost()
        if limit - used < required_reads:
            return f"physical_reads={used}/{limit}, required_reads={required_reads}"
        return None

    def _symbols_for_cycle(self) -> list[str]:
        all_open_symbols = [symbol for symbol in self._runtime_account_snapshot().positions]
        open_symbols = self._rotated_open_position_symbols(all_open_symbols)
        if self.settings.kill_switch:
            return open_symbols
        if not self.symbols:
            return open_symbols

        ranked_symbols = self._entry_scan_order(self.symbols)
        scan_budget = self._scan_symbol_budget(open_symbols)
        warmup_symbols = self._scannable_warmup_symbols(ranked_symbols, all_open_symbols)
        if self._uses_authoritative_final_quote_scan_window():
            scanned_symbols = self._authoritative_final_quote_scan_symbols(
                warmup_symbols,
                open_symbols,
                scan_budget,
            )
        elif self._uses_kis_warmup_scan_batch(warmup_symbols, scan_budget):
            scanned_symbols = self._warmup_scan_symbols(warmup_symbols, scan_budget)
        elif self.scan_limit_per_cycle is None or self.scan_limit_per_cycle >= len(ranked_symbols):
            scanned_symbols = self._pre_request_scan_symbols(ranked_symbols)
        else:
            scanned_symbols = self._symbols_from_cursor(ranked_symbols, limit=self.scan_limit_per_cycle)

        symbols: list[str] = []
        for symbol in [*open_symbols, *scanned_symbols]:
            if symbol not in symbols:
                symbols.append(symbol)
        return symbols

    def _rotated_open_position_symbols(self, symbols: list[str]) -> list[str]:
        if not symbols:
            self._open_position_monitor_queue = []
            self._open_position_monitor_failures = {}
            return []
        current_symbols = set(symbols)
        queue = [symbol for symbol in self._open_position_monitor_queue if symbol in current_symbols]
        self._open_position_monitor_failures = {
            symbol: failures
            for symbol, failures in self._open_position_monitor_failures.items()
            if symbol in current_symbols
        }
        queued = set(queue)
        queue.extend(symbol for symbol in symbols if symbol not in queued)
        self._open_position_monitor_queue = queue
        positions_per_cycle = self._open_position_monitor_limit(len(symbols))
        if positions_per_cycle <= 0:
            return []
        return queue[:positions_per_cycle]

    def _record_open_position_processed(self, symbol: str) -> None:
        self._open_position_monitor_failures.pop(symbol, None)
        if symbol not in self._open_position_monitor_queue:
            return
        self._open_position_monitor_queue.remove(symbol)
        self._open_position_monitor_queue.append(symbol)

    def _record_open_position_deferred(self, symbol: str) -> None:
        if symbol not in self._open_position_monitor_queue:
            return
        if self._open_position_monitor_failures.get(symbol, 0) == 0:
            self._open_position_monitor_failures[symbol] = 1
            self._prioritize_open_position(symbol)
            return
        self._open_position_monitor_failures.pop(symbol, None)
        self._open_position_monitor_queue.remove(symbol)
        self._open_position_monitor_queue.append(symbol)

    def _prioritize_open_position(self, symbol: str) -> None:
        if symbol not in self._open_position_monitor_queue:
            return
        self._open_position_monitor_queue.remove(symbol)
        self._open_position_monitor_queue.insert(0, symbol)

    def _open_position_monitor_limit(self, position_count: int) -> int:
        limit = max(0, int(position_count))
        if self.execution_mode == "live" and self.max_physical_market_reads_per_cycle is not None:
            state = self._physical_market_read_budget_state()
            physical_budget = (
                state[1]
                if state is not None
                else self.max_physical_market_reads_per_cycle
            )
            reads_already_used = (
                state[0]
                if state is not None
                else self._observed_live_account_read_cost()
            )
            sell_preflight_reserve = self._live_order_preflight_market_read_cost()
            physical_limit = max(
                0,
                (
                    physical_budget
                    - reads_already_used
                    - sell_preflight_reserve
                )
                // self._live_monitor_market_reads_per_position(),
            )
            limit = min(limit, physical_limit)
            if self._reserve_live_entry_lane(position_count):
                has_dynamic_extension = (
                    state is not None
                    and state[1] > self.max_physical_market_reads_per_cycle
                )
                minimum_sell_monitor = (
                    1
                    if has_dynamic_extension and position_count > 0 and physical_limit > 0
                    else 0
                )
                while (
                    limit > minimum_sell_monitor
                    and self._live_physical_entry_capacity_for_monitored_positions(limit) <= 0
                ):
                    limit -= 1
        return limit

    def _uses_kis_warmup_scan_batch(self, ranked_symbols: list[str], scan_budget: int) -> bool:
        return (
            self.data_source_kind == "kis-vts"
            and self.scan_limit_per_cycle is not None
            and scan_budget > 0
            and scan_budget < len(ranked_symbols)
            and self._entry_evaluation_samples_required() > 0
        )

    def _uses_authoritative_final_quote_scan_window(self) -> bool:
        return self._uses_authoritative_scanner() and self._uses_final_quote_budget_for_entries()

    def _authoritative_final_quote_scan_symbols(
        self,
        ranked_symbols: list[str],
        open_symbols: list[str],
        scan_budget: int,
    ) -> list[str]:
        if not ranked_symbols or scan_budget <= 0:
            return []

        limit = min(scan_budget, len(ranked_symbols))
        if self.scan_limit_per_cycle is not None:
            limit = min(limit, max(0, int(self.scan_limit_per_cycle)))
        if limit <= 0:
            return []
        self._prime_entry_prices_from_authoritative_scanner(ranked_symbols[:limit])
        selected = self._symbols_from_cursor(ranked_symbols, limit=limit)
        if self.max_final_quote_requests_per_cycle is None or not selected:
            return selected

        reserved_for_open_positions = len(dict.fromkeys(open_symbols))
        sparse_quote_window = max(0, self.max_final_quote_requests_per_cycle - reserved_for_open_positions)
        sparse_symbols = self._sparse_scanner_symbols(selected)
        if not sparse_symbols:
            return selected
        sparse_symbol_set = set(sparse_symbols)
        dense_first_selected = [
            symbol
            for symbol in selected
            if symbol not in sparse_symbol_set
        ] + sparse_symbols
        if sparse_quote_window >= len(sparse_symbols):
            return dense_first_selected
        if sparse_quote_window <= 0:
            return dense_first_selected

        return dense_first_selected

    def _sparse_scanner_symbols(self, symbols: Iterable[str]) -> list[str]:
        sparse_symbols: list[str] = []
        for symbol in symbols:
            bar = self._scanner_snapshot.bars.get(symbol)
            if bar is not None and self._scanner_bar_is_sparse(bar):
                sparse_symbols.append(symbol)
        return sparse_symbols

    def _move_scan_cursor_after_symbol(self, ordered_symbols: list[str], symbol: str) -> None:
        try:
            self._scan_cursor = (ordered_symbols.index(symbol) + 1) % len(ordered_symbols)
            self._scan_cursor_anchor = ordered_symbols[self._scan_cursor]
        except ValueError:
            pass

    def _move_scan_cursor_to_symbol(self, ordered_symbols: list[str], symbol: str) -> None:
        try:
            self._scan_cursor = ordered_symbols.index(symbol) % len(ordered_symbols)
            self._scan_cursor_anchor = ordered_symbols[self._scan_cursor]
        except ValueError:
            pass

    def _warmup_scan_symbols(self, ranked_symbols: list[str], scan_budget: int) -> list[str]:
        current_batch = [symbol for symbol in self._scan_batch_symbols if symbol in ranked_symbols]
        current_batch = current_batch[:scan_budget]
        if self._skips_known_unaffordable_before_scan():
            current_batch = [
                symbol
                for symbol in current_batch
                if not self._known_entry_unavailable_after_current_scan_prime(symbol)
            ]
        if current_batch and self._scan_batch_needs_warmup(current_batch):
            selected = self._fill_scan_batch(current_batch, ranked_symbols, scan_budget)
            self._scan_batch_symbols = selected
            return selected

        limit = min(scan_budget, int(self.scan_limit_per_cycle or len(ranked_symbols)), len(ranked_symbols))
        if limit <= 0:
            self._scan_batch_symbols = []
            return []

        selected = self._symbols_from_cursor(ranked_symbols, limit=limit)
        self._scan_batch_symbols = selected
        return selected

    def _pre_request_scan_symbols(self, ranked_symbols: list[str]) -> list[str]:
        if not self._skips_known_unaffordable_before_scan():
            return ranked_symbols
        selected: list[str] = []
        for symbol in ranked_symbols:
            if not self._known_entry_unavailable_after_current_scan_prime(symbol):
                selected.append(symbol)
        return selected

    def _uses_authoritative_scanner(self) -> bool:
        return self.data_source_kind in {"external-scan-kis", "live"} and self.scanner_provider is not None

    def _skips_known_unaffordable_before_scan(self) -> bool:
        if (
            self.execution_mode == "live"
            and not getattr(self, "_cycle_planning_buying_power_refreshed", False)
            and callable(getattr(self.broker, "refresh_planning_account", None))
        ):
            return False
        return self.data_source_kind == "kis-vts" or self._uses_authoritative_scanner()

    def _fill_scan_batch(self, current_batch: list[str], ranked_symbols: list[str], scan_budget: int) -> list[str]:
        selected: list[str] = []
        for symbol in current_batch:
            if symbol not in selected:
                selected.append(symbol)
        remaining = scan_budget - len(selected)
        if remaining <= 0:
            return selected[:scan_budget]
        selected.extend(self._symbols_from_cursor(ranked_symbols, limit=remaining, excluded=selected))
        return selected[:scan_budget]

    def _scannable_warmup_symbols(self, ranked_symbols: list[str], open_symbols: list[str]) -> list[str]:
        open_symbol_set = set(open_symbols)
        return [symbol for symbol in ranked_symbols if symbol not in open_symbol_set]

    def _scan_symbol_budget(self, open_symbols: list[str]) -> int:
        if self.scan_limit_per_cycle is None:
            configured_scan_limit = len(self.symbols)
        else:
            configured_scan_limit = self.scan_limit_per_cycle
        if self.max_bar_requests_per_cycle is None:
            return max(0, configured_scan_limit)
        open_request_count = len(dict.fromkeys(open_symbols))
        return max(0, min(configured_scan_limit, self.max_bar_requests_per_cycle - open_request_count))

    def _scan_batch_needs_warmup(self, symbols: list[str]) -> bool:
        required_samples = self._entry_evaluation_samples_required()
        attempt_limit = self._warmup_attempt_limit()
        return any(
            self._successful_bar_samples.get(symbol, 0) < required_samples
            and self._bar_request_attempts.get(symbol, 0) < attempt_limit
            for symbol in symbols
        )

    def _entry_evaluation_samples_required(self) -> int:
        config = getattr(self.strategy, "config", None)
        if config is None:
            return 0
        required_history = max(
            int(getattr(config, "momentum_window", 0) or 0),
            int(getattr(config, "volume_window", 0) or 0),
            int(getattr(config, "trend_boundary_window", 0) or 0),
            2,
        )
        return required_history + 1

    def _live_completed_history_samples_required(self) -> int:
        config = getattr(self.strategy, "config", None)
        if config is None:
            return 0
        volume_window = int(getattr(config, "volume_window", 0) or 0)
        min_volume_ratio = Decimal(
            str(getattr(config, "min_volume_ratio", Decimal("0")) or Decimal("0"))
        )
        return max(
            int(getattr(config, "momentum_window", 0) or 0),
            int(getattr(config, "trend_boundary_window", 0) or 0),
            volume_window + 1 if min_volume_ratio > 0 else volume_window,
            3,
        )

    def _strategy_signals_for_bar(self, bar: MarketBar, account: AccountSnapshot) -> list[Signal]:
        if self._uses_minute_strategy_buckets():
            on_live_bar = getattr(self.strategy, "on_live_bar", None)
            if callable(on_live_bar):
                return list(on_live_bar(bar, account))
        return list(self.strategy.on_bar(bar, account))

    def _record_successful_bar_sample(self, symbol: str, bar: MarketBar) -> None:
        if self._uses_minute_strategy_buckets():
            return
        self._successful_bar_samples[symbol] = self._successful_bar_samples.get(symbol, 0) + 1

    def _uses_minute_strategy_buckets(self) -> bool:
        return self.execution_mode == "live"

    def _live_history_refresh_required(
        self,
        symbol: str,
        bar: MarketBar,
    ) -> bool:
        if self.execution_mode != "live":
            return False
        return (
            self._live_history_refresh_buckets.get(symbol)
            != _bar_minute_bucket_key(bar)
            or self._live_history_refresh_days.get(symbol)
            != _bar_trading_day_key(bar)
        )

    def _reset_strategy_history(self, symbol: str) -> None:
        reset_history = getattr(self.strategy, "reset_history", None)
        if callable(reset_history):
            reset_history(symbol)
            return
        seed_history = getattr(self.strategy, "seed_history", None)
        if callable(seed_history):
            seed_history(symbol, [])

    def _record_bar_request_attempt(self, symbol: str) -> None:
        self._bar_request_attempts[symbol] = self._bar_request_attempts.get(symbol, 0) + 1

    def _record_latest_entry_price(self, symbol: str, bar: MarketBar) -> None:
        price = entry_reference_price(bar)
        if price is not None:
            self._latest_entry_prices[symbol] = price
        short_price = entry_reference_price(bar, "SHORT_ENTRY")
        if short_price is not None:
            self._latest_short_entry_prices[symbol] = short_price

    def _scan_confirmation_bar(
        self,
        symbol: str,
        bar: MarketBar,
        cycle_events: list[RuntimeEvent],
    ) -> MarketBar | None:
        if not self._needs_scan_confirmation_bar(symbol, bar):
            return None
        if self._scan_confirmation_limit_reached():
            return None

        self._final_quote_requests_this_cycle += 1
        try:
            confirmed_bar = self.final_quote_provider(symbol) if self.final_quote_provider is not None else None
        except Exception as exc:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"scanner_diagnostic - final_quote_scan_error: {self._label_for(symbol)} "
                        f"{_market_data_error_message(exc)}"
                    )
                )
            )
            return None
        if confirmed_bar is None:
            return None
        self.broker.update_market(confirmed_bar)
        return confirmed_bar

    def _needs_scan_confirmation_bar(self, symbol: str, bar: MarketBar) -> bool:
        return self._scan_confirmation_reason(symbol, bar) is not None

    def _scan_confirmation_reason(self, symbol: str, bar: MarketBar) -> str | None:
        if not self._uses_authoritative_scanner():
            return None
        if self.final_quote_provider is None:
            return None
        if symbol in self._confirmed_scan_bars_this_cycle:
            return None
        if self._scanner_bar_is_sparse(bar):
            return "scanner_bar_sparse"
        if self.execution_mode == "live" and (bar.bid is None or bar.ask is None):
            if self._supports_provisional_live_scanner_quote(bar):
                return None
            return "scanner_quote_missing"
        return None

    def _supports_provisional_live_scanner_quote(self, bar: MarketBar) -> bool:
        return (
            self.execution_mode == "live"
            and self.final_quote_provider is not None
            and not self._scanner_bar_is_sparse(bar)
            and getattr(
                self.strategy,
                "supports_provisional_live_scanner_quotes",
                False,
            )
            is True
            and callable(getattr(self.strategy, "revalidate_live_signal", None))
        )

    def _scanner_bar_is_sparse(self, bar: MarketBar) -> bool:
        if int(bar.volume) <= 0:
            return True
        return bar.open == bar.close and bar.high == bar.close and bar.low == bar.close

    def _scan_confirmation_limit_reached(self) -> bool:
        return (
            self.max_final_quote_requests_per_cycle is not None
            and self._final_quote_requests_this_cycle >= self.max_final_quote_requests_per_cycle
        )

    def _scan_confirmation_unavailable_reason(self, limit_reached_before_request: bool) -> str:
        if limit_reached_before_request:
            return "final_quote_limit_reached"
        return "final_quote_unavailable"

    def _prewarm_entry_history(self, symbol: str, bar: MarketBar) -> None:
        if self.data_source_kind != "kis-vts" and not self._uses_authoritative_scanner():
            return
        if (
            self.execution_mode != "live"
            and self._successful_bar_samples.get(symbol, 0) > 0
        ):
            return

        seed_history = getattr(self.strategy, "seed_history", None)
        if not callable(seed_history):
            return

        if self.execution_mode == "live":
            prior_sample_count = self._live_completed_history_samples_required()
        else:
            prior_sample_count = max(
                0,
                self._entry_evaluation_samples_required() - 1,
            )
        if prior_sample_count <= 0:
            return

        history_failure_reasons: tuple[str, ...] = ()
        if self.execution_mode == "live":
            scanner_history_seed = self._completed_history_seed(
                self._scanner_snapshot.histories.get(symbol, ()),
                symbol=symbol,
                current_bar=bar,
                sample_count=prior_sample_count,
            )
            scanner_failure_reasons = self._completed_history_failure_reasons(
                scanner_history_seed,
                current_bar=bar,
                sample_count=prior_sample_count,
            )
            scanner_seed_bars = [] if scanner_failure_reasons else scanner_history_seed
            if not scanner_seed_bars and not callable(self.entry_history_provider):
                self._record_history_failure(symbol, *scanner_failure_reasons)
                return
            current_bucket = _bar_minute_bucket_key(bar)
            current_day = _bar_trading_day_key(bar)
            if not self._live_history_refresh_required(symbol, bar):
                return
            if self._live_history_refresh_days.get(symbol) not in {
                None,
                current_day,
            }:
                self._reset_strategy_history(symbol)
                self._successful_bar_samples[symbol] = 0
            self._live_history_refresh_buckets[symbol] = current_bucket
            self._live_history_refresh_days[symbol] = current_day
            self._live_history_ready_buckets.pop(symbol, None)
            seed_bars = scanner_seed_bars
            if not seed_bars:
                try:
                    history_bars = list(self.entry_history_provider(symbol) or ())
                except Exception:
                    self._record_history_failure(symbol, "provider_exception")
                    return
                seed_bars = self._completed_history_seed(
                    history_bars,
                    symbol=symbol,
                    current_bar=bar,
                    sample_count=prior_sample_count,
                )
                history_failure_reasons = self._completed_history_failure_reasons(
                    seed_bars,
                    current_bar=bar,
                    sample_count=prior_sample_count,
                )
        else:
            volume_ratio_floor = Decimal("1")
            if self._uses_authoritative_scanner():
                config = getattr(self.strategy, "config", None)
                volume_ratio_floor = Decimal(
                    str(getattr(config, "min_volume_ratio", Decimal("1")) or Decimal("1"))
                )
            seed_bars = _intraday_proxy_history_from_quote(
                bar,
                prior_sample_count,
                volume_ratio_floor=volume_ratio_floor,
            )
        if not seed_bars:
            if self.execution_mode == "live":
                self._record_history_failure(
                    symbol,
                    *(history_failure_reasons or ("insufficient_count",)),
                )
                self._reset_strategy_history(symbol)
                self._successful_bar_samples[symbol] = 0
            return

        try:
            seeded_count = int(seed_history(symbol, seed_bars) or 0)
        except Exception:
            return
        if seeded_count <= 0:
            if self.execution_mode == "live":
                self._record_history_failure(symbol, "insufficient_count")
                self._successful_bar_samples[symbol] = 0
            return
        if self.execution_mode == "live":
            self._successful_bar_samples[symbol] = seeded_count
            readiness_failures = set(history_failure_reasons)
            if seeded_count < prior_sample_count:
                readiness_failures.add("insufficient_count")
            if not readiness_failures:
                self._live_history_ready_buckets[symbol] = current_bucket
            else:
                self._record_history_failure(symbol, *readiness_failures)
        else:
            self._successful_bar_samples[symbol] = max(
                self._successful_bar_samples.get(symbol, 0),
                seeded_count,
            )

    def _scanner_history_is_ready_for_bar(
        self,
        symbol: str,
        bar: MarketBar | None,
    ) -> bool:
        if bar is None:
            return False
        required = self._live_completed_history_samples_required()
        if required <= 0:
            return True
        seed_bars = self._completed_history_seed(
            self._scanner_snapshot.histories.get(symbol, ()),
            symbol=symbol,
            current_bar=bar,
            sample_count=required,
        )
        return self._completed_history_is_ready(
            seed_bars,
            current_bar=bar,
            sample_count=required,
        )

    def _history_prioritized_scanner_symbols(
        self,
        symbols: Iterable[str],
        *,
        open_symbols: Iterable[str] = (),
    ) -> tuple[list[str], tuple[str, ...], tuple[str, ...]]:
        listed = list(symbols)
        if self.execution_mode != "live" or not self._uses_authoritative_scanner():
            return listed, (), ()

        open_symbol_set = set(open_symbols)
        ready_symbols: list[str] = []
        fallback_symbols: list[str] = []
        for symbol in listed:
            if symbol in open_symbol_set:
                continue
            if self._scanner_history_is_ready_for_bar(
                symbol,
                self._scanner_snapshot.bars.get(symbol),
            ):
                ready_symbols.append(symbol)
            else:
                fallback_symbols.append(symbol)

        if not ready_symbols:
            return listed, (), tuple(fallback_symbols)
        monitored_symbols = [symbol for symbol in listed if symbol in open_symbol_set]
        return (
            [*monitored_symbols, *ready_symbols, *fallback_symbols],
            tuple(ready_symbols),
            tuple(fallback_symbols),
        )

    @staticmethod
    def _completed_history_seed(
        history_bars: Iterable[MarketBar],
        *,
        symbol: str,
        current_bar: MarketBar,
        sample_count: int,
    ) -> list[MarketBar]:
        current_bucket = _bar_minute_bucket_key(current_bar)
        current_day = _bar_trading_day_key(current_bar)
        latest_by_bucket: dict[int, MarketBar] = {}
        for history_bar in history_bars:
            if history_bar.symbol != symbol:
                continue
            if _bar_trading_day_key(history_bar) != current_day:
                continue
            history_bucket = _bar_minute_bucket_key(history_bar)
            if history_bucket >= current_bucket:
                continue
            existing = latest_by_bucket.get(history_bucket)
            if existing is None or _bar_timestamp_key(existing) < _bar_timestamp_key(
                history_bar
            ):
                latest_by_bucket[history_bucket] = history_bar
        parsed_sample_count = max(0, int(sample_count))
        if parsed_sample_count <= 0:
            return []
        return sorted(latest_by_bucket.values(), key=_bar_timestamp_key)[
            -parsed_sample_count:
        ]

    @staticmethod
    def _completed_history_is_ready(
        seed_bars: list[MarketBar],
        *,
        current_bar: MarketBar,
        sample_count: int,
    ) -> bool:
        parsed_sample_count = max(0, int(sample_count))
        current_bucket = _bar_minute_bucket_key(current_bar)
        if (
            len(seed_bars) < parsed_sample_count
            or not seed_bars
            or _bar_minute_bucket_key(seed_bars[-1]) != current_bucket - 1
            or not _bars_have_contiguous_minute_buckets(seed_bars)
        ):
            return False
        return True

    @staticmethod
    def _completed_history_failure_reasons(
        seed_bars: list[MarketBar],
        *,
        current_bar: MarketBar,
        sample_count: int,
    ) -> tuple[str, ...]:
        reasons: list[str] = []
        parsed_sample_count = max(0, int(sample_count))
        if len(seed_bars) < parsed_sample_count:
            reasons.append("insufficient_count")
        if seed_bars and _bar_minute_bucket_key(seed_bars[-1]) != _bar_minute_bucket_key(current_bar) - 1:
            reasons.append("latest_mismatch")
        if len(seed_bars) > 1 and not _bars_have_contiguous_minute_buckets(seed_bars):
            reasons.append("gap")
        return tuple(reasons)

    def _record_history_failure(self, symbol: str, *reasons: str) -> None:
        parsed_reasons = {
            reason
            for reason in reasons
            if reason in {"insufficient_count", "latest_mismatch", "gap", "provider_exception"}
        }
        if parsed_reasons:
            self._cycle_history_failure_reasons.setdefault(symbol, set()).update(parsed_reasons)

    def _known_entry_unaffordable(self, symbol: str) -> bool:
        return self._candidate_selector(
            self._runtime_account_snapshot(),
            prescan=True,
        ).known_entry_unaffordable(symbol)

    def _known_entry_unavailable_before_scan(self, symbol: str, as_of=None) -> bool:
        return self._entry_unavailable_before_scan_reason(symbol, as_of) is not None

    def _known_entry_unavailable_after_current_scan_prime(self, symbol: str) -> bool:
        if self._uses_authoritative_scanner():
            self._prime_entry_prices_from_authoritative_scanner([symbol])
        reason = self._entry_unavailable_before_scan_reason(symbol, self._scanner_bar_date(symbol))
        if reason is None:
            return False
        self._record_prescan_rejection(symbol, reason)
        return True

    def _entry_unavailable_before_scan_reason(self, symbol: str, as_of=None) -> str | None:
        if self._remaining_entry_slots(self._runtime_account_snapshot()) <= 0:
            return "no_entry_capacity"
        if self._known_entry_unaffordable(symbol):
            return "entry_unaffordable"
        if self._daily_entry_limit_reached(symbol, as_of):
            return "max_daily_entries_reached"
        return None

    def _record_prescan_rejection(self, symbol: str, reason: str) -> None:
        if reason not in {"entry_unaffordable", "max_daily_entries_reached", "no_entry_capacity"}:
            return
        reasons = self._cycle_prescan_rejection_reasons.setdefault(symbol, set())
        reasons.add(_summary_reason_key(reason))

    def _daily_entry_limit_reached(self, symbol: str, as_of=None) -> bool:
        checker = getattr(self.risk_manager, "entry_limit_reached", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(symbol, as_of))
        except TypeError:
            return bool(checker(symbol))

    def _warmup_attempt_limit(self) -> int:
        return self._entry_evaluation_samples_required() + 2

    def _entry_priority(self, signal: Signal) -> float:
        score_getter = getattr(self.strategy, "last_entry_score", None)
        if not callable(score_getter):
            return self._entry_tie_breaker(signal.symbol)
        score = score_getter(signal.symbol)
        if score is None:
            return self._entry_tie_breaker(signal.symbol)
        confidence = float(getattr(score, "confidence", 0.0))
        if signal.side == "SHORT_ENTRY":
            score_value = max(float(getattr(score, "short_score", 0.0)), confidence)
        else:
            score_value = max(float(getattr(score, "long_score", 0.0)), confidence)
        return score_value + self._entry_tie_breaker(signal.symbol)

    def _entry_tie_breaker(self, symbol: str) -> float:
        seed = sum((index + 1) * ord(char) for index, char in enumerate(symbol))
        rotated = (seed + (self.cycle_count * 997)) % 1000
        return rotated / 1_000_000

    def _entry_candidate_issue(
        self,
        signal: Signal,
        bar: MarketBar,
        account,
        *,
        prescan: bool = False,
    ) -> str | None:
        if signal.side == "SHORT_ENTRY" and not self.settings.allow_paper_short:
            return None
        if signal.symbol in self._cycle_entry_symbols:
            return "already_entered_this_cycle"
        if self._daily_entry_limit_reached(signal.symbol, bar.timestamp.date()):
            return "max_daily_entries_reached"
        issue = self._candidate_selector(account, prescan=prescan).entry_issue_for_signal(signal.side, bar)
        return None if issue is None else issue.message()

    def _exit_final_quote_issue(self, signal: Signal, bar: MarketBar, account) -> str | None:
        if self.final_quote_provider is None:
            return None
        if self.settings.kill_switch:
            return None
        if signal.side not in {"SELL", "SHORT_EXIT"}:
            return None
        if signal.reason in {"forced_exit", "max_holding_time", "cleanup exit"}:
            return None
        position = getattr(account, "positions", {}).get(signal.symbol)
        if position is None:
            return None
        if signal.side == "SELL" and bar.sell_price == position.avg_price:
            return "flat_final_quote_exit"
        if signal.side == "SHORT_EXIT" and bar.buy_price == position.avg_price:
            return "flat_final_quote_exit"
        return None

    def _entry_scan_issue(self, symbol: str, bar: MarketBar, account) -> str | None:
        if symbol in self._cycle_entry_symbols:
            return "already_entered_this_cycle"
        if symbol in account.positions:
            return None
        current_bucket = _bar_minute_bucket_key(bar)
        if (
            self.execution_mode == "live"
            and self._live_history_refresh_buckets.get(symbol) == current_bucket
            and self._live_history_ready_buckets.get(symbol) != current_bucket
        ):
            return "completed_minute_history_not_ready"
        if self._daily_entry_limit_reached(symbol, bar.timestamp.date()):
            return "max_daily_entries_reached"
        issue = self._candidate_selector(account, prescan=True).entry_issue_for_scan(bar)
        return None if issue is None else issue.message()

    def _has_entry_capacity(self) -> bool:
        if self.settings.kill_switch:
            return False
        configured_limit = self._position_limit()
        if configured_limit > 0:
            return self._position_count_with_cycle_entries(
                self._runtime_account_snapshot()
            ) < configured_limit
        if self._cycle_entry_slot_capacity is not None:
            return len(self._cycle_entry_symbols) < self._cycle_entry_slot_capacity
        return True

    def _cycle_blocks_new_entries_for_pending_order(self) -> bool:
        return (
            self._cycle_paused_for_live_pending_order
            or self._cycle_new_entries_blocked_for_live_pending_order
        )

    def _cycle_blocks_new_entries(self) -> bool:
        return (
            self._cycle_blocks_new_entries_for_pending_order()
            or self._cycle_new_entries_blocked_for_live_entry_count
        )

    def _position_limit(self) -> int:
        risk_config = getattr(self.risk_manager, "config", None)
        return int(getattr(risk_config, "max_positions", self.settings.max_positions))

    def _top_up_scan_limit(
        self,
        *,
        had_positions_at_cycle_start: bool = False,
        exited_symbols: set[str] | None = None,
    ) -> int:
        if self.settings.kill_switch:
            return 0
        if (
            not self._uses_authoritative_scanner()
            and not had_positions_at_cycle_start
            and not exited_symbols
        ):
            return 0
        configured_limit = self._position_limit()
        if configured_limit <= 0:
            open_slots = self._open_entry_slots_for_practical_target()
            if open_slots <= 0 and not (had_positions_at_cycle_start and exited_symbols):
                return 0
            return len(self.symbols)
        open_slots = configured_limit - self._position_count_with_cycle_entries(self._runtime_account_snapshot())
        if open_slots <= 0:
            return 0
        return len(self.symbols)

    def _open_entry_slots_for_practical_target(self) -> int:
        configured_limit = self._position_limit()
        account = self._runtime_account_snapshot()
        open_positions = self._position_count_with_cycle_entries(account)
        if configured_limit > 0:
            return max(0, configured_limit - open_positions)
        if self._cycle_entry_slot_target is not None:
            return max(0, self._cycle_entry_slot_target - open_positions)
        if self._cycle_entry_slot_capacity is not None:
            return max(0, self._cycle_entry_slot_capacity - len(self._cycle_entry_symbols))
        return self._unlimited_entry_slot_target()

    def _top_up_entry_fill_limit(
        self,
        *,
        had_positions_at_cycle_start: bool = False,
        exited_symbols: set[str] | None = None,
    ) -> int | None:
        if self._position_limit() > 0:
            return None
        exit_replacement_slots = len(exited_symbols or ()) if had_positions_at_cycle_start else 0
        open_target_slots = self._open_entry_slots_for_practical_target()
        return max(open_target_slots, exit_replacement_slots)

    def _uses_final_quote_budget_for_entries(self) -> bool:
        return self.final_quote_provider is not None and self.max_final_quote_requests_per_cycle is not None

    def _remaining_final_quote_capacity(self) -> int:
        if self.max_final_quote_requests_per_cycle is None:
            return len(self.symbols)
        return max(0, self.max_final_quote_requests_per_cycle - self._final_quote_requests_this_cycle)

    def _entry_candidate_deferred_by_final_quote_limit(self, signal: Signal) -> bool:
        if not self._uses_final_quote_budget_for_entries():
            return False
        if signal.symbol in self._confirmed_scan_bars_this_cycle:
            return False
        return self._remaining_final_quote_capacity() <= 0

    def _replacement_scan_symbols(self, processed_symbols: set[str], *, limit: int) -> list[str]:
        if not self.symbols:
            return []
        ranked_symbols = self._entry_scan_order(self.symbols)
        return self._symbols_from_cursor(ranked_symbols, limit=limit, excluded=processed_symbols)

    def _empty_portfolio_refill_symbols(self, excluded_symbols: set[str]) -> list[str]:
        if not self.symbols:
            return []
        ranked_symbols = self._entry_scan_order(self.symbols)
        return self._symbols_from_cursor(ranked_symbols, limit=len(ranked_symbols), excluded=excluded_symbols)

    def _empty_portfolio_refill_pass_limit(self) -> int:
        config = getattr(self.strategy, "config", None)
        required_history = max(
            int(getattr(config, "momentum_window", 0) or 0),
            int(getattr(config, "volume_window", 0) or 0),
            int(getattr(config, "trend_boundary_window", 0) or 0),
        )
        return min(max(3, required_history + 1), 10)

    def _rank_scan_symbols(self, symbols: Iterable[str]) -> list[str]:
        listed = list(symbols)
        cache_key = tuple(listed)
        cached = self._ranked_symbols_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        if self.scanner_provider is not None:
            try:
                ranked = self.scanner_provider.rank_symbols(listed)
            except Exception as exc:
                if self._uses_authoritative_scanner():
                    self._pending_scanner_rank_error = exc
                    ranked = []
                else:
                    ranked = listed
            self._ranked_symbols_cache[cache_key] = list(ranked)
            return list(ranked)
        if self.symbol_priority_provider is None:
            ranked = listed
        else:
            ranked = sorted(listed, key=lambda symbol: -self._symbol_priority(symbol))
        self._ranked_symbols_cache[cache_key] = list(ranked)
        return list(ranked)

    def _entry_scan_order(self, symbols: Iterable[str]) -> list[str]:
        ranked_symbols = self._rank_scan_symbols(symbols)
        priority_provider = (lambda _symbol: 0.0) if self.scanner_provider is not None else None
        ordered_symbols = self._candidate_selector(
            self._runtime_account_snapshot(),
            priority_provider=priority_provider,
            prescan=True,
        ).order_symbols(ranked_symbols)
        return ordered_symbols

    def _prime_entry_prices_from_authoritative_scanner(self, symbols: Iterable[str]) -> None:
        if not self._uses_authoritative_scanner() or self.scanner_provider is None:
            return
        requested_symbols = tuple(dict.fromkeys(symbols))
        requested_symbols = tuple(
            symbol for symbol in requested_symbols if symbol not in self._scanner_snapshot.bars
        )
        if not requested_symbols or requested_symbols in self._scanner_price_prime_keys:
            return
        self._scanner_price_prime_keys.add(requested_symbols)
        try:
            snapshot = self.scanner_provider.snapshot(requested_symbols)
        except Exception as exc:
            self._pending_scanner_rank_error = exc
            return

        self._scanner_snapshot = self._merge_scanner_snapshots(
            self._scanner_snapshot,
            snapshot,
            requested_symbols,
        )
        for bar in snapshot.bars.values():
            self._record_latest_entry_price(bar.symbol, bar)
        if snapshot.diagnostics.messages:
            self._pending_scanner_rank_error = RuntimeError("; ".join(snapshot.diagnostics.messages))

    def _cached_authoritative_scanner_snapshot(self, symbols: Iterable[str]) -> ScannerSnapshot | None:
        if not self._uses_authoritative_scanner():
            return None
        requested_symbols = list(dict.fromkeys(symbols))
        if not requested_symbols:
            return ScannerSnapshot()
        bars = {
            symbol: self._scanner_snapshot.bars[symbol]
            for symbol in requested_symbols
            if symbol in self._scanner_snapshot.bars
        }
        if len(bars) != len(requested_symbols):
            return None

        candidates_by_symbol = {
            candidate.symbol: candidate
            for candidate in self._scanner_snapshot.candidates
        }
        candidates = tuple(
            candidates_by_symbol.get(symbol, ScannerCandidate(symbol=symbol))
            for symbol in requested_symbols
        )
        return ScannerSnapshot(
            bars=bars,
            candidates=candidates,
            diagnostics=self._scanner_snapshot.diagnostics,
            histories={
                symbol: self._scanner_snapshot.histories[symbol]
                for symbol in requested_symbols
                if symbol in self._scanner_snapshot.histories
            },
        )

    def _symbols_from_cursor(
        self,
        ordered_symbols: list[str],
        *,
        limit: int | None,
        excluded: Iterable[str] = (),
    ) -> list[str]:
        if not ordered_symbols:
            return []
        parsed_limit = len(ordered_symbols) if limit is None else max(0, int(limit))
        if parsed_limit <= 0:
            return []

        excluded_symbols = set(excluded)
        selected: list[str] = []
        if self._scan_cursor_anchor and self._scan_cursor_anchor in ordered_symbols:
            start = ordered_symbols.index(self._scan_cursor_anchor)
        else:
            start = self._scan_cursor % len(ordered_symbols)
        cursor_advance = 0
        skip_passes = (True,) if self._skips_known_unaffordable_before_scan() else (False,)
        for skip_unaffordable in skip_passes:
            pass_inspected = 0
            for offset in range(len(ordered_symbols)):
                symbol = ordered_symbols[(start + offset) % len(ordered_symbols)]
                pass_inspected = offset + 1
                if symbol in excluded_symbols or symbol in selected:
                    continue
                if skip_unaffordable:
                    if self._known_entry_unavailable_after_current_scan_prime(symbol):
                        continue
                selected.append(symbol)
                cursor_advance = offset + 1
                if len(selected) >= parsed_limit:
                    break
            if len(selected) >= parsed_limit:
                break
            cursor_advance = max(cursor_advance, pass_inspected)

        self._scan_cursor = (start + cursor_advance) % len(ordered_symbols)
        self._scan_cursor_anchor = ordered_symbols[self._scan_cursor]
        return selected[:parsed_limit]

    def _scanner_bar_date(self, symbol: str):
        bar = self._scanner_snapshot.bars.get(symbol)
        if bar is None:
            return None
        timestamp = getattr(bar, "timestamp", None)
        date_getter = getattr(timestamp, "date", None)
        if not callable(date_getter):
            return None
        return date_getter()

    def _symbol_priority(self, symbol: str) -> float:
        scanner_priority = self._scanner_snapshot.candidate_priority(symbol)
        if scanner_priority is not None:
            return scanner_priority
        if self.symbol_priority_provider is None:
            return 0.0
        try:
            return float(self.symbol_priority_provider(symbol))
        except Exception:
            return 0.0

    def _order_budget_for_signal(
        self,
        signal: Signal,
        bar: MarketBar,
        account,
        *,
        reserved_entry_cash: Decimal = Decimal("0"),
    ) -> Decimal:
        if signal.side not in {"BUY", "SHORT_ENTRY"}:
            return self.settings.order_cash_amount
        budget = self._entry_budget_for_account(
            account,
            reserved_entry_cash=reserved_entry_cash,
        )
        position = account.positions.get(signal.symbol)
        price = entry_reference_price(bar, signal.side)
        if position is None or price is None or price <= 0:
            return budget
        if (signal.side == "BUY" and position.side != "LONG") or (
            signal.side == "SHORT_ENTRY" and position.side != "SHORT"
        ):
            return budget

        existing_amount = Decimal(position.quantity) * price
        additional_caps: list[Decimal] = []
        risk_config = getattr(self.risk_manager, "config", None)
        max_position_amount = Decimal(
            str(getattr(risk_config, "max_position_amount", Decimal("0")))
        )
        if max_position_amount > 0:
            additional_caps.append(max(Decimal("0"), max_position_amount - existing_amount))
        exposure_cap = Decimal(str(account.equity)) * self.settings.max_symbol_exposure
        if exposure_cap > 0:
            additional_caps.append(max(Decimal("0"), exposure_cap - existing_amount))
        if additional_caps:
            budget = min(budget, *additional_caps)
        return max(Decimal("0"), budget)

    def _entry_budget_for_account(
        self,
        account,
        *,
        reserved_entry_cash: Decimal = Decimal("0"),
    ) -> Decimal:
        allocatable_cash = self._remaining_cycle_allocation_cash(account)
        if allocatable_cash <= 0:
            return Decimal("0")
        budget = max(
            Decimal("0"),
            allocatable_cash - max(Decimal("0"), Decimal(str(reserved_entry_cash))),
        )
        return self._entry_budget_with_account_caps(account, budget)

    def _entry_budget_with_account_caps(self, account, budget: Decimal) -> Decimal:
        budget = min(
            max(Decimal("0"), Decimal(str(budget))),
            self._entry_budget_buying_power(account),
        )

        risk_config = getattr(self.risk_manager, "config", None)
        max_position_amount = getattr(risk_config, "max_position_amount", None)
        if max_position_amount is not None and Decimal(str(max_position_amount)) > 0:
            budget = min(budget, Decimal(str(max_position_amount)))

        exposure_cap = Decimal(
            str(getattr(account, "equity", self._entry_budget_buying_power(account)))
        ) * self.settings.max_symbol_exposure
        if exposure_cap > 0:
            budget = min(budget, exposure_cap)

        return max(Decimal("0"), budget)

    def _remaining_cycle_allocation_cash(self, account) -> Decimal:
        return max(Decimal("0"), self._entry_budget_buying_power(account))

    def _record_entry_budget_spend(self, fill) -> None:
        if not fill.accepted or fill.order.side not in {"BUY", "SHORT_ENTRY"}:
            return
        spend = Decimal(str(fill.price)) * Decimal(fill.quantity)
        self._cycle_entry_spent += spend
        self._cycle_entry_spend_by_symbol[fill.order.symbol] = (
            self._cycle_entry_spend_by_symbol.get(fill.order.symbol, Decimal("0")) + spend
        )
        self._cycle_entry_symbols.add(fill.order.symbol)
        side = "SHORT" if fill.order.side == "SHORT_ENTRY" else "LONG"
        self._cycle_entry_positions[fill.order.symbol] = Position(
            symbol=fill.order.symbol,
            quantity=fill.quantity,
            avg_price=fill.price,
            last_price=fill.price,
            opened_at=fill.timestamp,
            highest_price=fill.price,
            lowest_price=fill.price,
            side=side,
            sellable_quantity=fill.quantity,
            managed_quantity=fill.quantity,
        )

    def _record_pending_entry_budget_reservation(self, fill: Fill) -> None:
        if (
            not bool(getattr(fill, "pending_order_tracked", False))
            or fill.order.side not in {"BUY", "SHORT_ENTRY"}
            or fill.order.quantity <= 0
        ):
            return
        price = max(Decimal("0"), Decimal(str(getattr(fill, "price", Decimal("0")))))
        if price <= 0:
            price = max(
                Decimal("0"),
                Decimal(str(getattr(fill, "estimated_price", Decimal("0")))),
            )
        if price <= 0:
            return
        reserved = price * Decimal(fill.order.quantity)
        self._cycle_entry_spent += reserved
        self._cycle_entry_spend_by_symbol[fill.order.symbol] = (
            self._cycle_entry_spend_by_symbol.get(fill.order.symbol, Decimal("0"))
            + reserved
        )
        self._cycle_entry_symbols.add(fill.order.symbol)

    def _record_exit_overlay(self, fill) -> None:
        if not fill.accepted or fill.order.side not in {"SELL", "SHORT_EXIT"}:
            return
        if self.execution_mode == "live":
            self._clear_live_planning_buying_power_observation()
        self._cycle_exit_symbols.add(fill.order.symbol)
        self._cycle_exit_quantities[fill.order.symbol] = (
            self._cycle_exit_quantities.get(fill.order.symbol, 0) + int(fill.quantity)
        )

    def _entry_budget_buying_power(self, account) -> Decimal:
        buying_power = Decimal(str(getattr(account, "buying_power", Decimal("0"))))
        if self.execution_mode != "live":
            return buying_power

        cached_buying_power = getattr(self.broker, "cached_buying_power", None)
        if callable(cached_buying_power):
            try:
                buying_power = min(
                    buying_power,
                    max(Decimal("0"), Decimal(str(cached_buying_power()))),
                )
            except Exception:
                buying_power = Decimal("0")

        start_buying_power = getattr(self, "_cycle_start_buying_power", None)
        reflected_entry_spend = Decimal("0")
        if start_buying_power is not None:
            reflected_entry_spend = max(Decimal("0"), Decimal(str(start_buying_power)) - buying_power)
        unreflected_entry_spend = max(Decimal("0"), self._cycle_entry_spent - reflected_entry_spend)
        return max(Decimal("0"), buying_power - unreflected_entry_spend)

    def _refresh_live_planning_account(
        self,
        cycle_events: list[RuntimeEvent],
        market_bar: MarketBar,
    ) -> bool:
        if self.execution_mode != "live":
            return True
        if self._cycle_planning_buying_power_refreshed:
            return self._cycle_planning_buying_power_ready is not False
        refresh = getattr(self.broker, "refresh_planning_account", None)
        if not callable(refresh):
            self._cycle_planning_buying_power_refreshed = True
            self._cycle_planning_buying_power_ready = True
            return True

        self._cycle_planning_buying_power_refreshed = True
        account = self._runtime_account_snapshot()
        try:
            refreshed, blocker = refresh(account, market_bar)
        except Exception:
            refreshed = replace(account, buying_power_override=Decimal("0"))
            blocker = "live_buyable_inquiry_failed"

        self._cycle_account_snapshot = refreshed
        self._cycle_start_buying_power = Decimal(
            str(getattr(refreshed, "buying_power", Decimal("0")))
        )
        self._cycle_planning_buying_power_ready = not bool(blocker)
        if not blocker:
            self._last_live_planning_buying_power = max(
                Decimal("0"),
                self._cycle_start_buying_power,
            )
            self._last_live_planning_buying_power_at = datetime.now(timezone.utc)
            return True

        cycle_events.append(
            self._emit(
                RuntimeEvent.system(
                    f"live_planning_buying_power_failed - {blocker}"
                )
            )
        )
        return False

    def _account_with_cycle_overlays(self, account) -> AccountSnapshot:
        positions = self._positions_with_cycle_overlays(account)

        cash = Decimal(str(getattr(account, "cash", Decimal("0"))))
        equity_override = getattr(account, "equity_override", None)
        buying_power_override = getattr(account, "buying_power_override", None)
        if self.execution_mode == "live":
            live_buying_power = self._entry_budget_buying_power(account)
            buying_power_override = live_buying_power

        return AccountSnapshot(
            cash=max(Decimal("0"), cash),
            positions=positions,
            realized_pnl_today=getattr(account, "realized_pnl_today", Decimal("0")),
            realized_pnl_today_known=bool(
                getattr(account, "realized_pnl_today_known", True)
            ),
            equity_override=equity_override,
            buying_power_override=buying_power_override,
        )

    def _positions_with_cycle_overlays(self, account) -> dict[str, Position]:
        positions = dict(getattr(account, "positions", {}) or {})
        for symbol, exit_quantity in getattr(self, "_cycle_exit_quantities", {}).items():
            position = positions.get(symbol)
            if position is None:
                continue
            remaining_position = self._position_after_cycle_exit(position, exit_quantity)
            if remaining_position is None:
                positions.pop(symbol, None)
            else:
                positions[symbol] = remaining_position

        for symbol, entry_position in getattr(self, "_cycle_entry_positions", {}).items():
            existing = positions.get(symbol)
            positions[symbol] = (
                entry_position
                if existing is None
                else self._merge_cycle_entry_position(existing, entry_position)
            )
        return positions

    @staticmethod
    def _position_after_cycle_exit(position: Position, exit_quantity: int) -> Position | None:
        remaining_quantity = position.quantity - max(0, int(exit_quantity))
        if remaining_quantity <= 0:
            return None
        return Position(
            symbol=position.symbol,
            quantity=remaining_quantity,
            avg_price=position.avg_price,
            last_price=position.last_price,
            opened_at=position.opened_at,
            highest_price=position.highest_price,
            side=position.side,
            lowest_price=position.lowest_price,
            price_history=position.price_history,
            sellable_quantity=_subtract_optional_quantity(position.sellable_quantity, exit_quantity),
            managed_quantity=_subtract_optional_quantity(position.managed_quantity, exit_quantity),
        )

    @staticmethod
    def _merge_cycle_entry_position(existing: Position, entry: Position) -> Position:
        if existing.side != entry.side:
            return entry
        total_quantity = existing.quantity + entry.quantity
        if total_quantity <= 0:
            return entry
        avg_price = (
            (existing.avg_price * existing.quantity) + (entry.avg_price * entry.quantity)
        ) / Decimal(total_quantity)
        return Position(
            symbol=entry.symbol,
            quantity=total_quantity,
            avg_price=avg_price,
            last_price=entry.last_price,
            opened_at=existing.opened_at,
            highest_price=max(existing.highest_price, entry.highest_price),
            side=existing.side,
            lowest_price=min(existing.lowest_price or existing.last_price, entry.lowest_price or entry.last_price),
            price_history=existing.price_history + entry.price_history,
            sellable_quantity=_add_optional_quantity(existing.sellable_quantity, entry.sellable_quantity),
            managed_quantity=_add_optional_quantity(existing.managed_quantity, entry.managed_quantity),
        )

    def _remaining_entry_slots(self, account) -> int:
        configured_limit = self._position_limit()
        open_positions = self._position_count_with_cycle_entries(account)
        if configured_limit > 0:
            return max(0, configured_limit - open_positions)
        if self._cycle_entry_slot_target is None:
            if self._cycle_entry_sizing_slots is not None:
                return self._cycle_entry_sizing_slots
            if self._cycle_entry_slot_capacity is not None:
                return max(
                    0,
                    self._cycle_entry_slot_capacity - len(self._cycle_entry_symbols),
                )
            return self._unlimited_entry_slot_target()
        return max(1, self._cycle_entry_slot_target - open_positions)

    def _entry_sizing_slots_for_candidate(self, signal: Signal, bar: MarketBar, account) -> int:
        desired_slots = int(self._cycle_entry_sizing_slots or 0)
        if desired_slots <= 0:
            return 0
        price = entry_reference_price(bar, signal.side)
        allocatable_cash = self._remaining_cycle_allocation_cash(account)
        if price is None or price <= 0 or allocatable_cash <= 0:
            return 0
        affordable_slots = int(allocatable_cash / price)
        return max(0, min(desired_slots, affordable_slots))

    def _position_count_with_cycle_entries(self, account) -> int:
        return len(self._positions_with_cycle_overlays(account))

    def _unlimited_entry_slot_target(self) -> int:
        if self.scan_limit_per_cycle is not None:
            return max(1, min(len(self.symbols) or 1, self.scan_limit_per_cycle))
        return max(1, len(self.symbols))

    def _maximum_distinct_entry_candidates(
        self,
        candidates: Iterable[_EntryCandidate],
        *,
        available_cash: Decimal,
        limit: int,
    ) -> list[_EntryCandidate]:
        if limit <= 0 or available_cash <= 0:
            return []
        listed = list(candidates)
        original_order = {
            (candidate.signal.symbol, candidate.sequence): index
            for index, candidate in enumerate(listed)
        }
        affordable: list[tuple[_EntryCandidate, Decimal]] = []
        account = self._runtime_account_snapshot()
        per_symbol_cap = self._entry_budget_with_account_caps(account, available_cash)
        for candidate in listed:
            price = entry_reference_price(candidate.bar, candidate.signal.side)
            if price is None or price <= 0 or price > per_symbol_cap:
                continue
            affordable.append((candidate, price))

        cheapest_prices = sorted(price for _candidate, price in affordable)
        target_count = min(limit, len(cheapest_prices))
        while target_count > 0 and sum(cheapest_prices[:target_count], Decimal("0")) > available_cash:
            target_count -= 1

        selected: list[_EntryCandidate] = []
        remaining_cash = available_cash
        for index, (candidate, price) in enumerate(affordable):
            if len(selected) >= target_count:
                break
            needed_after = target_count - len(selected) - 1
            tail_prices = sorted(
                tail_price
                for _tail_candidate, tail_price in affordable[index + 1 :]
            )
            if len(tail_prices) < needed_after:
                continue
            minimum_tail_cash = sum(tail_prices[:needed_after], Decimal("0"))
            if price + minimum_tail_cash > remaining_cash:
                continue
            selected.append(candidate)
            remaining_cash -= price
        selected.sort(key=lambda candidate: original_order[(candidate.signal.symbol, candidate.sequence)])
        return selected

    def _candidate_selector(self, account, *, priority_provider=None, prescan: bool = False) -> CandidateSelector:
        order_cash_amount = self._entry_budget_for_account(account)
        if prescan and self._position_limit() <= 0 and self._cycle_entry_slot_target is None:
            order_cash_amount = self._entry_budget_with_account_caps(
                account,
                self._remaining_cycle_allocation_cash(account),
            )
        return CandidateSelector(
            latest_entry_prices=self._latest_entry_prices,
            latest_short_entry_prices=self._latest_short_entry_prices,
            order_cash_amount=order_cash_amount,
            priority_provider=priority_provider or self._symbol_priority,
            allow_short_entries=self.settings.allow_paper_short,
        )

    def _execute_signal(
        self,
        cycle_events: list[RuntimeEvent],
        signal: Signal,
        bar: MarketBar,
        *,
        reserved_entry_cash: Decimal = Decimal("0"),
    ) -> None:
        if signal.side == "SHORT_ENTRY" and not self.settings.allow_paper_short:
            account = self._runtime_account_snapshot()
            order = order_from_signal(
                signal,
                bar,
                account,
                ExecutionSettings(
                    order_cash_amount=self._order_budget_for_signal(
                        signal,
                        bar,
                        account,
                        reserved_entry_cash=reserved_entry_cash,
                    )
                ),
            )
            estimated_price = estimated_order_price(order, bar)
            self.metrics_tracker.record_rejection()
            cycle_events.append(self._emit_trade(order, estimated_price, "rejected", "paper_short_disabled", bar.timestamp))
            return

        confirmed_quote = self._final_quote_for_signal(cycle_events, signal, bar)
        if confirmed_quote is None:
            return
        provisional_bar = bar
        bar, requires_strategy_revalidation = confirmed_quote
        account = self._account_with_cycle_overlays(self._runtime_account_snapshot())
        if requires_strategy_revalidation:
            signal = self._revalidate_signal_at_final_quote(
                cycle_events,
                signal,
                provisional_bar,
                bar,
                account,
            )
            if signal is None:
                return
        final_exit_issue = self._exit_final_quote_issue(signal, bar, account)
        if final_exit_issue is not None:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_hold_summary - final_quote_exit_deferred: "
                        f"{self._label_for(signal.symbol)} reasons={final_exit_issue}"
                    )
                )
            )
            return
        order = order_from_signal(
            signal,
            bar,
            account,
            ExecutionSettings(
                order_cash_amount=self._order_budget_for_signal(
                    signal,
                    bar,
                    account,
                    reserved_entry_cash=reserved_entry_cash,
                )
            ),
        )
        estimated_price = estimated_order_price(order, bar)

        decision = self.risk_manager.check(
            order,
            account,
            estimated_price,
            as_of=bar.timestamp.date(),
        )
        if not decision.approved:
            self.metrics_tracker.record_rejection()
            cycle_events.append(self._emit_trade(order, estimated_price, "rejected", decision.reason, bar.timestamp))
            return

        fill = self.broker.place_order(order, bar)
        pending_order_tracked = bool(getattr(fill, "pending_order_tracked", False))
        if pending_order_tracked:
            self._cycle_pending_order_symbols.add(fill.order.symbol)
            if fill.order.side in {"SELL", "SHORT_EXIT"}:
                self._cycle_pending_sell_symbols.add(fill.order.symbol)
            elif fill.order.side in {"BUY", "SHORT_ENTRY"}:
                if fill.price <= 0:
                    fill = replace(fill, price=estimated_price)
                self._record_pending_entry_budget_reservation(fill)
            else:
                self._cycle_paused_for_live_pending_order = True
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_pending_order_scoped - "
                        f"symbol={fill.order.symbol}, side={fill.order.side}, "
                        f"new_entries_blocked={str(self._cycle_new_entries_blocked_for_live_pending_order).lower()}, "
                        f"cycle_paused={str(self._cycle_paused_for_live_pending_order).lower()}"
                    )
                )
            )

        requires_cycle_pause = bool(getattr(fill, "requires_cycle_pause", False))
        legacy_cycle_pause = (
            not pending_order_tracked
            and self._live_order_requires_cycle_pause(fill)
        )
        if requires_cycle_pause or legacy_cycle_pause:
            self._cycle_paused_for_live_pending_order = True

        if not fill.accepted and (
            pending_order_tracked
            or requires_cycle_pause
            or legacy_cycle_pause
        ):
            self.metrics_tracker.record_fill(fill)
            cycle_events.append(self._emit_trade(order, estimated_price, "rejected", fill.reject_reason, fill.timestamp))
            return

        self._record_risk_order_result(fill)
        if fill.accepted:
            self._record_entry_budget_spend(fill)
            self._record_exit_overlay(fill)
            self.metrics_tracker.record_fill(fill)
            if fill.order.side in {"BUY", "SHORT_ENTRY"}:
                self._record_entry_fill(fill)
            cycle_events.append(
                self._emit_trade(
                    fill.order,
                    fill.price,
                    "filled",
                    fill.order.reason,
                    fill.timestamp,
                    quantity=fill.quantity,
                    realized_pnl=fill.realized_pnl,
                )
            )
            return

        self.metrics_tracker.record_fill(fill)
        cycle_events.append(self._emit_trade(order, estimated_price, "rejected", fill.reject_reason, fill.timestamp))

    def _record_risk_order_result(self, fill: Fill) -> None:
        if fill.accepted:
            self.risk_manager.record_order_result(True)
            self._last_order_failure_class = "accepted"
            self._last_order_failure_reason = ""
            return

        reason = str(fill.reject_reason or "")
        if self._order_rejection_counts_toward_failure_limit(reason):
            self.risk_manager.record_order_result(False)
            self._last_order_failure_class = "hard_rejection"
        else:
            self._last_order_failure_class = "transient_or_preflight"
        self._last_order_failure_reason = reason

    def _order_rejection_counts_toward_failure_limit(self, reason: str) -> bool:
        if self.execution_mode != "live":
            return True
        if _is_kis_per_second_rate_limit_error(RuntimeError(reason)):
            return False
        normalized_reason = reason.strip().lower()
        if normalized_reason in _LIVE_ORDER_FAILURE_LOCK_EXEMPT_REASONS:
            return False
        if normalized_reason.startswith(_LIVE_ORDER_FAILURE_LOCK_EXEMPT_PREFIXES):
            return False
        return True

    def _live_order_requires_cycle_pause(self, fill: Fill) -> bool:
        if self.execution_mode != "live" or fill.accepted:
            return False
        reason = str(fill.reject_reason or "")
        if reason in LIVE_ORDER_CYCLE_PAUSE_REASONS:
            return True
        return any(reason.startswith(f"{pause_reason}:") for pause_reason in LIVE_ORDER_CYCLE_PAUSE_REASONS)

    def _final_quote_for_signal(
        self,
        cycle_events: list[RuntimeEvent],
        signal: Signal,
        scanner_bar: MarketBar,
    ) -> tuple[MarketBar, bool] | None:
        if signal.side in {"BUY", "SHORT_ENTRY"}:
            physical_issue = self._live_entry_market_read_budget_issue(
                signal.symbol,
                scanner_bar,
                include_final_quote=True,
                include_order_preflight=True,
            )
            if physical_issue is not None:
                self._cycle_physical_entry_capacity_exhausted = True
                cycle_events.append(
                    self._emit(
                        RuntimeEvent.system(
                            "scanner_diagnostic - physical_entry_capacity_reached: "
                            f"{self._label_for(signal.symbol)} deferred until the next cycle"
                        )
                    )
                )
                return None

        if self.final_quote_provider is None:
            market_trading_issue = self._live_market_trading_issue(
                scanner_bar,
                require_verified_state=True,
            )
            if market_trading_issue is not None:
                self._emit_market_trading_deferred(
                    cycle_events,
                    scanner_bar,
                    market_trading_issue,
                )
                return None
            if self._live_entry_quote_is_incomplete(signal, scanner_bar):
                self._emit_incomplete_final_quote(cycle_events, signal)
                return None
            return scanner_bar, False

        confirmed_scan_bar = self._confirmed_scan_bars_this_cycle.get(signal.symbol)
        if confirmed_scan_bar is not None:
            market_trading_issue = self._live_market_trading_issue(
                confirmed_scan_bar,
                require_verified_state=True,
            )
            if market_trading_issue is not None:
                self._emit_market_trading_deferred(
                    cycle_events,
                    confirmed_scan_bar,
                    market_trading_issue,
                )
                return None
            if self._live_entry_quote_is_incomplete(signal, confirmed_scan_bar):
                self._emit_incomplete_final_quote(cycle_events, signal)
                return None
            return confirmed_scan_bar, False

        if (
            self.max_final_quote_requests_per_cycle is not None
            and self._final_quote_requests_this_cycle >= self.max_final_quote_requests_per_cycle
        ):
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - final_quote_limit_reached: "
                        f"{self._label_for(signal.symbol)} deferred until the next cycle"
                    )
                )
            )
            return None

        self._final_quote_requests_this_cycle += 1
        try:
            final_bar = self.final_quote_provider(signal.symbol)
        except Exception as exc:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - final_quote_error: "
                        f"{self._label_for(signal.symbol)} {_market_data_error_message(exc)}"
                    )
                )
            )
            return None

        if final_bar is None:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - final_quote_unavailable: "
                        f"{self._label_for(signal.symbol)} deferred until the next cycle"
                    )
                )
            )
            return None

        market_trading_issue = self._live_market_trading_issue(
            final_bar,
            require_verified_state=True,
        )
        if market_trading_issue is not None:
            self._emit_market_trading_deferred(
                cycle_events,
                final_bar,
                market_trading_issue,
            )
            return None
        if self._live_entry_quote_is_incomplete(signal, final_bar):
            self._emit_incomplete_final_quote(cycle_events, signal)
            return None

        self._record_latest_entry_price(signal.symbol, final_bar)
        self.broker.update_market(final_bar)
        account = self._account_with_cycle_overlays(self._runtime_account_snapshot())
        if signal.side in {"BUY", "SHORT_ENTRY"}:
            entry_issue = self._entry_candidate_issue(signal, final_bar, account)
            if entry_issue is not None:
                cycle_events.append(
                    self._emit(
                        RuntimeEvent.system(
                            "scanner_hold_summary - final_quote_entry_deferred: "
                            f"{self._label_for(signal.symbol)} reasons={entry_issue}"
                        )
                    )
                )
                return None
        return final_bar, True

    def _live_entry_quote_is_incomplete(self, signal: Signal, bar: MarketBar) -> bool:
        return (
            self.execution_mode == "live"
            and signal.side in {"BUY", "SHORT_ENTRY"}
            and (bar.bid is None or bar.ask is None)
        )

    def _emit_incomplete_final_quote(
        self,
        cycle_events: list[RuntimeEvent],
        signal: Signal,
    ) -> None:
        cycle_events.append(
            self._emit(
                RuntimeEvent.system(
                    "scanner_diagnostic - final_quote_executable_quote_unavailable: "
                    f"{self._label_for(signal.symbol)} deferred until the next cycle"
                )
            )
        )

    def _live_market_trading_issue(
        self,
        bar: MarketBar,
        *,
        require_verified_state: bool = False,
    ) -> str | None:
        if self.execution_mode != "live":
            return None
        market = str(getattr(bar, "market", "") or "").strip().upper()
        source = str(
            getattr(bar, "trading_state_source", "") or ""
        ).strip().upper()
        temporary_stop = getattr(bar, "temporary_stop", None)
        if temporary_stop is True:
            issue = "temporary_stop"
            self._cycle_blocked_symbols.add(bar.symbol)
            self._cycle_symbol_trading_block_reasons[bar.symbol] = issue
        elif (
            require_verified_state
            and (
                source != "KIS_CURRENT_PRICE"
                or temporary_stop is not False
            )
        ) or (source and temporary_stop is None):
            issue = "trading_state_unknown"
            self._cycle_blocked_symbols.add(bar.symbol)
            self._cycle_symbol_trading_block_reasons[bar.symbol] = issue
        elif bar.symbol in self._cycle_blocked_symbols:
            return self._cycle_symbol_trading_block_reasons.get(
                bar.symbol,
                "temporary_stop",
            )
        else:
            return None
        key = (bar.symbol, market, issue)
        if key not in self._cycle_market_trading_block_keys:
            self._cycle_market_trading_block_keys.add(key)
            self._last_market_trading_block_reason = issue
            self._last_market_trading_block_market = market
            self._last_market_trading_block_symbol = bar.symbol
            self._last_market_trading_block_source = source
            self._last_market_trading_block_at = bar.timestamp
            self._market_trading_block_count += 1
        return issue

    def _emit_market_trading_deferred(
        self,
        cycle_events: list[RuntimeEvent],
        bar: MarketBar,
        reason: str,
    ) -> None:
        market = str(getattr(bar, "market", "") or "").strip().upper() or "UNKNOWN"
        cycle_events.append(
            self._emit(
                RuntimeEvent.system(
                    "market_trading_deferred - "
                    f"symbol={bar.symbol}, market={market}, reason={reason}, "
                    "scope=symbol, retry=next_cycle"
                )
            )
        )

    def _revalidate_signal_at_final_quote(
        self,
        cycle_events: list[RuntimeEvent],
        signal: Signal,
        provisional_bar: MarketBar,
        final_bar: MarketBar,
        account: AccountSnapshot,
    ) -> Signal | None:
        if self.execution_mode != "live":
            return signal
        revalidate_signal = getattr(self.strategy, "revalidate_live_signal", None)
        if not callable(revalidate_signal):
            revalidate_signal = getattr(self.strategy, "revalidate_signal", None)
        if not callable(revalidate_signal):
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - final_quote_strategy_revalidation_unavailable: "
                        f"{self._label_for(signal.symbol)} deferred until the next cycle"
                    )
                )
            )
            return None
        try:
            validated_signal = revalidate_signal(
                signal,
                provisional_bar,
                final_bar,
                account,
            )
        except Exception as exc:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_diagnostic - final_quote_strategy_revalidation_error: "
                        f"{self._label_for(signal.symbol)} {_market_data_error_message(exc)}"
                    )
                )
            )
            return None
        if (
            not isinstance(validated_signal, Signal)
            or validated_signal.symbol != signal.symbol
            or validated_signal.side != signal.side
        ):
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "scanner_hold_summary - final_quote_strategy_changed: "
                        f"{self._label_for(signal.symbol)} deferred until the next cycle"
                    )
                )
            )
            return None
        return validated_signal

    def _execute_signal_safely(
        self,
        cycle_events: list[RuntimeEvent],
        signal: Signal,
        bar: MarketBar,
        *,
        reserved_entry_cash: Decimal = Decimal("0"),
    ) -> None:
        try:
            self._execute_signal(
                cycle_events,
                signal,
                bar,
                reserved_entry_cash=reserved_entry_cash,
            )
        except Exception:
            cycle_events.append(self._symbol_processing_error(signal.symbol, "주문 처리 실패"))

    def _symbol_processing_error(self, symbol: str, reason: str) -> RuntimeEvent:
        return self._emit(RuntimeEvent.system(f"오류 - {self._label_for(symbol)}: {reason}"))

    def _finish_cycle(self, *, refresh_metrics: bool = True) -> None:
        if refresh_metrics:
            self._refresh_metrics()
        self._advance_live_planner_phase()
        self.cycle_count += 1
        self.last_update = datetime.now()
        self._cycle_account_snapshot = None
        self._end_physical_market_read_budget()
        self._end_pending_order_batch()

    def _begin_pending_order_batch(self) -> bool:
        begin = getattr(self.broker, "begin_pending_order_batch", None)
        if not callable(begin):
            return True
        try:
            begin()
        except Exception:
            return False
        return True

    def _end_pending_order_batch(self) -> None:
        end = getattr(self.broker, "end_pending_order_batch", None)
        if not callable(end):
            return
        try:
            end()
        except Exception:
            pass

    def _runtime_account_snapshot(self) -> AccountSnapshot:
        if self.execution_mode == "live" and self._cycle_account_snapshot is not None:
            return self._cycle_account_snapshot
        return self.broker.snapshot()

    def _sync_pending_live_orders(
        self,
        cycle_events: list[RuntimeEvent],
        *,
        exited_symbols: set[str] | None = None,
    ) -> bool:
        sync = getattr(self.broker, "sync_pending_order_statuses", None)
        if not callable(sync):
            self._last_pending_live_order_sync_summary = {
                "outcome": "not_supported",
                "remainingCount": 0,
                "entryBlockingCount": 0,
                "isolatedSellCount": 0,
                "fillCount": 0,
                "storeUnavailable": False,
                "syncUnavailable": False,
            }
            return False
        try:
            result = sync()
        except Exception as exc:
            error_detail = _safe_error_detail(exc)
            self._last_pending_live_order_sync_summary = {
                "outcome": "failed",
                "remainingCount": 0,
                "entryBlockingCount": 0,
                "isolatedSellCount": 0,
                "fillCount": 0,
                "storeUnavailable": False,
                "syncUnavailable": False,
                "errorType": exc.__class__.__name__,
                "errorDetail": error_detail,
            }
            detail_suffix = f", detail={error_detail}" if error_detail else ""
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_pending_order_sync_failed - "
                        f"error_type={exc.__class__.__name__}{detail_suffix}"
                    )
                )
            )
            return True

        fills = tuple(getattr(result, "fills", ()) or ())
        for fill in fills:
            if not isinstance(fill, Fill):
                continue
            self._record_risk_order_result(fill)
            self.metrics_tracker.record_fill(fill)
            if fill.accepted and fill.order.side in {"BUY", "SHORT_ENTRY"}:
                self._record_entry_budget_spend(fill)
                self._record_entry_fill(fill)
            if fill.accepted and fill.order.side in {"SELL", "SHORT_EXIT"}:
                self._record_exit_overlay(fill)
                if exited_symbols is not None:
                    exited_symbols.add(fill.order.symbol)
            cycle_events.append(
                self._emit_trade(
                    fill.order,
                    fill.price,
                    "filled" if fill.accepted else "rejected",
                    fill.order.reason if fill.accepted else fill.reject_reason,
                    fill.timestamp,
                    quantity=fill.quantity,
                    realized_pnl=fill.realized_pnl,
                )
            )

        remaining = tuple(getattr(result, "remaining", ()) or ())
        store_unavailable = bool(getattr(result, "store_unavailable", False))
        sync_unavailable = bool(getattr(result, "sync_unavailable", False))
        pending_buys = tuple(
            item
            for item in remaining
            if str(getattr(item, "side", "")).upper() == "BUY"
        )
        pending_buy_reservations = tuple(
            (item, _pending_buy_reserved_notional(item))
            for item in pending_buys
        )
        scoped_pending_buys = tuple(
            item
            for item, reservation in pending_buy_reservations
            if pending_buy_is_safely_scopable(item)
            and reservation is not None
        )
        pending_sells = tuple(
            item
            for item in remaining
            if str(getattr(item, "side", "")).upper() == "SELL"
        )
        isolated_sells = tuple(
            item for item in pending_sells if pending_order_is_safely_scopable(item)
        )
        blocking_remaining = tuple(
            item for item in pending_buys if item not in scoped_pending_buys
        ) + tuple(item for item in pending_sells if item not in isolated_sells)
        reserved_buy_notional = sum(
            (reservation or Decimal("0"))
            for item, reservation in pending_buy_reservations
            if item in scoped_pending_buys
        )
        unknown_side_orders = tuple(
            item
            for item in remaining
            if str(getattr(item, "side", "")).upper() not in {"BUY", "SELL"}
        )
        self._cycle_pending_order_symbols = {
            str(getattr(item, "symbol", ""))
            for item in remaining
            if str(getattr(item, "symbol", ""))
        }
        self._cycle_pending_sell_symbols = {
            str(getattr(item, "symbol", ""))
            for item in isolated_sells
            if str(getattr(item, "symbol", ""))
        }
        if store_unavailable:
            outcome = "store_unavailable"
        elif sync_unavailable:
            outcome = "sync_unavailable"
        elif unknown_side_orders:
            outcome = "unknown_side"
        elif blocking_remaining:
            outcome = "new_entries_blocked"
        elif scoped_pending_buys:
            outcome = "buy_isolated"
        elif isolated_sells:
            outcome = "sell_isolated"
        elif fills:
            outcome = "synced_with_fills"
        else:
            outcome = "clear"
        self._last_pending_live_order_sync_summary = {
            "outcome": outcome,
            "remainingCount": len(remaining),
            "entryBlockingCount": len(blocking_remaining),
            "isolatedBuyCount": len(scoped_pending_buys),
            "isolatedSellCount": len(isolated_sells),
            "reservedBuyNotional": reserved_buy_notional,
            "fillCount": len(fills),
            "storeUnavailable": store_unavailable,
            "syncUnavailable": sync_unavailable,
        }
        if store_unavailable:
            cycle_events.append(self._emit(RuntimeEvent.system("live_pending_order_store_unavailable")))
            return True
        if sync_unavailable:
            cycle_events.append(self._emit(RuntimeEvent.system("live_pending_order_sync_unavailable")))
            return True
        if unknown_side_orders:
            self._cycle_paused_for_live_pending_order = True
            self._last_pending_live_order_sync_summary["unknownSideCount"] = len(unknown_side_orders)
            symbols = ", ".join(
                str(getattr(item, "symbol", "")) for item in unknown_side_orders[:10]
            )
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_pending_order_side_unknown - "
                        f"pending_count={len(unknown_side_orders)}, symbols={symbols}, cycle_paused=true"
                    )
                )
            )
            return True
        if not self._begin_pending_order_batch():
            self._last_pending_live_order_sync_summary["outcome"] = "batch_start_failed"
            cycle_events.append(
                self._emit(RuntimeEvent.system("live_pending_order_batch_start_failed"))
            )
            return True
        if blocking_remaining:
            self._cycle_new_entries_blocked_for_live_pending_order = True
            symbols = ", ".join(str(getattr(item, "symbol", "")) for item in blocking_remaining[:10])
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_pending_orders_unresolved - "
                        f"pending_count={len(blocking_remaining)}, symbols={symbols}, "
                        "new_entries_blocked=true, unrelated_exits_enabled=true"
                    )
                )
            )
            return False
        if scoped_pending_buys:
            symbols = ", ".join(
                str(getattr(item, "symbol", ""))
                for item in scoped_pending_buys[:10]
            )
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_pending_buy_isolated - "
                        f"pending_count={len(scoped_pending_buys)}, symbols={symbols}, "
                        f"reserved_notional={reserved_buy_notional}, unrelated_entries_enabled=true"
                    )
                )
            )
        if remaining:
            symbols = ", ".join(str(getattr(item, "symbol", "")) for item in remaining[:10])
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"live_pending_sell_isolated - pending_count={len(remaining)}, symbols={symbols}"
                    )
                )
            )
        return False

    def _record_entry_fill(self, fill: Fill) -> None:
        trading_day = fill.timestamp.date()
        if self.execution_mode != "live":
            self.risk_manager.record_entry(fill.order.symbol, trading_day)
            return
        ledger = getattr(self.broker, "managed_position_ledger", None)
        if ledger is None:
            self.risk_manager.record_entry(fill.order.symbol, trading_day)
            return
        entry_counts = getattr(ledger, "entry_counts", None)
        if not callable(entry_counts):
            self.risk_manager.record_entry(fill.order.symbol, trading_day)
            return
        try:
            persisted_counts = entry_counts()
        except Exception:
            self.risk_manager.record_entry(fill.order.symbol, trading_day)
            return
        if int(persisted_counts.get((fill.order.symbol, trading_day), 0)) <= 0:
            self.risk_manager.record_entry(fill.order.symbol, trading_day)
            return
        self.risk_manager.restore_entry_counts(persisted_counts)

    def _sync_live_entry_count_state(self, cycle_events: list[RuntimeEvent]) -> bool:
        if self.execution_mode != "live":
            return True
        reconcile = getattr(self.broker, "reconcile_managed_entry_counts", None)
        if not callable(reconcile):
            return True
        try:
            reconciled = bool(reconcile())
        except Exception:
            reconciled = False
        if not reconciled:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_entry_count_reconciliation_pending - new BUY orders remain fail-closed"
                    )
                )
            )
            return False

        ledger = getattr(self.broker, "managed_position_ledger", None)
        entry_counts = getattr(ledger, "entry_counts", None)
        if not callable(entry_counts):
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_entry_count_reconciliation_pending - managed entry counts are unavailable"
                    )
                )
            )
            return False
        try:
            self.risk_manager.restore_entry_counts(entry_counts())
        except Exception:
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        "live_entry_count_reconciliation_pending - managed entry counts are unreadable"
                    )
                )
            )
            return False
        return True

    def _adopt_existing_live_positions(
        self,
        cycle_events: list[RuntimeEvent],
        *,
        account: AccountSnapshot,
    ) -> bool:
        if self.execution_mode != "live" or self._live_start_positions_adopted:
            return False
        adopt_existing_positions = getattr(self.broker, "adopt_existing_account_positions", None)
        if not callable(adopt_existing_positions):
            self._live_start_positions_adopted = True
            return False
        try:
            if callable(getattr(self.broker, "overlay_managed_positions", None)):
                adopted = adopt_existing_positions(account=account)
            else:
                adopted = adopt_existing_positions()
        except Exception as exc:
            detail = _safe_error_detail(exc) or exc.__class__.__name__
            cycle_events.append(
                self._emit(RuntimeEvent.system(f"live_existing_positions_adoption_failed - {detail}"))
            )
            return True
        self._live_start_positions_adopted = True
        overlay_managed_positions = getattr(self.broker, "overlay_managed_positions", None)
        if callable(overlay_managed_positions):
            self._cycle_account_snapshot = overlay_managed_positions(account)
            self._live_existing_positions_snapshot_refresh_needed = False
        else:
            self._live_existing_positions_snapshot_refresh_needed = True
        if adopted:
            cycle_events.append(
                self._emit(RuntimeEvent.system(f"live_existing_positions_adopted - count={len(adopted)}"))
            )
        return False

    def _emit_trade(
        self,
        order,
        price: Decimal,
        result: str,
        reason: str,
        timestamp: datetime,
        *,
        quantity: int | None = None,
        realized_pnl: Decimal = Decimal("0"),
    ) -> RuntimeEvent:
        return self._emit(
            RuntimeEvent.trade(
                symbol=order.symbol,
                company_name=self.symbol_directory.name_for(order.symbol),
                side=order.side,
                quantity=order.quantity if quantity is None else quantity,
                price=price,
                reason=reason,
                result=result,
                realized_pnl=realized_pnl,
                timestamp=timestamp,
            )
        )

    def _rate_limit_decision(self):
        if self.rate_limiter is None:
            return None
        return self.rate_limiter.allow_request("market_data_cycle")

    def _rate_limit_blocks_cycle(self, decision) -> bool:
        return not (self.execution_mode == "live" and getattr(decision, "reason", "") == "min_interval")

    def _live_account_snapshot_or_rate_limit_skip(
        self,
        cycle_events: list[RuntimeEvent],
        *,
        stage: str,
    ) -> AccountSnapshot | None:
        try:
            return self.broker.snapshot()
        except Exception as exc:
            if self.execution_mode != "live" or not _is_kis_per_second_rate_limit_error(exc):
                raise
            retry_after = self._record_live_account_rate_limit_error()
            cycle_events.append(
                self._emit(
                    RuntimeEvent.system(
                        f"rate_limit_skip - live_account_snapshot: stage={stage}, retry_after={retry_after:.1f}s"
                    )
                )
            )
            return None

    def _record_live_account_rate_limit_error(self) -> float:
        retry_after = 1.5
        recorder = getattr(self.rate_limiter, "record_rate_limit_error", None)
        if callable(recorder):
            try:
                recorder(retry_after)
            except Exception:
                pass
        decision = self._rate_limit_decision()
        if decision is not None and not decision.allowed:
            retry_after = max(retry_after, float(decision.retry_after_seconds))
        return retry_after

    def _market_session_status(self) -> MarketSessionStatus | None:
        if self.market_hours is None:
            return None
        return self.market_hours.status()

    def _record_rate_limited_request(self) -> None:
        if self.rate_limiter is not None:
            self.rate_limiter.record_request("market_data_cycle")

    def _hold_detail(self, symbol: str) -> str:
        score_getter = getattr(self.strategy, "last_entry_score", None)
        if not callable(score_getter):
            return ""
        try:
            score = score_getter(symbol)
        except Exception:
            return "(score unavailable)"
        if score is None:
            return ""
        try:
            reasons = ", ".join(_score_reason_label(reason) for reason in score.reasons)
            direction = _score_direction_label(str(score.direction))
            return f"(방향={direction}, 신뢰도={score.confidence:.2f}, 사유={reasons})"
        except Exception:
            return "(score unavailable)"

    def _hold_reason_codes(self, symbol: str) -> tuple[str, ...]:
        score_getter = getattr(self.strategy, "last_entry_score", None)
        if not callable(score_getter):
            return ("no_entry_signal",)
        try:
            score = score_getter(symbol)
        except Exception:
            return ("score_unavailable",)
        if score is None:
            return ("no_entry_signal",)
        reasons = tuple(str(reason) for reason in getattr(score, "reasons", ()) if str(reason).strip())
        return reasons or ("no_entry_signal",)

    def _hold_summary_detail(self, reason_counts: Counter[str]) -> str:
        if not reason_counts:
            return ""
        parts: list[str] = []
        for reason, count in reason_counts.most_common(5):
            parts.append(f"{reason}={count}")
        return ", ".join(parts)

    def _diagnostic_reason_counts_detail(self, reason_counts: Counter[str]) -> str:
        if not reason_counts:
            return "none"
        parts: list[str] = []
        for reason, count in reason_counts.most_common(5):
            parts.append(f"{reason}:{count}")
        return ",".join(parts)

    def _refresh_metrics(self) -> None:
        account = self._account_with_cycle_overlays(self._runtime_account_snapshot())
        self.performance_metrics = self.metrics_tracker.snapshot(account)
        self._latest_cycle_account_snapshot = account

    def _label_for(self, symbol: str) -> str:
        return self.symbol_directory.label_for(symbol)

    def _emit(self, event: RuntimeEvent) -> RuntimeEvent:
        if event.mode != self.execution_mode:
            event = replace(event, mode=self.execution_mode)
        self.events.append(event)
        return event


_SCORE_DIRECTION_LABELS = {
    "hold": "관망",
    "long": "롱",
    "short": "숏",
}

_SCORE_REASON_LABELS = {
    "insufficient_data": "데이터 누적 중",
    "insufficient_trend_boundary": "추세선 계산 데이터 부족",
    "invalid_reference_price": "기준 가격 오류",
    "invalid_average_volume": "평균 거래량 오류",
    "volume_below_minimum": "거래량 기준 미달",
    "wide_spread": "호가 차이 과다",
    "expected_net_profit_below_costs": "비용 차감 기대수익 미달",
    "upward_momentum": "상승 모멘텀",
    "downward_momentum": "하락 모멘텀",
    "close_strength": "종가 강도",
    "bullish_regime": "상승 우위 흐름",
    "bearish_regime": "하락 우위 흐름",
    "volume_expansion": "거래량 증가",
    "spread_allowed": "호가 차이 허용 범위",
    "signal_confidence_below_minimum": "signal confidence below minimum",
}


def _score_direction_label(direction: str) -> str:
    return _SCORE_DIRECTION_LABELS.get(direction, direction)


def _score_reason_label(reason: str) -> str:
    label = _SCORE_REASON_LABELS.get(reason)
    if label is None:
        return reason
    return f"{label} ({reason})"


def _summary_reason_key(reason: str) -> str:
    normalized = str(reason or "").strip()
    if not normalized:
        return "hold"
    return normalized.split(":", 1)[0].strip() or "hold"


def _subtract_optional_quantity(quantity: int | None, amount: int) -> int | None:
    if quantity is None:
        return None
    return max(0, int(quantity) - max(0, int(amount)))


def _add_optional_quantity(left: int | None, right: int | None) -> int | None:
    if left is None and right is None:
        return None
    return max(0, int(left or 0) + int(right or 0))


def _pending_buy_reserved_notional(pending: object) -> Decimal | None:
    try:
        remaining_quantity = int(getattr(pending, "remaining_quantity", 0) or 0)
        estimated_price = Decimal(
            str(getattr(pending, "estimated_price", Decimal("0")) or "0")
        )
    except (TypeError, ValueError, ArithmeticError):
        return None
    if remaining_quantity <= 0 or estimated_price <= 0:
        return None
    return estimated_price * Decimal(remaining_quantity)


_SENSITIVE_ERROR_PATTERNS = (
    re.compile(r"\bauthorization\b\s*[:=]\s*bearer\s+[^\s,;]+", re.IGNORECASE),
    re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE),
    re.compile(
        r"\b(app[_\s-]?secret|app[_\s-]?key|api[_\s-]?key|authorization|bearer|token)\b[:=\s]+[^,\s]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(KIS_[A-Z0-9_]*(?:KEY|SECRET|TOKEN|ACCOUNT)[A-Z0-9_]*|STOCKBOT_[A-Z0-9_]*(?:ACCOUNT|CONFIRM)[A-Z0-9_]*|account(?:[_\s-]?(?:no|number))?|accountno|acct|cano|acnt)\b\s*[:=]\s*[^,\s]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:secret|token)[-_][A-Za-z0-9._-]+\b|\b[A-Za-z0-9._-]+[-_](?:secret|token)(?:[-_][A-Za-z0-9._-]+)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bKIS_[A-Z0-9_]*(KEY|SECRET|TOKEN)[A-Z0-9_]*\s*=\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"[A-Z]:\\[^\r\n]*", re.IGNORECASE),
    re.compile(r"\b\d{8,}(?:-\d{2})?\b"),
)


def _market_data_error_message(exc: Exception) -> str:
    detail = _safe_error_detail(exc)
    if not detail:
        return "데이터 조회 실패"
    return f"데이터 조회 실패 - {detail}"


def _is_kis_per_second_rate_limit_error(exc: Exception) -> bool:
    message = str(exc)
    if "KIS local rate limit" in message:
        return True
    return "EGW00215" in message or "EGW00201" in message or "초당 거래건수" in message


def _bar_timestamp_key(bar: MarketBar) -> float:
    timestamp = bar.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


def _bar_minute_bucket_key(bar: MarketBar) -> int:
    return int(_bar_timestamp_key(bar) // 60)


def _bar_trading_day_key(bar: MarketBar) -> str:
    return bar.timestamp.date().isoformat()


def _bars_have_contiguous_minute_buckets(bars: list[MarketBar]) -> bool:
    buckets = [_bar_minute_bucket_key(bar) for bar in bars]
    return all(
        current == previous + 1
        for previous, current in zip(buckets, buckets[1:])
    )


def _intraday_proxy_history_from_quote(
    bar: MarketBar,
    count: int,
    *,
    volume_ratio_floor: Decimal = Decimal("1"),
) -> list[MarketBar]:
    parsed_count = max(0, int(count))
    if parsed_count <= 0 or bar.open <= 0 or bar.close <= 0:
        return []

    bars: list[MarketBar] = []
    price_step = (bar.close - bar.open) / Decimal(parsed_count + 1)
    volume = max(0, int(bar.volume))
    proxy_volume = volume if volume > 0 else 1
    parsed_volume_ratio_floor = Decimal(str(volume_ratio_floor or Decimal("1")))
    if volume > 1 and parsed_volume_ratio_floor > Decimal("1"):
        target_ratio = parsed_volume_ratio_floor + Decimal("0.01")
        proxy_volume = max(
            1,
            int((Decimal(volume) / target_ratio).to_integral_value(rounding=ROUND_FLOOR)),
        )
    for index in range(parsed_count):
        close = bar.open + (price_step * Decimal(index + 1))
        timestamp = bar.timestamp - timedelta(minutes=parsed_count - index)
        bars.append(
            replace(
                bar,
                timestamp=timestamp,
                open=close,
                high=close,
                low=close,
                close=close,
                volume=proxy_volume,
                vwap=close,
                bid=close,
                ask=close,
            )
        )
    return bars


def _safe_error_detail(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return ""
    for pattern in _SENSITIVE_ERROR_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text or text == "[REDACTED]":
        return ""
    return text[:240]
