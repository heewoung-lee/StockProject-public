from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterable


KST = timezone(timedelta(hours=9), "KST")
REGULAR_OPEN = time(9, 0)
REGULAR_CLOSE = time(15, 30)


@dataclass(frozen=True)
class MarketSessionStatus:
    is_open: bool
    label: str
    message: str
    checked_at: datetime
    next_open: datetime | None = None


class KoreanRegularMarketHours:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        holidays: Iterable[date] = (),
    ):
        self.clock = clock or (lambda: datetime.now(tz=KST))
        self.holidays = set(holidays)

    def status(self) -> MarketSessionStatus:
        now = _to_kst(self.clock())
        if self._is_regular_session(now):
            return MarketSessionStatus(
                is_open=True,
                label="정규장 진행 중",
                message="정규장 시간입니다. paper 자동매매 사이클을 실행합니다.",
                checked_at=now,
            )

        next_open = self.next_open_after(now)
        return MarketSessionStatus(
            is_open=False,
            label="장 대기",
            message=(
                "장 대기 - 정규장 시간이 아닙니다. "
                f"paper 자동매매는 정규장(09:00-15:30 KST)에만 실행합니다. "
                f"다음 정규장: {next_open:%Y-%m-%d %H:%M KST}"
            ),
            checked_at=now,
            next_open=next_open,
        )

    def next_open_after(self, value: datetime) -> datetime:
        cursor = _to_kst(value)
        current_day = cursor.date()
        for offset in range(0, 370):
            candidate_day = current_day + timedelta(days=offset)
            if not self._is_trading_day(candidate_day):
                continue
            candidate = datetime.combine(candidate_day, REGULAR_OPEN, tzinfo=KST)
            if candidate > cursor:
                return candidate
        raise RuntimeError("next market open could not be resolved within one year")

    def _is_regular_session(self, value: datetime) -> bool:
        return self._is_trading_day(value.date()) and REGULAR_OPEN <= value.time() < REGULAR_CLOSE

    def _is_trading_day(self, value: date) -> bool:
        return value.weekday() < 5 and value not in self.holidays


def default_krx_closed_dates(*, years: Iterable[int]) -> set[date]:
    closed: set[date] = set()
    for year in years:
        closed.add(date(year, 5, 1))
        closed.add(_year_end_market_closure(year))
    return closed


def parse_closed_dates(value: str | Iterable[str]) -> set[date]:
    if isinstance(value, str):
        raw_values = value.split(",")
    else:
        raw_values = value
    closed: set[date] = set()
    for raw in raw_values:
        text = str(raw).strip()
        if not text:
            continue
        closed.add(date.fromisoformat(text))
    return closed


def _year_end_market_closure(year: int) -> date:
    candidate = date(year, 12, 31)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def _to_kst(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)
