from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

DEFAULT_KIS_MARKET_DATA_SYMBOLS = ",".join(
    [
        "005930",  # Samsung Electronics
        "000660",  # SK hynix
        "035420",  # NAVER
        "035720",  # Kakao
        "051910",  # LG Chem
        "068270",  # Celltrion
        "005380",  # Hyundai Motor
        "000270",  # Kia
        "005490",  # POSCO Holdings
        "012330",  # Hyundai Mobis
        "055550",  # Shinhan Financial
        "066570",  # LG Electronics
        "086790",  # Hana Financial
        "105560",  # KB Financial
        "207940",  # Samsung Biologics
        "086520",  # EcoPro
        "196170",  # Alteogen
        "247540",  # EcoPro BM
        "028300",  # HLB
        "035900",  # JYP Ent.
    ]
)
KIS_INTRADAY_REHEARSAL_SCAN_LIMIT = 10
KIS_INTRADAY_REHEARSAL_MAX_POSITIONS = 10
KIS_INTRADAY_REHEARSAL_MIN_MOMENTUM_PCT = Decimal("0")
KIS_INTRADAY_REHEARSAL_MIN_SIGNAL_CONFIDENCE = Decimal("0.25")
KIS_INTRADAY_REHEARSAL_MIN_TREND_PCT = Decimal("0")
KIS_INTRADAY_REHEARSAL_MIN_VOLUME_RATIO = Decimal("0")


