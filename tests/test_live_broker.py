import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.config import BotConfig
from stockbot.kis import KisApiError, KisOrderSubmissionUncertain
from stockbot.live_audit import JsonlLiveAuditLog
from stockbot.live_broker import LiveBroker
from stockbot.live_order_state import (
    InMemoryManualReconciliationStore,
    InMemoryPendingLiveOrderStore,
    JsonManualReconciliationStore,
    JsonPendingLiveOrderStore,
    ManualReconciliationBlocker,
    PendingLiveOrder,
)
from stockbot.live_position_ledger import (
    InMemoryManagedLivePositionLedger,
    JsonManagedLivePositionLedger,
    managed_live_position_ledger_scope,
)
from stockbot.live_safety import LIVE_CONFIRMATION_PHRASE
from stockbot.models import AccountSnapshot, MarketBar, Order, Position


LIVE_ENV = {
    "KIS_LIVE_APP_KEY": "live-app-key",
    "KIS_LIVE_APP_SECRET": "live-app-secret",
    "KIS_LIVE_ACCOUNT_NO": "test-live-account40",
    "KIS_LIVE_ACCOUNT_PRODUCT_CODE": "01",
    "STOCKBOT_ALLOW_LIVE_TRADING": "true",
    "STOCKBOT_LIVE_TRADING_ENABLED": "true",
    "STOCKBOT_LIVE_TRADING_CONFIRM": LIVE_CONFIRMATION_PHRASE,
    "STOCKBOT_LIVE_ACCOUNT_CONFIRMATION": "40",
}


def live_config() -> BotConfig:
    return BotConfig(
        trading_mode="live",
        allow_live_trading=True,
        live_trading_enabled=True,
        max_order_amount=Decimal("100000"),
    )


def bar(symbol: str = "005930", price: str = "70000") -> MarketBar:
    value = Decimal(price)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 7, 2, 9, 1, tzinfo=timezone.utc),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=100,
        vwap=value,
        bid=value,
        ask=value,
        temporary_stop=False,
        trading_state_source="KIS_CURRENT_PRICE",
    )


class FakeLiveOrderClient:
    def __init__(self, account: AccountSnapshot, *, error: Exception | None = None):
        self.account = account
        self.error = error
        self.account_snapshot_error = None
        self.account_snapshot_calls = []
        self.buyable_calls = []
        self.calls = []
        self.cancelable_response = {"rt_cd": "0", "output": []}
        self.cancelable_responses = None
        self.cancelable_calls = []
        self.cancel_response = {"rt_cd": "0", "output": {}}
        self.cancel_calls = []
        self.cancel_error = None

    def account_snapshot(self, *, timestamp=None):
        self.account_snapshot_calls.append(timestamp)
        if self.account_snapshot_error is not None:
            raise self.account_snapshot_error
        return self.account

    def place_cash_order(self, order, *, order_price, order_division="00", exchange="KRX"):
        self.calls.append(
            {
                "order": order,
                "order_price": order_price,
                "order_division": order_division,
                "exchange": exchange,
            }
        )
        if self.error is not None:
            raise self.error
        return {"rt_cd": "0", "output": {"ODNO": "123"}}

    def inquire_buyable_order(self, symbol, *, order_price, order_division="00"):
        self.buyable_calls.append(
            {
                "symbol": symbol,
                "order_price": order_price,
                "order_division": order_division,
            }
        )
        return {"rt_cd": "0", "output": {"ord_psbl_cash": str(self.account.buying_power)}}

    def inquire_cancelable_orders(self, **kwargs):
        self.cancelable_calls.append(kwargs)
        if self.cancelable_responses:
            return self.cancelable_responses.pop(0)
        return self.cancelable_response

    def cancel_cash_order(self, **kwargs):
        self.cancel_calls.append(kwargs)
        if self.cancel_error is not None:
            raise self.cancel_error
        return self.cancel_response


class MarketStateFakeLiveOrderClient(FakeLiveOrderClient):
    def __init__(self, account: AccountSnapshot, *, market_state_response):
        super().__init__(account)
        self.market_state_response = market_state_response
        self.market_state_calls = []

    def inquire_price(self, symbol):
        self.market_state_calls.append(symbol)
        return self.market_state_response


class BuyableFakeLiveOrderClient(FakeLiveOrderClient):
    def __init__(
        self,
        account: AccountSnapshot,
        *,
        buyable_output: object,
        buyable_error: Exception | None = None,
    ):
        super().__init__(account)
        self.buyable_output = buyable_output
        self.buyable_error = buyable_error
        self.buyable_calls = []

    def inquire_buyable_order(self, symbol, *, order_price, order_division="00"):
        self.buyable_calls.append(
            {
                "symbol": symbol,
                "order_price": order_price,
                "order_division": order_division,
            }
        )
        if self.buyable_error is not None:
            raise self.buyable_error
        output = dict(self.buyable_output) if isinstance(self.buyable_output, dict) else self.buyable_output
        return {"rt_cd": "0", "output": output}


class FakeReconciler:
    def __init__(
        self,
        *,
        status="filled",
        order_no="123",
        filled_quantity=1,
        unfilled_quantity=0,
        average_fill_price=Decimal("70010"),
    ):
        self.status = status
        self.order_no = order_no
        self.filled_quantity = filled_quantity
        self.unfilled_quantity = unfilled_quantity
        self.average_fill_price = average_fill_price
        self.calls = []

    def reconcile(self, order, submission_response, *, query_date=None):
        self.calls.append(
            {
                "order": order,
                "submission_response": submission_response,
                "query_date": query_date,
            }
        )

        class Result:
            order_no = self.order_no
            status = self.status
            filled_quantity = self.filled_quantity
            unfilled_quantity = self.unfilled_quantity
            average_fill_price = self.average_fill_price

        return Result()


class EntryCountFakeReconciler(FakeReconciler):
    def __init__(self, *, entry_counts=None, entry_count_error=None, **kwargs):
        super().__init__(**kwargs)
        self.entry_counts = dict(entry_counts or {})
        self.entry_count_error = entry_count_error
        self.entry_count_calls = []

    def reconcile_entry_counts(self, trading_day):
        self.entry_count_calls.append(trading_day)
        if self.entry_count_error is not None:
            raise self.entry_count_error
        return SimpleNamespace(
            trading_day=trading_day,
            entry_counts=dict(self.entry_counts),
        )


class RaisingReconciler:
    def reconcile(self, order, submission_response, *, query_date=None):
        raise RuntimeError("reconciliation service unavailable")


class FailingAuditLog(JsonlLiveAuditLog):
    def __init__(self, path: Path, *, fail_events: set[str]):
        super().__init__(path, redact_values=LIVE_ENV.values())
        self.fail_events = fail_events
        self.calls = []

    def record(self, event, payload):
        self.calls.append(event)
        if event in self.fail_events:
            raise OSError(f"audit write failed for {event}")
        return super().record(event, payload)


class FailingUpsertAfterSetupStore(JsonPendingLiveOrderStore):
    def __init__(self, path: str | Path):
        super().__init__(path)
        self.fail_next_upsert = False

    def upsert(self, order):
        if self.fail_next_upsert:
            self.fail_next_upsert = False
            raise PermissionError("cannot replace pending order file")
        return super().upsert(order)


class FailingSecondUpsertStore(JsonPendingLiveOrderStore):
    def __init__(self, path: str | Path):
        super().__init__(path)
        self.upsert_count = 0

    def upsert(self, order):
        self.upsert_count += 1
        if self.upsert_count == 2:
            raise PermissionError("cannot replace pending order file")
        return super().upsert(order)


class FailingAllStore(JsonPendingLiveOrderStore):
    def __init__(self, path: str | Path, *, fail_on: int):
        super().__init__(path)
        self.all_count = 0
        self.fail_on = fail_on

    def all(self):
        self.all_count += 1
        if self.all_count == self.fail_on:
            raise PermissionError("cannot read pending order file")
        return super().all()


class FailingManualReconciliationStore(JsonManualReconciliationStore):
    def latch(self, blocker):
        raise PermissionError("cannot replace manual reconciliation file")


class UnavailableManualReconciliationStore(JsonManualReconciliationStore):
    is_durable = True

    def ensure_ready(self):
        raise PermissionError("cannot create manual reconciliation file")


def audit_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def durable_pending_store(directory: str | Path) -> JsonPendingLiveOrderStore:
    return JsonPendingLiveOrderStore(Path(directory) / "pending-live-orders.json")


def durable_manual_reconciliation_store(directory: str | Path) -> JsonManualReconciliationStore:
    return JsonManualReconciliationStore(Path(directory) / "manual-reconciliation.json")


def durable_managed_ledger(
    directory: str | Path,
    positions: dict[str, int] | None = None,
    *,
    scope: str = "",
) -> JsonManagedLivePositionLedger:
    ledger = JsonManagedLivePositionLedger(Path(directory) / "managed-live-positions.json", scope=scope)
    ledger.ensure_ready()
    for symbol, quantity in (positions or {}).items():
        ledger.add(symbol, quantity)
    return ledger


