from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .config import BotConfig
from .kis import KisApiError, KisLiveOrderClient, KisOrderSubmissionUncertain
from .live_audit import JsonlLiveAuditLog
from .live_order_state import (
    ManualReconciliationBlocker,
    ManualReconciliationStore,
    PendingLiveOrder,
    PendingLiveOrderStore,
    pending_order_is_safely_scopable,
    pending_order_reason_is_safely_scopable,
)
from .live_position_ledger import ManagedLivePositionLedger
from .live_reconciliation import LiveOrderReconciler, extract_live_order_number, extract_live_order_org_number
from .live_safety import LiveOrderPreflightRequest, assess_live_order_preflight, live_order_gate_configured
from .kis_models import parse_kis_price_bar
from .models import AccountSnapshot, Fill, MarketBar, Order
from .redaction import redact_sensitive_text


BoolProvider = bool | Callable[[], bool]
LIVE_PENDING_ORDER_CANCEL_AFTER = timedelta(minutes=2)
KOREA_TIMEZONE = timezone(timedelta(hours=9))
KRW_WON = Decimal("1")
# Common KRX stock tick sizes for KOSPI/KOSDAQ/NXT equities. ETF/ETN/ELW products
# have separate tick rules and should get an explicit product-aware path first.
KRX_STOCK_TICK_SIZE_BANDS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000"), Decimal("1")),
    (Decimal("5000"), Decimal("5")),
    (Decimal("20000"), Decimal("10")),
    (Decimal("50000"), Decimal("50")),
    (Decimal("200000"), Decimal("100")),
    (Decimal("500000"), Decimal("500")),
)
_MARKET_STATE_REJECTION_TOKENS = (
    "circuit breaker",
    "market halt",
    "temporary stop",
    "trading halt",
    "vi triggered",
    "매매거래정지",
    "거래정지",
    "서킷브레이커",
    "임시정지",
    "변동성완화",
    "vi발동",
)


@dataclass(frozen=True)
class _CancelablePendingOrder:
    quantity: int = 0
    order_org_no: str = ""
    checked: bool = False


@dataclass(frozen=True)
class _LiveExecutionPrice:
    reference_price: Decimal
    submitted_price: Decimal
    slippage_pct: Decimal
    blocker: str = ""


@dataclass(frozen=True)
class LivePendingOrderSyncResult:
    remaining: tuple[PendingLiveOrder, ...] = ()
    fills: tuple[Fill, ...] = ()
    store_unavailable: bool = False
    sync_unavailable: bool = False

    @property
    def unavailable_reason(self) -> str:
        if self.store_unavailable:
            return "live_pending_order_store_unavailable"
        if self.sync_unavailable:
            return "live_pending_order_sync_unavailable"
        return ""


def _live_execution_price(order: Order, bar: MarketBar, config: BotConfig) -> _LiveExecutionPrice:
    reference = bar.buy_price if order.side == "BUY" else bar.sell_price
    slippage = max(Decimal("0"), Decimal(str(config.slippage_pct)))
    if reference <= 0:
        return _LiveExecutionPrice(reference, reference, slippage)

    if order.side == "BUY":
        if bar.upper_limit is not None and bar.upper_limit > 0 and reference > bar.upper_limit:
            return _LiveExecutionPrice(
                reference,
                reference,
                slippage,
                "live_quote_above_daily_upper_limit",
            )
        submitted = reference * (Decimal("1") + slippage)
        submitted = _round_krx_stock_order_price(submitted, rounding=ROUND_CEILING)
        if bar.upper_limit is not None and bar.upper_limit > 0:
            submitted = min(submitted, bar.upper_limit)
        submitted = max(reference, submitted)
    else:
        if bar.lower_limit is not None and bar.lower_limit > 0 and reference < bar.lower_limit:
            return _LiveExecutionPrice(
                reference,
                reference,
                slippage,
                "live_quote_below_daily_lower_limit",
            )
        submitted = reference * (Decimal("1") - slippage)
        submitted = _round_krx_stock_order_price(submitted, rounding=ROUND_FLOOR)
        if bar.lower_limit is not None and bar.lower_limit > 0:
            submitted = max(submitted, bar.lower_limit)
        submitted = min(reference, submitted)
        submitted = max(KRW_WON, submitted)
    return _LiveExecutionPrice(reference, submitted, slippage)


