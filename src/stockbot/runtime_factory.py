from __future__ import annotations

import csv
import os
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re
import sys
from typing import Callable, Iterable, Mapping

from .broker import PaperBroker
from .candidate_selection import entry_affordability_issue, entry_reference_price
from .config import (
    BotConfig,
    DEFAULT_KIS_MARKET_DATA_SYMBOLS,
    KIS_INTRADAY_REHEARSAL_MAX_POSITIONS,
    KIS_INTRADAY_REHEARSAL_MIN_MOMENTUM_PCT,
    KIS_INTRADAY_REHEARSAL_MIN_SIGNAL_CONFIDENCE,
    KIS_INTRADAY_REHEARSAL_MIN_TREND_PCT,
    KIS_INTRADAY_REHEARSAL_MIN_VOLUME_RATIO,
    KIS_INTRADAY_REHEARSAL_SCAN_LIMIT,
    load_config,
)
from .scanner_collector import (
    DEFAULT_NAVER_MINUTE_HISTORY_CANDIDATES,
    DEFAULT_NAVER_MINUTE_HISTORY_TIMEOUT_SECONDS,
    DEFAULT_NAVER_MINUTE_HISTORY_WORKERS,
    collect_naver_market_scanner_snapshot,
)
from .scanner_snapshot import SnapshotWriteOptions
from .dashboard import DashboardController, DashboardServices
from .kis import KisLiveOrderClient, Transport
from .kis_market_data import KisPriceBarProvider, KisTokenFileCache
from .live_audit import JsonlLiveAuditLog
from .live_broker import LiveBroker
from .live_order_safety_context import LiveOrderSafetyContext
from .live_order_state import JsonManualReconciliationStore, JsonPendingLiveOrderStore
from .live_position_ledger import JsonManagedLivePositionLedger, managed_live_position_ledger_scope
from .live_reconciliation import KisLiveOrderReconciler
from .live_safety import (
    live_order_gate_configured,
    load_live_kis_credentials,
    read_env_file,
)
from .market_data import read_csv_bars
from .market_hours import KST, KoreanRegularMarketHours, default_krx_closed_dates, parse_closed_dates
from .models import MarketBar
from .profit_analytics import ProfitAnalyticsService, SqliteAccountProfitStore
from .rate_limit import LIVE_KIS_REST_MIN_INTERVAL_SECONDS, KisRateLimiter
from .risk import RiskConfig, RiskManager
from .runtime import CustomStrategySettings, PaperTradingRuntime
from .scanner import BarProviderScanner, JsonScannerProvider, ScannerProvider
from .strategy import FlowScalperConfig, FlowScalperStrategy
from .symbols import SymbolDirectory, load_symbol_directory
from .universe import VolumePriorityRanker


DEFAULT_SCANNER_SNAPSHOT_PATH = "data/scanner_snapshot.json"
LIVE_KIS_PHYSICAL_MARKET_READ_BUDGET = 26
LIVE_SCANNER_REFRESH_FAILURE_RETRY_SECONDS = 60.0
_KIS_OPENING_DAY_MAX_BUDGETED_READS = 10
_SECRET_DETAIL_PATTERNS = (
    re.compile(r"(?i)authorization\s*:\s*bearer\s+\S+"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"(?i)\b(api[_ -]?key|app[_ -]?key|secret|token)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(account|acct)\b\s*[:= ]+\d{4,}"),
    re.compile(r"\b\d{8,}\b"),
    re.compile(r"[A-Za-z]:\\[^\s]+"),
)


class _CachedKisOpeningDayGate:
    def __init__(self, client: KisLiveOrderClient, market_hours) -> None:
        self.client = client
        self.market_hours = market_hours
        self._cached_date = None
        self._cached_open = False

    def pending_market_read_cost(self) -> int:
        try:
            local_market_open = self.market_hours.status().is_open
        except Exception:
            return _KIS_OPENING_DAY_MAX_BUDGETED_READS
        if local_market_open is False:
            return 0
        if local_market_open is not True:
            return _KIS_OPENING_DAY_MAX_BUDGETED_READS
        try:
            trading_date = datetime.now(KST).date()
        except Exception:
            return _KIS_OPENING_DAY_MAX_BUDGETED_READS
        if trading_date == self._cached_date:
            return 0
        return _KIS_OPENING_DAY_MAX_BUDGETED_READS

    def __call__(self) -> bool:
        if not self.market_hours.status().is_open:
            return False
        trading_date = datetime.now(KST).date()
        if trading_date == self._cached_date:
            return self._cached_open
        try:
            is_open = self.client.is_opening_day(trading_date)
        except Exception:
            return False
        if not isinstance(is_open, bool):
            return False
        self._cached_date = trading_date
        self._cached_open = is_open
        return self._cached_open


