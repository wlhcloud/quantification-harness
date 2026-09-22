"""Unit tests for the ETF Quant MVP (additive; stock tests untouched)."""
from __future__ import annotations

import math
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

import shutil

from quant_engine.etf_quant import backtest as bt
from quant_engine.etf_quant import db as etf_db
from quant_engine.etf_quant import factors as ef
from quant_engine.etf_quant import ranker as etf_ranker
from quant_engine.etf_quant import research as rs
from quant_engine.etf_quant import runner as etf_runner


def _make_market(tmp: Path, codes: list[str] = ("510300.SH", "510500.SH", "159915.SZ", "512880.SH"),
                 days: int = 150, seed: int = 7) -> Path:
    rng = np.random.default_rng(seed)
    market = tmp / "market.db"
    con = sqlite3.connect(market)
    con.execute("CREATE TABLE etf_metadata (ts_code TEXT, name TEXT, eligible INTEGER)")
    for i, code in enumerate(codes):
        con.execute("INSERT INTO etf_metadata VALUES(?,?,1)", (code, f"测试ETF{i}"))
    con.execute("CREATE TABLE etf_daily_bars (ts_code TEXT, trade_date TEXT, close REAL, pct_chg REAL, vol REAL, amount REAL)")
    con.execute("CREATE TABLE index_daily_bars (ts_code TEXT, trade_date TEXT, close REAL)")
    dates = [str(d) for d in range(20260101, 20260101 + days)]
    for i, code in enumerate(codes):
        close = 3.0 + i * 0.5 + np.cumsum(rng.normal(0, 0.01, days))
        close = np.maximum(close, 1.0)
        for j, d in enumerate(dates):
            pct = close[j] / close[j - 1] - 1 if j else 0.0
            con.execute("INSERT INTO etf_daily_bars VALUES(?,?,?,?,?,?)",
                        (code, d, round(float(close[j]), 4), round(float(pct * 100), 4),
                         int(1e6 + j * 1000 + i * 1e5), 5e7 + j * 100 + i * 1e6))
    bench_close = 4000.0 + np.cumsum(rng.normal(0.0005, 0.008, days))
    for j, d in enumerate(dates):
        con.execute("INSERT INTO index_daily_bars VALUES('000300.SH',?,?)", (d, round(float(bench_close[j]), 4)))
    con.commit()
    con.close()
    return market


class FactorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.market = _make_market(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_eligible_pool_filters(self):
        codes = ef.eligible_pool(self.market, {"excludeNamePatterns": ["测试ETF1"], "maxEligible": 10})
        self.assertNotIn("510500.SH", codes)
        self.assertIn("510300.SH", codes)

    def test_compute_factors_basic(self):
        codes = ef.eligible_pool(self.market, {"maxEligible": 10})
        frame = ef.compute_factors(self.market, codes, "000300.SH", {"minHistoryDays": 40})
        self.assertFalse(frame.empty)
        self.assertIn("mom60", frame.columns)
        self.assertIn("forward_20", frame.columns)
        # spot check momentum on first code
        one = frame[frame["ts_code"] == "510300.SH"].sort_values("trade_date").iloc[-1]
        self.assertTrue(math.isfinite(one["mom20"]))
        self.assertTrue(math.isfinite(one["rs20"]))
        # history filter respected: first 39 rows dropped
        self.assertGreaterEqual(len(frame) // len(codes), 40)

    def test_no_future_leak_in_features(self):
        codes = ef.eligible_pool(self.market, {"maxEligible": 10})
        frame = ef.compute_factors(self.market, codes, "000300.SH", {"minHistoryDays": 40})
        one = frame[frame["ts_code"] == "510300.SH"].sort_values("trade_date").iloc[-1]
        # last row has no forward label (no future bar)
        self.assertTrue(pd.isna(one["forward_5"]))


class ResearchTests(unittest.TestCase):
    def test_rank_ic_perfect_positive(self):
        frame = [{"trade_date": "20260101", "ts_code": f"T{i}", "mom60": float(i + 1), "forward_20": 0.01 * (i + 1)}
                 for i in range(6)]
        frame += [{"trade_date": "20260102", "ts_code": f"T{i}", "mom60": float(6 - i), "forward_20": 0.01 * (6 - i)}
                  for i in range(6)]
        result = rs.run_research(frame, "forward_20", 1)
        mom = next(r for r in result["results"] if r["factor"] == "mom60")
        self.assertAlmostEqual(mom["rankIc"], 1.0, places=3)
        self.assertGreater(mom["icPositive"], 0.9)


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.market = _make_market(Path(self._tmp.name), days=90)

    def tearDown(self):
        self._tmp.cleanup()

    def test_backtest_top_n(self):
        dates = [str(d) for d in range(20260101, 20260101 + 80)]
        frame = [{"ts_code": f"C{i}", "trade_date": d, "mom20": float(i), "mom60": float(i),
                  "mom120": float(i), "rs20": 0.0, "rs60": 0.0, "trend_quality": 0.0, "ma_alignment": 1.0,
                  "vol_ratio": 0.0, "amount_trend": 0.0, "volatility20": 0.2, "drawdown60": 0.0,
                  "log_amount20": 10.0, "forward_20": 0.01}
                 for d in dates for i in range(4)]

        class FakeModel:
            def predict(self, x):
                return np.asarray(x)[:, 0]

        codes = [f"C{i}" for i in range(4)]
        bars = pd.DataFrame({"ts_code": [c for c in codes for _ in dates],
                             "trade_date": dates * len(codes),
                             "close": [3.0 + i * 0.1 for i in range(4) for _ in dates]})
        bench = pd.Series(4000.0, index=dates)
        result = bt.run_backtest(frame, FakeModel(), bars, bench,
                                 {"topN": 2, "rebalanceDays": 20, "costBps": 3},
                                 start_date=dates[0], end_date=dates[-1])
        self.assertEqual(len(result["tradesDetail"]), result["trades"] * 2)
        self.assertGreaterEqual(result["days"], 40)
        self.assertTrue(math.isfinite(result["totalReturn"]))


    def test_backtest_factor_score_mode(self):
        dates = [str(d) for d in range(20260101, 20260101 + 60)]
        frame = [{"ts_code": f"C{i}", "trade_date": d, "mom20": float(i), "mom60": float(i),
                  "mom120": float(i), "rs20": 0.0, "rs60": float(i), "trend_quality": 0.0, "ma_alignment": 1.0,
                  "vol_ratio": 0.0, "amount_trend": 0.0, "volatility20": 0.2, "drawdown60": 0.0,
                  "log_amount20": 10.0, "forward_20": 0.01}
                 for d in dates for i in range(4)]
        codes = [f"C{i}" for i in range(4)]
        bars = pd.DataFrame({"ts_code": [c for c in codes for _ in dates],
                             "trade_date": dates * len(codes),
                             "close": [3.0 + i * 0.1 for i in range(4) for _ in dates]})
        bench = pd.Series(4000.0, index=dates)
        result = bt.run_backtest(frame, None, bars, bench,
                                 {"topN": 2, "rebalanceDays": 20, "costBps": 3, "scoreMode": "factor:rs60"},
                                 start_date=dates[0], end_date=dates[-1])
        self.assertTrue(math.isfinite(result["totalReturn"]))

    def test_backtest_regime_bear_scale(self):
        dates = [str(d) for d in range(20260101, 20260101 + 60)]
        frame = [{"ts_code": f"C{i}", "trade_date": d, "mom20": float(i), "mom60": float(i),
                  "mom120": float(i), "rs20": 0.0, "rs60": float(i), "trend_quality": 0.0, "ma_alignment": 1.0,
                  "vol_ratio": 0.0, "amount_trend": 0.0, "volatility20": 0.2, "drawdown60": 0.0,
                  "log_amount20": 10.0, "forward_20": 0.01}
                 for d in dates for i in range(4)]
        codes = [f"C{i}" for i in range(4)]
        bars = pd.DataFrame({"ts_code": [c for c in codes for _ in dates],
                             "trade_date": dates * len(codes),
                             "close": [3.0 + i * 0.1 for i in range(4) for _ in dates]})
        # declining benchmark -> bear regime for most of the window
        bench = pd.Series([4000.0 - 5 * j for j in range(60)], index=dates)
        base = bt.run_backtest(frame, None, bars, bench,
                               {"topN": 2, "rebalanceDays": 20, "costBps": 3, "scoreMode": "factor:rs60"},
                               start_date=dates[0], end_date=dates[-1])
        gated = bt.run_backtest(frame, None, bars, bench,
                                {"topN": 2, "rebalanceDays": 20, "costBps": 3, "scoreMode": "factor:rs60",
                                 "regimeFilter": {"enabled": True, "maWindow": 10, "bearWeight": 0.0}},
                                start_date=dates[0], end_date=dates[-1])
        # zero exposure in bear should dampen losses when benchmark falls
        self.assertGreaterEqual(gated["totalReturn"], base["totalReturn"] - 1e-9)


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="etf-wf-test-"))
        self.market = self.tmp / "market.db"
        self.qdb = self.tmp / "etf-quant.db"
        dates = [str(d) for d in range(20260101, 20260101 + 80)]
        codes = [f"C{i}" for i in range(4)]
        con = sqlite3.connect(self.market)
        con.executescript("""CREATE TABLE etf_daily_bars(ts_code TEXT, trade_date TEXT, close REAL,
            pct_chg REAL, vol REAL, amount REAL);
            CREATE TABLE index_daily_bars(ts_code TEXT, trade_date TEXT, close REAL);""")
        for c in codes:
            for j, d in enumerate(dates):
                con.execute("INSERT INTO etf_daily_bars VALUES(?,?,?,?,?,?)",
                            (c, d, 3.0 + (j % 4) * 0.05, 0.001, 1e6, 1e8))
        for j, d in enumerate(dates):
            con.execute("INSERT INTO index_daily_bars VALUES(?,?,?)", ("000300.SH", d, 3000.0 + j))
        con.commit()
        con.close()
        qcon = etf_db.connect(self.qdb)
        frame = []
        for i, c in enumerate(codes):
            for j, d in enumerate(dates):
                row = {"ts_code": c, "trade_date": d,
                       "mom20": float(i), "mom60": float(i), "mom120": float(i),
                       "rs20": 0.0, "rs60": float(i), "trend_quality": 0.0, "ma_alignment": 1.0,
                       "vol_ratio": 0.0, "amount_trend": 0.0, "volatility20": 0.2,
                       "drawdown60": 0.0, "log_amount20": 10.0,
                       "rs60_vol_adj": float(i), "mom_accel": 0.0, "skew20": 0.0,
                       "drawdown20": 0.0, "amount_share": float(i) / 4.0,
                       "forward_5": 0.005 if j + 5 < len(dates) else None,
                       "forward_20": 0.01 if j + 20 < len(dates) else None}
                frame.append(row)
        import pandas as pd  # noqa: PLC0415
        etf_db.write_factor_snapshots(qcon, pd.DataFrame(frame))
        qcon.close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_walkforward_smoke(self):
        cfg = {"research": {"label": "forward_20", "minDays": 40},
               "model": {"type": "lgbm_ranker", "nEstimators": 20, "learningRate": 0.1,
                         "numLeaves": 7, "minDataInLeaf": 5, "subsample": 0.9,
                         "colsampleBytree": 0.9, "earlyStopping": 10, "randomState": 42,
                         "validationDays": 10, "testDays": 10, "labelRelative": True,
                         "featuresOverride": ["mom20", "mom60", "vol_ratio", "amount_share"]},
               "backtest": {"topN": 2, "rebalanceDays": 5, "costBps": 3}}
        result = etf_runner.run_walkforward(
            self.qdb, self.market, self.tmp, cfg,
            parameters={"walkforward": {"minTrain": 30, "validDays": 10, "testDays": 10, "stepDays": 10}})
        self.assertGreaterEqual(result["metrics"]["windows"], 1)
        self.assertIn("totalReturn", result["metrics"])
        self.assertIn("sharpe", result["metrics"])
        self.assertEqual(len(result["holdings"]["holdings"]), 2)  # topN=2
        self.assertIn("tradeDate", result["holdings"])
        latest = etf_runner.latest_walkforward(self.qdb)
        self.assertEqual(latest["run_id"], result["runId"])
        self.assertTrue(latest["daily"])

    def test_cpu_final_refit_is_reproducible_and_uses_all_rows(self):
        frame = []
        for day in range(12):
            for code in range(8):
                frame.append({"trade_date": f"202601{day + 1:02d}", "code": f"C{code}",
                              "mom20": float(code + day / 10),
                              "mom60": float((code * 3 + day) % 11),
                              "forward_3": float(code - 3) / 100 + day / 10000})
        cfg = {"deviceType": "cpu", "cpuNJobs": 2, "deterministic": True,
               "forceColWise": True, "randomState": 42, "learningRate": 0.05,
               "numLeaves": 7, "minDataInLeaf": 2, "labelGrades": 5,
               "featuresOverride": ["mom20", "mom60"], "subsample": 1.0,
               "colsampleBytree": 0.8}
        one = etf_ranker.train_ranker_final(frame, "forward_3", cfg, self.tmp / "final-one", 15)
        two = etf_ranker.train_ranker_final(frame, "forward_3", cfg, self.tmp / "final-two", 15)
        self.assertEqual(one["trainRows"], len(frame))
        self.assertEqual(one["trainEnd"], "20260112")
        self.assertEqual(Path(one["modelPath"]).read_bytes(), Path(two["modelPath"]).read_bytes())


if __name__ == "__main__":
    unittest.main()
