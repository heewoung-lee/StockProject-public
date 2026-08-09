from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping


DISABLED_VALUES = {"false", "0", "no", "off", "n"}


@dataclass(frozen=True)
class WatchlistEntry:
    symbol: str
    company_name: str
    enabled: bool = True
    tags: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class Watchlist:
    entries: tuple[WatchlistEntry, ...] = ()

    def active_symbols(self) -> tuple[str, ...]:
        return tuple(entry.symbol for entry in self.entries if entry.enabled)

    def company_name(self, symbol: str) -> str:
        normalized_symbol = str(symbol).strip()
        for entry in self.entries:
            if entry.symbol == normalized_symbol:
                return entry.company_name or normalized_symbol
        return normalized_symbol

    def enabled_entries(self) -> tuple[WatchlistEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]]) -> Watchlist:
        entries_by_symbol: dict[str, WatchlistEntry] = {}

        for row in rows:
            symbol = _text(row.get("symbol", "")).strip()
            if not symbol:
                continue

            company_name = _company_name(row)
            entry = WatchlistEntry(
                symbol=symbol,
                company_name=company_name,
                enabled=_enabled(row.get("enabled", "")),
                tags=_tags(row.get("tags", "")),
                notes=_text(row.get("notes", "")).strip(),
            )
            entries_by_symbol.pop(symbol, None)
            entries_by_symbol[symbol] = entry

        return cls(tuple(entries_by_symbol.values()))


def _company_name(row: Mapping[str, str]) -> str:
    for key in ("company_name", "company", "name"):
        value = _text(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _enabled(value: str | None) -> bool:
    return _text(value).strip().lower() not in DISABLED_VALUES


def _tags(value: str | None) -> tuple[str, ...]:
    return tuple(tag for tag in (_text(part).strip() for part in _text(value).split(",")) if tag)


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value)
