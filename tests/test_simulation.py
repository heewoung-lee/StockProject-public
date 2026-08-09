import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from stockbot.broker import PaperBroker
from stockbot.market_hours import KST, KoreanRegularMarketHours
from stockbot.models import MarketBar, Signal
from stockbot.risk import RiskConfig, RiskManager
from stockbot.runtime import CustomStrategySettings, PaperTradingRuntime
from stockbot.simulation import run_local_simulation
from stockbot.symbols import SymbolDirectory


def _bar(symbol="005930", close="10000", offset=0):
    price = Decimal(close)
    return MarketBar(
        symbol=symbol,
        timestamp=datetime(2026, 6, 11, 9, 0) + timedelta(minutes=offset),
        open=price,
        high=price,
        low=price,
        close=price,
        volume=1000,
        vwap=price,
        bid=price,
        ask=price,
    )


class CyclingProvider:
    def __init__(self, bars):
        self.bars = list(bars)
        self.index = 0

    def __call__(self, symbol):
        bar = self.bars[self.index % len(self.bars)]
        self.index += 1
        return bar


class BuyThenSellStrategy:
    def on_bar(self, bar, account):
        if bar.symbol in account.positions:
            return [Signal.sell(bar.symbol, "simulation_exit")]
        return [Signal.buy(bar.symbol, "simulation_entry")]


class SimulationRunnerTest(unittest.TestCase):
    def test_local_simulation_runs_requested_cycles_even_when_market_is_closed(self):
        market_hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=BuyThenSellStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=CyclingProvider([_bar(close="10000"), _bar(close="11000", offset=1)]),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=CustomStrategySettings.default(),
            market_hours=market_hours,
        )

        report = run_local_simulation(runtime, cycles=2)

        self.assertEqual(2, report.cycles_completed)
        self.assertEqual(2, runtime.cycle_count)
        self.assertEqual(2, report.filled_trades)
        self.assertEqual(Decimal("30000"), report.realized_pnl)
        self.assertEqual(Decimal("30000"), report.total_pnl)
        self.assertEqual(0, report.open_positions)
        self.assertEqual(market_hours, runtime.market_hours)
        self.assertFalse(runtime.status.running)

    def test_local_simulation_rejects_non_positive_cycle_count(self):
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=BuyThenSellStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=CyclingProvider([_bar()]),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=CustomStrategySettings.default(),
        )

        with self.assertRaisesRegex(ValueError, "cycles"):
            run_local_simulation(runtime, cycles=0)

    def test_local_simulation_restores_gates_when_cycle_raises(self):
        market_hours = KoreanRegularMarketHours(clock=lambda: datetime(2026, 6, 11, 20, 0, tzinfo=KST))
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=BuyThenSellStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=CyclingProvider([_bar()]),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=CustomStrategySettings.default(),
            market_hours=market_hours,
        )
        limiter = object()
        runtime.rate_limiter = limiter

        def fail_cycle():
            raise RuntimeError("boom")

        runtime.run_cycle = fail_cycle

        with self.assertRaises(RuntimeError):
            run_local_simulation(runtime, cycles=1)

        self.assertEqual(market_hours, runtime.market_hours)
        self.assertEqual(limiter, runtime.rate_limiter)
        self.assertFalse(runtime.status.running)

    def test_local_simulation_keeps_runtime_running_when_it_was_already_running(self):
        runtime = PaperTradingRuntime(
            symbols=["005930"],
            broker=PaperBroker(initial_cash=Decimal("1000000")),
            strategy=BuyThenSellStrategy(),
            risk_manager=RiskManager(RiskConfig(max_order_amount=Decimal("100000"))),
            bar_provider=CyclingProvider([_bar(close="10000"), _bar(close="11000", offset=1)]),
            symbol_directory=SymbolDirectory({"005930": "Samsung Electronics"}),
            settings=CustomStrategySettings.default(),
        )
        runtime.start()

        run_local_simulation(runtime, cycles=1)

        self.assertTrue(runtime.status.running)


if __name__ == "__main__":
    unittest.main()
