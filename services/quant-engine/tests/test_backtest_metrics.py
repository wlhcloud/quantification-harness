import unittest

from quant_engine.backtest import annualised, rebalance_dates


class BacktestMetricsTest(unittest.TestCase):
    def test_empty_returns_are_stable(self):
        result = annualised([])
        self.assertEqual(result["totalReturn"], 0)
        self.assertEqual(result["sharpe"], 0)

    def test_sharpe_uses_period_excess_returns(self):
        result = annualised([0.01, 0.02, -0.01, 0.03], 0.02)
        self.assertGreater(result["sharpe"], 0)
        self.assertGreater(result["volatility"], 0)

    def test_rebalance_dates_need_warmup(self):
        dates = [f"2025{month:02d}{day:02d}" for month in range(1, 13) for day in range(1, 21)]
        signals = rebalance_dates(dates)
        self.assertTrue(signals)
        self.assertGreaterEqual(dates.index(signals[0]), 120)


if __name__ == "__main__":
    unittest.main()
