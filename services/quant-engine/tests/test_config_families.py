# -*- coding: utf-8 -*-
"""其余运行族的配置档案列（补齐 ③ 的剩余四个族）。

覆盖面：
  - etf_walkforward_runs / etf_model_runs（etf-quant.db）
  - backtest_runs / short_backtest_runs（backtest.db）

这些族本来就有自己的配置列（config / params / config_json / params_json），
所以只补指纹四项；但**列必须在**，否则跨族"两次运行是否同配置"就查不了。
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

from quant_engine.backtest import MONTHLY_SCHEMA, SHORT_SCHEMA  # noqa: E402
from quant_engine.backtest import short as short_bt  # noqa: E402
from quant_engine.etf_quant import db as etf_db  # noqa: E402
from quant_engine.stock_ml.config_integrity import (  # noqa: E402
    CONFIG_COLUMNS, config_columns_for, ensure_config_columns,
)

FINGERPRINT_ONLY = ("config_sha256", "config_yaml_sha256", "git_commit", "git_dirty")


class SchemaDdlTest(unittest.TestCase):
    """新库应当**直接**带指纹列，而不是依赖事后迁移。"""

    def _cols(self, script: str, table: str) -> set[str]:
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(script)
            return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        finally:
            con.close()

    def test_backtest_runs_has_fingerprint_columns(self):
        cols = self._cols(MONTHLY_SCHEMA, "backtest_runs")
        for col in FINGERPRINT_ONLY:
            self.assertIn(col, cols, f"backtest_runs 缺少 {col}")
        self.assertIn("config_json", cols)

    def test_short_backtest_runs_has_fingerprint_columns(self):
        cols = self._cols(SHORT_SCHEMA, "short_backtest_runs")
        for col in FINGERPRINT_ONLY:
            self.assertIn(col, cols, f"short_backtest_runs 缺少 {col}")
        self.assertIn("params_json", cols)

    def test_short_schema_is_single_source_of_truth(self):
        """short.py 不得再抄一份表结构：改一处漏一处会让两条路径的列不一致。"""
        self.assertIs(short_bt.SCHEMA, SHORT_SCHEMA)

    def test_etf_tables_are_created_by_scimema_then_migrated(self):
        with tempfile.TemporaryDirectory() as td:
            con = etf_db.connect(Path(td) / "etf-quant.db")
            try:
                for table in ("etf_model_runs", "etf_walkforward_runs"):
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    for col in FINGERPRINT_ONLY:
                        self.assertIn(col, cols, f"{table} 缺少 {col}")
            finally:
                con.close()


class MigrationTest(unittest.TestCase):
    def test_etf_connect_migrates_old_tables(self):
        """旧库（无指纹列）经 connect() 后应被补齐。"""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "etf-quant.db"
            raw = sqlite3.connect(path)
            raw.executescript(
                "CREATE TABLE etf_model_runs (run_id TEXT PRIMARY KEY, generated_at TEXT,"
                " label TEXT, params TEXT, status TEXT);"
                "CREATE TABLE etf_walkforward_runs (run_id TEXT PRIMARY KEY, generated_at TEXT,"
                " label TEXT, config TEXT, metrics TEXT, holdings TEXT, status TEXT, error TEXT);")
            raw.commit()
            raw.close()
            con = etf_db.connect(path)
            try:
                for table in ("etf_model_runs", "etf_walkforward_runs"):
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    for col in FINGERPRINT_ONLY:
                        self.assertIn(col, cols, f"{table} 迁移后仍缺 {col}")
                    self.assertNotIn("config_json", cols,
                                     f"{table} 有自己的配置列，不该再被塞一份 config_json")
            finally:
                con.close()

    def test_ensure_config_columns_skips_requested(self):
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE TABLE t (run_id TEXT PRIMARY KEY, config TEXT)")
            added = ensure_config_columns(con, "t", skip=("config_json",))
            self.assertNotIn("config_json", added)
            for col in FINGERPRINT_ONLY:
                self.assertIn(col, added)
            # 幂等
            self.assertEqual(ensure_config_columns(con, "t", skip=("config_json",)), [])
        finally:
            con.close()

    def test_ensure_config_columns_on_missing_table_is_noop(self):
        """纯读路径不该因为"顺手迁移"而建出半张表。"""
        con = sqlite3.connect(":memory:")
        try:
            self.assertEqual(ensure_config_columns(con, "does_not_exist"), [])
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("does_not_exist", tables)
        finally:
            con.close()

    def test_config_columns_for_can_skip_config_json(self):
        cols = config_columns_for({"a": 1}, include_config_json=False)
        self.assertNotIn("config_json", cols)
        for col in FINGERPRINT_ONLY:
            self.assertIn(col, cols)
        self.assertEqual(len(cols["config_sha256"]), 64)

    def test_config_columns_for_includes_schema_version(self):
        cols = config_columns_for({"a": 1})
        payload = json.loads(cols["config_json"])
        self.assertEqual(payload["a"], 1)
        self.assertIn("configSchemaVersion", payload)

    def test_config_columns_constant_matches_helper(self):
        full = config_columns_for({"a": 1})
        self.assertEqual(set(full), set(CONFIG_COLUMNS))


class ResultVersionScopeTest(unittest.TestCase):
    """配置档案是**跨族**的：列名与语义必须一致，否则无法跨族比较。"""

    def test_all_families_use_same_fingerprint_field_names(self):
        con = sqlite3.connect(":memory:")
        try:
            con.executescript(MONTHLY_SCHEMA)
            con.executescript(SHORT_SCHEMA)
            for table in ("backtest_runs", "short_backtest_runs"):
                cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                self.assertTrue(set(FINGERPRINT_ONLY).issubset(cols),
                                f"{table} 的指纹列名与统一约定不符")
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