def resolve_app_asset_path(*parts: str, bundle_root: str | Path | None = None) -> Path:
    root = Path(bundle_root or getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return root.joinpath(*parts)


def create_default_controller(
    config_path: str | Path | None = None,
    *,
    env_file: str | Path | None = None,
) -> DashboardController:
    config_path = Path(config_path) if config_path is not None else _default_config_path()
    config = _load_app_config(config_path)
    paper_config = _default_dashboard_paper_config(config, config_path)
    live_config = _dashboard_live_runtime_config(config)
    env_path = Path(env_file) if env_file is not None else _default_env_file()
    _refresh_dashboard_scanner_snapshot_if_needed(paper_config)
    symbol_directory = _load_default_symbol_directory()
    paper_kis_rate_limiter = KisRateLimiter()
    live_kis_rate_limiter = KisRateLimiter(
        min_interval_seconds=LIVE_KIS_REST_MIN_INTERVAL_SECONDS,
    )
    live_token_cache = KisTokenFileCache(namespace="kis-live")
    live_order_safety_context = LiveOrderSafetyContext()
    runtime = _build_paper_runtime(
        paper_config,
        symbol_directory,
        data_path=paper_config.data_path,
        rate_limiter=paper_kis_rate_limiter,
        scanner_refresh_callback=_json_scanner_refresh_callback_for(paper_config),
    )
    return DashboardController(
        services=DashboardServices(
            runtime=runtime,
            runtime_builder=lambda source: create_paper_runtime_for_data_source(
                source,
                config_path=config_path,
                symbol_directory=symbol_directory,
                rate_limiter=paper_kis_rate_limiter,
            ),
            live_runtime_builder=lambda: create_live_runtime(
                config=live_config,
                config_path=config_path,
                symbol_directory=symbol_directory,
                rate_limiter=live_kis_rate_limiter,
                env_file=env_path,
                live_order_safety_context=live_order_safety_context,
                live_token_cache=live_token_cache,
            ),
            kis_market_status=lambda: market_status_for_data_source("kis-vts", config_path=config_path),
            symbol_names=symbol_directory.names,
            kis_rate_limiter=live_kis_rate_limiter,
            paper_kis_rate_limiter=paper_kis_rate_limiter,
            profit_report=lambda *, granularity, scope, anchor: _profit_analytics_service(
                config=_load_app_config(config_path),
                env_file=env_path,
                env_values=_live_env_values(env_path),
            ).query(
                granularity=granularity,
                scope=scope,
                anchor=anchor,
            ),
        ),
        config_path=str(config_path),
        env_file=str(env_path),
        live_order_safety_context=live_order_safety_context,
        live_token_cache=live_token_cache,
    )


def create_paper_runtime(
    config: BotConfig | None = None,
    symbol_directory: SymbolDirectory | None = None,
    bars: list[MarketBar] | None = None,
    *,
    data_path: str | None = None,
    rate_limiter: KisRateLimiter | None = None,
    kis_bar_provider: Callable[[str], MarketBar | None] | None = None,
    scanner_provider: ScannerProvider | None = None,
    env_file: str | Path | None = None,
) -> PaperTradingRuntime:
    config_path = _default_config_path()
    loaded_config = config or _load_app_config(config_path)
    loaded_symbols = symbol_directory or _load_default_symbol_directory()
    return _build_paper_runtime(
        loaded_config,
        loaded_symbols,
        bars=bars,
        data_path=data_path or loaded_config.data_path,
        rate_limiter=rate_limiter,
        kis_bar_provider=kis_bar_provider,
        scanner_provider=scanner_provider,
        env_file=env_file,
    )


def create_live_runtime(
    config: BotConfig | None = None,
    symbol_directory: SymbolDirectory | None = None,
    *,
    config_path: str | Path | None = None,
    rate_limiter: KisRateLimiter | None = None,
    kis_bar_provider: Callable[[str], MarketBar | None] | None = None,
    scanner_provider: ScannerProvider | None = None,
    env_file: str | Path | None = None,
    live_broker: object | None = None,
    live_transport: Transport | None = None,
    live_order_safety_context: LiveOrderSafetyContext | None = None,
    live_token_cache: KisTokenFileCache | None = None,
) -> PaperTradingRuntime:
    resolved_config_path = Path(config_path) if config_path is not None else _default_config_path()
    loaded_config = config or _load_app_config(resolved_config_path)
    data_config = _config_for_paper_data_source(loaded_config, "external-scan-kis")
    data_config = _resolve_app_config_paths(data_config, resolved_config_path)
    _refresh_dashboard_scanner_snapshot_if_needed(data_config)
    symbols = symbol_directory or _load_default_symbol_directory()
    env_path = Path(env_file) if env_file is not None else _default_env_file()
    env_values = _live_env_values(env_path)
    _validate_live_runtime_config_gate(loaded_config)
    order_gate = _live_order_gate_enabled(loaded_config, env_values)
    live_rate_limiter = rate_limiter or KisRateLimiter(
        min_interval_seconds=LIVE_KIS_REST_MIN_INTERVAL_SECONDS,
    )
    account_profit_store = _account_profit_store(
        loaded_config,
        env_path,
        env_values,
    )

    if live_broker is not None:
        if type(live_broker) is not LiveBroker:
            raise ValueError("injected live broker must be stockbot.live_broker.LiveBroker")
        if not order_gate:
            raise ValueError("live order approval is required for injected live broker")
        _validate_injected_live_broker(
            live_broker,
            config=loaded_config,
            env_values=env_values,
            env_file=env_path,
        )

    client: KisLiveOrderClient | None = None
    if kis_bar_provider is None or live_broker is None:
        credentials = load_live_kis_credentials(env_values)
        token_cache = live_token_cache or (
            KisTokenFileCache(namespace="kis-live") if live_transport is None else None
        )
        client = KisLiveOrderClient(
            credentials,
            transport=live_transport,
            allow_order_placement=order_gate,
            rate_limiter=live_rate_limiter,
            token_cache=token_cache,
            profit_observer=(
                account_profit_store.record_kis_period
                if account_profit_store is not None
                else None
            ),
        )
        _ensure_live_runtime_access_token(client, credentials, token_cache)

    quote_provider = kis_bar_provider or (client.price_bar if client is not None else None)
    if quote_provider is None:
        raise ValueError("live runtime requires a KIS live quote provider")
    history_client = client or getattr(live_broker, "client", None)
    history_provider = getattr(history_client, "minute_bars", None)
    if not callable(history_provider):
        history_provider = None

    runtime = _build_paper_runtime(
        data_config,
        symbols,
        data_path=data_config.data_path,
        rate_limiter=live_rate_limiter,
        kis_bar_provider=quote_provider,
        scanner_provider=scanner_provider,
        env_file=env_path,
        scanner_refresh_callback=_json_scanner_refresh_callback_for(data_config),
        execution_mode="live",
        entry_history_provider=history_provider,
        max_physical_market_reads_per_cycle=LIVE_KIS_PHYSICAL_MARKET_READ_BUDGET,
    )
    runtime.rate_limiter = live_rate_limiter

    broker = live_broker or _build_live_broker(
        loaded_config,
        env_values,
        client=client,
        env_file=env_path,
        live_order_safety_context=live_order_safety_context,
    )
    runtime.broker = broker
    _restore_live_entry_counts(runtime, broker)
    runtime.data_source_kind = "live"
    runtime.data_source_label = "KIS live orders / scanner"
    return runtime


def _restore_live_entry_counts(runtime: PaperTradingRuntime, broker: LiveBroker) -> None:
    ledger = getattr(broker, "managed_position_ledger", None)
    entry_counts = getattr(ledger, "entry_counts", None)
    ensure_ready = getattr(ledger, "ensure_ready", None)
    if not callable(entry_counts) or not callable(ensure_ready):
        raise ValueError("live entry count ledger is unavailable")
    try:
        ensure_ready()
        reconcile_entry_counts = getattr(broker, "reconcile_managed_entry_counts", None)
        if callable(reconcile_entry_counts):
            reconcile_entry_counts()
        runtime.risk_manager.restore_entry_counts(entry_counts())
    except Exception as exc:
        raise ValueError("live entry count ledger is unavailable") from exc


def _ensure_live_runtime_access_token(
    client: KisLiveOrderClient,
    credentials,
    token_cache: KisTokenFileCache | None,
) -> None:
    if token_cache is not None:
        cached_token = token_cache.read(credentials)
        if cached_token is not None:
            client.set_access_token(cached_token.access_token, expires_at=cached_token.expires_at)
            return

    access_token = client.issue_access_token_with_rate_limit()
    if token_cache is not None:
        token_cache.write(credentials, access_token, client.access_token_expires_at())


def _validate_live_runtime_config_gate(config: BotConfig) -> None:
    blockers: list[str] = []
    if config.trading_mode != "live":
        blockers.append("trading_mode=live")
    if not config.allow_live_trading:
        blockers.append("allow_live_trading=true")
    if not config.live_trading_enabled:
        blockers.append("live_trading_enabled=true")
    if config.allow_paper_short:
        blockers.append("allow_paper_short=false")
    if blockers:
        raise ValueError("live runtime requires " + ", ".join(blockers))


def _validate_injected_live_broker(
    broker: LiveBroker,
    *,
    config: BotConfig,
    env_values: Mapping[str, str],
    env_file: Path,
) -> None:
    expected_scope = _live_managed_positions_scope(env_values)
    if not expected_scope:
        raise ValueError("injected live broker requires scoped live account settings")
    if str(broker.env.get("KIS_LIVE_ACCOUNT_NO") or "").strip() != str(
        env_values.get("KIS_LIVE_ACCOUNT_NO") or ""
    ).strip():
        raise ValueError("injected live broker account does not match environment")
    if str(broker.env.get("KIS_LIVE_ACCOUNT_PRODUCT_CODE") or "").strip() != str(
        env_values.get("KIS_LIVE_ACCOUNT_PRODUCT_CODE") or ""
    ).strip():
        raise ValueError("injected live broker product code does not match environment")

    pending_store = broker.pending_order_store
    expected_pending_path = _live_pending_orders_path(config, env_file, env_values)
    if type(pending_store) is not JsonPendingLiveOrderStore:
        raise ValueError("injected live broker requires durable scoped pending order store")
    if pending_store.scope != expected_scope:
        raise ValueError("injected live broker pending order store scope mismatch")
    if pending_store.path.resolve() != expected_pending_path.resolve():
        raise ValueError("injected live broker pending order store path mismatch")

    manual_store = broker.manual_reconciliation_store
    expected_manual_path = _live_manual_reconciliation_path(config, env_file, env_values)
    if type(manual_store) is not JsonManualReconciliationStore:
        raise ValueError("injected live broker requires durable scoped manual reconciliation store")
    if manual_store.scope != expected_scope:
        raise ValueError("injected live broker manual reconciliation store scope mismatch")
    if manual_store.path.resolve() != expected_manual_path.resolve():
        raise ValueError("injected live broker manual reconciliation store path mismatch")

    managed_ledger = broker.managed_position_ledger
    expected_ledger_path = _live_managed_positions_path(config, env_file, env_values)
    if type(managed_ledger) is not JsonManagedLivePositionLedger:
        raise ValueError("injected live broker requires durable scoped managed position ledger")
    if managed_ledger.scope != expected_scope:
        raise ValueError("injected live broker managed position ledger scope mismatch")
    if managed_ledger.path.resolve() != expected_ledger_path.resolve():
        raise ValueError("injected live broker managed position ledger path mismatch")

    if type(broker.fill_reconciler) is not KisLiveOrderReconciler:
        raise ValueError("injected live broker requires KIS live order reconciler")
    if broker.fill_reconciler.client is not broker.client:
        raise ValueError("injected live broker reconciler client mismatch")


def create_paper_runtime_for_data_source(
    source: str,
    *,
    config_path: str | Path | None = None,
    symbol_directory: SymbolDirectory | None = None,
    rate_limiter: KisRateLimiter | None = None,
    env_file: str | Path | None = None,
) -> PaperTradingRuntime:
    resolved_config_path = Path(config_path) if config_path is not None else _default_config_path()
    config = _config_for_paper_data_source(_load_app_config(resolved_config_path), source)
    config = _resolve_app_config_paths(config, resolved_config_path)
    _refresh_dashboard_scanner_snapshot_if_needed(config)
    symbols = symbol_directory or _load_default_symbol_directory()
    return _build_paper_runtime(
        config,
        symbols,
        data_path=config.data_path,
        rate_limiter=rate_limiter,
        env_file=env_file,
        scanner_refresh_callback=_json_scanner_refresh_callback_for(config),
    )


def market_status_for_data_source(source: str, *, config_path: str | Path | None = None):
    normalized = source.strip().lower()
    if normalized == "local":
        return None
    resolved_config_path = Path(config_path) if config_path is not None else _default_config_path()
    config = _config_for_paper_data_source(_load_app_config(resolved_config_path), normalized)
    market_hours = _regular_market_hours_from_config(config)
    return market_hours.status()


def _default_dashboard_paper_config(config: BotConfig, config_path: Path) -> BotConfig:
    """Build the startup paper runtime from a broker-safe config copy.

    The dashboard always boots with a paper runtime service, even when the saved
    configuration contains live-trading gates for real-mode readiness checks.
    """
    source = str(config.market_data_source or "local").strip().lower() or "local"
    if source not in {"local", "kis-vts", "external-scan-kis"}:
        source = "local"
    paper_config = _config_for_paper_data_source(config, source)
    return _resolve_app_config_paths(paper_config, config_path)


def _dashboard_live_runtime_config(config: BotConfig) -> BotConfig:
    scanner_source = _external_scan_scanner_source(config)
    return replace(
        config,
        trading_mode="live",
        market_data_source="external-scan-kis",
        scanner_source=scanner_source,
        scanner_snapshot_path=_external_scan_snapshot_path(config, scanner_source),
        allow_after_hours_simulation=False,
        enforce_market_hours=True,
        allow_kis_vts_trading=False,
        allow_live_trading=True,
        live_trading_enabled=True,
        allow_paper_short=False,
    )


def _config_for_paper_data_source(config: BotConfig, source: str) -> BotConfig:
    normalized = source.strip().lower()
    if normalized not in {"local", "kis-vts", "external-scan-kis"}:
        raise ValueError("market data source must be local, kis-vts, or external-scan-kis")
    scanner_source = "local"
    scanner_snapshot_path = ""
    if normalized == "external-scan-kis":
        scanner_source = _external_scan_scanner_source(config)
        scanner_snapshot_path = _external_scan_snapshot_path(config, scanner_source)
    return replace(
        config,
        trading_mode="paper",
        market_data_source=normalized,
        scanner_source=scanner_source,
        scanner_snapshot_path=scanner_snapshot_path,
        allow_after_hours_simulation=normalized == "local",
        enforce_market_hours=True,
        allow_kis_vts_trading=False,
        allow_live_trading=False,
        live_trading_enabled=False,
    )


def _external_scan_scanner_source(config: BotConfig) -> str:
    scanner_source = str(config.scanner_source or "").strip().lower()
    if scanner_source in {"json", "kiwoom"}:
        return scanner_source
    return "json"


def _external_scan_snapshot_path(config: BotConfig, scanner_source: str) -> str:
    configured_path = str(config.scanner_snapshot_path or "").strip()
    if configured_path:
        return configured_path
    if scanner_source == "json":
        return DEFAULT_SCANNER_SNAPSHOT_PATH
    return ""


def _default_config_path() -> Path:
    configured_path = os.environ.get("STOCKBOT_CONFIG_PATH") or os.environ.get("STOCKBOT_CONFIG")
    if configured_path:
        return Path(configured_path)
    bundled_path = resolve_app_asset_path("config.example.yaml")
    if bundled_path.exists():
        return bundled_path
    return Path("config.example.yaml")


def _load_app_config(config_path: Path) -> BotConfig:
    config = load_config(config_path)
    return _resolve_app_config_paths(config, config_path)


def _resolve_app_config_paths(config: BotConfig, config_path: Path) -> BotConfig:
    config_base = config_path.resolve().parent
    if not Path(config.data_path).is_absolute():
        config = replace(config, data_path=str(config_base / config.data_path))
    if not Path(config.journal_path).is_absolute():
        config = replace(config, journal_path=str(config_base / config.journal_path))
    if config.scanner_snapshot_path and not Path(config.scanner_snapshot_path).is_absolute():
        config = replace(config, scanner_snapshot_path=str(config_base / config.scanner_snapshot_path))
    return config


def _refresh_dashboard_scanner_snapshot_if_needed(config: BotConfig) -> None:
    if config.market_data_source != "external-scan-kis":
        return
    if str(config.scanner_source).strip().lower() != "json":
        return
    if _json_scanner_snapshot_ready(config):
        return

    _refresh_json_scanner_snapshot(config)

    if not _json_scanner_snapshot_ready(config):
        raise ValueError("scanner snapshot refresh failed: refreshed snapshot is unavailable")


def _refresh_json_scanner_snapshot(config: BotConfig) -> None:
    snapshot_path = Path(config.scanner_snapshot_path)
    entry_budget = _history_entry_budget_from_config(config)
    try:
        collect_naver_market_scanner_snapshot(
            snapshot_path,
            SnapshotWriteOptions(
                provider="naver-mobile-auto",
                max_price=entry_budget,
            ),
            markets=("all",),
            pages=0,
            page_size=100,
            timeout=10.0,
            minute_history_candidates=DEFAULT_NAVER_MINUTE_HISTORY_CANDIDATES,
            minute_history_workers=DEFAULT_NAVER_MINUTE_HISTORY_WORKERS,
            minute_history_timeout=DEFAULT_NAVER_MINUTE_HISTORY_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        raise ValueError(f"scanner snapshot refresh failed: {exc}") from exc


def _json_scanner_snapshot_ready(config: BotConfig) -> bool:
    if str(config.scanner_snapshot_path or "").strip() == "":
        return False
    provider = JsonScannerProvider(
        config.scanner_snapshot_path,
        max_snapshot_age_seconds=int(config.scanner_snapshot_max_age_seconds),
    )
    try:
        return bool(provider.rank_symbols([]))
    except Exception:
        return False


def _json_scanner_refresh_callback(config: BotConfig) -> Callable[[], None]:
    return lambda: _refresh_json_scanner_snapshot(config)


def _json_scanner_refresh_callback_for(config: BotConfig) -> Callable[[], None] | None:
    if config.market_data_source != "external-scan-kis":
        return None
    if str(config.scanner_source).strip().lower() != "json":
        return None
    return _json_scanner_refresh_callback(config)


def _load_default_symbol_directory() -> SymbolDirectory:
    symbols_path = resolve_app_asset_path("data", "symbols.csv")
    try:
        if symbols_path.exists():
            return load_symbol_directory(symbols_path)
    except Exception:
        return SymbolDirectory({})
    return SymbolDirectory({})


def _load_default_bars(data_path: str) -> list[MarketBar]:
    try:
        return list(read_csv_bars(data_path))
    except Exception:
        return []


def _build_paper_runtime(
    config: BotConfig,
    symbol_directory: SymbolDirectory,
    bars: list[MarketBar] | None = None,
    *,
    data_path: str | None = None,
    rate_limiter: KisRateLimiter | None = None,
    kis_bar_provider: Callable[[str], MarketBar | None] | None = None,
    scanner_provider: ScannerProvider | None = None,
    env_file: str | Path | None = None,
    scanner_refresh_callback: Callable[[], None] | None = None,
    execution_mode: str = "paper",
    entry_history_provider: Callable[[str], Iterable[MarketBar] | None] | None = None,
    max_physical_market_reads_per_cycle: int | None = None,
) -> PaperTradingRuntime:
    config = _with_kis_intraday_rehearsal_safety(config)
    if config.trading_mode != "paper":
        raise ValueError("paper runtime requires trading_mode=paper")
    if (
        config.market_data_source == "external-scan-kis"
        and scanner_provider is not None
        and str(config.scanner_source).strip().lower() == "local"
    ):
        config = replace(config, scanner_source="kiwoom")
    config.validate_safety()
    if config.market_data_source == "external-scan-kis" and bars is not None:
        raise ValueError("external-scan-kis does not accept local bars fallback")
    settings = _custom_settings_from_config(config)
    resolved_data_path = data_path or config.data_path
    symbols = (
        _symbols_from_bars(bars)
        if bars is not None
        else _runtime_symbols_from_config(config, resolved_data_path, symbol_directory)
    )
    if bars is not None:
        bar_provider = CsvCycleBarProvider(bars)
        final_quote_provider = None
        symbol_priority_provider = _local_volume_priority_provider(symbols, bars, data_path=resolved_data_path)
        scanner_provider = BarProviderScanner(
            bar_provider,
            priority_provider=symbol_priority_provider,
            label="CSV scanner",
            kind="local",
        )
        data_source_label = "샘플 CSV"
        data_source_kind = "local"
        scan_limit_per_cycle = config.scan_limit_per_cycle
        max_bar_requests_per_cycle = None
        max_final_quote_requests_per_cycle = None
        runtime_rate_limiter = rate_limiter
    elif config.market_data_source == "external-scan-kis":
        env_path = Path(env_file) if env_file is not None else _default_env_file()
        provider_rate_limiter = rate_limiter or KisRateLimiter()
        final_quote_provider = kis_bar_provider or KisPriceBarProvider(
            env_file=env_path,
            env=_env_override_for(env_path),
            rate_limiter=provider_rate_limiter,
        )
        configured_scanner_provider = _scanner_provider_from_config(
            config,
            scanner_provider,
            refresh_callback=scanner_refresh_callback,
            require_current_minute=execution_mode == "live",
            refresh_failure_retry_seconds=(
                LIVE_SCANNER_REFRESH_FAILURE_RETRY_SECONDS
                if execution_mode == "live"
                else 0.0
            ),
        )
        if configured_scanner_provider is None:
            raise ValueError("external-scan-kis requires scanner_source=json or an injected ScannerProvider")
        strict_scanner = True
        scanner_symbols = _runtime_symbols_from_scanner_provider(
            configured_scanner_provider,
            config,
            strict=strict_scanner,
        )
        if scanner_symbols is not None:
            symbols = scanner_symbols
        bar_provider = NoFallbackBarProvider()
        symbol_priority_provider = _local_volume_priority_provider(symbols, bars, data_path=resolved_data_path)
        scanner_provider = configured_scanner_provider
        scanner_label = str(getattr(scanner_provider, "label", "wide scanner"))
        data_source_label = f"{scanner_label} / KIS final quote paper"
        data_source_kind = "external-scan-kis"
        scan_limit_per_cycle = config.scan_limit_per_cycle
        max_bar_requests_per_cycle = None
        max_final_quote_requests_per_cycle = _kis_final_quote_scan_limit(config)
        runtime_rate_limiter = None
    elif config.market_data_source == "kis-vts":
        env_path = Path(env_file) if env_file is not None else _default_env_file()
        provider_rate_limiter = rate_limiter or KisRateLimiter()
        provider = kis_bar_provider or KisPriceBarProvider(
            env_file=env_path,
            env=_env_override_for(env_path),
            rate_limiter=provider_rate_limiter,
        )
        bar_provider = provider
        final_quote_provider = None
        priority_provider = getattr(provider, "priority", None)
        symbol_priority_provider = priority_provider if callable(priority_provider) else None
        scanner_provider = None
        data_source_label = str(getattr(provider, "data_source_label", "KIS VTS 현재가 / paper 체결"))
        data_source_kind = "kis-vts"
        scan_limit_per_cycle = config.kis_market_data_scan_limit
        max_bar_requests_per_cycle = scan_limit_per_cycle
        max_final_quote_requests_per_cycle = None
        runtime_rate_limiter = None
    else:
        bar_provider = FallbackUniverseBarProvider(
            LazyCsvCycleBarProvider(resolved_data_path),
            symbols=symbols,
        )
        final_quote_provider = None
        symbol_priority_provider = _local_volume_priority_provider(symbols, bars, data_path=resolved_data_path)
        scanner_provider = BarProviderScanner(
            bar_provider,
            priority_provider=symbol_priority_provider,
            label="CSV scanner",
            kind="local",
        )
        data_source_label = "샘플 CSV"
        data_source_kind = "local"
        scan_limit_per_cycle = config.scan_limit_per_cycle
        max_bar_requests_per_cycle = None
        max_final_quote_requests_per_cycle = None
        runtime_rate_limiter = rate_limiter
    strategy = FlowScalperStrategy(
        FlowScalperConfig(
            momentum_window=config.momentum_window,
            min_momentum_pct=config.min_momentum_pct,
            min_short_momentum_pct=-config.min_momentum_pct,
            min_signal_confidence=config.min_signal_confidence,
            volume_window=config.volume_window,
            min_volume_ratio=config.min_volume_ratio,
            max_spread_bps=config.max_spread_bps,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            trailing_stop_pct=config.trailing_stop_pct,
            transaction_tax_pct=config.transaction_tax_pct,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            min_net_profit_pct=config.min_net_profit_pct,
            max_holding_minutes=config.max_holding_minutes,
            daily_loss_exit_amount=config.daily_loss_exit_amount,
            forced_exit_time=config.forced_exit_time,
            allow_paper_short=config.allow_paper_short,
            require_vwap_alignment=config.market_data_source != "kis-vts",
            min_trend_pct=(
                KIS_INTRADAY_REHEARSAL_MIN_TREND_PCT
                if config.market_data_source == "kis-vts"
                else FlowScalperConfig.min_trend_pct
            ),
        )
    )
    runtime = PaperTradingRuntime(
        symbols=symbols,
        broker=PaperBroker(initial_cash=config.initial_cash, allow_short=config.allow_paper_short),
        strategy=strategy,
        risk_manager=RiskManager(
            RiskConfig(
                max_order_amount=config.max_order_amount,
                max_position_amount=config.max_position_amount,
                max_positions=config.max_positions,
                max_daily_loss=config.max_daily_loss,
                max_daily_entries_per_symbol=config.max_daily_entries_per_symbol,
                max_consecutive_order_failures=config.max_consecutive_order_failures,
                kill_switch=config.kill_switch,
            )
        ),
        bar_provider=bar_provider,
        final_quote_provider=final_quote_provider,
        entry_history_provider=entry_history_provider,
        symbol_directory=symbol_directory,
        settings=settings,
        rate_limiter=runtime_rate_limiter,
        market_hours=_market_hours_from_config(config),
        scan_limit_per_cycle=scan_limit_per_cycle,
        max_bar_requests_per_cycle=max_bar_requests_per_cycle,
        max_final_quote_requests_per_cycle=max_final_quote_requests_per_cycle,
        max_physical_market_reads_per_cycle=max_physical_market_reads_per_cycle,
        symbol_priority_provider=symbol_priority_provider,
        scanner_provider=scanner_provider,
        data_source_label=data_source_label,
        data_source_kind=data_source_kind,
        execution_mode=execution_mode,
    )
    if execution_mode != "live" and config.market_data_source in {"kis-vts", "external-scan-kis"}:
        runtime.prewarm_strategy_history(
            _history_bars_by_symbol(resolved_data_path, symbols)
        )
    return runtime


def _custom_settings_from_config(config: BotConfig) -> CustomStrategySettings:
    max_symbol_exposure = (
        config.max_position_amount / config.initial_cash
        if config.initial_cash > 0
        else CustomStrategySettings.default().max_symbol_exposure
    )
    return CustomStrategySettings(
        order_cash_amount=CustomStrategySettings.default().order_cash_amount,
        cash_allocation_pct=Decimal("1.0"),
        max_order_amount=config.max_order_amount,
        max_position_amount=config.max_position_amount,
        max_symbol_exposure=max_symbol_exposure,
        max_positions=config.max_positions,
        max_daily_entries_per_symbol=config.max_daily_entries_per_symbol,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        trailing_stop_pct=config.trailing_stop_pct,
        max_holding_minutes=config.max_holding_minutes,
        daily_loss_limit=config.max_daily_loss,
        allow_paper_short=config.allow_paper_short,
        kill_switch=config.kill_switch,
    )


def _scanner_provider_from_config(
    config: BotConfig,
    injected_provider: ScannerProvider | None,
    *,
    refresh_callback: Callable[[], None] | None = None,
    require_current_minute: bool = False,
    refresh_failure_retry_seconds: float = 0.0,
) -> ScannerProvider | None:
    if injected_provider is not None:
        return injected_provider
    scanner_source = str(config.scanner_source).strip().lower()
    if scanner_source == "local":
        return None
    if scanner_source == "json":
        snapshot_path = Path(config.scanner_snapshot_path)
        if not snapshot_path.exists():
            raise ValueError("scanner_snapshot.json 파일이 없습니다. 외부 수집기로 data 폴더에 scanner_snapshot.json을 먼저 생성하세요")
        return JsonScannerProvider(
            config.scanner_snapshot_path,
            max_snapshot_age_seconds=int(config.scanner_snapshot_max_age_seconds),
            refresh_callback=refresh_callback,
            require_current_minute=require_current_minute,
            refresh_failure_retry_seconds=refresh_failure_retry_seconds,
        )
    raise ValueError(f"scanner_source={scanner_source} requires an injected ScannerProvider")


def _runtime_symbols_from_scanner_provider(
    scanner_provider: ScannerProvider | None,
    config: BotConfig,
    *,
    strict: bool = False,
) -> list[str] | None:
    if scanner_provider is None:
        return None
    try:
        ranked_symbols = scanner_provider.rank_symbols([])
    except Exception as exc:
        if strict:
            raise _scanner_source_unavailable_error(exc) from exc
        return None
    ranked_symbols = list(dict.fromkeys(ranked_symbols))
    if not ranked_symbols:
        if strict:
            try:
                probe = scanner_provider.snapshot([])
            except Exception as exc:
                raise _scanner_source_unavailable_error(exc) from exc
            if probe.diagnostics.messages:
                raise _scanner_source_unavailable_error("; ".join(probe.diagnostics.messages))
        return []

    if not isinstance(scanner_provider, JsonScannerProvider):
        return ranked_symbols

    try:
        snapshot = scanner_provider.snapshot(ranked_symbols)
    except Exception as exc:
        if strict:
            raise _scanner_source_unavailable_error(exc) from exc
        return ranked_symbols
    if not snapshot.bars:
        if strict:
            raise ValueError("configured scanner_source has no usable current prices")
        return ranked_symbols

    priced_symbols = [symbol for symbol in ranked_symbols if symbol in snapshot.bars]
    if not priced_symbols and strict:
        raise ValueError("configured scanner_source has no usable current prices")
    return priced_symbols


def _scanner_source_unavailable_error(cause: object | None = None) -> ValueError:
    detail = _redact_scanner_error_detail(str(cause or "").strip())
    if detail:
        return ValueError(f"configured scanner_source is unavailable: {detail}")
    return ValueError("configured scanner_source is unavailable")


def _redact_scanner_error_detail(detail: str) -> str:
    safe_detail = detail
    for pattern in _SECRET_DETAIL_PATTERNS:
        safe_detail = pattern.sub("[redacted]", safe_detail)
    if "怨꾩쥖" in safe_detail or "계좌" in safe_detail:
        safe_detail = re.sub(r"(怨꾩쥖|계좌)\s*[:= ]*\S*", "[redacted]", safe_detail)
    return safe_detail.strip()


def _with_kis_intraday_rehearsal_safety(config: BotConfig) -> BotConfig:
    if config.trading_mode != "paper" or config.market_data_source not in {"kis-vts", "external-scan-kis"}:
        return config

    if config.market_data_source == "external-scan-kis":
        return config

    relaxed_config = replace(
        config,
        min_momentum_pct=min(config.min_momentum_pct, KIS_INTRADAY_REHEARSAL_MIN_MOMENTUM_PCT),
        min_signal_confidence=min(config.min_signal_confidence, KIS_INTRADAY_REHEARSAL_MIN_SIGNAL_CONFIDENCE),
        min_volume_ratio=min(config.min_volume_ratio, KIS_INTRADAY_REHEARSAL_MIN_VOLUME_RATIO),
    )

    safe_scan_limit = min(
        max(1, int(relaxed_config.kis_market_data_scan_limit)),
        KIS_INTRADAY_REHEARSAL_SCAN_LIMIT,
    )

    requested_positions = int(relaxed_config.max_positions)
    if requested_positions <= 0:
        safe_positions = KIS_INTRADAY_REHEARSAL_MAX_POSITIONS
    else:
        safe_positions = requested_positions
    safe_positions = min(
        max(1, safe_positions),
        KIS_INTRADAY_REHEARSAL_MAX_POSITIONS,
    )

    return replace(
        relaxed_config,
        kis_market_data_scan_limit=safe_scan_limit,
        max_positions=safe_positions,
    )


def _kis_final_quote_scan_limit(config: BotConfig) -> int:
    return min(
        max(1, int(config.kis_market_data_scan_limit)),
        KIS_INTRADAY_REHEARSAL_SCAN_LIMIT,
    )


class CsvCycleBarProvider:
    def __init__(self, bars: list[MarketBar]):
        self._bars_by_symbol: dict[str, list[MarketBar]] = {}
        self._next_index: dict[str, int] = {}
        self._intervals_by_symbol: dict[str, timedelta] = {}
        for bar in bars:
            self._bars_by_symbol.setdefault(bar.symbol, []).append(bar)
        for symbol, symbol_bars in self._bars_by_symbol.items():
            self._intervals_by_symbol[symbol] = _bar_interval(symbol_bars)

    def __call__(self, symbol: str) -> MarketBar | None:
        bars = self._bars_by_symbol.get(symbol)
        if not bars:
            return None
        index = self._next_index.get(symbol, 0)
        self._next_index[symbol] = index + 1
        bar = bars[index % len(bars)]
        if index < len(bars):
            return bar
        return replace(bar, timestamp=bars[0].timestamp + self._intervals_by_symbol[symbol] * index)


class LazyCsvCycleBarProvider:
    def __init__(self, data_path: str):
        self.data_path = data_path
        self._provider: CsvCycleBarProvider | None = None

    def __call__(self, symbol: str) -> MarketBar | None:
        if self._provider is None:
            self._provider = CsvCycleBarProvider(_load_default_bars(self.data_path))
        return self._provider(symbol)


class NoFallbackBarProvider:
    def __call__(self, _symbol: str) -> MarketBar | None:
        return None


class FallbackUniverseBarProvider:
    def __init__(self, base_provider: Callable[[str], MarketBar | None], *, symbols: list[str]):
        self.base_provider = base_provider
        self.symbols = symbols
        self._next_index: dict[str, int] = {}

    def __call__(self, symbol: str) -> MarketBar | None:
        bar = self.base_provider(symbol)
        if bar is not None:
            return bar
        if symbol not in self.symbols:
            return None
        index = self._next_index.get(symbol, 0)
        self._next_index[symbol] = index + 1
        return _synthetic_universe_bar(symbol, index)


def _bar_interval(bars: list[MarketBar]) -> timedelta:
    if len(bars) < 2:
        return timedelta(minutes=1)
    interval = bars[1].timestamp - bars[0].timestamp
    if interval.total_seconds() <= 0:
        return timedelta(minutes=1)
    return interval


def _synthetic_universe_bar(symbol: str, index: int) -> MarketBar:
    symbol_seed = int(symbol) if symbol.isdigit() else sum(ord(char) for char in symbol)
    base_price = Decimal(3000 + (symbol_seed % 17000))
    patterns = (
        (
            Decimal("1.000"),
            Decimal("1.004"),
            Decimal("1.009"),
            Decimal("1.026"),
            Decimal("1.038"),
            Decimal("1.020"),
            Decimal("0.992"),
            Decimal("1.010"),
            Decimal("1.034"),
            Decimal("1.045"),
        ),
        (
            Decimal("1.000"),
            Decimal("1.004"),
            Decimal("1.009"),
            Decimal("1.026"),
            Decimal("1.036"),
            Decimal("1.041"),
            Decimal("1.046"),
            Decimal("1.030"),
            Decimal("1.034"),
            Decimal("1.044"),
        ),
        (
            Decimal("1.000"),
            Decimal("1.004"),
            Decimal("1.009"),
            Decimal("1.026"),
            Decimal("1.061"),
            Decimal("1.020"),
            Decimal("0.992"),
            Decimal("1.010"),
            Decimal("1.034"),
            Decimal("1.071"),
        ),
        (
            Decimal("1.040"),
            Decimal("1.033"),
            Decimal("1.027"),
            Decimal("1.018"),
            Decimal("0.998"),
            Decimal("0.982"),
            Decimal("0.970"),
            Decimal("0.955"),
            Decimal("0.948"),
            Decimal("0.940"),
        ),
    )
    pattern_seed = symbol_seed // 10 if symbol_seed % 10 == 0 else symbol_seed
    pattern_bucket = pattern_seed % len(patterns)
    pattern = patterns[pattern_bucket]
    pattern_index = (index + (symbol_seed % len(pattern))) % len(pattern)
    multiplier = pattern[pattern_index]
    close = (base_price * multiplier).quantize(Decimal("1"))
    volume = 1200 + ((symbol_seed + index) % 300)
    bearish_pattern = pattern_bucket == len(patterns) - 1
    spike_indexes = {4, 7} if bearish_pattern else {3, 8}
    if pattern_index in spike_indexes:
        volume *= 5
    vwap_multiplier = Decimal("1.004") if bearish_pattern else Decimal("0.996")
    vwap = (close * vwap_multiplier).quantize(Decimal("1"))
    bid = max(Decimal("1"), close - Decimal("1"))
    ask = close + Decimal("1")
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 8, 9, 0) + timedelta(minutes=index),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        vwap=vwap,
        bid=bid,
        ask=ask,
    )


