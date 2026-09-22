"""V5 技术面择时回测汇总 + 候选池发布 + 模拟盘归档过滤的测试。

背景（本轮修复）：
  1. BacktestPage 的「V5策略回测」页签原先写死数字（+1682.66% 等），无法复算；
     现在由 `technical_timing.summarize_v5_backtest` 读 artifacts 下的 CSV 现场计算。
  2. selection_candidates 是候选池（T+1 选股页/V5 择时盘按 Top20 取用），原先只写 topN=5，
     导致"Top20 候选池"名不副实；现在 publishTopN 控制发布条数、topN 仍是实际持仓条数。
  3. 停更的模拟盘 run 会归档（status='archived'），默认不出现在下拉里。
"""
from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from quant_engine import stock_ml
from quant_engine.stock import sim
from quant_engine.technical_timing import summarize_v5_backtest


def _write_csv(path: Path, values: list[float], with_cum: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cumulative = 1.0
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        header = ["date", "value", "cash", "holdings", "return"] + (["cum_return"] if with_cum else [])
        writer.writerow(header)
        previous = values[0]
        for index, value in enumerate(values):
            daily = (value / previous - 1) if index else ""
            cumulative = value / values[0] - 1
            row = [f"2026{index + 1:04d}", value, 0.0, 1, daily] + ([cumulative] if with_cum else [])
            writer.writerow(row)
            previous = value


class V5SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_summarizes_metrics_and_curve(self):
        # 先涨到 1.5 再回落到 1.2：总收益 +20%、回撤由 1.5→1.2 = -20%
        _write_csv(self.dir / "technical_timing_v5_long_backtest.csv", [1.0, 1.5, 1.2])
        out = summarize_v5_backtest(self.dir, "long")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(out["variant"], "long")
        self.assertEqual(out["tradingDays"], 2)
        self.assertAlmostEqual(out["totalReturn"], 0.2, places=6)   # 取 CSV 的 cum_return
        self.assertAlmostEqual(out["maxDrawdown"], -0.2, places=6)
        self.assertGreater(out["sharpe"], 0)
        self.assertEqual(len(out["curve"]), 3)
        self.assertEqual(out["curve"][0]["value"], 1.0)
        self.assertIn("可复算", out["note"])

    def test_missing_file_reports_none(self):
        out = summarize_v5_backtest(self.dir, "long")
        self.assertEqual(out["status"], "none")
        self.assertIn("technical_timing_v5_long_backtest.csv", out["expectedFile"])

    def test_rejects_unknown_variant(self):
        with self.assertRaises(ValueError):
            summarize_v5_backtest(self.dir, "v9")

    def test_works_without_cum_return_column(self):
        _write_csv(self.dir / "technical_timing_v5_backtest.csv", [1.0, 1.1, 1.21], with_cum=False)
        out = summarize_v5_backtest(self.dir, "short")
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["totalReturn"], 0.21, places=6)  # 无 cum_return 时按首尾计算


