"""meta.db 自举测试。

修复背景：quant-sync 原先完全依赖手工执行 scripts/migrate_meta.py —— 全新环境或临时目录下
meta.db 没有任何表，MetaStore 读 data_source_configs / tool_source_routes 直接 "no such table"，
SettingsStore 的目录写入也无处可落。现在启动时按 contracts/schemas/meta.sql 幂等自举。
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_sync.meta import ensure_meta_schema

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACT = REPO_ROOT / "contracts" / "schemas" / "meta.sql"

EXPECTED_TABLES = {"data_source_configs", "tool_source_routes", "datasets",
                   "dictionary_datasets", "dictionary_fields", "dictionary_mappings",
                   "dictionary_types", "dictionary_items"}


class MetaBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_creates_schema_in_fresh_environment(self):
        meta_path = self.root / "data" / "meta.db"
        self.assertTrue(ensure_meta_schema(meta_path, CONTRACT))
        con = sqlite3.connect(meta_path)
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
        self.assertTrue(EXPECTED_TABLES.issubset(tables), EXPECTED_TABLES - tables)

    def test_returns_false_and_creates_nothing_without_contract(self):
        meta_path = self.root / "data" / "meta.db"
        self.assertFalse(ensure_meta_schema(meta_path, self.root / "missing.sql"))
        self.assertFalse(meta_path.exists(), "契约缺失时不应创建空库")

    def test_is_idempotent_and_preserves_existing_rows(self):
        meta_path = self.root / "meta.db"
        self.assertTrue(ensure_meta_schema(meta_path, CONTRACT))
        con = sqlite3.connect(meta_path)
        con.execute("INSERT INTO data_source_configs(id,label,protocol,base_url,token,enabled,priority,"
                    "timeout_ms,updated_at) VALUES('promax','Promax','x-api-key','https://x','t',1,1,15000,'2026-09-01')")
        con.commit()
        con.close()
        self.assertTrue(ensure_meta_schema(meta_path, CONTRACT))  # 再自举一次
        con = sqlite3.connect(meta_path)
        try:
            rows = con.execute("SELECT id,base_url FROM data_source_configs").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("promax", "https://x")], "幂等自举不得清空既有数据")

    def test_deployed_layout_contract_is_readable(self):
        """测试用的临时 project_root 也要有契约文件（HTTP 测试依赖同一机制）。"""
        target = self.root / "contracts" / "schemas"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CONTRACT, target / "meta.sql")
        self.assertTrue(ensure_meta_schema(self.root / "data" / "meta.db", target / "meta.sql"))


if __name__ == "__main__":
    unittest.main()