def _local_volume_priority_provider(
    symbols: list[str],
    bars: list[MarketBar] | None,
    *,
    data_path: str | None = None,
):
    priority_bars = list(bars or [])
    if priority_bars:
        ranker = VolumePriorityRanker.from_bars(symbols, priority_bars)
        return ranker.priority

    ranker: VolumePriorityRanker | None = None

    def priority(symbol: str) -> float:
        nonlocal ranker
        if ranker is None:
            loaded_bars = _load_default_bars(data_path) if data_path else []
            if not loaded_bars:
                loaded_bars = [
                    _synthetic_universe_bar(item, index)
                    for item in symbols
                    for index in range(10)
                ]
            ranker = VolumePriorityRanker.from_bars(symbols, loaded_bars)
        return ranker.priority(symbol)

    return priority


def _market_hours_from_config(config: BotConfig):
    # Local paper replay is the unrestricted sandbox; broker-backed modes keep market-hours gates.
    if config.trading_mode == "paper":
        if config.market_data_source in {"kis-vts", "external-scan-kis"}:
            if not config.enforce_market_hours:
                return None
            return _regular_market_hours_from_config(config)
        return None
    return _configured_market_hours_from_config(config)


def _configured_market_hours_from_config(config: BotConfig):
    if config.trading_mode == "paper" and config.allow_after_hours_simulation:
        return None
    if not config.enforce_market_hours:
        return None
    return _regular_market_hours_from_config(config)