def _round_krx_stock_order_price(price: Decimal, *, rounding: str) -> Decimal:
    tick_size = _krx_stock_tick_size(price)
    ticks = (price / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size


def _krx_stock_tick_size(price: Decimal) -> Decimal:
    for upper_bound, tick_size in KRX_STOCK_TICK_SIZE_BANDS:
        if price < upper_bound:
            return tick_size
    return Decimal("1000")


def _live_execution_price_payload(execution_price: _LiveExecutionPrice) -> dict[str, str]:
    return {
        "reference_price": str(execution_price.reference_price),
        "submitted_price": str(execution_price.submitted_price),
        "slippage_pct": str(execution_price.slippage_pct),
    }


def _live_market_state_blocker(bar: MarketBar) -> str:
    temporary_stop = getattr(bar, "temporary_stop", None)
    source = str(
        getattr(bar, "trading_state_source", "") or ""
    ).strip().upper()
    if temporary_stop is True:
        return "live_market_temporarily_stopped"
    if source != "KIS_CURRENT_PRICE" or temporary_stop is not False:
        return "live_market_state_unknown"
    return ""


def _reconciled_live_reject_reason(status: str, detail: str) -> str:
    normalized_status = str(status or "").strip().lower() or "unknown"
    safe_detail = str(detail or "").strip()
    if normalized_status != "rejected":
        return f"live_order_{normalized_status}"
    if not safe_detail:
        return "live_order_rejected"
    normalized_detail = "".join(safe_detail.lower().split())
    if any(
        "".join(token.lower().split()) in normalized_detail
        for token in _MARKET_STATE_REJECTION_TOKENS
    ):
        return f"live_market_state_rejected: {safe_detail}"
    return f"live_order_rejected: {safe_detail}"


class LiveBroker:
    """Fail-closed broker adapter for real KIS orders.

    UI state is not trusted as a safety boundary. Every order must pass
    assess_live_order_preflight immediately before the KIS order call.
    """

    def __init__(
        self,
        *,
        client: KisLiveOrderClient,
        config: BotConfig,
        env: Mapping[str, str | None],
        audit_log: JsonlLiveAuditLog,
        market_is_open: BoolProvider,
        session_approved: BoolProvider = False,
        account_confirmation: str = "",
        expected_account_suffix: str = "",
        fill_reconciliation_available: BoolProvider = False,
        fill_reconciler: LiveOrderReconciler | None = None,
        pending_order_store: PendingLiveOrderStore | None = None,
        manual_reconciliation_store: ManualReconciliationStore | None = None,
        managed_position_ledger: ManagedLivePositionLedger | None = None,
        risk_limits_ok: BoolProvider = False,
        new_entries_allowed: BoolProvider = False,
    ):
        self.client = client
        self.config = config
        self.env = env
        self.audit_log = audit_log
        self.market_is_open = market_is_open
        self.session_approved = session_approved
        self.account_confirmation = account_confirmation
        self.expected_account_suffix = expected_account_suffix
        self.fill_reconciliation_available = fill_reconciliation_available
        self.fill_reconciler = fill_reconciler
        self.pending_order_store = pending_order_store
        self.manual_reconciliation_store = manual_reconciliation_store
        self.managed_position_ledger = managed_position_ledger
        self.risk_limits_ok = risk_limits_ok
        self.new_entries_allowed = new_entries_allowed
        self._redact_values = tuple(str(value) for value in env.values() if value)
        self._manual_reconciliation_blocker = ""
        self._manual_reconciliation_blocker_source = ""
        self._adopted_existing_position_symbols: set[str] = set()
        self._cached_orderable_cash: Decimal | None = None
        self._pending_order_batch_active = False

    def begin_pending_order_batch(self) -> None:
        self._pending_order_batch_active = True
        pending_state = self._pending_order_state_for_preflight()
        has_pending_buy = any(
            str(getattr(pending, "side", "")).strip().upper() == "BUY"
            for pending in pending_state.remaining
        )
        if pending_state.unavailable_reason:
            self._cached_orderable_cash = Decimal("0")
            return
        if has_pending_buy:
            if self._cached_orderable_cash is None:
                self._cached_orderable_cash = Decimal("0")
            return
        self._cached_orderable_cash = None

    def end_pending_order_batch(self) -> None:
        self._pending_order_batch_active = False

    def snapshot(self, *, timestamp: datetime | None = None) -> AccountSnapshot:
        account = self._normalize_account_snapshot(
            self.client.account_snapshot(timestamp=timestamp)
        )
        explicit_buying_power = getattr(account, "buying_power_override", None)
        if self._cached_orderable_cash is not None and (
            self._pending_order_batch_active or explicit_buying_power is None
        ):
            account = replace(account, buying_power_override=self._cached_orderable_cash)
        elif explicit_buying_power is None:
            account = replace(account, buying_power_override=Decimal("0"))
        return self._with_managed_quantities(account, timestamp=timestamp)

    def cached_buying_power(self) -> Decimal:
        if self._cached_orderable_cash is None:
            return Decimal("0")
        return max(Decimal("0"), self._cached_orderable_cash)

    def reconcile_managed_entry_counts(self, trading_day: date | None = None) -> bool:
        target_day = trading_day or datetime.now(tz=KOREA_TIMEZONE).date()
        ledger = self.managed_position_ledger
        if ledger is None or not bool(getattr(ledger, "is_durable", False)):
            self._record_audit_best_effort(
                "live_entry_count_reconciliation_failed",
                {"reason": "managed live position ledger is unavailable"},
            )
            return False

        entry_counts_are_known = getattr(ledger, "entry_counts_are_known", None)
        replace_entry_counts = getattr(ledger, "replace_entry_counts_for_date", None)
        reconcile_entry_counts = getattr(self.fill_reconciler, "reconcile_entry_counts", None)
        if not callable(entry_counts_are_known) or not callable(replace_entry_counts):
            self._record_audit_best_effort(
                "live_entry_count_reconciliation_failed",
                {"reason": "managed entry count state is unavailable"},
            )
            return False

        try:
            ledger.ensure_ready()
            if bool(entry_counts_are_known(target_day)):
                return True
            if not callable(reconcile_entry_counts):
                raise RuntimeError("authoritative KIS entry count reconciliation is unavailable")
            reconciliation = reconcile_entry_counts(target_day)
            if getattr(reconciliation, "trading_day", None) != target_day:
                raise ValueError("KIS entry count reconciliation returned the wrong trading day")
            counts = getattr(reconciliation, "entry_counts", None)
            if not isinstance(counts, Mapping):
                raise ValueError("KIS entry count reconciliation returned invalid counts")
            replace_entry_counts(target_day, counts)
            if not bool(entry_counts_are_known(target_day)):
                raise RuntimeError("managed entry count reconciliation was not persisted")
        except Exception as exc:
            self._record_audit_best_effort(
                "live_entry_count_reconciliation_failed",
                {"reason": self._redact(str(exc))},
            )
            return False

        self._record_audit_best_effort(
            "live_entry_count_reconciled",
            {
                "trading_day": target_day.isoformat(),
                "symbol_count": len(counts),
                "entry_count": sum(max(0, int(count)) for count in counts.values()),
            },
        )
        return True

    @staticmethod
    def _normalize_account_snapshot(account: object) -> AccountSnapshot:
        if isinstance(account, AccountSnapshot):
            return account

        equity = getattr(account, "equity", None)
        return AccountSnapshot(
            cash=Decimal(str(getattr(account, "cash", Decimal("0")))),
            positions=dict(getattr(account, "positions", {}) or {}),
            realized_pnl_today=Decimal(
                str(getattr(account, "realized_pnl_today", Decimal("0")))
            ),
            realized_pnl_today_known=bool(
                getattr(account, "realized_pnl_today_known", False)
            ),
            equity_override=Decimal(str(equity)) if equity is not None else None,
            buying_power_override=getattr(account, "buying_power_override", None),
        )

    def account_with_fresh_buying_power(
        self,
        account: AccountSnapshot,
        market_bar: MarketBar,
    ) -> tuple[AccountSnapshot, str]:
        order = Order.buy(market_bar.symbol, 1, "live_planning_buying_power")
        execution_price = _live_execution_price(order, market_bar, self.config)
        if execution_price.blocker:
            self._cached_orderable_cash = None
            return replace(account, buying_power_override=Decimal("0")), execution_price.blocker
        return self._account_after_buyable_inquiry(
            order,
            execution_price.submitted_price,
            account,
            validate_order=False,
        )

    def refresh_planning_account(
        self,
        account: AccountSnapshot,
        market_bar: MarketBar,
    ) -> tuple[AccountSnapshot, str]:
        return self.account_with_fresh_buying_power(account, market_bar)

    def adopt_existing_account_positions(
        self,
        *,
        timestamp: datetime | None = None,
        account: AccountSnapshot | None = None,
    ) -> dict[str, int]:
        self._adopted_existing_position_symbols = set()
        blockers = self._existing_position_adoption_blockers()
        if blockers:
            self._record_audit_best_effort(
                "live_existing_positions_adoption_denied",
                {"blockers": blockers},
            )
            raise RuntimeError("live_existing_positions_adoption_denied: " + ", ".join(blockers))

        pending_sync = self._pending_order_state_for_preflight()
        pending_unavailable_reason = pending_sync.unavailable_reason
        pending_entry_symbols = {
            str(getattr(pending, "symbol", ""))
            for pending in pending_sync.remaining
            if str(getattr(pending, "side", "")).upper() == "BUY"
        }
        pending_sell_symbols = {
            str(getattr(pending, "symbol", ""))
            for pending in pending_sync.remaining
            if str(getattr(pending, "side", "")).upper() == "SELL"
        }
        unknown_pending = tuple(
            pending
            for pending in pending_sync.remaining
            if str(getattr(pending, "side", "")).upper() not in {"BUY", "SELL"}
        )
        if pending_unavailable_reason or pending_sync.fills or unknown_pending:
            blockers = []
            if pending_unavailable_reason:
                blockers.append(pending_unavailable_reason)
            if pending_sync.fills:
                blockers.append("live_pending_order_fills_require_runtime_sync")
            if unknown_pending:
                blockers.append("live_pending_order_side_unknown")
            self._record_audit_best_effort(
                "live_existing_positions_adoption_denied",
                {
                    "blockers": tuple(blockers),
                    "pending_count": len(pending_sync.remaining),
                    "filled_count": len(pending_sync.fills),
                },
            )
            raise RuntimeError("live_existing_positions_adoption_denied: " + ", ".join(blockers))

        if account is None:
            try:
                account = self.client.account_snapshot(timestamp=timestamp)
            except Exception as exc:
                reason = self._redact(f"live_snapshot_failed: {exc}")
                self._record_audit_best_effort(
                    "live_existing_positions_adoption_snapshot_failed",
                    {"reason": reason},
                )
                raise RuntimeError(reason) from exc
        account = self._normalize_account_snapshot(account)

        if not self._managed_position_ledger_ready():
            self._record_audit_best_effort(
                "live_existing_positions_adoption_denied",
                {"blockers": ("managed live position ledger is not available",)},
            )
            raise RuntimeError("live_existing_positions_adoption_denied: managed live position ledger is not available")

        try:
            targets, skipped_symbols = self._adoptable_existing_position_quantities(account)
            for symbol in pending_entry_symbols:
                targets.pop(symbol, None)

            self.managed_position_ledger.ensure_ready()
            current_managed = self.managed_position_ledger.all()
            preserved_pending_sells: set[str] = set()
            for symbol in pending_sell_symbols:
                current_quantity = max(0, int(current_managed.get(symbol, 0)))
                if current_quantity <= 0:
                    continue
                account_position = account.positions.get(symbol)
                account_quantity = max(
                    0,
                    int(getattr(account_position, "quantity", 0)) if account_position is not None else 0,
                )
                if current_quantity > account_quantity:
                    raise RuntimeError(
                        "pending SELL account quantity is below the managed quantity"
                    )
                targets[symbol] = max(targets.get(symbol, 0), current_quantity)
                preserved_pending_sells.add(symbol)

            self._align_managed_position_ledger(
                targets,
                account=account,
                timestamp=timestamp,
                preserve_symbols=pending_entry_symbols | preserved_pending_sells,
                unknown_symbols=set(skipped_symbols),
            )
        except Exception as exc:
            reason = self._redact(str(exc))
            self._record_audit_best_effort(
                "live_existing_positions_adoption_failed",
                {"reason": reason},
            )
            raise RuntimeError(f"live_existing_positions_adoption_failed: {reason}") from exc

        self._record_audit_best_effort(
            "live_existing_positions_adopted",
            {
                "adopted_count": len(targets),
                "symbols": tuple(sorted(targets)[:20]),
                "skipped_count": len(skipped_symbols),
                "skipped_symbols": tuple(sorted(skipped_symbols)[:20]),
                "pending_entry_excluded_count": len(pending_entry_symbols),
                "pending_entry_excluded_symbols": tuple(sorted(pending_entry_symbols)[:20]),
                "pending_sell_preserved_count": len(preserved_pending_sells),
                "pending_sell_preserved_symbols": tuple(sorted(preserved_pending_sells)[:20]),
            },
        )
        self._adopted_existing_position_symbols = set(targets)
        return dict(targets)

    def overlay_managed_positions(
        self,
        account: AccountSnapshot,
        *,
        timestamp: datetime | None = None,
    ) -> AccountSnapshot:
        return self._with_managed_quantities(
            self._normalize_account_snapshot(account),
            timestamp=timestamp,
        )

    def update_market(self, bar: MarketBar) -> None:
        if self.managed_position_ledger is None:
            return None
        try:
            self.managed_position_ledger.ensure_ready()
            if self.managed_position_ledger.quantity_for(bar.symbol) > 0:
                self.managed_position_ledger.update_lifecycle_price(
                    bar.symbol,
                    bar.timestamp,
                    bar.close,
                )
        except Exception as exc:
            self._record_audit_best_effort(
                "live_position_lifecycle_update_failed",
                {"symbol": bar.symbol, "reason": self._redact(str(exc))},
            )
        return None

    def sync_pending_orders(
        self,
        *,
        query_date: date | None = None,
    ) -> tuple[PendingLiveOrder, ...]:
        return self.sync_pending_order_statuses(query_date=query_date).remaining

    def sync_pending_order_statuses(
        self,
        *,
        query_date: date | None = None,
        consume_fills: bool = True,
    ) -> LivePendingOrderSyncResult:
        if not self._fill_reconciliation_ready():
            self.audit_log.record("live_pending_order_sync_skipped", {"reason": "reconciliation_unavailable"})
            return LivePendingOrderSyncResult(
                store_unavailable=self.fill_reconciler is not None,
                sync_unavailable=True,
            )
        if self.fill_reconciler is None or self.pending_order_store is None:
            return LivePendingOrderSyncResult()

        fills: list[Fill] = []
        sync_unavailable = False
        logically_remaining_order_numbers: set[str] = set()
        logically_remaining_orders: dict[str, PendingLiveOrder] = {}

        def remember_remaining(pending_order: PendingLiveOrder) -> None:
            logically_remaining_order_numbers.add(pending_order.order_no)
            logically_remaining_orders[pending_order.order_no] = pending_order

        for pending in self.pending_order_store.all():
            order = Order(
                symbol=pending.symbol,
                side=pending.side,
                quantity=pending.requested_quantity,
                reason=f"pending_order_sync:{pending.reason}",
            )
            response = {"output": {"ODNO": pending.order_no}}
            effective_query_date = query_date or pending.submitted_at.date()
            try:
                reconciliation = self.fill_reconciler.reconcile(order, response, query_date=effective_query_date)
            except Exception as exc:
                sync_unavailable = True
                remember_remaining(pending)
                self.audit_log.record(
                    "live_pending_order_sync_failed",
                    {
                        "symbol": pending.symbol,
                        "side": pending.side,
                        "order_no": pending.order_no,
                        "reason": self._redact(str(exc)),
                    },
                )
                continue
            fill_key = self._pending_fill_key(pending)
            already_consumed = (
                self._managed_position_ledger_consumed_quantity(fill_key)
                if consume_fills
                else 0
            )
            fill_delta = self._pending_fill_delta(
                previous_remaining=pending.remaining_quantity,
                reconciliation_filled=reconciliation.filled_quantity,
                reconciliation_unfilled=reconciliation.unfilled_quantity,
                already_consumed=already_consumed,
            )
            if fill_delta > 0:
                if consume_fills and not self._managed_position_ledger_ready():
                    sync_unavailable = True
                    self._record_audit_best_effort(
                        "live_pending_order_sync_blocked_by_managed_ledger",
                        {
                            "symbol": pending.symbol,
                            "side": pending.side,
                            "order_no": pending.order_no,
                            "status": reconciliation.status,
                            "filled_delta": fill_delta,
                        },
                    )
                    remember_remaining(pending)
                    continue
                fill_price = self._pending_fill_incremental_price(
                    fill_key=fill_key,
                    fill_delta=fill_delta,
                    cumulative_filled=reconciliation.filled_quantity,
                    cumulative_average_price=reconciliation.average_fill_price,
                    already_consumed=already_consumed,
                    fallback_price=pending.estimated_price,
                )
                if fill_price is None:
                    sync_unavailable = True
                    self._record_audit_best_effort(
                        "live_pending_fill_notional_unavailable",
                        {
                            "symbol": pending.symbol,
                            "side": pending.side,
                            "order_no": pending.order_no,
                            "filled_delta": fill_delta,
                        },
                    )
                    remember_remaining(pending)
                    continue
                fill_order = Order(
                    symbol=pending.symbol,
                    side=pending.side,
                    quantity=fill_delta,
                    reason=f"pending_order_sync:{pending.reason}:{reconciliation.status}",
                )
                realized_pnl = self._realized_pnl_for_order(
                    fill_order,
                    fill_price,
                    fill_delta,
                    fallback_cost_basis=pending.cost_basis_price,
                )
                fill = Fill(
                    order=fill_order,
                    accepted=True,
                    timestamp=self._pending_fill_timestamp(pending, effective_query_date, reconciliation),
                    price=fill_price,
                    quantity=fill_delta,
                    realized_pnl=realized_pnl,
                )
                if consume_fills:
                    if not self._record_managed_pending_fill(
                        fill,
                        fill_key=fill_key,
                        cumulative_filled=reconciliation.filled_quantity,
                        execution_price_payload={
                            "reference_price": str(pending.estimated_price),
                            "submitted_price": str(pending.estimated_price),
                            "slippage_pct": "",
                        },
                    ):
                        sync_unavailable = True
                        self._record_audit_best_effort(
                            "live_pending_order_sync_blocked_by_managed_ledger_update",
                            {
                                "symbol": pending.symbol,
                                "side": pending.side,
                                "order_no": pending.order_no,
                                "status": reconciliation.status,
                                "filled_delta": fill_delta,
                            },
                        )
                        remember_remaining(pending)
                        continue
                    fills.append(fill)
                if not consume_fills:
                    fills.append(fill)
                    self._record_audit_best_effort(
                        "live_pending_order_fill_requires_runtime_sync",
                        {
                            "symbol": pending.symbol,
                            "side": pending.side,
                            "order_no": pending.order_no,
                            "status": reconciliation.status,
                            "filled_delta": fill_delta,
                        },
                    )
                    if reconciliation.status not in {"filled", "rejected", "canceled", "expired"}:
                        remember_remaining(pending)
                    continue
            self._record_audit_best_effort(
                "live_pending_order_synced",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "status": reconciliation.status,
                    "filled_quantity": reconciliation.filled_quantity,
                    "unfilled_quantity": reconciliation.unfilled_quantity,
                },
            )
            if reconciliation.status in {"filled", "rejected", "canceled", "expired"}:
                self._clear_pending_order(pending.order_no)
                continue
            if reconciliation.filled_quantity > 0 and reconciliation.unfilled_quantity <= 0:
                self._clear_pending_order(pending.order_no)
                continue
            if reconciliation.status in {"pending", "partial", "not_found", "unknown"}:
                order_org_no = self._pending_order_org_no(pending, reconciliation)
                if pending.reason == "cancel_requested":
                    reason = "cancel_requested"
                elif not pending_order_reason_is_safely_scopable(pending.reason):
                    reason = str(pending.reason or "unknown").strip() or "unknown"
                else:
                    reason = reconciliation.status
                cancel_requested, order_org_no, clear_pending = (
                    self._request_stale_pending_order_cancel(
                        pending,
                        order_org_no=order_org_no,
                        reconciliation_status=reconciliation.status,
                        reconciliation_filled_quantity=reconciliation.filled_quantity,
                        reconciliation_unfilled_quantity=reconciliation.unfilled_quantity,
                    )
                    if consume_fills
                    else (False, order_org_no, False)
                )
                if clear_pending and self._clear_pending_order(pending.order_no):
                    continue
                if cancel_requested:
                    reason = "cancel_requested"
                updated_pending = replace(
                    pending,
                    remaining_quantity=reconciliation.unfilled_quantity or pending.remaining_quantity,
                    reason=reason,
                    order_org_no=order_org_no,
                )
                remember_remaining(updated_pending)
                persisted = self._record_pending_order(
                    order=order,
                    timestamp=pending.submitted_at,
                    estimated_price=pending.estimated_price,
                    order_no=pending.order_no,
                    remaining_quantity=reconciliation.unfilled_quantity or pending.remaining_quantity,
                    reason=reason,
                    cost_basis_price=pending.cost_basis_price,
                    order_org_no=order_org_no,
                )
                if cancel_requested and not persisted:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=pending.order_no,
                        reason="cancel state pending store update failed",
                    )
                continue

        try:
            remaining = self.pending_order_store.all()
            store_unavailable = False
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_store_unavailable",
                {"reason": self._redact(str(exc))},
            )
            remaining = tuple(logically_remaining_orders.values())
            store_unavailable = True
        if not consume_fills:
            remaining = tuple(
                pending for pending in remaining if pending.order_no in logically_remaining_order_numbers
            )
        return LivePendingOrderSyncResult(
            remaining=remaining,
            fills=tuple(fills),
            store_unavailable=store_unavailable,
            sync_unavailable=sync_unavailable,
        )

    def _confirmed_market_state_rejection_detail(
        self,
        order: Order,
        *,
        timestamp: datetime,
    ) -> str:
        inquire_price = getattr(self.client, "inquire_price", None)
        if not callable(inquire_price):
            return ""
        try:
            state_bar = parse_kis_price_bar(
                inquire_price(order.symbol),
                symbol=order.symbol,
                timestamp=timestamp,
            )
        except Exception as exc:
            self._record_audit_best_effort(
                "live_market_state_rejection_check_failed",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "error": exc.__class__.__name__,
                },
            )
            return ""
        if _live_market_state_blocker(state_bar) != "live_market_temporarily_stopped":
            return ""
        self._record_audit_best_effort(
            "live_market_state_rejection_confirmed",
            {
                "symbol": order.symbol,
                "side": order.side,
                "market": str(getattr(state_bar, "market", "") or "")[:32],
                "source": str(
                    getattr(state_bar, "trading_state_source", "") or ""
                )[:32],
            },
        )
        return "temporary stop confirmed by KIS current price"

    def place_order(self, order: Order, bar: MarketBar) -> Fill:
        market_state_blocker = _live_market_state_blocker(bar)
        if market_state_blocker:
            self._record_audit_best_effort(
                "live_order_blocked_by_market_state",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "market": str(getattr(bar, "market", "") or "")[:32],
                    "reason": market_state_blocker,
                    "source": str(
                        getattr(bar, "trading_state_source", "") or ""
                    )[:32],
                    "vi_code": str(getattr(bar, "vi_code", "") or "")[:16],
                    "security_status_code": str(
                        getattr(bar, "security_status_code", "") or ""
                    )[:16],
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=bar.timestamp,
                reject_reason=market_state_blocker,
            )
        execution_price = _live_execution_price(order, bar, self.config)
        price = execution_price.submitted_price
        execution_price_payload = _live_execution_price_payload(execution_price)
        timestamp = bar.timestamp
        if execution_price.blocker:
            self._record_audit_best_effort(
                "live_order_invalid_daily_price_boundary",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": execution_price.blocker,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=execution_price.blocker,
            )
        manual_reconciliation_reason = self._ready_manual_reconciliation_blocker_reason()
        if manual_reconciliation_reason:
            self._record_audit_best_effort(
                "live_order_blocked_by_manual_reconciliation",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": manual_reconciliation_reason,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_manual_reconciliation_required",
            )
        pending_sync = self._pending_order_state_for_preflight()
        pending_unavailable_reason = pending_sync.unavailable_reason
        blocking_pending = tuple(
            pending
            for pending in pending_sync.remaining
            if (
                pending.symbol == order.symbol
                or (order.side == "SELL" and pending.side == "SELL" and pending.symbol == order.symbol)
                or not pending_order_is_safely_scopable(pending)
                or str(getattr(pending, "side", "")).upper() not in {"BUY", "SELL"}
            )
        )
        if (
            pending_unavailable_reason == "live_pending_order_sync_unavailable"
            and self.fill_reconciler is None
        ):
            pending_unavailable_reason = ""
        if (
            pending_sync.fills
            or blocking_pending
            or pending_unavailable_reason
        ):
            if pending_unavailable_reason:
                reject_reason = pending_unavailable_reason
            elif pending_sync.fills:
                reject_reason = "live_pending_orders_synced"
            else:
                reject_reason = "live_pending_orders_unresolved"
            pending_symbols = tuple(pending.symbol for pending in blocking_pending[:10])
            filled_symbols = tuple(fill.order.symbol for fill in pending_sync.fills[:10])
            self._record_audit_best_effort(
                "live_order_blocked_by_pending_orders",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "pending_count": len(blocking_pending),
                    "filled_count": len(pending_sync.fills),
                    "pending_store_unavailable": pending_sync.store_unavailable,
                    "pending_sync_unavailable": pending_sync.sync_unavailable,
                    "pending_symbols": pending_symbols,
                    "filled_symbols": filled_symbols,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=reject_reason,
            )
        try:
            account = self.snapshot(timestamp=timestamp)
        except Exception as exc:
            reason = self._redact(f"live_snapshot_failed: {exc}")
            self._record_audit_best_effort(
                "live_order_snapshot_failed",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": reason,
                    **execution_price_payload,
                },
            )
            return Fill(order=order, accepted=False, timestamp=timestamp, reject_reason=reason)

        if order.side == "BUY" and not account.realized_pnl_today_known:
            self._record_audit_best_effort(
                "live_order_daily_pnl_unknown",
                {"symbol": order.symbol, "side": order.side, "quantity": order.quantity},
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_daily_realized_pnl_unknown",
            )
        if order.side == "BUY" and _account_day_pnl(account) <= -self.config.max_daily_loss:
            self._record_audit_best_effort(
                "live_order_daily_loss_limit_reached",
                {"symbol": order.symbol, "side": order.side, "quantity": order.quantity},
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_daily_loss_limit_reached",
            )

        account_confirmation_blocker = self._account_quantity_confirmation_blocker(
            order.symbol,
            account,
        )
        if account_confirmation_blocker:
            self._record_audit_best_effort(
                "live_order_blocked_by_account_quantity_confirmation",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": account_confirmation_blocker,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=account_confirmation_blocker,
            )

        managed_position_ledger_ready = self._managed_position_ledger_ready(account=account)
        if not managed_position_ledger_ready:
            self._record_audit_best_effort(
                "live_order_managed_position_ledger_denied",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_managed_position_ledger_unavailable",
            )

        managed_entry_blocker = self._managed_entry_state_blocker(order, timestamp)
        if managed_entry_blocker:
            self._record_audit_best_effort(
                "live_order_blocked_by_managed_entry_state",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": managed_entry_blocker,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=managed_entry_blocker,
            )

        account, buyable_blocker = self._account_after_buyable_inquiry(order, price, account)
        if buyable_blocker:
            self._record_audit_best_effort(
                "live_order_blocked_by_buyable_inquiry",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "estimated_price": str(price),
                    "reason": buyable_blocker,
                    **execution_price_payload,
                },
            )
            return Fill(order=order, accepted=False, timestamp=timestamp, reject_reason=buyable_blocker)

        decision = assess_live_order_preflight(
            LiveOrderPreflightRequest(
                config=self.config,
                env=self.env,
                order=order,
                account=account,
                estimated_price=price,
                market_is_open=self._bool(self.market_is_open),
                session_approved=self._bool(self.session_approved),
                account_confirmation=self.account_confirmation,
                expected_account_suffix=self.expected_account_suffix,
                live_broker_available=True,
                fill_reconciliation_available=self._fill_reconciliation_ready(),
                audit_log_ready=self._audit_log_ready(),
                managed_position_ledger_available=managed_position_ledger_ready,
                risk_limits_ok=self._bool(self.risk_limits_ok),
                new_entries_allowed=self._bool(self.new_entries_allowed),
                allow_managed_partial_sell=order.side == "SELL"
                and order.symbol in self._adopted_existing_position_symbols,
            )
        )
        if not decision.approved:
            reason = "live_preflight_denied: " + ", ".join(decision.blockers)
            self._record_audit_best_effort(
                "live_order_preflight_denied",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "estimated_price": str(price),
                    "blockers": decision.blockers,
                    **execution_price_payload,
                },
            )
            return Fill(order=order, accepted=False, timestamp=timestamp, reject_reason=reason)

        manual_reconciliation_store_reason = self._manual_reconciliation_store_dependency_reason()
        if manual_reconciliation_store_reason:
            self._record_audit_best_effort(
                "live_order_blocked_by_manual_reconciliation_store",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": manual_reconciliation_store_reason,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=manual_reconciliation_store_reason,
            )
        manual_reconciliation_reason = self._manual_reconciliation_blocker_reason()
        if manual_reconciliation_reason:
            self._record_audit_best_effort(
                "live_order_blocked_by_manual_reconciliation",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "reason": manual_reconciliation_reason,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_manual_reconciliation_required",
            )

        if not self._record_audit_best_effort(
            "live_order_preflight_approved",
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "estimated_price": str(price),
                **execution_price_payload,
            },
        ):
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_audit_log_unavailable",
            )
        submission_guard_order_no = self._manual_reconciliation_order_no(order, timestamp)
        pending_cost_basis = self._pending_order_cost_basis(order, account)
        if not self._record_pending_order(
            order=order,
            timestamp=timestamp,
            estimated_price=price,
            order_no=submission_guard_order_no,
            remaining_quantity=order.quantity,
            reason="submission_in_progress",
            cost_basis_price=pending_cost_basis,
            require_audit=True,
        ):
            self._block_live_orders_for_manual_reconciliation(
                order=order,
                order_no=submission_guard_order_no,
                reason="submission_guard_pending_store_update_failed",
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_submission_guard_unavailable",
            )
        try:
            response = self.client.place_cash_order(order, order_price=price)
        except KisOrderSubmissionUncertain as exc:
            if order.side == "BUY":
                self._cached_orderable_cash = Decimal("0")
            reason = self._redact(f"live_order_submission_uncertain: {exc}")
            self._record_audit_best_effort(
                "live_order_submission_uncertain",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "estimated_price": str(price),
                    "order_no": submission_guard_order_no,
                    "reason": reason,
                    **execution_price_payload,
                },
            )
            tracked = self._record_pending_order(
                order=order,
                timestamp=timestamp,
                estimated_price=price,
                order_no=submission_guard_order_no,
                remaining_quantity=order.quantity,
                reason="submission_uncertain",
                cost_basis_price=pending_cost_basis,
            )
            self._block_live_orders_for_manual_reconciliation(
                order=order,
                order_no=submission_guard_order_no,
                reason="submission_uncertain"
                if tracked
                else "submission_uncertain_pending_store_update_failed",
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_order_submission_uncertain",
                requires_cycle_pause=True,
            )
        except (KisApiError, RuntimeError, ValueError) as exc:
            guard_cleanup_ready = self._clear_pending_order(submission_guard_order_no)
            if not guard_cleanup_ready:
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=submission_guard_order_no,
                    reason="rejected submission guard cleanup failed",
                )
            reason = _reconciled_live_reject_reason(
                "rejected",
                self._redact(str(exc)),
            )
            self._record_audit_best_effort(
                "live_order_rejected",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "estimated_price": str(price),
                    "reason": reason,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=reason,
                requires_cycle_pause=not guard_cleanup_ready,
            )

        self._reserve_cached_orderable_cash(order, price)
        self._record_audit_best_effort(
            "live_order_submitted",
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "estimated_price": str(price),
                "kis_result": _kis_response_result(response),
                **execution_price_payload,
            },
        )
        submitted_order_no = extract_live_order_number(response)
        submitted_order_org_no = extract_live_order_org_number(response)
        if self.fill_reconciler is None:
            self._record_audit_best_effort(
                "live_order_reconciliation_missing",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    **execution_price_payload,
                },
            )
            tracked = self._record_pending_order(
                order=order,
                timestamp=timestamp,
                estimated_price=price,
                order_no=submitted_order_no or submission_guard_order_no,
                remaining_quantity=order.quantity,
                reason="reconciliation_missing_after_submission",
                cost_basis_price=pending_cost_basis,
                order_org_no=submitted_order_org_no,
            )
            if tracked:
                self._clear_submission_guard(
                    submission_guard_order_no,
                    actual_order_no=submitted_order_no,
                )
            self._block_live_orders_for_manual_reconciliation(
                order=order,
                order_no=submitted_order_no or submission_guard_order_no,
                reason=(
                    "reconciliation_missing_after_submission"
                    if tracked
                    else "reconciliation_missing_pending_store_update_failed"
                ),
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_fill_reconciliation_unavailable",
                requires_cycle_pause=True,
            )

        try:
            reconciliation = self.fill_reconciler.reconcile(order, response, query_date=timestamp.date())
        except Exception as exc:
            order_no = submitted_order_no
            tracking_scopable = False
            reason = self._redact(f"live_order_reconciliation_failed: {exc}")
            self._record_audit_best_effort(
                "live_order_reconciliation_failed",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "order_no": order_no,
                    "reason": reason,
                    **execution_price_payload,
                },
            )
            if order_no:
                tracked = self._record_pending_order(
                    order=order,
                    timestamp=timestamp,
                    estimated_price=price,
                    order_no=order_no,
                    remaining_quantity=order.quantity,
                    reason="reconciliation_failed",
                    cost_basis_price=pending_cost_basis,
                    order_org_no=submitted_order_org_no,
                )
                tracking_scopable = self._pending_tracking_is_scopable(
                    order=order,
                    guard_order_no=submission_guard_order_no,
                    actual_order_no=order_no,
                    tracked=tracked,
                )
                if not tracked:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=order_no,
                        reason="reconciliation_failed_pending_store_update_failed",
                    )
            else:
                self._record_pending_order(
                    order=order,
                    timestamp=timestamp,
                    estimated_price=price,
                    order_no=submission_guard_order_no,
                    remaining_quantity=order.quantity,
                    reason="reconciliation_failed_without_order_no",
                    cost_basis_price=pending_cost_basis,
                    order_org_no=submitted_order_org_no,
                )
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=submission_guard_order_no,
                    reason="reconciliation_failed_without_order_no",
                )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=reason,
                pending_order_tracked=tracking_scopable,
                requires_cycle_pause=not tracking_scopable,
            )

        market_state_rejection_detail = (
            self._confirmed_market_state_rejection_detail(
                order,
                timestamp=timestamp,
            )
            if reconciliation.status == "rejected"
            else ""
        )
        self._record_audit_best_effort(
            "live_order_reconciled",
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "order_no": reconciliation.order_no,
                "status": reconciliation.status,
                "filled_quantity": reconciliation.filled_quantity,
                "unfilled_quantity": reconciliation.unfilled_quantity,
                "average_fill_price": str(reconciliation.average_fill_price),
                "market_state_rejection_detail": market_state_rejection_detail,
                **execution_price_payload,
            },
        )
        if reconciliation.filled_quantity <= 0:
            tracking_scopable = False
            terminal_cleanup_ready = True
            if reconciliation.order_no and reconciliation.status in {
                "pending",
                "not_found",
                "unknown",
            }:
                tracked = self._record_pending_order(
                    order=order,
                    timestamp=timestamp,
                    estimated_price=price,
                    order_no=reconciliation.order_no,
                    remaining_quantity=reconciliation.unfilled_quantity or order.quantity,
                    reason=reconciliation.status,
                    cost_basis_price=pending_cost_basis,
                    order_org_no=submitted_order_org_no or _reconciliation_order_org_no(reconciliation),
                )
                tracking_scopable = self._pending_tracking_is_scopable(
                    order=order,
                    guard_order_no=submission_guard_order_no,
                    actual_order_no=reconciliation.order_no,
                    tracked=tracked,
                )
                if not tracked:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=reconciliation.order_no,
                        reason=f"{reconciliation.status}_pending_store_update_failed",
                    )
            elif reconciliation.status == "submitted_without_order_no":
                self._record_audit_best_effort(
                    "live_order_manual_reconciliation_required",
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "reason": "submitted_without_order_no",
                        **execution_price_payload,
                    },
                )
                tracked = self._record_pending_order(
                    order=order,
                    timestamp=timestamp,
                    estimated_price=price,
                    order_no=submission_guard_order_no,
                    remaining_quantity=order.quantity,
                    reason="submitted_without_order_no",
                    cost_basis_price=pending_cost_basis,
                    order_org_no=submitted_order_org_no,
                )
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=submission_guard_order_no,
                    reason=(
                        "submitted_without_order_no"
                        if tracked
                        else "submitted_without_order_no_pending_store_update_failed"
                    ),
                )
            elif reconciliation.order_no:
                terminal_cleanup_ready = self._clear_pending_order(reconciliation.order_no)
                terminal_cleanup_ready = self._clear_submission_guard(
                    submission_guard_order_no,
                    actual_order_no=reconciliation.order_no,
                ) and terminal_cleanup_ready
                if not terminal_cleanup_ready:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=reconciliation.order_no,
                        reason="terminal pending state cleanup failed",
                    )
            elif reconciliation.status not in {"pending", "not_found", "unknown"}:
                terminal_cleanup_ready = self._clear_pending_order(submission_guard_order_no)
                if not terminal_cleanup_ready:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=submission_guard_order_no,
                        reason="terminal pending state cleanup failed",
                    )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason=_reconciled_live_reject_reason(
                    reconciliation.status,
                    market_state_rejection_detail,
                ),
                pending_order_tracked=tracking_scopable,
                requires_cycle_pause=bool(
                    reconciliation.status == "submitted_without_order_no"
                    or (
                        reconciliation.status in {"pending", "not_found", "unknown"}
                        and not tracking_scopable
                    )
                    or not terminal_cleanup_ready
                ),
            )
        pre_fill_pending_state_safe = True
        if reconciliation.unfilled_quantity > 0 and reconciliation.order_no:
            tracked = self._record_pending_order(
                order=order,
                timestamp=timestamp,
                estimated_price=price,
                order_no=reconciliation.order_no,
                remaining_quantity=order.quantity,
                reason=f"{reconciliation.status}_awaiting_fill_ledger",
                cost_basis_price=pending_cost_basis,
                order_org_no=submitted_order_org_no or _reconciliation_order_org_no(reconciliation),
            )
            pre_fill_pending_state_safe = self._pending_tracking_is_scopable(
                order=order,
                guard_order_no=submission_guard_order_no,
                actual_order_no=reconciliation.order_no,
                tracked=tracked,
            )
            if not tracked:
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=reconciliation.order_no,
                    reason=f"{reconciliation.status}_pending_store_update_failed_before_fill_ledger",
                )
        fill_price = reconciliation.average_fill_price if reconciliation.average_fill_price > 0 else price
        realized_pnl = self._realized_pnl_for_order(order, fill_price, reconciliation.filled_quantity, account)
        fill = Fill(
            order=order,
            accepted=True,
            timestamp=timestamp,
            price=fill_price,
            quantity=reconciliation.filled_quantity,
            realized_pnl=realized_pnl,
        )
        fill_key = self._live_fill_key(
            timestamp=timestamp,
            order_no=reconciliation.order_no or submitted_order_no or submission_guard_order_no,
            symbol=order.symbol,
            side=order.side,
        )
        if not self._record_managed_fill(
            fill,
            fill_key=fill_key,
            cumulative_filled=reconciliation.filled_quantity,
            execution_price_payload=execution_price_payload,
        ):
            if reconciliation.order_no:
                tracked = self._record_pending_order(
                    order=order,
                    timestamp=timestamp,
                    estimated_price=price,
                    order_no=reconciliation.order_no,
                    remaining_quantity=order.quantity,
                    reason="managed_position_ledger_update_failed",
                    cost_basis_price=pending_cost_basis,
                    order_org_no=submitted_order_org_no or _reconciliation_order_org_no(reconciliation),
                )
                if tracked:
                    self._clear_submission_guard(
                        submission_guard_order_no,
                        actual_order_no=reconciliation.order_no,
                    )
                if not tracked:
                    self._block_live_orders_for_manual_reconciliation(
                        order=order,
                        order_no=reconciliation.order_no,
                        reason="managed_position_ledger_and_pending_store_update_failed",
                    )
            self._record_audit_best_effort(
                "live_order_fill_not_consumed_due_to_managed_ledger_update",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": reconciliation.filled_quantity,
                    "order_no": reconciliation.order_no,
                    **execution_price_payload,
                },
            )
            return Fill(
                order=order,
                accepted=False,
                timestamp=timestamp,
                reject_reason="live_managed_position_ledger_update_failed_after_fill",
                requires_cycle_pause=True,
            )
        if reconciliation.unfilled_quantity > 0:
            if not reconciliation.order_no:
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=submission_guard_order_no,
                    reason="partial fill without order number",
                )
                return replace(fill, requires_cycle_pause=True)
            tracked = self._record_pending_order(
                order=order,
                timestamp=timestamp,
                estimated_price=price,
                order_no=reconciliation.order_no,
                remaining_quantity=reconciliation.unfilled_quantity,
                reason=reconciliation.status,
                cost_basis_price=pending_cost_basis,
                order_org_no=submitted_order_org_no or _reconciliation_order_org_no(reconciliation),
            )
            tracking_scopable = self._pending_tracking_is_scopable(
                order=order,
                guard_order_no=submission_guard_order_no,
                actual_order_no=reconciliation.order_no,
                tracked=tracked,
            )
            if not tracked:
                self._block_live_orders_for_manual_reconciliation(
                    order=order,
                    order_no=reconciliation.order_no,
                    reason=f"{reconciliation.status}_pending_store_update_failed_after_fill",
                )
            return replace(
                fill,
                pending_order_tracked=pre_fill_pending_state_safe and tracking_scopable,
                requires_cycle_pause=not (
                    pre_fill_pending_state_safe and tracking_scopable
                ),
            )
        pending_cleanup_ready = True
        if reconciliation.order_no:
            pending_cleanup_ready = self._clear_pending_order(reconciliation.order_no)
            pending_cleanup_ready = self._clear_submission_guard(
                submission_guard_order_no,
                actual_order_no=reconciliation.order_no,
            ) and pending_cleanup_ready
        else:
            pending_cleanup_ready = self._clear_pending_order(submission_guard_order_no)
        if not pending_cleanup_ready:
            self._block_live_orders_for_manual_reconciliation(
                order=order,
                order_no=reconciliation.order_no or submission_guard_order_no,
                reason="terminal pending state cleanup failed",
            )
            return replace(fill, requires_cycle_pause=True)
        return fill

    def _with_managed_quantities(
        self,
        account: AccountSnapshot,
        *,
        timestamp: datetime | None = None,
    ) -> AccountSnapshot:
        if self.managed_position_ledger is None:
            return account
        try:
            self.managed_position_ledger.ensure_ready()
            managed_quantities = self.managed_position_ledger.all()
            lifecycle_for = getattr(self.managed_position_ledger, "lifecycle_for", None)
            lifecycle_is_known = getattr(
                self.managed_position_ledger,
                "position_lifecycle_is_known",
                None,
            )
            managed_lifecycles = {
                symbol: (
                    lifecycle_for(symbol)
                    if not callable(lifecycle_is_known) or lifecycle_is_known(symbol)
                    else None
                )
                for symbol in managed_quantities
            } if callable(lifecycle_for) else {}
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_position_ledger_unavailable",
                {"reason": self._redact(str(exc))},
            )
            managed_quantities = {}
            managed_lifecycles = {}

        positions = {}
        drift_symbols = set(self._managed_position_ledger_drift_symbols(managed_quantities, account))
        if drift_symbols:
            self._record_audit_best_effort(
                "live_managed_position_ledger_drift",
                {"symbols": tuple(sorted(drift_symbols))},
            )
        for symbol, position in account.positions.items():
            ledger_quantity = managed_quantities.get(symbol, 0)
            managed_quantity = 0 if symbol in drift_symbols else min(position.quantity, ledger_quantity)
            lifecycle = managed_lifecycles.get(symbol) if managed_quantity > 0 else None
            positions[symbol] = replace(
                position,
                managed_quantity=managed_quantity,
                opened_at=lifecycle.opened_at if lifecycle is not None else position.opened_at,
                highest_price=lifecycle.highest_price if lifecycle is not None else position.highest_price,
                lowest_price=lifecycle.lowest_price if lifecycle is not None else position.lowest_price,
            )
        ledger_realized_pnl = self._managed_realized_pnl_today(timestamp)
        return AccountSnapshot(
            cash=account.cash,
            positions=positions,
            equity_override=getattr(account, "equity_override", None),
            buying_power_override=getattr(account, "buying_power_override", None),
            realized_pnl_today=(
                account.realized_pnl_today
                if account.realized_pnl_today_known
                else ledger_realized_pnl
            ),
            realized_pnl_today_known=account.realized_pnl_today_known,
        )

    def _account_after_buyable_inquiry(
        self,
        order: Order,
        price: Decimal,
        account: AccountSnapshot,
        *,
        validate_order: bool = True,
    ) -> tuple[AccountSnapshot, str]:
        if order.side != "BUY":
            return account, ""
        inquiry = getattr(self.client, "inquire_buyable_order", None)
        if not callable(inquiry):
            self._cached_orderable_cash = Decimal("0")
            return replace(account, buying_power_override=Decimal("0")), "live_buyable_inquiry_unavailable"
        try:
            response = inquiry(order.symbol, order_price=price)
        except Exception as exc:
            self._cached_orderable_cash = Decimal("0")
            return (
                replace(account, buying_power_override=Decimal("0")),
                f"live_buyable_inquiry_failed: {self._redact(str(exc))}",
            )

        output = self._buyable_output(response)
        if output is None:
            self._cached_orderable_cash = Decimal("0")
            return replace(account, buying_power_override=Decimal("0")), "live_buyable_inquiry_malformed"

        # KIS max-buy fields may include margin. Prefer the broker-calculated
        # no-receivable cash fields and retain raw orderable cash as a safe fallback.
        orderable_cash = self._first_decimal_or_none(
            output,
            ("nrcvb_buy_amt", "ord_psbl_cash", "ord_psbl_cash_amt"),
        )
        orderable_quantity = self._first_int_or_none(
            output,
            ("nrcvb_buy_qty", "ord_psbl_qty", "ord_psbl_qty1", "psbl_qty"),
        )
        if orderable_cash is None:
            self._cached_orderable_cash = Decimal("0")
            return replace(account, buying_power_override=Decimal("0")), "live_buyable_cash_unknown"
        refreshed_orderable_cash = max(Decimal("0"), orderable_cash)
        if self._pending_order_batch_active and self._cached_orderable_cash is not None:
            refreshed_orderable_cash = min(
                refreshed_orderable_cash,
                max(Decimal("0"), self._cached_orderable_cash),
            )
        self._cached_orderable_cash = refreshed_orderable_cash
        account = replace(account, buying_power_override=self._cached_orderable_cash)
        if validate_order and orderable_quantity is not None and orderable_quantity < order.quantity:
            return account, "live_buyable_quantity"
        if validate_order and price * Decimal(order.quantity) > self._cached_orderable_cash:
            return account, "live_buyable_cash"
        return account, ""

    def _reserve_cached_orderable_cash(self, order: Order, price: Decimal) -> None:
        if order.side != "BUY" or self._cached_orderable_cash is None:
            return
        reserved = max(Decimal("0"), price * Decimal(order.quantity))
        self._cached_orderable_cash = max(Decimal("0"), self._cached_orderable_cash - reserved)

    @staticmethod
    def _buyable_output(response: object) -> Mapping[str, object] | None:
        if not isinstance(response, Mapping):
            return None
        output = response.get("output")
        if isinstance(output, Mapping):
            return output
        if isinstance(output, Sequence) and not isinstance(output, (str, bytes, bytearray)) and output:
            first = output[0]
            if isinstance(first, Mapping):
                return first
        return None

    @staticmethod
    def _first_decimal_or_none(row: Mapping[str, object], keys: Sequence[str]) -> Decimal | None:
        for key in keys:
            value = row.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                return max(Decimal("0"), Decimal(str(value).strip().replace(",", "")))
            except Exception:
                continue
        return None

    @staticmethod
    def _first_int_or_none(row: Mapping[str, object], keys: Sequence[str]) -> int | None:
        value = LiveBroker._first_decimal_or_none(row, keys)
        if value is None:
            return None
        if value != value.to_integral_value():
            return None
        return int(value)

    def _record_managed_fill(
        self,
        fill: Fill,
        *,
        fill_key: str,
        cumulative_filled: int,
        execution_price_payload: Mapping[str, object] | None = None,
    ) -> bool:
        if self.managed_position_ledger is None or not fill.accepted or fill.quantity <= 0:
            return False
        try:
            self.managed_position_ledger.ensure_ready()
            recorder = getattr(self.managed_position_ledger, "record_fill_transaction", None)
            if not callable(recorder):
                raise RuntimeError("atomic managed fill ledger is unavailable")
            result = recorder(
                fill_key=fill_key,
                symbol=fill.order.symbol,
                side=fill.order.side,
                quantity_delta=fill.quantity,
                cumulative_filled=cumulative_filled,
                timestamp=fill.timestamp,
                price=fill.price,
                realized_pnl=fill.realized_pnl,
            )
            if int(getattr(result, "applied_quantity", -1)) != fill.quantity:
                return False
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_position_ledger_update_failed",
                {
                    "symbol": fill.order.symbol,
                    "side": fill.order.side,
                    "quantity": fill.quantity,
                    "reason": self._redact(str(exc)),
                    **dict(execution_price_payload or {}),
                },
            )
            return False
        return True

    def _record_managed_pending_fill(
        self,
        fill: Fill,
        *,
        fill_key: str,
        cumulative_filled: int,
        execution_price_payload: Mapping[str, object] | None = None,
    ) -> bool:
        return self._record_managed_fill(
            fill,
            fill_key=fill_key,
            cumulative_filled=cumulative_filled,
            execution_price_payload=execution_price_payload,
        )

    def _managed_realized_pnl_today(self, timestamp: datetime | None) -> Decimal:
        if self.managed_position_ledger is None:
            return Decimal("0")
        reader = getattr(self.managed_position_ledger, "realized_pnl_today", None)
        if reader is None:
            return Decimal("0")
        trading_day = (timestamp or datetime.now()).date()
        try:
            return Decimal(str(reader(trading_day)))
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_realized_pnl_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return Decimal("0")

    def _pending_order_cost_basis(self, order: Order, account: AccountSnapshot) -> Decimal:
        if order.side != "SELL":
            return Decimal("0")
        position = account.positions.get(order.symbol)
        if position is None:
            return Decimal("0")
        return position.avg_price

    def _realized_pnl_for_order(
        self,
        order: Order,
        fill_price: Decimal,
        fill_quantity: int,
        account: AccountSnapshot | None = None,
        fallback_cost_basis: Decimal = Decimal("0"),
    ) -> Decimal:
        if order.side != "SELL" or fill_quantity <= 0:
            return Decimal("0")
        parsed_fallback = Decimal(str(fallback_cost_basis or "0"))
        try:
            account = account or self.snapshot()
        except Exception:
            return (
                (fill_price - parsed_fallback) * Decimal(fill_quantity)
                if parsed_fallback > 0
                else Decimal("0")
            )
        position = account.positions.get(order.symbol)
        if position is None:
            return (
                (fill_price - parsed_fallback) * Decimal(fill_quantity)
                if parsed_fallback > 0
                else Decimal("0")
            )
        return (fill_price - position.avg_price) * Decimal(fill_quantity)

    def _record_pending_order(
        self,
        *,
        order: Order,
        timestamp: datetime,
        estimated_price: Decimal,
        order_no: str,
        remaining_quantity: int,
        reason: str,
        cost_basis_price: Decimal = Decimal("0"),
        order_org_no: str = "",
        require_audit: bool = False,
    ) -> bool:
        if self.pending_order_store is None:
            self._record_audit_best_effort(
                "live_pending_order_untracked",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "order_no": order_no,
                    "remaining_quantity": remaining_quantity,
                    "reason": reason,
                },
            )
            return False
        try:
            self.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no=order_no,
                    symbol=order.symbol,
                    side=order.side,
                    requested_quantity=order.quantity,
                    remaining_quantity=remaining_quantity,
                    submitted_at=timestamp,
                    estimated_price=estimated_price,
                    reason=reason,
                    cost_basis_price=cost_basis_price,
                    order_org_no=order_org_no,
                )
            )
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_store_update_failed",
                {
                    "symbol": order.symbol,
                    "side": order.side,
                    "quantity": order.quantity,
                    "order_no": order_no,
                    "remaining_quantity": remaining_quantity,
                    "reason": self._redact(str(exc)),
                },
            )
            return False
        tracked = self._record_audit_best_effort(
            "live_pending_order_tracked",
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "order_no": order_no,
                "remaining_quantity": remaining_quantity,
                "reason": reason,
            },
        )
        if require_audit and not tracked:
            return False
        return True

    def _pending_order_state_for_preflight(self) -> LivePendingOrderSyncResult:
        if not self._pending_order_batch_active:
            return self.sync_pending_order_statuses(consume_fills=False)
        if self.pending_order_store is None:
            return LivePendingOrderSyncResult(store_unavailable=True)
        try:
            remaining = tuple(self.pending_order_store.all() or ())
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_store_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return LivePendingOrderSyncResult(store_unavailable=True)
        return LivePendingOrderSyncResult(remaining=remaining)

    def _request_stale_pending_order_cancel(
        self,
        pending: PendingLiveOrder,
        *,
        order_org_no: str,
        reconciliation_status: str,
        reconciliation_filled_quantity: int,
        reconciliation_unfilled_quantity: int,
    ) -> tuple[bool, str, bool]:
        effective_order_org_no = order_org_no
        if pending.reason == "cancel_requested":
            terminal_candidate = (
                reconciliation_status in {"unknown", "not_found"}
                and reconciliation_filled_quantity == 0
                and reconciliation_unfilled_quantity == 0
            )
            if not terminal_candidate:
                return False, effective_order_org_no, False
            return (
                False,
                effective_order_org_no,
                self._confirm_pending_order_terminal(
                    pending,
                    reconciliation_status=reconciliation_status,
                ),
            )
        if reconciliation_status not in {"pending", "partial", "unknown", "not_found"}:
            return False, effective_order_org_no, False
        if self._pending_order_age(pending) < LIVE_PENDING_ORDER_CANCEL_AFTER:
            return False, effective_order_org_no, False
        cancel_blockers = self._stale_pending_order_cancel_blockers()
        if cancel_blockers:
            self._record_audit_best_effort(
                "live_pending_order_cancel_blocked",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "blockers": cancel_blockers,
                },
            )
            return False, effective_order_org_no, False
        cancelable_order = self._cancelable_pending_order(pending)
        if cancelable_order.quantity <= 0:
            if not cancelable_order.checked:
                return False, effective_order_org_no or cancelable_order.order_org_no, False
            self._record_audit_best_effort(
                "live_pending_order_cancel_skipped",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": "not_cancelable",
                },
            )
            if reconciliation_status in {"unknown", "not_found"} and cancelable_order.checked:
                return (
                    False,
                    effective_order_org_no or cancelable_order.order_org_no,
                    self._confirm_pending_order_terminal(
                        pending,
                        reconciliation_status=reconciliation_status,
                        cancelable_order=cancelable_order,
                    ),
                )
            return False, effective_order_org_no or cancelable_order.order_org_no, False
        if (
            effective_order_org_no
            and cancelable_order.order_org_no
            and cancelable_order.order_org_no != effective_order_org_no
        ):
            self._record_audit_best_effort(
                "live_pending_order_cancel_skipped",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": "order_org_no_mismatch",
                },
            )
            return False, effective_order_org_no, False
        effective_order_org_no = effective_order_org_no or cancelable_order.order_org_no
        if not effective_order_org_no:
            self._record_audit_best_effort(
                "live_pending_order_cancel_skipped",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": "missing_order_org_no",
                },
            )
            return False, effective_order_org_no, False
        quantity = min(pending.remaining_quantity, cancelable_order.quantity)
        try:
            response = self.client.cancel_cash_order(
                order_no=pending.order_no,
                order_org_no=effective_order_org_no,
                quantity=quantity,
                order_price=pending.estimated_price,
            )
        except KisOrderSubmissionUncertain as exc:
            self._record_audit_best_effort(
                "live_pending_order_cancel_submission_uncertain",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": self._redact(str(exc)),
                },
            )
            return True, effective_order_org_no, False
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_cancel_failed",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": self._redact(str(exc)),
                },
            )
            return False, effective_order_org_no, False
        if not _kis_response_success(response):
            self._record_audit_best_effort(
                "live_pending_order_cancel_failed",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "kis_result": _kis_response_result(response),
                },
            )
            return False, effective_order_org_no, False
        self._record_audit_best_effort(
            "live_pending_order_cancel_requested",
            {
                "symbol": pending.symbol,
                "side": pending.side,
                "order_no": pending.order_no,
                "quantity": quantity,
                "kis_result": _kis_response_result(response),
            },
        )
        return True, effective_order_org_no, False

    def _confirm_pending_order_terminal(
        self,
        pending: PendingLiveOrder,
        *,
        reconciliation_status: str,
        cancelable_order: _CancelablePendingOrder | None = None,
    ) -> bool:
        event_prefix = (
            "live_pending_order_post_cancel_confirmation"
            if pending.reason == "cancel_requested"
            else "live_pending_order_cancelable_absent_confirmation"
        )
        if cancelable_order is None:
            cancelable_order = self._cancelable_pending_order(pending)
        if not cancelable_order.checked:
            self._record_audit_best_effort(
                f"{event_prefix}_failed",
                {
                    "side": pending.side,
                    "status": reconciliation_status,
                    "reason": "cancelable_inquiry_failed",
                },
            )
            return False
        if cancelable_order.quantity > 0:
            self._record_audit_best_effort(
                f"{event_prefix}_blocked",
                {
                    "side": pending.side,
                    "status": reconciliation_status,
                    "reason": "order_still_cancelable",
                },
            )
            return False

        try:
            # The live client paces this request through its shared KIS rate limiter.
            account = self.client.account_snapshot()
        except Exception as exc:
            self._record_audit_best_effort(
                f"{event_prefix}_failed",
                {
                    "side": pending.side,
                    "status": reconciliation_status,
                    "reason": "account_snapshot_failed",
                    "error_type": type(exc).__name__,
                },
            )
            return False

        ledger = self.managed_position_ledger
        if ledger is None or not bool(getattr(ledger, "is_durable", False)):
            self._record_audit_best_effort(
                f"{event_prefix}_failed",
                {
                    "side": pending.side,
                    "status": reconciliation_status,
                    "reason": "managed_ledger_unavailable",
                },
            )
            return False
        try:
            ledger.ensure_ready()
            managed_quantity = int(ledger.all().get(pending.symbol, 0))
        except Exception as exc:
            self._record_audit_best_effort(
                f"{event_prefix}_failed",
                {
                    "side": pending.side,
                    "status": reconciliation_status,
                    "reason": "managed_ledger_unavailable",
                    "error_type": type(exc).__name__,
                },
            )
            return False

        position = account.positions.get(pending.symbol)
        account_quantity = int(position.quantity) if position is not None else 0
        confirmation = {
            "side": pending.side,
            "status": reconciliation_status,
            "account_quantity": account_quantity,
            "managed_quantity": managed_quantity,
        }
        if account_quantity != managed_quantity:
            self._record_audit_best_effort(
                f"{event_prefix}_blocked",
                {
                    **confirmation,
                    "reason": "account_ledger_quantity_mismatch",
                },
            )
            return False

        success_event = (
            "live_pending_order_cleared_after_post_cancel_confirmation"
            if pending.reason == "cancel_requested"
            else "live_pending_order_cleared_after_cancelable_absent"
        )
        return self._record_audit_best_effort(success_event, confirmation)

    def _cancelable_pending_order(self, pending: PendingLiveOrder) -> _CancelablePendingOrder:
        inquire_cancelable_orders = getattr(self.client, "inquire_cancelable_orders", None)
        if not callable(inquire_cancelable_orders):
            return _CancelablePendingOrder()
        ctx_area_fk100 = ""
        ctx_area_nk100 = ""
        tr_cont = ""
        try:
            for _ in range(10):
                response = inquire_cancelable_orders(
                    ctx_area_fk100=ctx_area_fk100,
                    ctx_area_nk100=ctx_area_nk100,
                    tr_cont=tr_cont,
                )
                if not _kis_response_success(response):
                    self._record_audit_best_effort(
                        "live_pending_order_cancelable_inquiry_failed",
                        {
                            "symbol": pending.symbol,
                            "side": pending.side,
                            "order_no": pending.order_no,
                            "reason": _kis_response_result(response),
                        },
                    )
                    return _CancelablePendingOrder()
                for row in _kis_response_rows(response):
                    if _first_string(row, "odno", "ODNO", "ord_no", "ORD_NO") != pending.order_no:
                        continue
                    symbol = _first_string(row, "pdno", "PDNO")
                    if symbol and symbol != pending.symbol:
                        continue
                    raw_quantity = _first_string(
                        row,
                        "psbl_qty",
                        "PSBL_QTY",
                        "rvse_cncl_psbl_qty",
                        "RVSE_CNCL_PSBL_QTY",
                    )
                    quantity = _first_int(
                        row,
                        "psbl_qty",
                        "PSBL_QTY",
                        "rvse_cncl_psbl_qty",
                        "RVSE_CNCL_PSBL_QTY",
                    )
                    # A matching row with zero or malformed quantity is not proof
                    # that the order disappeared. Keep the pending record until
                    # KIS gives a valid cancelable quantity or the row is absent
                    # after a complete inquiry.
                    if not raw_quantity or quantity <= 0:
                        self._record_audit_best_effort(
                            "live_pending_order_cancelable_inquiry_failed",
                            {
                                "symbol": pending.symbol,
                                "side": pending.side,
                                "order_no": pending.order_no,
                                "reason": "matching_row_quantity_unknown",
                            },
                        )
                        return _CancelablePendingOrder(
                            order_org_no=_first_string(
                                row,
                                "krx_fwdg_ord_orgno",
                                "KRX_FWDG_ORD_ORGNO",
                                "ord_gno_brno",
                                "ORD_GNO_BRNO",
                                "ord_orgno",
                                "ORD_ORGNO",
                            )
                        )
                    return _CancelablePendingOrder(
                        quantity=quantity,
                        order_org_no=_first_string(
                            row,
                            "krx_fwdg_ord_orgno",
                            "KRX_FWDG_ORD_ORGNO",
                            "ord_gno_brno",
                            "ORD_GNO_BRNO",
                            "ord_orgno",
                            "ORD_ORGNO",
                        ),
                        checked=True,
                    )
                if _kis_response_tr_cont(response) not in {"M", "F"}:
                    return _CancelablePendingOrder(checked=True)
                ctx_area_fk100, ctx_area_nk100 = _kis_response_continuation_keys(response)
                if not ctx_area_fk100 and not ctx_area_nk100:
                    self._record_audit_best_effort(
                        "live_pending_order_cancelable_inquiry_failed",
                        {
                            "symbol": pending.symbol,
                            "side": pending.side,
                            "order_no": pending.order_no,
                            "reason": "missing_continuation_keys",
                        },
                    )
                    return _CancelablePendingOrder()
                tr_cont = "N"
            self._record_audit_best_effort(
                "live_pending_order_cancelable_inquiry_failed",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": "continuation_page_limit_exceeded",
                },
            )
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_cancelable_inquiry_failed",
                {
                    "symbol": pending.symbol,
                    "side": pending.side,
                    "order_no": pending.order_no,
                    "reason": self._redact(str(exc)),
                },
            )
            return _CancelablePendingOrder()
        return _CancelablePendingOrder()

    def _stale_pending_order_cancel_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not live_order_gate_configured(self.config, self.env):
            blockers.append("live_order_gate_configured=true")
        expected_suffix = self.expected_account_suffix.strip() or self._env_live_account_suffix()
        if not expected_suffix:
            blockers.append("account_confirmation=<live account suffix>")
        elif self.account_confirmation.strip() != expected_suffix:
            blockers.append(f"account_confirmation={expected_suffix}")
        if not self._bool(self.market_is_open):
            blockers.append("market_is_open=true")
        if not self._bool(self.session_approved):
            blockers.append("session_approved=true")
        if not self._bool(self.risk_limits_ok):
            blockers.append("risk_limits_ok=true")
        if not self._audit_log_ready():
            blockers.append("audit_log_ready=true")
        if not self._fill_reconciliation_ready():
            blockers.append("live fill reconciliation is not implemented")
        manual_reconciliation_store_reason = self._manual_reconciliation_store_dependency_reason()
        if manual_reconciliation_store_reason:
            blockers.append(manual_reconciliation_store_reason)
        manual_reconciliation_reason = self._ready_manual_reconciliation_blocker_reason()
        if manual_reconciliation_reason:
            blockers.append("live_manual_reconciliation_required")
        if not self._managed_position_ledger_ready():
            blockers.append("managed live position ledger is not available")
        return tuple(dict.fromkeys(blockers))

    @staticmethod
    def _pending_order_org_no(pending: PendingLiveOrder, reconciliation: object) -> str:
        if pending.order_org_no:
            return pending.order_org_no
        execution = getattr(reconciliation, "execution", None)
        return str(getattr(execution, "order_org_no", "") or "")

    @staticmethod
    def _pending_order_age(pending: PendingLiveOrder) -> timedelta:
        now = datetime.now(tz=pending.submitted_at.tzinfo) if pending.submitted_at.tzinfo else datetime.now()
        return max(timedelta(0), now - pending.submitted_at)

    def _block_live_orders_for_manual_reconciliation(
        self,
        *,
        order: Order,
        order_no: str,
        reason: str,
    ) -> None:
        if not self._manual_reconciliation_blocker:
            self._manual_reconciliation_blocker = reason
            self._manual_reconciliation_blocker_source = "in_memory"
        blocker = ManualReconciliationBlocker(
            reason=reason,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_no=order_no,
            created_at=datetime.now(),
        )
        persisted = False
        if self.manual_reconciliation_store is not None:
            try:
                self.manual_reconciliation_store.ensure_ready()
                self.manual_reconciliation_store.latch(blocker)
                persisted = (
                    bool(getattr(self.manual_reconciliation_store, "is_durable", False))
                    and self.manual_reconciliation_store.blocker() is not None
                )
                if persisted:
                    self._manual_reconciliation_blocker = reason
                    self._manual_reconciliation_blocker_source = "manual_store"
            except Exception as exc:
                self._record_audit_best_effort(
                    "live_manual_reconciliation_store_update_failed",
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "quantity": order.quantity,
                        "order_no": order_no,
                        "reason": self._redact(str(exc)),
                    },
                )
        event = (
            "live_order_manual_reconciliation_blocker_latched"
            if persisted
            else "live_order_manual_reconciliation_blocker_in_memory_only"
        )
        self._record_audit_best_effort(
            event,
            {
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "order_no": order_no,
                "reason": reason,
            },
        )

    def _manual_reconciliation_blocker_reason(self) -> str:
        if self.manual_reconciliation_store is None:
            return self._manual_reconciliation_blocker
        try:
            self.manual_reconciliation_store.ensure_ready()
            blocker = self.manual_reconciliation_store.blocker()
        except Exception as exc:
            if self._manual_reconciliation_blocker:
                return self._manual_reconciliation_blocker
            reason = f"manual_reconciliation_store_unavailable: {self._redact(str(exc))}"
            self._manual_reconciliation_blocker = reason
            self._manual_reconciliation_blocker_source = "store_unavailable"
            self._record_audit_best_effort(
                "live_manual_reconciliation_store_unavailable",
                {"reason": reason},
            )
            return reason
        if blocker is None:
            if self._manual_reconciliation_blocker_source in {"manual_store", "store_unavailable"}:
                self._manual_reconciliation_blocker = ""
                self._manual_reconciliation_blocker_source = ""
            if self._manual_reconciliation_blocker_source == "in_memory":
                return self._manual_reconciliation_blocker
            return ""
        self._manual_reconciliation_blocker = blocker.reason
        self._manual_reconciliation_blocker_source = "manual_store"
        return blocker.reason

    def _ready_manual_reconciliation_blocker_reason(self) -> str:
        if self.manual_reconciliation_store is None:
            return self._manual_reconciliation_blocker or ""
        if self._manual_reconciliation_store_dependency_reason():
            return self._manual_reconciliation_blocker or ""
        return self._manual_reconciliation_blocker_reason()

    def _clear_pending_order(self, order_no: str) -> bool:
        if self.pending_order_store is None:
            return False
        try:
            self.pending_order_store.remove(order_no)
        except Exception as exc:
            self._record_audit_best_effort(
                "live_pending_order_store_update_failed",
                {
                    "order_no": order_no,
                    "reason": self._redact(str(exc)),
                },
            )
            return False
        return True

    def _clear_submission_guard(self, guard_order_no: str, *, actual_order_no: str = "") -> bool:
        if not guard_order_no or guard_order_no == actual_order_no:
            return True
        return self._clear_pending_order(guard_order_no)

    def _pending_tracking_is_scopable(
        self,
        *,
        order: Order,
        guard_order_no: str,
        actual_order_no: str,
        tracked: bool,
    ) -> bool:
        if not tracked:
            return False
        if self._clear_submission_guard(guard_order_no, actual_order_no=actual_order_no):
            return True
        self._block_live_orders_for_manual_reconciliation(
            order=order,
            order_no=actual_order_no or guard_order_no,
            reason="submission guard cleanup failed",
        )
        return False

    def _redact(self, text: str) -> str:
        return redact_sensitive_text(text, extra_values=self._redact_values)

    def _record_audit_best_effort(self, event: str, payload: Mapping[str, object]) -> bool:
        try:
            self.audit_log.record(event, payload)
        except Exception:
            return False
        return True

    def _existing_position_adoption_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not live_order_gate_configured(self.config, self.env):
            blockers.append("live_order_gate_configured=true")
        expected_suffix = self.expected_account_suffix.strip() or self._env_live_account_suffix()
        if not expected_suffix:
            blockers.append("account_confirmation=<live account suffix>")
        elif self.account_confirmation.strip() != expected_suffix:
            blockers.append(f"account_confirmation={expected_suffix}")
        if not self._bool(self.market_is_open):
            blockers.append("market_is_open=true")
        if not self._bool(self.session_approved):
            blockers.append("session_approved=true")
        if not self._bool(self.risk_limits_ok):
            blockers.append("risk_limits_ok=true")
        if not self._audit_log_ready():
            blockers.append("audit_log_ready=true")
        if not self._fill_reconciliation_ready():
            blockers.append("live fill reconciliation is not implemented")
        manual_reconciliation_store_reason = self._manual_reconciliation_store_dependency_reason()
        if manual_reconciliation_store_reason:
            blockers.append(manual_reconciliation_store_reason)
        manual_reconciliation_reason = self._ready_manual_reconciliation_blocker_reason()
        if manual_reconciliation_reason:
            blockers.append("live_manual_reconciliation_required")
        if not self._managed_position_ledger_ready():
            blockers.append("managed live position ledger is not available")
        return tuple(dict.fromkeys(blockers))

    @staticmethod
    def _adoptable_existing_position_quantities(
        account: AccountSnapshot,
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        targets: dict[str, int] = {}
        skipped_symbols: list[str] = []
        for symbol, position in account.positions.items():
            sellable_quantity = getattr(position, "sellable_quantity", None)
            if sellable_quantity is None:
                skipped_symbols.append(symbol)
                continue
            target_quantity = min(max(0, int(position.quantity)), max(0, int(sellable_quantity)))
            if target_quantity <= 0:
                continue
            targets[symbol] = target_quantity
        return targets, tuple(skipped_symbols)

    def _align_managed_position_ledger(
        self,
        targets: Mapping[str, int],
        *,
        account: AccountSnapshot,
        timestamp: datetime | None = None,
        preserve_symbols: set[str] | None = None,
        unknown_symbols: set[str] | None = None,
    ) -> None:
        if self.managed_position_ledger is None:
            raise RuntimeError("managed live position ledger is not available")
        self.managed_position_ledger.ensure_ready()
        current_quantities = self.managed_position_ledger.all()
        account_quantity_confirmations = getattr(
            self.managed_position_ledger,
            "account_quantity_confirmations",
            None,
        )
        account_quantity_confirmation_for = getattr(
            self.managed_position_ledger,
            "account_quantity_confirmation_for",
            None,
        )
        confirmation_symbols = (
            set(account_quantity_confirmations())
            if callable(account_quantity_confirmations)
            else set()
        )
        lifecycle_is_known = getattr(
            self.managed_position_ledger,
            "position_lifecycle_is_known",
            None,
        )
        preserved = set(preserve_symbols or ())
        unknown = set(unknown_symbols or ())
        for symbol in sorted(
            (set(current_quantities) | set(targets) | confirmation_symbols)
            - preserved
        ):
            if symbol in unknown:
                continue
            current_quantity = max(0, int(current_quantities.get(symbol, 0)))
            target_quantity = max(0, int(targets.get(symbol, 0)))
            reconcile_confirmation = getattr(
                self.managed_position_ledger,
                "reconcile_account_quantity_confirmation",
                None,
            )
            account_position = account.positions.get(symbol)
            observed_account_quantity = max(
                0,
                int(getattr(account_position, "quantity", 0))
                if account_position is not None
                else 0,
            )
            expected_account_quantity = (
                account_quantity_confirmation_for(symbol)
                if callable(account_quantity_confirmation_for)
                else None
            )
            if expected_account_quantity is not None and (
                observed_account_quantity != expected_account_quantity
                or target_quantity != expected_account_quantity
            ):
                self._record_audit_best_effort(
                    "live_existing_position_account_confirmation_pending",
                    {
                        "symbol": symbol,
                        "observed_quantity": observed_account_quantity,
                        "observed_sellable_quantity": target_quantity,
                    },
                )
                continue
            if callable(reconcile_confirmation) and not bool(
                reconcile_confirmation(symbol, observed_account_quantity)
            ):
                self._record_audit_best_effort(
                    "live_existing_position_account_confirmation_pending",
                    {
                        "symbol": symbol,
                        "observed_quantity": observed_account_quantity,
                    },
                )
                continue
            lifecycle_was_known = (
                bool(lifecycle_is_known(symbol))
                if callable(lifecycle_is_known)
                else True
            )
            if target_quantity > current_quantity:
                self.managed_position_ledger.add(symbol, target_quantity - current_quantity)
            elif current_quantity > target_quantity:
                self.managed_position_ledger.subtract(symbol, current_quantity - target_quantity)
            if target_quantity > 0:
                position = account.positions.get(symbol)
                if position is not None:
                    self.managed_position_ledger.initialize_lifecycle(
                        symbol,
                        timestamp or position.opened_at,
                        position.last_price,
                        preserve_unknown=not lifecycle_was_known,
                    )

    def _env_live_account_suffix(self) -> str:
        account = str(self.env.get("KIS_LIVE_ACCOUNT_NO") or "").strip()
        return account[-2:] if len(account) >= 2 else account

    def order_dependencies_ready(self) -> bool:
        return (
            self._audit_log_ready()
            and self._fill_reconciliation_ready()
            and self._manual_reconciliation_store_ready()
            and self._managed_position_ledger_ready()
        )

    def _audit_log_ready(self) -> bool:
        try:
            self.audit_log.record("live_audit_log_ready", {"source": "order_dependencies_ready"})
        except Exception:
            return False
        return True

    def order_submission_blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        manual_reconciliation_reason = self._ready_manual_reconciliation_blocker_reason()
        if manual_reconciliation_reason:
            blockers.append(f"live manual reconciliation required: {manual_reconciliation_reason}")
        manual_reconciliation_store_reason = self._manual_reconciliation_store_dependency_reason()
        if manual_reconciliation_store_reason:
            blockers.append("live manual reconciliation store unavailable")
        pending_sync = self.sync_pending_order_statuses(consume_fills=False)
        pending_unavailable_reason = pending_sync.unavailable_reason
        if pending_unavailable_reason == "live_pending_order_sync_unavailable":
            blockers.append("live pending order synchronization unavailable")
        if pending_unavailable_reason == "live_pending_order_store_unavailable":
            blockers.append("live pending order store unavailable")
        if pending_sync.fills:
            blockers.append(f"live pending order fills require runtime sync: {len(pending_sync.fills)}")
        if pending_sync.remaining:
            blockers.append(f"live pending orders unresolved: {len(pending_sync.remaining)}")
        return tuple(blockers)

    def _fill_reconciliation_ready(self) -> bool:
        if self.fill_reconciler is None or self.pending_order_store is None:
            return False
        if not bool(getattr(self.pending_order_store, "is_durable", False)):
            self.audit_log.record("live_pending_order_store_not_durable", {})
            return False
        try:
            self.pending_order_store.ensure_ready()
        except Exception as exc:
            self.audit_log.record(
                "live_pending_order_store_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return False
        return True

    def _manual_reconciliation_store_ready(self) -> bool:
        return self._manual_reconciliation_store_dependency_reason() == ""

    def _manual_reconciliation_store_dependency_reason(self) -> str:
        if self.manual_reconciliation_store is None:
            self._record_audit_best_effort("live_manual_reconciliation_store_missing", {})
            return "live_manual_reconciliation_store_unavailable"
        if not bool(getattr(self.manual_reconciliation_store, "is_durable", False)):
            self._record_audit_best_effort("live_manual_reconciliation_store_not_durable", {})
            return "live_manual_reconciliation_store_unavailable"
        try:
            self.manual_reconciliation_store.ensure_ready()
        except Exception as exc:
            self._record_audit_best_effort(
                "live_manual_reconciliation_store_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return "live_manual_reconciliation_store_unavailable"
        return ""

    def _managed_position_ledger_ready(self, *, account: AccountSnapshot | None = None) -> bool:
        if self.managed_position_ledger is None:
            self._record_audit_best_effort("live_managed_position_ledger_missing", {})
            return False
        if not bool(getattr(self.managed_position_ledger, "is_durable", False)):
            self._record_audit_best_effort("live_managed_position_ledger_not_durable", {})
            return False
        try:
            self.managed_position_ledger.ensure_ready()
            if account is not None:
                managed_quantities = self.managed_position_ledger.all()
                drift_symbols = self._managed_position_ledger_drift_symbols(managed_quantities, account)
                if drift_symbols:
                    self._record_audit_best_effort(
                        "live_managed_position_ledger_drift_denied",
                        {"symbols": tuple(sorted(drift_symbols))},
                    )
                    return False
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_position_ledger_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return False
        return True

    def _managed_entry_state_blocker(self, order: Order, timestamp: datetime) -> str:
        if order.side != "BUY":
            return ""
        if self.managed_position_ledger is None:
            return "live_managed_position_ledger_unavailable"
        entry_counts_are_known = getattr(
            self.managed_position_ledger,
            "entry_counts_are_known",
            None,
        )
        if not callable(entry_counts_are_known):
            return "live_entry_count_state_unavailable"
        try:
            if not bool(entry_counts_are_known(timestamp.date())):
                return "live_entry_count_unknown"
            lifecycle_is_known = getattr(
                self.managed_position_ledger,
                "position_lifecycle_is_known",
                None,
            )
            if not callable(lifecycle_is_known):
                return "live_position_lifecycle_state_unavailable"
            if (
                self.managed_position_ledger.quantity_for(order.symbol) > 0
                and not bool(lifecycle_is_known(order.symbol))
            ):
                return "live_position_lifecycle_unknown"
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_entry_state_unavailable",
                {
                    "symbol": order.symbol,
                    "reason": self._redact(str(exc)),
                },
            )
            return "live_entry_count_state_unavailable"
        return ""

    def _account_quantity_confirmation_blocker(
        self,
        symbol: str,
        account: AccountSnapshot,
    ) -> str:
        ledger = self.managed_position_ledger
        confirmation_for = getattr(ledger, "account_quantity_confirmation_for", None)
        reconcile_confirmation = getattr(
            ledger,
            "reconcile_account_quantity_confirmation",
            None,
        )
        if not callable(confirmation_for) or not callable(reconcile_confirmation):
            return ""
        try:
            expected_quantity = confirmation_for(symbol)
            if expected_quantity is None:
                return ""
            position = account.positions.get(symbol)
            observed_quantity = max(
                0,
                int(getattr(position, "quantity", 0))
                if position is not None
                else 0,
            )
            sellable_quantity = (
                getattr(position, "sellable_quantity", None)
                if position is not None
                else 0
            )
            if sellable_quantity is None:
                return "live_account_quantity_confirmation_pending"
            observed_sellable_quantity = min(
                observed_quantity,
                max(0, int(sellable_quantity)),
            )
            if (
                observed_quantity != expected_quantity
                or observed_sellable_quantity != expected_quantity
            ):
                return "live_account_quantity_confirmation_pending"
            if reconcile_confirmation(symbol, observed_quantity):
                return ""
        except Exception as exc:
            self._record_audit_best_effort(
                "live_account_quantity_confirmation_unavailable",
                {"symbol": symbol, "reason": self._redact(str(exc))},
            )
            return "live_account_quantity_confirmation_unavailable"
        return "live_account_quantity_confirmation_pending"

    @staticmethod
    def _managed_position_ledger_drift_symbols(
        managed_quantities: Mapping[str, int],
        account: AccountSnapshot,
    ) -> tuple[str, ...]:
        drift_symbols: list[str] = []
        for symbol, ledger_quantity in managed_quantities.items():
            broker_quantity = account.positions.get(symbol).quantity if symbol in account.positions else 0
            if int(ledger_quantity) > int(broker_quantity):
                drift_symbols.append(symbol)
        return tuple(drift_symbols)

    @staticmethod
    def _bool(provider: BoolProvider) -> bool:
        if callable(provider):
            return bool(provider())
        return bool(provider)

    @staticmethod
    def _pending_fill_key(pending: PendingLiveOrder) -> str:
        return LiveBroker._live_fill_key(
            timestamp=pending.submitted_at,
            order_no=pending.order_no,
            symbol=pending.symbol,
            side=pending.side,
        )

    @staticmethod
    def _live_fill_key(
        *,
        timestamp: datetime,
        order_no: str,
        symbol: str,
        side: str,
    ) -> str:
        submitted_date = timestamp.date().isoformat()
        return f"{submitted_date}:{order_no}:{symbol}:{side}"

    def _managed_position_ledger_consumed_quantity(self, fill_key: str) -> int:
        if self.managed_position_ledger is None:
            return 0
        try:
            return max(0, int(self.managed_position_ledger.consumed_quantity_for(fill_key)))
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_position_ledger_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return 0

    def _pending_fill_incremental_price(
        self,
        *,
        fill_key: str,
        fill_delta: int,
        cumulative_filled: int,
        cumulative_average_price: Decimal,
        already_consumed: int,
        fallback_price: Decimal,
    ) -> Decimal | None:
        parsed_delta = max(0, int(fill_delta))
        parsed_cumulative = max(0, int(cumulative_filled))
        average_price = Decimal(str(cumulative_average_price or "0"))
        fallback = Decimal(str(fallback_price or "0"))
        if parsed_delta <= 0:
            return None
        if not average_price.is_finite() or not fallback.is_finite():
            return None
        if average_price <= 0:
            return fallback if already_consumed <= 0 and fallback > 0 else None
        if already_consumed <= 0:
            return average_price
        if parsed_cumulative <= already_consumed or self.managed_position_ledger is None:
            return None
        reader = getattr(self.managed_position_ledger, "consumed_notional_for", None)
        if not callable(reader):
            return None
        try:
            prior_notional = reader(fill_key)
            if prior_notional is None:
                return None
            prior_notional = Decimal(str(prior_notional))
        except Exception as exc:
            self._record_audit_best_effort(
                "live_managed_position_ledger_unavailable",
                {"reason": self._redact(str(exc))},
            )
            return None
        if not prior_notional.is_finite() or prior_notional < 0:
            return None
        incremental_notional = (average_price * Decimal(parsed_cumulative)) - prior_notional
        if not incremental_notional.is_finite() or incremental_notional <= 0:
            return None
        incremental_price = incremental_notional / Decimal(parsed_delta)
        return incremental_price if incremental_price > 0 else None

    @staticmethod
    def _pending_fill_delta(
        *,
        previous_remaining: int,
        reconciliation_filled: int,
        reconciliation_unfilled: int,
        already_consumed: int = 0,
    ) -> int:
        if reconciliation_filled <= 0:
            return 0
        if already_consumed > 0:
            return max(0, reconciliation_filled - already_consumed)
        if reconciliation_unfilled > 0:
            return max(0, previous_remaining - reconciliation_unfilled)
        return max(0, previous_remaining)

    @staticmethod
    def _manual_reconciliation_order_no(order: Order, timestamp: datetime) -> str:
        return f"manual:{timestamp.strftime('%Y%m%d%H%M%S%f')}:{order.symbol}:{order.side}"

    @staticmethod
    def _pending_fill_timestamp(
        pending: PendingLiveOrder,
        query_date: date,
        reconciliation: object,
    ) -> datetime:
        execution = getattr(reconciliation, "execution", None)
        order_time = str(getattr(execution, "order_time", "") or "")
        digits = "".join(character for character in order_time if character.isdigit())
        if len(digits) >= 6:
            try:
                fill_time = datetime.strptime(digits[:6], "%H%M%S").time()
                if pending.submitted_at.tzinfo is None or pending.submitted_at.utcoffset() is None:
                    return datetime.combine(query_date, fill_time)
                korea_timestamp = datetime.combine(query_date, fill_time, tzinfo=KOREA_TIMEZONE)
                return korea_timestamp.astimezone(pending.submitted_at.tzinfo)
            except ValueError:
                pass
        return pending.submitted_at


def _reconciliation_order_org_no(reconciliation: object) -> str:
    execution = getattr(reconciliation, "execution", None)
    return str(getattr(execution, "order_org_no", "") or "")


def _kis_response_rows(response: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(response, Mapping):
        return ()
    rows = response.get("output")
    if isinstance(rows, list):
        return tuple(row for row in rows if isinstance(row, Mapping))
    rows = response.get("output1")
    if isinstance(rows, list):
        return tuple(row for row in rows if isinstance(row, Mapping))
    row = response.get("output")
    if isinstance(row, Mapping):
        return (row,)
    return ()


def _kis_response_success(response: object) -> bool:
    if not isinstance(response, Mapping):
        return False
    rt_cd = response.get("rt_cd")
    if rt_cd not in (None, ""):
        return str(rt_cd).strip() == "0"
    return "output" in response or "output1" in response


def _kis_response_tr_cont(response: object) -> str:
    if not isinstance(response, Mapping):
        return ""
    return _first_string(response, "tr_cont", "TR_CONT").upper()


def _kis_response_continuation_keys(response: object) -> tuple[str, str]:
    if not isinstance(response, Mapping):
        return "", ""
    fk100 = _first_string(response, "ctx_area_fk100", "CTX_AREA_FK100")
    nk100 = _first_string(response, "ctx_area_nk100", "CTX_AREA_NK100")
    if fk100 or nk100:
        return fk100, nk100
    output2 = response.get("output2")
    if isinstance(output2, Mapping):
        return (
            _first_string(output2, "ctx_area_fk100", "CTX_AREA_FK100"),
            _first_string(output2, "ctx_area_nk100", "CTX_AREA_NK100"),
        )
    if isinstance(output2, list) and output2 and isinstance(output2[0], Mapping):
        first = output2[0]
        return (
            _first_string(first, "ctx_area_fk100", "CTX_AREA_FK100"),
            _first_string(first, "ctx_area_nk100", "CTX_AREA_NK100"),
        )
    return "", ""


def _account_day_pnl(account: AccountSnapshot) -> Decimal:
    unrealized = sum(position.unrealized_pnl for position in account.positions.values())
    return account.realized_pnl_today + unrealized


def _kis_response_result(response: object) -> dict[str, str]:
    if not isinstance(response, Mapping):
        return {"response_type": type(response).__name__}
    result: dict[str, str] = {}
    for key in ("rt_cd", "msg_cd", "msg1", "msg"):
        value = response.get(key)
        if value not in (None, ""):
            result[key] = redact_sensitive_text(str(value))
    return result


def _first_string(row: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _first_int(row: Mapping[str, object], *keys: str) -> int:
    raw = _first_string(row, *keys).replace(",", "")
    if not raw:
        return 0
    try:
        return max(0, int(Decimal(raw)))
    except Exception:
        return 0
