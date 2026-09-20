import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_query.repository import ReadOnlyRepository
from data_query.settings import settings

DATA_DIR = settings.project_root / "data"


class SelectionCandidateProbeTest(unittest.TestCase):
    """模型下拉探测走统一只读仓储（原先 main.py 自己 connect，绕过 query_only/busy_timeout）。"""

    def _repo(self, with_rows: bool) -> ReadOnlyRepository:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data_dir = Path(folder.name)
        con = sqlite3.connect(data_dir / "factors.db")
        con.execute("CREATE TABLE selection_candidates(trade_date TEXT, model_id TEXT, rank INTEGER, "
                    "code TEXT, score REAL, reasons_json TEXT, computed_at TEXT)")
        if with_rows:
            con.execute("INSERT INTO selection_candidates VALUES('20260911','ml_walkforward',1,'000001.SZ',1.0,'[]','x')")
        con.commit()
        con.close()
        return ReadOnlyRepository(data_dir)

    def test_detects_existing_model(self):
        self.assertTrue(self._repo(True).has_selection_candidates("ml_walkforward"))

    def test_missing_model_is_false(self):
        self.assertFalse(self._repo(True).has_selection_candidates("no_such_model"))

    def test_missing_database_is_false(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        # 目录里没有任何库：仓储应返回 False 而不是抛异常
        self.assertFalse(ReadOnlyRepository(Path(folder.name)).has_selection_candidates("ml_walkforward"))


class SelectionQueryTest(unittest.TestCase):
    """回归：/selection 曾对所有模型恒返回 0 条。

    两个成因都要锁住——(1) 日期子查询没按 model_id 收敛，会被其它模型更新的日期顶掉；
    (2) factor_snapshots 用 INNER JOIN，ML 候选日没有同日因子快照就被整批丢弃。
    """

    CONFIG = {"models": {"ml_walkforward": {"label": "机器学习", "minScore": 0, "minCoverage": 1.0,
                                            "maxCandidates": 100, "weights": {}},
                         "balanced": {"label": "均衡增强", "minScore": 55, "minCoverage": 0.8,
                                      "maxCandidates": 100, "weights": {"roe": 1.0}}}}

    def _repo(self) -> ReadOnlyRepository:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        data_dir = Path(folder.name)

        con = sqlite3.connect(data_dir / "factors.db")
        con.execute("CREATE TABLE selection_candidates(trade_date TEXT, model_id TEXT, rank INTEGER, "
                    "code TEXT, score REAL, reasons_json TEXT, computed_at TEXT, "
                    "source_run_id TEXT, result_version INTEGER DEFAULT 1)")
        # legacy 模型停在 20260911，ML 走到 20260915：全局 MAX 会选中 20260915
        con.execute("INSERT INTO selection_candidates VALUES"
                    "('20260911','balanced',1,'000001.SZ',88.0,'[]','x',NULL,1)")
        con.execute("INSERT INTO selection_candidates VALUES"
                    "('20260915','ml_walkforward',1,'300475.SZ',432.1,"
                    "'[{\"factor\":\"ml\",\"label\":\"机器学习预测\",\"score\":4.3,\"contribution\":0.2}]','x','run-1',2)")
        con.execute("INSERT INTO selection_candidates VALUES"
                    "('20260915','ml_walkforward',2,'603045.SH',400.0,'[]','x','run-1',2)")
        # legacy 因子快照已停更、且 /selection 不再 JOIN 它：名称一律由证券主档补
        con.execute("CREATE TABLE factor_snapshots(trade_date TEXT, code TEXT, name TEXT, industry TEXT, "
                    "close REAL, metrics_json TEXT, scores_json TEXT)")
        con.execute("INSERT INTO factor_snapshots VALUES('20260911','000001.SZ','平安银行','银行',11.7,'{}','{}')")
        con.execute("CREATE TABLE stock_walkforward_runs(run_id TEXT PRIMARY KEY, is_published INTEGER)")
        con.execute("INSERT INTO stock_walkforward_runs VALUES('run-1',1)")
        con.commit()
        con.close()

        market = sqlite3.connect(data_dir / "market.db")
        market.execute("CREATE TABLE security_master(code TEXT, name TEXT, industry TEXT)")
        market.execute("INSERT INTO security_master VALUES('300475.SZ','香农芯创','元器件')")
        market.execute("INSERT INTO security_master VALUES('000001.SZ','平安银行','银行')")
        market.commit()
        market.close()
        return ReadOnlyRepository(data_dir)

    def test_legacy_model_is_not_masked_by_newer_ml_date(self):
        """回归：日期子查询必须按 model_id 收敛，否则 legacy 模型会被 ML 的更新日期顶掉。"""
        result = self._repo().selection(self.CONFIG, "balanced", 10)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["items"][0]["code"], "000001.SZ")
        self.assertEqual(result["items"][0]["tradeDate"], "20260911")
        # 名称来自证券主档（原实现靠同日 factor_snapshots，legacy 退役后该 JOIN 已移除）
        self.assertEqual(result["items"][0]["name"], "平安银行")
        self.assertEqual(result["items"][0]["close"], None)

    def test_ml_model_survives_missing_factor_snapshot_and_gets_name_backfilled(self):
        result = self._repo().selection(self.CONFIG, "ml_walkforward", 10)
        self.assertEqual(result["count"], 2)
        first = result["items"][0]
        self.assertEqual(first["tradeDate"], "20260915")
        self.assertEqual(first["code"], "300475.SZ")
        # 同日没有因子快照 → name 来自 security_master
        self.assertEqual(first["name"], "香农芯创")
        self.assertEqual(first["industry"], "元器件")
        self.assertEqual(first["close"], None)
        self.assertEqual(first["reasons"][0]["factor"], "ml")

    def test_unknown_model_still_raises(self):
        with self.assertRaises(ValueError):
            self._repo().selection(self.CONFIG, "no_such_model", 10)