def _regular_market_hours_from_config(config: BotConfig):
    now = datetime.now(tz=KST)
    years = {now.year, now.year + 1}
    holidays = default_krx_closed_dates(years=years) | parse_closed_dates(config.market_closed_dates)
    return KoreanRegularMarketHours(holidays=holidays)


def _build_live_broker(
    config: BotConfig,
    env_values: dict[str, str],
    *,
    client: KisLiveOrderClient | None,
    env_file: Path,
    live_order_safety_context: LiveOrderSafetyContext | None = None,
) -> LiveBroker:
    if client is None:
        raise ValueError("live broker requires a KIS live order client")
    _validate_live_runtime_config_gate(config)
    order_gate = _live_order_gate_enabled(config, env_values)
    live_config = replace(
        config,
        trading_mode="live",
        market_data_source="local",
        scanner_source="local",
        scanner_snapshot_path="",
        allow_kis_vts_trading=False,
        allow_paper_short=False,
        allow_live_trading=order_gate,
        live_trading_enabled=order_gate,
    )
    market_hours = _regular_market_hours_from_config(config)
    account_no = str(env_values.get("KIS_LIVE_ACCOUNT_NO") or "").strip()
    expected_suffix = account_no[-2:] if len(account_no) >= 2 else account_no
    account_confirmation = str(env_values.get("STOCKBOT_LIVE_ACCOUNT_CONFIRMATION") or "").strip()
    audit_log = JsonlLiveAuditLog(
        _live_audit_path(config, env_file),
        redact_values=tuple(env_values.values()),
    )
    safety_context = live_order_safety_context or LiveOrderSafetyContext()
    managed_ledger_scope = _live_managed_positions_scope(env_values)
    opening_day_gate = _CachedKisOpeningDayGate(client, market_hours)
    return LiveBroker(
        client=client,
        config=live_config,
        env=env_values,
        audit_log=audit_log,
        market_is_open=opening_day_gate,
        session_approved=lambda: bool(safety_context.session_approved),
        account_confirmation=account_confirmation,
        expected_account_suffix=expected_suffix,
        fill_reconciler=KisLiveOrderReconciler(client),
        pending_order_store=JsonPendingLiveOrderStore(
            _live_pending_orders_path(config, env_file, env_values),
            scope=managed_ledger_scope,
        ),
        manual_reconciliation_store=JsonManualReconciliationStore(
            _live_manual_reconciliation_path(config, env_file, env_values),
            scope=managed_ledger_scope,
        ),
        managed_position_ledger=JsonManagedLivePositionLedger(
            _live_managed_positions_path(config, env_file, env_values),
            scope=managed_ledger_scope,
        ),
        risk_limits_ok=lambda: bool(safety_context.risk_limits_ok),
        new_entries_allowed=lambda: bool(safety_context.new_entries_allowed),
    )


