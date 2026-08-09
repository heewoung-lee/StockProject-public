import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

from stockbot.broker import PaperBroker
from stockbot.config import BotConfig
from stockbot.live_position_ledger import InMemoryManagedLivePositionLedger
from stockbot.market_hours import KST, KoreanRegularMarketHours
from stockbot.models import AccountSnapshot, Fill, MarketBar, Order, Position, Signal
from stockbot.rate_limit import RateLimitDecision
from stockbot.risk import RiskConfig, RiskManager
from stockbot.runtime import (
    CustomStrategySettings,
    PaperTradingRuntime,
    RuntimeEvent,
    RuntimeStatus,
    _market_data_error_message,
)
from stockbot.scanner import BarProviderScanner, JsonScannerProvider, ScannerCandidate, ScannerSnapshot, StaticScannerProvider
from stockbot.signal_scoring import SignalScore
from stockbot.strategy import FlowScalperConfig, FlowScalperStrategy
from stockbot.symbols import SymbolDirectory


def _bar(symbol="005930", close="10000", offset=0):
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 11, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        vwap=price,
        bid=price,
        ask=price,
        temporary_stop=False,
        trading_state_source="KIS_CURRENT_PRICE",
    )


class DictBarProvider:
    def __init__(self, bars):
        self.bars = bars
        self.requested_symbols = []

    def __call__(self, symbol):
        self.requested_symbols.append(symbol)
        return self.bars[symbol]


class SequenceBarProvider:
    def __init__(self, bars_by_symbol):
        self.bars_by_symbol = bars_by_symbol
        self.next_indexes = {}
        self.requested_symbols = []

    def __call__(self, symbol):
        self.requested_symbols.append(symbol)
        bars = self.bars_by_symbol[symbol]
        index = self.next_indexes.get(symbol, 0)
        self.next_indexes[symbol] = index + 1
        return bars[min(index, len(bars) - 1)]


class SequenceScannerProvider:
    label = "sequence scanner"
    kind = "sequence"

    def __init__(self, bars_by_symbol, priorities=None):
        self.bars_by_symbol = {symbol: tuple(bars) for symbol, bars in bars_by_symbol.items()}
        self.priorities = dict(priorities or {})
        self.next_indexes = {}
        self.requested_symbols = []

    def snapshot(self, symbols):
        requested = list(dict.fromkeys(symbols))
        self.requested_symbols.append(tuple(requested))
        bars = {}
        candidates = []
        for symbol in requested:
            symbol_bars = self.bars_by_symbol.get(symbol, ())
            if not symbol_bars:
                continue
            index = self.next_indexes.get(symbol, 0)
            self.next_indexes[symbol] = index + 1
            bars[symbol] = symbol_bars[min(index, len(symbol_bars) - 1)]
            candidates.append(ScannerCandidate(symbol=symbol, priority=self.priorities.get(symbol, 0.0)))
        return ScannerSnapshot(bars=bars, candidates=tuple(candidates))

    def rank_symbols(self, symbols):
        requested = list(dict.fromkeys(symbols)) or list(self.bars_by_symbol)
        return sorted(requested, key=lambda symbol: -self.priorities.get(symbol, 0.0))


class RaisingBarProvider:
    def __init__(self):
        self.requested_symbols = []

    def __call__(self, symbol):
        self.requested_symbols.append(symbol)
        raise RuntimeError("secret-token-123")


class DiagnosticBarProvider:
    def __init__(self, message):
        self.message = message
        self.requested_symbols = []

    def __call__(self, symbol):
        self.requested_symbols.append(symbol)
        raise RuntimeError(self.message)


class FixedSignalStrategy:
    def __init__(self, signals):
        self.signals = signals
        self.seen_symbols = []

    def on_bar(self, bar, account):
        self.seen_symbols.append(bar.symbol)
        return list(self.signals.get(bar.symbol, ()))

    def revalidate_signal(self, provisional_signal, provisional_bar, final_bar, account):
        if provisional_signal.symbol != provisional_bar.symbol or final_bar.symbol != provisional_bar.symbol:
            return None
        return provisional_signal


class RefillOnSecondVisitStrategy(FixedSignalStrategy):
    def __init__(self):
        super().__init__({})
        self.visits = {}

    def on_bar(self, bar, account):
        self.seen_symbols.append(bar.symbol)
        self.visits[bar.symbol] = self.visits.get(bar.symbol, 0) + 1
        if bar.symbol == "EXIT01" and bar.symbol in account.positions:
            return [Signal.sell(bar.symbol, "take_profit")]
        if bar.symbol == "BUY002" and self.visits[bar.symbol] >= 2:
            return [Signal.buy(bar.symbol, "replacement_entry")]
        return []


class ExitAndReenterSameSymbolStrategy(FixedSignalStrategy):
    def __init__(self):
        super().__init__({})

    def on_bar(self, bar, account):
        self.seen_symbols.append(bar.symbol)
        if bar.symbol in account.positions:
            return [Signal.sell(bar.symbol, "take_profit"), Signal.buy(bar.symbol, "same_symbol_reentry")]
        return []


class HoldScoreStrategy(FixedSignalStrategy):
    def __init__(self, score):
        super().__init__({})
        self.score = score

    def last_entry_score(self, symbol):
        return self.score


class WarmupOnlyStrategy(FixedSignalStrategy):
    def __init__(self):
        super().__init__({})
        self.config = FlowScalperConfig()


class RaisingStrategy(FixedSignalStrategy):
    def __init__(self):
        super().__init__({})

    def on_bar(self, bar, account):
        raise RuntimeError("secret-token-123")


class RankedSignalStrategy(FixedSignalStrategy):
    def __init__(self, scores):
        super().__init__({symbol: [Signal.buy(symbol, f"score_{score}")] for symbol, score in scores.items()})
        self.scores = scores

    def last_entry_score(self, symbol):
        score = self.scores.get(symbol, 0.0)
        return SignalScore(
            symbol=symbol,
            long_score=score,
            short_score=0.0,
            confidence=score,
            direction="long" if score else "hold",
            reasons=("ranked_test",),
        )


class ConfigurableSignalStrategy(FixedSignalStrategy):
    def __init__(self, signals):
        super().__init__(signals)
        self.config = FlowScalperConfig()


class ApprovingRiskManager:
    def __init__(self):
        self.results = []
        self.checked_symbols = []
        self.entries = []

    def check(self, order, account, estimated_price, as_of=None):
        self.checked_symbols.append(order.symbol)

        class Decision:
            approved = True
            reason = ""

        return Decision()

    def record_order_result(self, accepted):
        self.results.append(accepted)

    def record_entry(self, symbol, as_of=None):
        self.entries.append((symbol, as_of))


class BlockingRateLimiter:
    def __init__(self):
        self.recorded = []

    def allow_request(self, kind="query"):
        return RateLimitDecision(False, 12.5, "token_cooldown")

    def record_request(self, kind="query"):
        self.recorded.append(kind)


class RuntimeSyncResult:
    def __init__(self, *, remaining=(), fills=(), store_unavailable=False, sync_unavailable=False):
        self.remaining = tuple(remaining)
        self.fills = tuple(fills)
        self.store_unavailable = store_unavailable
        self.sync_unavailable = sync_unavailable


class PendingMarker:
    def __init__(
        self,
        symbol,
        side="BUY",
        *,
        remaining_quantity=0,
        estimated_price=Decimal("0"),
        reason="pending",
    ):
        self.symbol = symbol
        self.side = side
        self.remaining_quantity = remaining_quantity
        self.estimated_price = estimated_price
        self.reason = reason


class PendingSyncBroker:
    def __init__(self, result):
        self.result = result
        self.place_order_calls = []
        self.updated_symbols = []

    def snapshot(self, *, timestamp=None):
        return AccountSnapshot(cash=Decimal("1000000"))

    def update_market(self, bar):
        self.updated_symbols.append(bar.symbol)

    def sync_pending_order_statuses(self):
        return self.result

    def place_order(self, order, bar):
        self.place_order_calls.append((order, bar))
        raise AssertionError("place_order should not be called while pending live orders remain")


class PendingSellSyncBroker(PendingSyncBroker):
    def place_order(self, order, bar):
        self.place_order_calls.append((order, bar))
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=order.quantity,
        )


class ScopedPendingBroker:
    def __init__(self, *, pending, positions):
        self.pending = pending
        self._snapshot = AccountSnapshot(cash=Decimal("1000000"), positions=positions)
        self.orders = []
        self.updated_symbols = []

    def snapshot(self, *, timestamp=None):
        return self._snapshot

    def update_market(self, bar):
        self.updated_symbols.append(bar.symbol)

    def sync_pending_order_statuses(self):
        return RuntimeSyncResult(remaining=(self.pending,))

    def place_order(self, order, bar):
        self.orders.append(order)
        if order.symbol == self.pending.symbol:
            return Fill(
                order=order,
                accepted=False,
                timestamp=bar.timestamp,
                reject_reason="live_pending_orders_unresolved",
            )
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=order.quantity,
        )


class LivePendingAfterSubmissionBroker:
    def __init__(self, reject_reason="live_order_pending"):
        opened_at = datetime(2026, 6, 11, 9, 0)
        self._snapshot = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "EXIT01": Position(
                    symbol="EXIT01",
                    quantity=2,
                    avg_price=Decimal("10000"),
                    last_price=Decimal("10000"),
                    opened_at=opened_at,
                    highest_price=Decimal("10000"),
                    sellable_quantity=2,
                    managed_quantity=2,
                )
            },
        )
        self.reject_reason = reject_reason
        self.orders = []
        self.updated_symbols = []

    def snapshot(self, *, timestamp=None):
        return self._snapshot

    def update_market(self, bar):
        self.updated_symbols.append(bar.symbol)

    def sync_pending_order_statuses(self):
        return RuntimeSyncResult()

    def place_order(self, order, bar):
        self.orders.append(order)
        if order.side == "SELL":
            return Fill(
                order=order,
                accepted=False,
                timestamp=bar.timestamp,
                reject_reason=self.reject_reason,
            )
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=order.quantity,
        )


class PendingEntryBudgetBroker:
    def __init__(self, fill):
        self.fill = fill
        self.synced = False
        self.place_order_calls = []

    def snapshot(self, *, timestamp=None):
        if not self.synced:
            return AccountSnapshot(cash=Decimal("1000000"))
        position = Position(
            symbol=self.fill.order.symbol,
            quantity=self.fill.quantity,
            avg_price=self.fill.price,
            last_price=self.fill.price,
            opened_at=self.fill.timestamp,
            highest_price=self.fill.price,
        )
        return AccountSnapshot(cash=Decimal("700000"), positions={position.symbol: position})

    def update_market(self, bar):
        return None

    def sync_pending_order_statuses(self):
        self.synced = True
        return RuntimeSyncResult(fills=(self.fill,))

    def place_order(self, order, bar):
        self.place_order_calls.append((order, bar))
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=order.quantity,
        )


class PartialFillBroker:
    def __init__(self, *, filled_quantity):
        self.filled_quantity = filled_quantity
        self.orders = []

    def snapshot(self, *, timestamp=None):
        return AccountSnapshot(cash=Decimal("1000000"))

    def update_market(self, bar):
        return None

    def place_order(self, order, bar):
        self.orders.append(order)
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=self.filled_quantity,
        )


class StaleLiveSnapshotBroker:
    def __init__(self, *, snapshot: AccountSnapshot):
        self._snapshot = snapshot
        self.orders = []
        self.snapshot_calls = 0

    def snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        return self._snapshot

    def update_market(self, bar):
        return None

    def place_order(self, order, bar):
        self.orders.append(order)
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=order.quantity,
        )


class StartAdoptingManagedLiveBroker(StaleLiveSnapshotBroker):
    def __init__(self, *, snapshot: AccountSnapshot):
        super().__init__(snapshot=snapshot)
        self.managed_position_ledger = InMemoryManagedLivePositionLedger()
        self.adoption_calls = 0

    def snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        return self.overlay_managed_positions(self._snapshot)

    def adopt_existing_account_positions(self, *, account):
        self.adoption_calls += 1
        targets = {
            symbol: min(position.quantity, position.sellable_quantity or 0)
            for symbol, position in account.positions.items()
            if min(position.quantity, position.sellable_quantity or 0) > 0
        }
        current = self.managed_position_ledger.all()
        for symbol in set(current) | set(targets):
            quantity = targets.get(symbol, 0)
            current_quantity = self.managed_position_ledger.quantity_for(symbol)
            if quantity > current_quantity:
                self.managed_position_ledger.add(symbol, quantity - current_quantity)
            elif current_quantity > quantity:
                self.managed_position_ledger.subtract(symbol, current_quantity - quantity)
        return targets

    def overlay_managed_positions(self, account):
        return replace(
            account,
            positions={
                symbol: replace(
                    position,
                    managed_quantity=self.managed_position_ledger.quantity_for(symbol),
                )
                for symbol, position in account.positions.items()
            },
        )

    def place_order(self, order, bar):
        fill = super().place_order(order, bar)
        if order.side == "SELL":
            self.managed_position_ledger.subtract(order.symbol, order.quantity)
        return fill


class PendingBuyStartAdoptingManagedLiveBroker(StartAdoptingManagedLiveBroker):
    def __init__(self, *, snapshot: AccountSnapshot, pending_symbol: str):
        super().__init__(snapshot=snapshot)
        self.pending_symbol = pending_symbol

    def sync_pending_order_statuses(self):
        return RuntimeSyncResult(
            remaining=(PendingMarker(self.pending_symbol, side="BUY"),),
        )


class PlanningCashLiveBroker(StaleLiveSnapshotBroker):
    def __init__(self, *, snapshot: AccountSnapshot, planning_cash: Decimal, blocker: str = ""):
        super().__init__(snapshot=snapshot)
        self.planning_cash = planning_cash
        self.blocker = blocker
        self.planning_calls = []
        self.cached_cash = planning_cash

    def refresh_planning_account(self, account, market_bar):
        self.planning_calls.append((account, market_bar))
        return replace(account, buying_power_override=self.planning_cash), self.blocker

    def cached_buying_power(self):
        return self.cached_cash

    def place_order(self, order, bar):
        fill = super().place_order(order, bar)
        if order.side == "BUY":
            self.cached_cash = max(
                Decimal("0"),
                self.cached_cash - (bar.close * Decimal(order.quantity)),
            )
        return fill


class PendingPlanningCashLiveBroker(PlanningCashLiveBroker):
    def place_order(self, order, bar):
        self.orders.append(order)
        if order.side == "BUY":
            self.cached_cash = max(
                Decimal("0"),
                self.cached_cash - (bar.close * Decimal(order.quantity)),
            )
        return Fill(
            order=order,
            accepted=False,
            timestamp=bar.timestamp,
            reject_reason="live_order_pending",
        )


class RateLimitedLiveSnapshotBroker(StaleLiveSnapshotBroker):
    def __init__(self, *, snapshot: AccountSnapshot, max_snapshot_calls: int):
        super().__init__(snapshot=snapshot)
        self.max_snapshot_calls = max_snapshot_calls

    def snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        if self.snapshot_calls > self.max_snapshot_calls:
            raise RuntimeError('KIS HTTP 500: {"msg_cd":"EGW00215","msg1":"원장 초당 거래건수 초과"}')
        return self._snapshot


class PendingRateLimitedLiveSnapshotBroker(RateLimitedLiveSnapshotBroker):
    def __init__(self, *, snapshot: AccountSnapshot, max_snapshot_calls: int, result):
        super().__init__(snapshot=snapshot, max_snapshot_calls=max_snapshot_calls)
        self.result = result

    def sync_pending_order_statuses(self):
        return self.result

    def place_order(self, order, bar):
        raise AssertionError("place_order should not be called while pending live orders remain")


class AdoptingRateLimitedLiveSnapshotBroker(RateLimitedLiveSnapshotBroker):
    def adopt_existing_account_positions(self):
        return {"HOLD01": 1}


class SnapshotShouldNotRunBroker:
    def __init__(self):
        self.snapshot_calls = 0
        self.updated_symbols = []
        self.orders = []

    def snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        return AccountSnapshot(cash=Decimal("1000000"))

    def update_market(self, bar):
        self.updated_symbols.append(bar.symbol)

    def place_order(self, order, bar):
        self.orders.append(order)
        raise AssertionError("place_order should not be called while market is closed")


class DynamicBudgetClient:
    def __init__(self):
        self.limit = None
        self.used = 0
        self.begin_limits = []
        self.ensure_calls = []
        self.completed_cycles = []

    def begin_market_read_budget(self, limit):
        self.limit = limit
        self.used = 0
        self.begin_limits.append(limit)

    def ensure_market_read_budget(self, minimum_limit):
        self.ensure_calls.append(minimum_limit)
        if self.limit is not None:
            self.limit = max(self.limit, minimum_limit)

    def market_read_budget_state(self):
        if self.limit is None:
            return None
        return self.used, self.limit

    def consume(self, count):
        if self.limit is not None and self.used + count > self.limit:
            raise RuntimeError("KIS physical market read budget exhausted")
        if self.limit is not None:
            self.used += count

    def end_market_read_budget(self):
        if self.limit is not None:
            self.completed_cycles.append((self.used, self.limit))
        self.limit = None
        self.used = 0


class BudgetedOpeningDayGate:
    def __init__(self, client, *, pending=10, reads=10, result=True, error=None):
        self.client = client
        self.pending = pending
        self.reads = reads
        self.result = result
        self.error = error
        self.calls = 0
        self.physical_read_calls = 0

    def pending_market_read_cost(self):
        return self.pending

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.pending > 0:
            self.client.consume(self.reads)
            self.physical_read_calls += 1
        if self.result:
            self.pending = 0
        return self.result


class DynamicBudgetPaperBroker(PaperBroker):
    def __init__(self, *, account_read_cost, initial_cash=Decimal("1000000")):
        super().__init__(initial_cash=initial_cash)
        self.client = DynamicBudgetClient()
        self.account_read_cost = account_read_cost
        self.snapshot_calls = 0
        self.submitted_orders = []

    def snapshot(self, *, timestamp=None):
        self.snapshot_calls += 1
        self.client.consume(self.account_read_cost)
        return super().snapshot()

    def sync_pending_order_statuses(self):
        return RuntimeSyncResult()

    def place_order(self, order, bar):
        self.client.consume(self.account_read_cost)
        self.submitted_orders.append(order)
        return super().place_order(order, bar)


class DynamicBudgetProvider:
    def __init__(self, client, *, reads, values):
        self.client = client
        self.reads = reads
        self.values = values
        self.requested_symbols = []

    def __call__(self, symbol):
        self.requested_symbols.append(symbol)
        self.client.consume(self.reads)
        return self.values[symbol]


class PartialExitStaleLiveSnapshotBroker(StaleLiveSnapshotBroker):
    def __init__(self, *, snapshot: AccountSnapshot, exit_fill_quantity: int):
        super().__init__(snapshot=snapshot)
        self.exit_fill_quantity = exit_fill_quantity

    def place_order(self, order, bar):
        self.orders.append(order)
        quantity = self.exit_fill_quantity if order.side in {"SELL", "SHORT_EXIT"} else order.quantity
        return Fill(
            order=order,
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=quantity,
        )


def make_runtime(
    *,
    symbols=None,
    bars=None,
    signals=None,
    broker=None,
    risk_manager=None,
    strategy=None,
    rate_limiter=None,
    market_hours=None,
    symbol_priority_provider=None,
):
    symbols = symbols or ["005930"]
    bars = bars or {symbol: _bar(symbol=symbol) for symbol in symbols}
    return PaperTradingRuntime(
        symbols=symbols,
        broker=broker or PaperBroker(initial_cash=Decimal("1000000")),
        strategy=strategy or FixedSignalStrategy(signals or {}),
        risk_manager=risk_manager or RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
        bar_provider=DictBarProvider(bars),
        symbol_directory=SymbolDirectory({"005930": "삼성전자"}),
        settings=CustomStrategySettings.default(),
        rate_limiter=rate_limiter,
        market_hours=market_hours,
        symbol_priority_provider=symbol_priority_provider,
    )


def make_runtime_with_settings(*, settings, symbols=None, bars=None, signals=None, broker=None, risk_manager=None):
    symbols = symbols or ["005930"]
    bars = bars or {symbol: _bar(symbol=symbol) for symbol in symbols}
    return PaperTradingRuntime(
        symbols=symbols,
        broker=broker or PaperBroker(initial_cash=Decimal("1000000"), allow_short=settings.allow_paper_short),
        strategy=FixedSignalStrategy(signals or {}),
        risk_manager=risk_manager or RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
        bar_provider=DictBarProvider(bars),
        symbol_directory=SymbolDirectory({"005930": "삼성전자"}),
        settings=settings,
    )


def make_zero_balance_probe_runtime(*, planning_cash, blocker="", held_count=2):
    held_symbols = [f"HOLD{index:02d}" for index in range(1, held_count + 1)]
    entry_symbols = [f"BUY{index:03d}" for index in range(1, 11)]
    entry_symbol = entry_symbols[0]
    positions = {
        symbol: Position(
            symbol=symbol,
            quantity=1,
            avg_price=Decimal("1000"),
            last_price=Decimal("1000"),
            opened_at=datetime(2026, 6, 11, 9, 0),
            highest_price=Decimal("1000"),
        )
        for symbol in held_symbols
    }
    broker = PlanningCashLiveBroker(
        snapshot=AccountSnapshot(
            cash=Decimal("82546"),
            positions=positions,
            buying_power_override=Decimal("0"),
        ),
        planning_cash=Decimal(planning_cash),
        blocker=blocker,
    )

    class ThreeReadAccountClient:
        def begin_market_read_budget(self, limit):
            self.limit = limit

        def market_read_budget_state(self):
            return 3, self.limit

        def end_market_read_budget(self):
            self.limit = None

    broker.client = ThreeReadAccountClient()
    all_symbols = [*held_symbols, *entry_symbols]
    strategy = FixedSignalStrategy({entry_symbol: [Signal.buy(entry_symbol, "entry")]})
    runtime = PaperTradingRuntime(
        symbols=entry_symbols,
        broker=broker,
        strategy=strategy,
        risk_manager=RiskManager(
            RiskConfig(
                max_order_amount=Decimal("0"),
                max_position_amount=Decimal("300000"),
                max_positions=0,
            )
        ),
        bar_provider=DictBarProvider({}),
        final_quote_provider=DictBarProvider(
            {symbol: _bar(symbol=symbol, close="1000", offset=1) for symbol in all_symbols}
        ),
        scanner_provider=StaticScannerProvider(
            bars={symbol: _bar(symbol=symbol, close="1000") for symbol in entry_symbols}
        ),
        settings=CustomStrategySettings.default().with_updates(
            cash_allocation_pct=Decimal("0.70"),
            max_positions=0,
            max_position_amount=Decimal("300000"),
        ),
        data_source_kind="live",
        execution_mode="live",
        max_final_quote_requests_per_cycle=10,
        max_physical_market_reads_per_cycle=14,
    )
    return runtime, broker, strategy, held_symbols, entry_symbol


class RuntimeEventTest(unittest.TestCase):
    def test_trade_event_keeps_company_name_side_and_defaults_to_paper_mode(self):
        event = RuntimeEvent.trade(
            symbol="005930",
            company_name="삼성전자",
            side="BUY",
            quantity=3,
            price=Decimal("70000"),
            reason="flow_breakout",
            result="filled",
            realized_pnl=Decimal("1200"),
        )

        self.assertEqual("trade", event.kind)
        self.assertEqual("삼성전자", event.company_name)
        self.assertEqual("BUY", event.side)
        self.assertEqual(3, event.quantity)
        self.assertEqual(Decimal("70000"), event.price)
        self.assertEqual("flow_breakout", event.reason)
        self.assertEqual("filled", event.result)
        self.assertEqual("paper", event.mode)
        self.assertEqual(Decimal("1200"), event.realized_pnl)

    def test_system_event_defaults_to_system_kind_without_trade_fields(self):
        event = RuntimeEvent.system("자동 모의투자 루프 시작")

        self.assertEqual("system", event.kind)
        self.assertEqual("자동 모의투자 루프 시작", event.message)
        self.assertEqual("", event.symbol)
        self.assertEqual("", event.side)
        self.assertEqual(0, event.quantity)

    def test_status_defaults_to_stopped(self):
        status = RuntimeStatus()

        self.assertEqual("정지", status.label)
        self.assertFalse(status.running)


class CustomStrategySettingsTest(unittest.TestCase):
    def test_default_settings_are_paper_safe(self):
        settings = CustomStrategySettings.default()

        self.assertEqual(Decimal("50000"), settings.order_cash_amount)
        self.assertEqual(Decimal("1.0"), settings.cash_allocation_pct)
        self.assertEqual(Decimal("0"), settings.max_order_amount)
        self.assertEqual(Decimal("300000"), settings.max_position_amount)
        self.assertEqual(Decimal("0.30"), settings.max_symbol_exposure)
        self.assertEqual(0, settings.max_positions)
        self.assertFalse(settings.allow_paper_short)
        self.assertFalse(settings.kill_switch)
        self.assertFalse(hasattr(settings, "allow_live_orders"))

    def test_paper_short_can_be_enabled_for_paper_settings(self):
        settings = CustomStrategySettings.default().with_updates(allow_paper_short=True)

        self.assertTrue(settings.allow_paper_short)
        self.assertFalse(hasattr(settings, "allow_live_orders"))

    def test_boolean_settings_reject_string_values(self):
        with self.assertRaisesRegex(ValueError, "allow_paper_short must be boolean"):
            CustomStrategySettings.default().with_updates(allow_paper_short="false")

        with self.assertRaisesRegex(ValueError, "kill_switch must be boolean"):
            CustomStrategySettings.default().with_updates(kill_switch="true")

    def test_invalid_stop_loss_is_rejected(self):
        with self.assertRaises(ValueError):
            CustomStrategySettings.default().with_updates(stop_loss_pct=Decimal("0"))

    def test_order_cash_above_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            CustomStrategySettings.default().with_updates(order_cash_amount=Decimal("1000001"))


class PaperTradingRuntimeSettingsTest(unittest.TestCase):
    def test_constructor_syncs_initial_paper_short_setting_to_broker(self):
        settings = CustomStrategySettings.default().with_updates(allow_paper_short=True)
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=False),
            strategy=FixedSignalStrategy({"005930": [Signal.short("005930", "downtrend_short")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar()}),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=settings,
        )

        runtime.start()
        events = runtime.run_cycle()

        trade_events = [item for item in events if item.kind == "trade"]
        self.assertEqual("filled", trade_events[0].result)
        self.assertEqual("SHORT_ENTRY", trade_events[0].side)

    def test_apply_strategy_settings_updates_runtime_strategy_risk_and_broker(self):
        settings = CustomStrategySettings.default().with_updates(
            order_cash_amount=Decimal("80000"),
            max_positions=4,
            stop_loss_pct=Decimal("0.03"),
            take_profit_pct=Decimal("0.045"),
            trailing_stop_pct=Decimal("0.02"),
            daily_loss_limit=Decimal("150000"),
            allow_paper_short=True,
        )
        strategy_config = FlowScalperConfig(
            min_momentum_pct=Decimal("0.007"),
            max_spread_bps=Decimal("45"),
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            trailing_stop_pct=settings.trailing_stop_pct,
            allow_paper_short=True,
        )
        risk_config = RiskConfig(
            max_order_amount=Decimal("150000"),
            max_position_amount=Decimal("450000"),
            max_positions=4,
            max_daily_loss=Decimal("150000"),
        )
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=False),
            strategy=ConfigurableSignalStrategy({"005930": [Signal.short("005930", "downtrend_short")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar()}),
            symbol_directory=SymbolDirectory({"005930": "?쇱꽦?꾩옄"}),
            settings=CustomStrategySettings.default(),
        )

        event = runtime.apply_strategy_settings(
            settings=settings,
            strategy_config=strategy_config,
            risk_config=risk_config,
            profile_label="공격형",
        )
        runtime.start()
        events = runtime.run_cycle()

        trade_events = [item for item in events if item.kind == "trade"]
        self.assertEqual(settings, runtime.settings)
        self.assertEqual(strategy_config, runtime.strategy.config)
        self.assertEqual(risk_config, runtime.risk_manager.config)
        self.assertEqual("filled", trade_events[0].result)
        self.assertEqual("SHORT_ENTRY", trade_events[0].side)
        self.assertIn("공격형", event.message)

    def test_apply_strategy_settings_clears_existing_order_failure_lock(self):
        risk_manager = RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_consecutive_order_failures=1))
        risk_manager.record_order_result(False)
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "flow_breakout")]}),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({"005930": _bar()}),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=CustomStrategySettings.default(),
        )

        runtime.apply_strategy_settings(
            settings=CustomStrategySettings.default(),
            risk_config=RiskConfig(max_order_amount=Decimal("100000"), max_consecutive_order_failures=1),
            profile_label="custom",
        )
        runtime.start()
        events = runtime.run_cycle()

        trade_events = [item for item in events if item.kind == "trade"]
        self.assertEqual("filled", trade_events[0].result)

    def test_apply_strategy_settings_does_not_change_broker_account_value(self):
        settings = CustomStrategySettings.default().with_updates(allow_paper_short=True)
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(Order.buy("LONG01", 5, "seed_long"), _bar(symbol="LONG01", close="10000"))
        broker.place_order(Order.short("SHORT1", 4, "seed_short"), _bar(symbol="SHORT1", close="20000"))
        broker.update_market(_bar(symbol="LONG01", close="11000", offset=1))
        broker.update_market(_bar(symbol="SHORT1", close="19000", offset=1))
        runtime = PaperTradingRuntime(
            symbols=["LONG01", "SHORT1"],
            broker=broker,
            strategy=ConfigurableSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider(
                {
                    "LONG01": _bar(symbol="LONG01", close="11000", offset=2),
                    "SHORT1": _bar(symbol="SHORT1", close="19000", offset=2),
                }
            ),
            symbol_directory=SymbolDirectory({"LONG01": "Long Test", "SHORT1": "Short Test"}),
            settings=settings,
        )
        before = runtime.broker.snapshot()

        runtime.apply_strategy_settings(
            settings=CustomStrategySettings.default().with_updates(allow_paper_short=False),
            strategy_config=FlowScalperConfig(allow_paper_short=False),
            risk_config=RiskConfig(max_order_amount=Decimal("80000"), max_position_amount=Decimal("200000")),
            profile_label="보수형",
        )
        after = runtime.broker.snapshot()

        self.assertEqual(before.cash, after.cash)
        self.assertEqual(before.equity, after.equity)
        self.assertEqual(before.positions, after.positions)


