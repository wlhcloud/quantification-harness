"""数据库备份脚本测试：在线快照一致性 + 校验 + 轮转。

用临时目录造真 SQLite 库，覆盖：
- 库正在被别的连接写入时，VACUUM INTO 快照仍可打开且数据完整（WAL 场景）；
- 快照能通过 integrity_check，表数量与源库一致；
- 轮转只保留最近 N 份；
- 源库缺失时返回非 0，不静默成功。
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import backup_databases as backup  # noqa: E402


def _make_db(path: Path, rows: int = 50) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE bars (code TEXT, close REAL)")
        conn.executemany("INSERT INTO bars VALUES (?, ?)", [(f"{i:06d}", i * 1.5) for i in range(rows)])
        conn.commit()
    finally:
        conn.close()


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / "data"
        self.dest = self.root / "backups"
        self.data.mkdir()
        _make_db(self.data / "market.db", 50)
        _make_db(self.data / "factors.db", 20)

    def test_snapshot_is_readable_and_consistent(self):
        # 保持一个写入方连接开着，模拟服务正在写库
        writer = sqlite3.connect(self.data / "market.db")
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("INSERT INTO bars VALUES ('999999', 1.0)")
        writer.commit()

        code = backup.main(["--data-dir", str(self.data), "--dest", str(self.dest), "--keep", "2"])
        self.assertEqual(code, 0)
        snapshots = list(self.dest.iterdir())
        self.assertEqual(len(snapshots), 1)
        copy = snapshots[0] / "market.db"
        self.assertTrue(copy.exists())
        conn = sqlite3.connect(f"file:{copy.as_posix()}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0], 51)
        finally:
            conn.close()
        # 快照与 WAL/SHM 无关：目录里只有 .db 文件
        self.assertEqual(sorted(p.name for p in snapshots[0].iterdir()), ["factors.db", "market.db"])

    def test_rotation_keeps_newest(self):
        for stamp in ("20260101-010101", "20260102-010101", "20260103-010101"):
            (self.dest / stamp).mkdir(parents=True)
        (self.dest / "20260103-010101" / "keep.txt").write_text("x", encoding="utf-8")
        removed = backup.rotate(self.dest, keep=2)
        self.assertEqual(removed, ["20260101-010101"])
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["20260102-010101", "20260103-010101"])

    def test_missing_database_reports_failure(self):
        code = backup.main(["--data-dir", str(self.data), "--dest", str(self.dest),
                            "--databases", "nope.db"])
        self.assertEqual(code, 2)
        self.assertFalse(self.dest.exists())

    def test_dry_run_writes_nothing(self):
        code = backup.main(["--data-dir", str(self.data), "--dest", str(self.dest), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertFalse(self.dest.exists())

    def test_list_databases_orders_by_size_desc(self):
        # 大库先备（磁盘不足时尽早失败）；同尺寸时顺序不保证，只断言单调不增
        paths = backup.list_databases(self.data)
        sizes = [p.stat().st_size for p in paths]
        self.assertEqual(sorted(sizes, reverse=True), sizes)
        self.assertEqual({p.name for p in paths}, {"market.db", "factors.db"})


if __name__ == "__main__":
    unittest.main()
