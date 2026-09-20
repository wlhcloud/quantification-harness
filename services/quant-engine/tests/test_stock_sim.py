"""股票 14:40 信号、当日尾盘成交模拟盘测试。"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine.stock import sim


def _make_env() -> tuple[Path, Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

    f = sqlite3.connect(root / "factors.db")
    # 只需候选表：stock/sim.py 从 selection_candidates 取信号。
    # （原 fixture 还建了一张 factor_snapshots，那是 legacy 因子选股的产物，已随其退役删除，
    #   模拟盘从不读它。）
    f.executescript("""
        CREATE TABLE selection_candidates (
          trade_date TEXT, model_id TEXT, rank INTEGER, code TEXT,
          score REAL, reasons_json TEXT, computed_at TEXT
        );
    """)
    # 信号日 20260901：目标 A、B
    rows = [(1, "AAA.SH"), (2, "BBB.SZ")]
    for rank, code in rows:
        f.execute("INSERT INTO selection_candidates VALUES (?,?,?,?,?,?,?)",
                  ("20260901", "balanced", rank, code, 90 - rank, "[]", "t"))
    # 下一信号日 20260903：目标 B、C（A 退出、C 新进）
    for rank, code in [(1, "BBB.SZ"), (2, "CCC.SH")]:
        f.execute("INSERT INTO selection_candidates VALUES (?,?,?,?,?,?,?)",
                  ("20260903", "balanced", rank, code, 90 - rank, "[]", "t"))
    f.execute("INSERT INTO selection_candidates VALUES (?,?,?,?,?,?,?)",
              ("20260903", "balanced", 3, "AAA.SH", 80, "[]", "t"))
    f.commit()
    f.close()

    m = sqlite3.connect(root / "market.db")
    m.executescript("""
        CREATE TABLE daily_bars (
          code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
          pre_close REAL, change REAL, pct_chg REAL, volume REAL, amount REAL
        );
        CREATE TABLE index_daily_bars (
          ts_code TEXT, trade_date TEXT, open REAL, close REAL
        );
    """)
    data = [
        # 信号日 20260901
        ("AAA.SH", "20260901", 10.0, 10, 10, 10.0, 10, 0, 0, 0, 0),
        ("BBB.SZ", "20260901", 20.0, 20, 20, 20.0, 20, 0, 0, 0, 0),
        # T+1 = 20260902
        ("AAA.SH", "20260902", 10.5, 11, 10, 11.0, 10, 0, 0, 0, 0),
        ("BBB.SZ", "20260902", 20.5, 21, 20, 21.0, 20, 0, 0, 0, 0),
        # 下一信号日 20260903 + T+1 = 20260904
        ("AAA.SH", "20260903", 11.0, 11, 11, 11.0, 11, 0, 0, 0, 0),
        ("BBB.SZ", "20260903", 21.0, 21, 21, 21.0, 21, 0, 0, 0, 0),
        ("CCC.SH", "20260903", 5.0, 5, 5, 5.0, 5, 0, 0, 0, 0),
        ("AAA.SH", "20260904", 11.2, 11.5, 11, 11.5, 11, 0, 0, 0, 0),
        ("BBB.SZ", "20260904", 21.2, 22, 21, 22.0, 21, 0, 0, 0, 0),
        ("CCC.SH", "20260904", 5.1, 5.3, 5, 5.3, 5, 0, 0, 0, 0),
    ]
    m.executemany("INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)", data)
    m.commit()
    m.close()
    return root / "factors.db", root / "market.db", tmp


class StockSimTest(unittest.TestCase):
    def test_start_rejects_model_without_signals(self):
        """没有候选信号的模型必须直接拒绝建盘。

        背景：前端曾提供后端并不生产的模型（ml_technical_timing*），而 start_sim 不校验、
        无信号也照常建 run，结果只写一行 INIT（净值恒 1.0、持仓 0），用户看到"创建成功"却永远不动账。
        """
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(ValueError) as ctx:
            sim.start_sim(factors, model_id="no_such_model", top_n=2, initial_capital=1_000_000)
        self.assertIn("no_such_model", str(ctx.exception))
        # 不能留下半成品 run
        con = sqlite3.connect(factors)
        try:
            count = con.execute("SELECT COUNT(*) FROM stock_sim_runs").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(count, 0)

    def test_waiting_when_signal_day_bar_missing(self):
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        # 删除信号日行情，无法取得尾盘成交代理价时必须等待。
        con = sqlite3.connect(market)
        con.execute("DELETE FROM daily_bars WHERE trade_date='20260901'")
        con.commit(); con.close()
        started = sim.start_sim(factors, top_n=2, initial_capital=1_000_000)
        self.assertEqual(started["targetCount"], 2)
        r = sim.advance_sim(factors, market, started["runId"])
        self.assertFalse(r["ok"])
        self.assertTrue(r["waiting"])

    def test_regime_filter_bear_clears_to_cash(self):
        """regime_filter=1 且信号日沪深300收在MA20下方：当日尾盘清仓。"""
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        # 构造熊市：index_daily_bars 中 000300.SH 信号日前 25 个交易日单调下跌
        con = sqlite3.connect(market)
        from datetime import date, timedelta
        d = date(2026, 8, 5)
        idx_days = []
        while len(idx_days) < 25:
            s = d.strftime("%Y%m%d")
            if s <= "20260901":
                idx_days.append(s)
            d += timedelta(days=1)
        ins = (f"INSERT OR REPLACE INTO index_daily_bars (ts_code, trade_date, open, close) VALUES "
               f"('000300.SH', ?, ?, ?)")
        base = 5000.0
        for i, day in enumerate(idx_days):
            close = base - i * 20.0  # 单调下跌，最后一日必低于 MA20
            con.execute(ins, (day, close, close))
        con.commit(); con.close()
        started = sim.start_sim(factors, top_n=2, initial_capital=1_000_000, regime_filter=True)
        r = sim.advance_sim(factors, market, started["runId"])
        self.assertTrue(r["ok"])
        self.assertTrue(r["bearRegime"])
        self.assertEqual(r["positions"], [])
        self.assertGreater(r["cash"], 0)
        self.assertEqual(r["nav"], r["cash"] / 1_000_000)

    def test_buy_then_rotate_with_costs(self):
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        started = sim.start_sim(factors, top_n=2, initial_capital=1_000_000)
        rid = started["runId"]

        # 第一次 advance：信号 20260901，按当日收盘代理尾盘买入 A、B
        r1 = sim.advance_sim(factors, market, rid)
        self.assertTrue(r1["ok"], r1)
        self.assertEqual(r1["tradeDate"], "20260901")
        self.assertEqual(r1["executionMode"], "same_day_close")
        self.assertEqual(len(r1["positions"]), 2)
        bought = {b["code"]: b for b in r1["trades"]["bought"]}
        self.assertEqual(bought["AAA.SH"]["price"], 10.04)  # 收盘价 + 40bp尾盘滑点
        # 整手 100 股
        self.assertEqual(bought["AAA.SH"]["shares"] % 100, 0)
        # 佣金万0.8
        self.assertAlmostEqual(bought["AAA.SH"]["fee"],
                               bought["AAA.SH"]["shares"] * 10.04 * 0.00008, places=2)

        # 下一次 advance：顺序推进到信号 20260903（目标 B、C），当日尾盘轮换
        # A 被卖出（佣金+印花税），C 买入，B 保留
        r2 = sim.advance_sim(factors, market, rid)
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["tradeDate"], "20260903")
        codes = {p["code"] for p in r2["positions"]}
        self.assertEqual(codes, {"BBB.SZ", "CCC.SH"})
        sold = {s["code"]: s for s in r2["trades"]["sold"]}
        self.assertIn("AAA.SH", sold)
        # 卖出费 = 成交额 * (佣金万0.8 + 印花万5)，round 到分
        self.assertAlmostEqual(sold["AAA.SH"]["fee"],
                               sold["AAA.SH"]["shares"] * (11.0 * 0.996) * 0.00058, places=2)

        # 无更新信号 -> waiting
        r3 = sim.advance_sim(factors, market, rid)
        self.assertFalse(r3["ok"])
        self.assertTrue(r3["waiting"])

        st = sim.sim_status(factors, rid)
        self.assertEqual(len(st["positions"]), 2)
        hist = sim.sim_history(factors, rid)
        self.assertEqual(len(hist["items"]), 3)  # 起点 + 两次调仓

    def test_split_adjusts_sim_shares_and_stop_basis(self):
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        mkt = sqlite3.connect(market)
        mkt.execute("CREATE TABLE adjustment_factors(code TEXT,trade_date TEXT,adj_factor REAL)")
        mkt.executemany("INSERT INTO adjustment_factors VALUES(?,?,?)", [
            ("AAA.SH", "20260901", 1.0), ("AAA.SH", "20260903", 2.0),
            ("BBB.SZ", "20260901", 1.0), ("BBB.SZ", "20260903", 1.0),
            ("CCC.SH", "20260903", 1.0),
        ])
        # 1拆2后原始价格减半；经济价值与止损基准都不应腰斩。
        mkt.execute("UPDATE daily_bars SET open=5.5,high=5.5,low=5.5,close=5.5 "
                    "WHERE code='AAA.SH' AND trade_date='20260903'")
        mkt.commit(); mkt.close()

        started = sim.start_sim(factors, top_n=2, initial_capital=1_000_000)
        first = sim.advance_sim(factors, market, started["runId"])
        initial_shares = next(p["shares"] for p in first["positions"] if p["code"] == "AAA.SH")
        second = sim.advance_sim(factors, market, started["runId"])
        sold = next(t for t in second["trades"]["sold"] if t["code"] == "AAA.SH")
        self.assertAlmostEqual(sold["shares"], initial_shares * 2)
        self.assertFalse(any(t["code"] == "AAA.SH" for t in second["trades"]["tailStopped"]))

    def test_three_day_rebalance_and_holding_buffer(self):
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        started = sim.start_sim(factors, top_n=2, rebalance_days=3, holding_buffer_rank=3)
        first = sim.advance_sim(factors, market, started["runId"])
        second = sim.advance_sim(factors, market, started["runId"])
        self.assertEqual({p["code"] for p in first["positions"]}, {"AAA.SH", "BBB.SZ"})
        # 仅过去2个交易日，不能提前按新Top2换仓；且AAA仍在缓冲区前3。
        self.assertEqual({p["code"] for p in second["positions"]}, {"AAA.SH", "BBB.SZ"})
        self.assertEqual(second["trades"]["sold"], [])

    def test_configurable_five_percent_hard_stop(self):
        factors, market, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        mkt = sqlite3.connect(market)
        mkt.execute("UPDATE daily_bars SET open=9.4,high=9.4,low=9.4,close=9.4 "
                    "WHERE code='AAA.SH' AND trade_date='20260903'")
        mkt.commit(); mkt.close()
        started = sim.start_sim(factors, top_n=2, rebalance_days=3,
                                initial_stop_loss=0.05, trailing_drawdown=0.08)
        sim.advance_sim(factors, market, started["runId"])
        second = sim.advance_sim(factors, market, started["runId"])
        self.assertIn("AAA.SH", {x["code"] for x in second["trades"]["tailStopped"]})


if __name__ == "__main__":
    unittest.main()