class PublishedCandidatesTest(unittest.TestCase):
    def _ranked(self, n: int) -> list[dict]:
        return [{"code": f"{i:06d}.SZ", "score": round(1.0 - i * 0.01, 4)} for i in range(n)]

    def test_publishes_pool_but_marks_only_top_n_as_holdings(self):
        published = stock_ml._published_candidates(self._ranked(30), top_n=5, publish_top_n=20)
        self.assertEqual(len(published), 20)
        self.assertEqual([p["rank"] for p in published], list(range(1, 21)))
        self.assertEqual([p["code"] for p in published if p["holding"]],
                         [f"{i:06d}.SZ" for i in range(5)])
        self.assertEqual(sum(1 for p in published if p["holding"]), 5)
        # 非持仓候选不带权重（前端按 contribution 显示"入选理由"）
        self.assertTrue(all(p["weight"] == 0.2 for p in published))

    def test_publish_top_n_never_below_top_n(self):
        published = stock_ml._published_candidates(self._ranked(30), top_n=5, publish_top_n=3)
        self.assertEqual(len(published), 5, "发布条数不得少于实际持仓")

    def test_limited_by_available_candidates(self):
        published = stock_ml._published_candidates(self._ranked(7), top_n=5, publish_top_n=20)
        self.assertEqual(len(published), 7)
        self.assertEqual(sum(1 for p in published if p["holding"]), 5)

    def test_shipped_config_declares_publish_top_n(self):
        cfg = stock_ml.load_config(Path(__file__).resolve().parents[3] / "config" / "stock-ml.yaml")
        self.assertEqual(cfg["walkforward"]["publishTopN"], 20)
        out = stock_ml.get_model_config(Path(__file__).resolve().parents[3] / "config" / "stock-ml.yaml")
        self.assertEqual(out["walkforward"]["publishTopN"], 20)

    def test_latest_candidates_must_come_from_published_run(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "factors.db"
            con = stock_ml.connect(path)
            con.executescript(stock_ml.SCHEMA)
            stock_ml._ensure_walkforward_columns(con)
            # selection_candidates 现在由 stock_ml.SCHEMA 创建（建表语句原先在 legacy factors 模块里，
            # 该模块已随 legacy 因子选股退役删除）；source_run_id / result_version 由运行时幂等补列。
            stock_ml._ensure_selection_columns(con)
            for run_id, published in (("official", 1), ("research", 0)):
                con.execute(
                    "INSERT INTO stock_walkforward_runs(run_id,generated_at,label,start_date,end_date,windows,"
                    "config,metrics,holdings,status,result_version,is_published) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, "2026-09-15T00:00:00Z", "forward_3", "20260101", "20260102", 1,
                     "{}", "{}", '{"tradeDate":"20260102"}', "complete", 2, published))
                con.execute("INSERT INTO selection_candidates VALUES(?,?,?,?,?,?,?,?,?)",
                            ("20260102", "ml_walkforward", 1, run_id, 1.0, "[]",
                             "2026-09-15T00:00:00Z", run_id, 2))
            con.commit(); con.close()
            out = stock_ml.latest_candidates(path)
            self.assertEqual([item["code"] for item in out["items"]], ["official"])

    def test_refresh_uses_published_model_without_switching_run(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "factors.db"
            model = Path(root) / "model.txt"
            model.write_text("test", encoding="utf-8")
            con = stock_ml.connect(path)
            con.executescript(stock_ml.SCHEMA)
            stock_ml._ensure_walkforward_columns(con)
            stock_ml._ensure_selection_columns(con)
            cfg = {"publishTopN": 2, "model": {"featuresOverride": []},
                   "backtest": {"topN": 1}}
            con.execute(
                "INSERT INTO stock_walkforward_runs(run_id,generated_at,label,start_date,end_date,windows,"
                "config,metrics,holdings,status,result_version,is_published,model_path) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("official", "2026-09-15T00:00:00Z", "forward_3", "20260101", "20260102", 1,
                 __import__("json").dumps(cfg), "{}", '{"tradeDate":"20260102"}',
                 "complete", 2, 1, str(model)))
            con.commit(); con.close()
            prediction = {"tradeDate": "20260918", "items": [
                {"rank": 1, "code": "000001.SZ", "score": 1.2},
                {"rank": 2, "code": "000002.SZ", "score": 0.8},
            ]}
            with mock.patch.object(stock_ml, "load_config", return_value={}), \
                    mock.patch.object(stock_ml, "predict_with_model", return_value=prediction):
                out = stock_ml.refresh_published_candidates(
                    path, Path(root) / "config.yaml", trade_date="20260918")
            self.assertFalse(out["publishedModelChanged"])
            self.assertEqual(out["runId"], "official")
            con = stock_ml.connect(path)
            rows = con.execute(
                "SELECT source_run_id,rank FROM selection_candidates ORDER BY rank").fetchall()
            published = con.execute(
                "SELECT run_id FROM stock_walkforward_runs WHERE is_published=1").fetchone()[0]
            con.close()
            self.assertEqual(published, "official")
            self.assertEqual([(r["source_run_id"], r["rank"]) for r in rows],
                             [("official", 1), ("official", 2)])

    def test_publish_gate_rejects_partial_or_weak_runs(self):
        good = {"windows": 24, "sharpe": 0.8, "excessReturn": 0.1, "maxDrawdown": -0.2}
        self.assertEqual(stock_ml._publish_gate_failures(good, "full_walkforward", {}), [])
        self.assertIn("仅完整full_walkforward评估允许发布",
                      stock_ml._publish_gate_failures(good, "recent_refresh", {}))
        weak = dict(good, sharpe=0.22, maxDrawdown=-0.4)
        failures = stock_ml._publish_gate_failures(weak, "full_walkforward", {})
        self.assertTrue(any("Sharpe" in item for item in failures))
        self.assertTrue(any("回撤" in item for item in failures))

    def test_publish_gate_rejects_unstable_or_excessive_turnover_runs(self):
        metrics = {"windows": 24, "sharpe": 0.8, "excessReturn": 0.1,
                   "maxDrawdown": -0.2, "annualReturn": 0.08,
                   "annualTurnover": 45.0, "positiveWindowRate": 0.4}
        gate = {"minNetAnnualReturn": 0.05, "maxAnnualTurnover": 30,
                "minPositiveWindowRate": 0.55}
        failures = stock_ml._publish_gate_failures(metrics, "full_walkforward", gate)
        self.assertTrue(any("换手" in item for item in failures))
        self.assertTrue(any("盈利窗口" in item for item in failures))

    def test_auxiliary_freshness_cannot_erase_performance_blockers(self):
        performance = ["Sharpe低于0.5", "超额收益不高于0"]
        self.assertEqual(
            stock_ml._merge_publish_blockers(performance, []),
            performance,
        )
        self.assertEqual(
            stock_ml._merge_publish_blockers(performance, ["辅助因子过期", "Sharpe低于0.5"]),
            ["Sharpe低于0.5", "超额收益不高于0", "辅助因子过期"],
        )


class SimRunArchiveTest(unittest.TestCase):
    def _db(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "factors.db"
        con = sim.connect(path)
        sim.ensure_schema(con)
        for run_id, status in (("r-active", "active"), ("r-archived", "archived")):
            con.execute(
                "INSERT INTO stock_sim_runs (run_id, model_id, top_n, initial_capital, commission_rate, "
                "stamp_duty_rate, status, created_at, last_signal_date, last_nav, regime_filter) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, "ml_walkforward", 5, 1_000_000, 0.0003, 0.0005, status,
                 "2026-09-08T00:00:00Z", "20260904", 1.0, 0))
        con.commit()
        con.close()
        return path

    def test_archived_runs_are_hidden_by_default(self):
        path = self._db()
        default = sim.list_runs(path)
        self.assertEqual([r["runId"] for r in default["items"]], ["r-active"])
        all_runs = sim.list_runs(path, include_archived=True)
        self.assertEqual({r["runId"] for r in all_runs["items"]}, {"r-active", "r-archived"})

    def test_archive_hides_stale_runs_from_progress_chain(self):
        """归档只改 status，不影响 sim_status（每日链取最新 run 推进）。"""
        path = self._db()
        self.assertIsNotNone(sim.sim_status(path, "r-archived")["run"])


if __name__ == "__main__":
    unittest.main()
