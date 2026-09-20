"""ETF 模拟盘账务回归测试：跨期资金/持仓结转、T+1 开盘卖出、无法定价仓位保留。

背景（修复前行为）：advance_sim 每期都把现金重置为 initial_capital 并按 initial_capital*weight
重新买入全部目标，导致前期盈亏不结转、NAV 永远回到 1.0；卖出用最新 close 而非 T+1 开盘价；
无法定价的持仓在整表重写时被静默丢弃。本文件锁定修复后的口径。
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine.etf_quant import sim

WF_SCHEMA = """
CREATE TABLE etf_walkforward_runs (
  run_id TEXT PRIMARY KEY, generated_at TEXT, label TEXT, start_date TEXT, end_date TEXT,
  windows INTEGER, config TEXT, metrics TEXT, holdings TEXT, status TEXT, error TEXT
);
"""

BARS_SCHEMA = """
CREATE TABLE etf_daily_bars (
  ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
  pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT
);
"""

# A=510300.SH：0902 T+1 开盘 4.05、收盘 4.08；0903 开盘 6.00、收盘 6.10
# B=510500.SH：0902 T+1 开盘 6.10、收盘 6.15；0903 开盘 9.00、收盘 9.20
BARS = [
    ("510300.SH", "20260901", 4.00, 4.00, 4.00, 4.00, 4.00, 0, 0, 0, 0, "test"),
    ("510300.SH", "20260902", 4.05, 4.10, 4.00, 4.08, 4.00, 0.08, 2.0, 0, 0, "test"),
    ("510300.SH", "20260903", 6.00, 6.20, 5.95, 6.10, 4.08, 2.02, 49.5, 0, 0, "test"),
    ("510500.SH", "20260901", 6.00, 6.00, 6.00, 6.00, 6.00, 0, 0, 0, 0, "test"),
    ("510500.SH", "20260902", 6.10, 6.20, 6.05, 6.15, 6.00, 0.15, 2.5, 0, 0, "test"),
    ("510500.SH", "20260903", 9.00, 9.30, 8.95, 9.20, 6.15, 3.05, 49.6, 0, 0, "test"),
]


def _make_env() -> tuple[Path, object]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

    e = sqlite3.connect(root / "etf-quant.db")
    e.executescript(WF_SCHEMA)
    _add_signal(e, "wf-1", "2026-09-01T00:00:00Z", "20260901", ["510300.SH", "510500.SH"])
    e.commit()
    e.close()

    m = sqlite3.connect(root / "market.db")
    m.executescript(BARS_SCHEMA)
    m.executemany("INSERT INTO etf_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", BARS)
    m.commit()
    m.close()
    return root, tmp


def _add_signal(con: sqlite3.Connection, run_id: str, generated_at: str, trade_date: str,
                holdings: list[str], bear: bool = False) -> None:
    con.execute(
        "INSERT OR REPLACE INTO etf_walkforward_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, generated_at, "forward_20", "20220607", trade_date, 25, "{}", "{}",
         json.dumps({"tradeDate": trade_date, "bearRegime": bear, "holdings": holdings}),
         "complete", None),
    )


class NavCompoundingTest(unittest.TestCase):
    """NAV 必须跨期复利累积：第 2 期权益 = 第 1 期权益按新价重估，而不是回到初始资金。"""

    def test_nav_compounds_across_periods(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        etf_db, market = root / "etf-quant.db", root / "market.db"

        started = sim.start_sim(etf_db, market, initial_capital=1_000_000)
        p1 = sim.advance_sim(etf_db, market, started["runId"])
        self.assertTrue(p1["ok"])
        self.assertEqual(p1["valuationDate"], "20260902")

        # 第 2 期：同样的持仓，价格在 0903 接近翻倍（T+1 开盘 6.00 / 9.00）
        con = sqlite3.connect(etf_db)
        _add_signal(con, "wf-2", "2026-09-02T00:00:00Z", "20260902", ["510300.SH", "510500.SH"])
        con.commit()
        con.close()

        p2 = sim.advance_sim(etf_db, market, started["runId"])
        self.assertTrue(p2["ok"])
        self.assertEqual(p2["valuationDate"], "20260903")

        # 修复前：现金被重置为 100 万并按 100 万*权重重买 → nav≈1.0；
        # 修复后：第 1 期权益整体按新价重估 → nav≈1.48
        self.assertGreater(p2["nav"], 1.30, f"NAV 未结转前期盈亏：{p2['nav']}")
        self.assertLess(p2["nav"], 1.60)

        # 账务恒等式：nav*initial == cash + 持仓市值（nav 被 round 到 6 位小数，容差 1 元）
        status = sim.sim_status(etf_db, started["runId"])
        equity = p2["cash"] + p2["marketValue"]
        self.assertAlmostEqual(p2["nav"] * 1_000_000, equity, delta=1.0)
        self.assertEqual(len(status["positions"]), 2)
        # 持仓数量级对得上：约 740k/6.00 与 740k/9.00
        shares = {p["ts_code"]: p["shares"] for p in status["positions"]}
        self.assertGreater(shares["510300.SH"], 100_000)
        self.assertLess(shares["510300.SH"], 130_000)
        self.assertGreater(shares["510500.SH"], 70_000)
        self.assertLess(shares["510500.SH"], 90_000)

    def test_bear_regime_liquidates_and_keeps_cash_after_later_period(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        etf_db, market = root / "etf-quant.db", root / "market.db"
        started = sim.start_sim(etf_db, market, initial_capital=1_000_000)
        sim.advance_sim(etf_db, market, started["runId"])  # 建仓 @0902
        con = sqlite3.connect(etf_db)
        _add_signal(con, "wf-bear", "2026-09-02T00:00:00Z", "20260902", [], bear=True)
        con.commit()
        con.close()

        p2 = sim.advance_sim(etf_db, market, started["runId"])
        self.assertTrue(p2["ok"])
        self.assertTrue(p2["bearRegime"])
        self.assertEqual(p2["marketValue"], 0.0)
        # 按 0903 开盘清仓，现金应等于上一期权益（减手续费），NAV 不回退到 1.0
        self.assertGreater(p2["nav"], 1.30)
        self.assertAlmostEqual(p2["cash"], p2["nav"] * 1_000_000, delta=1.0)


class ExecutionPriceTest(unittest.TestCase):
    """卖出必须用 T+1 开盘价；无法定价的仓位保留而不是被丢弃。"""

    def test_sell_uses_t1_open_not_latest_close(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        etf_db, market = root / "etf-quant.db", root / "market.db"
        started = sim.start_sim(etf_db, market, initial_capital=1_000_000)
        sim.advance_sim(etf_db, market, started["runId"])
        # 第 2 期只留 A，B 应被卖出：0903 开盘 9.00（而非 0902 收盘 6.15）
        con = sqlite3.connect(etf_db)
        _add_signal(con, "wf-3", "2026-09-02T00:00:00Z", "20260902", ["510300.SH"])
        con.commit()
        con.close()

        p2 = sim.advance_sim(etf_db, market, started["runId"])
        sold = {s["tsCode"]: s for s in p2["trades"]["sold"]}
        self.assertIn("510500.SH", sold)
        self.assertEqual(sold["510500.SH"]["price"], 9.00)
        self.assertEqual(sold["510500.SH"]["tradeDate"], "20260903")
        self.assertEqual([p["tsCode"] for p in p2["positions"]], ["510300.SH"])

    def test_unpriced_position_is_retained(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        etf_db, market = root / "etf-quant.db", root / "market.db"
        started = sim.start_sim(etf_db, market, initial_capital=1_000_000)
        # 直接注入一个行情库中完全不存在的持仓（模拟停牌/未同步）
        con = sqlite3.connect(etf_db)
        sim.ensure_schema(con)
        con.execute(
            "INSERT INTO etf_sim_positions (run_id, ts_code, shares, avg_cost, last_price, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (started["runId"], "999999.SH", 1000, 10.0, 10.0, "2026-09-01T00:00:00Z"),
        )
        con.execute(
            "UPDATE etf_sim_runs SET last_signal_date=NULL, last_nav=NULL WHERE run_id=?", (started["runId"],))
        con.commit()
        con.close()

        p1 = sim.advance_sim(etf_db, market, started["runId"])
        codes = [p["tsCode"] for p in p1["positions"]]
        self.assertIn("999999.SH", codes, "无法定价的持仓被静默丢弃")
        # 该仓位按上次估值计入市值，没有被当成 0
        self.assertGreater(p1["marketValue"], 1000 * 10.0)


if __name__ == "__main__":
    unittest.main()