def _live_env_values(env_file: Path) -> dict[str, str]:
    return read_env_file(env_file)


def _live_order_gate_enabled(config: BotConfig, env_values: dict[str, str]) -> bool:
    return live_order_gate_configured(config, env_values)


def _live_audit_path(config: BotConfig, env_file: Path) -> Path:
    journal = Path(config.journal_path)
    if journal.is_absolute():
        return journal.parent / "live_orders.jsonl"
    return env_file.resolve().parent / "logs" / "live_orders.jsonl"


def _live_pending_orders_path(
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str] | None = None,
) -> Path:
    base_path = _live_audit_path(config, env_file).with_name("pending_live_orders.json")
    scope = _live_managed_positions_scope(env_values or {})
    if not scope:
        return base_path
    return base_path.with_name(f"pending_live_orders_{scope}.json")


def _live_manual_reconciliation_path(
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str] | None = None,
) -> Path:
    base_path = _live_audit_path(config, env_file).with_name("live_manual_reconciliation_required.json")
    scope = _live_managed_positions_scope(env_values or {})
    if not scope:
        return base_path
    return base_path.with_name(f"live_manual_reconciliation_required_{scope}.json")


def _live_managed_positions_path(
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str] | None = None,
) -> Path:
    base_path = _live_audit_path(config, env_file).with_name("managed_live_positions.json")
    scope = _live_managed_positions_scope(env_values or {})
    if not scope:
        return base_path
    return base_path.with_name(f"managed_live_positions_{scope}.json")


