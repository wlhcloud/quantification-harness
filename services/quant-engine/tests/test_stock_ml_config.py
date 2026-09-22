"""stock-ml 配置体检测试（修复：配置键静默失效）。

修复前：config/stock-ml.yaml 的 backtest.transactionCostBps/slippageBps 与整个 universe 段
从未被任何代码读取（代码读 commissionRate/stampDutyRate/slippageRate，且不读 universe），
配置看起来生效、实际无效。现在：① yaml 改为与实际生效的费率一致（历史回测数值不变）；
② 出现代码不读取的键时由 _config_warnings 显式告警并写进 job 结果。
"""
from __future__ import annotations

import unittest
import sqlite3
import tempfile
from pathlib import Path

from quant_engine import stock_ml

CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "stock-ml.yaml"


class ConfigWarningTest(unittest.TestCase):
    def test_canonical_cost_keys_do_not_warn(self):
        cfg = {"backtest": {"topN": 5, "rebalanceDays": 3, "commissionRate": 0.0003,
                            "stampDutyRate": 0.0005, "slippageRate": 0.001}}
        self.assertEqual(stock_ml._config_warnings(cfg), [])

    def test_unread_cost_keys_warn(self):
        warnings = stock_ml._config_warnings({"backtest": {"transactionCostBps": 15, "slippageBps": 5}})
        self.assertEqual(len(warnings), 1)
        self.assertIn("transactionCostBps", warnings[0])
        self.assertIn("slippageBps", warnings[0])

    def test_universe_section_warns(self):
        warnings = stock_ml._config_warnings({"universe": {"minMarketCap": 200000, "excludeST": True}})
        self.assertTrue(any("universe" in w for w in warnings))

    def test_unknown_technical_timing_key_warns(self):
        """technicalTiming 段内写错键名必须告警。

        回归背景：该段是嵌套 dict，_config_warnings 原先只校验 backtest 顶层键名，
        段内键名写错（例如 minHoldingDay 少个 s）不会生效也不会告警 —— 实验静默失效。
        """
        cfg = {"backtest": {"technicalTiming": {"enabled": True, "minHoldingDay": 3}}}
        warnings = stock_ml._config_warnings(cfg)
        self.assertEqual(len(warnings), 1)
        self.assertIn("minHoldingDay", warnings[0])
        self.assertIn("technicalTiming", warnings[0])
        ok = {"backtest": {"technicalTiming": {"enabled": True, "minHoldingDays": 3}}}
        self.assertEqual(stock_ml._config_warnings(ok), [])

    def test_stale_window_tmp_sweep_keeps_recent(self):
        """被 kill 的 job 会残留 2.5GB 临时帧；清扫只该删过期的，不能碰正在跑的。"""
        import os
        import time
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "tmp"
            old = tmp / "stock-wf-20260101T000000-old-w1"
            fresh = tmp / "stock-wf-20260101T000000-fresh-w1"
            other = tmp / "unrelated-dir"
            for d in (old, fresh, other):
                d.mkdir(parents=True)
                (d / "train-frame.pkl").write_bytes(b"x")
            past = time.time() - 7200
            os.utime(old, (past, past))
            os.utime(other, (past, past))
            removed = stock_ml._sweep_stale_window_tmp(Path(td), preserve_seconds=3600)
            self.assertEqual(removed, 1)
            self.assertFalse(old.exists(), "过期窗口临时目录应被清理")
            self.assertTrue(fresh.exists(), "未过期的窗口目录不能删（可能正在跑）")
            self.assertTrue(other.exists(), "非 stock-wf- 前缀的目录不能删")

    def test_non_mapping_section_warns_instead_of_crashing(self):
        warnings = stock_ml._config_warnings({"backtest": ["topN", 5]})
        self.assertEqual(len(warnings), 1)
        self.assertIn("不是键值表", warnings[0])

    def test_shipped_config_is_clean(self):
        cfg = stock_ml.load_config(CONFIG_PATH)
        self.assertEqual(stock_ml._config_warnings(cfg), [], "config/stock-ml.yaml 含不会生效的键")

    def test_get_model_config_reports_effective_costs(self):
        out = stock_ml.get_model_config(CONFIG_PATH)
        self.assertAlmostEqual(out["backtest"]["commissionRate"], 0.00008)
        self.assertAlmostEqual(out["backtest"]["stampDutyRate"], 0.0005)
        self.assertAlmostEqual(out["backtest"]["slippageRate"], 0.0)
        # 前端仍按 bps 展示，但值来自实际生效费率（0.8bp/5bp/40bp）。
        self.assertAlmostEqual(out["backtest"]["commissionBps"], 0.8)
        self.assertAlmostEqual(out["backtest"]["slippageBps"], 0.0)
        self.assertEqual(out["backtest"]["executionMode"], "same_day_close")
        self.assertEqual(out["backtest"]["snapshotTime"], "14:40")
        self.assertEqual(out["backtest"]["snapshotSource"], "close_proxy")
        self.assertTrue(out["backtest"]["technicalTiming"]["enabled"])
        self.assertEqual(out["backtest"]["technicalTiming"]["candidateTopN"], 20)
        self.assertAlmostEqual(out["backtest"]["maxPositionWeight"], 0.20)
        self.assertEqual(out["backtest"]["technicalTiming"]["goldenCrossLookbackDays"], 1)
        self.assertFalse(out["backtest"]["technicalTiming"]["volumeBreakout"])
        # 死叉出场口径：false = 一见死叉即卖（v2 口径）。
        # 固化依据：cuda 设备上两次跑出 Sharpe 1.128 / 1.097，而 true（v3）在 cpu 上仅 0.82–1.00。
        # 注意历史教训：该键曾在 yaml 与发布 run 之间漂移过（yaml false / job 参数传 true），
        # 所以这里必须断言"shipped config 的实际值"，以锁死漂移。
        self.assertFalse(out["backtest"]["technicalTiming"]["deathCrossRequireBelowMa20"])
        # 固化口径：最小持有期默认关闭（实测强制延长持有有害，Sharpe 1.129→0.273）。
        self.assertEqual(out["backtest"]["technicalTiming"]["minHoldingDays"], 0)
        # deviceType / cudaInProcess 必须在配置回显里可见（同步覆盖这两个键的后果最严重）
        self.assertEqual(out["model"]["deviceType"], "cpu")
        self.assertFalse(out["model"]["cudaInProcess"])
        self.assertTrue(out["model"]["deterministic"])
        self.assertTrue(out["model"]["forceColWise"])
        # 发布门槛：minPositiveWindowRate 于 2026-09-19 由 0.55 下调至 0.50
        # （25 样本窗口统计功效不足，13/25 与 14/25 不可区分）。锁死该值防止无声回退。
        self.assertAlmostEqual(out.get("publishGate", {}).get("minPositiveWindowRate"), 0.50)
        self.assertEqual(out["configWarnings"], [])

    def test_label_in_shipped_config_is_service_owned(self):
        cfg = stock_ml.load_config(CONFIG_PATH)
        self.assertIn(cfg["label"], stock_ml.LABEL_COLUMNS)

    def test_nested_backtest_override_keeps_sibling_keys(self):
        """job 参数覆盖 backtest.technicalTiming 里的单个开关时，不得丢掉同段其他键。

        回归背景：原实现用 dict.update() 做浅合并，传入
        {"backtest": {"technicalTiming": {"deathCrossRequireBelowMa20": True}}}
        会把整个 technicalTiming 段替换成只含该开关的字典，goldenCross/deathCross
        等键随之丢失。A/B 实验因此静默失效（表面看"开关无效"，实际是实验无效）。
        """
        base = {"technicalTiming": {"enabled": True, "goldenCross": True,
                                    "deathCross": True, "requireAboveMa20": True},
                "topN": 5}
        merged = stock_ml._deep_merge(base, {"technicalTiming": {"deathCrossRequireBelowMa20": True}})
        timing = merged["technicalTiming"]
        self.assertTrue(timing["deathCrossRequireBelowMa20"])
        self.assertTrue(timing["goldenCross"])
        self.assertTrue(timing["deathCross"])
        self.assertTrue(timing["requireAboveMa20"])
        self.assertEqual(merged["topN"], 5)
        # 不修改入参（同一 job 内 cfg 可能被多处共享）
        self.assertNotIn("deathCrossRequireBelowMa20", base["technicalTiming"])

    def test_existing_walkforward_rows_are_marked_legacy_by_migration(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "factors.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE stock_walkforward_runs(run_id TEXT PRIMARY KEY,generated_at TEXT,label TEXT,"
                        "start_date TEXT,end_date TEXT,windows INTEGER,config TEXT,metrics TEXT,holdings TEXT,"
                        "status TEXT)")
            con.execute("INSERT INTO stock_walkforward_runs VALUES('old','t','forward_3','a','b',1,'{}','{}','{}','complete')")
            con.commit(); con.close()
            out = stock_ml.list_walkforward(path)
            self.assertTrue(out["items"][0]["legacy"])
            self.assertEqual(out["items"][0]["resultVersion"], 1)
            self.assertFalse(stock_ml.publish_walkforward(path, "old")["ok"])

    def test_result_without_provenance_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "factors.db"
            con = stock_ml.connect(path)
            con.executescript(stock_ml.SCHEMA)
            stock_ml._ensure_walkforward_columns(con)
            con.execute(
                "INSERT INTO stock_walkforward_runs(run_id,generated_at,label,start_date,end_date,windows,"
                "config,metrics,holdings,status,result_version,is_published) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("no-proof", "t", "forward_3", "a", "b", 25,
                 '{"evaluationScope":"full_walkforward"}',
                 '{"windows":25,"sharpe":1,"excessReturn":1,"maxDrawdown":-0.1}',
                 '{"tradeDate":"20260918"}', "complete", 2, 0))
            con.commit(); con.close()
            out = stock_ml.publish_walkforward(path, "no-proof")
            self.assertFalse(out["ok"])
            self.assertIn("指纹", out["error"])


if __name__ == "__main__":
    unittest.main()
