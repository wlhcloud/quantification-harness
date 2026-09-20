"""防未来函数的黄金测试（PIT golden test）。

分析报告点名缺口："尚未有『删掉 t 之后的数据重算，t 日特征逐位一致』的断言"。
本文件验证 ETF 因子链路的因果性：

  1. 只在**尾部截断**样本（头部、每只标的的索引位置、交易日集合的前缀都不变），
     因此任何"用了未来信息"的实现都会在 t 日特征上产生差异；
  2. 比较全部因子列（`forward_5/forward_20` 是训练标签，天然含未来信息，不参与比较）；
  3. 要求**精确相等**（同样输入 + 同样运算顺序），不做容差；
  4. 反向对照：确认尾部截断确实改变了标签列，证明比较不是"空跑"。

注意：本测试只验证"计算因果性"，不改变任何口径（标的池的幸存者偏差、标签用未复权价等
已知限制见 docs/operations.md 与 `stock_ml.audit_label_price_basis`）。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from quant_engine import stock_ml
from quant_engine.etf_quant import factors as ef

CODES = ["510300.SH", "510500.SH", "159915.SZ", "512880.SH"]
BENCH = "000300.SH"
_MAX_DAYS = 400  # 固定长度抽样后再切片：保证不同 days 的构建共享完全相同的前缀行情


def _dates(count: int) -> list[str]:
    start = datetime(2026, 1, 1)
    return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(count)]


def _build_market(root: Path, days: int, seed: int = 11) -> Path:
    """构造可复现的合成行情（只写市场库，不含行业指数 → ind_rs 为空）。

    关键：先按 _MAX_DAYS 抽满再切前 days 行。否则同一 seed 下不同 days 会消耗不同的随机数，
    导致"截断样本"的前缀行情与完整样本并不相同（本测试第一版就踩了这个坑）。
    """
    rng = np.random.default_rng(seed)
    dates = _dates(_MAX_DAYS)
    market = root / "market.db"
    root.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(market)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS etf_daily_bars(ts_code TEXT, trade_date TEXT, close REAL,
            pct_chg REAL, vol REAL, amount REAL);
        CREATE TABLE IF NOT EXISTS index_daily_bars(ts_code TEXT, trade_date TEXT, close REAL);
    """)
    for index, code in enumerate(CODES):
        close = np.maximum(3.0 + index * 0.7 + np.cumsum(rng.normal(0, 0.02, _MAX_DAYS)), 0.5)
        pct = np.concatenate([[0.0], (close[1:] / close[:-1] - 1) * 100])
        vol = 1e6 + rng.integers(0, 1000, _MAX_DAYS)
        amount = 5e7 + rng.random(_MAX_DAYS) * 1e6 + index * 1e6
        con.executemany("INSERT INTO etf_daily_bars VALUES(?,?,?,?,?,?)",
                        [(code, dates[i], float(close[i]), float(pct[i]), float(vol[i]), float(amount[i]))
                         for i in range(days)])
    bench_close = 4000 + np.cumsum(rng.normal(0.0003, 0.01, _MAX_DAYS))
    con.executemany("INSERT INTO index_daily_bars VALUES(?,?,?)",
                    [(BENCH, dates[i], float(bench_close[i])) for i in range(days)])
    con.commit()
    con.close()
    return market


class NoLookaheadGoldenTest(unittest.TestCase):
    DAYS = 200
    CUTOFF_INDEX = 150  # 截断点：保留前 150 个交易日

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dates = _dates(self.DAYS)
        self.cutoff = self.dates[self.CUTOFF_INDEX - 1]
        self.full_market = _build_market(self.root / "full", self.DAYS)
        self.part_market = _build_market(self.root / "part", self.CUTOFF_INDEX)

    def _factors(self, market: Path) -> pd.DataFrame:
        return ef.compute_factors(market, CODES, BENCH, {"minHistoryDays": 60, "minAvgAmount20": 0})

    def test_features_are_identical_when_future_rows_are_removed(self):
        full = self._factors(self.full_market)
        part = self._factors(self.part_market)
        self.assertGreater(len(full), len(part), "尾部截断应减少样本数")

        factor_cols = [c for c in ef.FACTOR_NAMES if c in full.columns]
        self.assertGreaterEqual(len(factor_cols), 10)
        left = full[full["trade_date"] <= self.cutoff].set_index(["ts_code", "trade_date"])[factor_cols].sort_index()
        right = part[part["trade_date"] <= self.cutoff].set_index(["ts_code", "trade_date"])[factor_cols].sort_index()
        self.assertEqual(len(left), len(right))
        self.assertTrue(left.index.equals(right.index), "截断前后可比样本的行集合应完全一致")
        # 精确相等：任何"用了未来数据"的实现都会在这里失败
        pd.testing.assert_frame_equal(left, right, check_exact=True)

    def test_labels_do_differ_at_the_tail_proving_comparison_is_sensitive(self):
        """反向对照：标签列在截断后确实变化，说明上面的精确比较不是空跑。"""
        full = self._factors(self.full_market)
        part = self._factors(self.part_market)
        tail_full = full[full["trade_date"] == self.cutoff].set_index("ts_code")["forward_5"]
        tail_part = part[part["trade_date"] == self.cutoff].set_index("ts_code")["forward_5"]
        # 截断样本在最后一天没有未来 5 日数据 -> 标签为 NaN；完整样本有值
        self.assertTrue(tail_full.notna().all(), "完整样本的 forward_5 在截断点应有值")
        self.assertTrue(tail_part.isna().all(), "截断样本的 forward_5 在末段应为 NaN")
        self.assertFalse(tail_full.equals(tail_part))

    def test_rolling_windows_do_not_reach_forward(self):
        """逐列自检：构造"未来暴涨"的尾部，前段因子不受影响（只影响标签）。"""
        altered = self.root / "altered"
        altered.mkdir(parents=True, exist_ok=True)
        market = _build_market(altered, self.DAYS)
        con = sqlite3.connect(market)
        # 把最后 10 个交易日的收盘价整体抬升 3 倍（纯未来信息），并同步 pct_chg
        con.execute("UPDATE etf_daily_bars SET close = close * 3.0 WHERE trade_date > ?",
                    (self.dates[self.DAYS - 11],))
        con.execute("UPDATE index_daily_bars SET close = close * 3.0 WHERE trade_date > ?",
                    (self.dates[self.DAYS - 11],))
        con.commit()
        con.close()

        baseline = self._factors(self.full_market)
        shifted = self._factors(market)
        factor_cols = [c for c in ef.FACTOR_NAMES if c in baseline.columns]
        horizon = self.dates[self.DAYS - 11]  # 末段变化只允许影响该日之前的 forward 窗口
        left = baseline[baseline["trade_date"] <= horizon].set_index(["ts_code", "trade_date"])[factor_cols].sort_index()
        right = shifted[shifted["trade_date"] <= horizon].set_index(["ts_code", "trade_date"])[factor_cols].sort_index()
        self.assertTrue(left.index.equals(right.index))
        pd.testing.assert_frame_equal(left, right, check_exact=True)


