# -*- coding: utf-8 -*-
"""配置可观测性（config_integrity）的行为锁。

这些用例的作用是防止"指纹机制悄悄失效"——它本身不产出业务指标，
一旦坏掉不会有任何功能报错，只会让历史运行重新变得不可回溯。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "quant-engine" / "src"))

from quant_engine.stock_ml import config_integrity as ci  # noqa: E402
from quant_engine.stock_ml.common import SCHEMA  # noqa: E402


class EffectiveConfigHashTest(unittest.TestCase):
    def test_hash_is_deterministic_regardless_of_key_order(self):
        a = ci.effective_config_sha256({"x": 1, "y": [1, 2], "z": {"b": 2, "a": 1}})
        b = ci.effective_config_sha256({"z": {"a": 1, "b": 2}, "y": [1, 2], "x": 1})
        self.assertEqual(a, b, "键序不同的等价配置必须得到同一指纹")

    def test_different_values_produce_different_hash(self):
        a = ci.effective_config_sha256({"minPositiveWindowRate": 0.55})
        b = ci.effective_config_sha256({"minPositiveWindowRate": 0.50})
        self.assertNotEqual(a, b, "配置取值不同必须指纹不同")

    def test_nested_change_is_detected(self):
        # 真实的坑：只改 technicalTiming 里的一个开关，必须能被指纹捕获
        base = {"backtest": {"technicalTiming": {"goldenCross": True, "volumeBreakout": False}}}
        changed = {"backtest": {"technicalTiming": {"goldenCross": True, "volumeBreakout": True}}}
        self.assertNotEqual(ci.effective_config_sha256(base), ci.effective_config_sha256(changed))

    def test_fingerprint_fields_must_not_be_hashed(self):
        """指纹字段自身参与哈希会造成自指——必须由调用方剔除。"""
        cfg = {"topN": 5}
        h1 = ci.effective_config_sha256(cfg)
        with_fp = {**cfg, "effectiveConfigSha256": h1}
        self.assertNotEqual(h1, ci.effective_config_sha256(with_fp))


class FingerprintShapeTest(unittest.TestCase):
    def test_build_fingerprint_reports_all_four_dimensions(self):
        fp = ci.build_fingerprint({"topN": 5})
        for key in ("effectiveConfigSha256", "yamlSha256", "gitCommit", "gitDirty"):
            self.assertIn(key, fp, f"指纹必须包含 {key}")
        self.assertEqual(len(fp["effectiveConfigSha256"]), 64)

    def test_yaml_hash_none_when_no_path(self):
        fp = ci.build_fingerprint({"topN": 5}, None)
        self.assertIsNone(fp["yamlSha256"])

    def test_yaml_hash_changes_when_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a.yaml"
            p.write_text("topN: 5\n", encoding="utf-8")
            h1 = ci.build_fingerprint({"topN": 5}, p)["yamlSha256"]
            p.write_text("topN: 6\n", encoding="utf-8")
            h2 = ci.build_fingerprint({"topN": 5}, p)["yamlSha256"]
            self.assertIsNotNone(h1)
            self.assertNotEqual(h1, h2, "YAML 文件改动必须反映在 yamlSha256 上")

    def test_file_sha256_returns_none_for_missing_file(self):
        self.assertIsNone(ci.file_sha256(Path("definitely/not/here.yaml")))


class FingerprintPersistenceTest(unittest.TestCase):
    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        return con

    def test_record_then_get_roundtrip(self):
        con = self._con()
        eff = {"topN": 5, "model": {"nEstimators": 300}}
        fp = ci.build_fingerprint(eff)
        ci.record_fingerprint(con, fp, eff, run_id="run-A", generated_at="2026-09-20T00:00:00Z")
        got = ci.get_fingerprint(con, fp["effectiveConfigSha256"])
        self.assertIsNotNone(got)
        self.assertEqual(got["run_id"], "run-A")
        self.assertEqual(got["config"], eff, "必须能取回完整生效配置")

    def test_same_config_reuses_one_row(self):
        """同配置跑多次只应留一条档案——这是"配置 → 运行"查询的基础。"""
        con = self._con()
        eff = {"topN": 5}
        fp = ci.build_fingerprint(eff)
        ci.record_fingerprint(con, fp, eff, run_id="run-A", generated_at="t1")
        ci.record_fingerprint(con, fp, eff, run_id="run-B", generated_at="t2")
        n = con.execute("SELECT COUNT(*) FROM config_fingerprint").fetchone()[0]
        self.assertEqual(n, 1, "相同哈希不应产生第二行")

    def test_original_yaml_text_is_preserved(self):
        """先写入的证据更可信：后写的空值不得覆盖已有 YAML 文本。"""
        con = self._con()
        eff = {"topN": 5}
        fp = ci.build_fingerprint(eff)
        ci.record_fingerprint(con, fp, eff, run_id="run-A", yaml_text="topN: 5\n")
        ci.record_fingerprint(con, fp, eff, run_id="run-B", yaml_text=None)
        got = ci.get_fingerprint(con, fp["effectiveConfigSha256"])
        self.assertEqual(got["yaml_text"], "topN: 5\n")

    def test_list_fingerprints_returns_rows(self):
        con = self._con()
        for i in range(3):
            eff = {"topN": i}
            fp = ci.build_fingerprint(eff)
            ci.record_fingerprint(con, fp, eff, run_id=f"run-{i}", generated_at=f"t{i}")
        items = ci.list_fingerprints(con)
        self.assertEqual(len(items), 3)
        self.assertIn("config_sha256", items[0])

    def test_get_fingerprint_missing_returns_none(self):
        con = self._con()
        self.assertIsNone(ci.get_fingerprint(con, "0" * 64))


class SnapshotTest(unittest.TestCase):
    def test_snapshot_written_and_content_addressed(self):
        with tempfile.TemporaryDirectory() as td:
            art = Path(td)
            eff = {"topN": 5}
            fp = ci.build_fingerprint(eff)
            target = ci.write_snapshot(art, fp, eff, run_id="run-A", yaml_text="topN: 5\n")
            self.assertTrue((target / "config.json").exists())
            self.assertTrue((target / "meta.json").exists())
            self.assertTrue((target / "config.yaml").exists())
            self.assertEqual(target.name, fp["effectiveConfigSha256"],
                             "快照目录名必须是配置哈希")

    def test_snapshot_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            art = Path(td)
            eff = {"topN": 5}
            fp = ci.build_fingerprint(eff)
            p1 = ci.write_snapshot(art, fp, eff, run_id="run-A")
            p2 = ci.write_snapshot(art, fp, eff, run_id="run-B")
            self.assertEqual(p1, p2)
            self.assertEqual(len(list((art / ci.SNAPSHOT_DIRNAME).iterdir())), 1,
                             "同配置不应产生第二个目录")

    def test_snapshot_config_is_restorable(self):
        """存档必须足以还原配置本体（不含指纹字段，故可直接重算哈希）。"""
        with tempfile.TemporaryDirectory() as td:
            art = Path(td)
            eff = {"topN": 5, "backtest": {"technicalTiming": {"goldenCross": True}}}
            fp = ci.build_fingerprint(eff)
            target = ci.write_snapshot(art, fp, eff, run_id="run-A")
            restored = json.loads((target / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(restored, eff)
            self.assertEqual(ci.effective_config_sha256(restored),
                             fp["effectiveConfigSha256"], "还原后的配置必须重算出同一哈希")


class GitInfoTest(unittest.TestCase):
    """代码版本来自部署时写下的构建信息，运行时**不得** shell out。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("QUANT_BUILD_COMMIT", "QUANT_BUILD_DIRTY", "QUANT_PROJECT_ROOT",
                        "QUANT_ENGINE_PROJECT_ROOT")}
        for key in self._saved:
            os.environ.pop(key, None)
        ci.git_info(refresh=True)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        ci.git_info(refresh=True)

    def test_git_info_shape(self):
        info = ci.git_info(refresh=True)
        for key in ("commit", "dirty", "short", "error", "source"):
            self.assertIn(key, info)

    def test_git_info_is_cached(self):
        """必须进程内缓存：服务运行中被人 git pull 不应改变已执行运行的代码版本记录。"""
        first = ci.git_info(refresh=True)
        second = ci.git_info()
        self.assertIs(first, second)

    def test_env_vars_are_honoured(self):
        os.environ["QUANT_BUILD_COMMIT"] = "a" * 40
        os.environ["QUANT_BUILD_DIRTY"] = "0"
        info = ci.git_info(refresh=True)
        self.assertEqual(info["commit"], "a" * 40)
        self.assertEqual(info["short"], "a" * 7)
        self.assertFalse(info["dirty"])
        self.assertEqual(info["source"], "env")
        self.assertIsNone(info["error"])

    def test_dirty_parsing_accepts_common_spellings(self):
        for raw, expected in (("1", True), ("true", True), ("dirty", True),
                              ("0", False), ("false", False), ("clean", False)):
            self.assertIs(ci._parse_dirty(raw), expected, f"{raw!r} 应解析为 {expected}")

    def test_unknown_dirty_is_none_not_false(self):
        """未知必须是 None：把它当成 clean 会把不可复现的运行标成可复现。"""
        self.assertIsNone(ci._parse_dirty(None))
        self.assertIsNone(ci._parse_dirty(""))
        self.assertIsNone(ci._parse_dirty("maybe"))

    def test_build_info_file_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ci.BUILD_INFO_FILENAME).write_text(
                json.dumps({"commit": "b" * 40, "dirty": False}), encoding="utf-8")
            os.environ["QUANT_PROJECT_ROOT"] = td
            info = ci.git_info(refresh=True)
            self.assertEqual(info["commit"], "b" * 40)
            self.assertEqual(info["source"], "build-info")

    def test_missing_build_info_reports_error_not_silent(self):
        """拿不到版本必须显式报错：静默的 None 会让人以为"没有版本"是正常的。"""
        with tempfile.TemporaryDirectory() as td:
            os.environ["QUANT_PROJECT_ROOT"] = td
            info = ci.git_info(refresh=True)
            self.assertIsNone(info["commit"])
            self.assertIsNotNone(info["error"])
            self.assertIn(ci.BUILD_INFO_FILENAME, info["error"])

    def test_env_takes_precedence_over_file(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ci.BUILD_INFO_FILENAME).write_text(
                json.dumps({"commit": "from-file", "dirty": True}), encoding="utf-8")
            os.environ["QUANT_PROJECT_ROOT"] = td
            os.environ["QUANT_BUILD_COMMIT"] = "from-env"
            os.environ["QUANT_BUILD_DIRTY"] = "0"
            info = ci.git_info(refresh=True)
            self.assertEqual(info["commit"], "from-env")
            self.assertFalse(info["dirty"])

    def test_module_does_not_import_subprocess(self):
        """架构约定：服务进程不得 shell out。这条曾经真的被违反过。"""
        source = Path(ci.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("subprocess.run", source)


class LegacyRowTest(unittest.TestCase):
    """指纹机制上线前的旧 run 必须可读，且不能被误报成"无配置"。"""

    def test_row_without_fingerprint_columns_falls_back_to_config(self):
        from quant_engine.stock_ml import _fingerprint_from_row
        row = {"config": "{}"}  # 老 schema：没有指纹列
        cfg = {"effectiveConfigSha256": "abc", "gitCommit": "deadbeef", "gitDirty": True}
        fp = _fingerprint_from_row(row, cfg)
        self.assertEqual(fp["configSha256"], "abc")
        self.assertEqual(fp["gitCommit"], "deadbeef")
        self.assertTrue(fp["gitDirty"])

    def test_legacy_row_reports_none_not_empty_string(self):
        from quant_engine.stock_ml import _fingerprint_from_row
        fp = _fingerprint_from_row({}, {})
        self.assertIsNone(fp["configSha256"], "旧记录应是 None（未知），不是空串")
        self.assertIsNone(fp["gitDirty"])

    def test_columns_take_precedence_over_config_json(self):
        from quant_engine.stock_ml import _fingerprint_from_row
        row = {"config_sha256": "from-column", "git_commit": "c1", "git_dirty": 0,
               "config_yaml_sha256": "y1"}
        fp = _fingerprint_from_row(row, {"effectiveConfigSha256": "from-json"})
        self.assertEqual(fp["configSha256"], "from-column")
        self.assertFalse(fp["gitDirty"])


class MigrationTest(unittest.TestCase):
    def test_ensure_walkforward_columns_adds_fingerprint_columns(self):
        from quant_engine.stock_ml.common import _ensure_walkforward_columns
        con = sqlite3.connect(":memory:")
        try:
            # 模拟旧库：先建不含新列的表
            con.executescript(
                "CREATE TABLE stock_walkforward_runs (run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL,"
                " label TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,"
                " windows INTEGER NOT NULL, config TEXT NOT NULL, metrics TEXT NOT NULL,"
                " holdings TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'complete');")
            _ensure_walkforward_columns(con)
            cols = {r[1] for r in con.execute("PRAGMA table_info(stock_walkforward_runs)")}
            for col in ("config_sha256", "config_yaml_sha256", "git_commit", "git_dirty"):
                self.assertIn(col, cols, f"迁移必须补上 {col}")
            # 幂等：再跑一次不应报错
            _ensure_walkforward_columns(con)
        finally:
            con.close()

    def test_schema_ddl_includes_fingerprint_columns(self):
        for col in ("config_sha256", "config_yaml_sha256", "git_commit", "git_dirty"):
            self.assertIn(col, SCHEMA, f"新库 DDL 必须直接含 {col}（避免依赖迁移）")


if __name__ == "__main__":
    unittest.main()
