# -*- coding: utf-8 -*-
"""模拟盘配置档案（③）：股票 / ETF 模拟盘都必须留下完整生效配置。

背景：此前 `stock_sim_runs` 只有 19 个扁平标量列、`etf_sim_runs` 只有 6 个，
`featuresOverride` / `publishGate` / `technicalTiming` 等一律没存，
"这个模拟盘当初按什么配置跑的"无法重建。这些用例锁住修复，防止再退回去。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "quant-engine" / "src"))

from quant_engine.etf_quant import sim as etf_sim  # noqa: E402
from quant_engine.stock import sim as stock_sim  # noqa: E402
from quant_engine.stock_ml.config_integrity import CONFIG_SCHEMA_VERSION, load_config_json  # noqa: E402

FINGERPRINT_COLUMNS = ("config_json", "config_sha256", "config_yaml_sha256",
                       "git_commit", "git_dirty")


def _stock_env() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    f = sqlite3.connect(root / "factors.db")
    f.executescript("""
        CREATE TABLE selection_candidates (
          trade_date TEXT, model_id TEXT, rank INTEGER, code TEXT,
          score REAL, reasons_json TEXT, computed_at TEXT,
          source_run_id TEXT, result_version INTEGER DEFAULT 2
        );
        -- model_id='ml_walkforward' 的信号查询会 JOIN 这张表并只认 is_published=1
        CREATE TABLE stock_walkforward_runs (
          run_id TEXT PRIMARY KEY, generated_at TEXT, label TEXT, start_date TEXT,
          end_date TEXT, windows INTEGER, config TEXT, metrics TEXT, holdings TEXT,
          status TEXT, is_published INTEGER DEFAULT 0, result_version INTEGER DEFAULT 2
        );
    """)
    f.execute("INSERT INTO stock_walkforward_runs (run_id,generated_at,holdings,status,is_published,result_version)"
              " VALUES (?,?,?,?,?,?)",
              ("stock-wf-test-1", "2026-09-01T00:00:00Z", "{}", "complete", 1, 2))
    for rank, code in [(1, "AAA.SH"), (2, "BBB.SZ")]:
        f.execute("INSERT INTO selection_candidates"
                  " (trade_date,model_id,rank,code,score,reasons_json,computed_at,source_run_id,result_version)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  ("20260901", "ml_walkforward", rank, code, 90 - rank, "[]", "t",
                   "stock-wf-test-1", 2))
    f.commit()
    f.close()
    return root, tmp


def _etf_env() -> tuple[Path, tempfile.TemporaryDirectory]:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
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
         json.dumps({"tradeDate": "20260901", "bearRegime": False,
                     "holdings": ["510300.SH", "510500.SH"]}),
         "complete", None))
    e.commit()
    e.close()
    m = sqlite3.connect(root / "market.db")
    m.executescript("""
        CREATE TABLE etf_daily_bars (
          ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
          pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT
        );
    """)
    m.commit()
    m.close()
    return root, tmp


class LoadConfigJsonTest(unittest.TestCase):
    def test_parses_valid_json(self):
        self.assertEqual(load_config_json('{"a": 1}'), {"a": 1})

    def test_empty_and_none_give_empty_dict(self):
        for raw in (None, "", sqlite3.Binary(b"")):
            self.assertEqual(load_config_json(raw), {})

    def test_corrupt_json_does_not_raise(self):
        """一条脏记录不该让整个列表接口 500。"""
        self.assertEqual(load_config_json("{not json"), {})

    def test_non_object_json_gives_empty_dict(self):
        self.assertEqual(load_config_json("[1,2,3]"), {})

    def test_passthrough_for_dict(self):
        self.assertEqual(load_config_json({"a": 1}), {"a": 1})


class MigrationTest(unittest.TestCase):
    def test_stock_sim_migration_adds_config_columns(self):
        with tempfile.TemporaryDirectory() as td:
            con = stock_sim.connect(Path(td) / "factors.db")
            try:
                # 模拟旧库：先建不含新列的表
                con.execute(
                    "CREATE TABLE stock_sim_runs (run_id TEXT PRIMARY KEY, model_id TEXT NOT NULL,"
                    " top_n INTEGER NOT NULL, initial_capital REAL NOT NULL,"
                    " commission_rate REAL NOT NULL DEFAULT 0.00008,"
                    " stamp_duty_rate REAL NOT NULL DEFAULT 0.0005,"
                    " slippage_rate REAL NOT NULL DEFAULT 0.004,"
                    " execution_mode TEXT NOT NULL DEFAULT 'same_day_close',"
                    " status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,"
                    " last_signal_date TEXT, last_nav REAL)")
                con.commit()
                stock_sim.ensure_schema(con)
                cols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_runs)")}
                for col in FINGERPRINT_COLUMNS:
                    self.assertIn(col, cols, f"迁移必须补上 {col}")
                stock_sim.ensure_schema(con)  # 幂等
            finally:
                con.close()

    def test_etf_sim_migration_adds_config_columns(self):
        with tempfile.TemporaryDirectory() as td:
            con = etf_sim.connect(Path(td) / "etf-quant.db")
            try:
                con.execute(
                    "CREATE TABLE etf_sim_runs (run_id TEXT PRIMARY KEY,"
                    " initial_capital REAL NOT NULL, start_date TEXT NOT NULL,"
                    " commission_rate REAL NOT NULL DEFAULT 0.0002,"
                    " status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,"
                    " last_signal_date TEXT, last_nav REAL)")
                con.commit()
                etf_sim.ensure_schema(con)
                cols = {r[1] for r in con.execute("PRAGMA table_info(etf_sim_runs)")}
                for col in FINGERPRINT_COLUMNS:
                    self.assertIn(col, cols, f"迁移必须补上 {col}")
                etf_sim.ensure_schema(con)
            finally:
                con.close()


class StockSimConfigCaptureTest(unittest.TestCase):
    def test_start_sim_records_full_effective_config(self):
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2,
                            initial_capital=1_000_000, regime_filter=True,
                            rebalance_days=3, holding_buffer_rank=3)
        con = stock_sim.connect(root / "factors.db")
        try:
            row = con.execute("SELECT * FROM stock_sim_runs").fetchone()
        finally:
            con.close()
        cfg = load_config_json(row["config_json"])
        # 这些正是旧扁平标量列存不下、导致无法重建的部分
        self.assertEqual(cfg["kind"], "stock_sim")
        self.assertEqual(cfg["modelId"], "ml_walkforward")
        self.assertEqual(cfg["rebalanceDays"], 3)
        self.assertEqual(cfg["holdingBufferRank"], 3)
        self.assertTrue(cfg["regimeFilter"])
        self.assertIn("initialStopLoss", cfg)
        self.assertIn("trailingDrawdown", cfg)
        self.assertIn("slippageRate", cfg)
        self.assertEqual(cfg["configSchemaVersion"], CONFIG_SCHEMA_VERSION)

    def test_start_sim_records_fingerprint_and_git(self):
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2)
        con = stock_sim.connect(root / "factors.db")
        try:
            row = con.execute("SELECT * FROM stock_sim_runs").fetchone()
            fpr = con.execute("SELECT * FROM config_fingerprint").fetchone()
        finally:
            con.close()
        self.assertEqual(len(row["config_sha256"]), 64)
        # 同源档案表里必须能查到同一个哈希（"配置 → 运行"的查询入口）
        self.assertIsNotNone(fpr, "指纹必须同时写入 config_fingerprint 表")
        self.assertEqual(fpr["config_sha256"], row["config_sha256"])

    def test_source_config_is_frozen_when_provided(self):
        """ML 模拟盘的参数派生自 stock-ml.yaml：必须连那份 YAML 一起冻结。"""
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        source = {"backtest": {"topN": 5, "technicalTiming": {"goldenCross": True}}}
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2,
                            source_config=source)
        con = stock_sim.connect(root / "factors.db")
        try:
            cfg = load_config_json(
                con.execute("SELECT config_json FROM stock_sim_runs").fetchone()["config_json"])
        finally:
            con.close()
        self.assertEqual(cfg["sourceConfig"]["backtest"]["topN"], 5)
        # 嵌套子键必须原样保留——浅合并丢键正是历史上踩过的坑
        self.assertTrue(cfg["sourceConfig"]["backtest"]["technicalTiming"]["goldenCross"])

    def test_snapshot_written_to_given_root(self):
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        snap = root / "artifacts"
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2,
                            snapshot_root=snap)
        dirs = list((snap / "config-snapshots").iterdir())
        self.assertEqual(len(dirs), 1, "应生成一个内容寻址快照目录")
        self.assertTrue((dirs[0] / "config.json").exists())

    def test_no_snapshot_root_writes_db_only(self):
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2)
        self.assertFalse((root / "config-snapshots").exists(),
                         "未给 snapshot_root 时不应凭空造目录")

    def test_list_runs_exposes_config(self):
        root, tmp = _stock_env()
        self.addCleanup(tmp.cleanup)
        stock_sim.start_sim(root / "factors.db", model_id="ml_walkforward", top_n=2)
        listed = stock_sim.list_runs(root / "factors.db")["items"]
        self.assertEqual(len(listed), 1)
        item = listed[0]
        self.assertEqual(item["config"]["modelId"], "ml_walkforward")
        self.assertEqual(len(item["configSha256"]), 64)
        self.assertIn("gitCommit", item)


class EtfSimConfigCaptureTest(unittest.TestCase):
    def test_start_sim_records_config_and_signal_source(self):
        root, tmp = _etf_env()
        self.addCleanup(tmp.cleanup)
        etf_sim.start_sim(root / "etf-quant.db", root / "market.db", initial_capital=500_000)
        con = etf_sim.connect(root / "etf-quant.db")
        try:
            row = con.execute("SELECT * FROM etf_sim_runs").fetchone()
        finally:
            con.close()
        cfg = load_config_json(row["config_json"])
        self.assertEqual(cfg["kind"], "etf_sim")
        self.assertEqual(cfg["initialCapital"], 500_000)
        # 同源：必须记下这个盘跟的是哪次 walkforward
        self.assertEqual(cfg["signalRunId"], "etf-wf-test-1")
        self.assertEqual(cfg["configSchemaVersion"], CONFIG_SCHEMA_VERSION)
        self.assertEqual(len(row["config_sha256"]), 64)

    def test_etf_sim_status_exposes_parsed_config(self):
        root, tmp = _etf_env()
        self.addCleanup(tmp.cleanup)
        etf_sim.start_sim(root / "etf-quant.db", root / "market.db")
        status = etf_sim.sim_status(root / "etf-quant.db")
        run = status["run"]
        self.assertEqual(run["config"]["kind"], "etf_sim")
        self.assertEqual(len(run["configSha256"]), 64)
        self.assertIn("gitCommit", run)

    def test_snapshot_written_when_root_given(self):
        root, tmp = _etf_env()
        self.addCleanup(tmp.cleanup)
        snap = root / "artifacts"
        etf_sim.start_sim(root / "etf-quant.db", root / "market.db", snapshot_root=snap)
        self.assertTrue((snap / "config-snapshots").exists())


class CrossFamilySnapshotTest(unittest.TestCase):
    """不同运行族应写进同一个内容寻址档案库，而不是各自散落一份。"""

    def test_stock_and_etf_share_snapshot_root(self):
        sroot, stmp = _stock_env()
        eroot, etmp = _etf_env()
        self.addCleanup(stmp.cleanup)
        self.addCleanup(etmp.cleanup)
        common = sroot / "artifacts"
        stock_sim.start_sim(sroot / "factors.db", model_id="ml_walkforward", top_n=2,
                            snapshot_root=common)
        etf_sim.start_sim(eroot / "etf-quant.db", eroot / "market.db",
                          snapshot_root=common)
        dirs = sorted(p.name for p in (common / "config-snapshots").iterdir())
        self.assertEqual(len(dirs), 2, "两族配置不同，应各有一个哈希目录")
        for name in dirs:
            self.assertEqual(len(name), 64)


if __name__ == "__main__":
    unittest.main()
