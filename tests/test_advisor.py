import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from stockbot.advisor import StrategyAdvisor
from stockbot.config import BotConfig
from stockbot.models import MarketBar


def make_bar(index, close, volume=1000, bid=None, ask=None):
    close_decimal = Decimal(str(close))
    return MarketBar(
        symbol="005930",
        timestamp=datetime(2026, 6, 10, 9, 0) + timedelta(minutes=index),
        open=close_decimal,
        high=close_decimal,
        low=close_decimal,
        close=close_decimal,
        volume=volume,
        vwap=close_decimal,
        bid=Decimal(str(bid)) if bid is not None else close_decimal - Decimal("0.05"),
        ask=Decimal(str(ask)) if ask is not None else close_decimal + Decimal("0.05"),
    )


class StrategyAdvisorTest(unittest.TestCase):
    def test_recommends_conservative_profile_for_wide_spreads_and_high_volatility(self):
        bars = [
            make_bar(0, 100, bid=99, ask=101),
            make_bar(1, 106, bid=104, ask=108),
            make_bar(2, 94, bid=92, ask=96),
            make_bar(3, 103, bid=101, ask=105),
        ]

        recommendation = StrategyAdvisor().recommend(BotConfig.default(), bars)

        self.assertEqual("conservative", recommendation.recommended_profile)
        self.assertIn("spread", " ".join(recommendation.reasons))
        self.assertEqual("30000", recommendation.suggested_changes["order_cash_amount"])
        self.assertEqual("0.012", recommendation.suggested_changes["stop_loss_pct"])

    def test_recommends_aggressive_profile_for_clean_momentum_and_volume(self):
        bars = [
            make_bar(0, 100, volume=1000),
            make_bar(1, 101, volume=1100),
            make_bar(2, 102, volume=1200),
            make_bar(3, 104, volume=4000),
        ]

        recommendation = StrategyAdvisor().recommend(BotConfig.default(), bars)

        self.assertEqual("aggressive", recommendation.recommended_profile)
        self.assertIn("momentum", " ".join(recommendation.reasons))
        self.assertEqual("80000", recommendation.suggested_changes["order_cash_amount"])
        self.assertEqual("0.03", recommendation.suggested_changes["stop_loss_pct"])

    def test_recommendation_serializes_to_json_ready_dict(self):
        bars = [
            make_bar(0, 100),
            make_bar(1, 101),
            make_bar(2, 102),
            make_bar(3, 103),
        ]

        payload = StrategyAdvisor().recommend(BotConfig.default(), bars).to_dict()

        self.assertIn(payload["recommended_profile"], {"conservative", "balanced", "aggressive"})
        self.assertIn(payload["confidence"], {"low", "medium", "high"})
        self.assertIsInstance(payload["reasons"], list)
        self.assertIsInstance(payload["suggested_changes"], dict)


if __name__ == "__main__":
    unittest.main()
