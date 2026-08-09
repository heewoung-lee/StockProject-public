from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Protocol


SCOPABLE_PENDING_ORDER_REASONS = frozenset({"pending", "partial", "cancel_requested"})


@dataclass(frozen=True)
class PendingLiveOrder:
    order_no: str
    symbol: str
    side: str
    requested_quantity: int
    remaining_quantity: int
    submitted_at: datetime
    estimated_price: Decimal
    reason: str = ""
    cost_basis_price: Decimal = Decimal("0")
    order_org_no: str = ""


def pending_order_reason_is_safely_scopable(reason: object) -> bool:
    return str(reason or "").strip().lower() in SCOPABLE_PENDING_ORDER_REASONS


def pending_order_is_safely_scopable(pending: object) -> bool:
    if str(getattr(pending, "side", "")).strip().upper() not in {"BUY", "SELL"}:
        return False
    if not str(getattr(pending, "symbol", "")).strip():
        return False
    if not pending_order_reason_is_safely_scopable(getattr(pending, "reason", "")):
        return False
    try:
        remaining_quantity = int(getattr(pending, "remaining_quantity", 0) or 0)
        estimated_price = Decimal(
            str(getattr(pending, "estimated_price", Decimal("0")) or "0")
        )
    except (TypeError, ValueError, ArithmeticError):
        return False
    return (
        remaining_quantity > 0
        and estimated_price.is_finite()
        and estimated_price > 0
    )


def pending_buy_is_safely_scopable(pending: object) -> bool:
    return (
        str(getattr(pending, "side", "")).strip().upper() == "BUY"
        and pending_order_is_safely_scopable(pending)
    )


@dataclass(frozen=True)
class ManualReconciliationBlocker:
    reason: str
    symbol: str
    side: str
    quantity: int
    order_no: str
    created_at: datetime


class PendingLiveOrderStore(Protocol):
    is_durable: bool

    def ensure_ready(self) -> None:
        ...

    def upsert(self, order: PendingLiveOrder) -> None:
        ...

    def remove(self, order_no: str) -> None:
        ...

    def all(self) -> tuple[PendingLiveOrder, ...]:
        ...


class ManualReconciliationStore(Protocol):
    is_durable: bool

    def ensure_ready(self) -> None:
        ...

    def blocker(self) -> ManualReconciliationBlocker | None:
        ...

    def latch(self, blocker: ManualReconciliationBlocker) -> None:
        ...

    def clear(self) -> None:
        ...


class InMemoryPendingLiveOrderStore:
    is_durable = False

    def __init__(self):
        self._orders: dict[str, PendingLiveOrder] = {}

    def ensure_ready(self) -> None:
        return None

    def upsert(self, order: PendingLiveOrder) -> None:
        self._orders[order.order_no] = order

    def remove(self, order_no: str) -> None:
        self._orders.pop(order_no, None)

    def all(self) -> tuple[PendingLiveOrder, ...]:
        return tuple(self._orders.values())


class InMemoryManualReconciliationStore:
    is_durable = False

    def __init__(self):
        self._blocker: ManualReconciliationBlocker | None = None

    def ensure_ready(self) -> None:
        return None

    def blocker(self) -> ManualReconciliationBlocker | None:
        return self._blocker

    def latch(self, blocker: ManualReconciliationBlocker) -> None:
        if self._blocker is None:
            self._blocker = blocker

    def clear(self) -> None:
        self._blocker = None


