from __future__ import annotations

from decimal import Decimal


PROFILE_NAMES = ("conservative", "balanced", "aggressive")


_PROFILE_SETTINGS: dict[str, dict[str, object]] = {
    "conservative": {
        "order_cash_amount": Decimal("30000"),
        "cash_allocation_pct": Decimal("0.50"),
        "max_order_amount": Decimal("0"),
        "max_position_amount": Decimal("180000"),
        "max_positions": 0,
        "max_daily_loss": Decimal("60000"),
        "max_daily_entries_per_symbol": 1,
        "min_momentum_pct": Decimal("0.003"),
        "min_signal_confidence": Decimal("0.70"),
        "min_volume_ratio": Decimal("2"),
        "max_spread_bps": Decimal("20"),
        "stop_loss_pct": Decimal("0.012"),
        "take_profit_pct": Decimal("0.02"),
        "trailing_stop_pct": Decimal("0.01"),
    },
    "balanced": {
        "order_cash_amount": Decimal("50000"),
        "cash_allocation_pct": Decimal("0.70"),
        "max_order_amount": Decimal("0"),
        "max_position_amount": Decimal("300000"),
        "max_positions": 0,
        "max_daily_loss": Decimal("100000"),
        "max_daily_entries_per_symbol": 1,
        "min_momentum_pct": Decimal("0.001"),
        "min_signal_confidence": Decimal("0.55"),
        "min_volume_ratio": Decimal("1.2"),
        "max_spread_bps": Decimal("30"),
        "stop_loss_pct": Decimal("0.02"),
        "take_profit_pct": Decimal("0.03"),
        "trailing_stop_pct": Decimal("0.015"),
    },
    "aggressive": {
        "order_cash_amount": Decimal("80000"),
        "cash_allocation_pct": Decimal("0.90"),
        "max_order_amount": Decimal("0"),
        "max_position_amount": Decimal("450000"),
        "max_positions": 0,
        "max_daily_loss": Decimal("150000"),
        "max_daily_entries_per_symbol": 2,
        "min_momentum_pct": Decimal("0.0005"),
        "min_signal_confidence": Decimal("0.55"),
        "min_volume_ratio": Decimal("1"),
        "max_spread_bps": Decimal("45"),
        "stop_loss_pct": Decimal("0.03"),
        "take_profit_pct": Decimal("0.045"),
        "trailing_stop_pct": Decimal("0.02"),
    },
}


def get_profile_settings(name: str) -> dict[str, object]:
    normalized = name.strip().lower()
    if normalized not in _PROFILE_SETTINGS:
        allowed = ", ".join(PROFILE_NAMES)
        raise ValueError(f"unknown strategy_profile '{name}'; expected one of: {allowed}")
    return dict(_PROFILE_SETTINGS[normalized])
