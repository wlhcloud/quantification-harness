"""Unit tests for 熊市空仓离线诊断（只读分析，构造临时库，不触碰真实数据）。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine.stock import regime_analysis as ra


class RegimeAnalysisTest(unittest.TestCase):
    def _build(self, tmp: Path) -> tuple[Path, Path]:
        # 指数：构造 40 天，前 25 天缓涨（牛市），后 15 天跌破 MA20（熊市）
        mk = sqlite3.connect(tmp / "market.db")
        mk.execute("CREATE TABLE index_daily_bars (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT)")
        base = "202201"
        rows = []
        price = 100.0
        for i in range(40):
            d = f"2022{(i // 28) + 1:02d}{(i % 28) + 1:02d}"
            # 0-24 上行，25-39 快速下行
            price = price + 1.0 if i < 25 else price - 4.0
            rows.append(("000300.SH", d, price, price, price, price, price, 0, 0, 0, 0, "x"))
        mk.executemany("INSERT INTO index_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        mk.commit(); mk.close()
        # 回测：2 期，一期牛市一期熊市，每期净收益 -10%
        bt = sqlite3.connect(tmp / "backtest.db")
        bt.executescript("""
            CREATE TABLE backtest_runs (run_id TEXT, started_at TEXT, finished_at TEXT, status TEXT, config_json TEXT, summary_json TEXT, error TEXT);
            CREATE TABLE backtest_periods (run_id TEXT, model_id TEXT, signal_date TEXT, entry_date TEXT, exit_date TEXT, holdings INTEGER, skipped INTEGER, gross_return REAL, net_return REAL, benchmark_return REAL, turnover REAL);
        """)
        bt.execute("INSERT INTO backtest_runs VALUES ('r1','t','t','complete','{}','{}',NULL)")
        bull_d = rows[20][1]; bear_d = rows[35][1]
        for d in (bull_d, bear_d):
            bt.execute("INSERT INTO backtest_periods VALUES ('r1','m1',?,?,?,30,0,-0.10,-0.10,-0.02,1.0)", (d, d, d))
        bt.commit(); bt.close()
        return tmp / "backtest.db", tmp / "market.db"

    def test_metrics_compound(self):
        m = ra._metrics([0.1, -0.1, 0.2])
        self.assertAlmostEqual(m["finalNav"], 1.1 * 0.9 * 1.2, places=9)
        self.assertEqual(m["periods"], 3)
        self.assertLess(m["maxDrawdown"], 0)

    def test_bear_period_flattened(self):
        with tempfile.TemporaryDirectory() as d:
            bt, mk = self._build(Path(d))
            out = ra.analyze(bt, mk, "r1")
            m1 = out["models"]["m1"]
            # 原始两期均 -10%：净值 0.9*0.9=0.81
            self.assertAlmostEqual(m1["raw"]["finalNav"], 0.81, places=6)
            # 熊期被空仓（收益0），只吃牛市那期 -10%：净值 0.9
            self.assertEqual(m1["attribution"]["bearPeriods"], 1)
            self.assertEqual(m1["attribution"]["bullPeriods"], 1)
            self.assertAlmostEqual(m1["timed"]["finalNav"], 0.9, places=6)
            self.assertGreater(m1["timed"]["totalReturn"], m1["raw"]["totalReturn"])


if __name__ == "__main__":
    unittest.main()