class JsonPendingLiveOrderStore:
    is_durable = True

    def __init__(self, path: str | Path, *, scope: str = ""):
        self.path = Path(path)
        self.scope = str(scope or "").strip()

    def ensure_ready(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._write(self._load())
            return
        self._write(())

    def upsert(self, order: PendingLiveOrder) -> None:
        orders = {existing.order_no: existing for existing in self.all()}
        orders[order.order_no] = order
        self._write(orders.values())

    def remove(self, order_no: str) -> None:
        orders = {existing.order_no: existing for existing in self.all()}
        orders.pop(order_no, None)
        self._write(orders.values())

    def all(self) -> tuple[PendingLiveOrder, ...]:
        if not self.path.exists():
            return ()
        return self._load()

    def _load(self) -> tuple[PendingLiveOrder, ...]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid pending live order store: {self.path}") from exc

        if isinstance(payload, dict) and "orders" in payload:
            saved_scope = str(payload.get("scope") or "").strip()
            if self.scope and saved_scope != self.scope:
                raise ValueError(f"pending live order store scope mismatch: {self.path}")
            payload = payload.get("orders")
        elif self.scope and payload:
            raise ValueError(f"legacy pending live order store requires manual reconciliation: {self.path}")

        if not isinstance(payload, list):
            raise ValueError(f"invalid pending live order store: {self.path}")
        orders: list[PendingLiveOrder] = []
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError(f"invalid pending live order store: {self.path}")
            orders.append(_order_from_json(item))
        return tuple(orders)

    def _write(self, orders: Iterable[PendingLiveOrder]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [_order_to_json(order) for order in orders]
        if self.scope:
            payload = {
                "scope": self.scope,
                "orders": payload,
            }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)


class JsonManualReconciliationStore:
    is_durable = True

    def __init__(self, path: str | Path, *, scope: str = ""):
        self.path = Path(path)
        self.scope = str(scope or "").strip()

    def ensure_ready(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self._write(self.blocker())
            return
        self._write(None)

    def blocker(self) -> ManualReconciliationBlocker | None:
        if not self.path.exists():
            return None
        return self._load()

    def latch(self, blocker: ManualReconciliationBlocker) -> None:
        existing = self.blocker()
        if existing is not None:
            return
        self._write(blocker)

    def clear(self) -> None:
        self._write(None)

    def _load(self) -> ManualReconciliationBlocker | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid manual reconciliation store: {self.path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"invalid manual reconciliation store: {self.path}")

        saved_scope = str(payload.get("scope") or "").strip()
        if self.scope and saved_scope != self.scope:
            raise ValueError(f"manual reconciliation store scope mismatch: {self.path}")
        blocker_payload = payload.get("blocker")
        if blocker_payload in (None, {}):
            return None
        if not isinstance(blocker_payload, dict):
            raise ValueError(f"invalid manual reconciliation store: {self.path}")
        return _manual_blocker_from_json(blocker_payload)

    def _write(self, blocker: ManualReconciliationBlocker | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {
            "scope": self.scope,
            "blocker": _manual_blocker_to_json(blocker) if blocker is not None else None,
        }
        temp_path = self.path.with_name(f"{self.path.name}.tmp")
        temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)


def _order_to_json(order: PendingLiveOrder) -> dict[str, object]:
    return {
        "order_no": order.order_no,
        "symbol": order.symbol,
        "side": order.side,
        "requested_quantity": order.requested_quantity,
        "remaining_quantity": order.remaining_quantity,
        "submitted_at": order.submitted_at.isoformat(),
        "estimated_price": str(order.estimated_price),
        "reason": order.reason,
        "cost_basis_price": str(order.cost_basis_price),
        "order_org_no": order.order_org_no,
    }


def _manual_blocker_to_json(blocker: ManualReconciliationBlocker) -> dict[str, object]:
    return {
        "reason": blocker.reason,
        "symbol": blocker.symbol,
        "side": blocker.side,
        "quantity": blocker.quantity,
        "order_no": blocker.order_no,
        "created_at": blocker.created_at.isoformat(),
    }


def _manual_blocker_from_json(item: dict[str, object]) -> ManualReconciliationBlocker:
    blocker = ManualReconciliationBlocker(
        reason=str(item.get("reason") or ""),
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or ""),
        quantity=_int_value(item.get("quantity")),
        order_no=str(item.get("order_no") or ""),
        created_at=datetime.fromisoformat(str(item.get("created_at") or "")),
    )
    if not blocker.reason:
        raise ValueError("invalid manual reconciliation record")
    return blocker


def _order_from_json(item: dict[str, object]) -> PendingLiveOrder:
    order = PendingLiveOrder(
        order_no=str(item.get("order_no") or ""),
        symbol=str(item.get("symbol") or ""),
        side=str(item.get("side") or ""),
        requested_quantity=_int_value(item.get("requested_quantity")),
        remaining_quantity=_int_value(item.get("remaining_quantity")),
        submitted_at=datetime.fromisoformat(str(item.get("submitted_at") or "")),
        estimated_price=Decimal(str(item.get("estimated_price") or "0")),
        reason=str(item.get("reason") or ""),
        cost_basis_price=Decimal(str(item.get("cost_basis_price") or "0")),
        order_org_no=str(item.get("order_org_no") or ""),
    )
    if not order.order_no or not order.symbol or order.side not in {"BUY", "SELL"}:
        raise ValueError("invalid pending live order record")
    if order.requested_quantity <= 0 or order.remaining_quantity < 0 or order.estimated_price <= 0:
        raise ValueError("invalid pending live order record")
    return order


def _int_value(value: object) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0