def _live_profit_analytics_path(
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str] | None = None,
) -> Path:
    base_path = _live_audit_path(config, env_file).with_name("profit_analytics.sqlite3")
    scope = _live_managed_positions_scope(env_values or {})
    if not scope:
        return base_path
    return base_path.with_name(f"profit_analytics_{scope}.sqlite3")


def _profit_analytics_service(
    *,
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str],
) -> ProfitAnalyticsService:
    scope = _live_managed_positions_scope(env_values)
    if not scope:
        return ProfitAnalyticsService(account_store=None, managed_ledger=None)
    return ProfitAnalyticsService(
        account_store=_account_profit_store(config, env_file, env_values),
        managed_ledger=JsonManagedLivePositionLedger(
            _live_managed_positions_path(config, env_file, env_values),
            scope=scope,
        ),
    )


def _account_profit_store(
    config: BotConfig,
    env_file: Path,
    env_values: Mapping[str, str],
) -> SqliteAccountProfitStore | None:
    scope = _live_managed_positions_scope(env_values)
    if not scope:
        return None
    return SqliteAccountProfitStore(
        _live_profit_analytics_path(config, env_file, env_values),
        scope=scope,
    )


def _live_managed_positions_scope(env_values: Mapping[str, str]) -> str:
    account_no = str(env_values.get("KIS_LIVE_ACCOUNT_NO") or "").strip()
    product_code = str(env_values.get("KIS_LIVE_ACCOUNT_PRODUCT_CODE") or "").strip()
    return managed_live_position_ledger_scope(account_no, product_code)