class PaperTradingRuntimeCycleTest(unittest.TestCase):
    def test_start_and_pause_update_status_and_emit_system_events(self):
        runtime = make_runtime()

        start_event = runtime.start()
        pause_event = runtime.pause()

        self.assertFalse(runtime.status.running)
        self.assertEqual("일시정지", runtime.status.label)
        self.assertEqual("system", start_event.kind)
        self.assertIn("시작", start_event.message)
        self.assertEqual("system", pause_event.kind)
        self.assertIn("일시정지", pause_event.message)

    def test_runtime_can_emit_live_mode_events_when_configured_for_live_execution(self):
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )

        event = runtime.start()

        self.assertEqual("live", event.mode)
        self.assertEqual("live", runtime.events[-1].mode)

    def test_cycle_blocks_new_orders_when_live_pending_orders_remain_unresolved(self):
        broker = PendingSyncBroker(
            RuntimeSyncResult(remaining=(PendingMarker("005930"),), fills=())
        )
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual([], broker.place_order_calls)
        self.assertTrue(any("live_pending_orders_unresolved" in event.message for event in events))
        self.assertFalse(getattr(runtime, "_cycle_paused_for_live_pending_order"))
        self.assertTrue(getattr(runtime, "_cycle_new_entries_blocked_for_live_pending_order"))
        self.assertEqual(
            {
                "outcome": "new_entries_blocked",
                "remainingCount": 1,
                "entryBlockingCount": 1,
                "isolatedBuyCount": 0,
                "isolatedSellCount": 0,
                "reservedBuyNotional": Decimal("0"),
                "fillCount": 0,
                "storeUnavailable": False,
                "syncUnavailable": False,
            },
            runtime._last_pending_live_order_sync_summary,
        )

    def test_cycle_fails_closed_when_pending_sync_is_unavailable(self):
        broker = PendingSyncBroker(
            RuntimeSyncResult(
                remaining=(PendingMarker("005930", "SELL"),),
                sync_unavailable=True,
            )
        )
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual([], broker.place_order_calls)
        self.assertEqual("sync_unavailable", runtime._last_pending_live_order_sync_summary["outcome"])
        self.assertTrue(runtime._last_pending_live_order_sync_summary["syncUnavailable"])
        self.assertTrue(any("live_pending_order_sync_unavailable" in event.message for event in events))

    def test_pending_sync_failure_records_redacted_error_detail_for_diagnostics(self):
        class FailingPendingSyncBroker(PendingSyncBroker):
            def sync_pending_order_statuses(self):
                raise RuntimeError("KIS daily reconciliation failed account_no=12345678 continuation keys missing")

        broker = FailingPendingSyncBroker(RuntimeSyncResult())
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        summary = runtime._last_pending_live_order_sync_summary
        self.assertEqual("failed", summary["outcome"])
        self.assertEqual("RuntimeError", summary["errorType"])
        self.assertIn("continuation keys missing", summary["errorDetail"])
        self.assertNotIn("12345678", summary["errorDetail"])
        rendered_events = " ".join(event.message for event in events)
        self.assertIn("continuation keys missing", rendered_events)
        self.assertNotIn("12345678", rendered_events)

    def test_cycle_isolates_pending_sell_and_still_buys_unrelated_symbol(self):
        broker = PendingSellSyncBroker(
            RuntimeSyncResult(
                remaining=(
                    PendingMarker(
                        "OLD001",
                        side="SELL",
                        remaining_quantity=1,
                        estimated_price=Decimal("10000"),
                    ),
                ),
                fills=(),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=["NEW001"],
            broker=broker,
            strategy=FixedSignalStrategy({"NEW001": [Signal.buy("NEW001", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"NEW001": _bar(symbol="NEW001")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["NEW001"], runtime.bar_provider.requested_symbols)
        self.assertEqual(["BUY"], [order.side for order, _bar_for_order in broker.place_order_calls])
        self.assertTrue(any("live_pending_sell_isolated" in event.message for event in events))
        self.assertFalse(getattr(runtime, "_cycle_paused_for_live_pending_order"))
        self.assertEqual(
            {
                "outcome": "sell_isolated",
                "remainingCount": 1,
                "entryBlockingCount": 0,
                "isolatedBuyCount": 0,
                "isolatedSellCount": 1,
                "reservedBuyNotional": Decimal("0"),
                "fillCount": 0,
                "storeUnavailable": False,
                "syncUnavailable": False,
            },
            runtime._last_pending_live_order_sync_summary,
        )

    def test_uncertain_pending_sell_blocks_unrelated_entry(self):
        broker = PendingSellSyncBroker(
            RuntimeSyncResult(
                remaining=(
                    PendingMarker(
                        "OLD001",
                        side="SELL",
                        reason="submission_uncertain",
                    ),
                ),
                fills=(),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=["NEW001"],
            broker=broker,
            strategy=FixedSignalStrategy({"NEW001": [Signal.buy("NEW001", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"NEW001": _bar(symbol="NEW001")}),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual([], broker.place_order_calls)
        self.assertTrue(runtime._cycle_new_entries_blocked_for_live_pending_order)
        self.assertEqual("new_entries_blocked", runtime._last_pending_live_order_sync_summary["outcome"])

    def test_live_cycle_opens_and_closes_pending_order_batch(self):
        class BatchAwareBroker(PendingSellSyncBroker):
            def __init__(self, result):
                super().__init__(result)
                self.batch_events = []

            def begin_pending_order_batch(self):
                self.batch_events.append("begin")

            def end_pending_order_batch(self):
                self.batch_events.append("end")

        broker = BatchAwareBroker(RuntimeSyncResult())
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001")}),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["end", "begin", "end"], broker.batch_events)

    def test_reserved_pending_buy_isolated_and_allows_unrelated_entry_and_exit(self):
        opened_at = datetime(2026, 6, 11, 9, 0)
        exit_position = Position(
            symbol="EXIT01",
            quantity=2,
            avg_price=Decimal("10000"),
            last_price=Decimal("11000"),
            opened_at=opened_at,
            highest_price=Decimal("11000"),
            sellable_quantity=2,
            managed_quantity=2,
        )
        broker = ScopedPendingBroker(
            pending=PendingMarker(
                "PEND01",
                side="BUY",
                remaining_quantity=1,
                estimated_price=Decimal("10000"),
            ),
            positions={"EXIT01": exit_position},
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "BUY002": [Signal.buy("BUY002", "entry")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="9000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["EXIT01", "BUY002"], runtime.bar_provider.requested_symbols)
        self.assertEqual(
            [("EXIT01", "SELL"), ("BUY002", "BUY")],
            [(order.symbol, order.side) for order in broker.orders],
        )
        self.assertTrue(any(event.kind == "trade" and event.side == "SELL" for event in events))
        self.assertTrue(any(event.kind == "trade" and event.side == "BUY" for event in events))
        self.assertFalse(runtime._cycle_new_entries_blocked_for_live_pending_order)
        self.assertEqual("buy_isolated", runtime._last_pending_live_order_sync_summary["outcome"])
        self.assertEqual(Decimal("10000"), runtime._last_pending_live_order_sync_summary["reservedBuyNotional"])

    def test_uncertain_pending_buy_blocks_unrelated_entries_but_keeps_exit_management(self):
        exit_position = Position(
            symbol="EXIT01",
            quantity=2,
            avg_price=Decimal("10000"),
            last_price=Decimal("11000"),
            opened_at=datetime(2026, 6, 11, 9, 0),
            highest_price=Decimal("11000"),
            sellable_quantity=2,
            managed_quantity=2,
        )
        broker = ScopedPendingBroker(
            pending=PendingMarker(
                "PEND01",
                side="BUY",
                remaining_quantity=1,
                estimated_price=Decimal("10000"),
                reason="submission_uncertain",
            ),
            positions={"EXIT01": exit_position},
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "BUY002": [Signal.buy("BUY002", "entry")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="9000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["EXIT01"], runtime.bar_provider.requested_symbols)
        self.assertEqual([("EXIT01", "SELL")], [(order.symbol, order.side) for order in broker.orders])
        self.assertTrue(runtime._cycle_new_entries_blocked_for_live_pending_order)
        self.assertEqual("new_entries_blocked", runtime._last_pending_live_order_sync_summary["outcome"])

    def test_pending_sell_symbol_is_skipped_without_stopping_other_position_exit(self):
        opened_at = datetime(2026, 6, 11, 9, 0)
        positions = {
            symbol: Position(
                symbol=symbol,
                quantity=1,
                avg_price=Decimal("10000"),
                last_price=Decimal("9000"),
                opened_at=opened_at,
                highest_price=Decimal("10000"),
                sellable_quantity=1,
                managed_quantity=1,
            )
            for symbol in ("PEND01", "EXIT02")
        }
        broker = ScopedPendingBroker(
            pending=PendingMarker(
                "PEND01",
                side="SELL",
                remaining_quantity=1,
                estimated_price=Decimal("10000"),
            ),
            positions=positions,
        )
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "PEND01": [Signal.sell("PEND01", "stop_loss")],
                    "EXIT02": [Signal.sell("EXIT02", "stop_loss")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "PEND01": _bar(symbol="PEND01", close="9000"),
                    "EXIT02": _bar(symbol="EXIT02", close="9000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["PEND01", "EXIT02"], runtime.bar_provider.requested_symbols)
        self.assertEqual([("EXIT02", "SELL")], [(order.symbol, order.side) for order in broker.orders])
        self.assertTrue(any("live_pending_symbol_isolated" in event.message for event in events))
        self.assertFalse(runtime._cycle_paused_for_live_pending_order)

    def test_unknown_pending_side_pauses_the_cycle_fail_closed(self):
        broker = PendingSyncBroker(
            RuntimeSyncResult(remaining=(PendingMarker("PEND01", side="UNKNOWN"),), fills=())
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertTrue(runtime._cycle_paused_for_live_pending_order)
        self.assertEqual("unknown_side", runtime._last_pending_live_order_sync_summary["outcome"])
        self.assertEqual(1, runtime._last_pending_live_order_sync_summary["unknownSideCount"])
        self.assertTrue(any("live_pending_order_side_unknown" in event.message for event in events))

    def test_current_cycle_tracked_pending_sell_isolates_only_that_symbol(self):
        opened_at = datetime(2026, 6, 11, 9, 0)
        positions = {
            symbol: Position(
                symbol=symbol,
                quantity=1,
                avg_price=Decimal("10000"),
                last_price=Decimal("9000"),
                opened_at=opened_at,
                highest_price=Decimal("10000"),
                sellable_quantity=1,
                managed_quantity=1,
            )
            for symbol in ("EXIT01", "EXIT02")
        }

        class CurrentCyclePendingSellBroker:
            def __init__(self):
                self.orders = []

            def snapshot(self, *, timestamp=None):
                return AccountSnapshot(cash=Decimal("1000000"), positions=positions)

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.orders.append(order)
                if order.symbol == "EXIT01":
                    return Fill(
                        order=order,
                        accepted=False,
                        timestamp=bar.timestamp,
                        reject_reason="live_order_pending",
                        pending_order_tracked=True,
                    )
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        broker = CurrentCyclePendingSellBroker()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "EXIT02": [Signal.sell("EXIT02", "stop_loss")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="9000"),
                    "EXIT02": _bar(symbol="EXIT02", close="9000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(
            [("EXIT01", "SELL"), ("EXIT02", "SELL")],
            [(order.symbol, order.side) for order in broker.orders],
        )
        self.assertFalse(runtime._cycle_paused_for_live_pending_order)
        self.assertEqual({"EXIT01"}, runtime._cycle_pending_sell_symbols)
        self.assertTrue(any(event.symbol == "EXIT02" and event.result == "filled" for event in events))

    def test_current_cycle_tracked_pending_buy_reserves_cash_and_allows_later_entries(self):
        class CurrentCyclePendingBuyBroker:
            def __init__(self):
                self.orders = []

            def snapshot(self, *, timestamp=None):
                return AccountSnapshot(cash=Decimal("1000000"))

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.orders.append(order)
                return Fill(
                    order=order,
                    accepted=False,
                    timestamp=bar.timestamp,
                    reject_reason="live_order_pending",
                    pending_order_tracked=True,
                )

        broker = CurrentCyclePendingBuyBroker()
        runtime = PaperTradingRuntime(
            symbols=["BUY001", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "BUY001": [Signal.buy("BUY001", "entry")],
                    "BUY002": [Signal.buy("BUY002", "entry")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider(
                {
                    "BUY001": _bar(symbol="BUY001"),
                    "BUY002": _bar(symbol="BUY002"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(2, len(broker.orders))
        self.assertEqual(["BUY", "BUY"], [order.side for order in broker.orders])
        self.assertFalse(runtime._cycle_paused_for_live_pending_order)
        self.assertFalse(runtime._cycle_new_entries_blocked_for_live_pending_order)
        self.assertEqual({order.symbol for order in broker.orders}, runtime._cycle_pending_order_symbols)

    def test_live_pending_order_cycle_reuses_pre_sync_snapshot_for_metrics(self):
        broker = PendingRateLimitedLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100202"),
                equity_override=Decimal("100202"),
                buying_power_override=Decimal("100202"),
            ),
            max_snapshot_calls=1,
            result=RuntimeSyncResult(remaining=(PendingMarker("005930"),), fills=()),
        )
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()
        broker.snapshot_calls = 0

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertTrue(any("live_pending_orders_unresolved" in event.message for event in events))
        self.assertEqual(1, runtime.cycle_count)
        self.assertEqual(1, broker.snapshot_calls)

    def test_live_order_pending_pauses_cycle_without_tripping_failure_limit(self):
        broker = LivePendingAfterSubmissionBroker()
        runtime = PaperTradingRuntime(
            symbols=["BUY01"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY01": [Signal.buy("BUY01", "flow_breakout")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(max_order_amount=Decimal("100000"), max_positions=2, max_consecutive_order_failures=1)
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY01": _bar(symbol="BUY01", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual(["SELL"], [event.side for event in trades])
        self.assertEqual(["live_order_pending"], [event.reason for event in trades])
        self.assertEqual(["EXIT01"], [order.symbol for order in broker.orders])
        self.assertFalse(any(event.reason == "order_failure_limit_reached" for event in trades))

    def test_live_order_reconciliation_failure_pauses_cycle_without_tripping_failure_limit(self):
        broker = LivePendingAfterSubmissionBroker(
            reject_reason="live_order_reconciliation_failed: reconciliation timeout"
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY01"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY01": [Signal.buy("BUY01", "flow_breakout")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(max_order_amount=Decimal("100000"), max_positions=2, max_consecutive_order_failures=1)
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY01": _bar(symbol="BUY01", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual(["SELL"], [event.side for event in trades])
        self.assertEqual(["live_order_reconciliation_failed: reconciliation timeout"], [event.reason for event in trades])
        self.assertEqual(["EXIT01"], [order.symbol for order in broker.orders])
        self.assertFalse(any(event.reason == "order_failure_limit_reached" for event in trades))

    def test_live_order_hard_rejection_still_trips_failure_limit(self):
        broker = LivePendingAfterSubmissionBroker(reject_reason="live_order_rejected: insufficient balance")
        runtime = PaperTradingRuntime(
            symbols=["BUY01"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY01": [Signal.buy("BUY01", "flow_breakout")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(max_order_amount=Decimal("100000"), max_positions=2, max_consecutive_order_failures=1)
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY01": _bar(symbol="BUY01", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual(["SELL", "BUY"], [event.side for event in trades])
        self.assertEqual(
            ["live_order_rejected: insufficient balance", "order_failure_limit_reached"],
            [event.reason for event in trades],
        )
        self.assertEqual(["EXIT01"], [order.symbol for order in broker.orders])

    def test_live_transient_preflight_rejections_do_not_trip_failure_limit(self):
        transient_reasons = (
            'live_order_rejected: KIS HTTP 500: {"msg_cd":"EGW00215","msg1":"rate limit"}',
            "live_entry_count_unknown",
            "live_market_state_rejected: 매매거래정지",
        )

        for transient_reason in transient_reasons:
            with self.subTest(transient_reason=transient_reason):
                broker = LivePendingAfterSubmissionBroker(reject_reason=transient_reason)
                runtime = PaperTradingRuntime(
                    symbols=["BUY01"],
                    broker=broker,
                    strategy=FixedSignalStrategy(
                        {
                            "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                            "BUY01": [Signal.buy("BUY01", "flow_breakout")],
                        }
                    ),
                    risk_manager=RiskManager(
                        RiskConfig(
                            max_order_amount=Decimal("100000"),
                            max_positions=2,
                            max_consecutive_order_failures=1,
                        )
                    ),
                    bar_provider=DictBarProvider(
                        {
                            "EXIT01": _bar(symbol="EXIT01", close="11000"),
                            "BUY01": _bar(symbol="BUY01", close="10000"),
                        }
                    ),
                    execution_mode="live",
                )
                runtime.start()

                events = runtime.run_cycle()

                trades = [event for event in events if event.kind == "trade"]
                self.assertEqual(["SELL", "BUY"], [event.side for event in trades])
                self.assertEqual(["rejected", "filled"], [event.result for event in trades])
                self.assertEqual(["EXIT01", "BUY01"], [order.symbol for order in broker.orders])
                self.assertFalse(any(event.reason == "order_failure_limit_reached" for event in trades))

    def test_live_terminal_rejections_count_after_an_intervening_preflight_rejection(self):
        for terminal_reason in ("live_order_canceled", "live_order_expired"):
            with self.subTest(terminal_reason=terminal_reason):
                risk_manager = RiskManager(
                    RiskConfig(
                        max_order_amount=Decimal("100000"),
                        max_consecutive_order_failures=2,
                    )
                )
                runtime = PaperTradingRuntime(
                    symbols=["005930"],
                    broker=PendingSyncBroker(RuntimeSyncResult()),
                    strategy=FixedSignalStrategy({}),
                    risk_manager=risk_manager,
                    bar_provider=DictBarProvider({"005930": _bar()}),
                    execution_mode="live",
                )
                order = Order.sell("005930", 1, "exit")

                runtime._record_risk_order_result(
                    Fill(
                        order=order,
                        accepted=False,
                        timestamp=_bar().timestamp,
                        reject_reason="live_order_rejected: broker denial",
                    )
                )
                runtime._record_risk_order_result(
                    Fill(
                        order=order,
                        accepted=False,
                        timestamp=_bar().timestamp,
                        reject_reason="live_entry_count_unknown",
                    )
                )

                self.assertEqual(1, risk_manager._consecutive_order_failures)

                runtime._record_risk_order_result(
                    Fill(
                        order=order,
                        accepted=False,
                        timestamp=_bar().timestamp,
                        reject_reason=terminal_reason,
                    )
                )

                self.assertEqual(2, risk_manager._consecutive_order_failures)
                self.assertEqual("hard_rejection", runtime._last_order_failure_class)

    def test_cycle_emits_late_live_pending_fills_before_scanning(self):
        filled_at = datetime(2026, 6, 10, 9, 1)
        fill = Fill(
            order=Order.buy("005930", 2, "pending_order_sync:pending:filled"),
            accepted=True,
            timestamp=filled_at,
            price=Decimal("10000"),
            quantity=2,
        )
        risk_manager = ApprovingRiskManager()
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PendingSyncBroker(RuntimeSyncResult(fills=(fill,))),
            strategy=FixedSignalStrategy({}),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual(1, len(trades))
        self.assertEqual("filled", trades[0].result)
        self.assertEqual(2, trades[0].quantity)
        self.assertEqual([True], risk_manager.results)
        self.assertEqual([("005930", filled_at.date())], risk_manager.entries)

    def test_cycle_does_not_rebuy_symbol_exited_by_late_live_pending_fill(self):
        filled_at = datetime(2026, 6, 10, 9, 1)
        sell_fill = Fill(
            order=Order.sell("005930", 2, "pending_order_sync:pending:filled"),
            accepted=True,
            timestamp=filled_at,
            price=Decimal("10000"),
            quantity=2,
        )
        broker = PendingSyncBroker(RuntimeSyncResult(fills=(sell_fill,)))
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "same_cycle_reentry")]}),
            risk_manager=ApprovingRiskManager(),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930")}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual(["SELL"], [event.side for event in trades])
        self.assertEqual([], broker.place_order_calls)

    def test_pending_live_entry_fill_counts_against_same_cycle_entry_budget(self):
        filled_at = datetime(2026, 6, 10, 9, 1)
        pending_entry_fill = Fill(
            order=Order.buy("PEND01", 30, "pending_order_sync:pending:filled"),
            accepted=True,
            timestamp=filled_at,
            price=Decimal("10000"),
            quantity=30,
        )
        broker = PendingEntryBudgetBroker(pending_entry_fill)
        runtime = PaperTradingRuntime(
            symbols=["BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY002": [Signal.buy("BUY002", "replacement_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({"BUY002": _bar(symbol="BUY002", close="10000")}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, len(broker.place_order_calls))
        placed_order, _bar_for_order = broker.place_order_calls[0]
        self.assertEqual(70, placed_order.quantity)

    def test_live_entry_capacity_counts_same_cycle_fill_when_account_snapshot_is_stale(self):
        symbols = ["BUY001", "BUY002"]
        broker = StaleLiveSnapshotBroker(snapshot=AccountSnapshot(cash=Decimal("1000000")))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, len(broker.orders))

    def test_live_order_sizing_preserves_kis_equity_and_orderable_cash(self):
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1000000"),
                buying_power_override=Decimal("600000"),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "live_slot_budget")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("0.20"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, len(broker.orders))
        self.assertEqual(20, broker.orders[0].quantity)

    def test_live_cycle_refreshes_exact_planning_cash_once_before_sizing_entries(self):
        symbols = ["BUY001", "BUY002"]
        broker = PlanningCashLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1000000"),
            ),
            planning_cash=Decimal("100000"),
        )
        bars = {symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "flow_score_80")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(bars),
            scanner_provider=StaticScannerProvider(
                bars=bars,
                priorities={"BUY001": 100.0, "BUY002": 90.0},
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, len(broker.planning_calls))
        self.assertEqual(Decimal("100000"), sum(order.quantity * Decimal("10000") for order in broker.orders))
        self.assertEqual(Decimal("1000000"), runtime.performance_metrics.cash)
        self.assertEqual(Decimal("0"), runtime._latest_cycle_account_snapshot.buying_power)

    def test_live_planning_skips_expensive_first_candidate_and_buys_affordable_followup(self):
        symbols = ["EXPENSIVE", "AFFORDABLE"]
        broker = PlanningCashLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1000000"),
            ),
            planning_cash=Decimal("5000"),
        )
        bars = {
            "EXPENSIVE": _bar(symbol="EXPENSIVE", close="10000"),
            "AFFORDABLE": _bar(symbol="AFFORDABLE", close="1000"),
        }
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "flow_score_80")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(bars),
            scanner_provider=StaticScannerProvider(
                bars=bars,
                priorities={"EXPENSIVE": 100.0, "AFFORDABLE": 90.0},
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["AFFORDABLE"], [order.symbol for order in broker.orders])
        self.assertEqual(1, len(broker.planning_calls))

    def test_live_pending_buy_reservation_reduces_cycle_buying_power_snapshot(self):
        broker = PendingPlanningCashLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100000"),
                equity_override=Decimal("100000"),
            ),
            planning_cash=Decimal("100000"),
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_80")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertTrue(any(event.reason == "live_order_pending" for event in events if event.kind == "trade"))
        self.assertEqual(Decimal("0"), runtime._latest_cycle_account_snapshot.buying_power)

    def test_live_planning_cash_failure_blocks_buys_but_keeps_sell_management(self):
        held = Position(
            symbol="EXIT01",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 11, 9, 0),
            highest_price=Decimal("10000"),
            sellable_quantity=1,
            managed_quantity=1,
        )
        broker = PlanningCashLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                positions={"EXIT01": held},
                equity_override=Decimal("1010000"),
            ),
            planning_cash=Decimal("0"),
            blocker="live_buyable_inquiry_failed: unavailable",
        )
        bars = {
            "EXIT01": _bar(symbol="EXIT01", close="9000"),
            "BUY001": _bar(symbol="BUY001", close="10000"),
        }
        runtime = PaperTradingRuntime(
            symbols=list(bars),
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "lower_trend_boundary")],
                    "BUY001": [Signal.buy("BUY001", "flow_score_80")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(bars),
            scanner_provider=StaticScannerProvider(
                bars=bars,
                priorities={"EXIT01": 100.0, "BUY001": 90.0},
            ),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["SELL"], [order.side for order in broker.orders])
        self.assertEqual(1, len(broker.planning_calls))
        self.assertTrue(
            any(
                event.kind == "system" and "live_planning_buying_power_failed" in event.message
                for event in events
            )
        )

    def test_live_scale_in_spend_counts_against_same_cycle_budget_when_snapshot_is_stale(self):
        existing_position = Position(
            symbol="LIVE01",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 10, 9, 0),
            highest_price=Decimal("10000"),
            sellable_quantity=1,
            managed_quantity=1,
        )
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(cash=Decimal("1000000"), positions={"LIVE01": existing_position})
        )
        runtime = PaperTradingRuntime(
            symbols=["LIVE01", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "LIVE01": [Signal.buy("LIVE01", "scale_in")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "LIVE01": _bar(symbol="LIVE01", close="10000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["LIVE01"], [order.symbol for order in broker.orders])

    def test_live_exit_frees_slot_when_account_snapshot_still_contains_sold_position(self):
        exited_position = Position(
            symbol="EXIT01",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 10, 9, 0),
            highest_price=Decimal("10000"),
        )
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(cash=Decimal("1000000"), positions={"EXIT01": exited_position})
        )
        runtime = PaperTradingRuntime(
            symbols=["EXIT01", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["SELL", "BUY"], [order.side for order in broker.orders])

    def test_live_cycle_caches_account_with_entry_and_exit_overlays(self):
        exited_position = Position(
            symbol="EXIT01",
            quantity=1,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 10, 9, 0),
            highest_price=Decimal("10000"),
        )
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(cash=Decimal("1000000"), positions={"EXIT01": exited_position})
        )
        runtime = PaperTradingRuntime(
            symbols=["EXIT01", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        cached = runtime.latest_cycle_account_snapshot
        self.assertIsNone(runtime._cycle_account_snapshot)
        self.assertEqual({"BUY002"}, set(cached.positions))
        self.assertEqual(broker.orders[1].quantity, cached.positions["BUY002"].quantity)
        with self.assertRaises(AttributeError):
            runtime.latest_cycle_account_snapshot = AccountSnapshot(cash=Decimal("0"))

    def test_live_partial_exit_keeps_remaining_position_in_cycle_overlay(self):
        exited_position = Position(
            symbol="EXIT01",
            quantity=5,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 10, 9, 0),
            highest_price=Decimal("10000"),
            sellable_quantity=5,
            managed_quantity=5,
        )
        broker = PartialExitStaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(cash=Decimal("1000000"), positions={"EXIT01": exited_position}),
            exit_fill_quantity=2,
        )
        runtime = PaperTradingRuntime(
            symbols=["EXIT01", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="11000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["SELL"], [order.side for order in broker.orders])

    def test_filled_trade_event_uses_actual_fill_quantity_not_requested_order_quantity(self):
        broker = PartialFillBroker(filled_quantity=2)
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=broker,
            strategy=FixedSignalStrategy({"005930": [Signal.buy("005930", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"005930": _bar(symbol="005930", close="10000")}),
        )
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(1, len(trades))
        self.assertGreater(broker.orders[0].quantity, 2)
        self.assertEqual(2, trades[0].quantity)

    def test_start_immediately_reports_waiting_status_outside_regular_market_hours(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))
        runtime = make_runtime(market_hours=hours)

        event = runtime.start()

        self.assertTrue(runtime.status.running)
        self.assertEqual("장 대기", runtime.status.label)
        self.assertIn("정규장", event.message)

    def test_live_start_resets_planning_probe_state_outside_market_hours(self):
        runtime, _, _, _, _ = make_zero_balance_probe_runtime(planning_cash="0")
        runtime.market_hours = KoreanRegularMarketHours(
            clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST)
        )
        runtime._last_live_planning_buying_power = Decimal("0")
        runtime._last_live_planning_buying_power_at = datetime.now().astimezone()
        runtime._next_live_planner_phase = "monitoring"
        runtime._live_start_positions_adopted = True

        runtime.start()

        self.assertIsNone(runtime._last_live_planning_buying_power)
        self.assertIsNone(runtime._last_live_planning_buying_power_at)
        self.assertEqual("entry_reserved", runtime._next_live_planner_phase)
        self.assertFalse(runtime._live_start_positions_adopted)

    def test_cycle_evaluates_every_symbol_once_and_logs_hold_when_no_signal(self):
        runtime = make_runtime(symbols=["005930", "000660"])
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["005930", "000660"], runtime.bar_provider.requested_symbols)
        hold_events = [event for event in events if event.kind == "system" and "관망" in event.message]
        self.assertEqual(2, len(hold_events))
        self.assertEqual(1, runtime.cycle_count)
        self.assertIsNotNone(runtime.last_update)

    def test_cycle_tops_up_underfilled_large_universe_but_always_checks_open_positions(self):
        symbols = ["005930", "000660", "035420", "005380"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("005380", 1, "seed"), _bar(symbol="005380", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=4)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=4),
            scan_limit_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()
        first_cycle = list(runtime.bar_provider.requested_symbols)
        runtime.run_cycle()
        second_cycle = runtime.bar_provider.requested_symbols[len(first_cycle) :]

        self.assertEqual(["005380", "005930", "000660", "035420"], first_cycle)
        self.assertEqual(["005380", "035420", "005930", "000660"], second_cycle)

    def test_cycle_fills_highest_scored_entry_candidates_before_lower_scores(self):
        symbols = ["LOW001", "HIGH01"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=RankedSignalStrategy({"LOW001": 0.30, "HIGH01": 0.95}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["HIGH01"], [event.symbol for event in filled])
        self.assertIn("HIGH01", runtime.broker.snapshot().positions)

    def test_external_scanner_order_controls_entry_execution_when_slots_are_limited(self):
        symbols = ["SCANHI", "SCOREH"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=RankedSignalStrategy({"SCANHI": 0.10, "SCOREH": 0.95}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            scanner_provider=StaticScannerProvider(
                bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
                priorities={"SCANHI": 100.0, "SCOREH": 1.0},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["SCANHI"], [event.symbol for event in filled])
        self.assertIn("SCANHI", runtime.broker.snapshot().positions)

    def test_cycle_calls_final_quote_provider_only_for_entry_candidates(self):
        symbols = ["AAA001", "BBB001", "CCC001"]
        scanner = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        final_quotes = DictBarProvider({"BBB001": _bar(symbol="BBB001", close="12000")})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BBB001": [Signal.buy("BBB001", "flow_score_100")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(symbols, scanner.requested_symbols)
        self.assertEqual(["BBB001"], final_quotes.requested_symbols)

    def test_final_quote_limit_does_not_limit_external_scanner_evaluation(self):
        symbols = ["BUY001", "BUY002", "BUY003", "BUY004"]
        scanner_bars = {}
        for symbol in symbols:
            bar = _bar(symbol=symbol, close="10000")
            scanner_bars[symbol] = MarketBar(
                symbol=symbol,
                timestamp=bar.timestamp,
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9900"),
                close=bar.close,
                volume=bar.volume,
                vwap=bar.vwap,
                bid=bar.bid,
                ask=bar.ask,
            )
        scanner_provider = StaticScannerProvider(
            bars=scanner_bars,
            priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})

        class BuyUnheldStrategy:
            def __init__(self):
                self.seen_symbols = []

            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol in account.positions:
                    return []
                return [Signal.buy(bar.symbol, "flow_score_100")]

        strategy = BuyUnheldStrategy()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("10000"), max_positions=10)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(cash_allocation_pct=Decimal("0.10"), max_positions=10),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=10,
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(symbols, strategy.seen_symbols)
        self.assertEqual(2, len(final_quotes.requested_symbols))
        self.assertTrue(set(final_quotes.requested_symbols).issubset(set(symbols)))
        snapshot = runtime.broker.snapshot()
        self.assertEqual(set(final_quotes.requested_symbols), set(snapshot.positions))
        self.assertEqual(Decimal("400000"), snapshot.cash)
        self.assertEqual(2, len([event for event in events if event.kind == "trade" and event.result == "filled"]))
        self.assertEqual(
            [],
            [event.reason for event in events if event.kind == "trade" and event.result == "rejected"],
        )
        self.assertTrue(
            any(
                "scanner_diagnostic - external_scan_cycle" in event.message
                and "final_quotes=2/2" in event.message
                for event in events
                if event.kind == "system"
            )
        )
        self.assertEqual(0, runtime.performance_metrics.rejected_trades)

        strategy.seen_symbols.clear()
        runtime.run_cycle()

        self.assertCountEqual(symbols, strategy.seen_symbols)
        self.assertEqual(6, len(final_quotes.requested_symbols))
        self.assertEqual(2, final_quotes.requested_symbols.count("BUY001"))
        self.assertEqual(2, final_quotes.requested_symbols.count("BUY002"))
        self.assertEqual(1, final_quotes.requested_symbols.count("BUY003"))
        self.assertEqual(1, final_quotes.requested_symbols.count("BUY004"))
        self.assertEqual(set(symbols), set(runtime.broker.snapshot().positions))

    def test_authoritative_scanner_rotates_after_last_final_quote_attempt(self):
        symbols = ["BUY001", "BUY002", "BUY003", "BUY004"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000"),
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9900"),
            )
            for symbol in symbols
        }
        requested_symbols = []

        def unavailable_final_quote(symbol):
            requested_symbols.append(symbol)
            return None

        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "flow_score_80")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=unavailable_final_quote,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={symbol: 100.0 - index for index, symbol in enumerate(symbols)},
            ),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(["BUY001", "BUY002", "BUY003", "BUY004"], requested_symbols)

    def test_authoritative_scanner_rotates_in_affordability_adjusted_order(self):
        symbols = ["EXPENSIVE", "BUY001", "BUY002", "BUY003"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="2000000" if symbol == "EXPENSIVE" else "10000"),
                open=Decimal("1990000" if symbol == "EXPENSIVE" else "9900"),
                high=Decimal("2010000" if symbol == "EXPENSIVE" else "10100"),
                low=Decimal("1990000" if symbol == "EXPENSIVE" else "9900"),
            )
            for symbol in symbols
        }
        requested_symbols = []

        def unavailable_final_quote(symbol):
            requested_symbols.append(symbol)
            return None

        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "flow_score_80")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=unavailable_final_quote,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={symbol: 100.0 - index for index, symbol in enumerate(symbols)},
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(["BUY001", "BUY002"], requested_symbols)

    def test_top_up_scan_limit_does_not_use_final_quote_cap_as_scanner_width(self):
        symbols = ["NO0001", "NO0002", "NO0003", "BUY004"]

        def dense_bar(symbol):
            bar = _bar(symbol=symbol, close="10000")
            return MarketBar(
                symbol=symbol,
                timestamp=bar.timestamp,
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9900"),
                close=bar.close,
                volume=bar.volume,
                vwap=bar.vwap,
                bid=bar.bid,
                ask=bar.ask,
            )

        scanner_provider = StaticScannerProvider(
            bars={symbol: dense_bar(symbol) for symbol in symbols},
            priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
        )
        final_quotes = DictBarProvider({"BUY004": _bar(symbol="BUY004", close="10000")})
        strategy = FixedSignalStrategy({"BUY004": [Signal.buy("BUY004", "late_external_candidate")]})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("10000"), max_positions=10)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=10),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=2,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(symbols, strategy.seen_symbols)
        self.assertEqual(["BUY004"], final_quotes.requested_symbols)
        self.assertEqual({"BUY004"}, set(runtime.broker.snapshot().positions))
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY004"], [event.symbol for event in filled])

    def test_external_scanner_top_up_does_not_flood_final_quote_budget(self):
        symbols = [f"BUY{index:03d}" for index in range(1, 8)]
        scanner_provider = StaticScannerProvider(
            bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
            priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        strategy = FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("10000"), max_positions=7)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=7),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertLessEqual(len(final_quotes.requested_symbols), 2)
        self.assertLessEqual(len(strategy.seen_symbols), 2)
        self.assertEqual(2, len(runtime.broker.snapshot().positions))
        self.assertFalse(
            any(
                event.reason == "final_quote_limit_reached"
                for event in events
                if event.kind == "trade" and event.result == "rejected"
            )
        )

    def test_external_scanner_rotates_sparse_quote_candidates_after_final_quote_cap(self):
        symbols = ["NO0001", "NO0002", "BUY003", "BUY004"]
        scanner_provider = StaticScannerProvider(
            bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
            priorities={
                "NO0001": 100.0,
                "NO0002": 90.0,
                "BUY003": 80.0,
                "BUY004": 70.0,
            },
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        strategy = FixedSignalStrategy(
            {
                "BUY003": [Signal.buy("BUY003", "replacement_entry")],
                "BUY004": [Signal.buy("BUY004", "replacement_entry")],
            }
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        first_events = runtime.run_cycle()
        second_events = runtime.run_cycle()

        self.assertEqual(["NO0001", "NO0002", "BUY003", "BUY004"], final_quotes.requested_symbols)
        self.assertEqual(["NO0001", "NO0002", "BUY003", "BUY004"], strategy.seen_symbols)
        self.assertFalse([event for event in first_events if event.kind == "trade"])
        filled = [event for event in second_events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY003", "BUY004"], [event.symbol for event in filled])
        self.assertEqual({"BUY003", "BUY004"}, set(runtime.broker.snapshot().positions))

    def test_external_scanner_rotates_mixed_sparse_quote_candidates_after_final_quote_cap(self):
        symbols = ["DENSE1", "NO0002", "BUY003"]
        dense_bar = _bar(symbol="DENSE1", close="10000")
        scanner_provider = StaticScannerProvider(
            bars={
                "DENSE1": MarketBar(
                    symbol="DENSE1",
                    timestamp=dense_bar.timestamp,
                    open=Decimal("9900"),
                    high=Decimal("10100"),
                    low=Decimal("9900"),
                    close=dense_bar.close,
                    volume=dense_bar.volume,
                    vwap=dense_bar.vwap,
                    bid=dense_bar.bid,
                    ask=dense_bar.ask,
                ),
                "NO0002": _bar(symbol="NO0002", close="10000"),
                "BUY003": _bar(symbol="BUY003", close="10000"),
            },
            priorities={
                "DENSE1": 100.0,
                "NO0002": 90.0,
                "BUY003": 80.0,
            },
        )
        final_quotes = DictBarProvider(
            {
                "NO0002": _bar(symbol="NO0002", close="10000"),
                "BUY003": _bar(symbol="BUY003", close="10000"),
            }
        )
        strategy = FixedSignalStrategy({"BUY003": [Signal.buy("BUY003", "replacement_entry")]})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        first_events = runtime.run_cycle()
        second_events = runtime.run_cycle()

        self.assertEqual(["NO0002", "BUY003"], final_quotes.requested_symbols)
        self.assertNotIn("BUY003", strategy.seen_symbols[:2])
        filled = [event for event in second_events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY003"], [event.symbol for event in filled])
        self.assertFalse([event for event in first_events if event.kind == "trade"])
        self.assertEqual({"BUY003"}, set(runtime.broker.snapshot().positions))

    def test_external_scanner_confirms_dense_entry_before_later_sparse_quote_candidate(self):
        symbols = ["BUY001", "NO0002"]
        dense_bar = _bar(symbol="BUY001", close="10000")
        scanner_provider = StaticScannerProvider(
            bars={
                "BUY001": MarketBar(
                    symbol="BUY001",
                    timestamp=dense_bar.timestamp,
                    open=Decimal("9900"),
                    high=Decimal("10100"),
                    low=Decimal("9900"),
                    close=dense_bar.close,
                    volume=dense_bar.volume,
                    vwap=dense_bar.vwap,
                    bid=dense_bar.bid,
                    ask=dense_bar.ask,
                ),
                "NO0002": _bar(symbol="NO0002", close="10000"),
            },
            priorities={"BUY001": 100.0, "NO0002": 90.0},
        )
        final_quotes = DictBarProvider(
            {
                "BUY001": _bar(symbol="BUY001", close="10000"),
                "NO0002": _bar(symbol="NO0002", close="10000"),
            }
        )
        strategy = FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY001"], [event.symbol for event in filled])
        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))

    def test_external_scanner_fills_target_slots_from_affordable_ranked_candidates(self):
        symbols = ["HIGH01", "BUY001", "BUY002", "BUY003", "LOW004"]
        bars = {
            "HIGH01": _bar(symbol="HIGH01", close="1100000"),
            "BUY001": _bar(symbol="BUY001", close="120000"),
            "BUY002": _bar(symbol="BUY002", close="100000"),
            "BUY003": _bar(symbol="BUY003", close="80000"),
            "LOW004": _bar(symbol="LOW004", close="20000"),
        }
        scanner_provider = StaticScannerProvider(
            bars=bars,
            priorities={
                "HIGH01": 100.0,
                "BUY001": 90.0,
                "BUY002": 80.0,
                "BUY003": 70.0,
                "LOW004": 10.0,
            },
        )
        strategy = FixedSignalStrategy(
            {
                "HIGH01": [Signal.buy("HIGH01", "should_not_reach_strategy")],
                "BUY001": [Signal.buy("BUY001", "flow_score_90")],
                "BUY002": [Signal.buy("BUY002", "flow_score_80")],
                "BUY003": [Signal.buy("BUY003", "flow_score_70")],
                "LOW004": [Signal.buy("LOW004", "flow_score_10")],
            }
        )
        final_quotes = DictBarProvider(bars)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("1000000"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=3,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=3,
                order_cash_amount=Decimal("10000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=3,
            max_final_quote_requests_per_cycle=10,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY001", "BUY002", "BUY003"], [event.symbol for event in filled])
        self.assertEqual([6, 2, 1], [event.quantity for event in filled])
        self.assertEqual({"BUY001", "BUY002", "BUY003"}, set(runtime.broker.snapshot().positions))
        self.assertNotIn("HIGH01", strategy.seen_symbols)
        self.assertNotIn("LOW004", runtime.broker.snapshot().positions)
        self.assertEqual(["BUY001", "BUY002", "BUY003"], final_quotes.requested_symbols)
        self.assertFalse(any(event.kind == "trade" and event.result == "rejected" for event in events))
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertTrue(diagnostics)
        self.assertIn("entry_candidates=3", diagnostics[-1])
        self.assertIn("entry_fills=3", diagnostics[-1])
        self.assertIn("open_target_slots=0", diagnostics[-1])

    def test_external_scan_kis_refills_after_sell_and_reports_prescan_rejections(self):
        symbols = ["EXIT01", "HIGH01", "BUY002"]
        class RecordingPaperBroker(PaperBroker):
            def __init__(self):
                super().__init__(initial_cash=Decimal("1000000"))
                self.updated_bars = []

            def update_market(self, bar):
                self.updated_bars.append(bar)
                return super().update_market(bar)

        broker = RecordingPaperBroker()
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner_bars = {
            "EXIT01": _bar(symbol="EXIT01", close="11000", offset=1),
            "HIGH01": _bar(symbol="HIGH01", close="1100000", offset=1),
            "BUY002": _bar(symbol="BUY002", close="10000", offset=1),
        }
        strategy = FixedSignalStrategy(
            {
                "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                "HIGH01": [Signal.buy("HIGH01", "should_not_reach_strategy")],
                "BUY002": [Signal.buy("BUY002", "replacement_entry")],
            }
        )
        final_quotes = DictBarProvider(
            {
                "EXIT01": _bar(symbol="EXIT01", close="11000", offset=2),
                "BUY002": _bar(symbol="BUY002", close="10000", offset=2),
            }
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("1000000"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"HIGH01": 100.0, "BUY002": 90.0, "EXIT01": 0.0},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        rendered_system_log = "\n".join(event.message for event in events if event.kind == "system")
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertEqual([("EXIT01", "SELL"), ("BUY002", "BUY")], [(event.symbol, event.side) for event in filled])
        self.assertEqual({"BUY002"}, set(runtime.broker.snapshot().positions))
        self.assertEqual(["EXIT01", "BUY002"], final_quotes.requested_symbols)
        self.assertNotIn("HIGH01", strategy.seen_symbols)
        self.assertNotIn("HIGH01", rendered_system_log)
        self.assertTrue(diagnostics)
        self.assertIn("prescan_rejections=", diagnostics[-1])
        self.assertIn("entry_unaffordable:1", diagnostics[-1])
        self.assertIn("entry_fills=1", diagnostics[-1])

    def test_cycle_uses_scanner_snapshot_and_skips_unaffordable_before_strategy_or_final_quote(self):
        symbols = ["EXPENS", "BUY001"]
        fallback_provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        scanner_provider = StaticScannerProvider(
            bars={
                "EXPENS": _bar(symbol="EXPENS", close="500000"),
                "BUY001": _bar(symbol="BUY001", close="10000"),
            },
            priorities={"EXPENS": 999.0, "BUY001": 100.0},
        )
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        strategy = FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("50000"),
                    max_position_amount=Decimal("50000"),
                    max_positions=1,
                )
            ),
            bar_provider=fallback_provider,
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            scan_limit_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertNotIn("EXPENS", strategy.seen_symbols)
        self.assertEqual(["BUY001"], strategy.seen_symbols)
        self.assertEqual([], fallback_provider.requested_symbols)
        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))

    def test_external_scan_pre_filters_unaffordable_snapshot_candidate_before_hold_logging(self):
        symbols = ["EXPENS", "BUY001"]
        fallback_provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        scanner_provider = StaticScannerProvider(
            bars={
                "EXPENS": _bar(symbol="EXPENS", close="500000"),
                "BUY001": _bar(symbol="BUY001", close="10000"),
            },
            priorities={"EXPENS": 999.0, "BUY001": 100.0},
        )
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        strategy = FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("50000"),
                    max_position_amount=Decimal("50000"),
                    max_positions=1,
                )
            ),
            bar_provider=fallback_provider,
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        rendered_system_log = "\n".join(event.message for event in events if event.kind == "system")
        self.assertNotIn("EXPENS", rendered_system_log)
        self.assertEqual(["BUY001"], strategy.seen_symbols)
        self.assertEqual([], fallback_provider.requested_symbols)
        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))

    def test_external_scan_primes_only_needed_snapshot_symbols_and_reuses_them(self):
        class CountingScannerProvider:
            label = "external scanner"
            kind = "kiwoom"

            def __init__(self):
                self.snapshot_requests: list[list[str]] = []
                self.bars = {
                    "EXPENS": _bar(symbol="EXPENS", close="500000"),
                    "BUY001": _bar(symbol="BUY001", close="10000"),
                }

            def rank_symbols(self, symbols):
                return list(symbols) or list(self.bars)

            def snapshot(self, symbols):
                requested = list(symbols)
                self.snapshot_requests.append(requested)
                bars = {
                    symbol: self.bars[symbol]
                    for symbol in requested
                    if symbol in self.bars
                }
                return ScannerSnapshot(
                    bars=bars,
                    candidates=tuple(ScannerCandidate(symbol=symbol, priority=100.0) for symbol in bars),
                )

        symbols = ["EXPENS", "BUY001"]
        scanner_provider = CountingScannerProvider()
        strategy = FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("50000"),
                    max_position_amount=Decimal("50000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")}),
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual([["EXPENS"], ["BUY001"]], scanner_provider.snapshot_requests)
        self.assertEqual(["BUY001"], strategy.seen_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))

    def test_external_scan_kis_does_not_fallback_when_authoritative_scanner_fails(self):
        class FailingScannerProvider:
            label = "external scanner"
            kind = "kiwoom"

            def rank_symbols(self, symbols):
                raise RuntimeError("scanner unavailable")

            def snapshot(self, symbols):
                raise RuntimeError("scanner unavailable")

        symbols = ["BUY001"]
        fallback_provider = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        strategy = FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("50000"))),
            bar_provider=fallback_provider,
            final_quote_provider=final_quotes,
            scanner_provider=FailingScannerProvider(),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], fallback_provider.requested_symbols)
        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual([], strategy.seen_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertTrue(any("scanner_diagnostic" in event.message for event in events if event.kind == "system"))

    def test_external_scanner_rank_order_is_cached_within_cycle(self):
        class CountingScannerProvider:
            label = "external scanner"
            kind = "kiwoom"

            def __init__(self):
                self.rank_calls = 0
                self.symbols = ["BUY001", "BUY002"]

            def rank_symbols(self, symbols):
                self.rank_calls += 1
                return list(symbols) or list(self.symbols)

            def snapshot(self, symbols):
                return ScannerSnapshot(
                    bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
                    candidates=tuple(ScannerCandidate(symbol=symbol, priority=100.0) for symbol in symbols),
                )

        scanner_provider = CountingScannerProvider()
        runtime = PaperTradingRuntime(
            symbols=["BUY001", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "BUY001": [Signal.buy("BUY001", "flow_score_100")],
                    "BUY002": [Signal.buy("BUY002", "flow_score_90")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("50000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, scanner_provider.rank_calls)
        self.assertEqual({"BUY001", "BUY002"}, set(runtime.broker.snapshot().positions))

    def test_cycle_preserves_scanner_candidate_order_when_priorities_are_equal(self):
        symbols = ["AAA001", "BBB001"]
        strategy = FixedSignalStrategy({})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            scanner_provider=StaticScannerProvider(
                bars={
                    "BBB001": _bar(symbol="BBB001", close="10000"),
                    "AAA001": _bar(symbol="AAA001", close="10000"),
                },
                priorities={"AAA001": 0.0, "BBB001": 0.0},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BBB001", "AAA001"], strategy.seen_symbols)

    def test_cycle_preserves_scanner_order_over_fallback_priority_provider(self):
        symbols = ["AAA001", "BBB001"]
        strategy = FixedSignalStrategy({})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            scanner_provider=StaticScannerProvider(
                bars={
                    "BBB001": _bar(symbol="BBB001", close="10000"),
                    "AAA001": _bar(symbol="AAA001", close="10000"),
                },
                priorities={"AAA001": 0.0, "BBB001": 0.0},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default(),
            symbol_priority_provider=lambda symbol: {"AAA001": 1000.0, "BBB001": 0.0}[symbol],
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BBB001", "AAA001"], strategy.seen_symbols)

    def test_scanner_fetches_only_selected_symbols_after_scan_limit(self):
        symbols = ["BUY001", "SKIP02"]
        source_provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        fallback_provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=fallback_provider,
            scanner_provider=BarProviderScanner(
                source_provider,
                priority_provider=lambda symbol: {"BUY001": 100.0, "SKIP02": 1.0}[symbol],
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default(),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BUY001"], source_provider.requested_symbols)
        self.assertEqual([], fallback_provider.requested_symbols)

    def test_cycle_logs_sanitized_scanner_diagnostics(self):
        symbols = ["BUY001"]
        source_provider = DiagnosticBarProvider("Authorization: Bearer secret-token-123")
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")}),
            scanner_provider=BarProviderScanner(source_provider),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        rendered = "\n".join(event.message for event in events if event.kind == "system")
        self.assertIn("scanner_diagnostic", rendered)
        self.assertNotIn("secret-token-123", rendered)
        self.assertNotIn("Bearer", rendered)

    def test_external_scan_cycle_logs_rank_failure_when_snapshot_is_stale(self):
        class StaleScannerProvider:
            label = "stale scanner"
            kind = "json"

            def rank_symbols(self, symbols):
                raise RuntimeError("json: stale scanner snapshot age_seconds=121")

            def snapshot(self, symbols):
                return ScannerSnapshot()

        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaleScannerProvider(),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
            data_source_kind="external-scan-kis",
        )
        runtime.start()

        events = runtime.run_cycle()

        rendered = "\n".join(event.message for event in events if event.kind == "system")
        self.assertIn("scanner_diagnostic", rendered)
        self.assertIn("stale scanner snapshot", rendered)

    def test_external_scan_cycle_refreshes_stale_json_snapshot_before_selecting_candidates(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        import json

        with TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "scanner.json"
            snapshot_path.write_text(
                json.dumps(
                    {
                        "provider": "stale-file",
                        "generated_at": "2026-06-19T09:00:00+09:00",
                        "candidates": [
                            {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            refresh_calls = []

            def refresh_snapshot():
                refresh_calls.append("refresh")
                snapshot_path.write_text(
                    json.dumps(
                        {
                            "provider": "fresh-file",
                            "generated_at": "2026-06-19T09:02:01+09:00",
                            "candidates": [
                                {"symbol": "BUY001", "price": "10000", "volume": 1000, "priority": 100},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            scanner = JsonScannerProvider(
                snapshot_path,
                max_snapshot_age_seconds=60,
                now_provider=lambda: datetime(2026, 6, 19, 9, 2, 1, tzinfo=KST),
                refresh_callback=refresh_snapshot,
            )
            runtime = PaperTradingRuntime(
                symbols=["BUY001"],
                broker=PaperBroker(initial_cash=Decimal("1000000")),
                strategy=FixedSignalStrategy({}),
                risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
                bar_provider=DictBarProvider({}),
                scanner_provider=scanner,
                symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
                settings=CustomStrategySettings.default(),
                data_source_kind="external-scan-kis",
            )
            runtime.start()

            events = runtime.run_cycle()

        rendered = "\n".join(event.message for event in events if event.kind == "system")
        self.assertEqual(["refresh"], refresh_calls)
        self.assertIn("external_scan_cycle: candidates=1, selected=1, processed=1", rendered)
        self.assertNotIn("external_scan_cycle: candidates=0", rendered)

    def test_scanner_snapshot_merge_removes_stale_bars_for_requested_symbols(self):
        runtime = make_runtime(
            symbols=["OLD001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            bars={"OLD001": _bar(symbol="OLD001", close="12000")},
        )
        base = StaticScannerProvider(
            bars={"OLD001": _bar(symbol="OLD001", close="10000")},
        ).snapshot(["OLD001"])
        update = StaticScannerProvider(bars={}).snapshot(["OLD001"])

        merged = runtime._merge_scanner_snapshots(base, update, ["OLD001"])

        self.assertNotIn("OLD001", merged.bars)
        self.assertEqual([], list(merged.candidates))

    def test_cycle_uses_final_quote_price_for_paper_fill(self):
        scanner = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="12000")})
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(1, len(filled))
        self.assertEqual(Decimal("12000"), filled[0].price)
        position = runtime.broker.snapshot().positions["BUY001"]
        self.assertEqual(25, position.quantity)
        self.assertEqual(Decimal("12000"), position.avg_price)

    def test_cycle_defers_entry_when_final_quote_exceeds_remaining_slot_budget(self):
        scanner = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="400000")})
        risk_manager = ApprovingRiskManager()
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]}),
            risk_manager=risk_manager,
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual([], risk_manager.checked_symbols)
        self.assertFalse(runtime.broker.snapshot().positions)
        rejected = [event for event in events if event.kind == "trade" and event.result == "rejected"]
        self.assertEqual([], rejected)
        self.assertTrue(any("entry_unaffordable" in event.message for event in events if event.kind == "system"))

    def test_cycle_defers_entry_when_final_quote_is_unavailable(self):
        scanner = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})

        def missing_quote(_symbol):
            return None

        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]}),
            risk_manager=ApprovingRiskManager(),
            bar_provider=scanner,
            final_quote_provider=missing_quote,
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertFalse(runtime.broker.snapshot().positions)
        self.assertFalse(any(event.kind == "trade" and event.result == "rejected" for event in events))
        self.assertTrue(any("final_quote_unavailable" in event.message for event in events if event.kind == "system"))

    def test_cycle_defers_entry_when_final_quote_errors(self):
        scanner = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="10000")})

        def broken_quote(_symbol):
            raise RuntimeError("quote provider down")

        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "flow_score_100")]}),
            risk_manager=ApprovingRiskManager(),
            bar_provider=scanner,
            final_quote_provider=broken_quote,
            symbol_directory=SymbolDirectory({"BUY001": "Buy One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertFalse(runtime.broker.snapshot().positions)
        self.assertFalse(any(event.kind == "trade" and event.result == "rejected" for event in events))
        self.assertTrue(any("final_quote_error" in event.message for event in events if event.kind == "system"))

    def test_cycle_uses_final_quote_price_for_exit_fill(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="11000")})
        final_quotes = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="12000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.sell("EXIT01", "take_profit")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual(1, len(filled))
        self.assertEqual("SELL", filled[0].side)
        self.assertEqual(Decimal("12000"), filled[0].price)
        self.assertEqual(Decimal("10000"), filled[0].realized_pnl)
        self.assertFalse(runtime.broker.snapshot().positions)

    def test_cycle_defers_boundary_exit_when_final_quote_would_close_long_flat(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="9800")})
        final_quotes = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.sell("EXIT01", "lower_trend_boundary")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual([], filled)
        self.assertIn("EXIT01", runtime.broker.snapshot().positions)
        self.assertTrue(any("flat_final_quote_exit" in event.message for event in events if event.kind == "system"))

    def test_cycle_defers_boundary_exit_when_final_quote_would_close_short_flat(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(Order.short("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10200")})
        final_quotes = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.cover("EXIT01", "upper_trend_boundary")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit One"}),
            settings=CustomStrategySettings.default().with_updates(allow_paper_short=True),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual([], filled)
        self.assertIn("EXIT01", runtime.broker.snapshot().positions)
        self.assertTrue(any("flat_final_quote_exit" in event.message for event in events if event.kind == "system"))

    def test_cycle_defers_stop_loss_when_final_quote_would_close_long_flat(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="9400")})
        final_quotes = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.sell("EXIT01", "stop_loss")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit One"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()
        account = runtime.broker.snapshot()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual([], filled)
        self.assertIn("EXIT01", account.positions)
        self.assertEqual(Decimal("950000"), account.cash)
        self.assertEqual(Decimal("0"), account.realized_pnl_today)
        self.assertTrue(any("flat_final_quote_exit" in event.message for event in events if event.kind == "system"))

    def test_cycle_defers_stop_loss_when_final_quote_would_close_short_flat(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"), allow_short=True)
        broker.place_order(Order.short("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        scanner = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10600")})
        final_quotes = DictBarProvider({"EXIT01": _bar(symbol="EXIT01", close="10000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.cover("EXIT01", "stop_loss")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit One"}),
            settings=CustomStrategySettings.default().with_updates(allow_paper_short=True),
        )
        runtime.start()

        events = runtime.run_cycle()
        account = runtime.broker.snapshot()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual([], filled)
        self.assertIn("EXIT01", account.positions)
        self.assertEqual(Decimal("1050000"), account.cash)
        self.assertEqual(Decimal("0"), account.realized_pnl_today)
        self.assertTrue(any("flat_final_quote_exit" in event.message for event in events if event.kind == "system"))

    def test_cycle_scans_high_volume_priority_symbols_before_low_priority_symbols(self):
        symbols = ["LOW001", "HIGH01"]
        runtime = make_runtime(
            symbols=symbols,
            symbol_priority_provider=lambda symbol: {"LOW001": 1.0, "HIGH01": 10.0}[symbol],
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["HIGH01", "LOW001"], runtime.bar_provider.requested_symbols)

    def test_cycle_priority_keeps_rotating_through_broad_universe(self):
        symbols = ["LOW001", "HIGH01", "MID002", "LOW003"]
        runtime = make_runtime(
            symbols=symbols,
            symbol_priority_provider=lambda symbol: {
                "LOW001": 1.0,
                "HIGH01": 10.0,
                "MID002": 5.0,
                "LOW003": 0.5,
            }[symbol],
        )
        runtime.scan_limit_per_cycle = 2
        runtime.start()

        runtime.run_cycle()
        first_cycle = list(runtime.bar_provider.requested_symbols)
        runtime.run_cycle()
        second_cycle = runtime.bar_provider.requested_symbols[len(first_cycle) :]

        self.assertEqual(["HIGH01", "MID002"], first_cycle)
        self.assertEqual(["LOW001", "LOW003"], second_cycle)

    def test_kis_cycle_keeps_scan_batch_until_strategy_warmup_can_evaluate(self):
        symbols = [f"{index:06d}" for index in range(20)]
        bars_by_symbol = {
            symbol: [_bar(symbol=symbol, close=str(10000 + offset), offset=offset) for offset in range(8)]
            for symbol in symbols
        }
        provider = SequenceBarProvider(bars_by_symbol)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=WarmupOnlyStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        cycles = []
        for _ in range(5):
            start = len(provider.requested_symbols)
            runtime.run_cycle()
            cycles.append(provider.requested_symbols[start:])

        self.assertEqual(cycles[0], cycles[1])
        self.assertEqual(cycles[0], cycles[2])
        self.assertEqual(cycles[0], cycles[3])
        self.assertNotEqual(cycles[0], cycles[4])

    def test_kis_cycle_rotates_warmup_batch_after_repeated_missing_bars(self):
        symbols = [f"{index:06d}" for index in range(10)]

        class MissingFirstBatchProvider:
            def __init__(self):
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                if symbol in symbols[:5]:
                    return None
                return _bar(symbol=symbol)

        provider = MissingFirstBatchProvider()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=WarmupOnlyStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        cycles = []
        for _ in range(7):
            start = len(provider.requested_symbols)
            runtime.run_cycle()
            cycles.append(provider.requested_symbols[start:])

        self.assertEqual(symbols[:5], cycles[0])
        self.assertEqual(symbols[:5], cycles[5])
        self.assertEqual(symbols[5:], cycles[6])

    def test_kis_cycle_reserves_warmup_scan_budget_for_open_positions(self):
        symbols = ["OPEN01", *[f"BUY{index:03d}" for index in range(8)]]
        bars_by_symbol = {
            symbol: [_bar(symbol=symbol, close=str(10000 + offset), offset=offset) for offset in range(8)]
            for symbol in symbols
        }
        provider = SequenceBarProvider(bars_by_symbol)
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("OPEN01", 1, "seed"), _bar(symbol="OPEN01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=WarmupOnlyStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            symbol_priority_provider=lambda symbol: -1.0 if symbol == "OPEN01" else 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        cycles = []
        for _ in range(5):
            start = len(provider.requested_symbols)
            runtime.run_cycle()
            cycles.append(provider.requested_symbols[start:])

        first_scan_batch = cycles[0][1:]
        self.assertEqual(["OPEN01"], cycles[0][:1])
        self.assertEqual(4, len(first_scan_batch))
        self.assertEqual(cycles[1][1:], first_scan_batch)
        self.assertEqual(cycles[2][1:], first_scan_batch)
        self.assertEqual(cycles[3][1:], first_scan_batch)
        self.assertNotEqual(cycles[4][1:], first_scan_batch)
        self.assertTrue(all(len(cycle) == 5 for cycle in cycles))

    def test_kis_cycle_batches_when_open_positions_leave_exactly_one_extra_candidate(self):
        symbols = ["OPEN01", *[f"BUY{index:03d}" for index in range(5)]]
        bars_by_symbol = {
            symbol: [_bar(symbol=symbol, close=str(10000 + offset), offset=offset) for offset in range(8)]
            for symbol in symbols
        }
        provider = SequenceBarProvider(bars_by_symbol)
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("OPEN01", 1, "seed"), _bar(symbol="OPEN01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=WarmupOnlyStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            symbol_priority_provider=lambda symbol: -1.0 if symbol == "OPEN01" else 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        cycles = []
        for _ in range(5):
            start = len(provider.requested_symbols)
            runtime.run_cycle()
            cycles.append(provider.requested_symbols[start:])

        first_scan_batch = cycles[0][1:]
        self.assertEqual(4, len(first_scan_batch))
        self.assertEqual(cycles[1][1:], first_scan_batch)
        self.assertEqual(cycles[2][1:], first_scan_batch)
        self.assertEqual(cycles[3][1:], first_scan_batch)
        self.assertNotEqual(cycles[4][1:], first_scan_batch)

    def test_kis_cycle_replaces_known_unaffordable_warmup_symbols(self):
        symbols = ["HIGH01", "HIGH02", "BUY003", "BUY004"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ConfigurableSignalStrategy({symbol: [Signal.buy(symbol, "ranked_test")] for symbol in symbols}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="200000"),
                    "HIGH02": _bar(symbol="HIGH02", close="180000"),
                    "BUY003": _bar(symbol="BUY003", close="10000"),
                    "BUY004": _bar(symbol="BUY004", close="20000"),
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=2,
            max_bar_requests_per_cycle=2,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        runtime.run_cycle()
        first_cycle = list(runtime.bar_provider.requested_symbols)
        runtime.run_cycle()
        second_cycle = runtime.bar_provider.requested_symbols[len(first_cycle) :]

        self.assertEqual(["HIGH01", "HIGH02"], first_cycle)
        self.assertEqual(["BUY003", "BUY004"], second_cycle)

    def test_kis_affordable_order_keeps_volume_priority_before_price(self):
        symbols = ["CHEAP1", "FLOW02"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ConfigurableSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            symbol_priority_provider=lambda symbol: {"CHEAP1": 1.0, "FLOW02": 10.0}[symbol],
            data_source_kind="kis-vts",
        )
        runtime._latest_entry_prices.update({"CHEAP1": Decimal("5000"), "FLOW02": Decimal("20000")})
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["FLOW02"], runtime.bar_provider.requested_symbols)

    def test_kis_known_unaffordable_candidates_are_not_queried_even_when_all_are_too_expensive(self):
        symbols = ["HIGH01", "HIGH02", "HIGH03"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ConfigurableSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="200000") for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime._latest_entry_prices.update({symbol: Decimal("200000") for symbol in symbols})
        runtime.start()

        first_cycle = runtime.run_cycle()
        second_cycle = runtime.run_cycle()
        third_cycle = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        for events in (first_cycle, second_cycle, third_cycle):
            self.assertTrue(
                any(
                    event.kind == "system"
                    and "known_unaffordable" in event.message
                    and "no_scannable_candidates" in event.message
                    for event in events
                )
            )

    def test_kis_known_unaffordable_symbols_do_not_consume_quote_budget(self):
        symbols = ["HIGH01", "HIGH02", "BUY003", "BUY004"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ConfigurableSignalStrategy({symbol: [Signal.buy(symbol, "ranked_test")] for symbol in symbols}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="200000"),
                    "HIGH02": _bar(symbol="HIGH02", close="180000"),
                    "BUY003": _bar(symbol="BUY003", close="10000"),
                    "BUY004": _bar(symbol="BUY004", close="20000"),
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2, order_cash_amount=Decimal("50000")),
            scan_limit_per_cycle=3,
            max_bar_requests_per_cycle=3,
            symbol_priority_provider=lambda symbol: {"HIGH01": 100.0, "HIGH02": 90.0, "BUY003": 10.0, "BUY004": 9.0}[symbol],
            data_source_kind="kis-vts",
        )
        runtime.seed_entry_prices(
            {
                "HIGH01": Decimal("400000"),
                "HIGH02": Decimal("350000"),
                "BUY003": Decimal("10000"),
                "BUY004": Decimal("20000"),
            }
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BUY003", "BUY004"], runtime.bar_provider.requested_symbols)
        self.assertEqual({"BUY003", "BUY004"}, set(runtime.broker.snapshot().positions))

    def test_kis_short_enabled_does_not_skip_buy_unaffordable_sell_affordable_symbol(self):
        symbol = "SHORT1"
        bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 9, 0),
            open=Decimal("50000"),
            high=Decimal("50001"),
            low=Decimal("50000"),
            close=Decimal("50000"),
            volume=1000,
            vwap=Decimal("50000"),
            bid=Decimal("50000"),
            ask=Decimal("50001"),
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            strategy=ConfigurableSignalStrategy({symbol: [Signal.short(symbol, "downtrend_short")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: bar}),
            symbol_directory=SymbolDirectory({symbol: "Short Test"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                allow_paper_short=True,
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            data_source_kind="kis-vts",
        )
        runtime.seed_entry_prices({symbol: Decimal("50001")})
        runtime.start()

        events = runtime.run_cycle()

        trades = [event for event in events if event.kind == "trade"]
        self.assertEqual([symbol], runtime.bar_provider.requested_symbols)
        self.assertEqual([("SHORT_ENTRY", "filled")], [(event.side, event.result) for event in trades])

    def test_kis_short_enabled_does_not_requery_symbol_when_both_entry_prices_are_unaffordable(self):
        strategy = ConfigurableSignalStrategy(
            {
                "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                "BUY002": [Signal.buy("BUY002", "replacement_entry")],
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="400000"),
                    "BUY002": _bar(symbol="BUY002", close="10000", offset=1),
                }
            ),
            symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY002": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                allow_paper_short=True,
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            symbol_priority_provider=lambda symbol: {"HIGH01": 10.0, "BUY002": 1.0}[symbol],
            data_source_kind="kis-vts",
        )
        runtime.start()

        first_cycle = runtime.run_cycle()
        second_cycle = runtime.run_cycle()

        self.assertEqual(["HIGH01", "BUY002"], runtime.bar_provider.requested_symbols)
        self.assertNotIn("HIGH01", strategy.seen_symbols)
        self.assertIn("BUY002", strategy.seen_symbols)
        self.assertTrue(any("HIGH01" in event.message and "entry_unaffordable" in event.message for event in first_cycle))
        self.assertFalse(any("HIGH01" in event.message and "entry_unaffordable" in event.message for event in second_cycle))

    def test_kis_full_watchlist_does_not_requery_known_unaffordable_symbol_when_short_enabled(self):
        symbol = "HIGH01"
        strategy = ConfigurableSignalStrategy({symbol: [Signal.buy(symbol, "too_expensive")]})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("50000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="400000")}),
            symbol_directory=SymbolDirectory({symbol: "High Price"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                allow_paper_short=True,
            ),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            data_source_kind="kis-vts",
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual([symbol], runtime.bar_provider.requested_symbols)
        self.assertNotIn(symbol, strategy.seen_symbols)

    def test_kis_known_unaffordable_symbol_can_reenter_scan_when_dynamic_budget_increases(self):
        symbol = "HIGH01"
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            strategy=ConfigurableSignalStrategy({symbol: [Signal.buy(symbol, "now_affordable")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("50000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="350000")}),
            symbol_directory=SymbolDirectory({symbol: "High Price"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                allow_paper_short=True,
            ),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            data_source_kind="kis-vts",
        )
        runtime.seed_entry_prices({symbol: Decimal("350000")}, {symbol: Decimal("350000")})
        runtime.start()

        runtime.run_cycle()
        runtime.apply_strategy_settings(
            settings=runtime.settings.with_updates(max_symbol_exposure=Decimal("0.50")),
            risk_config=RiskConfig(max_order_amount=Decimal("0"), max_position_amount=Decimal("500000"), max_positions=1),
            profile_label="budget increase",
        )
        runtime.run_cycle()

        self.assertEqual([symbol], runtime.bar_provider.requested_symbols)

    def test_kis_long_only_affordability_uses_buy_price_not_bid(self):
        strategy = ConfigurableSignalStrategy(
            {
                "WIDE01": [Signal.buy("WIDE01", "wide_spread")],
                "BUY002": [Signal.buy("BUY002", "replacement_entry")],
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["WIDE01", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider(
                {
                    "WIDE01": MarketBar(
                        symbol="WIDE01",
                        timestamp=datetime(2026, 6, 11, 9, 0),
                        open=Decimal("50000"),
                        high=Decimal("50000"),
                        low=Decimal("50000"),
                        close=Decimal("50000"),
                        volume=1000,
                        vwap=Decimal("50000"),
                        bid=Decimal("50000"),
                        ask=Decimal("50001"),
                    ),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            symbol_directory=SymbolDirectory({"WIDE01": "Wide Spread", "BUY002": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        runtime.run_cycle()
        first_cycle = list(runtime.bar_provider.requested_symbols)
        runtime.run_cycle()
        second_cycle = runtime.bar_provider.requested_symbols[len(first_cycle) :]

        self.assertEqual(["WIDE01"], first_cycle)
        self.assertEqual(["BUY002"], second_cycle)
        self.assertNotIn("WIDE01", strategy.seen_symbols)

    def test_kis_unaffordable_quote_skips_entry_warmup_strategy(self):
        strategy = ConfigurableSignalStrategy({})
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="200000"),
                    "BUY002": _bar(symbol="BUY002", close="10000"),
                }
            ),
            symbol_directory=SymbolDirectory({"HIGH01": "Too Expensive", "BUY002": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            symbol_priority_provider=lambda symbol: 0.0,
            data_source_kind="kis-vts",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["HIGH01"], runtime.bar_provider.requested_symbols)
        self.assertNotIn("HIGH01", strategy.seen_symbols)
        self.assertNotIn("HIGH01", runtime._successful_bar_samples)
        self.assertTrue(any("entry_unaffordable" in event.message for event in events if event.kind == "system"))

    def test_kis_first_live_quote_uses_intraday_open_to_seed_strategy_history(self):
        symbol = "BUY001"
        bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 13, 0),
            open=Decimal("10000"),
            high=Decimal("11100"),
            low=Decimal("9900"),
            close=Decimal("11000"),
            volume=0,
            vwap=Decimal("10900"),
            bid=Decimal("10990"),
            ask=Decimal("11010"),
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_signal_confidence=Decimal("0.25"),
                    min_volume_ratio=Decimal("0"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({symbol: bar}),
            symbol_directory=SymbolDirectory({symbol: "Buy One"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            data_source_kind="kis-vts",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertFalse(any("insufficient_data" in event.message for event in events if event.kind == "system"))
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))

    def test_external_scan_kis_sparse_scanner_bar_uses_final_quote_to_seed_strategy_history(self):
        symbol = "BUY001"
        scanner_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 12, 59),
            open=Decimal("10000"),
            high=Decimal("10000"),
            low=Decimal("10000"),
            close=Decimal("10000"),
            volume=0,
            vwap=Decimal("10000"),
            bid=Decimal("10000"),
            ask=Decimal("10000"),
        )
        final_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 13, 0),
            open=Decimal("10000"),
            high=Decimal("11100"),
            low=Decimal("9900"),
            close=Decimal("11000"),
            volume=5000,
            vwap=Decimal("10900"),
            bid=Decimal("10990"),
            ask=Decimal("11010"),
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_signal_confidence=Decimal("0.25"),
                    min_volume_ratio=Decimal("0"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}, priorities={symbol: 100.0}),
            symbol_directory=SymbolDirectory({symbol: "Buy One"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=5,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertFalse(any("scanner_data_sparse" in event.message for event in events if event.kind == "system"))
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))

    def test_live_execution_preserves_external_scanner_final_quote_confirmation(self):
        symbol = "BUY001"
        scanner_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 12, 59),
            open=Decimal("10000"),
            high=Decimal("10000"),
            low=Decimal("10000"),
            close=Decimal("10000"),
            volume=0,
            vwap=Decimal("10000"),
            bid=Decimal("10000"),
            ask=Decimal("10000"),
        )
        final_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 13, 0),
            open=Decimal("10000"),
            high=Decimal("11100"),
            low=Decimal("9900"),
            close=Decimal("11000"),
            volume=5000,
            vwap=Decimal("10900"),
            bid=Decimal("10990"),
            ask=Decimal("11010"),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        actual_history = [
            replace(
                final_bar,
                timestamp=datetime(2026, 6, 11, 12, 57 + index),
                open=Decimal(str(10000 + (index * 300))),
                high=Decimal(str(10000 + (index * 300))),
                low=Decimal(str(10000 + (index * 300))),
                close=Decimal(str(10000 + (index * 300))),
                volume=1000,
                vwap=Decimal(str(10000 + (index * 300))),
                bid=Decimal(str(10000 + (index * 300))),
                ask=Decimal(str(10000 + (index * 300))),
            )
            for index in range(3)
        ]
        final_quotes = DictBarProvider({symbol: final_bar})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_signal_confidence=Decimal("0.25"),
                    min_volume_ratio=Decimal("0"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda requested_symbol: (
                actual_history if requested_symbol == symbol else []
            ),
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}, priorities={symbol: 100.0}),
            symbol_directory=SymbolDirectory({symbol: "Buy One"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=5,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertTrue(any("scanner_diagnostic - external_scan_cycle" in event.message for event in events))
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))

    def test_live_authoritative_scanner_reuses_cycle_account_snapshot_for_multiple_entries(self):
        symbols = ["BUY001", "BUY002", "BUY003"]
        scanner_bars = {symbol: _bar(symbol=symbol, close="6000") for symbol in symbols}
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="6000", offset=1) for symbol in symbols})
        broker = RateLimitedLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100202"),
                equity_override=Decimal("100202"),
                buying_power_override=Decimal("100202"),
            ),
            max_snapshot_calls=1,
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "slot_fill")] for symbol in symbols}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("300000"),
                    max_positions=10,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=10,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=3,
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=3,
        )
        runtime.start()
        broker.snapshot_calls = 0

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(symbols, [event.symbol for event in filled])
        self.assertEqual(symbols, final_quotes.requested_symbols)
        self.assertEqual(1, broker.snapshot_calls)

    def test_live_cycle_turns_account_snapshot_rate_limit_into_controlled_skip(self):
        symbol = "BUY001"
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="6000", offset=1)})
        broker = RateLimitedLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100202"),
                equity_override=Decimal("100202"),
                buying_power_override=Decimal("100202"),
            ),
            max_snapshot_calls=1,
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "slot_fill")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("0"), max_positions=10)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: _bar(symbol=symbol, close="6000")}),
            symbol_directory=SymbolDirectory({symbol: "Buy One"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=10,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()
        broker.snapshot_calls = 0
        broker.max_snapshot_calls = 0

        events = runtime.run_cycle()

        messages = [event.message for event in events if event.kind == "system"]
        self.assertTrue(any("rate_limit_skip - live_account_snapshot" in message for message in messages))
        self.assertFalse(any("scanner_diagnostic - external_scan_cycle" in message for message in messages))
        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual(1, runtime.cycle_count)

    def test_live_start_adopts_all_sellable_holdings_without_re_adopting_stale_post_sell_snapshot(self):
        exit_symbol = "EXIT01"
        held_symbol = "HOLD02"
        positions = {
            exit_symbol: Position(
                symbol=exit_symbol,
                quantity=2,
                avg_price=Decimal("10000"),
                last_price=Decimal("9000"),
                opened_at=datetime(2026, 6, 11, 9, 0),
                highest_price=Decimal("10000"),
                sellable_quantity=2,
            ),
            held_symbol: Position(
                symbol=held_symbol,
                quantity=3,
                avg_price=Decimal("7000"),
                last_price=Decimal("7100"),
                opened_at=datetime(2026, 6, 11, 9, 0),
                highest_price=Decimal("7100"),
                sellable_quantity=3,
            ),
        }
        broker = StartAdoptingManagedLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                positions=positions,
                equity_override=Decimal("1039300"),
                buying_power_override=Decimal("1000000"),
            )
        )
        strategy = FixedSignalStrategy({})
        runtime = PaperTradingRuntime(
            symbols=[exit_symbol, held_symbol],
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=10,
                    kill_switch=True,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    exit_symbol: _bar(symbol=exit_symbol, close="9000"),
                    held_symbol: _bar(symbol=held_symbol, close="7100"),
                }
            ),
            symbol_directory=SymbolDirectory(
                {
                    exit_symbol: "Exit Holding",
                    held_symbol: "Held Holding",
                }
            ),
            settings=CustomStrategySettings.default().with_updates(kill_switch=True),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, broker.adoption_calls)
        self.assertEqual(2, broker.managed_position_ledger.quantity_for(exit_symbol))
        self.assertEqual(3, broker.managed_position_ledger.quantity_for(held_symbol))

        strategy.signals = {exit_symbol: [Signal.sell(exit_symbol, "strategy_exit")]}
        exit_events = runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(1, broker.adoption_calls)
        self.assertEqual(
            [("SELL", exit_symbol, 2)],
            [(order.side, order.symbol, order.quantity) for order in broker.orders],
        )
        self.assertEqual(0, broker.managed_position_ledger.quantity_for(exit_symbol))
        self.assertEqual(3, broker.managed_position_ledger.quantity_for(held_symbol))
        self.assertTrue(
            any(
                event.kind == "trade"
                and event.result == "filled"
                and event.side == "SELL"
                and event.symbol == exit_symbol
                for event in exit_events
            )
        )

    def test_live_start_adoption_completes_once_while_pending_buy_blocks_only_new_entries(self):
        exit_symbol = "EXIT01"
        broker = PendingBuyStartAdoptingManagedLiveBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("1000000"),
                positions={
                    exit_symbol: Position(
                        symbol=exit_symbol,
                        quantity=2,
                        avg_price=Decimal("10000"),
                        last_price=Decimal("9000"),
                        opened_at=datetime(2026, 6, 11, 9, 0),
                        highest_price=Decimal("10000"),
                        sellable_quantity=2,
                    )
                },
            ),
            pending_symbol="PEND01",
        )
        strategy = FixedSignalStrategy({})
        runtime = PaperTradingRuntime(
            symbols=[exit_symbol],
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=10,
                    kill_switch=True,
                )
            ),
            bar_provider=DictBarProvider(
                {exit_symbol: _bar(symbol=exit_symbol, close="9000")}
            ),
            symbol_directory=SymbolDirectory({exit_symbol: "Exit Holding"}),
            settings=CustomStrategySettings.default().with_updates(kill_switch=True),
            execution_mode="live",
        )
        runtime.start()

        runtime.run_cycle()
        strategy.signals = {
            exit_symbol: [Signal.sell(exit_symbol, "strategy_exit")]
        }
        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(1, broker.adoption_calls)
        self.assertEqual(
            [("SELL", exit_symbol, 2)],
            [(order.side, order.symbol, order.quantity) for order in broker.orders],
        )
        self.assertEqual(
            0,
            broker.managed_position_ledger.quantity_for(exit_symbol),
        )

    def test_live_cycle_turns_post_adoption_snapshot_rate_limit_into_controlled_skip(self):
        symbol = "BUY001"
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="6000", offset=1)})
        broker = AdoptingRateLimitedLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100202"),
                equity_override=Decimal("100202"),
                buying_power_override=Decimal("100202"),
                positions={
                    "HOLD01": Position(
                        symbol="HOLD01",
                        quantity=1,
                        avg_price=Decimal("4335"),
                        last_price=Decimal("4225"),
                        opened_at=datetime(2026, 6, 11, 9, 0),
                        highest_price=Decimal("4335"),
                    )
                },
            ),
            max_snapshot_calls=1,
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "slot_fill")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("0"), max_positions=10)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: _bar(symbol=symbol, close="6000")}),
            symbol_directory=SymbolDirectory({symbol: "Buy One", "HOLD01": "Held One"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=10,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()
        broker.snapshot_calls = 0

        events = runtime.run_cycle()

        messages = [event.message for event in events if event.kind == "system"]
        self.assertTrue(any("live_existing_positions_adopted - count=1" in message for message in messages))
        self.assertTrue(
            any(
                "rate_limit_skip - live_account_snapshot: stage=post_adoption" in message
                for message in messages
            )
        )
        self.assertFalse(any("scanner_diagnostic - external_scan_cycle" in message for message in messages))
        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual(1, runtime.cycle_count)

    def test_external_scan_kis_ranked_candidate_can_pass_volume_warmup(self):
        symbol = "BUYVOL"
        scanner_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 12, 59),
            open=Decimal("10000"),
            high=Decimal("11100"),
            low=Decimal("9900"),
            close=Decimal("11000"),
            volume=1_000_000,
            vwap=Decimal("10500"),
            bid=Decimal("10990"),
            ask=Decimal("11010"),
        )
        final_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 13, 0),
            open=Decimal("10000"),
            high=Decimal("11100"),
            low=Decimal("9900"),
            close=Decimal("11000"),
            volume=1_000_000,
            vwap=Decimal("10500"),
            bid=Decimal("10990"),
            ask=Decimal("11010"),
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_signal_confidence=Decimal("0.25"),
                    min_volume_ratio=Decimal("1.2"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}, priorities={symbol: 100.0}),
            symbol_directory=SymbolDirectory({symbol: "Volume Candidate"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                max_symbol_exposure=Decimal("0.10"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertTrue(diagnostics)
        self.assertIn("final_quotes=1/1", diagnostics[-1])
        self.assertFalse(
            any("volume_below_minimum" in event.message for event in events if event.kind == "system")
        )

    def test_live_external_scan_confirms_missing_executable_quote_before_strategy(self):
        class FinalQuoteOnlyStrategy(FixedSignalStrategy):
            def __init__(self):
                super().__init__({})
                self.seen_bars = []

            def on_bar(self, bar, account):
                self.seen_bars.append(bar)
                if bar.ask is None or bar.close != Decimal("10100"):
                    return []
                return [Signal.buy(bar.symbol, "confirmed_final_quote")]

        symbol = "BUY001"
        scanner_bar = replace(
            _bar(symbol=symbol, close="10000"),
            open=Decimal("9900"),
            high=Decimal("10100"),
            low=Decimal("9900"),
            bid=None,
            ask=None,
            trading_state_source="",
        )
        final_bar = replace(
            _bar(symbol=symbol, close="10100", offset=1),
            open=Decimal("10000"),
            high=Decimal("10150"),
            low=Decimal("9950"),
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        strategy = FinalQuoteOnlyStrategy()
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                max_symbol_exposure=Decimal("0.10"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual([final_bar], strategy.seen_bars)
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertTrue(diagnostics)
        self.assertIn("sparse_candidates=0", diagnostics[-1])
        self.assertIn("confirmation_candidates=1", diagnostics[-1])
        self.assertIn("confirmation_reasons=scanner_quote_missing:1", diagnostics[-1])
        self.assertFalse(any("final_quote_strategy_changed" in event.message for event in events))

    def test_paper_external_scan_does_not_confirm_dense_missing_quote_before_strategy(self):
        class RecordingStrategy(FixedSignalStrategy):
            def __init__(self):
                super().__init__({})
                self.seen_bars = []

            def on_bar(self, bar, account):
                self.seen_bars.append(bar)
                return []

        symbols = ["BUY001", "BUY002"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000"),
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9900"),
                bid=None,
                ask=None,
                trading_state_source="",
            )
            for symbol in symbols
        }
        final_quotes = DictBarProvider(
            {symbol: _bar(symbol=symbol, close="10100", offset=1) for symbol in symbols}
        )
        strategy = RecordingStrategy()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            scan_limit_per_cycle=2,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual([scanner_bars[symbol] for symbol in symbols], strategy.seen_bars)
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertTrue(diagnostics)
        self.assertIn("confirmation_candidates=0", diagnostics[-1])
        self.assertIn("confirmation_reasons=none", diagnostics[-1])

    def test_external_scan_kis_scan_confirmation_respects_final_quote_cap_and_reuses_quote(self):
        symbols = ["BUY001", "BUY002"]
        scanner_bars = {
            symbol: MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 6, 11, 12, 59),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=0,
                vwap=Decimal("10000"),
                bid=Decimal("10000"),
                ask=Decimal("10000"),
            )
            for symbol in symbols
        }
        final_quotes = DictBarProvider(
            {
                "BUY001": MarketBar(
                    symbol="BUY001",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("11100"),
                    low=Decimal("9900"),
                    close=Decimal("11000"),
                    volume=5000,
                    vwap=Decimal("10900"),
                    bid=Decimal("10990"),
                    ask=Decimal("11010"),
                ),
                "BUY002": MarketBar(
                    symbol="BUY002",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("11100"),
                    low=Decimal("9900"),
                    close=Decimal("11000"),
                    volume=5000,
                    vwap=Decimal("10900"),
                    bid=Decimal("10990"),
                    ask=Decimal("11010"),
                ),
            }
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_signal_confidence=Decimal("0.25"),
                    min_volume_ratio=Decimal("0"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"BUY001": 100.0, "BUY002": 90.0},
            ),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=2,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))
        self.assertFalse(
            any(
                event.reason == "final_quote_limit_reached"
                for event in events
                if event.kind == "trade"
            )
        )

    def test_live_scanner_prioritizes_dense_entry_before_sparse_candidates_exhaust_physical_budget(self):
        sparse_symbols = [f"SPARSE{index}" for index in range(1, 6)]
        dense_symbol = "BUY001"
        held_symbol = "HOLD01"
        symbols = [*sparse_symbols, dense_symbol]
        dense_bar = replace(
            _bar(symbol=dense_symbol, close="10000"),
            open=Decimal("9900"),
            high=Decimal("10100"),
            low=Decimal("9900"),
        )

        class BudgetClient:
            def __init__(self):
                self.used = 0
                self.limit = None

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 2

            def market_read_budget_state(self):
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                self.used += count

            def end_market_read_budget(self):
                self.limit = None

        class BudgetedQuotes:
            def __init__(self, client, bars):
                self.client = client
                self.bars = bars
                self.requested_symbols = []

            def __call__(self, symbol):
                self.client.consume(2)
                self.requested_symbols.append(symbol)
                return self.bars.get(symbol)

        broker = PaperBroker(initial_cash=Decimal("80457"))
        broker.place_order(
            Order.buy(held_symbol, 1, "seed"),
            _bar(symbol=held_symbol, close="10000"),
        )
        broker.client = BudgetClient()
        scanner_bars = {
            **{symbol: _bar(symbol=symbol, close="10000") for symbol in sparse_symbols},
            dense_symbol: dense_bar,
        }
        final_quotes = BudgetedQuotes(
            broker.client,
            {
                symbol: _bar(symbol=symbol, close="10000", offset=1)
                for symbol in [held_symbol, *symbols]
            },
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {dense_symbol: [Signal.buy(dense_symbol, "dense_entry")]}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("300000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_position_amount=Decimal("300000"),
            ),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([dense_symbol], [event.symbol for event in filled])
        self.assertEqual([held_symbol, dense_symbol], final_quotes.requested_symbols[:2])
        self.assertEqual({held_symbol, dense_symbol}, set(broker.snapshot().positions))

    def test_live_sparse_confirmation_does_not_start_without_quote_read_budget(self):
        symbols = [f"SPARSE{index}" for index in range(1, 13)]

        class OneReadRemainingClient:
            def __init__(self):
                self.limit = None

            def begin_market_read_budget(self, limit):
                self.limit = limit

            def market_read_budget_state(self):
                return 13, self.limit

            def end_market_read_budget(self):
                self.limit = None

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = OneReadRemainingClient()
        final_quotes = DictBarProvider(
            {symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(
                RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual(symbols[0], runtime._scan_cursor_anchor)
        self.assertTrue(
            any(
                event.kind == "system"
                and "physical market read budget reached" in event.message
                for event in events
            )
        )
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertIn("processed=0", diagnostics[-1])
        self.assertIn("final_quotes=0/10", diagnostics[-1])

    def test_external_scan_kis_skips_unbuyable_candidates_before_final_quote_budget(self):
        symbols = ["HIGH01", "LIMIT2", "BUYOK3"]
        scanner_bars = {
            "HIGH01": _bar(symbol="HIGH01", close="380000"),
            "LIMIT2": _bar(symbol="LIMIT2", close="20000"),
            "BUYOK3": _bar(symbol="BUYOK3", close="20000"),
        }
        final_quotes = DictBarProvider({"BUYOK3": _bar(symbol="BUYOK3", close="20000", offset=1)})
        risk_manager = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_positions=3))
        risk_manager.record_entry("LIMIT2", _bar(symbol="LIMIT2").timestamp.date())
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("300000")),
            strategy=FixedSignalStrategy(
                {
                    "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                    "LIMIT2": [Signal.buy("LIMIT2", "daily_limit")],
                    "BUYOK3": [Signal.buy("BUYOK3", "replacement_entry")],
                }
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"HIGH01": 100.0, "LIMIT2": 90.0, "BUYOK3": 80.0},
            ),
            symbol_directory=SymbolDirectory(
                {"HIGH01": "High Price", "LIMIT2": "Daily Limit", "BUYOK3": "Affordable"}
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=3,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=3,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["BUYOK3"], final_quotes.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUYOK3"], [event.symbol for event in filled])
        self.assertEqual({"BUYOK3"}, set(runtime.broker.snapshot().positions))
        non_diagnostic = "\n".join(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" not in event.message
        )
        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertNotIn("HIGH01", non_diagnostic)
        self.assertNotIn("LIMIT2", non_diagnostic)
        self.assertNotIn("max_daily_entries_reached", non_diagnostic)
        self.assertTrue(diagnostics)
        self.assertIn("selected=1", diagnostics[-1])
        self.assertIn("processed=1", diagnostics[-1])
        self.assertIn("final_quotes=1/1", diagnostics[-1])
        self.assertIn("prescan_rejections=entry_unaffordable:2,max_daily_entries_reached:1", diagnostics[-1])

    def test_external_scan_prescan_rejections_include_broadened_top_up_skips(self):
        symbols = ["HIGH01", "BUYOK2", "HIGH03"]
        scanner_bars = {
            "HIGH01": _bar(symbol="HIGH01", close="380000"),
            "BUYOK2": _bar(symbol="BUYOK2", close="20000"),
            "HIGH03": _bar(symbol="HIGH03", close="380000"),
        }
        final_quotes = DictBarProvider({"BUYOK2": _bar(symbol="BUYOK2", close="20000", offset=1)})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("300000")),
            strategy=FixedSignalStrategy(
                {
                    "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                    "BUYOK2": [Signal.buy("BUYOK2", "replacement_entry")],
                    "HIGH03": [Signal.buy("HIGH03", "cached_but_not_inspected")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=3)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"HIGH01": 100.0, "BUYOK2": 90.0, "HIGH03": 80.0},
            ),
            symbol_directory=SymbolDirectory(
                {"HIGH01": "High One", "BUYOK2": "Affordable", "HIGH03": "High Three"}
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=3,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()
        runtime._latest_entry_prices["HIGH03"] = Decimal("380000")

        events = runtime.run_cycle()

        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertEqual(["BUYOK2"], final_quotes.requested_symbols)
        self.assertNotIn("HIGH01", runtime.strategy.seen_symbols)
        self.assertNotIn("HIGH03", runtime.strategy.seen_symbols)
        self.assertTrue(diagnostics)
        self.assertIn("prescan_rejections=entry_unaffordable:2", diagnostics[-1])

    def test_external_scan_kis_reaches_affordable_candidate_when_scan_limit_prefix_is_unavailable(self):
        symbols = ["HIGH01", "LIMIT2", "BUYOK3"]
        scanner_bars = {
            "HIGH01": _bar(symbol="HIGH01", close="380000"),
            "LIMIT2": _bar(symbol="LIMIT2", close="20000"),
            "BUYOK3": _bar(symbol="BUYOK3", close="20000"),
        }
        final_quotes = DictBarProvider({"BUYOK3": _bar(symbol="BUYOK3", close="20000", offset=1)})
        risk_manager = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_positions=3))
        risk_manager.record_entry("LIMIT2", _bar(symbol="LIMIT2").timestamp.date())
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("300000")),
            strategy=FixedSignalStrategy(
                {
                    "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                    "LIMIT2": [Signal.buy("LIMIT2", "daily_limit")],
                    "BUYOK3": [Signal.buy("BUYOK3", "replacement_entry")],
                }
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"HIGH01": 100.0, "LIMIT2": 90.0, "BUYOK3": 80.0},
            ),
            symbol_directory=SymbolDirectory(
                {"HIGH01": "High Price", "LIMIT2": "Daily Limit", "BUYOK3": "Affordable"}
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=3,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["BUYOK3"], final_quotes.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUYOK3"], [event.symbol for event in filled])
        rendered = "\n".join(event.message for event in events if event.kind == "system")
        self.assertNotIn("HIGH01", rendered)
        self.assertNotIn("LIMIT2", rendered)
        self.assertIn("selected=1", rendered)
        self.assertIn("processed=1", rendered)
        self.assertIn("final_quotes=1/1", rendered)

    def test_external_scan_refreshes_prior_unaffordable_cache_before_skipping_candidate(self):
        symbol = "MOVE01"
        scanner_provider = SequenceScannerProvider(
            {
                symbol: (
                    _bar(symbol=symbol, close="180000"),
                    _bar(symbol=symbol, close="20000", offset=1),
                )
            },
            priorities={symbol: 100.0},
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="20000", offset=2)})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("100000")),
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "replacement_entry")]}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: "Moved Price"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        first_events = runtime.run_cycle()
        second_events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        first_fills = [event for event in first_events if event.kind == "trade" and event.result == "filled"]
        second_fills = [event for event in second_events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([], first_fills)
        self.assertEqual([symbol], [event.symbol for event in second_fills])
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))

    def test_external_scan_cursor_candidate_is_refreshed_before_cached_skip(self):
        symbols = ["WARM01", "MOVE01"]
        scanner_provider = SequenceScannerProvider(
            {
                "WARM01": (_bar(symbol="WARM01", close="50000"),),
                "MOVE01": (_bar(symbol="MOVE01", close="20000", offset=1),),
            },
            priorities={"WARM01": 100.0, "MOVE01": 90.0},
        )
        final_quotes = DictBarProvider({"MOVE01": _bar(symbol="MOVE01", close="20000", offset=2)})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("100000")),
            strategy=FixedSignalStrategy({"MOVE01": [Signal.buy("MOVE01", "replacement_entry")]}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({"WARM01": "Warmup", "MOVE01": "Moved Price"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime._latest_entry_prices["MOVE01"] = Decimal("180000")
        runtime._scan_cursor = 1
        runtime.start()

        events = runtime.run_cycle()

        self.assertIn(("MOVE01",), scanner_provider.requested_symbols)
        self.assertEqual(["MOVE01"], final_quotes.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["MOVE01"], [event.symbol for event in filled])
        self.assertEqual({"MOVE01"}, set(runtime.broker.snapshot().positions))

    def test_external_scan_daily_limit_uses_current_scanner_bar_date_after_prime(self):
        symbol = "DAY001"
        prior_day_bar = _bar(symbol=symbol, close="20000")
        current_day_bar = MarketBar(
            symbol=symbol,
            timestamp=prior_day_bar.timestamp + timedelta(days=1),
            open=Decimal("20000"),
            high=Decimal("20000"),
            low=Decimal("20000"),
            close=Decimal("20000"),
            volume=1000,
            vwap=Decimal("20000"),
            bid=Decimal("20000"),
            ask=Decimal("20000"),
        )
        risk_manager = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_positions=1))
        risk_manager.record_entry(symbol, prior_day_bar.timestamp.date())
        final_quotes = DictBarProvider({symbol: current_day_bar})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("100000")),
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "new_trading_day")]}),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=SequenceScannerProvider(
                {symbol: (current_day_bar,)},
                priorities={symbol: 100.0},
            ),
            symbol_directory=SymbolDirectory({symbol: "New Day"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([symbol], [event.symbol for event in filled])
        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))

    def test_external_scan_no_scannable_candidates_uses_scanner_bar_date_for_daily_limit(self):
        symbol = "LIMIT1"
        bar = _bar(symbol=symbol, close="20000")
        risk_manager = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_positions=1))
        risk_manager.record_entry(symbol, bar.timestamp.date())
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("300000")),
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "daily_limit")]}),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(
                bars={symbol: bar},
                priorities={symbol: 100.0},
            ),
            symbol_directory=SymbolDirectory({symbol: "Daily Limit"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=1,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.final_quote_provider.requested_symbols)
        rendered = "\n".join(event.message for event in events if event.kind == "system")
        self.assertIn("no_scannable_candidates", rendered)
        self.assertIn("known_unavailable=1/1", rendered)
        self.assertIn("final_quotes=0/1", rendered)

    def test_external_scan_kis_emits_cycle_diagnostic_with_quote_counts(self):
        symbols = ["BUY001", "BUY002"]
        scanner_bars = {
            symbol: MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 6, 11, 12, 59),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=0,
                vwap=Decimal("10000"),
                bid=Decimal("10000"),
                ask=Decimal("10000"),
            )
            for symbol in symbols
        }
        final_quotes = DictBarProvider(
            {
                "BUY001": MarketBar(
                    symbol="BUY001",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("11100"),
                    low=Decimal("9900"),
                    close=Decimal("11000"),
                    volume=5000,
                    vwap=Decimal("10900"),
                    bid=Decimal("10990"),
                    ask=Decimal("11010"),
                )
            }
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"BUY001": 100.0, "BUY002": 90.0},
            ),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=2,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        diagnostics = [
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        ]
        self.assertTrue(diagnostics)
        self.assertIn("candidates=2", diagnostics[-1])
        self.assertIn("selected=2", diagnostics[-1])
        self.assertIn("processed=1", diagnostics[-1])
        self.assertIn("sparse_candidates=2", diagnostics[-1])
        self.assertIn("final_quotes=1/1", diagnostics[-1])
        self.assertIn("confirmed=1", diagnostics[-1])

    def test_external_scan_kis_sparse_entry_does_not_reach_strategy_when_quote_cap_exhausted(self):
        symbols = ["BUY001", "BUY002"]
        scanner_bars = {
            symbol: MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 6, 11, 12, 59),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=0,
                vwap=Decimal("10000"),
                bid=Decimal("10000"),
                ask=Decimal("10000"),
            )
            for symbol in symbols
        }
        final_quotes = DictBarProvider(
            {
                "BUY001": MarketBar(
                    symbol="BUY001",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("11100"),
                    low=Decimal("9900"),
                    close=Decimal("11000"),
                    volume=5000,
                    vwap=Decimal("10900"),
                    bid=Decimal("10990"),
                    ask=Decimal("11010"),
                ),
                "BUY002": MarketBar(
                    symbol="BUY002",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("11100"),
                    low=Decimal("9900"),
                    close=Decimal("11000"),
                    volume=5000,
                    vwap=Decimal("10900"),
                    bid=Decimal("10990"),
                    ask=Decimal("11010"),
                ),
            }
        )
        strategy = FixedSignalStrategy({symbol: [Signal.buy(symbol, "flow_score_100")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"BUY001": 100.0, "BUY002": 90.0},
            ),
            symbol_directory=SymbolDirectory({"BUY001": "Buy One", "BUY002": "Buy Two"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=2,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual(["BUY001"], strategy.seen_symbols)
        self.assertEqual({"BUY001"}, set(runtime.broker.snapshot().positions))
    def test_external_scan_kis_sparse_open_position_uses_final_quote_for_exit(self):
        symbol = "OPEN01"
        scanner_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 12, 59),
            open=Decimal("10000"),
            high=Decimal("10000"),
            low=Decimal("10000"),
            close=Decimal("10000"),
            volume=0,
            vwap=Decimal("10000"),
            bid=Decimal("10000"),
            ask=Decimal("10000"),
        )
        final_bar = MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 6, 11, 13, 0),
            open=Decimal("10000"),
            high=Decimal("12100"),
            low=Decimal("9900"),
            close=Decimal("12000"),
            volume=5000,
            vwap=Decimal("11800"),
            bid=Decimal("11990"),
            ask=Decimal("12010"),
        )
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="10000"))
        final_quotes = DictBarProvider({symbol: final_bar})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.sell(symbol, "take_profit")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}, priorities={symbol: 100.0}),
            symbol_directory=SymbolDirectory({symbol: "Open One"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertNotIn(symbol, runtime.broker.snapshot().positions)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["SELL"], [event.side for event in filled])
        self.assertEqual(Decimal("11990"), filled[0].price)

    def test_external_scan_kis_missing_open_positions_do_not_consume_entry_final_quote_cap(self):
        symbols = ["OPEN01", "OPEN02"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        for symbol in symbols:
            broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="10000"))
        final_quotes = DictBarProvider(
            {
                **{symbol: _bar(symbol=symbol, close="12000") for symbol in symbols},
                "BUY001": _bar(symbol="BUY001", close="10000"),
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    **{symbol: [Signal.sell(symbol, "take_profit")] for symbol in symbols},
                    "BUY001": [Signal.buy("BUY001", "flow_breakout")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars={"BUY001": _bar(symbol="BUY001", close="10000")},
                priorities={"BUY001": 100.0},
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in [*symbols, "BUY001"]}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([*symbols, "BUY001"], final_quotes.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([*symbols, "BUY001"], [event.symbol for event in filled])
        self.assertEqual(["SELL", "SELL", "BUY"], [event.side for event in filled])

    def test_external_scan_kis_sparse_open_positions_use_kis_quotes_beyond_entry_cap(self):
        symbols = ["OPEN01", "OPEN02"]
        scanner_bars = {
            symbol: MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 6, 11, 12, 59),
                open=Decimal("10000"),
                high=Decimal("10000"),
                low=Decimal("10000"),
                close=Decimal("10000"),
                volume=0,
                vwap=Decimal("10000"),
                bid=Decimal("10000"),
                ask=Decimal("10000"),
            )
            for symbol in symbols
        }
        final_quotes = DictBarProvider(
            {
                "OPEN01": MarketBar(
                    symbol="OPEN01",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("12100"),
                    low=Decimal("9900"),
                    close=Decimal("12000"),
                    volume=5000,
                    vwap=Decimal("11800"),
                    bid=Decimal("11990"),
                    ask=Decimal("12010"),
                ),
                "OPEN02": MarketBar(
                    symbol="OPEN02",
                    timestamp=datetime(2026, 6, 11, 13, 0),
                    open=Decimal("10000"),
                    high=Decimal("12100"),
                    low=Decimal("9900"),
                    close=Decimal("12000"),
                    volume=5000,
                    vwap=Decimal("11800"),
                    bid=Decimal("11990"),
                    ask=Decimal("12010"),
                ),
            }
        )
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        for symbol in symbols:
            broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="10000"))
        strategy = FixedSignalStrategy({symbol: [Signal.sell(symbol, "take_profit")] for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"OPEN01": 100.0, "OPEN02": 90.0},
            ),
            symbol_directory=SymbolDirectory({"OPEN01": "Open One", "OPEN02": "Open Two"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=2,
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["OPEN01", "OPEN02"], final_quotes.requested_symbols)
        self.assertEqual(["OPEN01", "OPEN02"], strategy.seen_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertFalse(any("final_quote_limit_reached" in event.message for event in events if event.kind == "system"))

    def test_kis_seeded_prices_skip_unaffordable_symbols_before_quote_budget_is_spent(self):
        symbols = ["HIGH01", "BUY001", "BUY002", "BUY003", "BUY004", "BUY005"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ConfigurableSignalStrategy({symbol: [Signal.buy(symbol, "seeded_entry")] for symbol in symbols}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=5,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="200000"),
                    **{symbol: _bar(symbol=symbol, close="10000") for symbol in symbols if symbol != "HIGH01"},
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5, order_cash_amount=Decimal("50000")),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
            symbol_priority_provider=lambda symbol: 100.0 if symbol == "HIGH01" else 10.0,
            data_source_kind="kis-vts",
        )
        runtime.seed_entry_prices({"HIGH01": Decimal("200000"), **{symbol: Decimal("10000") for symbol in symbols[1:]}})
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(symbols[1:], runtime.bar_provider.requested_symbols)
        self.assertEqual(set(symbols[1:]), set(runtime.broker.snapshot().positions))

    def test_cycle_keeps_open_positions_first_before_volume_priority_scan(self):
        symbols = ["LOW001", "HIGH01"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("LOW001", 1, "seed"), _bar(symbol="LOW001", close="10000"))
        runtime = make_runtime(
            symbols=symbols,
            broker=broker,
            symbol_priority_provider=lambda symbol: {"LOW001": 1.0, "HIGH01": 10.0}[symbol],
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["LOW001", "HIGH01"], runtime.bar_provider.requested_symbols)

    def test_cycle_top_up_uses_volume_priority_beyond_normal_scan_window(self):
        symbols = ["EXIT01", "LOW001", "HIGH01"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "HIGH01": [Signal.buy("HIGH01", "high_priority_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
            symbol_priority_provider=lambda symbol: {"EXIT01": 0.0, "LOW001": 1.0, "HIGH01": 10.0}[symbol],
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event.symbol for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01", "HIGH01"], filled)
        self.assertEqual(["EXIT01", "HIGH01"], runtime.bar_provider.requested_symbols[:2])
        self.assertEqual({"HIGH01"}, set(runtime.broker.snapshot().positions))

    def test_empty_portfolio_refill_pass_limit_accounts_for_strategy_history_window(self):
        strategy = FlowScalperStrategy(
            FlowScalperConfig(momentum_window=2, volume_window=2, trend_boundary_window=3)
        )
        runtime = make_runtime(strategy=strategy)

        self.assertEqual(4, runtime._empty_portfolio_refill_pass_limit())

    def test_cycle_replenishes_position_slot_after_exit_in_same_cycle(self):
        symbols = ["EXIT01", "BUY002"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01", "BUY002"], [event.symbol for event in filled])
        self.assertEqual({"BUY002"}, set(runtime.broker.snapshot().positions))

    def test_cycle_rechecks_next_paper_sample_when_all_positions_exit_without_replacement(self):
        symbols = ["EXIT01", "BUY002"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=RefillOnSecondVisitStrategy(),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01", "BUY002"], [event.symbol for event in filled])
        self.assertEqual({"BUY002"}, set(runtime.broker.snapshot().positions))
        self.assertGreaterEqual(provider.requested_symbols.count("BUY002"), 2)

    def test_cycle_does_not_rebuy_symbol_exited_in_same_cycle(self):
        symbols = ["EXIT01"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=ExitAndReenterSameSymbolStrategy(),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["SELL"], [event.side for event in filled])
        self.assertEqual({}, runtime.broker.snapshot().positions)

    def test_cycle_top_up_scans_past_normal_window_until_replacement_candidate_is_found(self):
        symbols = ["EXIT01", "NO0001", "NO0002", "BUY004"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY004": [Signal.buy("BUY004", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["EXIT01", "NO0001", "NO0002", "BUY004"], runtime.bar_provider.requested_symbols)
        self.assertIn("BUY004", runtime.broker.snapshot().positions)

    def test_cycle_respects_total_bar_request_cap_during_top_up_scan(self):
        symbols = ["NO0001", "NO0002", "NO0003", "NO0004", "BUY005", "NO0006", "BUY007"]
        provider = DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY007": [Signal.buy("BUY007", "late_replacement_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=5,
                )
            ),
            bar_provider=provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=5, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(symbols[:5], provider.requested_symbols)
        self.assertNotIn("BUY007", runtime.broker.snapshot().positions)
        self.assertEqual([], [event for event in events if event.kind == "trade" and event.result == "filled"])

    def test_cycle_replenishes_after_partial_exit_with_large_cap_and_small_scan(self):
        symbols = ["NO0000", "EXIT01", "KEEP01", "NO0001", "NO0002", "NO0003", "BUY007"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        broker.place_order(Order.buy("KEEP01", 5, "seed"), _bar(symbol="KEEP01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "BUY007": [Signal.buy("BUY007", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=4,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=4, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01", "BUY007"], [event.symbol for event in filled])
        self.assertEqual(["SELL", "BUY"], [event.side for event in filled])
        self.assertEqual({"KEEP01", "BUY007"}, set(runtime.broker.snapshot().positions))

    def test_cycle_replenishes_after_partial_exit_when_positions_are_unlimited(self):
        symbols = ["NO0000", "EXIT01", "KEEP01", "NO0001", "NO0002", "NO0003", "BUY007"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        broker.place_order(Order.buy("KEEP01", 5, "seed"), _bar(symbol="KEEP01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "BUY007": [Signal.buy("BUY007", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01", "BUY007"], [event.symbol for event in filled])
        self.assertEqual(["SELL", "BUY"], [event.side for event in filled])
        self.assertEqual({"KEEP01", "BUY007"}, set(runtime.broker.snapshot().positions))

    def test_cycle_tops_up_underfilled_finite_position_limit_without_waiting_for_exit(self):
        symbols = ["KEEP01", "NO0001", "BUY003"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("KEEP01", 5, "seed"), _bar(symbol="KEEP01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({"BUY003": [Signal.buy("BUY003", "top_up_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY003"], [event.symbol for event in filled])
        self.assertEqual({"KEEP01", "BUY003"}, set(runtime.broker.snapshot().positions))

    def test_cycle_tops_up_underfilled_unlimited_position_target_without_waiting_for_exit(self):
        symbols = ["KEEP01", "NO0001", "NO0002", "BUY004", "BUY005"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("KEEP01", 5, "seed"), _bar(symbol="KEEP01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "BUY004": [Signal.buy("BUY004", "top_up_entry")],
                    "BUY005": [Signal.buy("BUY005", "top_up_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=3,
        )
        runtime.start()

        self.assertEqual(3, runtime._unlimited_entry_slot_target())
        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertCountEqual(["BUY004", "BUY005"], [event.symbol for event in filled])
        self.assertEqual({"KEEP01", "BUY004", "BUY005"}, set(runtime.broker.snapshot().positions))

    def test_cycle_top_up_can_expand_unlimited_positions_after_exit(self):
        symbols = ["EXIT01", "KEEP01", "KEEP02", "BUY004", "BUY005"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        for symbol in ["EXIT01", "KEEP01", "KEEP02"]:
            broker.place_order(Order.buy(symbol, 5, "seed"), _bar(symbol=symbol, close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY004": [Signal.buy("BUY004", "top_up_entry")],
                    "BUY005": [Signal.buy("BUY005", "top_up_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=3,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled_entries = [
            event
            for event in events
            if event.kind == "trade" and event.result == "filled" and event.side == "BUY"
        ]
        self.assertEqual(2, len(filled_entries))
        self.assertEqual(4, len(runtime.broker.snapshot().positions))
        self.assertNotIn("EXIT01", runtime.broker.snapshot().positions)

    def test_empty_unlimited_cycle_expands_past_initial_scan_limit_to_fill_target(self):
        symbols = ["NO0001", "NO0002", "NO0003", "BUY004", "BUY005"]
        scanner_provider = StaticScannerProvider(
            bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
            priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "BUY004": [Signal.buy("BUY004", "late_entry")],
                    "BUY005": [Signal.buy("BUY005", "late_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=3,
            data_source_kind="external-scan-kis",
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertCountEqual(["BUY004", "BUY005"], [event.symbol for event in filled])
        self.assertEqual(["NO0001", "NO0002", "NO0003", "BUY004", "BUY005"], runtime.strategy.seen_symbols)

    def test_cycle_top_up_keeps_scanning_when_pending_entry_candidate_rejects(self):
        class ScoredFixedSignalStrategy(FixedSignalStrategy):
            def last_entry_score(self, symbol):
                score = {"BAD001": 0.9, "BUY003": 0.1}.get(symbol, 0.0)
                return SignalScore(
                    symbol=symbol,
                    long_score=score,
                    short_score=score,
                    confidence=score,
                    direction="long",
                    reasons=("ranked_test",),
                )

        symbols = ["EXIT01", "BAD001", "BUY003"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=ScoredFixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BAD001": [Signal.short("BAD001", "short_disabled")],
                    "BUY003": [Signal.buy("BUY003", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=1,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(["filled", "rejected", "filled"], [event.result for event in trade_events])
        self.assertEqual(["EXIT01", "BAD001", "BUY003"], [event.symbol for event in trade_events])
        self.assertEqual({"BUY003"}, set(runtime.broker.snapshot().positions))

    def test_cycle_replenishment_ignores_existing_position_entry_candidate_for_open_slots(self):
        symbols = ["EXIT01", "KEEP01", "BUY003"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        broker.place_order(Order.buy("KEEP01", 5, "seed"), _bar(symbol="KEEP01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "KEEP01": [Signal.buy("KEEP01", "scale_in")],
                    "BUY003": [Signal.buy("BUY003", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event.symbol for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertIn("BUY003", filled)
        self.assertEqual({"KEEP01", "BUY003"}, set(runtime.broker.snapshot().positions))

    def test_cycle_replenishment_counts_unique_new_candidate_symbols_for_open_slots(self):
        symbols = ["EXIT01", "EXIT02", "DUPE01", "BUY004"]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="10000"))
        broker.place_order(Order.buy("EXIT02", 5, "seed"), _bar(symbol="EXIT02", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "EXIT02": [Signal.sell("EXIT02", "take_profit")],
                    "DUPE01": [Signal.buy("DUPE01", "duplicate_entry"), Signal.buy("DUPE01", "duplicate_entry_again")],
                    "BUY004": [Signal.buy("BUY004", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=2, order_cash_amount=Decimal("10000")),
            scan_limit_per_cycle=3,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event.symbol for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertIn("BUY004", filled)
        self.assertEqual({"DUPE01", "BUY004"}, set(runtime.broker.snapshot().positions))

    def test_cycle_with_unlimited_positions_fills_all_ranked_entry_candidates(self):
        symbols = ["AAA001", "BBB002", "CCC003", "DDD004", "EEE005"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=RankedSignalStrategy(
                {
                    "AAA001": 0.91,
                    "BBB002": 0.92,
                    "CCC003": 0.93,
                    "DDD004": 0.94,
                    "EEE005": 0.95,
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0, order_cash_amount=Decimal("10000")),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(5, len(filled))
        self.assertEqual(set(symbols), set(runtime.broker.snapshot().positions))

    def test_kis_cycle_uses_remaining_slot_budget_for_entry_affordability_and_order_size(self):
        runtime = PaperTradingRuntime(
            symbols=["BIG001", "MID002"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "BIG001": [Signal.buy("BIG001", "slot_budget_entry")],
                    "MID002": [Signal.buy("MID002", "slot_budget_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("250000"),
                    max_position_amount=Decimal("250000"),
                    max_positions=5,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "BIG001": _bar(symbol="BIG001", close="130000"),
                    "MID002": _bar(symbol="MID002", close="120000"),
                }
            ),
            symbol_directory=SymbolDirectory({"BIG001": "Big Price", "MID002": "Middle Price"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=5,
                order_cash_amount=Decimal("50000"),
            ),
            data_source_kind="kis-vts",
            scan_limit_per_cycle=5,
            max_bar_requests_per_cycle=5,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual({"BIG001", "MID002"}, {event.symbol for event in filled})
        self.assertEqual([2, 1], [event.quantity for event in filled])
        self.assertEqual({"BIG001", "MID002"}, set(runtime.broker.snapshot().positions))

    def test_kis_cycle_filters_only_candidates_above_remaining_slot_budget_before_strategy(self):
        strategy = FixedSignalStrategy(
            {
                "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                "BUY002": [Signal.buy("BUY002", "slot_budget_entry")],
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("250000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("200000"),
                    max_position_amount=Decimal("200000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="180000"),
                    "BUY002": _bar(symbol="BUY002", close="80000"),
                }
            ),
            symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY002": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.50"),
            ),
            data_source_kind="kis-vts",
            scan_limit_per_cycle=2,
            max_bar_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY002"], [event.symbol for event in filled])
        self.assertEqual(["BUY002"], strategy.seen_symbols)
        self.assertTrue(any("HIGH01" in event.message and "entry_unaffordable" in event.message for event in events))
        self.assertEqual({"BUY002"}, set(runtime.broker.snapshot().positions))

    def test_local_cycle_filters_unaffordable_candidates_before_strategy(self):
        strategy = FixedSignalStrategy(
            {
                "HIGH01": [Signal.buy("HIGH01", "too_expensive")],
                "BUY002": [Signal.buy("BUY002", "slot_budget_entry")],
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("250000")),
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("200000"),
                    max_position_amount=Decimal("200000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="180000"),
                    "BUY002": _bar(symbol="BUY002", close="80000"),
                }
            ),
            symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY002": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.50"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY002"], [event.symbol for event in filled])
        self.assertEqual(["BUY002"], strategy.seen_symbols)
        self.assertTrue(any("HIGH01" in event.message and "entry_unaffordable" in event.message for event in events))
        self.assertEqual({"BUY002"}, set(runtime.broker.snapshot().positions))

    def test_local_unlimited_positions_do_not_reduce_budget_to_order_cash_amount(self):
        runtime = PaperTradingRuntime(
            symbols=["BUY090"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY090": [Signal.buy("BUY090", "slot_budget_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("100000"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({"BUY090": _bar(symbol="BUY090", close="90000")}),
            symbol_directory=SymbolDirectory({"BUY090": "Budget Fit"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                order_cash_amount=Decimal("10000"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY090", 1)], [(event.symbol, event.quantity) for event in filled])
        self.assertEqual({"BUY090"}, set(runtime.broker.snapshot().positions))

    def test_unlimited_positions_use_full_cash_after_reserving_one_share_per_symbol(self):
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{i:03d}" for i in range(10)],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {f"BUY{i:03d}": [Signal.buy(f"BUY{i:03d}", "cash_slot_target")] for i in range(10)}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({f"BUY{i:03d}": _bar(symbol=f"BUY{i:03d}", close="10000") for i in range(10)}),
            symbol_directory=SymbolDirectory({f"BUY{i:03d}": f"Buy {i}" for i in range(10)}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=10,
            max_final_quote_requests_per_cycle=10,
        )
        runtime.start()

        budget = runtime._entry_budget_for_account(runtime.broker.snapshot())
        events = runtime.run_cycle()

        self.assertEqual(Decimal("1000000.00"), budget.quantize(Decimal("0.01")))
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(10, len(filled))
        self.assertTrue(all(event.quantity >= 1 for event in filled))
        self.assertEqual(100, sum(event.quantity for event in filled))
        self.assertEqual(Decimal("0"), runtime.broker.snapshot().cash)

    def test_unlimited_positions_use_affordable_universe_instead_of_hidden_ten_position_target(self):
        symbols = [f"BUY{i:03d}" for i in range(20)]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "affordable_breadth_target")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider(
                {symbol: _bar(symbol=symbol, close="10000") for symbol in symbols}
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            max_final_quote_requests_per_cycle=20,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        snapshot = runtime.broker.snapshot()
        self.assertEqual(20, len(filled))
        self.assertEqual(20, len(snapshot.positions))
        self.assertEqual(Decimal("0"), snapshot.cash)

    def test_unlimited_slot_budget_uses_actual_entry_candidates_not_no_signal_prices(self):
        cheap_symbols = [f"HOLD{i:03d}" for i in range(19)]
        buy_symbol = "BUY100"
        symbols = [*cheap_symbols, buy_symbol]
        bars = {
            **{symbol: _bar(symbol=symbol, close="10000") for symbol in cheap_symbols},
            buy_symbol: _bar(symbol=buy_symbol, close="100000"),
        }
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {buy_symbol: [Signal.buy(buy_symbol, "only_valid_entry_candidate")]}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider(bars),
            final_quote_provider=DictBarProvider(bars),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            max_final_quote_requests_per_cycle=20,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([(buy_symbol, 10)], [(event.symbol, event.quantity) for event in filled])

    def test_sparse_unlimited_candidates_share_one_actual_entry_batch(self):
        symbols = [f"BUY{i:03d}" for i in range(5)]
        scanner_provider = StaticScannerProvider(
            bars={symbol: _bar(symbol=symbol, close="10000") for symbol in symbols},
            priorities={symbol: float(100 - index) for index, symbol in enumerate(symbols)},
        )
        final_bars = {}
        for symbol in symbols:
            source = _bar(symbol=symbol, close="10000")
            final_bars[symbol] = MarketBar(
                symbol=symbol,
                timestamp=source.timestamp,
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9900"),
                close=Decimal("10000"),
                volume=source.volume,
                vwap=source.vwap,
                bid=source.bid,
                ask=source.ask,
            )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "sparse_entry_batch")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("300000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(final_bars),
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            max_final_quote_requests_per_cycle=5,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(5, len(filled))
        self.assertTrue(all(event.quantity >= 1 for event in filled))
        self.assertEqual(100, sum(event.quantity for event in filled))
        self.assertEqual(Decimal("0"), runtime.broker.snapshot().cash)

    def test_unlimited_slot_target_ignores_final_quote_cap(self):
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{i:03d}" for i in range(10)],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider({}),
            symbol_directory=SymbolDirectory({f"BUY{i:03d}": f"Buy {i}" for i in range(10)}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            scan_limit_per_cycle=10,
            max_final_quote_requests_per_cycle=2,
        )

        self.assertEqual(10, runtime._unlimited_entry_slot_target())

    def test_local_entry_quantity_uses_slot_budget_not_order_cash_amount(self):
        runtime = PaperTradingRuntime(
            symbols=["BUY060"],
            broker=PaperBroker(initial_cash=Decimal("500000")),
            strategy=FixedSignalStrategy({"BUY060": [Signal.buy("BUY060", "slot_budget_quantity")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("200000"),
                    max_position_amount=Decimal("200000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({"BUY060": _bar(symbol="BUY060", close="60000")}),
            symbol_directory=SymbolDirectory({"BUY060": "Budget Quantity"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("10000"),
                max_symbol_exposure=Decimal("0.50"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY060", 3)], [(event.symbol, event.quantity) for event in filled])
        position = runtime.broker.snapshot().positions["BUY060"]
        self.assertEqual(3, position.quantity)

    def test_single_entry_can_use_available_cash_without_profile_allocation_ratio(self):
        runtime = PaperTradingRuntime(
            symbols=["BUY010"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY010": [Signal.buy("BUY010", "cash_ratio_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=10,
                )
            ),
            bar_provider=DictBarProvider({"BUY010": _bar(symbol="BUY010", close="10000")}),
            symbol_directory=SymbolDirectory({"BUY010": "Cash Ratio"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=10,
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY010", 100)], [(event.symbol, event.quantity) for event in filled])

    def test_entry_planner_reserves_one_share_for_each_selected_distinct_symbol(self):
        runtime = PaperTradingRuntime(
            symbols=["CHEAP1", "LARGE1"],
            broker=PaperBroker(initial_cash=Decimal("100000")),
            strategy=RankedSignalStrategy({"CHEAP1": 0.95, "LARGE1": 0.90}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("100000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "CHEAP1": _bar(symbol="CHEAP1", close="1000"),
                    "LARGE1": _bar(symbol="LARGE1", close="99000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                max_position_amount=Decimal("100000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(
            [("CHEAP1", 1), ("LARGE1", 1)],
            [(event.symbol, event.quantity) for event in filled],
        )
        self.assertEqual({"CHEAP1", "LARGE1"}, set(runtime.broker.snapshot().positions))

    def test_entry_planner_chooses_maximum_distinct_affordable_set_before_rank(self):
        symbols = ["HIGH01", "CHEAP1", "CHEAP2", "CHEAP3"]
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("100000")),
            strategy=RankedSignalStrategy(
                {"HIGH01": 0.99, "CHEAP1": 0.90, "CHEAP2": 0.80, "CHEAP3": 0.70}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("100000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="99000"),
                    "CHEAP1": _bar(symbol="CHEAP1", close="1000"),
                    "CHEAP2": _bar(symbol="CHEAP2", close="2000"),
                    "CHEAP3": _bar(symbol="CHEAP3", close="3000"),
                }
            ),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                max_position_amount=Decimal("100000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled_symbols = {
            event.symbol
            for event in events
            if event.kind == "trade" and event.result == "filled"
        }
        self.assertEqual({"CHEAP1", "CHEAP2", "CHEAP3"}, filled_symbols)
        self.assertNotIn("HIGH01", runtime.broker.snapshot().positions)

    def test_zero_max_order_amount_does_not_cap_cash_allocation_entry(self):
        runtime = PaperTradingRuntime(
            symbols=["BUY050"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({"BUY050": [Signal.buy("BUY050", "no_order_cap_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({"BUY050": _bar(symbol="BUY050", close="50000")}),
            symbol_directory=SymbolDirectory({"BUY050": "No Order Cap"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.90"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY050", 20)], [(event.symbol, event.quantity) for event in filled])

    def test_entry_budget_uses_current_cash_not_existing_position_exposure(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("HOLD01", 8, "seed"), _bar(symbol="HOLD01", close="100000"))
        runtime = PaperTradingRuntime(
            symbols=["BUY050"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY050": [Signal.buy("BUY050", "cash_based_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider({"BUY050": _bar(symbol="BUY050", close="50000")}),
            symbol_directory=SymbolDirectory({"HOLD01": "Held", "BUY050": "Cash Based"}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        budget = runtime._entry_budget_for_account(runtime.broker.snapshot())
        events = runtime.run_cycle()

        self.assertEqual(Decimal("200000.00"), budget.quantize(Decimal("0.01")))
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY050", 4)], [(event.symbol, event.quantity) for event in filled])

    def test_entry_quantity_recomputes_slot_budget_after_each_fill(self):
        runtime = PaperTradingRuntime(
            symbols=["FIRST1", "SECOND"],
            broker=PaperBroker(initial_cash=Decimal("500000")),
            strategy=RankedSignalStrategy({"FIRST1": 0.95, "SECOND": 0.90}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("1000000"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "FIRST1": _bar(symbol="FIRST1", close="60000"),
                    "SECOND": _bar(symbol="SECOND", close="40000"),
                }
            ),
            symbol_directory=SymbolDirectory({"FIRST1": "First", "SECOND": "Second"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=2,
                order_cash_amount=Decimal("10000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(
            [("FIRST1", 7), ("SECOND", 2)],
            [(event.symbol, event.quantity) for event in filled],
        )
        positions = runtime.broker.snapshot().positions
        self.assertEqual(7, positions["FIRST1"].quantity)
        self.assertEqual(2, positions["SECOND"].quantity)

    def test_replacement_entry_uses_post_exit_cash_and_remaining_slot_budget(self):
        symbols = ["EXIT01", "BUY002"]
        broker = PaperBroker(initial_cash=Decimal("500000"))
        broker.place_order(Order.buy("EXIT01", 5, "seed"), _bar(symbol="EXIT01", close="20000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="22000", offset=1),
                    "BUY002": _bar(symbol="BUY002", close="50000", offset=1),
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(
            [("EXIT01", "SELL", 5), ("BUY002", "BUY", 10)],
            [(event.symbol, event.side, event.quantity) for event in filled],
        )
        snapshot = runtime.broker.snapshot()
        self.assertEqual(Decimal("10000"), snapshot.cash)
        self.assertEqual(Decimal("510000"), snapshot.equity)
        self.assertEqual({"BUY002"}, set(snapshot.positions))

    def test_entry_budget_uses_short_reserved_free_cash_not_raw_cash(self):
        symbols = ["SHORT1", "BUY002"]
        broker = PaperBroker(initial_cash=Decimal("500000"), allow_short=True)
        broker.place_order(Order.short("SHORT1", 4, "seed"), _bar(symbol="SHORT1", close="50000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({"BUY002": [Signal.buy("BUY002", "replacement_entry")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "SHORT1": _bar(symbol="SHORT1", close="10000", offset=1),
                    "BUY002": _bar(symbol="BUY002", close="110000", offset=1),
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                allow_paper_short=True,
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("BUY002", "BUY", 2)], [(event.symbol, event.side, event.quantity) for event in filled])
        snapshot = runtime.broker.snapshot()
        self.assertEqual(Decimal("480000"), snapshot.cash)
        self.assertEqual(Decimal("80000"), snapshot.free_cash)
        self.assertEqual(Decimal("80000"), snapshot.buying_power)
        self.assertEqual(2, snapshot.positions["BUY002"].quantity)

    def test_short_cover_releases_buying_power_for_same_cycle_replacement(self):
        symbols = ["SHORT1", "BUY002"]
        broker = PaperBroker(initial_cash=Decimal("500000"), allow_short=True)
        broker.place_order(Order.short("SHORT1", 4, "seed"), _bar(symbol="SHORT1", close="50000"))
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "SHORT1": [Signal.cover("SHORT1", "take_profit")],
                    "BUY002": [Signal.buy("BUY002", "replacement_entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=2,
                )
            ),
            bar_provider=DictBarProvider(
                {
                    "SHORT1": _bar(symbol="SHORT1", close="60000", offset=1),
                    "BUY002": _bar(symbol="BUY002", close="50000", offset=1),
                }
            ),
            symbol_directory=SymbolDirectory({symbol: symbol for symbol in symbols}),
            settings=CustomStrategySettings.default().with_updates(
                allow_paper_short=True,
                cash_allocation_pct=Decimal("0.70"),
                max_positions=2,
                max_symbol_exposure=Decimal("1.0"),
            ),
            scan_limit_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(
            [("SHORT1", "SHORT_EXIT", 4), ("BUY002", "BUY", 9)],
            [(event.symbol, event.side, event.quantity) for event in filled],
        )
        snapshot = runtime.broker.snapshot()
        self.assertEqual(Decimal("10000"), snapshot.cash)
        self.assertEqual(Decimal("460000"), snapshot.equity)
        self.assertEqual(Decimal("10000"), snapshot.free_cash)
        self.assertEqual(Decimal("10000"), snapshot.buying_power)
        self.assertEqual(9, snapshot.positions["BUY002"].quantity)

    def test_kis_short_entry_uses_remaining_slot_budget_when_short_is_allowed(self):
        runtime = PaperTradingRuntime(
            symbols=["SHORT1"],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            strategy=FixedSignalStrategy({"SHORT1": [Signal.short("SHORT1", "slot_budget_short")]}),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("250000"),
                    max_position_amount=Decimal("250000"),
                    max_positions=5,
                )
            ),
            bar_provider=DictBarProvider({"SHORT1": _bar(symbol="SHORT1", close="120000")}),
            symbol_directory=SymbolDirectory({"SHORT1": "Short Budget"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=5,
                order_cash_amount=Decimal("50000"),
                allow_paper_short=True,
            ),
            data_source_kind="kis-vts",
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual([("SHORT1", "SHORT_ENTRY", 2)], [(event.symbol, event.side, event.quantity) for event in filled])
        position = runtime.broker.snapshot().positions["SHORT1"]
        self.assertEqual("SHORT", position.side)

    def test_cycle_skips_unaffordable_entry_candidate_and_fills_next_candidate(self):
        class ScoredFixedSignalStrategy(FixedSignalStrategy):
            def last_entry_score(self, symbol):
                score = {"HIGH01": 0.9, "BUY003": 0.1}.get(symbol, 0.0)
                return SignalScore(
                    symbol=symbol,
                    long_score=score,
                    short_score=0.0,
                    confidence=score,
                    direction="long",
                    reasons=("ranked_test",),
                )

        risk_manager = ApprovingRiskManager()
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY003"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=ScoredFixedSignalStrategy(
                {
                    "HIGH01": [Signal.buy("HIGH01", "flow_score_90")],
                    "BUY003": [Signal.buy("BUY003", "flow_score_10")],
                }
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider(
                {
                    "HIGH01": _bar(symbol="HIGH01", close="200000"),
                    "BUY003": _bar(symbol="BUY003", close="10000"),
                }
            ),
            symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY003": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(["BUY003"], [event.symbol for event in trade_events])
        self.assertEqual(["filled"], [event.result for event in trade_events])
        self.assertEqual(["BUY003"], risk_manager.checked_symbols)
        self.assertEqual({"BUY003"}, set(runtime.broker.snapshot().positions))
        self.assertTrue(any("entry_unaffordable" in event.message for event in events))

    def test_cycle_filters_daily_entry_limit_before_order_and_fills_next_candidate(self):
        class TrackingRiskManager(RiskManager):
            def __init__(self):
                super().__init__(RiskConfig(max_daily_entries_per_symbol=1, max_order_amount=Decimal("100000")))
                self.checked_symbols = []

            def check(self, order, account, estimated_price, as_of=None):
                self.checked_symbols.append(order.symbol)
                return super().check(order, account, estimated_price, as_of=as_of)

        risk_manager = TrackingRiskManager()
        risk_manager.record_entry("035720", _bar(symbol="035720").timestamp.date())
        runtime = PaperTradingRuntime(
            symbols=["035720", "028300"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "035720": [Signal.buy("035720", "flow_score_90")],
                    "028300": [Signal.buy("028300", "flow_score_10")],
                }
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider(
                {
                    "035720": _bar(symbol="035720", close="40000"),
                    "028300": _bar(symbol="028300", close="10000"),
                }
            ),
            symbol_directory=SymbolDirectory({"035720": "Kakao", "028300": "HLB"}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(["028300"], [event.symbol for event in trade_events])
        self.assertEqual(["filled"], [event.result for event in trade_events])
        self.assertEqual(["028300"], risk_manager.checked_symbols)
        self.assertTrue(any("max_daily_entries_reached" in event.message for event in events))
        self.assertFalse(any(event.reason == "max_daily_entries_reached" for event in trade_events))

    def test_kis_scan_cursor_skips_symbol_that_already_hit_daily_entry_limit(self):
        risk_manager = RiskManager(RiskConfig(max_daily_entries_per_symbol=1, max_order_amount=Decimal("100000")))
        risk_manager.record_entry("035720")
        bar_provider = DictBarProvider(
            {
                "035720": _bar(symbol="035720", close="40000"),
                "028300": _bar(symbol="028300", close="10000"),
            }
        )
        runtime = PaperTradingRuntime(
            symbols=["035720", "028300"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "035720": [Signal.buy("035720", "flow_score_90")],
                    "028300": [Signal.buy("028300", "flow_score_10")],
                }
            ),
            risk_manager=risk_manager,
            bar_provider=bar_provider,
            symbol_directory=SymbolDirectory({"035720": "Kakao", "028300": "HLB"}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            scan_limit_per_cycle=1,
            max_bar_requests_per_cycle=1,
            data_source_kind="kis-vts",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["028300"], bar_provider.requested_symbols)
        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["028300"], [event.symbol for event in filled])

    def test_cycle_top_up_scans_after_unaffordable_candidate_when_scan_limit_is_small(self):
        scanner_provider = StaticScannerProvider(
            bars={
                "HIGH01": _bar(symbol="HIGH01", close="200000"),
                "BUY003": _bar(symbol="BUY003", close="10000"),
            },
            priorities={"HIGH01": 100.0, "BUY003": 90.0},
        )
        runtime = PaperTradingRuntime(
            symbols=["HIGH01", "BUY003"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    "HIGH01": [Signal.buy("HIGH01", "flow_score_90")],
                    "BUY003": [Signal.buy("BUY003", "flow_score_10")],
                }
            ),
            risk_manager=ApprovingRiskManager(),
            bar_provider=DictBarProvider({}),
            scanner_provider=scanner_provider,
            symbol_directory=SymbolDirectory({"HIGH01": "High Price", "BUY003": "Affordable"}),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=1,
                order_cash_amount=Decimal("50000"),
                max_symbol_exposure=Decimal("0.05"),
            ),
            scan_limit_per_cycle=1,
            data_source_kind="external-scan-kis",
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["BUY003"], [event.symbol for event in filled])
        self.assertEqual(["BUY003"], runtime.strategy.seen_symbols)
        self.assertEqual({"BUY003"}, set(runtime.broker.snapshot().positions))

    def test_cycle_keeps_open_position_when_price_is_between_exit_zones(self):
        symbol = "005930"
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy(symbol, 10, "seed"), _bar(symbol=symbol, close="10000", offset=0))
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FlowScalperStrategy(FlowScalperConfig()),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DictBarProvider({symbol: _bar(symbol=symbol, close="10050", offset=30)}),
            symbol_directory=SymbolDirectory({symbol: "삼성전자"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertIn(symbol, runtime.broker.snapshot().positions)
        self.assertFalse(any(event.kind == "trade" and event.side == "SELL" for event in events))

    def test_cycle_logs_hold_score_reasons_when_strategy_exposes_score(self):
        score = SignalScore(
            symbol="005930",
            long_score=0.4,
            short_score=0.1,
            confidence=0.4,
            direction="hold",
            reasons=("volume_below_minimum", "spread_allowed"),
        )
        runtime = make_runtime(strategy=HoldScoreStrategy(score))
        runtime.start()

        events = runtime.run_cycle()

        self.assertTrue(any("volume_below_minimum" in event.message for event in events))
        self.assertTrue(any("0.40" in event.message for event in events))

    def test_cycle_summarizes_bulk_hold_reasons(self):
        symbols = [f"BUY{index:03d}" for index in range(51)]
        score = SignalScore(
            symbol="BUY000",
            long_score=0.2,
            short_score=0.0,
            confidence=0.2,
            direction="hold",
            reasons=("volume_below_minimum",),
        )
        runtime = make_runtime(symbols=symbols, strategy=HoldScoreStrategy(score))
        runtime.start()

        events = runtime.run_cycle()

        summaries = [event.message for event in events if "scanner_hold_summary" in event.message]
        self.assertTrue(summaries)
        self.assertIn("volume_below_minimum=51", summaries[-1])

    def test_cycle_labels_insufficient_data_as_warmup(self):
        score = SignalScore(
            symbol="005930",
            long_score=0.0,
            short_score=0.0,
            confidence=0.0,
            direction="hold",
            reasons=("insufficient_data",),
        )
        runtime = make_runtime(strategy=HoldScoreStrategy(score))
        runtime.start()

        events = runtime.run_cycle()

        self.assertTrue(any("데이터 누적 중" in event.message for event in events))
        self.assertTrue(any("insufficient_data" in event.message for event in events))

    def test_cycle_skips_market_data_when_rate_limited(self):
        limiter = BlockingRateLimiter()
        runtime = make_runtime(rate_limiter=limiter)
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual([], limiter.recorded)
        self.assertEqual(1, runtime.cycle_count)
        self.assertTrue(any("token_cooldown" in event.message for event in events))
        self.assertTrue(any("12.5" in event.message for event in events))

    def test_cycle_waits_outside_regular_market_hours_without_fetching_bars(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))
        runtime = make_runtime(
            signals={"005930": [Signal.buy("005930", "flow_breakout")]},
            market_hours=hours,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertEqual("장 대기", runtime.status.label)
        self.assertEqual(1, runtime.cycle_count)
        self.assertTrue(any("정규장" in event.message for event in events))

    def test_cycle_waits_outside_market_hours_before_live_account_snapshot(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))
        broker = SnapshotShouldNotRunBroker()
        runtime = make_runtime(
            signals={"005930": [Signal.buy("005930", "flow_breakout")]},
            broker=broker,
            market_hours=hours,
        )
        runtime.execution_mode = "live"
        runtime.start()
        broker.snapshot_calls = 0

        events = runtime.run_cycle()

        self.assertEqual(0, broker.snapshot_calls)
        self.assertEqual([], broker.updated_symbols)
        self.assertEqual([], broker.orders)
        self.assertEqual([], runtime.bar_provider.requested_symbols)
        self.assertEqual("장 대기", runtime.status.label)
        self.assertEqual(1, runtime.cycle_count)
        self.assertTrue(any("정규장" in event.message for event in events))

    def test_cycle_runs_inside_regular_market_hours(self):
        hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 10, 0, tzinfo=KST))
        runtime = make_runtime(
            signals={"005930": [Signal.buy("005930", "flow_breakout")]},
            market_hours=hours,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(["005930"], runtime.bar_provider.requested_symbols)
        self.assertIn("005930", runtime.broker.snapshot().positions)
        self.assertEqual("실행 중", runtime.status.label)
        self.assertTrue(any(event.kind == "trade" for event in events))

    def test_cycle_emits_filled_trade_event_for_paper_order_with_company_name(self):
        runtime = make_runtime(signals={"005930": [Signal.buy("005930", "flow_breakout")]})
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(1, len(trade_events))
        self.assertEqual("삼성전자", trade_events[0].company_name)
        self.assertEqual("BUY", trade_events[0].side)
        self.assertEqual("filled", trade_events[0].result)
        self.assertEqual("paper", trade_events[0].mode)
        self.assertEqual(1, runtime.performance_metrics.filled_trades)

    def test_cycle_emits_rejected_trade_event_when_risk_blocks_order(self):
        class RejectingRiskManager:
            config = RiskConfig(max_order_amount=Decimal("100000"), max_position_amount=Decimal("300000"))

            def check(self, order, account, estimated_price, as_of=None):
                class Decision:
                    approved = False
                    reason = "manual_risk_block"

                return Decision()

            def record_order_result(self, accepted):
                pass

        runtime = make_runtime(
            signals={"005930": [Signal.buy("005930", "flow_breakout")]},
            risk_manager=RejectingRiskManager(),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual("rejected", trade_events[0].result)
        self.assertEqual("manual_risk_block", trade_events[0].reason)
        self.assertEqual(1, runtime.performance_metrics.rejected_trades)

    def test_cycle_emits_rejected_trade_event_when_broker_rejects_order(self):
        runtime = make_runtime(
            signals={"005930": [Signal.short("005930", "downtrend_short")]},
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=False),
            risk_manager=ApprovingRiskManager(),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual("rejected", trade_events[0].result)
        self.assertEqual("paper_short_disabled", trade_events[0].reason)

    def test_cycle_rejects_disabled_short_before_final_quote_lookup(self):
        scanner = DictBarProvider({"SHORT1": _bar(symbol="SHORT1", close="10000")})
        final_quotes = DictBarProvider({"SHORT1": _bar(symbol="SHORT1", close="9000")})
        runtime = PaperTradingRuntime(
            symbols=["SHORT1"],
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=False),
            strategy=FixedSignalStrategy({"SHORT1": [Signal.short("SHORT1", "downtrend_short")]}),
            risk_manager=ApprovingRiskManager(),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"SHORT1": "Short One"}),
            settings=CustomStrategySettings.default().with_updates(allow_paper_short=False),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual("rejected", trade_events[0].result)
        self.assertEqual("paper_short_disabled", trade_events[0].reason)

    def test_cycle_fills_short_order_through_normal_risk_path_when_paper_short_enabled(self):
        runtime = make_runtime_with_settings(
            settings=CustomStrategySettings.default().with_updates(allow_paper_short=True),
            signals={"005930": [Signal.short("005930", "downtrend_short")]},
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual("filled", trade_events[0].result)
        self.assertEqual("SHORT_ENTRY", trade_events[0].side)
        self.assertIn("005930", runtime.broker.snapshot().positions)

    def test_cost_filtered_entry_does_not_reach_risk_but_next_valid_candidate_fills(self):
        def bar(symbol, offset, close, *, volume=1000, vwap=None):
            price = Decimal(str(close))
            return MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 6, 11, 9, 0) + timedelta(minutes=offset),
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
                vwap=Decimal(str(vwap if vwap is not None else close)),
                bid=price,
                ask=price,
            )

        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=3,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0.010"),
                commission_pct=Decimal("0.001"),
                slippage_pct=Decimal("0.001"),
                min_net_profit_pct=Decimal("0.005"),
            )
        )
        risk_manager = ApprovingRiskManager()
        runtime = PaperTradingRuntime(
            symbols=["BLOCK1", "VALID1"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=risk_manager,
            bar_provider=SequenceBarProvider(
                {
                    "BLOCK1": [
                        bar("BLOCK1", 0, 100),
                        bar("BLOCK1", 1, 102),
                        bar("BLOCK1", 2, 104),
                        bar("BLOCK1", 3, 106, volume=3000, vwap=105),
                    ],
                    "VALID1": [
                        bar("VALID1", 0, 100),
                        bar("VALID1", 1, 108),
                        bar("VALID1", 2, 104),
                        bar("VALID1", 3, 112, volume=3000, vwap=111),
                    ],
                }
            ),
            symbol_directory=SymbolDirectory({"BLOCK1": "Blocked", "VALID1": "Valid"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        for _ in range(3):
            runtime.run_cycle()
        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        blocked_score = strategy.last_entry_score("BLOCK1")
        self.assertEqual(["VALID1"], [event.symbol for event in trade_events])
        self.assertEqual(["filled"], [event.result for event in trade_events])
        self.assertEqual(["VALID1"], risk_manager.checked_symbols)
        self.assertIsNotNone(blocked_score)
        self.assertIn("expected_net_profit_below_costs", blocked_score.reasons)
        self.assertNotIn("BLOCK1", runtime.broker.snapshot().positions)
        self.assertIn("VALID1", runtime.broker.snapshot().positions)

    def test_cycle_records_realized_pnl_and_metrics_for_exit_fill(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("005930", 2, "seed"), _bar(close="10000"))
        runtime = make_runtime(
            bars={"005930": _bar(close="11000", offset=1)},
            signals={"005930": [Signal.sell("005930", "take_profit")]},
            broker=broker,
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(Decimal("2000"), trade_events[0].realized_pnl)
        self.assertEqual(Decimal("2000"), runtime.performance_metrics.realized_pnl)
        self.assertEqual(Decimal("100"), runtime.performance_metrics.win_rate_pct)

    def test_cycle_rejects_short_when_runtime_paper_short_setting_is_off(self):
        runtime = make_runtime(
            signals={"005930": [Signal.short("005930", "downtrend_short")]},
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual("rejected", trade_events[0].result)
        self.assertEqual("paper_short_disabled", trade_events[0].reason)
        self.assertEqual({}, runtime.broker.snapshot().positions)

    def test_policy_rejections_do_not_lock_out_later_valid_entries_in_same_cycle(self):
        class ScoredFixedSignalStrategy(FixedSignalStrategy):
            def __init__(self, signals, scores):
                super().__init__(signals)
                self.scores = scores

            def last_entry_score(self, symbol):
                score = self.scores[symbol]
                return SignalScore(
                    symbol=symbol,
                    long_score=score,
                    short_score=score,
                    confidence=score,
                    direction="long",
                    reasons=("test_score",),
                )

        symbols = ["000001", "000002", "005930"]
        runtime = make_runtime(
            symbols=symbols,
            strategy=ScoredFixedSignalStrategy(
                {
                    "000001": [Signal.short("000001", "short_disabled")],
                    "000002": [Signal.short("000002", "short_disabled")],
                    "005930": [Signal.buy("005930", "flow_breakout")],
                },
                {"000001": 0.9, "000002": 0.8, "005930": 0.1},
            ),
            broker=PaperBroker(initial_cash=Decimal("1000000"), allow_short=True),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_consecutive_order_failures=2)),
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual(["rejected", "rejected", "filled"], [event.result for event in trade_events])
        self.assertEqual(
            ["paper_short_disabled", "paper_short_disabled", "flow_breakout"],
            [event.reason for event in trade_events],
        )
        self.assertIn("005930", runtime.broker.snapshot().positions)

    def test_cycle_cleanup_mode_exits_positions_but_blocks_new_entries(self):
        settings = CustomStrategySettings.default().with_updates(kill_switch=True)
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 2, "seed"), _bar("EXIT01", "10000"))
        runtime = PaperTradingRuntime(
            symbols=["EXIT01", "BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "take_profit")],
                    "BUY001": [Signal.buy("BUY001", "flow_breakout")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=DictBarProvider({"EXIT01": _bar("EXIT01", "11000"), "BUY001": _bar("BUY001", "10000")}),
            symbol_directory=SymbolDirectory({"EXIT01": "Exit Co", "BUY001": "Buy Co"}),
            settings=settings,
        )
        runtime.start()

        events = runtime.run_cycle()

        trade_events = [event for event in events if event.kind == "trade"]
        self.assertEqual([("SELL", "filled")], [(event.side, event.result) for event in trade_events])
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertEqual(["EXIT01"], runtime.bar_provider.requested_symbols)
        self.assertTrue(any("정리 모드" in event.message for event in events))

    def test_cycle_cleanup_mode_allows_flat_final_quote_exit(self):
        settings = CustomStrategySettings.default().with_updates(kill_switch=True)
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("EXIT01", 2, "seed"), _bar("EXIT01", "10000"))
        scanner = DictBarProvider({"EXIT01": _bar("EXIT01", "9400")})
        final_quotes = DictBarProvider({"EXIT01": _bar("EXIT01", "10000")})
        runtime = PaperTradingRuntime(
            symbols=["EXIT01"],
            broker=broker,
            strategy=FixedSignalStrategy({"EXIT01": [Signal.sell("EXIT01", "stop_loss")]}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"), max_positions=5)),
            bar_provider=scanner,
            final_quote_provider=final_quotes,
            symbol_directory=SymbolDirectory({"EXIT01": "Exit Co"}),
            settings=settings,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(["EXIT01"], final_quotes.requested_symbols)
        self.assertEqual([("SELL", Decimal("10000"))], [(event.side, event.price) for event in filled])
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertFalse(any("flat_final_quote_exit" in event.message for event in events if event.kind == "system"))

    def test_cycle_logs_bar_provider_errors_without_leaking_exception_values(self):
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=RaisingBarProvider(),
            symbol_directory=SymbolDirectory({"005930": "삼성전자"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, len(events))
        self.assertEqual("system", events[0].kind)
        self.assertIn("데이터 조회 실패", events[0].message)
        self.assertNotIn("secret-token-123", events[0].message)

    def test_cycle_logs_sanitized_bar_provider_error_summary_for_diagnostics(self):
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=DiagnosticBarProvider(
                "KIS HTTP 500: EGW00201 초당 거래건수를 초과하였습니다 appsecret=secret-token-123"
            ),
            symbol_directory=SymbolDirectory({"005930": "삼성전자"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, len(events))
        self.assertEqual("system", events[0].kind)
        self.assertIn("데이터 조회 실패", events[0].message)
        self.assertIn("KIS HTTP 500", events[0].message)
        self.assertIn("EGW00201", events[0].message)
        self.assertNotIn("secret-token-123", events[0].message)
        self.assertNotIn("appsecret=", events[0].message)

    def test_market_data_error_message_redacts_sensitive_diagnostic_values(self):
        cases = [
            (
                RuntimeError("KIS HTTP 401 authorization: Bearer abc.def.ghi"),
                ["abc.def.ghi", "Bearer"],
                ["KIS HTTP 401"],
            ),
            (
                RuntimeError(r"failed at C:\Users\example-user\Documents\StockProject\.env"),
                [r"C:\Users", r"\Documents\StockProject", ".env"],
                ["failed at"],
            ),
            (
                RuntimeError("KIS_VTS_APP_KEY=PS1234567890 appsecret=secret-token-123"),
                ["KIS_VTS_APP_KEY", "PS1234567890", "appsecret=", "secret-token-123"],
                [],
            ),
            (
                RuntimeError("account 12345678-01 returned EGW00201"),
                ["12345678-01"],
                ["EGW00201"],
            ),
        ]

        for exc, hidden_values, expected_values in cases:
            with self.subTest(message=str(exc)):
                message = _market_data_error_message(exc)
                self.assertIn("데이터 조회 실패", message)
                for value in hidden_values:
                    self.assertNotIn(value, message)
                for value in expected_values:
                    self.assertIn(value, message)

    def test_cycle_logs_strategy_errors_and_finishes_cycle_without_leaking_exception_values(self):
        runtime = make_runtime(strategy=RaisingStrategy())
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, runtime.cycle_count)
        self.assertEqual("system", events[0].kind)
        self.assertIn("전략 처리 실패", events[0].message)
        self.assertNotIn("secret-token-123", events[0].message)

    def test_cycle_does_not_call_kis_order_api_even_if_injected_object_has_one(self):
        class ExplodingKisLikeObject:
            def place_cash_order(self, *args, **kwargs):
                raise AssertionError("KIS order API must not be called by paper runtime")

        runtime = make_runtime()
        runtime.kis_client = ExplodingKisLikeObject()
        runtime.start()

        runtime.run_cycle()

    def test_authoritative_scanner_refreshes_full_candidate_universe_each_cycle(self):
        class RotatingScanner:
            label = "rotating scanner"
            kind = "test"

            def __init__(self):
                self.rank_calls = 0

            def rank_symbols(self, symbols):
                if symbols:
                    return list(symbols)
                universes = (["BUY001"], ["BUY002"])
                selected = universes[min(self.rank_calls, len(universes) - 1)]
                self.rank_calls += 1
                return list(selected)

            def snapshot(self, symbols):
                bars = {
                    symbol: replace(
                        _bar(symbol=symbol),
                        open=Decimal("9900"),
                        high=Decimal("10100"),
                        low=Decimal("9900"),
                    )
                    for symbol in symbols
                }
                return ScannerSnapshot(
                    bars=bars,
                    candidates=tuple(ScannerCandidate(symbol=symbol, priority=100.0) for symbol in symbols),
                )

        class BuyUnheldStrategy(FixedSignalStrategy):
            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol in account.positions:
                    return []
                return [Signal.buy(bar.symbol, "dynamic_universe")]

        provider = RotatingScanner()
        runtime = PaperTradingRuntime(
            symbols=["STALE0"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=BuyUnheldStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(
                {symbol: _bar(symbol=symbol) for symbol in ("BUY001", "BUY002")}
            ),
            scanner_provider=provider,
            settings=CustomStrategySettings.default().with_updates(max_positions=2),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(2, provider.rank_calls)
        self.assertEqual(["BUY002"], runtime.symbols)
        self.assertEqual({"BUY001", "BUY002"}, set(runtime.broker.snapshot().positions))

    def test_unlimited_live_style_scan_filters_by_slot_budget_before_final_quote_cap(self):
        symbols = ["EXPENS", "BUY001", "BUY002"]

        def scanner_bar(symbol, price):
            value = Decimal(price)
            return replace(
                _bar(symbol=symbol, close=price),
                open=value - Decimal("100"),
                high=value + Decimal("100"),
                low=value - Decimal("100"),
            )

        scanner_bars = {
            "EXPENS": scanner_bar("EXPENS", "500000"),
            "BUY001": scanner_bar("BUY001", "300000"),
            "BUY002": scanner_bar("BUY002", "300000"),
        }
        final_quotes = DictBarProvider(
            {symbol: _bar(symbol=symbol, close="300000") for symbol in ("BUY001", "BUY002")}
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "slot_budget")] for symbol in symbols}
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=0, max_position_amount=Decimal("300000"))),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                priorities={"EXPENS": 100.0, "BUY001": 90.0, "BUY002": 80.0},
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_position_amount=Decimal("300000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="external-scan-kis",
            scan_limit_per_cycle=3,
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BUY001", "BUY002"], final_quotes.requested_symbols)
        self.assertEqual({"BUY001", "BUY002"}, set(runtime.broker.snapshot().positions))

    def test_live_flow_strategy_does_not_synthesize_missing_entry_history(self):
        symbol = "BUY001"
        scanner_bar = replace(
            _bar(symbol=symbol),
            open=Decimal("9900"),
            high=Decimal("10100"),
            low=Decimal("9900"),
            volume=5000,
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="10100", offset=1)})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FlowScalperStrategy(
                FlowScalperConfig(
                    momentum_window=3,
                    volume_window=3,
                    min_momentum_pct=Decimal("0"),
                    min_volume_ratio=Decimal("0"),
                    min_trend_pct=Decimal("0"),
                    require_vwap_alignment=False,
                    transaction_tax_pct=Decimal("0"),
                    slippage_pct=Decimal("0"),
                    min_net_profit_pct=Decimal("0"),
                )
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertTrue(
            any(
                "stale_completed_minute_history" in event.message
                for event in events
            )
        )

    def test_live_runtime_uses_live_strategy_path_without_changing_paper_path(self):
        class LiveAwareStrategy(FixedSignalStrategy):
            def __init__(self):
                super().__init__({})
                self.live_bars = []

            def on_live_bar(self, bar, account):
                self.live_bars.append(bar)
                return []

        bar = _bar(symbol="BUY001")
        live_strategy = LiveAwareStrategy()
        live_runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=live_strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({"BUY001": bar}),
            execution_mode="live",
        )
        paper_strategy = LiveAwareStrategy()
        paper_runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=paper_strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({"BUY001": bar}),
            execution_mode="paper",
        )
        account = AccountSnapshot(cash=Decimal("1000000"))

        live_runtime._strategy_signals_for_bar(bar, account)
        paper_runtime._strategy_signals_for_bar(bar, account)

        self.assertEqual([bar], live_strategy.live_bars)
        self.assertEqual([], live_strategy.seen_symbols)
        self.assertEqual([], paper_strategy.live_bars)
        self.assertEqual(["BUY001"], paper_strategy.seen_symbols)

    def test_live_history_seed_uses_completed_unique_minutes_in_timestamp_order(self):
        class RecordingSeedStrategy(WarmupOnlyStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__()
                self.seeded_bars = []

            def seed_history(self, symbol, bars):
                self.seeded_bars = list(bars)
                return len(self.seeded_bars)

        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol, close="10400"),
            timestamp=datetime(2026, 6, 11, 9, 3, 30),
        )
        minute_1 = replace(
            _bar(symbol=symbol, close="10100"),
            timestamp=datetime(2026, 6, 11, 9, 1, 30),
        )
        minute_2_early = replace(
            _bar(symbol=symbol, close="10150"),
            timestamp=datetime(2026, 6, 11, 9, 2, 10),
        )
        minute_2_latest = replace(
            _bar(symbol=symbol, close="10200"),
            timestamp=datetime(2026, 6, 11, 9, 2, 50),
        )
        current_minute = replace(
            _bar(symbol=symbol, close="10300"),
            timestamp=datetime(2026, 6, 11, 9, 3),
        )
        future = replace(
            _bar(symbol=symbol, close="10500"),
            timestamp=datetime(2026, 6, 11, 9, 4),
        )
        previous_day = replace(
            _bar(symbol=symbol, close="9900"),
            timestamp=datetime(2026, 6, 10, 15, 29),
        )
        strategy = RecordingSeedStrategy()
        requested_symbols = []

        def entry_history_provider(requested_symbol):
            requested_symbols.append(requested_symbol)
            return [
                current_minute,
                minute_2_latest,
                future,
                previous_day,
                minute_1,
                minute_2_early,
            ]

        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: current}),
            entry_history_provider=entry_history_provider,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            data_source_kind="live",
            execution_mode="live",
        )

        runtime._prewarm_entry_history(symbol, current)

        self.assertEqual([symbol], requested_symbols)
        self.assertEqual([minute_1, minute_2_latest], strategy.seeded_bars)
        self.assertEqual(2, runtime._successful_bar_samples[symbol])

    def test_live_history_prefers_complete_external_scanner_minutes_without_kis_history_read(self):
        class RecordingSeedStrategy(WarmupOnlyStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__()
                self.seeded_bars = []

            def seed_history(self, symbol, bars):
                self.seeded_bars = list(bars)
                return len(self.seeded_bars)

        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol, close="10400"),
            timestamp=datetime(2026, 6, 11, 9, 5, 30),
        )
        completed = [
            replace(
                _bar(symbol=symbol, close=str(10100 + (index * 100))),
                timestamp=datetime(2026, 6, 11, 9, 1 + index, 30),
            )
            for index in range(4)
        ]
        scanner = StaticScannerProvider(
            bars={symbol: current},
            histories={symbol: completed},
        )
        strategy = RecordingSeedStrategy()
        history_calls = []
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: current}),
            entry_history_provider=lambda requested: history_calls.append(requested),
            scanner_provider=scanner,
            data_source_kind="live",
            execution_mode="live",
        )
        runtime._scanner_snapshot = scanner.snapshot([symbol])

        runtime._prewarm_entry_history(symbol, current)

        self.assertEqual([], history_calls)
        self.assertEqual(completed, strategy.seeded_bars)
        self.assertEqual(4, runtime._successful_bar_samples[symbol])
        self.assertTrue(runtime._scanner_history_is_ready_for_bar(symbol, current))

    def test_live_scanner_history_ready_candidate_precedes_budgeted_kis_history_fallbacks(self):
        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.limit = None

        class RecordingSeedStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 3
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__({})

            def seed_history(self, symbol, bars):
                return len(list(bars))

        symbols = ["FALL001", "FALL002", "FALL003", "READY04", "NEXT005"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000", offset=4),
                timestamp=datetime(2026, 6, 11, 9, 4, 20),
                open=Decimal("9990"),
                high=Decimal("10010"),
                low=Decimal("9990"),
            )
            for symbol in symbols
        }
        completed_history = {
            symbol: [
                replace(scanner_bars[symbol], timestamp=datetime(2026, 6, 11, 9, minute))
                for minute in (1, 2, 3)
            ]
            for symbol in symbols
        }
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        budget_client = BudgetClient()
        broker.client = budget_client
        history_calls = []

        def entry_history_provider(symbol):
            history_calls.append(symbol)
            budget_client.consume(1)
            return completed_history[symbol]

        strategy = RecordingSeedStrategy()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=5)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(scanner_bars),
            entry_history_provider=entry_history_provider,
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                histories={"READY04": completed_history["READY04"]},
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=4,
            max_final_quote_requests_per_cycle=4,
            max_physical_market_reads_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual("READY04", strategy.seen_symbols[0])
        self.assertEqual(["FALL001", "FALL002"], history_calls)
        self.assertEqual("FALL003", runtime._scan_cursor_anchor)
        diagnostic = next(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        )
        self.assertIn("history_ready_candidates=1", diagnostic)
        self.assertIn("history_fallback_candidates=3", diagnostic)

    def test_live_scanner_history_priority_preserves_cursor_after_full_window(self):
        class RecordingSeedStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 3
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__({})

            def seed_history(self, symbol, bars):
                return len(list(bars))

        symbols = ["FALL001", "FALL002", "READY03"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000", offset=4),
                timestamp=datetime(2026, 6, 11, 9, 4, 20),
                open=Decimal("9990"),
                high=Decimal("10010"),
                low=Decimal("9990"),
            )
            for symbol in symbols
        }
        histories = {
            symbol: [
                replace(scanner_bars[symbol], timestamp=datetime(2026, 6, 11, 9, minute))
                for minute in (1, 2, 3)
            ]
            for symbol in symbols
        }
        strategy = RecordingSeedStrategy()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=4)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(scanner_bars),
            entry_history_provider=lambda symbol: histories[symbol],
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                histories={"READY03": histories["READY03"]},
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=4),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=3,
            max_final_quote_requests_per_cycle=3,
        )
        runtime._scan_cursor_anchor = "FALL001"
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["READY03", "FALL001", "FALL002"], strategy.seen_symbols)
        self.assertEqual("FALL001", runtime._scan_cursor_anchor)

    def test_live_scanner_history_priority_advances_cursor_after_partial_window(self):
        class RecordingSeedStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 3
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__({})

            def seed_history(self, symbol, bars):
                return len(list(bars))

        symbols = ["FALL001", "FALL002", "READY03", "NEXT004", "NEXT005"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000", offset=4),
                timestamp=datetime(2026, 6, 11, 9, 4, 20),
                open=Decimal("9990"),
                high=Decimal("10010"),
                low=Decimal("9990"),
            )
            for symbol in symbols
        }
        histories = {
            symbol: [
                replace(scanner_bars[symbol], timestamp=datetime(2026, 6, 11, 9, minute))
                for minute in (1, 2, 3)
            ]
            for symbol in symbols
        }
        strategy = RecordingSeedStrategy()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=5)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(scanner_bars),
            entry_history_provider=lambda symbol: histories[symbol],
            scanner_provider=StaticScannerProvider(
                bars=scanner_bars,
                histories={"READY03": histories["READY03"]},
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=5),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=3,
            max_bar_requests_per_cycle=3,
            max_final_quote_requests_per_cycle=3,
        )
        runtime._scan_cursor_anchor = "FALL001"
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["READY03", "FALL001", "FALL002"], strategy.seen_symbols)
        self.assertEqual("NEXT004", runtime._scan_cursor_anchor)

    def test_live_history_diagnostic_aggregates_sanitized_fallback_failures(self):
        class RecordingSeedStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 3
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__({})

            def seed_history(self, symbol, bars):
                return len(list(bars))

        symbols = ["COUNT01", "LATEST2", "GAP0003", "ERROR04"]
        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000", offset=4),
                timestamp=datetime(2026, 6, 11, 9, 4, 20),
                open=Decimal("9990"),
                high=Decimal("10010"),
                low=Decimal("9990"),
            )
            for symbol in symbols
        }

        def history_bars(symbol, minutes):
            return [
                replace(scanner_bars[symbol], timestamp=datetime(2026, 6, 11, 9, minute))
                for minute in minutes
            ]

        def entry_history_provider(symbol):
            if symbol == "COUNT01":
                return history_bars(symbol, (3,))
            if symbol == "LATEST2":
                return history_bars(symbol, (0, 1, 2))
            if symbol == "GAP0003":
                return history_bars(symbol, (0, 2, 3))
            raise RuntimeError("sensitive provider detail")

        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=RecordingSeedStrategy(),
            risk_manager=RiskManager(RiskConfig(max_positions=4)),
            bar_provider=DictBarProvider({}),
            entry_history_provider=entry_history_provider,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            settings=CustomStrategySettings.default().with_updates(max_positions=4),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=4,
        )
        runtime.start()

        events = runtime.run_cycle()

        diagnostic = next(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        )
        self.assertIn("history_ready_candidates=0", diagnostic)
        self.assertIn("history_fallback_candidates=4", diagnostic)
        self.assertIn("insufficient_count:1", diagnostic)
        self.assertIn("latest_mismatch:1", diagnostic)
        self.assertIn("gap:1", diagnostic)
        self.assertIn("provider_exception:1", diagnostic)
        self.assertNotIn("sensitive provider detail", diagnostic)

    def test_live_history_refreshes_once_per_symbol_minute_and_quotes_do_not_count_as_history(self):
        class RecordingSeedStrategy(WarmupOnlyStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__()
                self.seeded_batches = []

            def seed_history(self, symbol, bars):
                batch = list(bars)
                self.seeded_batches.append(batch)
                return len(batch)

        symbol = "BUY001"
        first_current = replace(
            _bar(symbol=symbol),
            timestamp=datetime(2026, 6, 11, 9, 3, 5),
        )
        same_minute = replace(
            first_current,
            timestamp=datetime(2026, 6, 11, 9, 3, 50),
        )
        next_minute = replace(
            first_current,
            timestamp=datetime(2026, 6, 11, 9, 4, 5),
        )
        first_history = [
            replace(first_current, timestamp=datetime(2026, 6, 11, 9, minute))
            for minute in range(3)
        ]
        second_history = [
            replace(first_current, timestamp=datetime(2026, 6, 11, 9, minute))
            for minute in range(1, 4)
        ]
        requested_symbols = []

        def entry_history_provider(requested_symbol):
            requested_symbols.append(requested_symbol)
            return first_history if len(requested_symbols) == 1 else second_history

        strategy = RecordingSeedStrategy()
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: first_current}),
            entry_history_provider=entry_history_provider,
            scanner_provider=StaticScannerProvider(bars={symbol: first_current}),
            data_source_kind="live",
            execution_mode="live",
        )

        runtime._prewarm_entry_history(symbol, first_current)
        runtime._record_successful_bar_sample(symbol, first_current)
        runtime._prewarm_entry_history(symbol, same_minute)
        runtime._prewarm_entry_history(symbol, next_minute)

        self.assertEqual([symbol, symbol], requested_symbols)
        self.assertEqual([first_history, second_history], strategy.seeded_batches)
        self.assertEqual(3, runtime._successful_bar_samples[symbol])

    def test_live_history_gap_is_seeded_for_diagnostics_but_not_marked_ready(self):
        class RecordingSeedStrategy(WarmupOnlyStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def __init__(self):
                super().__init__()
                self.seeded_bars = []

            def seed_history(self, symbol, bars):
                self.seeded_bars = list(bars)
                return len(self.seeded_bars)

        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol),
            timestamp=datetime(2026, 6, 11, 9, 4, 5),
        )
        completed_with_gap = [
            replace(current, timestamp=datetime(2026, 6, 11, 9, minute))
            for minute in (0, 2, 3)
        ]
        strategy = RecordingSeedStrategy()
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: current}),
            entry_history_provider=lambda _symbol: completed_with_gap,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            data_source_kind="live",
            execution_mode="live",
        )

        runtime._prewarm_entry_history(symbol, current)
        issue = runtime._entry_scan_issue(
            symbol,
            current,
            AccountSnapshot(cash=Decimal("1000000")),
        )

        self.assertEqual(completed_with_gap, strategy.seeded_bars)
        self.assertNotIn(symbol, runtime._live_history_ready_buckets)
        self.assertEqual("completed_minute_history_not_ready", issue)

    def test_live_history_clears_previous_day_before_failed_refresh(self):
        symbol = "BUY001"
        first_current = replace(
            _bar(symbol=symbol),
            timestamp=datetime(2026, 6, 11, 9, 3, 5),
        )
        next_day = replace(
            first_current,
            timestamp=datetime(2026, 6, 12, 9, 3, 5),
        )
        first_history = [
            replace(first_current, timestamp=datetime(2026, 6, 11, 9, minute))
            for minute in range(3)
        ]
        calls = 0

        def entry_history_provider(_requested_symbol):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first_history
            raise RuntimeError("history unavailable")

        strategy = FlowScalperStrategy(FlowScalperConfig())
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: first_current}),
            entry_history_provider=entry_history_provider,
            scanner_provider=StaticScannerProvider(bars={symbol: first_current}),
            data_source_kind="live",
            execution_mode="live",
        )

        runtime._prewarm_entry_history(symbol, first_current)
        runtime._prewarm_entry_history(symbol, next_day)
        runtime._prewarm_entry_history(symbol, next_day)

        self.assertEqual(2, calls)
        self.assertEqual([], strategy._history[symbol])
        self.assertEqual(0, runtime._successful_bar_samples[symbol])

    def test_live_history_failure_does_not_block_quote_based_stop_loss(self):
        symbol = "BUY001"
        quote = replace(
            _bar(symbol=symbol, close="9700"),
            timestamp=datetime(2026, 6, 11, 9, 3, 5),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        position = Position(
            symbol=symbol,
            quantity=10,
            avg_price=Decimal("10000"),
            last_price=Decimal("10000"),
            opened_at=datetime(2026, 6, 11, 9, 0),
            highest_price=Decimal("10000"),
        )
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("900000"),
                positions={symbol: position},
            )
        )
        history_calls = 0

        def failed_history(_requested_symbol):
            nonlocal history_calls
            history_calls += 1
            raise RuntimeError("history unavailable")

        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FlowScalperStrategy(
                FlowScalperConfig(stop_loss_pct=Decimal("0.02"))
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: quote}),
            entry_history_provider=failed_history,
            scanner_provider=StaticScannerProvider(bars={symbol: quote}),
            data_source_kind="live",
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, history_calls)
        self.assertEqual(["SELL"], [order.side for order in broker.orders])
        self.assertTrue(
            any(
                event.kind == "trade"
                and event.side == "SELL"
                and event.result == "filled"
                for event in events
            )
        )

    def test_live_final_quote_uses_live_revalidation_path(self):
        class LiveRevalidationStrategy(FixedSignalStrategy):
            def __init__(self):
                super().__init__({})
                self.live_revalidation_calls = []

            def revalidate_signal(self, provisional_signal, provisional_bar, final_bar, account):
                raise AssertionError("paper revalidation path must not run in live execution")

            def revalidate_live_signal(self, provisional_signal, provisional_bar, final_bar, account):
                self.live_revalidation_calls.append((provisional_bar, final_bar))
                return provisional_signal

        symbol = "BUY001"
        strategy = LiveRevalidationStrategy()
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            execution_mode="live",
        )
        provisional_bar = _bar(symbol=symbol)
        final_bar = replace(
            provisional_bar,
            timestamp=provisional_bar.timestamp + timedelta(seconds=5),
            close=Decimal("10100"),
        )
        signal = Signal.buy(symbol, "scanner_signal")

        result = runtime._revalidate_signal_at_final_quote(
            [],
            signal,
            provisional_bar,
            final_bar,
            AccountSnapshot(cash=Decimal("1000000")),
        )

        self.assertEqual(signal, result)
        self.assertEqual([(provisional_bar, final_bar)], strategy.live_revalidation_calls)

    def test_live_final_quote_must_preserve_the_strategy_signal(self):
        class FinalQuoteRejectingStrategy(FixedSignalStrategy):
            def __init__(self):
                super().__init__({"BUY001": [Signal.buy("BUY001", "scanner_signal")]})
                self.revalidation_calls = []

            def revalidate_signal(self, provisional_signal, provisional_bar, final_bar, account):
                self.revalidation_calls.append((provisional_bar, final_bar))
                return None

        scanner_bar = replace(
            _bar(symbol="BUY001"),
            open=Decimal("9900"),
            high=Decimal("10100"),
            low=Decimal("9900"),
        )
        strategy = FinalQuoteRejectingStrategy()
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001", close="12000", offset=1)}),
            scanner_provider=StaticScannerProvider(bars={"BUY001": scanner_bar}),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, len(strategy.revalidation_calls))
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertTrue(any("final_quote_strategy_changed" in event.message for event in events))

    def test_live_temporary_stop_defers_order_without_failure_and_retries_next_cycle(self):
        symbol = "BUY001"
        scanner_bar = replace(_bar(symbol=symbol), market="KOSDAQ")
        stopped_quote = replace(
            _bar(symbol=symbol, close="10100", offset=1),
            market="KOSDAQ",
            temporary_stop=True,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        resumed_quote = replace(
            _bar(symbol=symbol, close="10100", offset=2),
            market="KOSDAQ",
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        final_quotes = SequenceBarProvider({symbol: [stopped_quote, resumed_quote]})
        risk_manager = RiskManager(
            RiskConfig(max_positions=1, max_consecutive_order_failures=1)
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "scanner_signal")]}
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        stopped_events = runtime.run_cycle()

        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertEqual(0, risk_manager._consecutive_order_failures)
        self.assertTrue(
            any(
                "market_trading_deferred" in event.message
                and "temporary_stop" in event.message
                for event in stopped_events
            )
        )

        resumed_events = runtime.run_cycle()

        self.assertEqual({symbol}, set(runtime.broker.snapshot().positions))
        self.assertTrue(
            any(
                event.kind == "trade"
                and event.result == "filled"
                and event.symbol == symbol
                for event in resumed_events
            )
        )
        self.assertEqual([symbol, symbol], final_quotes.requested_symbols)

    def test_live_symbol_stop_keeps_other_symbols_in_same_and_other_markets_operating(self):
        symbols = ["STOP01", "OPEN02", "OPEN01"]
        scanner_bars = {
            "STOP01": replace(_bar(symbol="STOP01"), market="KOSDAQ"),
            "OPEN02": replace(_bar(symbol="OPEN02"), market="KOSDAQ"),
            "OPEN01": replace(_bar(symbol="OPEN01"), market="KOSPI"),
        }
        final_quotes = DictBarProvider(
            {
                "STOP01": replace(
                    _bar(symbol="STOP01", offset=1),
                    market="KOSDAQ",
                    temporary_stop=True,
                    trading_state_source="KIS_CURRENT_PRICE",
                ),
                "OPEN02": replace(
                    _bar(symbol="OPEN02", offset=1),
                    market="KOSDAQ",
                    temporary_stop=False,
                    trading_state_source="KIS_CURRENT_PRICE",
                ),
                "OPEN01": replace(
                    _bar(symbol="OPEN01", offset=1),
                    market="KOSPI",
                    temporary_stop=False,
                    trading_state_source="KIS_CURRENT_PRICE",
                ),
            }
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {
                    symbol: [Signal.buy(symbol, "scanner_signal")]
                    for symbol in symbols
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=3)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=3,
            max_final_quote_requests_per_cycle=3,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual({"OPEN01", "OPEN02"}, set(runtime.broker.snapshot().positions))
        self.assertEqual(["STOP01", "OPEN02", "OPEN01"], final_quotes.requested_symbols)
        self.assertEqual({"STOP01"}, runtime._cycle_blocked_symbols)
        self.assertFalse(
            any(
                event.kind == "trade" and event.symbol == "STOP01"
                for event in events
            )
        )

    def test_live_stopped_holding_does_not_block_other_position_exit(self):
        opened_at = datetime(2026, 7, 29, 9, 0)
        positions = {
            symbol: Position(
                symbol=symbol,
                quantity=1,
                avg_price=Decimal("10000"),
                last_price=Decimal("9000"),
                opened_at=opened_at,
                highest_price=Decimal("10000"),
                sellable_quantity=1,
                managed_quantity=1,
            )
            for symbol in ("STOP01", "EXIT02")
        }
        broker = StaleLiveSnapshotBroker(
            snapshot=AccountSnapshot(
                cash=Decimal("100000"),
                positions=positions,
            )
        )
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "STOP01": [Signal.sell("STOP01", "stop_loss")],
                    "EXIT02": [Signal.sell("EXIT02", "stop_loss")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "STOP01": replace(
                        _bar(symbol="STOP01", close="9000"),
                        market="KOSDAQ",
                        temporary_stop=True,
                        trading_state_source="KIS_CURRENT_PRICE",
                    ),
                    "EXIT02": replace(
                        _bar(symbol="EXIT02", close="9000"),
                        market="KOSDAQ",
                        temporary_stop=False,
                        trading_state_source="KIS_CURRENT_PRICE",
                    ),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(
            [("EXIT02", "SELL")],
            [(order.symbol, order.side) for order in broker.orders],
        )
        self.assertEqual({"STOP01"}, runtime._cycle_blocked_symbols)
        self.assertTrue(
            any(
                event.kind == "trade"
                and event.symbol == "EXIT02"
                and event.result == "filled"
                for event in events
            )
        )

    def test_live_unverified_trading_state_source_defers_only_that_symbol(self):
        symbol = "UNKNOWN1"
        final_quotes = DictBarProvider(
            {
                symbol: replace(
                    _bar(symbol=symbol, offset=1),
                    market="KOSDAQ",
                    temporary_stop=False,
                    trading_state_source="",
                )
            }
        )
        risk_manager = RiskManager(
            RiskConfig(max_positions=1, max_consecutive_order_failures=1)
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "scanner_signal")]}
            ),
            risk_manager=risk_manager,
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars={symbol: replace(_bar(symbol=symbol), market="KOSDAQ")}
            ),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=1,
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertEqual(0, risk_manager._consecutive_order_failures)
        self.assertEqual({symbol}, runtime._cycle_blocked_symbols)
        self.assertTrue(
            any(
                "market_trading_deferred" in event.message
                and "trading_state_unknown" in event.message
                for event in events
            )
        )

    def test_direct_live_runtime_without_final_provider_requires_verified_kis_bar(self):
        symbol = "UNVERIFIED1"
        unverified_bar = replace(
            _bar(symbol=symbol),
            temporary_stop=False,
            trading_state_source="",
        )
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "entry")]}
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({symbol: unverified_bar}),
            execution_mode="live",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual({}, broker.snapshot().positions)
        self.assertEqual({symbol}, runtime._cycle_blocked_symbols)
        self.assertTrue(
            any(
                "market_trading_deferred" in event.message
                and "trading_state_unknown" in event.message
                for event in events
            )
        )

    def test_cached_live_final_quote_requires_verified_kis_state(self):
        symbol = "UNVERIFIED1"
        runtime = make_runtime(
            symbols=[symbol],
            signals={symbol: [Signal.buy(symbol, "entry")]},
        )
        runtime.execution_mode = "live"
        runtime.final_quote_provider = DictBarProvider(
            {symbol: _bar(symbol=symbol)}
        )
        runtime._confirmed_scan_bars_this_cycle = {
            symbol: replace(
                _bar(symbol=symbol),
                temporary_stop=False,
                trading_state_source="",
            )
        }
        cycle_events = []

        result = runtime._final_quote_for_signal(
            cycle_events,
            Signal.buy(symbol, "entry"),
            _bar(symbol=symbol),
        )

        self.assertIsNone(result)
        self.assertEqual({symbol}, runtime._cycle_blocked_symbols)
        self.assertTrue(
            any("trading_state_unknown" in event.message for event in cycle_events)
        )

    def test_live_entry_fill_reuses_atomic_broker_daily_symbol_count(self):
        ledger = InMemoryManagedLivePositionLedger()
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.managed_position_ledger = ledger
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({"BUY001": _bar(symbol="BUY001")}),
            execution_mode="live",
        )
        bar = _bar(symbol="BUY001")
        fill = Fill(
            order=Order.buy("BUY001", 1, "entry"),
            accepted=True,
            timestamp=bar.timestamp,
            price=bar.close,
            quantity=1,
        )
        ledger.record_fill_transaction(
            fill_key="order-1",
            symbol="BUY001",
            side="BUY",
            quantity_delta=1,
            cumulative_filled=1,
            timestamp=fill.timestamp,
            price=fill.price,
        )

        runtime._record_entry_fill(fill)

        trading_day = bar.timestamp.date()
        self.assertEqual({("BUY001", trading_day): 1}, ledger.entry_counts())
        self.assertTrue(runtime.risk_manager.entry_limit_reached("BUY001", trading_day))

    def test_live_cycle_entry_count_reconciliation_recovers_after_transient_failure(self):
        trading_day = date(2026, 7, 10)
        ledger = InMemoryManagedLivePositionLedger()

        class RecoveringBroker(PaperBroker):
            def __init__(self):
                super().__init__(initial_cash=Decimal("1000000"))
                self.managed_position_ledger = ledger
                self.reconciliation_results = [False, True]
                self.reconciliation_calls = 0

            def reconcile_managed_entry_counts(self):
                self.reconciliation_calls += 1
                result = self.reconciliation_results.pop(0)
                if result:
                    ledger.replace_entry_counts_for_date(trading_day, {"BUY001": 1})
                return result

        broker = RecoveringBroker()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            execution_mode="live",
        )
        first_events = []
        second_events = []

        first = runtime._sync_live_entry_count_state(first_events)
        second = runtime._sync_live_entry_count_state(second_events)

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(2, broker.reconciliation_calls)
        self.assertTrue(runtime.risk_manager.entry_limit_reached("BUY001", trading_day))
        self.assertTrue(any("live_entry_count_reconciliation_pending" in event.message for event in first_events))

    def test_live_entry_count_sync_failure_blocks_buys_but_allows_exit_until_recovery(self):
        ledger = InMemoryManagedLivePositionLedger()

        class RecoveringBroker(PaperBroker):
            def __init__(self):
                super().__init__(initial_cash=Decimal("1000000"))
                self.managed_position_ledger = ledger
                self.reconciliation_results = [False, True]

            def reconcile_managed_entry_counts(self):
                return self.reconciliation_results.pop(0)

        broker = RecoveringBroker()
        broker.place_order(Order.buy("EXIT01", 1, "seed"), _bar(symbol="EXIT01", close="10000"))
        runtime = PaperTradingRuntime(
            symbols=["BUY001"],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    "EXIT01": [Signal.sell("EXIT01", "stop_loss")],
                    "BUY001": [Signal.buy("BUY001", "entry")],
                }
            ),
            risk_manager=RiskManager(
                RiskConfig(max_order_amount=Decimal("100000"), max_positions=1)
            ),
            bar_provider=DictBarProvider(
                {
                    "EXIT01": _bar(symbol="EXIT01", close="9000"),
                    "BUY001": _bar(symbol="BUY001", close="10000"),
                }
            ),
            execution_mode="live",
        )
        runtime.start()

        blocked_cycle = runtime.run_cycle()
        recovered_cycle = runtime.run_cycle()

        self.assertEqual(
            [("SELL", "filled")],
            [(event.side, event.result) for event in blocked_cycle if event.kind == "trade"],
        )
        self.assertTrue(runtime._last_live_entry_count_sync_ready)
        self.assertEqual(
            [("BUY", "filled")],
            [(event.side, event.result) for event in recovered_cycle if event.kind == "trade"],
        )
        self.assertEqual({"BUY001"}, set(broker.snapshot().positions))

    def test_live_unlimited_execution_capacity_uses_scanner_history_read_savings(self):
        class HistoryAwareFixedSignalStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def seed_history(self, _symbol, bars):
                return len(list(bars))

        symbols = [f"BUY{index:03d}" for index in range(10)]
        bars = {
            symbol: _bar(symbol=symbol, close="10000", offset=5)
            for symbol in symbols
        }
        histories = {
            symbol: tuple(
                _bar(symbol=symbol, close="10000", offset=minute)
                for minute in range(1, 5)
            )
            for symbol in symbols
        }
        scanner = StaticScannerProvider(bars=bars, histories=histories)
        history_calls = []
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=HistoryAwareFixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "live_cash_slot_target")] for symbol in symbols}
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            entry_history_provider=lambda symbol: history_calls.append(symbol),
            scanner_provider=scanner,
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=10,
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=11,
        )

        runtime._initialize_cycle_entry_slot_target()

        self.assertIsNone(runtime._cycle_entry_slot_target)
        self.assertEqual(1, runtime._cycle_entry_slot_capacity)
        runtime._scanner_snapshot = scanner.snapshot(symbols)
        runtime._expand_cycle_entry_capacity_for_scanner_history(symbols)
        self.assertEqual(2, runtime._cycle_entry_slot_capacity)
        self.assertEqual(
            Decimal("300000.00"),
            runtime._entry_budget_for_account(runtime.broker.snapshot()).quantize(Decimal("0.01")),
        )

        runtime.start()
        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        self.assertEqual(2, len(filled))
        self.assertTrue(all(event.quantity >= 1 for event in filled))
        self.assertEqual([], history_calls)

    def test_live_unlimited_capacity_reserves_kis_history_read_when_scanner_history_is_missing(self):
        class HistoryAwareFixedSignalStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

        symbols = ["BUY001", "BUY002"]
        bars = {
            symbol: _bar(symbol=symbol, close="10000", offset=5)
            for symbol in symbols
        }
        scanner = StaticScannerProvider(bars=bars)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=HistoryAwareFixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            entry_history_provider=lambda _symbol: (),
            scanner_provider=scanner,
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=11,
        )

        runtime._initialize_cycle_entry_slot_target()
        runtime._scanner_snapshot = scanner.snapshot(symbols)
        runtime._expand_cycle_entry_capacity_for_scanner_history(symbols)

        self.assertEqual(1, runtime._cycle_entry_slot_capacity)

    def test_live_unlimited_capacity_uses_ready_scanner_histories_when_fallbacks_are_mixed_in(self):
        class HistoryAwareFixedSignalStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 2
                volume_window = 2
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

        symbols = ["READY1", "READY2", "READY3", "FALL04"]
        bars = {
            symbol: _bar(symbol=symbol, close="10000", offset=5)
            for symbol in symbols
        }
        histories = {
            symbol: tuple(
                _bar(symbol=symbol, close="10000", offset=minute)
                for minute in range(1, 5)
            )
            for symbol in symbols[:3]
        }
        scanner = StaticScannerProvider(bars=bars, histories=histories)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=HistoryAwareFixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            entry_history_provider=lambda _symbol: (),
            scanner_provider=scanner,
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=11,
        )

        runtime._initialize_cycle_entry_slot_target()
        runtime._scanner_snapshot = scanner.snapshot(symbols)
        runtime._expand_cycle_entry_capacity_for_scanner_history(symbols)

        self.assertEqual(2, runtime._cycle_entry_slot_capacity)

    def test_live_sizing_keeps_one_share_affordable_and_skips_spent_candidates(self):
        symbols = ["HIGH01", "HIGH02", "CHEAP1"]
        bars = {
            "HIGH01": _bar(symbol="HIGH01", close="400000"),
            "HIGH02": _bar(symbol="HIGH02", close="400000"),
            "CHEAP1": _bar(symbol="CHEAP1", close="10000"),
        }
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "live_affordable_slot_target")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(max_positions=0, max_position_amount=Decimal("1000000"))
            ),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(bars=bars),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                max_position_amount=Decimal("1000000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [event for event in events if event.kind == "trade" and event.result == "filled"]
        rejected = [event for event in events if event.kind == "trade" and event.result == "rejected"]
        self.assertEqual([("HIGH01", 1), ("HIGH02", 1)], [(event.symbol, event.quantity) for event in filled])
        self.assertEqual([], rejected)

    def test_live_physical_capacity_uses_observed_paginated_account_reads(self):
        class PaginatedAccountClient:
            def market_read_budget_state(self):
                return 3, 14

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = PaginatedAccountClient()
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{index:03d}" for index in range(10)],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(bars={}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )

        self.assertEqual(1, runtime._open_position_monitor_limit(10))
        self.assertEqual(2, runtime._live_physical_entry_slot_capacity(0))
        self.assertEqual(1, runtime._live_physical_entry_slot_capacity(1))

        runtime._advance_live_planner_phase()
        runtime._cycle_live_planner_phase = None

        self.assertEqual(2, runtime._live_physical_entry_slot_capacity(0))

        runtime._cycle_live_planner_phase = None
        self.assertEqual(3, runtime._open_position_monitor_limit(10))
        self.assertEqual(1, runtime._live_physical_entry_slot_capacity(1))

    def test_live_buy_preflight_uses_observed_paginated_account_cost_before_quote(self):
        symbol = "BUY001"

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                self.used += count

            def end_market_read_budget(self):
                self.limit = None

        class CachedOpeningDayGate:
            def __call__(self):
                return True

            def pending_market_read_cost(self):
                return 10

        class PaginatedBroker:
            def __init__(self):
                self.client = BudgetClient()
                self.market_is_open = CachedOpeningDayGate()
                self.orders = []

            def snapshot(self, *, timestamp=None):
                self.client.consume(4)
                return AccountSnapshot(cash=Decimal("1000000"))

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.orders.append(order)
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        scanner_bar = replace(
            _bar(symbol=symbol),
            open=Decimal("9900"),
            high=Decimal("10100"),
            low=Decimal("9800"),
        )
        broker = PaginatedBroker()
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, offset=1)})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=FixedSignalStrategy({symbol: [Signal.buy(symbol, "entry")]}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
            max_physical_market_reads_per_cycle=12,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual([], broker.orders)
        self.assertTrue(
            any("physical_entry_capacity_reached" in event.message for event in events)
        )

    def test_live_buy_preflight_cost_clamps_invalid_opening_day_estimates(self):
        class AccountClient:
            def market_read_budget_state(self):
                return 2, 26

        class InvalidEstimateGate:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return True

            def pending_market_read_cost(self):
                if isinstance(self.value, Exception):
                    raise self.value
                return self.value

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = AccountClient()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=26,
        )

        for invalid in (-1, 99, "invalid", RuntimeError("estimate unavailable")):
            broker.market_is_open = InvalidEstimateGate(invalid)
            self.assertEqual(12, runtime._live_buy_preflight_market_read_cost())

        broker.market_is_open = lambda: True
        self.assertEqual(3, runtime._live_buy_preflight_market_read_cost())

    def test_live_physical_capacity_at_twenty_six_uses_cached_opening_day_cost(self):
        class TwoReadAccountClient:
            def market_read_budget_state(self):
                return 2, 26

        class OpeningDayGate:
            def __init__(self):
                self.remaining = 10

            def __call__(self):
                return True

            def pending_market_read_cost(self):
                return self.remaining

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = TwoReadAccountClient()
        opening_day_gate = OpeningDayGate()
        broker.market_is_open = opening_day_gate
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{index:03d}" for index in range(10)],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(bars={}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=26,
        )

        self.assertEqual(3, runtime._live_physical_entry_slot_capacity(0))

        opening_day_gate.remaining = 0
        self.assertEqual(6, runtime._live_physical_entry_slot_capacity(0))
        self.assertEqual(9, runtime._open_position_monitor_limit(10))
        self.assertEqual(1, runtime._live_physical_entry_slot_capacity(10))

        runtime._advance_live_planner_phase()
        runtime._cycle_live_planner_phase = None

        self.assertEqual(10, runtime._open_position_monitor_limit(10))
        self.assertEqual(0, runtime._live_physical_entry_slot_capacity(10))

    def test_live_cold_opening_day_gate_with_paginated_account_does_not_starve_buys(self):
        symbols = ["BUY001", "BUY002"]
        broker = DynamicBudgetPaperBroker(account_read_cost=7)
        opening_day_gate = BudgetedOpeningDayGate(broker.client)
        broker.market_is_open = opening_day_gate
        final_quotes = DynamicBudgetProvider(
            broker.client,
            reads=2,
            values={symbol: _bar(symbol=symbol, offset=1) for symbol in symbols},
        )
        strategy = FixedSignalStrategy(
            {symbol: [Signal.buy(symbol, "entry")] for symbol in symbols}
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars={symbol: _bar(symbol=symbol) for symbol in symbols}
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=2),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
            max_physical_market_reads_per_cycle=26,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(2, opening_day_gate.calls)
        self.assertEqual(1, opening_day_gate.physical_read_calls)
        self.assertEqual([36, 26], broker.client.begin_limits)
        self.assertEqual(symbols, [order.symbol for order in broker.submitted_orders])
        self.assertEqual(1, final_quotes.requested_symbols.count("BUY002"))
        self.assertEqual("BUY002", final_quotes.requested_symbols[-1])

    def test_live_opening_day_gate_false_or_error_stops_before_account_and_market_reads(self):
        for result, error, pending in (
            (False, None, 10),
            (False, None, 0),
            (True, RuntimeError("secret-token-123"), 10),
        ):
            with self.subTest(
                result=result,
                error=type(error).__name__ if error else None,
                pending=pending,
            ):
                symbol = "BUY001"
                broker = DynamicBudgetPaperBroker(account_read_cost=7)
                opening_day_gate = BudgetedOpeningDayGate(
                    broker.client,
                    pending=pending,
                    reads=0,
                    result=result,
                    error=error,
                )
                broker.market_is_open = opening_day_gate
                final_quotes = DynamicBudgetProvider(
                    broker.client,
                    reads=2,
                    values={symbol: _bar(symbol=symbol, offset=1)},
                )
                runtime = PaperTradingRuntime(
                    symbols=[symbol],
                    broker=broker,
                    strategy=FixedSignalStrategy(
                        {symbol: [Signal.buy(symbol, "entry")]}
                    ),
                    risk_manager=RiskManager(RiskConfig(max_positions=1)),
                    bar_provider=DictBarProvider({}),
                    final_quote_provider=final_quotes,
                    scanner_provider=StaticScannerProvider(
                        bars={symbol: _bar(symbol=symbol)}
                    ),
                    settings=CustomStrategySettings.default().with_updates(max_positions=1),
                    data_source_kind="live",
                    execution_mode="live",
                    max_final_quote_requests_per_cycle=1,
                    max_physical_market_reads_per_cycle=26,
                )
                runtime.start()
                snapshot_calls_before_cycle = broker.snapshot_calls

                events = runtime.run_cycle()

                self.assertEqual(1, opening_day_gate.calls)
                self.assertEqual(snapshot_calls_before_cycle, broker.snapshot_calls)
                self.assertEqual([], final_quotes.requested_symbols)
                self.assertEqual([], broker.submitted_orders)
                messages = "\n".join(event.message for event in events)
                self.assertIn("live_opening_day_gate_failed", messages)
                self.assertNotIn("secret-token-123", messages)

    def test_live_account_read_cost_delta_excludes_opening_day_reads(self):
        broker = DynamicBudgetPaperBroker(account_read_cost=7)
        opening_day_gate = BudgetedOpeningDayGate(
            broker.client,
            reads=4,
        )
        broker.market_is_open = opening_day_gate
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=26,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(1, opening_day_gate.calls)
        self.assertEqual(7, runtime._cycle_live_account_read_cost)
        self.assertEqual([18], broker.client.ensure_calls)

    def test_live_high_pagination_executes_sell_before_considering_buy(self):
        held_symbol = "HOLD01"
        buy_symbol = "BUY001"
        broker = DynamicBudgetPaperBroker(account_read_cost=20)
        PaperBroker.place_order(
            broker,
            Order.buy(held_symbol, 1, "seed"),
            _bar(symbol=held_symbol, close="10000"),
        )
        broker.submitted_orders.clear()
        opening_day_gate = BudgetedOpeningDayGate(broker.client)
        broker.market_is_open = opening_day_gate
        final_quotes = DynamicBudgetProvider(
            broker.client,
            reads=2,
            values={
                held_symbol: _bar(symbol=held_symbol, close="9900", offset=1),
                buy_symbol: _bar(symbol=buy_symbol, close="10000", offset=1),
            },
        )
        runtime = PaperTradingRuntime(
            symbols=[buy_symbol],
            broker=broker,
            strategy=FixedSignalStrategy(
                {
                    held_symbol: [Signal.sell(held_symbol, "risk_exit")],
                    buy_symbol: [Signal.buy(buy_symbol, "entry")],
                }
            ),
            risk_manager=RiskManager(RiskConfig(max_positions=2)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(
                bars={buy_symbol: _bar(symbol=buy_symbol)}
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=2),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
            max_physical_market_reads_per_cycle=26,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertGreaterEqual(broker.client.ensure_calls[-1], 52)
        self.assertEqual(
            [(held_symbol, "SELL")],
            [(order.symbol, order.side) for order in broker.submitted_orders],
        )

    def test_live_active_extended_limit_drives_entry_and_monitor_capacity(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        client = DynamicBudgetClient()
        client.limit = 40
        client.used = 7
        broker.client = client
        opening_day_gate = BudgetedOpeningDayGate(client, pending=0, reads=0)
        broker.market_is_open = opening_day_gate
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{index:03d}" for index in range(10)],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(bars={}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=26,
        )
        runtime._cycle_live_account_read_cost = 7
        runtime._cycle_live_planner_phase = "monitoring"

        self.assertEqual(
            3,
            runtime._live_physical_entry_capacity_for_monitored_positions(0),
        )
        self.assertEqual(10, runtime._open_position_monitor_limit(10))

    def test_flow_scalper_provisional_buy_revalidates_and_fills_with_verified_quote(self):
        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol, close="106", offset=3),
            timestamp=datetime(2026, 6, 11, 9, 3, 20),
            open=Decimal("105"),
            high=Decimal("107"),
            low=Decimal("104"),
            volume=4000,
            vwap=Decimal("105"),
            bid=None,
            ask=None,
            temporary_stop=None,
            trading_state_source="",
        )
        history = [
            replace(_bar(symbol=symbol, close="100", offset=0), volume=1000),
            replace(_bar(symbol=symbol, close="102", offset=1), volume=1000),
            replace(_bar(symbol=symbol, close="104", offset=2), volume=3000),
        ]
        final_bar = replace(
            current,
            timestamp=datetime(2026, 6, 11, 9, 3, 25),
            bid=Decimal("105.9"),
            ask=Decimal("106.1"),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                max_spread_bps=Decimal("50"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda _symbol: history,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        filled = [
            event
            for event in events
            if event.kind == "trade" and event.result == "filled"
        ]
        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual([(symbol, "BUY")], [(event.symbol, event.side) for event in filled])
        self.assertIn(symbol, broker.snapshot().positions)

    def test_live_physical_capacity_reserves_one_entry_while_rotating_held_positions(self):
        class TwoReadAccountClient:
            def market_read_budget_state(self):
                return 2, 14

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = TwoReadAccountClient()
        runtime = PaperTradingRuntime(
            symbols=[f"BUY{index:03d}" for index in range(10)],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(bars={}),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )

        self.assertEqual(2, runtime._open_position_monitor_limit(3))
        self.assertEqual(1, runtime._live_physical_entry_slot_capacity(3))

    def test_live_position_monitor_capacity_reserves_completed_history_read(self):
        class TwoReadAccountClient:
            def market_read_budget_state(self):
                return 2, 14

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = TwoReadAccountClient()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FlowScalperStrategy(FlowScalperConfig()),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            entry_history_provider=lambda _symbol: [],
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=14,
        )
        runtime._cycle_live_planner_phase = "monitoring"

        self.assertEqual(3, runtime._open_position_monitor_limit(4))
        self.assertEqual(
            0,
            runtime._live_physical_entry_capacity_for_monitored_positions(3),
        )

    def test_live_holding_rotation_keeps_next_symbol_after_a_monitored_position_is_removed(self):
        class TwoReadAccountClient:
            def market_read_budget_state(self):
                return 2, 14

        held_symbols = [f"HOLD{index:02d}" for index in range(1, 7)]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = TwoReadAccountClient()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=14,
        )

        first_batch = runtime._rotated_open_position_symbols(held_symbols)
        for symbol in first_batch:
            runtime._record_open_position_processed(symbol)
        remaining_symbols = [symbol for symbol in held_symbols if symbol != "HOLD04"]
        second_batch = runtime._rotated_open_position_symbols(remaining_symbols)

        self.assertEqual(held_symbols[:4], first_batch)
        self.assertEqual(["HOLD05", "HOLD06", "HOLD01", "HOLD02"], second_batch)

    def test_live_holding_rotation_retries_the_first_unprocessed_position_next_cycle(self):
        class TwoReadAccountClient:
            def market_read_budget_state(self):
                return 2, 14

        held_symbols = [f"HOLD{index:02d}" for index in range(1, 7)]
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.client = TwoReadAccountClient()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=14,
        )

        first_batch = runtime._rotated_open_position_symbols(held_symbols)
        runtime._record_open_position_processed("HOLD01")
        runtime._record_open_position_processed("HOLD03")
        runtime._record_open_position_processed("HOLD04")
        second_batch = runtime._rotated_open_position_symbols(held_symbols)

        self.assertEqual(held_symbols[:4], first_batch)
        self.assertEqual("HOLD02", second_batch[0])

    def test_live_rejected_exit_retries_once_then_rotates_to_other_positions(self):
        class RejectingExitBroker(PaperBroker):
            def place_order(self, order, bar):
                if order.side == "SELL":
                    return Fill(
                        order=order,
                        accepted=False,
                        timestamp=bar.timestamp,
                        price=bar.close,
                        quantity=0,
                        reject_reason="test_exit_rejected",
                    )
                return super().place_order(order, bar)

        class FirstPositionExitStrategy(FixedSignalStrategy):
            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol == "HOLD01":
                    return [Signal.sell(bar.symbol, "stop_loss")]
                return []

        broker = RejectingExitBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("HOLD01", 1, "seed"), _bar(symbol="HOLD01", close="100"))
        broker.place_order(Order.buy("HOLD02", 1, "seed"), _bar(symbol="HOLD02", close="100"))
        strategy = FirstPositionExitStrategy({})
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "HOLD01": _bar(symbol="HOLD01", close="80", offset=1),
                    "HOLD02": _bar(symbol="HOLD02", close="100", offset=1),
                }
            ),
            execution_mode="live",
            max_bar_requests_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(["HOLD01", "HOLD01", "HOLD02"], strategy.seen_symbols)

    def test_live_repeated_holding_quote_failure_retries_once_then_rotates(self):
        class FlakyHoldingBarProvider:
            def __init__(self):
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                if symbol == "HOLD01" and self.requested_symbols.count(symbol) <= 2:
                    raise RuntimeError("temporary quote failure")
                return _bar(symbol=symbol, close="100", offset=1)

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("HOLD01", 1, "seed"), _bar(symbol="HOLD01", close="100"))
        broker.place_order(Order.buy("HOLD02", 1, "seed"), _bar(symbol="HOLD02", close="100"))
        bar_provider = FlakyHoldingBarProvider()
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=bar_provider,
            execution_mode="live",
            max_bar_requests_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(["HOLD01", "HOLD01", "HOLD02"], bar_provider.requested_symbols)

    def test_live_repeated_holding_strategy_failure_retries_once_then_rotates(self):
        class FlakyHoldingStrategy(FixedSignalStrategy):
            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol == "HOLD01" and self.seen_symbols.count(bar.symbol) <= 2:
                    raise RuntimeError("temporary strategy failure")
                return []

        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy("HOLD01", 1, "seed"), _bar(symbol="HOLD01", close="100"))
        broker.place_order(Order.buy("HOLD02", 1, "seed"), _bar(symbol="HOLD02", close="100"))
        strategy = FlakyHoldingStrategy({})
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider(
                {
                    "HOLD01": _bar(symbol="HOLD01", close="100", offset=1),
                    "HOLD02": _bar(symbol="HOLD02", close="100", offset=1),
                }
            ),
            execution_mode="live",
            max_bar_requests_per_cycle=1,
        )
        runtime.start()

        runtime.run_cycle()
        runtime.run_cycle()
        runtime.run_cycle()

        self.assertEqual(["HOLD01", "HOLD01", "HOLD02"], strategy.seen_symbols)

    def test_live_entry_lane_probes_exact_buying_power_when_balance_snapshot_reports_zero(self):
        runtime, broker, _, _, entry_symbol = make_zero_balance_probe_runtime(
            planning_cash="64050"
        )
        runtime.start()

        events = runtime.run_cycle()

        fills = [
            event
            for event in events
            if event.kind == "trade" and event.side == "BUY" and event.result == "filled"
        ]
        self.assertEqual(1, len(broker.planning_calls))
        self.assertEqual([(entry_symbol, 25)], [(event.symbol, event.quantity) for event in fills])
        self.assertEqual(1, runtime._cycle_entry_slot_capacity)

    def test_live_entry_lane_does_not_order_when_exact_buying_power_is_zero(self):
        runtime, broker, strategy, held_symbols, _ = make_zero_balance_probe_runtime(
            planning_cash="0"
        )
        runtime.start()

        first_cycle = runtime.run_cycle()
        strategy.seen_symbols.clear()
        second_cycle = runtime.run_cycle()
        third_cycle = runtime.run_cycle()

        self.assertEqual(1, len(broker.planning_calls))
        self.assertEqual([], broker.orders)
        self.assertFalse(
            any(event.kind == "trade" for event in first_cycle + second_cycle + third_cycle)
        )
        self.assertCountEqual(held_symbols * 2, strategy.seen_symbols)
        self.assertEqual("entry_reserved", runtime._next_live_planner_phase)

        runtime._last_live_planning_buying_power_at -= timedelta(minutes=6)
        runtime.run_cycle()

        self.assertEqual(2, len(broker.planning_calls))

    def test_live_exact_zero_cooldown_skips_repeated_probe_without_holdings(self):
        runtime, broker, _, _, _ = make_zero_balance_probe_runtime(
            planning_cash="0",
            held_count=0,
        )
        runtime.start()

        runtime.run_cycle()
        second_cycle = runtime.run_cycle()

        self.assertEqual(1, len(broker.planning_calls))
        self.assertEqual([], broker.orders)
        self.assertTrue(
            any(
                event.kind == "system"
                and "exact_zero_buying_power_cooldown" in event.message
                for event in second_cycle
            )
        )

    def test_live_entry_lane_rechecks_exact_zero_immediately_after_sell_fill(self):
        runtime, broker, strategy, held_symbols, _ = make_zero_balance_probe_runtime(
            planning_cash="0"
        )
        runtime.start()

        runtime.run_cycle()
        strategy.signals[held_symbols[0]] = [Signal.sell(held_symbols[0], "forced_exit")]
        runtime.run_cycle()
        strategy.signals.pop(held_symbols[0])
        runtime.run_cycle()

        self.assertEqual(2, len(broker.planning_calls))
        self.assertTrue(
            any(order.side == "SELL" and order.symbol == held_symbols[0] for order in broker.orders)
        )

    def test_live_entry_lane_does_not_order_when_exact_buying_power_refresh_fails(self):
        runtime, broker, _, _, _ = make_zero_balance_probe_runtime(
            planning_cash="0",
            blocker="live_buyable_inquiry_failed: unavailable",
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(1, len(broker.planning_calls))
        self.assertEqual([], broker.orders)
        self.assertTrue(
            any(
                event.kind == "system" and "live_planning_buying_power_failed" in event.message
                for event in events
            )
        )

    def test_live_entry_lane_rechecks_exact_zero_immediately_after_positive_balance(self):
        runtime, broker, _, _, entry_symbol = make_zero_balance_probe_runtime(
            planning_cash="0"
        )
        runtime.start()
        runtime.run_cycle()
        broker._snapshot = replace(
            broker._snapshot,
            buying_power_override=Decimal("64050"),
        )
        broker.planning_cash = Decimal("64050")
        broker.cached_cash = Decimal("64050")

        events = runtime.run_cycle()

        self.assertEqual(2, len(broker.planning_calls))
        self.assertTrue(
            any(
                event.kind == "trade"
                and event.side == "BUY"
                and event.symbol == entry_symbol
                and event.result == "filled"
                for event in events
            )
        )

    def test_live_positive_balance_clears_exact_zero_cooldown_while_entries_are_blocked(self):
        runtime, broker, _, _, _ = make_zero_balance_probe_runtime(planning_cash="0")
        runtime.start()
        runtime.run_cycle()
        broker._snapshot = replace(
            broker._snapshot,
            buying_power_override=Decimal("64050"),
        )
        runtime.settings = runtime.settings.with_updates(kill_switch=True)

        runtime.run_cycle()

        self.assertIsNone(runtime._last_live_planning_buying_power)
        self.assertIsNone(runtime._last_live_planning_buying_power_at)
        self.assertEqual(1, len(broker.planning_calls))

    def test_live_entry_lane_phase_ignores_pre_sync_rate_limit_skips(self):
        held_symbols = ["HOLD01", "HOLD02", "HOLD03"]
        entry_symbols = ["BUY001", "BUY002"]

        class TwoReadAccountClient:
            def __init__(self):
                self.limit = None

            def begin_market_read_budget(self, limit):
                self.limit = limit

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return 2, self.limit

            def end_market_read_budget(self):
                self.limit = None

        class AlternatingRateLimitedBroker(PaperBroker):
            def __init__(self):
                super().__init__(initial_cash=Decimal("1000000"))
                self.client = TwoReadAccountClient()
                self.fail_next_snapshot = False

            def snapshot(self, *, timestamp=None):
                if self.fail_next_snapshot:
                    self.fail_next_snapshot = False
                    raise RuntimeError(
                        'KIS HTTP 500: {"msg_cd":"EGW00215","msg1":"rate limit"}'
                    )
                return super().snapshot()

        broker = AlternatingRateLimitedBroker()
        for symbol in held_symbols:
            broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="10000"))

        all_symbols = [*held_symbols, *entry_symbols]
        runtime = PaperTradingRuntime(
            symbols=entry_symbols,
            broker=broker,
            strategy=FixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "entry")] for symbol in entry_symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("300000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=DictBarProvider(
                {symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in all_symbols}
            ),
            scanner_provider=StaticScannerProvider(
                bars={symbol: _bar(symbol=symbol, close="10000") for symbol in entry_symbols}
            ),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_position_amount=Decimal("300000"),
            ),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=10,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        capacities = []
        fills_per_successful_cycle = []
        for _ in range(3):
            broker.fail_next_snapshot = True
            skipped_events = runtime.run_cycle()
            self.assertTrue(
                any("rate_limit_skip - live_account_snapshot" in event.message for event in skipped_events)
            )

            successful_events = runtime.run_cycle()
            capacities.append(runtime._cycle_entry_slot_capacity)
            fills_per_successful_cycle.append(
                len(
                    [
                        event
                        for event in successful_events
                        if event.kind == "trade" and event.side == "BUY" and event.result == "filled"
                    ]
                )
            )

        self.assertEqual([1, 0, 1], capacities)
        self.assertEqual([1, 0, 1], fills_per_successful_cycle)
        self.assertEqual(5, len(broker.snapshot().positions))
        self.assertEqual("monitoring", runtime._next_live_planner_phase)

    def test_live_entry_quote_capacity_is_independent_from_held_symbol_monitoring(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        for symbol in ("HOLD01", "HOLD02"):
            broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="10000"))
        runtime = PaperTradingRuntime(
            symbols=["BUY001", "BUY002"],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            scanner_provider=StaticScannerProvider(
                bars={symbol: _bar(symbol=symbol) for symbol in ("BUY001", "BUY002")}
            ),
            settings=CustomStrategySettings.default().with_updates(max_positions=0),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
        )

        runtime._initialize_cycle_entry_slot_target()

        self.assertEqual(2, runtime._cycle_entry_slot_capacity)

    def test_live_held_symbol_uses_final_quote_even_when_scanner_bar_is_dense(self):
        symbol = "HOLD01"
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.place_order(Order.buy(symbol, 1, "seed"), _bar(symbol=symbol, close="100"))

        class StopLossStrategy(FixedSignalStrategy):
            def __init__(self, signals):
                super().__init__(signals)
                self.seen_prices = []

            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                self.seen_prices.append(bar.close)
                if bar.symbol in account.positions and bar.sell_price <= Decimal("80"):
                    return [Signal.sell(bar.symbol, "stop_loss")]
                return []

        scanner_bar = replace(
            _bar(symbol=symbol, close="100"),
            open=Decimal("99"),
            high=Decimal("101"),
            low=Decimal("99"),
        )
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="80", offset=1)})
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=broker,
            strategy=StopLossStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual(0, runtime._final_quote_requests_this_cycle)
        self.assertEqual([Decimal("80")], runtime.strategy.seen_prices)
        self.assertNotIn(symbol, broker.snapshot().positions)
        self.assertTrue(
            any(event.kind == "trade" and event.side == "SELL" and event.result == "filled" for event in events)
        )

    def test_live_holding_rotation_reserves_sell_preflight_reads(self):
        symbols = [f"HOLD{index:02d}" for index in range(1, 7)]

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0
                self.completed_cycles = []

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.completed_cycles.append(self.used)
                self.limit = None

        class BudgetedBroker:
            def __init__(self):
                opened_at = datetime(2026, 6, 11, 9, 0)
                self.client = BudgetClient()
                self.positions = {
                    symbol: Position(
                        symbol=symbol,
                        quantity=1,
                        avg_price=Decimal("100"),
                        last_price=Decimal("100"),
                        opened_at=opened_at,
                        highest_price=Decimal("100"),
                        sellable_quantity=1,
                        managed_quantity=1,
                    )
                    for symbol in symbols
                }
                self.orders = []

            def snapshot(self, *, timestamp=None):
                self.client.consume(2)
                return AccountSnapshot(cash=Decimal("1000000"), positions=dict(self.positions))

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.client.consume(3)
                self.orders.append(order)
                if order.side == "SELL":
                    self.positions.pop(order.symbol, None)
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        class BudgetedQuotes:
            def __init__(self, client):
                self.client = client
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                self.client.consume(2)
                price = "80" if symbol == "HOLD04" else "100"
                return _bar(symbol=symbol, close=price, offset=1)

        class LastHoldingStopStrategy(FixedSignalStrategy):
            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol == "HOLD04" and bar.sell_price <= Decimal("80"):
                    return [Signal.sell(bar.symbol, "stop_loss")]
                return []

        class EmptyScanner:
            label = "empty"
            kind = "test"

            def rank_symbols(self, symbols):
                return []

            def snapshot(self, symbols):
                return ScannerSnapshot()

        broker = BudgetedBroker()
        final_quotes = BudgetedQuotes(broker.client)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=LastHoldingStopStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=EmptyScanner(),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        cycle = runtime.run_cycle()

        self.assertEqual(symbols[:4], final_quotes.requested_symbols)
        self.assertNotIn("HOLD04", broker.positions)
        self.assertEqual(["HOLD04"], [order.symbol for order in broker.orders])
        self.assertTrue(all(used <= 14 for used in broker.client.completed_cycles))
        self.assertTrue(
            any(event.kind == "trade" and event.side == "SELL" and event.result == "filled" for event in cycle)
        )

    def test_live_multiple_sell_signals_defer_the_first_exit_without_preflight_read_capacity(self):
        symbols = ["HOLD01", "HOLD02", "HOLD03"]

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.limit = None

        class BudgetedBroker:
            def __init__(self):
                opened_at = datetime(2026, 6, 11, 9, 0)
                self.client = BudgetClient()
                self.positions = {
                    symbol: Position(
                        symbol=symbol,
                        quantity=1,
                        avg_price=Decimal("100"),
                        last_price=Decimal("100"),
                        opened_at=opened_at,
                        highest_price=Decimal("100"),
                        sellable_quantity=1,
                        managed_quantity=1,
                    )
                    for symbol in symbols
                }
                self.orders = []

            def snapshot(self, *, timestamp=None):
                self.client.consume(3)
                return AccountSnapshot(cash=Decimal("1000000"), positions=dict(self.positions))

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.client.consume(4)
                self.orders.append(order)
                if order.side == "SELL":
                    self.positions.pop(order.symbol, None)
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        class BudgetedQuotes:
            def __init__(self, client):
                self.client = client

            def __call__(self, symbol):
                self.client.consume(2)
                return _bar(symbol=symbol, close="80", offset=1)

        class MultipleExitStrategy(FixedSignalStrategy):
            def on_bar(self, bar, account):
                self.seen_symbols.append(bar.symbol)
                if bar.symbol in {"HOLD02", "HOLD03"}:
                    return [Signal.sell(bar.symbol, "stop_loss")]
                return []

        class EmptyScanner:
            label = "empty"
            kind = "test"

            def rank_symbols(self, symbols):
                return []

            def snapshot(self, symbols):
                return ScannerSnapshot()

        broker = BudgetedBroker()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=MultipleExitStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=0)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=BudgetedQuotes(broker.client),
            scanner_provider=EmptyScanner(),
            data_source_kind="live",
            execution_mode="live",
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        cycle = runtime.run_cycle()

        self.assertEqual(["HOLD02"], [order.symbol for order in broker.orders])
        self.assertTrue(
            any("live_exit_deferred_physical_read_budget" in event.message for event in cycle)
        )
        self.assertEqual("HOLD03", runtime._open_position_monitor_queue[0])

    def test_live_candidate_queue_preserves_read_budget_for_all_entry_slots(self):
        symbols = ["BUY001", "BUY002", "BUY003"]

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0
                self.completed_cycles = []

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.completed_cycles.append(self.used)
                self.limit = None

        class BudgetedBroker:
            def __init__(self):
                self.client = BudgetClient()
                self.orders = []

            def snapshot(self, *, timestamp=None):
                self.client.consume(2)
                return AccountSnapshot(cash=Decimal("1000000"))

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.client.consume(3)
                self.orders.append(order)
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        class BudgetedProvider:
            def __init__(self, client, reads):
                self.client = client
                self.reads = reads
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                self.client.consume(self.reads)
                if self.reads == 1:
                    return [
                        _bar(symbol=symbol, close="9990", offset=-2),
                        _bar(symbol=symbol, close="9995", offset=-1),
                    ]
                return _bar(symbol=symbol, close="10000", offset=1)

        class SeededFixedSignalStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 1
                min_volume_ratio = Decimal("1")

            config = Config()

            def seed_history(self, symbol, bars):
                return len(bars)

        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000"),
                open=Decimal("9990"),
                high=Decimal("10010"),
                low=Decimal("9990"),
            )
            for symbol in symbols
        }
        broker = BudgetedBroker()
        final_quotes = BudgetedProvider(broker.client, 2)
        entry_history = BudgetedProvider(broker.client, 1)
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=SeededFixedSignalStrategy(
                {symbol: [Signal.buy(symbol, "entry")] for symbol in symbols}
            ),
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=entry_history,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                max_position_amount=Decimal("1000000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=3,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(2, runtime._cycle_entry_slot_capacity)
        self.assertEqual(symbols[:2], [order.symbol for order in broker.orders])
        self.assertEqual(symbols[:2], final_quotes.requested_symbols)
        self.assertEqual(symbols[:2], entry_history.requested_symbols)
        self.assertEqual([14], broker.client.completed_cycles)
        scanner_diagnostic = next(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        )
        self.assertIn("entry_capacity_stop=1", scanner_diagnostic)

    def test_live_dense_hold_evaluation_reserves_only_history_reads(self):
        symbols = [f"HOLD{index:03d}" for index in range(1, 5)]

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0
                self.completed_cycles = []

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.completed_cycles.append(self.used)
                self.limit = None

        class BudgetedBroker:
            def __init__(self):
                self.client = BudgetClient()

            def snapshot(self, *, timestamp=None):
                self.client.consume(2)
                return AccountSnapshot(cash=Decimal("1000000"))

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                raise AssertionError("HOLD candidates must not place orders")

        class BudgetedProvider:
            def __init__(self, client, reads, values):
                self.client = client
                self.reads = reads
                self.values = values
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                self.client.consume(self.reads)
                return self.values[symbol]

        class SeededHoldStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 2
                min_volume_ratio = Decimal("1")

            config = Config()

            def seed_history(self, symbol, bars):
                return len(bars)

        scanner_bars = {
            symbol: replace(
                _bar(symbol=symbol, close="10000"),
                open=Decimal("9900"),
                high=Decimal("10100"),
                low=Decimal("9800"),
            )
            for symbol in symbols
        }
        history_bars = {
            symbol: [
                _bar(symbol=symbol, close="9900", offset=-3),
                _bar(symbol=symbol, close="9950", offset=-2),
                _bar(symbol=symbol, close="9990", offset=-1),
            ]
            for symbol in symbols
        }
        broker = BudgetedBroker()
        final_quotes = BudgetedProvider(
            broker.client,
            2,
            {symbol: _bar(symbol=symbol, close="10000", offset=1) for symbol in symbols},
        )
        entry_history = BudgetedProvider(broker.client, 1, history_bars)
        strategy = SeededHoldStrategy({})
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=4)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=entry_history,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            settings=CustomStrategySettings.default().with_updates(max_positions=4),
            scan_limit_per_cycle=4,
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=4,
            max_physical_market_reads_per_cycle=6,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(symbols, strategy.seen_symbols)
        self.assertEqual(symbols, entry_history.requested_symbols)
        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual([6], broker.client.completed_cycles)

    def test_flow_scalper_provisional_missing_quote_hold_avoids_final_quote(self):
        symbol = "HOLD001"
        current = replace(
            _bar(symbol=symbol, close="103", offset=3),
            timestamp=datetime(2026, 6, 11, 9, 3, 20),
            open=Decimal("102"),
            high=Decimal("104"),
            low=Decimal("101"),
            vwap=Decimal("102"),
            bid=None,
            ask=None,
            temporary_stop=None,
            trading_state_source="",
        )
        history = [
            replace(_bar(symbol=symbol, close="100", offset=0), volume=1000),
            replace(_bar(symbol=symbol, close="101", offset=1), volume=1000),
            replace(_bar(symbol=symbol, close="102", offset=2), volume=3000),
        ]
        final_quotes = DictBarProvider({symbol: _bar(symbol=symbol, close="103", offset=3)})
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.50"),
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda _symbol: history,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([], final_quotes.requested_symbols)
        self.assertEqual(current, strategy._last_live_quotes[symbol])
        self.assertEqual({}, runtime.broker.snapshot().positions)
        diagnostic = next(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        )
        self.assertIn("confirmation_candidates=0", diagnostic)

    def test_flow_scalper_provisional_buy_wide_final_quote_blocks_order(self):
        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol, close="106", offset=3),
            timestamp=datetime(2026, 6, 11, 9, 3, 20),
            open=Decimal("105"),
            high=Decimal("107"),
            low=Decimal("104"),
            volume=4000,
            vwap=Decimal("105"),
            bid=None,
            ask=None,
            temporary_stop=None,
            trading_state_source="",
        )
        history = [
            replace(_bar(symbol=symbol, close="100", offset=0), volume=1000),
            replace(_bar(symbol=symbol, close="102", offset=1), volume=1000),
            replace(_bar(symbol=symbol, close="104", offset=2), volume=3000),
        ]
        final_bar = replace(
            current,
            timestamp=datetime(2026, 6, 11, 9, 3, 25),
            bid=Decimal("103"),
            ask=Decimal("109"),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                max_spread_bps=Decimal("10"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda _symbol: history,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertTrue(any("final_quote_strategy_changed" in event.message for event in events))

    def test_flow_scalper_sparse_scanner_bar_is_confirmed_before_strategy(self):
        symbol = "SPARSE1"
        scanner_bar = replace(
            _bar(symbol=symbol, close="103", offset=3),
            timestamp=datetime(2026, 6, 11, 9, 3, 20),
            bid=None,
            ask=None,
            temporary_stop=None,
            trading_state_source="",
        )
        history = [
            replace(_bar(symbol=symbol, close="100", offset=0), volume=1000),
            replace(_bar(symbol=symbol, close="101", offset=1), volume=1000),
            replace(_bar(symbol=symbol, close="102", offset=2), volume=3000),
        ]
        final_bar = replace(
            scanner_bar,
            timestamp=datetime(2026, 6, 11, 9, 3, 25),
            open=Decimal("102"),
            high=Decimal("104"),
            low=Decimal("101"),
            bid=Decimal("102.9"),
            ask=Decimal("103.1"),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                min_momentum_pct=Decimal("0.50"),
                volume_window=2,
                min_volume_ratio=Decimal("1"),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda _symbol: history,
            scanner_provider=StaticScannerProvider(bars={symbol: scanner_bar}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual(final_bar, strategy._last_live_quotes[symbol])
        diagnostic = next(
            event.message
            for event in events
            if event.kind == "system" and "scanner_diagnostic - external_scan_cycle" in event.message
        )
        self.assertIn("confirmation_reasons=scanner_bar_sparse:1", diagnostic)

    def test_flow_scalper_provisional_buy_missing_final_book_blocks_order(self):
        symbol = "BUY001"
        current = replace(
            _bar(symbol=symbol, close="106", offset=3),
            timestamp=datetime(2026, 6, 11, 9, 3, 20),
            open=Decimal("105"),
            high=Decimal("107"),
            low=Decimal("104"),
            volume=4000,
            vwap=Decimal("105"),
            bid=None,
            ask=None,
            temporary_stop=None,
            trading_state_source="",
        )
        history = [
            replace(_bar(symbol=symbol, close="100", offset=0), volume=1000),
            replace(_bar(symbol=symbol, close="102", offset=1), volume=1000),
            replace(_bar(symbol=symbol, close="104", offset=2), volume=3000),
        ]
        final_bar = replace(
            current,
            timestamp=datetime(2026, 6, 11, 9, 3, 25),
            temporary_stop=False,
            trading_state_source="KIS_CURRENT_PRICE",
        )
        final_quotes = DictBarProvider({symbol: final_bar})
        strategy = FlowScalperStrategy(
            FlowScalperConfig(
                momentum_window=2,
                volume_window=2,
                min_volume_ratio=Decimal("1"),
                transaction_tax_pct=Decimal("0"),
                slippage_pct=Decimal("0"),
                min_net_profit_pct=Decimal("0"),
            )
        )
        runtime = PaperTradingRuntime(
            symbols=[symbol],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=strategy,
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=lambda _symbol: history,
            scanner_provider=StaticScannerProvider(bars={symbol: current}),
            settings=CustomStrategySettings.default().with_updates(max_positions=1),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=1,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual([symbol], final_quotes.requested_symbols)
        self.assertEqual({}, runtime.broker.snapshot().positions)
        self.assertTrue(
            any("final_quote_executable_quote_unavailable" in event.message for event in events)
        )

    def test_live_candidate_evaluation_defers_buy_only_after_preflight_budget_check(self):
        symbols = ["HOLD001", "HOLD002", "HOLD003", "BUY004"]

        class BudgetClient:
            def __init__(self):
                self.limit = None
                self.used = 0
                self.completed_cycles = []

            def begin_market_read_budget(self, limit):
                self.limit = limit
                self.used = 0

            def market_read_budget_state(self):
                if self.limit is None:
                    return None
                return self.used, self.limit

            def consume(self, count):
                if self.limit is not None and self.used + count > self.limit:
                    raise RuntimeError("KIS physical market read budget exhausted")
                if self.limit is not None:
                    self.used += count

            def end_market_read_budget(self):
                self.completed_cycles.append(self.used)
                self.limit = None

        class BudgetedBroker:
            def __init__(self):
                self.client = BudgetClient()
                self.orders = []

            def snapshot(self, *, timestamp=None):
                self.client.consume(2)
                return AccountSnapshot(cash=Decimal("1000000"))

            def sync_pending_order_statuses(self):
                return RuntimeSyncResult()

            def update_market(self, bar):
                return None

            def place_order(self, order, bar):
                self.client.consume(3)
                self.orders.append(order)
                return Fill(
                    order=order,
                    accepted=True,
                    timestamp=bar.timestamp,
                    price=bar.close,
                    quantity=order.quantity,
                )

        class BudgetedProvider:
            def __init__(self, client, reads):
                self.client = client
                self.reads = reads
                self.requested_symbols = []

            def __call__(self, symbol):
                self.requested_symbols.append(symbol)
                self.client.consume(self.reads)
                if self.reads == 1:
                    return [
                        _bar(symbol=symbol, close="9990", offset=-2),
                        _bar(symbol=symbol, close="9995", offset=-1),
                    ]
                return _bar(symbol=symbol, close="10000", offset=1)

        class SeededFixedSignalStrategy(FixedSignalStrategy):
            class Config:
                momentum_window = 1
                volume_window = 1
                trend_boundary_window = 1
                min_volume_ratio = Decimal("1")

            config = Config()

            def seed_history(self, symbol, bars):
                return len(bars)

        scanner_bars = {
            symbol: _bar(symbol=symbol, close="10000")
            for symbol in symbols
        }
        broker = BudgetedBroker()
        final_quotes = BudgetedProvider(broker.client, 2)
        entry_history = BudgetedProvider(broker.client, 1)
        strategy = SeededFixedSignalStrategy(
            {"BUY004": [Signal.buy("BUY004", "entry_after_holds")]}
        )
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=strategy,
            risk_manager=RiskManager(
                RiskConfig(
                    max_order_amount=Decimal("0"),
                    max_position_amount=Decimal("1000000"),
                    max_positions=0,
                )
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            entry_history_provider=entry_history,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            settings=CustomStrategySettings.default().with_updates(
                max_positions=0,
                max_position_amount=Decimal("1000000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="live",
            execution_mode="live",
            max_final_quote_requests_per_cycle=4,
            max_physical_market_reads_per_cycle=14,
        )
        runtime.start()

        events = runtime.run_cycle()

        self.assertEqual(symbols, final_quotes.requested_symbols)
        self.assertEqual(symbols, entry_history.requested_symbols)
        self.assertEqual(symbols, strategy.seen_symbols)
        self.assertEqual([], broker.orders)
        self.assertEqual([14], broker.client.completed_cycles)
        self.assertEqual("BUY004", runtime._scan_cursor_anchor)
        self.assertTrue(
            any("physical_entry_capacity_reached" in event.message for event in events)
        )

    def test_unlimited_authoritative_slots_use_actual_entry_candidates(self):
        symbols = ["NOSIG1", "BUY001"]
        scanner_bars = {
            "NOSIG1": replace(
                _bar(symbol="NOSIG1", close="100000"),
                open=Decimal("99000"),
                high=Decimal("101000"),
                low=Decimal("99000"),
            ),
            "BUY001": replace(
                _bar(symbol="BUY001", close="400000"),
                open=Decimal("399000"),
                high=Decimal("401000"),
                low=Decimal("399000"),
            ),
        }
        final_quotes = DictBarProvider({"BUY001": _bar(symbol="BUY001", close="400000", offset=1)})
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.managed_position_ledger = InMemoryManagedLivePositionLedger()
        runtime = PaperTradingRuntime(
            symbols=symbols,
            broker=broker,
            strategy=FixedSignalStrategy({"BUY001": [Signal.buy("BUY001", "only_candidate")]}),
            risk_manager=RiskManager(
                RiskConfig(max_positions=0, max_position_amount=Decimal("1000000"))
            ),
            bar_provider=DictBarProvider({}),
            final_quote_provider=final_quotes,
            scanner_provider=StaticScannerProvider(bars=scanner_bars),
            settings=CustomStrategySettings.default().with_updates(
                cash_allocation_pct=Decimal("0.70"),
                max_positions=0,
                max_position_amount=Decimal("1000000"),
                max_symbol_exposure=Decimal("1.0"),
            ),
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=2,
            max_final_quote_requests_per_cycle=2,
        )
        runtime.start()

        runtime.run_cycle()

        self.assertEqual(["BUY001"], final_quotes.requested_symbols)
        self.assertEqual(2, broker.snapshot().positions["BUY001"].quantity)

    def test_authoritative_cursor_keeps_adjusted_symbol_anchor_after_rank_refresh(self):
        class ReorderingScanner:
            label = "reordering"
            kind = "test"

            def __init__(self):
                self.refresh_count = 0
                self.current_order = []

            def rank_symbols(self, symbols):
                requested = list(symbols)
                if not requested:
                    orders = (["EXPENS", "BUY001", "BUY002"], ["BUY001", "EXPENS", "BUY002"])
                    self.current_order = list(orders[min(self.refresh_count, 1)])
                    self.refresh_count += 1
                    return list(self.current_order)
                return [symbol for symbol in self.current_order if symbol in requested]

            def snapshot(self, symbols):
                return ScannerSnapshot(
                    bars={symbol: _bar(symbol=symbol, close="100000") for symbol in symbols}
                )

        scanner = ReorderingScanner()
        runtime = PaperTradingRuntime(
            symbols=["EXPENS", "BUY001", "BUY002"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig(max_positions=1)),
            bar_provider=DictBarProvider({}),
            scanner_provider=scanner,
            data_source_kind="live",
            execution_mode="live",
            scan_limit_per_cycle=1,
        )
        runtime.seed_entry_prices(
            {
                "EXPENS": Decimal("900000"),
                "BUY001": Decimal("100000"),
                "BUY002": Decimal("100000"),
            }
        )

        runtime._ranked_symbols_cache = {}
        runtime._refresh_authoritative_symbol_universe()
        first = runtime._symbols_from_cursor(runtime._entry_scan_order(runtime.symbols), limit=1)
        runtime._ranked_symbols_cache = {}
        runtime._refresh_authoritative_symbol_universe()
        second = runtime._symbols_from_cursor(runtime._entry_scan_order(runtime.symbols), limit=1)

        self.assertEqual(["BUY001"], first)
        self.assertEqual(["BUY002"], second)

    def test_apply_strategy_settings_syncs_live_broker_order_time_risk_limits(self):
        broker = PaperBroker(initial_cash=Decimal("1000000"))
        broker.config = BotConfig(
            allow_live_trading=True,
            live_trading_enabled=True,
            max_order_amount=Decimal("100000"),
            max_position_amount=Decimal("300000"),
            max_daily_loss=Decimal("100000"),
            kill_switch=False,
        )
        runtime = PaperTradingRuntime(
            symbols=[],
            broker=broker,
            strategy=FixedSignalStrategy({}),
            risk_manager=RiskManager(RiskConfig()),
            bar_provider=DictBarProvider({}),
            execution_mode="live",
        )
        risk_config = RiskConfig(
            max_order_amount=Decimal("25000"),
            max_position_amount=Decimal("125000"),
            max_daily_loss=Decimal("25000"),
            kill_switch=True,
        )

        runtime.apply_strategy_settings(
            settings=CustomStrategySettings.default().with_updates(kill_switch=True),
            risk_config=risk_config,
            profile_label="live risk sync",
        )

        self.assertEqual(risk_config, runtime.risk_manager.config)
        self.assertEqual(Decimal("25000"), broker.config.max_order_amount)
        self.assertEqual(Decimal("125000"), broker.config.max_position_amount)
        self.assertEqual(Decimal("25000"), broker.config.max_daily_loss)
        self.assertTrue(broker.config.kill_switch)
        self.assertTrue(broker.config.allow_live_trading)
        self.assertTrue(broker.config.live_trading_enabled)


if __name__ == "__main__":
    unittest.main()
