from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta


DEFAULT_APPROVAL_TTL = timedelta(hours=8)


@dataclass
class LiveOrderSafetyContext:
    """Mutable in-process safety gates for live order placement."""

    approval_ttl: timedelta = DEFAULT_APPROVAL_TTL
    approved_at: datetime | None = None
    approval_date: date | None = None
    _session_approved: bool = False
    _risk_limits_ok: bool = False
    _new_entries_allowed: bool = False

    @property
    def session_approved(self) -> bool:
        return self.approval_current()

    @property
    def risk_limits_ok(self) -> bool:
        return self.approval_current() and self._risk_limits_ok

    @property
    def new_entries_allowed(self) -> bool:
        return self.approval_current() and self._new_entries_allowed

    def approve_session(
        self,
        *,
        allow_new_entries: bool = True,
        timestamp: datetime | None = None,
    ) -> None:
        now = timestamp or datetime.now()
        self.approved_at = now
        self.approval_date = now.date()
        self._session_approved = True
        self._risk_limits_ok = True
        self._new_entries_allowed = bool(allow_new_entries)

    def reset(self) -> None:
        self.approved_at = None
        self.approval_date = None
        self._session_approved = False
        self._risk_limits_ok = False
        self._new_entries_allowed = False

    def set_cleanup_mode(self, enabled: bool) -> None:
        if self.session_approved and self.risk_limits_ok:
            self._new_entries_allowed = not enabled

    def approval_current(self, *, now: datetime | None = None) -> bool:
        if not self._session_approved or self.approved_at is None or self.approval_date is None:
            return False
        checked_at = now or datetime.now()
        if checked_at.date() != self.approval_date:
            return False
        return checked_at - self.approved_at <= self.approval_ttl