class LiveBrokerTest(unittest.TestCase):
    @staticmethod
    def pending_order(submitted_at: datetime) -> PendingLiveOrder:
        return PendingLiveOrder(
            order_no="",
            symbol="005930",
            side="buy",
            requested_quantity=1,
            remaining_quantity=1,
            submitted_at=submitted_at,
            estimated_price=Decimal("70000"),
        )

    def pending_terminal_confirmation_broker(
        self,
        directory: str | Path,
        *,
        status: str = "unknown",
        reason: str = "cancel_requested",
        side: str = "SELL",
        account_quantity: int = 3,
        ledger_quantity: int = 3,
        account_snapshot_error: Exception | None = None,
    ):
        reference_bar = bar()
        pending = PendingLiveOrder(
            order_no=f"{self.__class__.__name__}-pending",
            symbol=reference_bar.symbol,
            side=side,
            requested_quantity=3,
            remaining_quantity=3,
            submitted_at=reference_bar.timestamp - timedelta(minutes=16),
            estimated_price=reference_bar.close,
            reason=reason,
            order_org_no=f"{self.__class__.__name__}-org",
        )
        pending_store = durable_pending_store(directory)
        pending_store.upsert(pending)
        positions = {}
        if account_quantity > 0:
            positions[pending.symbol] = Position(
                symbol=pending.symbol,
                quantity=account_quantity,
                sellable_quantity=account_quantity,
                avg_price=reference_bar.close,
                last_price=reference_bar.close,
                opened_at=reference_bar.timestamp,
                highest_price=reference_bar.close,
            )
        client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"), positions=positions))
        client.account_snapshot_error = account_snapshot_error
        audit_path = Path(directory) / "live.jsonl"
        broker = LiveBroker(
            client=client,
            config=live_config(),
            env=LIVE_ENV,
            audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
            market_is_open=lambda: True,
            session_approved=lambda: True,
            account_confirmation="40",
            expected_account_suffix="40",
            fill_reconciler=FakeReconciler(status=status, filled_quantity=0, unfilled_quantity=0),
            pending_order_store=pending_store,
            manual_reconciliation_store=durable_manual_reconciliation_store(directory),
            managed_position_ledger=durable_managed_ledger(
                directory,
                {pending.symbol: ledger_quantity} if ledger_quantity > 0 else {},
            ),
            risk_limits_ok=lambda: True,
        )
        return broker, client, pending_store, pending, audit_path

    @staticmethod
    def ready_broker(directory: str | Path, client, *, fill_reconciler=None) -> LiveBroker:
        return LiveBroker(
            client=client,
            config=live_config(),
            env=LIVE_ENV,
            audit_log=JsonlLiveAuditLog(
                Path(directory) / "live.jsonl",
                redact_values=LIVE_ENV.values(),
            ),
            market_is_open=lambda: True,
            session_approved=lambda: True,
            account_confirmation="40",
            expected_account_suffix="40",
            fill_reconciler=fill_reconciler or FakeReconciler(),
            pending_order_store=durable_pending_store(directory),
            manual_reconciliation_store=durable_manual_reconciliation_store(directory),
            managed_position_ledger=durable_managed_ledger(directory),
            risk_limits_ok=lambda: True,
            new_entries_allowed=lambda: True,
        )

    def test_pending_fill_timestamp_converts_korea_execution_time_to_pending_timezone(self):
        pending = self.pending_order(datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc))
        reconciliation = SimpleNamespace(execution=SimpleNamespace(order_time="120800"))

        timestamp = LiveBroker._pending_fill_timestamp(pending, date(2026, 7, 10), reconciliation)

        self.assertEqual(datetime(2026, 7, 10, 3, 8, tzinfo=timezone.utc), timestamp)

    def test_pending_fill_timestamp_falls_back_when_execution_time_is_absent_or_invalid(self):
        submitted_at = datetime(2026, 7, 10, 3, 0, tzinfo=timezone.utc)
        pending = self.pending_order(submitted_at)
        reconciliations = (
            SimpleNamespace(execution=SimpleNamespace()),
            SimpleNamespace(execution=SimpleNamespace(order_time="250000")),
        )

        for reconciliation in reconciliations:
            with self.subTest(reconciliation=reconciliation):
                self.assertEqual(
                    submitted_at,
                    LiveBroker._pending_fill_timestamp(pending, date(2026, 7, 10), reconciliation),
                )

    def test_pending_fill_timestamp_preserves_naive_korea_local_time(self):
        pending = self.pending_order(datetime(2026, 7, 10, 11, 55))
        reconciliation = SimpleNamespace(execution=SimpleNamespace(order_time="120800"))

        timestamp = LiveBroker._pending_fill_timestamp(pending, date(2026, 7, 10), reconciliation)

        self.assertEqual(datetime(2026, 7, 10, 12, 8), timestamp)

    def test_preflight_denial_does_not_call_live_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=BotConfig.default(),
                env={},
                audit_log=JsonlLiveAuditLog(audit_path),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)

        self.assertFalse(fill.accepted)
        self.assertIn("live_preflight_denied", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_order_preflight_denied", [row["event"] for row in rows])

    def test_missing_fill_reconciliation_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)

        self.assertFalse(fill.accepted)
        self.assertIn("live fill reconciliation is not implemented", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_order_preflight_denied", [row["event"] for row in rows])

    def test_fill_reconciliation_flag_without_reconciler_still_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciliation_available=lambda: True,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertIn("live fill reconciliation is not implemented", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_update_market_is_noop_for_runtime_compatibility(self):
        account = AccountSnapshot(cash=Decimal("1000000"))
        broker = LiveBroker(
            client=FakeLiveOrderClient(account),
            config=live_config(),
            env=LIVE_ENV,
            audit_log=JsonlLiveAuditLog(Path(tempfile.gettempdir()) / "stockbot-test-live-noop.jsonl"),
            market_is_open=lambda: True,
        )

        broker.update_market(bar())
        snapshot = broker.snapshot()

        self.assertEqual(account.cash, snapshot.cash)
        self.assertEqual(account.positions, snapshot.positions)
        self.assertEqual(Decimal("0"), snapshot.buying_power)

    def test_snapshot_without_orderable_cash_does_not_expose_deposit_cash(self):
        account = AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000"))
        broker = LiveBroker(
            client=FakeLiveOrderClient(account),
            config=live_config(),
            env=LIVE_ENV,
            audit_log=JsonlLiveAuditLog(Path(tempfile.gettempdir()) / "stockbot-test-live-cash.jsonl"),
            market_is_open=lambda: True,
        )

        snapshot = broker.snapshot()

        self.assertEqual(Decimal("1000000"), snapshot.cash)
        self.assertEqual(Decimal("0"), snapshot.buying_power)
        self.assertEqual(Decimal("0"), snapshot.buying_power_override)

    def test_account_with_fresh_buying_power_caches_exact_cash_for_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output={"ord_psbl_cash": "250000", "ord_psbl_qty": "3"},
            )
            broker = self.ready_broker(tmp, client)

            account, blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())
            cached_snapshot = broker.snapshot()

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("1000000"), account.cash)
        self.assertEqual(Decimal("250000"), account.buying_power)
        self.assertEqual(Decimal("250000"), cached_snapshot.buying_power)
        self.assertEqual(Decimal("70100"), client.buyable_calls[0]["order_price"])

    def test_planning_buying_power_prefers_no_receivable_buy_amount(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output={
                    "ord_psbl_cash": "151",
                    "nrcvb_buy_amt": "36047",
                    "ord_psbl_qty": "0",
                    "nrcvb_buy_qty": "1",
                },
            )
            broker = self.ready_broker(tmp, client)

            account, blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("36047"), account.buying_power)

    def test_pending_batch_does_not_clamp_exact_cash_to_balance_orderable_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("32981"),
                    equity_override=Decimal("100000"),
                    buying_power_override=Decimal("151"),
                ),
                buyable_output={
                    "ord_psbl_cash": "151",
                    "nrcvb_buy_amt": "36047",
                    "ord_psbl_qty": "0",
                    "nrcvb_buy_qty": "1",
                },
            )
            broker = self.ready_broker(tmp, client)
            balance_snapshot = broker.snapshot()
            broker.begin_pending_order_batch()

            refreshed, blocker = broker.refresh_planning_account(balance_snapshot, bar(price="30000"))
            cached_snapshot = broker.snapshot()
            broker.end_pending_order_batch()

        self.assertEqual(Decimal("151"), balance_snapshot.buying_power)
        self.assertEqual("", blocker)
        self.assertEqual(Decimal("36047"), refreshed.buying_power)
        self.assertEqual(Decimal("36047"), cached_snapshot.buying_power)

    def test_snapshot_outside_pending_batch_prefers_fresh_balance_buying_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("100000"),
                    equity_override=Decimal("100000"),
                    buying_power_override=Decimal("10000"),
                ),
                buyable_output={"nrcvb_buy_amt": "100000", "nrcvb_buy_qty": "1"},
            )
            broker = self.ready_broker(tmp, client)
            refreshed, blocker = broker.refresh_planning_account(client.account, bar())

            fresh_balance_snapshot = broker.snapshot()

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("100000"), refreshed.buying_power)
        self.assertEqual(Decimal("10000"), fresh_balance_snapshot.buying_power)

    def test_new_batch_without_pending_buy_accepts_increased_exact_buying_power(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"nrcvb_buy_amt": "151", "nrcvb_buy_qty": "0"},
            )
            broker = self.ready_broker(tmp, client)
            broker.begin_pending_order_batch()
            first, first_blocker = broker.refresh_planning_account(broker.snapshot(), bar())
            broker.end_pending_order_batch()
            client.buyable_output = {"nrcvb_buy_amt": "36047", "nrcvb_buy_qty": "1"}

            broker.begin_pending_order_batch()
            second, second_blocker = broker.refresh_planning_account(broker.snapshot(), bar())
            broker.end_pending_order_batch()

        self.assertEqual("", first_blocker)
        self.assertEqual(Decimal("151"), first.buying_power)
        self.assertEqual("", second_blocker)
        self.assertEqual(Decimal("36047"), second.buying_power)

    def test_planning_buying_power_does_not_fail_when_first_symbol_costs_more_than_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output={"ord_psbl_cash": "5000", "ord_psbl_qty": "0"},
            )
            broker = self.ready_broker(tmp, client)

            account, blocker = broker.account_with_fresh_buying_power(
                broker.snapshot(),
                bar(price="10000"),
            )

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("5000"), account.buying_power)

    def test_planning_buying_power_rejects_max_buy_amount_without_orderable_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output={
                    "max_buy_amt": "250000",
                    "max_buy_qty": "3",
                    "ord_psbl_qty": "3",
                },
            )
            broker = self.ready_broker(tmp, client)

            account, blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())

        self.assertEqual("live_buyable_cash_unknown", blocker)
        self.assertEqual(Decimal("0"), account.buying_power)

    def test_account_with_fresh_buying_power_failure_invalidates_cached_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output={"ord_psbl_cash": "250000", "ord_psbl_qty": "3"},
            )
            broker = self.ready_broker(tmp, client)
            refreshed, initial_blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())
            client.buyable_error = RuntimeError("buyable inquiry unavailable")

            blocked, blocker = broker.account_with_fresh_buying_power(refreshed, bar())
            cached_snapshot = broker.snapshot()

        self.assertEqual("", initial_blocker)
        self.assertIn("live_buyable_inquiry_failed", blocker)
        self.assertEqual(Decimal("0"), blocked.buying_power)
        self.assertEqual(Decimal("0"), cached_snapshot.buying_power)

    def test_account_with_fresh_buying_power_malformed_output_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), equity_override=Decimal("1000000")),
                buyable_output=None,
            )
            broker = self.ready_broker(tmp, client)

            account, blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())

        self.assertEqual("live_buyable_inquiry_malformed", blocker)
        self.assertEqual(Decimal("0"), account.buying_power)

    def test_buy_preflight_requeries_and_successful_submission_reduces_cached_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "2"},
            )
            broker = self.ready_broker(tmp, client)
            planned, blocker = broker.account_with_fresh_buying_power(broker.snapshot(), bar())

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            cached_snapshot = broker.snapshot()

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("100000"), planned.buying_power)
        self.assertTrue(fill.accepted)
        self.assertEqual(2, len(client.buyable_calls))
        self.assertEqual(Decimal("29900"), cached_snapshot.buying_power)

    def test_uncertain_buy_submission_reduces_cached_cash_conservatively(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "2"},
            )
            client.error = KisOrderSubmissionUncertain("KIS live order submission uncertain: timeout")
            broker = self.ready_broker(tmp, client)

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            cached_snapshot = broker.snapshot()

        self.assertFalse(fill.accepted)
        self.assertEqual("live_order_submission_uncertain", fill.reject_reason)
        self.assertLessEqual(cached_snapshot.buying_power, Decimal("29900"))

    def test_approved_buy_calls_live_client_and_writes_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            reconciler = FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010"))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))
        self.assertEqual("005930", client.calls[0]["order"].symbol)
        self.assertEqual(Decimal("70100"), client.calls[0]["order_price"])
        events = [row["event"] for row in rows]
        self.assertIn("live_order_preflight_approved", events)
        self.assertIn("live_order_submitted", events)

    def test_approved_buy_uses_bounded_marketable_limit_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    max_order_amount=Decimal("100000"),
                    slippage_pct=Decimal("0.001"),
                ),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar("005930", "70000"))
            rows = audit_rows(audit_path)

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("70100"), client.calls[0]["order_price"])
        approved = next(row for row in rows if row["event"] == "live_order_preflight_approved")
        self.assertEqual("70100", approved["payload"]["submitted_price"])
        self.assertEqual("70000", approved["payload"]["reference_price"])

    def test_approved_buy_caps_marketable_limit_at_daily_upper_limit(self):
        upper_limit_bar = bar("073240", "7790")
        upper_limit_bar = MarketBar(
            **{
                **upper_limit_bar.__dict__,
                "upper_limit": Decimal("7790"),
                "lower_limit": Decimal("4200"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    max_order_amount=Decimal("100000"),
                    slippage_pct=Decimal("0.001"),
                ),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("7790")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("073240", 1, "entry"), upper_limit_bar)

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("7790"), client.calls[0]["order_price"])

    def test_buy_fails_closed_when_reference_exceeds_daily_upper_limit(self):
        invalid_bar = bar("073240", "7800")
        invalid_bar = MarketBar(
            **{
                **invalid_bar.__dict__,
                "upper_limit": Decimal("7790"),
                "lower_limit": Decimal("4200"),
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("073240", 1, "entry"), invalid_bar)

        self.assertFalse(fill.accepted)
        self.assertEqual("live_quote_above_daily_upper_limit", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_approved_sell_uses_bounded_marketable_limit_price(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    sellable_quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    max_order_amount=Decimal("100000"),
                    slippage_pct=Decimal("0.001"),
                ),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("69900")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 1}),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
            )

            fill = broker.place_order(Order.sell("005930", 1, "take_profit"), bar("005930", "70000"))

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("69900"), client.calls[0]["order_price"])

    def test_buyable_inquiry_and_preflight_use_marketable_limit_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("70100"), equity_override=Decimal("70100")),
                buyable_output={"ord_psbl_cash": "70000", "ord_psbl_qty": "1"},
            )
            broker = LiveBroker(
                client=client,
                config=BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    max_order_amount=Decimal("100000"),
                    slippage_pct=Decimal("0.001"),
                ),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70000")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar("005930", "70000"))

        self.assertFalse(fill.accepted)
        self.assertEqual("live_buyable_cash", fill.reject_reason)
        self.assertEqual(Decimal("70100"), client.buyable_calls[0]["order_price"])
        self.assertEqual([], client.calls)

    def test_buy_uses_kis_orderable_cash_not_deposit_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("1000000"),
                    equity_override=Decimal("1000000"),
                    buying_power_override=Decimal("0"),
                )
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            snapshot = broker.snapshot()
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertEqual(Decimal("0"), snapshot.buying_power)
        self.assertFalse(fill.accepted)
        self.assertEqual("live_buyable_cash", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_buy_uses_kis_buyable_inquiry_when_balance_snapshot_lacks_orderable_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("100000"),
                    equity_override=Decimal("100000"),
                    buying_power_override=Decimal("0"),
                ),
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "2"},
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.buyable_calls))
        self.assertEqual("005930", client.buyable_calls[0]["symbol"])
        self.assertEqual(Decimal("70100"), client.buyable_calls[0]["order_price"])
        self.assertEqual(1, len(client.calls))

    def test_refresh_planning_account_uses_and_caches_exact_orderable_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1200000"),
            )
            client = BuyableFakeLiveOrderClient(
                account,
                buyable_output={"ord_psbl_cash": "250000", "ord_psbl_qty": "3"},
            )
            broker = self.ready_broker(tmp, client)

            refreshed, blocker = broker.refresh_planning_account(account, bar())
            cached = broker.snapshot()

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("250000"), refreshed.buying_power)
        self.assertEqual(Decimal("250000"), cached.buying_power)
        self.assertEqual(1, len(client.buyable_calls))

    def test_refresh_planning_account_fails_closed_when_buyable_inquiry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1200000"),
            )
            client = BuyableFakeLiveOrderClient(
                account,
                buyable_output={},
                buyable_error=KisApiError("buyable unavailable"),
            )
            broker = self.ready_broker(tmp, client)

            refreshed, blocker = broker.refresh_planning_account(account, bar())

        self.assertEqual(Decimal("0"), refreshed.buying_power)
        self.assertIn("live_buyable_inquiry_failed", blocker)

    def test_pending_buy_reserves_cached_orderable_cash_after_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1200000"),
            )
            client = BuyableFakeLiveOrderClient(
                account,
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "1"},
            )
            broker = self.ready_broker(
                tmp,
                client,
                fill_reconciler=FakeReconciler(
                    status="pending",
                    filled_quantity=0,
                    unfilled_quantity=1,
                ),
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            cached = broker.snapshot()

        self.assertFalse(fill.accepted)
        self.assertEqual("live_order_pending", fill.reject_reason)
        self.assertEqual(Decimal("29900"), cached.buying_power)

    def test_scoped_pending_buy_allows_unrelated_buy_without_restoring_stale_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1200000"),
            )
            client = BuyableFakeLiveOrderClient(
                account,
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "10"},
            )
            broker = self.ready_broker(
                tmp,
                client,
                fill_reconciler=FakeReconciler(
                    status="pending",
                    filled_quantity=0,
                    unfilled_quantity=1,
                ),
            )
            broker.begin_pending_order_batch()

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(
                Order.buy("000660", 1, "entry"),
                bar("000660", "29000"),
            )
            cached = broker.snapshot()
            broker.end_pending_order_batch()

        self.assertTrue(first.pending_order_tracked)
        self.assertTrue(second.pending_order_tracked)
        self.assertEqual(2, len(client.calls))
        self.assertGreaterEqual(cached.buying_power, Decimal("0"))
        self.assertLess(cached.buying_power, Decimal("1000"))

    def test_scoped_pending_buy_preserves_reserved_cash_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            account = AccountSnapshot(
                cash=Decimal("1000000"),
                equity_override=Decimal("1200000"),
            )
            client = BuyableFakeLiveOrderClient(
                account,
                buyable_output={"nrcvb_buy_amt": "100000", "nrcvb_buy_qty": "10"},
            )
            broker = self.ready_broker(
                tmp,
                client,
                fill_reconciler=FakeReconciler(
                    status="pending",
                    filled_quantity=0,
                    unfilled_quantity=1,
                ),
            )
            broker.begin_pending_order_batch()
            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            broker.end_pending_order_batch()

            broker.begin_pending_order_batch()
            second = broker.place_order(
                Order.buy("000660", 1, "entry"),
                bar("000660", "40000"),
            )
            broker.end_pending_order_batch()

        self.assertTrue(first.pending_order_tracked)
        self.assertFalse(second.accepted)
        self.assertEqual("live_buyable_cash", second.reject_reason)
        self.assertEqual(1, len(client.calls))

    def test_cold_cache_blocks_new_buy_until_tracked_pending_buy_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.ensure_ready()
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="existing-pending-buy",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime.now(timezone.utc),
                    estimated_price=Decimal("70100"),
                    reason="pending",
                )
            )
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"nrcvb_buy_amt": "100000", "nrcvb_buy_qty": "10"},
            )
            broker = self.ready_broker(tmp, client)
            broker.begin_pending_order_batch()

            blocked = broker.place_order(
                Order.buy("000660", 1, "entry"),
                bar("000660", "10000"),
            )
            broker.end_pending_order_batch()
            pending_store.remove("existing-pending-buy")
            client.buyable_output = {"nrcvb_buy_amt": "36047", "nrcvb_buy_qty": "3"}
            broker.begin_pending_order_batch()
            refreshed, blocker = broker.refresh_planning_account(broker.snapshot(), bar("000660", "10000"))
            broker.end_pending_order_batch()

        self.assertFalse(blocked.accepted)
        self.assertEqual("live_buyable_cash", blocked.reject_reason)
        self.assertEqual([], client.calls)
        self.assertEqual("", blocker)
        self.assertEqual(Decimal("36047"), refreshed.buying_power)

    def test_pending_store_failure_invalidates_existing_buying_power_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"nrcvb_buy_amt": "100000", "nrcvb_buy_qty": "10"},
            )
            broker = self.ready_broker(tmp, client)
            refreshed, blocker = broker.refresh_planning_account(broker.snapshot(), bar())
            failing_store = FailingAllStore(Path(tmp) / "failing-pending.json", fail_on=1)
            failing_store.ensure_ready()
            broker.pending_order_store = failing_store

            broker.begin_pending_order_batch()
            cached_buying_power = broker.cached_buying_power()
            broker.end_pending_order_batch()

        self.assertEqual("", blocker)
        self.assertEqual(Decimal("100000"), refreshed.buying_power)
        self.assertEqual(Decimal("0"), cached_buying_power)

    def test_buy_blocks_when_kis_buyable_quantity_is_too_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "0"},
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)

        self.assertFalse(fill.accepted)
        self.assertEqual("live_buyable_quantity", fill.reject_reason)
        self.assertEqual(1, len(client.buyable_calls))
        self.assertEqual([], client.calls)
        self.assertIn("live_order_blocked_by_buyable_inquiry", [row["event"] for row in rows])

    def test_buy_preflight_prefers_no_receivable_buy_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={
                    "ord_psbl_cash": "100000",
                    "nrcvb_buy_amt": "100000",
                    "ord_psbl_qty": "0",
                    "nrcvb_buy_qty": "1",
                },
            )
            broker = self.ready_broker(tmp, client)

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))

    def test_buy_preflight_falls_back_to_orderable_cash_and_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = BuyableFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("100000"), equity_override=Decimal("100000")),
                buyable_output={"ord_psbl_cash": "100000", "ord_psbl_qty": "1"},
            )
            broker = self.ready_broker(tmp, client)

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))

    def test_approved_buy_uses_reconciled_fill_before_accepting_trade(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            reconciler = FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010"))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)

        self.assertTrue(fill.accepted)
        self.assertEqual(Decimal("70010"), fill.price)
        self.assertEqual(1, fill.quantity)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(reconciler.calls))
        reconciled = next(row for row in rows if row["event"] == "live_order_reconciled")
        self.assertEqual("70100", reconciled["payload"]["submitted_price"])
        self.assertEqual("70000", reconciled["payload"]["reference_price"])

    def test_snapshot_marks_only_bot_managed_live_quantity(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=5,
                    sellable_quantity=5,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        ledger = InMemoryManagedLivePositionLedger({"005930": 2})
        broker = LiveBroker(
            client=FakeLiveOrderClient(account),
            config=live_config(),
            env=LIVE_ENV,
            audit_log=JsonlLiveAuditLog(Path(tempfile.gettempdir()) / "stockbot-test-live-managed.jsonl"),
            market_is_open=lambda: True,
            managed_position_ledger=ledger,
        )

        snapshot = broker.snapshot()

        self.assertEqual(2, snapshot.positions["005930"].managed_quantity)

    def test_managed_live_ledger_rejects_mismatched_account_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            first_scope = managed_live_position_ledger_scope("account-a", "01")
            second_scope = managed_live_position_ledger_scope("account-b", "01")
            first_ledger = JsonManagedLivePositionLedger(ledger_path, scope=first_scope)
            first_ledger.ensure_ready()
            first_ledger.add("005930", 1)

            second_ledger = JsonManagedLivePositionLedger(ledger_path, scope=second_scope)

            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                second_ledger.ensure_ready()

    def test_managed_live_ledger_drift_blocks_before_live_client_call(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    sellable_quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 2}),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
            )

            fill = broker.place_order(Order.sell("005930", 1, "take_profit"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_managed_position_ledger_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_managed_position_ledger_drift_denied", events)

    def test_sell_rejects_unmanaged_live_holding_before_client_call(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    sellable_quantity=2,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            fill = broker.place_order(Order.sell("005930", 1, "take_profit"), bar())

        self.assertFalse(fill.accepted)
        self.assertIn("managed_position", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_adopt_existing_account_positions_marks_sellable_quantity_managed(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=5,
                    sellable_quantity=3,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            ledger = durable_managed_ledger(tmp)
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=BotConfig(
                    trading_mode="live",
                    allow_live_trading=True,
                    live_trading_enabled=True,
                    max_order_amount=Decimal("300000"),
                ),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=3, average_fill_price=Decimal("70000")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=ledger,
            )

            adopted = broker.adopt_existing_account_positions()
            lifecycle_bar = replace(
                bar("005930", "71000"),
                bid=Decimal("70900"),
            )
            broker.update_market(lifecycle_bar)
            snapshot = broker.snapshot()
            lifecycle = ledger.lifecycle_for("005930")
            fill = broker.place_order(Order.sell("005930", 3, "take_profit"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual({"005930": 3}, adopted)
        self.assertEqual(3, snapshot.positions["005930"].managed_quantity)
        self.assertEqual(opened_at, snapshot.positions["005930"].opened_at)
        self.assertEqual(Decimal("71000"), snapshot.positions["005930"].highest_price)
        self.assertEqual(Decimal("70000"), snapshot.positions["005930"].lowest_price)
        self.assertEqual(opened_at, lifecycle.opened_at)
        self.assertEqual(Decimal("71000"), lifecycle.highest_price)
        self.assertEqual(Decimal("70000"), lifecycle.lowest_price)
        self.assertTrue(fill.accepted)
        self.assertEqual(0, ledger.quantity_for("005930"))
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_existing_positions_adopted", events)

    def test_adoption_preserves_buy_fill_until_account_quantity_catches_up(self):
        symbol = "005930"
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(ledger_path, scope="account-scope")
            ledger.ensure_ready()
            ledger.record_fill_transaction(
                fill_key="buy-fill",
                symbol=symbol,
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=bar().timestamp,
                price=Decimal("70000"),
            )
            restarted_ledger = JsonManagedLivePositionLedger(
                ledger_path,
                scope="account-scope",
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(
                    Path(tmp) / "live.jsonl",
                    redact_values=LIVE_ENV.values(),
                ),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=restarted_ledger,
            )

            broker.adopt_existing_account_positions(
                account=AccountSnapshot(cash=Decimal("1000000"))
            )

            self.assertEqual(1, restarted_ledger.quantity_for(symbol))
            self.assertEqual(
                1,
                restarted_ledger.account_quantity_confirmation_for(symbol),
            )

            quantity_only_caught_up = AccountSnapshot(
                cash=Decimal("930000"),
                positions={
                    symbol: Position(
                        symbol=symbol,
                        quantity=1,
                        sellable_quantity=0,
                        avg_price=Decimal("70000"),
                        last_price=Decimal("70000"),
                        opened_at=bar().timestamp,
                        highest_price=Decimal("70000"),
                    )
                },
            )
            broker.adopt_existing_account_positions(
                account=quantity_only_caught_up
            )

            self.assertEqual(1, restarted_ledger.quantity_for(symbol))
            self.assertEqual(
                1,
                restarted_ledger.account_quantity_confirmation_for(symbol),
            )

            caught_up = AccountSnapshot(
                cash=Decimal("930000"),
                positions={
                    symbol: Position(
                        symbol=symbol,
                        quantity=1,
                        sellable_quantity=1,
                        avg_price=Decimal("70000"),
                        last_price=Decimal("70000"),
                        opened_at=bar().timestamp,
                        highest_price=Decimal("70000"),
                    )
                },
            )
            broker.adopt_existing_account_positions(account=caught_up)

            self.assertEqual(1, restarted_ledger.quantity_for(symbol))
            self.assertIsNone(
                restarted_ledger.account_quantity_confirmation_for(symbol)
            )

    def test_adoption_does_not_revive_sold_quantity_from_stale_account_after_restart(self):
        symbol = "005930"
        stale_position = Position(
            symbol=symbol,
            quantity=2,
            sellable_quantity=0,
            avg_price=Decimal("69000"),
            last_price=Decimal("70000"),
            opened_at=bar().timestamp,
            highest_price=Decimal("70000"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger = JsonManagedLivePositionLedger(ledger_path, scope="account-scope")
            ledger.ensure_ready()
            ledger.add(symbol, 2)
            ledger.record_fill_transaction(
                fill_key="sell-fill",
                symbol=symbol,
                side="SELL",
                quantity_delta=2,
                cumulative_filled=2,
                timestamp=bar().timestamp,
                price=Decimal("70000"),
            )
            restarted_ledger = JsonManagedLivePositionLedger(
                ledger_path,
                scope="account-scope",
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(
                    AccountSnapshot(
                        cash=Decimal("1000000"),
                        positions={symbol: stale_position},
                    )
                ),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(
                    Path(tmp) / "live.jsonl",
                    redact_values=LIVE_ENV.values(),
                ),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=restarted_ledger,
            )

            broker.adopt_existing_account_positions(
                account=AccountSnapshot(
                    cash=Decimal("1000000"),
                    positions={symbol: stale_position},
                )
            )

            self.assertEqual(0, restarted_ledger.quantity_for(symbol))
            self.assertEqual(
                0,
                restarted_ledger.account_quantity_confirmation_for(symbol),
            )

            broker.adopt_existing_account_positions(
                account=AccountSnapshot(
                    cash=Decimal("1000000"),
                    positions={
                        symbol: replace(stale_position, sellable_quantity=2),
                    },
                )
            )

            self.assertEqual(0, restarted_ledger.quantity_for(symbol))
            self.assertEqual(
                0,
                restarted_ledger.account_quantity_confirmation_for(symbol),
            )

            broker.adopt_existing_account_positions(
                account=AccountSnapshot(cash=Decimal("1140000"))
            )

            self.assertEqual(0, restarted_ledger.quantity_for(symbol))
            self.assertIsNone(
                restarted_ledger.account_quantity_confirmation_for(symbol)
            )

    def test_order_waits_for_account_quantity_to_confirm_previous_fill(self):
        symbol = "005930"
        opened_at = bar().timestamp
        stale_position = Position(
            symbol=symbol,
            quantity=2,
            sellable_quantity=2,
            avg_price=Decimal("69000"),
            last_price=Decimal("70000"),
            opened_at=opened_at,
            highest_price=Decimal("70000"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = durable_managed_ledger(tmp, {symbol: 2})
            ledger.record_fill_transaction(
                fill_key="2026-07-02:122:005930:SELL",
                symbol=symbol,
                side="SELL",
                quantity_delta=1,
                cumulative_filled=1,
                timestamp=opened_at,
                price=Decimal("70000"),
            )
            client = FakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("1000000"),
                    positions={symbol: stale_position},
                )
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(
                    Path(tmp) / "live.jsonl",
                    redact_values=LIVE_ENV.values(),
                ),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    filled_quantity=1,
                    average_fill_price=Decimal("70000"),
                ),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=ledger,
            )
            broker.adopt_existing_account_positions(account=client.account)

            blocked = broker.place_order(
                Order.sell(symbol, 1, "strategy_exit"),
                bar(),
            )

            self.assertFalse(blocked.accepted)
            self.assertEqual(
                "live_account_quantity_confirmation_pending",
                blocked.reject_reason,
            )
            self.assertEqual([], client.calls)

            client.account = AccountSnapshot(
                cash=Decimal("1070000"),
                positions={
                    symbol: replace(
                        stale_position,
                        quantity=1,
                        sellable_quantity=0,
                    ),
                },
            )
            still_blocked = broker.place_order(
                Order.sell(symbol, 1, "strategy_exit"),
                bar(),
            )

            self.assertFalse(still_blocked.accepted)
            self.assertEqual(
                "live_account_quantity_confirmation_pending",
                still_blocked.reject_reason,
            )
            self.assertEqual([], client.calls)

            client.account = AccountSnapshot(
                cash=Decimal("1070000"),
                positions={
                    symbol: replace(
                        stale_position,
                        quantity=1,
                        sellable_quantity=1,
                    ),
                },
            )
            accepted = broker.place_order(
                Order.sell(symbol, 1, "strategy_exit"),
                bar(),
            )

            self.assertTrue(accepted.accepted)
            self.assertEqual(1, len(client.calls))

    def test_adopt_existing_positions_allows_pending_sell_but_keeps_it_tracked(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    sellable_quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            ledger = durable_managed_ledger(tmp)
            ledger.add("005930", 2)
            broker = LiveBroker(
                client=FakeLiveOrderClient(account),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                managed_position_ledger=ledger,
            )

            adopted = broker.adopt_existing_account_positions(account=account)

            self.assertEqual({"005930": 2}, adopted)
            self.assertEqual(2, ledger.quantity_for("005930"))
            self.assertEqual(1, len(pending_store.all()))

    def test_buy_fill_records_managed_live_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = durable_managed_ledger(tmp)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=3, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
                managed_position_ledger=ledger,
            )

            fill = broker.place_order(Order.buy("005930", 3, "entry"), bar("005930", "30000"))
            managed_quantity = ledger.quantity_for("005930")
            lifecycle = ledger.lifecycle_for("005930")
            entry_counts = ledger.entry_counts()
            consumed_quantity = ledger.consumed_quantity_for("2026-07-02:123:005930:BUY")

        self.assertTrue(fill.accepted)
        self.assertEqual(3, managed_quantity)
        self.assertEqual(3, consumed_quantity)
        self.assertEqual({("005930", date(2026, 7, 2)): 1}, entry_counts)
        self.assertEqual(fill.timestamp, lifecycle.opened_at)
        self.assertEqual(fill.price, lifecycle.highest_price)
        self.assertEqual(fill.price, lifecycle.lowest_price)

    def test_buy_fails_closed_when_migrated_ledger_entry_count_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: date(2026, 7, 2),
            )
            ledger.ensure_ready()
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_entry_count_unknown", fill.reject_reason)
        self.assertEqual([], client.buyable_calls)
        self.assertEqual([], client.calls)

    def test_authoritative_entry_count_reconciliation_unlocks_migrated_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            trading_day = date(2026, 7, 2)
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: trading_day,
            )
            ledger.ensure_ready()
            reconciler = EntryCountFakeReconciler(entry_counts={"005930": 1})
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            reconciled = broker.reconcile_managed_entry_counts(trading_day)
            fill = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "30000"))

            self.assertTrue(reconciled)
            self.assertTrue(fill.accepted)
            self.assertTrue(ledger.entry_counts_are_known(trading_day))
            self.assertEqual(
                {
                    ("005930", trading_day): 1,
                    ("000660", trading_day): 1,
                },
                ledger.entry_counts(),
            )
            self.assertEqual([trading_day], reconciler.entry_count_calls)

    def test_failed_entry_count_reconciliation_keeps_migrated_day_locked(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            trading_day = date(2026, 7, 2)
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: trading_day,
            )
            ledger.ensure_ready()
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=EntryCountFakeReconciler(
                    entry_count_error=RuntimeError("incomplete KIS daily order pages")
                ),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            reconciled = broker.reconcile_managed_entry_counts(trading_day)
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

            self.assertFalse(reconciled)
            self.assertFalse(fill.accepted)
            self.assertEqual("live_entry_count_unknown", fill.reject_reason)
            self.assertFalse(ledger.entry_counts_are_known(trading_day))

    def test_migrated_ledger_allows_buy_on_next_trading_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: date(2026, 7, 2),
            )
            ledger.ensure_ready()
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            next_day_bar = replace(bar(), timestamp=bar().timestamp + timedelta(days=1))

            fill = broker.place_order(Order.buy("005930", 1, "entry"), next_day_bar)
            entry_counts = ledger.entry_counts()

        self.assertTrue(fill.accepted)
        self.assertEqual({("005930", date(2026, 7, 3)): 1}, entry_counts)

    def test_migrated_unknown_lifecycle_blocks_scale_in_after_entry_count_rollover(self):
        reference_bar = bar()
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    sellable_quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=reference_bar.close,
                    opened_at=reference_bar.timestamp,
                    highest_price=reference_bar.close,
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {"005930": 1},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: date(2026, 7, 2),
            )
            ledger.ensure_ready()
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            next_day_bar = replace(reference_bar, timestamp=reference_bar.timestamp + timedelta(days=1))

            fill = broker.place_order(Order.buy("005930", 1, "scale_in"), next_day_bar)

        self.assertFalse(fill.accepted)
        self.assertEqual("live_position_lifecycle_unknown", fill.reject_reason)
        self.assertEqual([], client.buyable_calls)
        self.assertEqual([], client.calls)

    def test_migrated_ledger_unknown_state_still_allows_managed_sell(self):
        reference_bar = bar()
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            realized_pnl_today_known=False,
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=1,
                    sellable_quantity=1,
                    avg_price=Decimal("69000"),
                    last_price=reference_bar.close,
                    opened_at=reference_bar.timestamp,
                    highest_price=reference_bar.close,
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "managed-live-positions.json"
            ledger_path.write_text(
                json.dumps(
                    {
                        "positions": {"005930": 1},
                        "consumed_fills": {},
                        "realized_pnl_by_date": {},
                    }
                ),
                encoding="utf-8",
            )
            ledger = JsonManagedLivePositionLedger(
                ledger_path,
                trading_day_provider=lambda: date(2026, 7, 2),
            )
            ledger.ensure_ready()
            client = FakeLiveOrderClient(account)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
            )

            fill = broker.place_order(Order.sell("005930", 1, "protective_exit"), reference_bar)

        self.assertTrue(fill.accepted)
        self.assertEqual(0, ledger.quantity_for("005930"))
        self.assertEqual(1, len(client.calls))

    def test_buy_fails_closed_when_fresh_account_daily_pnl_is_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("1000000"),
                    realized_pnl_today_known=False,
                )
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_daily_realized_pnl_unknown", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_buy_fails_closed_when_fresh_account_crosses_daily_loss_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(
                AccountSnapshot(
                    cash=Decimal("1000000"),
                    realized_pnl_today=Decimal("-100001"),
                )
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_daily_loss_limit_reached", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_sell_fill_reduces_managed_quantity_and_reports_realized_pnl(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            realized_pnl_today_known=False,
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    sellable_quantity=2,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = durable_managed_ledger(tmp, {"005930": 2})
            broker = LiveBroker(
                client=FakeLiveOrderClient(account),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=ledger,
            )

            fill = broker.place_order(Order.sell("005930", 1, "take_profit"), bar())
            managed_quantity = ledger.quantity_for("005930")
            snapshot = broker.snapshot(timestamp=bar().timestamp)

        self.assertTrue(fill.accepted)
        self.assertEqual(1, managed_quantity)
        self.assertEqual(Decimal("1010"), fill.realized_pnl)
        self.assertEqual(Decimal("1010"), snapshot.realized_pnl_today)

    def test_pre_submission_guard_audit_failure_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            audit_log = FailingAuditLog(audit_path, fail_events={"live_pending_order_tracked"})
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=audit_log,
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010")),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual([], client.calls)
        self.assertEqual("live_submission_guard_unavailable", fill.reject_reason)
        self.assertIn("live_pending_order_tracked", audit_log.calls)

    def test_temporary_market_stop_blocks_before_account_or_order_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = self.ready_broker(tmp, client)
            stopped_bar = replace(
                bar(),
                market="KOSDAQ",
                temporary_stop=True,
                trading_state_source="KIS_CURRENT_PRICE",
                security_status_code="51",
            )

            fill = broker.place_order(
                Order.buy(stopped_bar.symbol, 1, "entry"),
                stopped_bar,
            )

            self.assertFalse(fill.accepted)
            self.assertEqual("live_market_temporarily_stopped", fill.reject_reason)
            self.assertEqual([], client.account_snapshot_calls)
            self.assertEqual([], client.buyable_calls)
            self.assertEqual([], client.calls)
            self.assertEqual(
                ["live_order_blocked_by_market_state"],
                [row["event"] for row in audit_rows(Path(tmp) / "live.jsonl")],
            )

    def test_market_state_reconciliation_rejection_requires_fresh_kis_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MarketStateFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                market_state_response={
                    "rt_cd": "0",
                    "output": {
                        "stck_prpr": "70000",
                        "temp_stop_yn": "Y",
                        "rprs_mrkt_kor_name": "코스닥",
                    },
                },
            )
            broker = self.ready_broker(
                tmp,
                client,
                fill_reconciler=FakeReconciler(
                    status="rejected",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual(
            "live_market_state_rejected: temporary stop confirmed by KIS current price",
            fill.reject_reason,
        )
        self.assertEqual(["005930"], client.market_state_calls)

    def test_reconciliation_rejection_with_normal_fresh_state_remains_hard(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = MarketStateFakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                market_state_response={
                    "rt_cd": "0",
                    "output": {
                        "stck_prpr": "70000",
                        "temp_stop_yn": "N",
                    },
                },
            )
            broker = self.ready_broker(
                tmp,
                client,
                fill_reconciler=FakeReconciler(
                    status="rejected",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_order_rejected", fill.reject_reason)
        self.assertEqual(["005930"], client.market_state_calls)

    def test_immediate_market_state_rejection_uses_transient_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                error=KisApiError("VI 발동으로 인한 변동성 완화장치"),
            )
            broker = self.ready_broker(tmp, client)

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual(
            "live_market_state_rejected: VI 발동으로 인한 변동성 완화장치",
            fill.reject_reason,
        )

    def test_unknown_kis_trading_state_blocks_before_account_or_order_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = self.ready_broker(tmp, client)
            unknown_bar = replace(
                bar(),
                market="KOSDAQ",
                temporary_stop=None,
                trading_state_source="KIS_CURRENT_PRICE",
            )

            fill = broker.place_order(
                Order.buy(unknown_bar.symbol, 1, "entry"),
                unknown_bar,
            )

            self.assertFalse(fill.accepted)
            self.assertEqual("live_market_state_unknown", fill.reject_reason)
            self.assertEqual([], client.account_snapshot_calls)
            self.assertEqual([], client.calls)

    def test_unverified_market_state_source_blocks_before_account_or_order_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = self.ready_broker(tmp, client)
            unverified_bar = replace(
                bar(),
                temporary_stop=False,
                trading_state_source="",
            )

            fill = broker.place_order(
                Order.buy(unverified_bar.symbol, 1, "entry"),
                unverified_bar,
            )

            self.assertFalse(fill.accepted)
            self.assertEqual("live_market_state_unknown", fill.reject_reason)
            self.assertEqual([], client.account_snapshot_calls)
            self.assertEqual([], client.calls)

    def test_post_submission_audit_failure_does_not_skip_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            reconciler = FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010"))
            audit_log = FailingAuditLog(audit_path, fail_events={"live_order_submitted"})
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=audit_log,
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(reconciler.calls))
        self.assertIn("live_order_submitted", audit_log.calls)
        self.assertIn("live_order_reconciled", audit_log.calls)

    def test_reconciliation_failure_audit_failure_still_tracks_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            audit_log = FailingAuditLog(audit_path, fail_events={"live_order_reconciliation_failed"})
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=audit_log,
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            pending = pending_store.all()

        self.assertFalse(fill.accepted)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(pending))
        self.assertEqual("123", pending[0].order_no)
        self.assertEqual("reconciliation_failed", pending[0].reason)

    def test_manual_reconciliation_audit_failure_still_tracks_manual_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            audit_log = FailingAuditLog(audit_path, fail_events={"live_order_manual_reconciliation_required"})
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=audit_log,
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="submitted_without_order_no",
                    order_no="",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second_fill = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "30000"))
            pending = pending_store.all()
            blocker = manual_store.blocker()

        self.assertFalse(fill.accepted)
        self.assertFalse(second_fill.accepted)
        self.assertEqual("live_manual_reconciliation_required", second_fill.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0].order_no.startswith("manual:"))
        self.assertEqual("submitted_without_order_no", pending[0].reason)
        self.assertIsNotNone(blocker)
        self.assertEqual("submitted_without_order_no", blocker.reason)

    def test_manual_reconciliation_store_failure_blocks_in_process_without_latched_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            manual_store = FailingManualReconciliationStore(Path(tmp) / "manual-reconciliation.json")
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="submitted_without_order_no",
                    order_no="",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second_fill = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "30000"))
            pending = pending_store.all()
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertFalse(second_fill.accepted)
        self.assertEqual("live_manual_reconciliation_required", second_fill.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0].order_no.startswith("manual:"))
        self.assertIn("live_manual_reconciliation_store_update_failed", events)
        self.assertIn("live_order_manual_reconciliation_blocker_in_memory_only", events)
        self.assertNotIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_pending_sync_audit_failure_still_returns_fill_and_clears_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=FailingAuditLog(audit_path, fail_events={"live_pending_order_synced"}),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_sync = pending_store.all()

        self.assertEqual(1, len(result.fills))
        self.assertEqual("005930", result.fills[0].order.symbol)
        self.assertEqual((), pending_after_sync)

    def test_pending_sync_preview_reports_terminal_fill_without_unresolved_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            preview = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3), consume_fills=False)
            pending_after_preview = pending_store.all()

        self.assertEqual(1, len(preview.fills))
        self.assertEqual((), preview.remaining)
        self.assertEqual(1, len(pending_after_preview))
        self.assertEqual("123", pending_after_preview[0].order_no)

    def test_pending_sync_preview_keeps_reconciliation_failure_as_remaining(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            preview = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3), consume_fills=False)
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertEqual((), preview.fills)
        self.assertEqual(1, len(preview.remaining))
        self.assertEqual("123", preview.remaining[0].order_no)
        self.assertTrue(preview.sync_unavailable)
        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_order_sync_unavailable", fill.reject_reason)

    def test_pending_sync_preview_fail_closed_when_final_pending_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingAllStore(Path(tmp) / "pending-live-orders.json", fail_on=3)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            preview = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3), consume_fills=False)
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertEqual((), preview.fills)
        self.assertEqual(1, len(preview.remaining))
        self.assertEqual("123", preview.remaining[0].order_no)
        self.assertTrue(preview.sync_unavailable)
        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_order_sync_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_place_order_fails_closed_when_empty_pending_store_final_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingAllStore(Path(tmp) / "pending-live-orders.json", fail_on=2)
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_order_store_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_order_submission_blockers_report_pending_reconciliation_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            blockers = broker.order_submission_blockers()

        rendered = " ".join(blockers)
        self.assertIn("live pending order synchronization unavailable", rendered)
        self.assertIn("live pending orders unresolved: 1", rendered)

    def test_order_submission_blockers_preserve_in_memory_manual_blocker_when_store_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            manual_store = UnavailableManualReconciliationStore(Path(tmp) / "manual-reconciliation.json")
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            broker._block_live_orders_for_manual_reconciliation(
                order=Order.buy("005930", 1, "entry"),
                order_no="manual:005930",
                reason="submission_uncertain",
            )

            blockers = broker.order_submission_blockers()

        rendered = " ".join(blockers)
        self.assertIn("live manual reconciliation required: submission_uncertain", rendered)
        self.assertIn("live manual reconciliation store unavailable", rendered)
        self.assertIn("live pending orders unresolved: 1", rendered)

    def test_cleared_manual_reconciliation_store_releases_same_broker_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            manual_store = durable_manual_reconciliation_store(tmp)
            manual_store.latch(
                ManualReconciliationBlocker(
                    reason="operator_review_required",
                    symbol="005930",
                    side="BUY",
                    quantity=1,
                    order_no="manual:005930",
                    created_at=bar().timestamp,
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            before_clear = broker.order_submission_blockers()

            manual_store.clear()
            after_clear = broker.order_submission_blockers()
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertIn("live manual reconciliation required: operator_review_required", " ".join(before_clear))
        self.assertNotIn("live manual reconciliation required", " ".join(after_clear))
        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))

    def test_pending_sync_does_not_consume_fill_without_durable_managed_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=InMemoryManagedLivePositionLedger(),
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_sync = pending_store.all()
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual((), result.fills)
        self.assertTrue(result.sync_unavailable)
        self.assertEqual(1, len(pending_after_sync))
        self.assertEqual("123", pending_after_sync[0].order_no)
        self.assertIn("live_pending_order_sync_blocked_by_managed_ledger", events)

    def test_pending_sync_does_not_consume_fill_when_managed_ledger_write_fails(self):
        class FailingManagedLedger:
            is_durable = True

            def ensure_ready(self):
                return None

            def all(self):
                return {}

            def quantity_for(self, symbol):
                return 0

            def consumed_quantity_for(self, fill_key):
                return 0

            def entry_counts_are_known(self, trading_day):
                return True

            def position_lifecycle_is_known(self, symbol):
                return True

            def add(self, symbol, quantity):
                raise RuntimeError("managed ledger write failed")

            def subtract(self, symbol, quantity):
                raise RuntimeError("managed ledger write failed")

            def record_consumed_fill(self, **_kwargs):
                raise RuntimeError("managed ledger write failed")

            def record_fill_transaction(self, **_kwargs):
                raise RuntimeError("managed ledger write failed")

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=FailingManagedLedger(),
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_sync = pending_store.all()
            rows = audit_rows(audit_path)
            events = [row["event"] for row in rows]

        self.assertEqual((), result.fills)
        self.assertTrue(result.sync_unavailable)
        self.assertEqual(1, len(pending_after_sync))
        self.assertEqual("123", pending_after_sync[0].order_no)
        self.assertIn("live_managed_position_ledger_update_failed", events)
        ledger_failure = next(row for row in rows if row["event"] == "live_managed_position_ledger_update_failed")
        self.assertEqual("70000", ledger_failure["payload"]["submitted_price"])
        self.assertEqual("70000", ledger_failure["payload"]["reference_price"])
        self.assertIn("live_pending_order_sync_blocked_by_managed_ledger_update", events)

    def test_reconciler_without_pending_store_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_order_store_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_in_memory_pending_store_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=InMemoryPendingLiveOrderStore(),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual([], client.calls)
        self.assertEqual("live_pending_order_store_unavailable", fill.reject_reason)
        self.assertIn("live_pending_order_store_not_durable", events)

    def test_unavailable_manual_reconciliation_store_blocks_live_order_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=UnavailableManualReconciliationStore(
                    Path(tmp) / "manual-reconciliation.json"
                ),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            ready = broker.order_dependencies_ready()
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(ready)
        self.assertIn("live_manual_reconciliation_store_unavailable", events)

    def test_missing_manual_reconciliation_store_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_manual_reconciliation_store_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_manual_reconciliation_store_missing", events)
        self.assertIn("live_order_blocked_by_manual_reconciliation_store", events)

    def test_in_memory_manual_reconciliation_store_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=InMemoryManualReconciliationStore(),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_manual_reconciliation_store_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_manual_reconciliation_store_not_durable", events)
        self.assertIn("live_order_blocked_by_manual_reconciliation_store", events)

    def test_unavailable_manual_reconciliation_store_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=UnavailableManualReconciliationStore(
                    Path(tmp) / "manual-reconciliation.json"
                ),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            audit_text = audit_path.read_text(encoding="utf-8")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_manual_reconciliation_store_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_manual_reconciliation_store_unavailable", events)
        self.assertIn("live_order_blocked_by_manual_reconciliation_store", events)
        self.assertNotIn("live-app-secret", audit_text)

    def test_unavailable_audit_log_blocks_live_order_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            audit_log = FailingAuditLog(audit_path, fail_events={"live_audit_log_ready"})
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=audit_log,
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            ready = broker.order_dependencies_ready()

        self.assertFalse(ready)
        self.assertEqual(["live_audit_log_ready"], audit_log.calls)

    def test_unavailable_pending_store_blocks_before_live_client_call(self):
        class BrokenPendingStore:
            is_durable = True

            def ensure_ready(self):
                raise RuntimeError("store path contains live-app-secret")

            def upsert(self, order):
                raise AssertionError("upsert should not be called")

            def remove(self, order_no):
                raise AssertionError("remove should not be called")

            def all(self):
                return ()

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=BrokenPendingStore(),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            pending_sync = broker.sync_pending_order_statuses()
            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            audit_text = audit_path.read_text(encoding="utf-8")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertTrue(pending_sync.store_unavailable)
        self.assertEqual([], client.calls)
        self.assertIn("live_pending_order_store_unavailable", events)
        self.assertNotIn("live-app-secret", audit_text)

    def test_missing_managed_ledger_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_managed_position_ledger_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_managed_position_ledger_missing", events)

    def test_in_memory_managed_ledger_blocks_before_live_client_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                managed_position_ledger=InMemoryManagedLivePositionLedger(),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_managed_position_ledger_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_managed_position_ledger_not_durable", events)

    def test_unavailable_managed_ledger_blocks_before_live_client_call(self):
        class BrokenManagedLedger:
            is_durable = True

            def ensure_ready(self):
                raise RuntimeError("ledger path contains live-app-secret")

            def all(self):
                return {}

            def quantity_for(self, symbol):
                return 0

            def consumed_quantity_for(self, fill_key):
                return 0

            def add(self, symbol, quantity):
                raise AssertionError("add should not be called")

            def subtract(self, symbol, quantity):
                raise AssertionError("subtract should not be called")

            def record_consumed_fill(self, **_kwargs):
                raise AssertionError("record_consumed_fill should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                managed_position_ledger=BrokenManagedLedger(),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            audit_text = audit_path.read_text(encoding="utf-8")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_managed_position_ledger_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_managed_position_ledger_unavailable", events)
        self.assertNotIn("live-app-secret", audit_text)

    def test_submitted_but_unfilled_order_is_tracked_when_store_is_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            reconciler = FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1)
            pending_store = durable_pending_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)
            pending = pending_store.all()

        self.assertFalse(fill.accepted)
        self.assertEqual(1, len(pending))
        self.assertEqual("123", pending[0].order_no)
        self.assertEqual(1, pending[0].remaining_quantity)
        self.assertTrue(fill.pending_order_tracked)
        self.assertFalse(fill.requires_cycle_pause)
        self.assertIn("live_pending_order_tracked", [row["event"] for row in rows])

    def test_cycle_pending_batch_does_not_repeat_prior_pending_kis_sync_per_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="OLD123",
                    symbol="OLD001",
                    side="SELL",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("10000"),
                    reason="pending",
                )
            )
            reconciler = FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            broker.sync_pending_order_statuses()
            broker.begin_pending_order_batch()
            fill = broker.place_order(Order.buy("NEW001", 1, "entry"), bar("NEW001", "10000"))
            broker.end_pending_order_batch()

        self.assertFalse(fill.accepted)
        self.assertTrue(fill.pending_order_tracked)
        self.assertEqual(2, len(reconciler.calls))

    def test_pending_order_is_not_scoped_when_submission_guard_cleanup_fails(self):
        class GuardCleanupFailsOnceStore(JsonPendingLiveOrderStore):
            def __init__(self, path):
                super().__init__(path)
                self.remove_calls = 0

            def remove(self, order_no):
                self.remove_calls += 1
                if self.remove_calls == 1:
                    raise PermissionError("cannot remove submission guard")
                return super().remove(order_no)

        with tempfile.TemporaryDirectory() as tmp:
            pending_store = GuardCleanupFailsOnceStore(Path(tmp) / "pending-live-orders.json")
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            blocker = manual_store.blocker()

        self.assertFalse(fill.accepted)
        self.assertFalse(fill.pending_order_tracked)
        self.assertTrue(fill.requires_cycle_pause)
        self.assertIsNotNone(blocker)
        self.assertIn("submission guard cleanup", blocker.reason)

    def test_terminal_rejection_cleanup_failure_requires_cycle_pause(self):
        class TerminalCleanupFailsOnceStore(JsonPendingLiveOrderStore):
            def __init__(self, path):
                super().__init__(path)
                self.remove_calls = 0

            def remove(self, order_no):
                self.remove_calls += 1
                if self.remove_calls == 1:
                    raise PermissionError("cannot remove terminal pending state")
                return super().remove(order_no)

        with tempfile.TemporaryDirectory() as tmp:
            pending_store = TerminalCleanupFailsOnceStore(Path(tmp) / "pending-live-orders.json")
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="rejected", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            blocker = manual_store.blocker()

        self.assertFalse(fill.accepted)
        self.assertTrue(fill.requires_cycle_pause)
        self.assertIsNotNone(blocker)
        self.assertIn("terminal pending state cleanup", blocker.reason)

    def test_explicit_submission_rejection_cleanup_failure_requires_cycle_pause(self):
        class RejectionCleanupFailsStore(JsonPendingLiveOrderStore):
            def remove(self, order_no):
                raise PermissionError("cannot remove rejected submission guard")

        with tempfile.TemporaryDirectory() as tmp:
            pending_store = RejectionCleanupFailsStore(Path(tmp) / "pending-live-orders.json")
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=FakeLiveOrderClient(
                    AccountSnapshot(cash=Decimal("1000000")),
                    error=KisApiError("explicit broker rejection"),
                ),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            blocker = manual_store.blocker()

        self.assertFalse(fill.accepted)
        self.assertTrue(fill.requires_cycle_pause)
        self.assertIsNotNone(blocker)
        self.assertIn("rejected submission guard cleanup", blocker.reason)

    def test_partial_fill_tracks_remaining_live_quantity(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            reconciler = FakeReconciler(
                status="partial",
                filled_quantity=1,
                unfilled_quantity=2,
                average_fill_price=Decimal("30010"),
            )
            pending_store = durable_pending_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 3, "entry"), bar(price="30000"))
            pending = pending_store.all()

        self.assertTrue(fill.accepted)
        self.assertEqual(1, fill.quantity)
        self.assertTrue(fill.pending_order_tracked)
        self.assertFalse(fill.requires_cycle_pause)
        self.assertEqual(1, len(pending))
        self.assertEqual(2, pending[0].remaining_quantity)
        self.assertEqual("partial", pending[0].reason)

    def test_filled_existing_pending_order_blocks_new_submission_until_runtime_consumes_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            pending = pending_store.all()
            runtime_sync = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_runtime_sync = pending_store.all()

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_orders_synced", fill.reject_reason)
        self.assertEqual(1, len(pending))
        self.assertEqual("123", pending[0].order_no)
        self.assertEqual(1, len(runtime_sync.fills))
        self.assertEqual(1, runtime_sync.fills[0].quantity)
        self.assertEqual((), pending_after_runtime_sync)

    def test_reconciliation_failure_tracks_submitted_order_as_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            rows = audit_rows(audit_path)
            pending = pending_store.all()

        self.assertFalse(fill.accepted)
        self.assertIn("live_order_reconciliation_failed", fill.reject_reason)
        self.assertTrue(fill.pending_order_tracked)
        self.assertFalse(fill.requires_cycle_pause)
        self.assertEqual(1, len(pending))
        self.assertEqual("reconciliation_failed", pending[0].reason)
        self.assertIn("live_order_reconciliation_failed", [row["event"] for row in rows])

    def test_submission_without_order_number_requires_manual_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="submitted_without_order_no",
                    order_no="",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            events = [row["event"] for row in audit_rows(audit_path)]
            pending = pending_store.all()
            blocker = manual_store.blocker()

        self.assertFalse(fill.accepted)
        self.assertEqual("live_order_submitted_without_order_no", fill.reject_reason)
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0].order_no.startswith("manual:"))
        self.assertEqual("submitted_without_order_no", pending[0].reason)
        self.assertIsNotNone(blocker)
        self.assertEqual("submitted_without_order_no", blocker.reason)
        self.assertIn("live_order_manual_reconciliation_required", events)
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_manual_reconciliation_pending_order_blocks_follow_up_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="submitted_without_order_no",
                    order_no="",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "30000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_order_blocked_by_manual_reconciliation", events)

    def test_pending_tracking_failure_after_submission_blocks_follow_up_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingSecondUpsertStore(Path(tmp) / "pending-live-orders.json")
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(first.accepted)
        self.assertEqual("live_order_pending", first.reject_reason)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)
        self.assertIn("live_order_blocked_by_manual_reconciliation", events)

    def test_manual_reconciliation_blocker_survives_broker_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingSecondUpsertStore(Path(tmp) / "pending-live-orders.json")
            manual_store = durable_manual_reconciliation_store(tmp)
            first_client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            first_broker = LiveBroker(
                client=first_client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            first = first_broker.place_order(Order.buy("005930", 1, "entry"), bar())

            restarted_client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            restarted_broker = LiveBroker(
                client=restarted_client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            second = restarted_broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))

        self.assertFalse(first.accepted)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(first_client.calls))
        self.assertEqual(0, len(restarted_client.calls))

    def test_reconciliation_failure_tracking_failure_blocks_follow_up_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingSecondUpsertStore(Path(tmp) / "pending-live-orders.json")
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(first.accepted)
        self.assertIn("live_order_reconciliation_failed", first.reject_reason)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_manual_reconciliation_tracking_failure_blocks_follow_up_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingSecondUpsertStore(Path(tmp) / "pending-live-orders.json")
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="submitted_without_order_no",
                    order_no="",
                    filled_quantity=0,
                    unfilled_quantity=0,
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(first.accepted)
        self.assertEqual("live_order_submitted_without_order_no", first.reject_reason)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_partial_fill_tracking_failure_blocks_follow_up_submission(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingSecondUpsertStore(Path(tmp) / "pending-live-orders.json")
            managed_ledger = durable_managed_ledger(tmp)
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="partial",
                    filled_quantity=1,
                    unfilled_quantity=2,
                    average_fill_price=Decimal("70010"),
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=managed_ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 3, "entry"), bar("005930", "30000"))
            managed_quantity = managed_ledger.quantity_for("005930")
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertTrue(first.accepted)
        self.assertFalse(first.pending_order_tracked)
        self.assertTrue(first.requires_cycle_pause)
        self.assertEqual(1, managed_quantity)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(client.calls))
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_partial_fill_keeps_full_pending_quantity_until_ledger_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)

            class InspectingManagedLedger(JsonManagedLivePositionLedger):
                def __init__(self, path, *, pending_order_store):
                    super().__init__(path)
                    self.pending_order_store = pending_order_store
                    self.pending_during_commit = ()

                def record_fill_transaction(self, **kwargs):
                    self.pending_during_commit = self.pending_order_store.all()
                    return super().record_fill_transaction(**kwargs)

            managed_ledger = InspectingManagedLedger(
                Path(tmp) / "managed-live-positions.json",
                pending_order_store=pending_store,
            )
            managed_ledger.ensure_ready()
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status="partial",
                    filled_quantity=1,
                    unfilled_quantity=2,
                    average_fill_price=Decimal("70010"),
                ),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=managed_ledger,
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 3, "entry"), bar("005930", "30000"))
            pending_after_commit = pending_store.all()

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(managed_ledger.pending_during_commit))
        self.assertEqual("123", managed_ledger.pending_during_commit[0].order_no)
        self.assertEqual(3, managed_ledger.pending_during_commit[0].remaining_quantity)
        self.assertEqual(1, len(pending_after_commit))
        self.assertEqual("123", pending_after_commit[0].order_no)
        self.assertEqual(2, pending_after_commit[0].remaining_quantity)

    def test_sell_can_submit_when_new_entries_are_disabled_but_position_exists(self):
        opened_at = datetime(2026, 7, 2, 9, 0, tzinfo=timezone.utc)
        account = AccountSnapshot(
            cash=Decimal("1000000"),
            positions={
                "005930": Position(
                    symbol="005930",
                    quantity=2,
                    sellable_quantity=2,
                    avg_price=Decimal("69000"),
                    last_price=Decimal("70000"),
                    opened_at=opened_at,
                    highest_price=Decimal("70000"),
                )
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(account)
            reconciler = FakeReconciler(filled_quantity=1, average_fill_price=Decimal("70010"))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=reconciler,
                pending_order_store=durable_pending_store(tmp),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: False,
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 2}),
            )

            fill = broker.place_order(Order.sell("005930", 1, "exit"), bar())

        self.assertTrue(fill.accepted)
        self.assertEqual(1, len(client.calls))
        self.assertEqual("SELL", client.calls[0]["order"].side)

    def test_sync_pending_orders_clears_filled_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 3))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual((), remaining)
        self.assertIn("live_pending_order_synced", events)

    def test_pending_sell_sync_uses_saved_cost_basis_for_realized_pnl_when_account_position_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            ledger = durable_managed_ledger(tmp, {"005930": 1})
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                    cost_basis_price=Decimal("69000"),
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(
                    AccountSnapshot(
                        cash=Decimal("1000000"),
                        positions={},
                        realized_pnl_today_known=False,
                    )
                ),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(
                    status="filled",
                    filled_quantity=1,
                    unfilled_quantity=0,
                    average_fill_price=Decimal("70010"),
                ),
                pending_order_store=pending_store,
                managed_position_ledger=ledger,
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            snapshot = broker.snapshot(timestamp=bar().timestamp)

        self.assertEqual(1, len(result.fills))
        self.assertEqual(Decimal("1010"), result.fills[0].realized_pnl)
        self.assertEqual(Decimal("1010"), snapshot.realized_pnl_today)

    def test_sync_pending_order_statuses_reports_only_new_partial_fill_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="partial", filled_quantity=1, unfilled_quantity=2),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            first = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            second = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))

        self.assertEqual(1, len(first.fills))
        self.assertEqual(1, first.fills[0].quantity)
        self.assertEqual(2, first.remaining[0].remaining_quantity)
        self.assertEqual((), second.fills)
        self.assertEqual(2, second.remaining[0].remaining_quantity)

    def test_pending_partial_fill_transaction_counts_entry_once_and_updates_lifecycle(self):
        class SequentialReconciler:
            def __init__(self):
                self.results = [
                    SimpleNamespace(
                        order_no="123",
                        status="partial",
                        filled_quantity=1,
                        unfilled_quantity=2,
                        average_fill_price=Decimal("70000"),
                    ),
                    SimpleNamespace(
                        order_no="123",
                        status="filled",
                        filled_quantity=3,
                        unfilled_quantity=0,
                        average_fill_price=Decimal("71000"),
                    ),
                ]

            def reconcile(self, order, submission_response, *, query_date=None):
                return self.results.pop(0)

        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            ledger = durable_managed_ledger(tmp)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=SequentialReconciler(),
                pending_order_store=pending_store,
                managed_position_ledger=ledger,
            )

            first = broker.sync_pending_order_statuses(query_date=date(2026, 7, 2))
            second = broker.sync_pending_order_statuses(query_date=date(2026, 7, 2))
            lifecycle = ledger.lifecycle_for("005930")
            entry_counts = ledger.entry_counts()
            managed_quantity = ledger.quantity_for("005930")
            fill_key = "2026-07-02:123:005930:BUY"
            consumed_quantity = ledger.consumed_quantity_for(fill_key)
            consumed_notional = ledger.consumed_notional_for(fill_key)

        self.assertEqual(1, first.fills[0].quantity)
        self.assertEqual(Decimal("70000"), first.fills[0].price)
        self.assertEqual(2, second.fills[0].quantity)
        self.assertEqual(Decimal("71500"), second.fills[0].price)
        self.assertEqual(3, managed_quantity)
        self.assertEqual(3, consumed_quantity)
        self.assertEqual(Decimal("213000"), consumed_notional)
        self.assertEqual({("005930", date(2026, 7, 2)): 1}, entry_counts)
        self.assertEqual(Decimal("71500"), lifecycle.highest_price)
        self.assertEqual(Decimal("70000"), lifecycle.lowest_price)

    def test_pending_partial_sell_uses_incremental_fill_price_for_realized_pnl(self):
        class SequentialReconciler:
            def __init__(self):
                self.results = [
                    SimpleNamespace(
                        order_no="123",
                        status="partial",
                        filled_quantity=1,
                        unfilled_quantity=2,
                        average_fill_price=Decimal("70000"),
                    ),
                    SimpleNamespace(
                        order_no="123",
                        status="filled",
                        filled_quantity=3,
                        unfilled_quantity=0,
                        average_fill_price=Decimal("71000"),
                    ),
                ]

            def reconcile(self, order, submission_response, *, query_date=None):
                return self.results.pop(0)

        position = Position(
            symbol="005930",
            quantity=3,
            avg_price=Decimal("69000"),
            last_price=Decimal("70000"),
            opened_at=bar().timestamp,
            highest_price=Decimal("70000"),
            sellable_quantity=3,
            managed_quantity=3,
        )
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                    cost_basis_price=Decimal("69000"),
                )
            )
            ledger = durable_managed_ledger(tmp)
            ledger.add("005930", 3)
            ledger.initialize_lifecycle("005930", bar().timestamp, Decimal("70000"))
            broker = LiveBroker(
                client=FakeLiveOrderClient(
                    AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position})
                ),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=SequentialReconciler(),
                pending_order_store=pending_store,
                managed_position_ledger=ledger,
            )

            first = broker.sync_pending_order_statuses(query_date=date(2026, 7, 2))
            second = broker.sync_pending_order_statuses(query_date=date(2026, 7, 2))
            fill_key = "2026-07-02:123:005930:SELL"

            self.assertEqual(Decimal("70000"), first.fills[0].price)
            self.assertEqual(Decimal("1000"), first.fills[0].realized_pnl)
            self.assertEqual(Decimal("71500"), second.fills[0].price)
            self.assertEqual(Decimal("5000"), second.fills[0].realized_pnl)
            self.assertEqual(Decimal("6000"), ledger.realized_pnl_today(date(2026, 7, 2)))
            self.assertEqual(Decimal("213000"), ledger.consumed_notional_for(fill_key))
            self.assertEqual(0, ledger.quantity_for("005930"))

    def test_pending_partial_fill_stays_unconsumed_when_prior_notional_is_unknown(self):
        fill_key = "2026-07-02:123:005930:BUY"
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=2,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="partial",
                )
            )
            ledger = durable_managed_ledger(tmp)
            ledger.record_consumed_fill(
                fill_key=fill_key,
                symbol="005930",
                side="BUY",
                quantity_delta=1,
                cumulative_filled=1,
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(
                    status="filled",
                    filled_quantity=3,
                    unfilled_quantity=0,
                    average_fill_price=Decimal("71000"),
                ),
                pending_order_store=pending_store,
                managed_position_ledger=ledger,
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 2))

            self.assertEqual((), result.fills)
            self.assertTrue(result.sync_unavailable)
            self.assertEqual(1, len(result.remaining))
            self.assertEqual(1, ledger.quantity_for("005930"))
            self.assertEqual(1, ledger.consumed_quantity_for(fill_key))
            self.assertIsNone(ledger.consumed_notional_for(fill_key))

    def test_pending_sync_does_not_double_apply_fill_when_remove_fails_once(self):
        class RemoveFailsOnceStore(JsonPendingLiveOrderStore):
            def __init__(self, path):
                super().__init__(path)
                self.remove_calls = 0

            def remove(self, order_no):
                self.remove_calls += 1
                if self.remove_calls == 1:
                    raise PermissionError("cannot replace pending order file")
                return super().remove(order_no)

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = RemoveFailsOnceStore(Path(tmp) / "pending-live-orders.json")
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            managed_ledger = durable_managed_ledger(tmp, scope="account-scope")
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=managed_ledger,
            )

            first = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_first = pending_store.all()
            quantity_after_first = managed_ledger.quantity_for("005930")
            consumed_after_first = managed_ledger.consumed_quantity_for("2026-07-02:123:005930:BUY")
            second = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            quantity_after_second = managed_ledger.quantity_for("005930")
            consumed_after_second = managed_ledger.consumed_quantity_for("2026-07-02:123:005930:BUY")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(first.fills))
        self.assertEqual(1, first.fills[0].quantity)
        self.assertEqual(1, quantity_after_first)
        self.assertEqual(1, consumed_after_first)
        self.assertEqual(1, len(pending_after_first))
        self.assertEqual((), second.fills)
        self.assertEqual((), second.remaining)
        self.assertEqual(1, quantity_after_second)
        self.assertEqual(1, consumed_after_second)
        self.assertIn("live_pending_order_store_update_failed", events)

    def test_pending_sync_does_not_double_apply_partial_fill_when_upsert_fails_once(self):
        class UpsertFailsOnceAfterSetupStore(JsonPendingLiveOrderStore):
            def __init__(self, path):
                super().__init__(path)
                self.fail_next_upsert = False

            def upsert(self, order):
                if self.fail_next_upsert:
                    self.fail_next_upsert = False
                    raise PermissionError("cannot replace pending order file")
                return super().upsert(order)

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = UpsertFailsOnceAfterSetupStore(Path(tmp) / "pending-live-orders.json")
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            pending_store.fail_next_upsert = True
            managed_ledger = durable_managed_ledger(tmp, scope="account-scope")
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="partial", filled_quantity=1, unfilled_quantity=2),
                pending_order_store=pending_store,
                managed_position_ledger=managed_ledger,
            )

            first = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_first = pending_store.all()
            quantity_after_first = managed_ledger.quantity_for("005930")
            second = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            pending_after_second = pending_store.all()
            quantity_after_second = managed_ledger.quantity_for("005930")
            consumed_after_second = managed_ledger.consumed_quantity_for("2026-07-02:123:005930:BUY")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(first.fills))
        self.assertEqual(1, first.fills[0].quantity)
        self.assertEqual(1, quantity_after_first)
        self.assertEqual(3, pending_after_first[0].remaining_quantity)
        self.assertEqual((), second.fills)
        self.assertEqual(1, quantity_after_second)
        self.assertEqual(2, pending_after_second[0].remaining_quantity)
        self.assertEqual(1, consumed_after_second)
        self.assertIn("live_pending_order_store_update_failed", events)

    def test_sync_pending_order_statuses_queries_pending_submitted_date_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            submitted_at = datetime(2026, 7, 2, 9, 1, tzinfo=timezone.utc)
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=submitted_at,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            reconciler = FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0)
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=reconciler,
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            result = broker.sync_pending_order_statuses()

        self.assertEqual(date(2026, 7, 2), reconciler.calls[0]["query_date"])
        self.assertEqual(submitted_at, result.fills[0].timestamp)

    def test_sync_pending_orders_updates_partial_remainder(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="partial", filled_quantity=1, unfilled_quantity=2),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 3))

        self.assertEqual(1, len(remaining))
        self.assertEqual(2, remaining[0].remaining_quantity)
        self.assertEqual("partial", remaining[0].reason)

    def test_sync_pending_orders_preserves_not_found_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("30000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(status="not_found", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 3))

        self.assertEqual(1, len(remaining))
        self.assertEqual(3, remaining[0].remaining_quantity)
        self.assertEqual("not_found", remaining[0].reason)

    def test_sync_pending_orders_requests_cancel_for_stale_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            stale_submitted_at = bar().timestamp - timedelta(minutes=16)
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=stale_submitted_at,
                    estimated_price=Decimal("2180"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("123", client.cancel_calls[0]["order_no"])
        self.assertEqual("54321", client.cancel_calls[0]["order_org_no"])
        self.assertEqual(3, client.cancel_calls[0]["quantity"])
        self.assertEqual(Decimal("2180"), client.cancel_calls[0]["order_price"])
        self.assertEqual(1, len(remaining))
        self.assertEqual("cancel_requested", remaining[0].reason)
        self.assertIn("live_pending_order_cancel_requested", events)

    def test_sync_pending_orders_requests_cancel_after_two_minutes_for_marketable_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime.now() - timedelta(minutes=3),
                    estimated_price=Decimal("8210"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "pdno": "005930", "psbl_qty": "1", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date.today())

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("123", client.cancel_calls[0]["order_no"])
        self.assertEqual("54321", client.cancel_calls[0]["order_org_no"])
        self.assertEqual(1, client.cancel_calls[0]["quantity"])
        self.assertEqual(1, len(remaining))
        self.assertEqual("cancel_requested", remaining[0].reason)

    def test_sync_pending_orders_does_not_cancel_before_two_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime.now() - timedelta(seconds=90),
                    estimated_price=Decimal("8210"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "pdno": "005930", "psbl_qty": "1", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date.today())

        self.assertEqual([], client.cancelable_calls)
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(1, len(remaining))
        self.assertEqual("pending", remaining[0].reason)

    def test_sync_pending_orders_does_not_repeat_cancel_requested_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime.now() - timedelta(minutes=3),
                    estimated_price=Decimal("8210"),
                    reason="cancel_requested",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date.today())

        self.assertEqual([], client.cancelable_calls)
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(1, len(remaining))
        self.assertEqual("cancel_requested", remaining[0].reason)

    def test_sync_pending_orders_clears_cancel_requested_terminal_order_after_account_ledger_match(self):
        for status in ("unknown", "not_found"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(
                    tmp,
                    status=status,
                )

                remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
                events = [row["event"] for row in audit_rows(audit_path)]

                self.assertEqual((), remaining)
                self.assertEqual((), pending_store.all())
                self.assertEqual(1, len(client.cancelable_calls))
                self.assertEqual(1, len(client.account_snapshot_calls))
                self.assertEqual([], client.cancel_calls)
                self.assertIn("live_pending_order_cleared_after_post_cancel_confirmation", events)

    def test_sync_pending_orders_preserves_cancel_requested_terminal_order_on_account_ledger_mismatch(self):
        cases = (("BUY", 4, 3), ("SELL", 2, 3))
        for side, account_quantity, ledger_quantity in cases:
            with self.subTest(side=side), tempfile.TemporaryDirectory() as tmp:
                broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(
                    tmp,
                    side=side,
                    account_quantity=account_quantity,
                    ledger_quantity=ledger_quantity,
                )

                remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
                rows = audit_rows(audit_path)
                blocked = next(
                    row for row in rows if row["event"] == "live_pending_order_post_cancel_confirmation_blocked"
                )

                self.assertEqual(1, len(remaining))
                self.assertEqual("cancel_requested", remaining[0].reason)
                self.assertEqual(1, len(pending_store.all()))
                self.assertEqual(1, len(client.account_snapshot_calls))
                self.assertEqual([], client.cancel_calls)
                self.assertEqual("account_ledger_quantity_mismatch", blocked["payload"]["reason"])

    def test_sync_pending_orders_preserves_stale_unknown_after_account_ledger_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(
                tmp,
                reason="unknown",
                account_quantity=0,
                ledger_quantity=3,
            )

            remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
            rows = audit_rows(audit_path)
            blocked = next(
                row
                for row in rows
                if row["event"] == "live_pending_order_cancelable_absent_confirmation_blocked"
            )

            self.assertEqual(1, len(remaining))
            self.assertEqual("unknown", remaining[0].reason)
            self.assertEqual(1, len(pending_store.all()))
            self.assertEqual(1, len(client.account_snapshot_calls))
            self.assertEqual([], client.cancel_calls)
            self.assertEqual("account_ledger_quantity_mismatch", blocked["payload"]["reason"])

    def test_sync_pending_orders_preserves_cancel_requested_terminal_order_when_inquiry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(tmp)
            client.cancelable_response = {
                "rt_cd": "1",
                "msg_cd": "inquiry_failed",
                "msg1": "cancelable inquiry unavailable",
            }

            remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
            events = [row["event"] for row in audit_rows(audit_path)]

            self.assertEqual(1, len(remaining))
            self.assertEqual(1, len(pending_store.all()))
            self.assertEqual(1, len(client.cancelable_calls))
            self.assertEqual([], client.account_snapshot_calls)
            self.assertEqual([], client.cancel_calls)
            self.assertIn("live_pending_order_post_cancel_confirmation_failed", events)

    def test_sync_pending_orders_preserves_cancel_requested_terminal_order_when_still_cancelable(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(tmp)
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [
                    {
                        "odno": pending.order_no,
                        "pdno": pending.symbol,
                        "psbl_qty": str(pending.remaining_quantity),
                        "ord_gno_brno": pending.order_org_no,
                    }
                ],
            }

            remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
            rows = audit_rows(audit_path)
            blocked = next(
                row for row in rows if row["event"] == "live_pending_order_post_cancel_confirmation_blocked"
            )

            self.assertEqual(1, len(remaining))
            self.assertEqual(1, len(pending_store.all()))
            self.assertEqual(1, len(client.cancelable_calls))
            self.assertEqual([], client.account_snapshot_calls)
            self.assertEqual([], client.cancel_calls)
            self.assertEqual("order_still_cancelable", blocked["payload"]["reason"])

    def test_sync_pending_orders_preserves_cancel_requested_terminal_order_when_account_snapshot_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            broker, client, pending_store, pending, audit_path = self.pending_terminal_confirmation_broker(
                tmp,
                account_snapshot_error=RuntimeError("account snapshot unavailable"),
            )

            remaining = broker.sync_pending_orders(query_date=pending.submitted_at.date())
            rows = audit_rows(audit_path)
            failed = next(
                row for row in rows if row["event"] == "live_pending_order_post_cancel_confirmation_failed"
            )

            self.assertEqual(1, len(remaining))
            self.assertEqual(1, len(pending_store.all()))
            self.assertEqual(1, len(client.account_snapshot_calls))
            self.assertEqual([], client.cancel_calls)
            self.assertEqual("account_snapshot_failed", failed["payload"]["reason"])

    def test_sync_pending_orders_preserves_pending_when_cancel_response_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=datetime.now() - timedelta(minutes=3),
                    estimated_price=Decimal("8210"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "pdno": "005930", "psbl_qty": "1", "ord_gno_brno": "54321"}],
            }
            client.cancel_response = {"rt_cd": "1", "msg_cd": "EGW00000", "msg1": "cancel rejected"}
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date.today())
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual(1, len(remaining))
        self.assertEqual("pending", remaining[0].reason)
        self.assertIn("live_pending_order_cancel_failed", events)
        self.assertNotIn("live_pending_order_cancel_requested", events)

    def test_sync_pending_orders_clears_stale_unknown_after_account_ledger_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="unknown",
                    order_org_no="54321",
                )
            )
            position = Position(
                symbol="005930",
                quantity=3,
                sellable_quantity=3,
                avg_price=Decimal("2180"),
                last_price=Decimal("2180"),
                opened_at=bar().timestamp,
                highest_price=Decimal("2180"),
            )
            client = FakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000"), positions={"005930": position})
            )
            client.cancelable_response = {"rt_cd": "0", "output": []}
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="unknown", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual((), remaining)
        self.assertEqual((), pending_store.all())
        self.assertEqual([], client.cancel_calls)
        self.assertEqual(1, len(client.cancelable_calls))
        self.assertEqual(1, len(client.account_snapshot_calls))
        self.assertIn("live_pending_order_cleared_after_cancelable_absent", events)

    def test_sync_pending_orders_preserves_stale_unknown_when_cancelable_inquiry_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="unknown",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {"rt_cd": "1", "msg_cd": "EGW00215", "msg1": "rate limit"}
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="unknown", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(remaining))
        self.assertEqual("unknown", remaining[0].reason)
        self.assertEqual(1, len(client.cancelable_calls))
        self.assertEqual([], client.cancel_calls)
        self.assertNotIn("live_pending_order_cleared_after_cancelable_absent", events)
        self.assertIn("live_pending_order_cancelable_inquiry_failed", events)

    def test_sync_pending_orders_preserves_stale_unknown_when_cancelable_pages_not_exhausted(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="unknown",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_responses = [
                {
                    "rt_cd": "0",
                    "tr_cont": "M",
                    "ctx_area_fk100": f"next-fk-{index}",
                    "ctx_area_nk100": f"next-nk-{index}",
                    "output": [{"odno": f"999{index}", "psbl_qty": "1", "ord_gno_brno": "11111"}],
                }
                for index in range(10)
            ]
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="unknown", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            rows = audit_rows(audit_path)
            events = [row["event"] for row in rows]

        self.assertEqual(1, len(remaining))
        self.assertEqual("unknown", remaining[0].reason)
        self.assertEqual(10, len(client.cancelable_calls))
        self.assertEqual([], client.cancel_calls)
        self.assertNotIn("live_pending_order_cleared_after_cancelable_absent", events)
        self.assertIn("live_pending_order_cancelable_inquiry_failed", events)
        self.assertTrue(
            any(row.get("payload", {}).get("reason") == "continuation_page_limit_exceeded" for row in rows),
            rows,
        )

    def test_sync_pending_orders_cancels_stale_unknown_when_cancelable_quantity_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="unknown",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="unknown", filled_quantity=0, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("123", client.cancel_calls[0]["order_no"])
        self.assertEqual("54321", client.cancel_calls[0]["order_org_no"])
        self.assertEqual(3, client.cancel_calls[0]["quantity"])
        self.assertEqual(Decimal("2180"), client.cancel_calls[0]["order_price"])
        self.assertEqual(1, len(remaining))
        self.assertEqual("cancel_requested", remaining[0].reason)
        self.assertIn("live_pending_order_cancel_requested", events)

    def test_sync_pending_orders_requests_cancel_for_legacy_pending_order_when_cancelable_row_has_org_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("54321", client.cancel_calls[0]["order_org_no"])
        self.assertEqual(1, len(remaining))
        self.assertEqual("cancel_requested", remaining[0].reason)
        self.assertEqual("54321", remaining[0].order_org_no)

    def test_sync_pending_orders_requests_cancel_from_second_cancelable_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_responses = [
                {
                    "rt_cd": "0",
                    "tr_cont": "M",
                    "ctx_area_fk100": "next-fk",
                    "ctx_area_nk100": "next-nk",
                    "output": [{"odno": "999", "psbl_qty": "1", "ord_gno_brno": "11111"}],
                },
                {
                    "rt_cd": "0",
                    "tr_cont": "",
                    "ctx_area_fk100": "",
                    "ctx_area_nk100": "",
                    "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
                },
            ]
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("54321", client.cancel_calls[0]["order_org_no"])
        self.assertEqual(2, len(client.cancelable_calls))
        self.assertEqual("next-fk", client.cancelable_calls[1]["ctx_area_fk100"])
        self.assertEqual("next-nk", client.cancelable_calls[1]["ctx_area_nk100"])
        self.assertEqual("N", client.cancelable_calls[1]["tr_cont"])
        self.assertEqual("cancel_requested", remaining[0].reason)

    def test_sync_pending_orders_does_not_cancel_when_cancel_preflight_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: False,
                session_approved=lambda: False,
                account_confirmation="",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: False,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual([], client.cancel_calls)
        self.assertEqual([], client.cancelable_calls)
        self.assertEqual(1, len(remaining))
        self.assertEqual("pending", remaining[0].reason)
        self.assertIn("live_pending_order_cancel_blocked", events)

    def test_sync_pending_orders_can_cancel_in_cleanup_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            broker = LiveBroker(
                client=client,
                config=replace(live_config(), kill_switch=True),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))

        self.assertEqual(1, len(client.cancel_calls))
        self.assertEqual("cancel_requested", remaining[0].reason)

    def test_uncertain_cancel_submission_is_latched_without_duplicate_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            client.cancel_error = KisOrderSubmissionUncertain("KIS live cancel submission uncertain: timeout")
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            first = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            second = broker.sync_pending_orders(query_date=date(2026, 7, 2))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual("cancel_requested", first[0].reason)
        self.assertEqual("cancel_requested", second[0].reason)
        self.assertEqual(1, len(client.cancel_calls))
        self.assertIn("live_pending_order_cancel_submission_uncertain", events)

    def test_uncertain_cancel_with_state_write_failure_latches_manual_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = FailingUpsertAfterSetupStore(Path(tmp) / "pending-live-orders.json")
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                    order_org_no="54321",
                )
            )
            pending_store.fail_next_upsert = True
            manual_store = durable_manual_reconciliation_store(tmp)
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {
                "rt_cd": "0",
                "output": [{"odno": "123", "psbl_qty": "3", "ord_gno_brno": "54321"}],
            }
            client.cancel_error = KisOrderSubmissionUncertain("KIS live cancel submission uncertain: timeout")
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
            )

            broker.sync_pending_orders(query_date=date(2026, 7, 2))
            broker.sync_pending_orders(query_date=date(2026, 7, 2))
            blocker = manual_store.blocker()

        self.assertEqual(1, len(client.cancel_calls))
        self.assertIsNotNone(blocker)
        self.assertIn("cancel state", blocker.reason)

    def test_sync_pending_orders_does_not_cancel_without_order_org_no(self):
        with tempfile.TemporaryDirectory() as tmp:
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=3,
                    remaining_quantity=3,
                    submitted_at=bar().timestamp - timedelta(minutes=16),
                    estimated_price=Decimal("2180"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            client.cancelable_response = {"rt_cd": "0", "output": [{"odno": "123", "psbl_qty": "3"}]}
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(Path(tmp) / "live.jsonl", redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=3),
                pending_order_store=pending_store,
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp, {"005930": 3}),
                risk_limits_ok=lambda: True,
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 2))

        self.assertEqual([], client.cancel_calls)
        self.assertEqual(1, len(remaining))
        self.assertEqual("pending", remaining[0].reason)

    def test_sync_pending_orders_redacts_reconciliation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=RaisingReconciler(),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            result = broker.sync_pending_order_statuses(query_date=date(2026, 7, 3))
            audit_text = audit_path.read_text(encoding="utf-8")
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual(1, len(result.remaining))
        self.assertTrue(result.sync_unavailable)
        self.assertIn("live_pending_order_sync_failed", events)
        self.assertNotIn("live-app-secret", audit_text)

    def test_place_order_blocks_account_wide_when_pending_reconciliation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = self.ready_broker(tmp, client, fill_reconciler=RaisingReconciler())
            broker.pending_order_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="SELL",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )

            fill = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "70000"))

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_order_sync_unavailable", fill.reject_reason)
        self.assertEqual([], client.calls)

    def test_sync_pending_orders_requires_durable_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = InMemoryPendingLiveOrderStore()
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                fill_reconciler=FakeReconciler(),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
            )

            remaining = broker.sync_pending_orders(query_date=date(2026, 7, 3))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertEqual((), remaining)
        self.assertIn("live_pending_order_store_not_durable", events)
        self.assertIn("live_pending_order_sync_skipped", events)

    def test_place_order_blocks_same_symbol_when_existing_pending_order_remains_unresolved(self):
        for status in ("pending", "partial", "not_found", "unknown"):
            with self.subTest(status=status):
                self._assert_place_order_blocks_for_existing_unresolved_pending(status)

    def _assert_place_order_blocks_for_existing_unresolved_pending(self, status: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(
                    status=status,
                    filled_quantity=1 if status == "partial" else 0,
                    unfilled_quantity=1 if status in {"pending", "partial"} else 0,
                ),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar("005930", "30000"))
            events = [row["event"] for row in audit_rows(audit_path)]

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_orders_unresolved", fill.reject_reason)
        self.assertEqual([], client.calls)
        self.assertIn("live_order_blocked_by_pending_orders", events)

    def test_place_order_blocks_after_existing_pending_order_reaches_terminal_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "30000"))
            events = [row["event"] for row in audit_rows(audit_path)]
            pending = pending_store.all()

        self.assertFalse(fill.accepted)
        self.assertEqual("live_pending_orders_synced", fill.reject_reason)
        self.assertEqual(1, len(pending))
        self.assertEqual([], client.calls)
        self.assertIn("live_order_blocked_by_pending_orders", events)

    def test_order_submission_blockers_report_manual_and_pending_blocks_before_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_store = durable_pending_store(tmp)
            pending_store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=1,
                    remaining_quantity=1,
                    submitted_at=bar().timestamp,
                    estimated_price=Decimal("70000"),
                    reason="pending",
                )
            )
            manual_store = durable_manual_reconciliation_store(tmp)
            manual_store.latch(
                ManualReconciliationBlocker(
                    reason="order number missing",
                    symbol="000660",
                    side="BUY",
                    quantity=1,
                    order_no="manual-1",
                    created_at=bar().timestamp,
                )
            )
            broker = LiveBroker(
                client=FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000"))),
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            blockers = broker.order_submission_blockers()

        rendered = " ".join(blockers)
        self.assertIn("manual reconciliation", rendered)
        self.assertIn("pending orders unresolved", rendered)

    def test_live_client_errors_are_redacted_in_fill_and_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                error=KisApiError("EGW00000: appsecret=live-app-secret account_no=test-live-account40"),
            )
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=durable_pending_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            fill = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            audit_text = audit_path.read_text(encoding="utf-8")

        self.assertFalse(fill.accepted)
        self.assertNotIn("live-app-secret", fill.reject_reason)
        self.assertNotIn("test-live-account40", fill.reject_reason)
        self.assertNotIn("live-app-secret", audit_text)
        self.assertNotIn("test-live-account40", audit_text)

    def test_full_fill_keeps_submission_guard_when_managed_ledger_and_fallback_tracking_fail(self):
        class FailingManagedLedger:
            is_durable = True

            def ensure_ready(self):
                return None

            def all(self):
                return {}

            def quantity_for(self, symbol):
                return 0

            def consumed_quantity_for(self, fill_key):
                return 0

            def entry_counts_are_known(self, trading_day):
                return True

            def position_lifecycle_is_known(self, symbol):
                return True

            def add(self, symbol, quantity):
                raise RuntimeError("managed ledger write failed")

            def subtract(self, symbol, quantity):
                raise RuntimeError("managed ledger write failed")

            def record_consumed_fill(self, **_kwargs):
                raise RuntimeError("managed ledger write failed")

            def record_fill_transaction(self, **_kwargs):
                raise RuntimeError("managed ledger write failed")

        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            pending_path = Path(tmp) / "pending-live-orders.json"
            pending_store = FailingSecondUpsertStore(pending_path)
            manual_store = FailingManualReconciliationStore(Path(tmp) / "manual-reconciliation.json")
            first_client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            first_broker = LiveBroker(
                client=first_client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="filled", filled_quantity=1, unfilled_quantity=0),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=FailingManagedLedger(),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = first_broker.place_order(Order.buy("005930", 1, "entry"), bar())
            pending_after_failure = JsonPendingLiveOrderStore(pending_path).all()
            rows_after_first = audit_rows(audit_path)
            restarted_client = FakeLiveOrderClient(AccountSnapshot(cash=Decimal("1000000")))
            restarted_broker = LiveBroker(
                client=restarted_client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=JsonPendingLiveOrderStore(pending_path),
                manual_reconciliation_store=durable_manual_reconciliation_store(tmp),
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )
            second = restarted_broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "50000"))

        self.assertFalse(first.accepted)
        self.assertEqual("live_managed_position_ledger_update_failed_after_fill", first.reject_reason)
        self.assertEqual(1, len(pending_after_failure))
        self.assertEqual("submission_in_progress", pending_after_failure[0].reason)
        self.assertFalse(second.accepted)
        self.assertEqual("live_pending_orders_unresolved", second.reject_reason)
        self.assertEqual(1, len(first_client.calls))
        self.assertEqual(0, len(restarted_client.calls))
        ledger_failure = next(
            row for row in rows_after_first if row["event"] == "live_managed_position_ledger_update_failed"
        )
        self.assertEqual("70100", ledger_failure["payload"]["submitted_price"])
        self.assertEqual("70000", ledger_failure["payload"]["reference_price"])

    def test_uncertain_live_order_submission_requires_manual_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                error=KisOrderSubmissionUncertain("KIS live order submission uncertain: timeout"),
            )
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "100000"))
            events = [row["event"] for row in audit_rows(audit_path)]
            pending = pending_store.all()
            blocker = manual_store.blocker()

        self.assertFalse(first.accepted)
        self.assertEqual("live_order_submission_uncertain", first.reject_reason)
        self.assertFalse(second.accepted)
        self.assertEqual("live_manual_reconciliation_required", second.reject_reason)
        self.assertEqual(1, len(pending))
        self.assertTrue(pending[0].order_no.startswith("manual:"))
        self.assertEqual("submission_uncertain", pending[0].reason)
        self.assertIsNotNone(blocker)
        self.assertEqual("submission_uncertain", blocker.reason)
        self.assertIn("live_order_submission_uncertain", events)
        self.assertIn("live_order_manual_reconciliation_blocker_latched", events)

    def test_clearing_manual_reconciliation_does_not_release_unresolved_pending_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "live.jsonl"
            client = FakeLiveOrderClient(
                AccountSnapshot(cash=Decimal("1000000")),
                error=KisOrderSubmissionUncertain("KIS live order submission uncertain: timeout"),
            )
            pending_store = durable_pending_store(tmp)
            manual_store = durable_manual_reconciliation_store(tmp)
            broker = LiveBroker(
                client=client,
                config=live_config(),
                env=LIVE_ENV,
                audit_log=JsonlLiveAuditLog(audit_path, redact_values=LIVE_ENV.values()),
                market_is_open=lambda: True,
                session_approved=lambda: True,
                account_confirmation="40",
                expected_account_suffix="40",
                fill_reconciler=FakeReconciler(status="pending", filled_quantity=0, unfilled_quantity=1),
                pending_order_store=pending_store,
                manual_reconciliation_store=manual_store,
                managed_position_ledger=durable_managed_ledger(tmp),
                risk_limits_ok=lambda: True,
                new_entries_allowed=lambda: True,
            )

            first = broker.place_order(Order.buy("005930", 1, "entry"), bar())
            manual_store.clear()
            blockers = broker.order_submission_blockers()
            second = broker.place_order(Order.buy("000660", 1, "entry"), bar("000660", "100000"))

        rendered = " ".join(blockers)
        self.assertFalse(first.accepted)
        self.assertEqual("live_order_submission_uncertain", first.reject_reason)
        self.assertNotIn("live manual reconciliation required", rendered)
        self.assertIn("live pending orders unresolved: 1", rendered)
        self.assertFalse(second.accepted)
        self.assertEqual("live_pending_orders_unresolved", second.reject_reason)
        self.assertEqual(1, len(client.calls))


if __name__ == "__main__":
    unittest.main()
