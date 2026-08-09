import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.live_order_state import (
    JsonPendingLiveOrderStore,
    PendingLiveOrder,
    pending_buy_is_safely_scopable,
    pending_order_is_safely_scopable,
    pending_order_reason_is_safely_scopable,
)


def pending_order(order_no: str = "123") -> PendingLiveOrder:
    return PendingLiveOrder(
        order_no=order_no,
        symbol="005930",
        side="BUY",
        requested_quantity=3,
        remaining_quantity=2,
        submitted_at=datetime(2026, 7, 3, 9, 1, tzinfo=timezone.utc),
        estimated_price=Decimal("70000"),
        reason="partial",
        order_org_no="54321",
    )


class JsonPendingLiveOrderStoreTest(unittest.TestCase):
    def test_only_reconciled_nonterminal_reasons_are_safely_scopable(self):
        for reason in ("pending", "partial", "cancel_requested"):
            with self.subTest(reason=reason):
                self.assertTrue(pending_order_reason_is_safely_scopable(reason))

        for reason in ("", "unknown", "submission_in_progress", "submission_uncertain"):
            with self.subTest(reason=reason):
                self.assertFalse(pending_order_reason_is_safely_scopable(reason))

    def test_pending_order_scope_requires_known_side_symbol_quantity_and_price(self):
        buy = pending_order()
        sell = PendingLiveOrder(**{**buy.__dict__, "side": "SELL"})
        uncertain = PendingLiveOrder(**{**buy.__dict__, "reason": "submission_uncertain"})
        missing_price = PendingLiveOrder(**{**buy.__dict__, "estimated_price": Decimal("0")})

        self.assertTrue(pending_order_is_safely_scopable(buy))
        self.assertTrue(pending_order_is_safely_scopable(sell))
        self.assertTrue(pending_buy_is_safely_scopable(buy))
        self.assertFalse(pending_buy_is_safely_scopable(sell))
        self.assertFalse(pending_order_is_safely_scopable(uncertain))
        self.assertFalse(pending_order_is_safely_scopable(missing_price))

    def test_ensure_ready_creates_empty_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            store = JsonPendingLiveOrderStore(path)

            store.ensure_ready()

            self.assertEqual((), store.all())
            self.assertTrue(path.exists())

    def test_upsert_persists_pending_order_across_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            store = JsonPendingLiveOrderStore(path)

            store.upsert(pending_order())
            restored = JsonPendingLiveOrderStore(path).all()

            self.assertEqual(1, len(restored))
            self.assertEqual("123", restored[0].order_no)
            self.assertEqual("005930", restored[0].symbol)
            self.assertEqual("BUY", restored[0].side)
            self.assertEqual(3, restored[0].requested_quantity)
            self.assertEqual(2, restored[0].remaining_quantity)
            self.assertEqual(Decimal("70000"), restored[0].estimated_price)
            self.assertEqual("partial", restored[0].reason)
            self.assertEqual("54321", restored[0].order_org_no)

    def test_legacy_pending_order_without_order_org_no_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            path.write_text(
                """
                [
                  {
                    "order_no": "123",
                    "symbol": "005930",
                    "side": "BUY",
                    "requested_quantity": 3,
                    "remaining_quantity": 2,
                    "submitted_at": "2026-07-03T09:01:00+00:00",
                    "estimated_price": "70000",
                    "reason": "partial",
                    "cost_basis_price": "0"
                  }
                ]
                """,
                encoding="utf-8",
            )
            store = JsonPendingLiveOrderStore(path)

            restored = store.all()

            self.assertEqual(1, len(restored))
            self.assertEqual("123", restored[0].order_no)
            self.assertEqual("", restored[0].order_org_no)

    def test_scoped_store_persists_scope_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            store = JsonPendingLiveOrderStore(path, scope="account-scope")

            store.upsert(pending_order())
            payload = path.read_text(encoding="utf-8")
            restored = JsonPendingLiveOrderStore(path, scope="account-scope").all()

            self.assertIn('"scope": "account-scope"', payload)
            self.assertIn('"orders"', payload)
            self.assertEqual(1, len(restored))
            self.assertEqual("123", restored[0].order_no)

    def test_scoped_store_rejects_mismatched_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            JsonPendingLiveOrderStore(path, scope="account-a").upsert(pending_order())

            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                JsonPendingLiveOrderStore(path, scope="account-b").ensure_ready()

    def test_scoped_store_rejects_non_empty_legacy_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            JsonPendingLiveOrderStore(path).upsert(pending_order())

            with self.assertRaisesRegex(ValueError, "legacy pending live order store requires manual reconciliation"):
                JsonPendingLiveOrderStore(path, scope="account-scope").ensure_ready()

    def test_upsert_replaces_existing_order_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            store = JsonPendingLiveOrderStore(path)

            store.upsert(pending_order("123"))
            store.upsert(
                PendingLiveOrder(
                    order_no="123",
                    symbol="005930",
                    side="BUY",
                    requested_quantity=3,
                    remaining_quantity=1,
                    submitted_at=datetime(2026, 7, 3, 9, 2, tzinfo=timezone.utc),
                    estimated_price=Decimal("70100"),
                    reason="partial",
                )
            )

            restored = store.all()

            self.assertEqual(1, len(restored))
            self.assertEqual(1, restored[0].remaining_quantity)
            self.assertEqual(Decimal("70100"), restored[0].estimated_price)

    def test_remove_deletes_order_without_affecting_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            store = JsonPendingLiveOrderStore(path)
            store.upsert(pending_order("123"))
            store.upsert(pending_order("456"))

            store.remove("123")

            restored = store.all()
            self.assertEqual(1, len(restored))
            self.assertEqual("456", restored[0].order_no)

    def test_corrupt_store_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            path.write_text("{bad json", encoding="utf-8")
            store = JsonPendingLiveOrderStore(path)

            with self.assertRaises(ValueError):
                store.ensure_ready()

    def test_invalid_order_record_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            path.write_text('[{"order_no": "123", "symbol": "005930", "side": "UNKNOWN"}]', encoding="utf-8")
            store = JsonPendingLiveOrderStore(path)

            with self.assertRaises(ValueError):
                store.ensure_ready()

    def test_existing_store_must_be_rewritable(self):
        class ReadOnlyAfterLoadStore(JsonPendingLiveOrderStore):
            def _write(self, orders):
                raise PermissionError("cannot replace pending order file")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending-live-orders.json"
            JsonPendingLiveOrderStore(path).upsert(pending_order())
            store = ReadOnlyAfterLoadStore(path)

            with self.assertRaises(PermissionError):
                store.ensure_ready()


if __name__ == "__main__":
    unittest.main()
