from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from .models import MarketBar


@dataclass(frozen=True)
class VolumePriorityRanker:
    priorities: dict[str, float]

    @classmethod
    def from_bars(cls, symbols: Iterable[str], bars: Sequence[MarketBar]) -> "VolumePriorityRanker":
        volumes: dict[str, int] = {symbol: 0 for symbol in symbols}
        for bar in bars:
            volumes[bar.symbol] = max(volumes.get(bar.symbol, 0), int(bar.volume))
        return cls({symbol: float(volume) for symbol, volume in volumes.items()})

    def priority(self, symbol: str) -> float:
        return float(self.priorities.get(symbol, 0.0))

    def rank(self, symbols: Iterable[str]) -> list[str]:
        return sorted(list(symbols), key=lambda symbol: -self.priority(symbol))