class StockMlNoLookaheadGoldenTest(unittest.TestCase):
    """同一黄金口径用于股票 ML 因子构建（SQL 窗口因子 + _rolling_corr）。"""

    DAYS = 200
    CUTOFF_INDEX = 150
    MAX_DAYS = 400
    CODES = ["000001.SZ", "600000.SH", "600519.SH"]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dates = _dates(self.DAYS)
        self.cutoff = self.dates[self.CUTOFF_INDEX - 1]

    def _build(self, name: str, days: int) -> tuple[Path, Path]:
        root = self.root / name
        root.mkdir(parents=True, exist_ok=True)
        market, factors = root / "market.db", root / "factors.db"
        rng = np.random.default_rng(23)
        con = sqlite3.connect(market)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS daily_bars(code TEXT, trade_date TEXT, open REAL, high REAL,
                low REAL, close REAL, pre_close REAL, change REAL, pct_chg REAL, volume REAL, amount REAL);
            CREATE TABLE IF NOT EXISTS daily_basic(code TEXT, trade_date TEXT, turnover_rate REAL,
                pe_ttm REAL, pb REAL, total_mv REAL, volume_ratio REAL, circ_mv REAL, ps_ttm REAL, dv_ttm REAL);
            CREATE TABLE IF NOT EXISTS security_master(code TEXT, name TEXT, list_date TEXT);
        """)
        for index, code in enumerate(self.CODES):
            close = np.maximum(10 + index * 5 + np.cumsum(rng.normal(0, 0.3, self.MAX_DAYS)), 1.0)
            pct = np.concatenate([[0.0], (close[1:] / close[:-1] - 1) * 100])
            volume = 1e6 + rng.random(self.MAX_DAYS) * 1e5
            amount = volume * close
            high = close * 1.01
            low = close * 0.99
            con.executemany("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            [(code, self.dates[i], float(close[i]), float(high[i]), float(low[i]),
                              float(close[i]), float(close[i - 1] if i else close[i]), 0.0,
                              float(pct[i]), float(volume[i]), float(amount[i])) for i in range(days)])
            con.executemany("INSERT INTO daily_basic VALUES(?,?,?,?,?,?,?,?,?,?)",
                            [(code, self.dates[i], 1.5, 15.0, 2.0, 1e10, 1.1, 6e9, 3.0, 0.02)
                             for i in range(days)])
            con.execute("INSERT INTO security_master VALUES(?,?,?)", (code, f"测试股{index}", "20200101"))
        con.commit()
        con.close()
        return market, factors

    def _factors(self, name: str, days: int) -> pd.DataFrame:
        market, factors = self._build(name, days)
        stock_ml.build_factors(market, factors, None,
                               {"minHistory": 20, "minListDays": 0, "minClose": 0, "minAmount20": 0})
        con = sqlite3.connect(factors)
        con.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in con.execute("SELECT * FROM stock_ml_factors")]
        finally:
            con.close()
        return pd.DataFrame(rows)

    def test_stock_ml_features_do_not_use_future_rows(self):
        full = self._factors("full", self.DAYS)
        part = self._factors("part", self.CUTOFF_INDEX)
        self.assertGreater(len(full), len(part))

        cols = [c for c in stock_ml.FACTOR_COLUMNS if c in full.columns]
        self.assertGreaterEqual(len(cols), 20)
        left = full[full["trade_date"] <= self.cutoff].set_index(["code", "trade_date"])[cols].sort_index()
        right = part[part["trade_date"] <= self.cutoff].set_index(["code", "trade_date"])[cols].sort_index()
        self.assertTrue(left.index.equals(right.index), "截断前后可比样本的行集合应完全一致")
        pd.testing.assert_frame_equal(left, right, check_exact=True)


if __name__ == "__main__":
    unittest.main()