def _symbols_from_bars(bars: list[MarketBar]) -> list[str]:
    symbols: list[str] = []
    for bar in bars:
        if bar.symbol not in symbols:
            symbols.append(bar.symbol)
    return symbols or ["005930"]


def _default_runtime_symbols(symbol_directory: SymbolDirectory) -> list[str]:
    symbols = list(symbol_directory.names)
    return symbols or ["005930"]


def _runtime_symbols_from_data_path(data_path: str, symbol_directory: SymbolDirectory) -> list[str]:
    symbols: list[str] = []
    try:
        with Path(data_path).open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                symbol = str(row.get("symbol", "")).strip()
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
    except Exception:
        return _default_runtime_symbols(symbol_directory)
    return _merge_symbols(symbols, _default_runtime_symbols(symbol_directory))


def _runtime_symbols_from_config(
    config: BotConfig,
    data_path: str,
    symbol_directory: SymbolDirectory,
) -> list[str]:
    if config.market_data_source == "kis-vts":
        return _kis_runtime_symbols_from_config(config, data_path, symbol_directory)
    if config.market_data_source == "external-scan-kis":
        return []
    return _runtime_symbols_from_data_path(data_path, symbol_directory)


def _kis_runtime_symbols_from_config(
    config: BotConfig,
    data_path: str,
    symbol_directory: SymbolDirectory,
) -> list[str]:
    configured_symbols = _kis_market_data_symbols(config.kis_market_data_symbols)
    budget = _history_entry_budget_from_config(config)
    if not _is_default_kis_market_data_symbols(config.kis_market_data_symbols):
        return _filter_known_unaffordable_history_symbols(
            configured_symbols,
            data_path,
            entry_budget=budget,
            allow_short_entries=config.allow_paper_short,
            keep_unknown=True,
        )

    history_symbols = _affordable_history_symbols(
        data_path,
        entry_budget=budget,
        allow_short_entries=config.allow_paper_short,
    )
    default_symbols = _filter_known_unaffordable_history_symbols(
        configured_symbols,
        data_path,
        entry_budget=budget,
        allow_short_entries=config.allow_paper_short,
        keep_unknown=True,
    )
    return _merge_symbols(history_symbols, default_symbols)