class SelectionModelsTest(unittest.TestCase):
    """`/selection/models` 的清单与去重（legacy 退役后只剩 ml_walkforward）。

    原实现无条件追加 ml_walkforward，一旦它已经被登记进 config 就会输出重复项。
    """

    def _call(self, config: dict, has_ml: bool) -> list[dict]:
        from data_query import main as dq_main

        original_config = dq_main._model_config
        original_has = dq_main.repository.has_selection_candidates
        dq_main._model_config = lambda: config
        dq_main.repository.has_selection_candidates = lambda model_id: has_ml
        self.addCleanup(lambda: (setattr(dq_main, "_model_config", original_config),
                                 setattr(dq_main.repository, "has_selection_candidates", original_has)))
        return dq_main.selection_models()["items"]

    def test_config_model_is_not_duplicated(self):
        config = {"models": {"ml_walkforward": {"label": "机器学习", "minScore": 0, "minCoverage": 1.0,
                                                "maxCandidates": 100, "weights": {}}}}
        items = self._call(config, has_ml=True)
        self.assertEqual([item["id"] for item in items], ["ml_walkforward"])

    def test_ml_is_appended_when_only_absent_from_config(self):
        config = {"models": {"some_other": {"label": "其它", "minScore": 1, "minCoverage": 1.0,
                                            "maxCandidates": 10, "weights": {"roe": 1.0}}}}
        self.assertEqual([i["id"] for i in self._call(config, has_ml=True)], ["some_other", "ml_walkforward"])
        self.assertEqual([i["id"] for i in self._call(config, has_ml=False)], ["some_other"])

    def test_real_config_lists_only_ml(self):
        """锁死退役事实：真实配置里只剩 ml_walkforward。"""
        config = json.loads((DATA_DIR.parent / "config" / "factor-models.yaml").read_text(encoding="utf-8"))
        self.assertEqual(list(config["models"]), ["ml_walkforward"])


class RepositoryTest(unittest.TestCase):
    def test_index_minutes_aggregate_to_requested_frequency(self):
        rows = [
            {"code": "000001.SH", "freq": "1MIN", "tradeTime": f"2026-09-07 09:3{i}:00",
             "open": 10 + i, "high": 11 + i, "low": 9 + i, "close": 10.5 + i,
             "volume": 100, "amount": 1000, "source": "promax"}
            for i in range(5)
        ]
        result = ReadOnlyRepository._aggregate_index_minutes(rows, 5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["freq"], "5MIN")
        self.assertEqual(result[0]["open"], 10)
        self.assertEqual(result[0]["close"], 14.5)
        self.assertEqual(result[0]["volume"], 500)

    @unittest.skipUnless((DATA_DIR / "minute.db").exists(),
                         "集成测试：需要本地 data/ 下的真实库（CI 上跳过）")
    def test_real_databases_are_readable(self):
        """真实库集成测试。

        注意：股票分钟线已于 2026-09-12 按使用方要求清除（minute.db 归零，见 docs/operations.md §7），
        因此分钟端点必须"可读且返回空"，而不是报错；数据集目录也不应再声明有 5m 数据。
        """
        repository = ReadOnlyRepository(DATA_DIR)
        datasets = repository.datasets()
        self.assertIsInstance(datasets, list)
        self.assertTrue(datasets, "数据集目录不应为空（日线等仍在）")

        rows = repository.minute_bars("600519.SH", "5m", "2026-08-28", "2026-08-30", 10)
        self.assertIsInstance(rows, list)
        self.assertLessEqual(len(rows), 10)
        for row in rows:  # 若将来恢复数据，字段也必须正确
            self.assertEqual(row["code"], "600519.SH")
            self.assertEqual(row["freq"], "5m")

        governance = repository.minute_governance("5m", 8, 4)
        self.assertEqual(governance["expected_buckets"], 49)
        self.assertLessEqual(len(governance["dates"]), 8)
        self.assertLessEqual(len(governance["codes"]), 4)

        for source in repository.meta_data_sources():
            self.assertEqual(source["token"], "")
            self.assertIn("hasToken", source)

if __name__ == "__main__":
    unittest.main()