@dataclass(frozen=True)
class BotConfig:
    trading_mode: str = "paper"
    strategy_profile: str = "single"
    initial_cash: Decimal = Decimal("1000000")
    order_cash_amount: Decimal = Decimal("50000")
    cash_allocation_pct: Decimal = Decimal("1.0")
    max_order_amount: Decimal = Decimal("0")
    max_position_amount: Decimal = Decimal("300000")
    max_positions: int = 0
    max_daily_loss: Decimal = Decimal("100000")
    max_daily_entries_per_symbol: int = 1
    max_consecutive_order_failures: int = 3
    momentum_window: int = 3
    min_momentum_pct: Decimal = Decimal("0.001")
    min_signal_confidence: Decimal = Decimal("0.55")
    volume_window: int = 3
    scan_limit_per_cycle: int = 0
    min_volume_ratio: Decimal = Decimal("1.2")
    max_spread_bps: Decimal = Decimal("30")
    stop_loss_pct: Decimal = Decimal("0.02")
    take_profit_pct: Decimal = Decimal("0.03")
    trailing_stop_pct: Decimal = Decimal("0.015")
    transaction_tax_pct: Decimal = Decimal("0.002")
    commission_pct: Decimal = Decimal("0")
    slippage_pct: Decimal = Decimal("0.001")
    min_net_profit_pct: Decimal = Decimal("0.001")
    max_holding_minutes: int = 0
    daily_loss_exit_amount: Decimal = Decimal("100000")
    forced_exit_time: str = ""
    allow_kis_vts_trading: bool = False
    allow_live_trading: bool = False
    live_trading_enabled: bool = False
    allow_paper_short: bool = False
    allow_after_hours_simulation: bool = True
    enforce_market_hours: bool = True
    market_closed_dates: str = ""
    market_data_source: str = "local"
    scanner_source: str = "local"
    scanner_snapshot_path: str = ""
    scanner_snapshot_max_age_seconds: int = 300
    kis_market_data_symbols: str = DEFAULT_KIS_MARKET_DATA_SYMBOLS
    kis_market_data_scan_limit: int = KIS_INTRADAY_REHEARSAL_SCAN_LIMIT
    kill_switch: bool = False
    data_path: str = "data/sample_bars.csv"
    journal_path: str = "logs/trades.csv"

    @classmethod
    def default(cls) -> "BotConfig":
        return cls()

    def validate_safety(self) -> None:
        _require_bool("allow_kis_vts_trading", self.allow_kis_vts_trading)
        _require_bool("allow_live_trading", self.allow_live_trading)
        _require_bool("live_trading_enabled", self.live_trading_enabled)
        _require_bool("allow_paper_short", self.allow_paper_short)
        _require_bool("allow_after_hours_simulation", self.allow_after_hours_simulation)
        _require_bool("enforce_market_hours", self.enforce_market_hours)
        _require_bool("kill_switch", self.kill_switch)
        try:
            max_positions = int(self.max_positions)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_positions must be an integer") from exc
        if max_positions < 0:
            raise ValueError("max_positions must be 0 or greater")
        try:
            max_holding_minutes = int(self.max_holding_minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_holding_minutes must be an integer") from exc
        if max_holding_minutes < 0:
            raise ValueError("max_holding_minutes must be 0 or greater")
        for name in (
            "cash_allocation_pct",
            "min_signal_confidence",
            "transaction_tax_pct",
            "commission_pct",
            "slippage_pct",
            "min_net_profit_pct",
        ):
            if Decimal(str(getattr(self, name))) < 0:
                raise ValueError(f"{name} must be non-negative")
        if Decimal(str(self.cash_allocation_pct)) <= 0 or Decimal(str(self.cash_allocation_pct)) > 1:
            raise ValueError("cash_allocation_pct must be greater than 0 and less than or equal to 1")
        if Decimal(str(self.min_signal_confidence)) > 1:
            raise ValueError("min_signal_confidence must be between 0 and 1")
        try:
            kis_market_data_scan_limit = int(self.kis_market_data_scan_limit)
        except (TypeError, ValueError) as exc:
            raise ValueError("kis_market_data_scan_limit must be an integer") from exc
        if kis_market_data_scan_limit <= 0:
            raise ValueError("kis_market_data_scan_limit must be greater than 0")
        try:
            scanner_snapshot_max_age_seconds = int(self.scanner_snapshot_max_age_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("scanner_snapshot_max_age_seconds must be an integer") from exc
        if scanner_snapshot_max_age_seconds < 0:
            raise ValueError("scanner_snapshot_max_age_seconds must be 0 or greater")
        if self.market_data_source not in {"local", "kis-vts", "external-scan-kis"}:
            raise ValueError("market_data_source must be local, kis-vts, or external-scan-kis")
        scanner_source = str(self.scanner_source).strip().lower()
        if scanner_source not in {"local", "json", "kiwoom"}:
            raise ValueError("scanner_source must be local, json, or kiwoom")
        if scanner_source != "local" and self.market_data_source != "external-scan-kis":
            raise ValueError(f"scanner_source={scanner_source} requires market_data_source=external-scan-kis")
        if self.market_data_source == "external-scan-kis" and scanner_source == "local":
            raise ValueError("external-scan-kis requires scanner_source=json or scanner_source=kiwoom")
        if scanner_source == "json" and not str(self.scanner_snapshot_path).strip():
            raise ValueError("scanner_snapshot_path is required for scanner_source=json")
        if self.market_data_source in {"kis-vts", "external-scan-kis"} and self.trading_mode != "paper":
            raise ValueError("KIS market data source is only supported in paper mode")
        if self.market_data_source in {"kis-vts", "external-scan-kis"} and not self.enforce_market_hours:
            raise ValueError("KIS market data source requires enforce_market_hours=true")

        if self.trading_mode == "paper":
            if self.allow_kis_vts_trading or self.allow_live_trading or self.live_trading_enabled:
                raise ValueError("paper mode must not enable broker trading gates")
            return
        if self.allow_paper_short:
            raise ValueError("allow_paper_short is only supported in paper mode")
        if self.trading_mode == "kis-vts":
            if self.allow_kis_vts_trading:
                return
            raise ValueError("kis-vts trading requires allow_kis_vts_trading=true")
        if not (self.trading_mode == "live" and self.allow_live_trading and self.live_trading_enabled):
            raise ValueError("live trading requires trading_mode=live, allow_live_trading=true, and live_trading_enabled=true")
        return


def load_config(path: str | Path | None = None) -> BotConfig:
    if path is None:
        return BotConfig.default()

    values: dict[str, object] = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = _parse_scalar(value.strip())

    decimal_fields = {
        "initial_cash",
        "order_cash_amount",
        "cash_allocation_pct",
        "max_order_amount",
        "max_position_amount",
        "max_daily_loss",
        "min_momentum_pct",
        "min_signal_confidence",
        "min_volume_ratio",
        "max_spread_bps",
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "transaction_tax_pct",
        "commission_pct",
        "slippage_pct",
        "min_net_profit_pct",
        "daily_loss_exit_amount",
    }
    int_fields = {
        "max_positions",
        "max_daily_entries_per_symbol",
        "max_consecutive_order_failures",
        "momentum_window",
        "volume_window",
        "scan_limit_per_cycle",
        "max_holding_minutes",
        "kis_market_data_scan_limit",
        "scanner_snapshot_max_age_seconds",
    }
    bool_fields = {
        "allow_kis_vts_trading",
        "allow_live_trading",
        "live_trading_enabled",
        "allow_paper_short",
        "allow_after_hours_simulation",
        "enforce_market_hours",
        "kill_switch",
    }

    normalized: dict[str, object] = {}
    ignored_legacy_keys = {
        "strategy_profile",
        "order_cash_amount",
        "cash_allocation_pct",
    }
    for key, value in values.items():
        if key in ignored_legacy_keys:
            continue
        if key in decimal_fields:
            normalized[key] = Decimal(str(value))
        elif key in int_fields:
            normalized[key] = int(str(value))
        elif key in bool_fields:
            normalized[key] = _to_bool(value, key)
        else:
            normalized[key] = str(value)

    config = BotConfig(**normalized)
    config.validate_safety()
    return config


def _parse_scalar(value: str) -> object:
    value = value.strip().strip('"').strip("'")
    lower = value.lower()
    if lower in {"true", "false"}:
        return lower == "true"
    return value


def _to_bool(value: object, name: str = "boolean") -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} has invalid boolean value: {value}")


def _require_bool(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be boolean")