def _is_default_kis_market_data_symbols(value: str) -> bool:
    return _kis_market_data_symbols(value) == _kis_market_data_symbols(DEFAULT_KIS_MARKET_DATA_SYMBOLS)


def _affordable_history_symbols(
    data_path: str,
    *,
    entry_budget: Decimal,
    allow_short_entries: bool,
) -> list[str]:
    latest_bars, max_volumes = _latest_history_bars_and_volumes(data_path)

    affordable_symbols = [
        symbol
        for symbol, bar in latest_bars.items()
        if _history_bar_is_entry_affordable(
            bar,
            entry_budget=entry_budget,
            allow_short_entries=allow_short_entries,
        )
    ]
    return sorted(
        affordable_symbols,
        key=lambda symbol: (-max_volumes.get(symbol, 0), symbol),
    )


def _filter_known_unaffordable_history_symbols(
    symbols: list[str],
    data_path: str,
    *,
    entry_budget: Decimal,
    allow_short_entries: bool,
    keep_unknown: bool,
) -> list[str]:
    latest_bars, _max_volumes = _latest_history_bars_and_volumes(data_path)
    if not latest_bars:
        return symbols if keep_unknown else []

    filtered_symbols: list[str] = []
    for symbol in symbols:
        bar = latest_bars.get(symbol)
        if bar is None:
            if keep_unknown:
                filtered_symbols.append(symbol)
            continue
        if _history_bar_is_entry_affordable(
            bar,
            entry_budget=entry_budget,
            allow_short_entries=allow_short_entries,
        ):
            filtered_symbols.append(symbol)
    return filtered_symbols


def _latest_history_bars_and_volumes(data_path: str) -> tuple[dict[str, MarketBar], dict[str, int]]:
    latest_bars: dict[str, MarketBar] = {}
    max_volumes: dict[str, int] = {}
    for bar in _load_default_bars(data_path):
        latest_bars[bar.symbol] = bar
        max_volumes[bar.symbol] = max(max_volumes.get(bar.symbol, 0), int(bar.volume))
    return latest_bars, max_volumes


def _history_bar_is_entry_affordable(
    bar: MarketBar,
    *,
    entry_budget: Decimal,
    allow_short_entries: bool,
) -> bool:
    buy_price = entry_reference_price(bar)
    if entry_affordability_issue(buy_price, entry_budget) is None:
        return True
    if not allow_short_entries:
        return False
    sell_price = entry_reference_price(bar, "SHORT_ENTRY")
    return entry_affordability_issue(sell_price, entry_budget) is None


def _history_entry_budget_from_config(config: BotConfig) -> Decimal:
    initial_cash = Decimal(str(config.initial_cash))
    if initial_cash <= 0:
        return Decimal("0")

    budget = initial_cash
    if config.max_position_amount > 0:
        budget = min(budget, Decimal(str(config.max_position_amount)))
    return max(Decimal("0"), budget)


def _kis_market_data_symbols(value: str) -> list[str]:
    symbols: list[str] = []
    for raw_symbol in str(value).replace("\n", ",").split(","):
        symbol = raw_symbol.strip()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols or ["005930"]


def _history_bars_by_symbol(data_path: str, symbols: list[str]) -> dict[str, list[MarketBar]]:
    symbol_set = set(symbols)
    bars_by_symbol: dict[str, list[MarketBar]] = {}
    for bar in _load_default_bars(data_path):
        if bar.symbol in symbol_set:
            bars_by_symbol.setdefault(bar.symbol, []).append(bar)
    return bars_by_symbol


def _default_env_file() -> Path:
    candidates: list[Path] = []
    executable = Path(sys.executable).resolve()
    if getattr(sys, "frozen", False):
        candidates.extend(
            [
                executable.parent / ".env",
                executable.parent.parent / ".env",
                executable.parent.parent.parent / ".env",
            ]
        )
    candidates.extend(
        [
            Path.cwd() / ".env",
            Path.cwd().parent / ".env",
            Path.cwd().parent.parent / ".env",
            resolve_app_asset_path(".env"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(".env")


def _env_override_for(env_file: Path) -> dict[str, str] | None:
    return {} if env_file.exists() else None


def _merge_symbols(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    for group in groups:
        for symbol in group:
            if symbol and symbol not in merged:
                merged.append(symbol)
    return merged or ["005930"]
