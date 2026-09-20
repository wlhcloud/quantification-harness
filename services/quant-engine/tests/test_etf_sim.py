"""Unit tests for the ETF 模拟盘台账（纯增量；不触碰既有测试）。"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine.etf_quant import sim


def _make_env() -> tuple[Path, Path]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)

    # etf-quant.db：先建 walkforward 表并插入一个非空持仓信号
    e = sqlite3.connect(root / "etf-quant.db")
    e.executescript("""
        CREATE TABLE etf_walkforward_runs (
          run_id TEXT PRIMARY KEY, generated_at TEXT, label TEXT, start_date TEXT, end_date TEXT,
          windows INTEGER, config TEXT, metrics TEXT, holdings TEXT, status TEXT, error TEXT
        );
    """)
    e.execute(
        "INSERT INTO etf_walkforward_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("etf-wf-test-1", "2026-09-01T00:00:00Z", "forward_20", "20220607", "20260901", 25,
         "{}", "{}",
         json.dumps({"tradeDate": "20260901", "bearRegime": False, "holdings": ["510300.SH", "510500.SH"]}),
         "complete", None),
    )
    e.commit()
    e.close()

    # market.db：两只 ETF 的信号日次日 bar（T+1 开盘价）与最新 bar
    m = sqlite3.connect(root / "market.db")
    m.executescript("""
        CREATE TABLE etf_daily_bars (
          ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
          pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT
        );
    """)
    rows = [
        ("510300.SH", "20260901", 4.0, 4.0, 4.0, 4.0, 4.0, 0, 0, 0, 0, "test"),
        ("510300.SH", "20260902", 4.05, 4.10, 4.00, 4.08, 4.0, 0.08, 2.0, 0, 0, "test"),
        ("510500.SH", "20260901", 6.0, 6.0, 6.0, 6.0, 6.0, 0, 0, 0, 0, "test"),
        ("510500.SH", "20260902", 6.10, 6.20, 6.05, 6.15, 6.0, 0.15, 2.5, 0, 0, "test"),
    ]
    m.executemany("INSERT INTO etf_daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    m.commit()
    m.close()
    return root, tmp


class SimSchemaTest(unittest.TestCase):
    def test_schema_created_idempotently(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        con = sim.connect(root / "etf-quant.db")
        sim.ensure_schema(con)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("etf_sim_runs", tables)
        self.assertIn("etf_sim_daily", tables)
        self.assertIn("etf_sim_positions", tables)
        # 幂等：再建一次不报错
        sim.ensure_schema(con)
        con.close()

    def test_latest_signal_reads_holdings(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        sig = sim.latest_signal(root / "etf-quant.db")
        self.assertFalse(sig["bearRegime"])
        self.assertEqual(sig["holdings"], ["510300.SH", "510500.SH"])
        self.assertEqual(sig["signalDate"], "20260901")


class SimFlowTest(unittest.TestCase):
    def test_start_then_advance_buys_at_t1_open(self):
        root, tmp = _make_env()
        self.addCleanup(tmp.cleanup)
        started = sim.start_sim(root / "etf-quant.db", root / "market.db", initial_capital=1_000_000)
        self.assertEqual(started["nav"], 1.0)
        self.assertFalse(started["bearRegime"])

        result = sim.advance_sim(root / "etf-quant.db", root / "market.db", started["runId"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["positions"]), 2)
        # T+1 开盘价成交：0902 open 4.05 / 6.10
        buy = {b["tsCode"]: b for b in result["trades"]["bought"]}
        self.assertEqual(buy["510300.SH"]["price"], 4.05)
        self.assertEqual(buy["510500.SH"]["price"], 6.10)
        self.assertTrue(buy["510300.SH"]["t1Price"])
        # 等权：每只 50 万预算，100 份整手
        self.assertEqual(buy["510300.SH"]["shares"] % 100, 0)
        # 佣金万 2
        fee = buy["510300.SH"]["fee"]
        self.assertAlmostEqual(fee, buy["510300.SH"]["shares"] * 4.05 * 0.0002, places=6)
        # 净值略高于 1.0（0902 收盘 4.08/6.15 相对成本上涨）
        self.assertGreater(result["nav"], 1.0)
        self.assertLess(result["nav"], 1.05)

        status = sim.sim_status(root / "etf-quant.db", started["runId"])
        self.assertEqual(len(status["positions"]), 2)
        history = sim.sim_history(root / "etf-quant.db", started["runId"])
        self.assertEqual(len(history["items"]), 2)  # 起点 + 调仓日


if __name__ == "__main__":
    unittest.main()
