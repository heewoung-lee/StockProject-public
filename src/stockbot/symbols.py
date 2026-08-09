from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


UNKNOWN_SYMBOL_NAME = "알 수 없음"


@dataclass(frozen=True)
class SymbolDirectory:
    names: Mapping[str, str]

    def name_for(self, symbol: str) -> str:
        return self.names.get(symbol, UNKNOWN_SYMBOL_NAME)

    def label_for(self, symbol: str) -> str:
        return f"{self.name_for(symbol)} ({symbol})"


def load_symbol_directory(path: str | Path) -> SymbolDirectory:
    names: dict[str, str] = {}

    with Path(path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or ())
        required_headers = {"symbol", "name"}
        if not required_headers.issubset(headers):
            raise ValueError("symbols CSV requires symbol,name headers")

        for row in reader:
            symbol = row.get("symbol", "").strip()
            name = row.get("name", "").strip()
            if not symbol or not name:
                raise ValueError("symbols CSV rows require symbol and name values")
            if symbol in names:
                raise ValueError(f"duplicate symbol in symbols CSV: {symbol}")
            names[symbol] = name

    return SymbolDirectory(names)
