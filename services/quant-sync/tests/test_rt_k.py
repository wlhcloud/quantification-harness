import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from quant_sync.audit import AuditStore
from quant_sync.tool_sync import ToolSync


class _Client:
    def call(self, api, params, dataset=None):
        self.request = (api, params, dataset)
        row = {
            "code": "000001.SH", "name": "上证指数", "open": 3900, "high": 3950, "low": 3890,
            "close": 3936.8, "pre_close": 3930.1, "volume": 10, "amount": 20, "num": None,
        }
        return SimpleNamespace(rows=[row], raw_rows=[{"ts_code": "000001.SH"}], source="promax")


class _Meta:
    def register_dataset(self, *args):
        self.dataset = args


class RealtimeIndexSyncTest(unittest.TestCase):
    def test_writes_calculated_snapshot_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, meta = _Client(), _Meta()
            audit = AuditStore(root / "sync.db")
            sync = ToolSync(client, root / "market.db", root / "finance.db", meta, audit)
            run_id = audit.start("rt_idx_k", {}, tool_id="rt_idx_k")
            sync.states["rt_idx_k"]["runId"] = run_id

            sync._sync_realtime_index("rt_idx_k")

            db = sqlite3.connect(root / "market.db")
            row = db.execute("SELECT code,change,pct_chg,source FROM index_snapshots").fetchone()
            db.close()
            self.assertEqual(row[0], "000001.SH")
            self.assertAlmostEqual(row[1], 6.7)
            self.assertAlmostEqual(row[2], 6.7 / 3930.1 * 100)
            self.assertEqual(row[3], "promax")
            self.assertEqual(client.request[0], "rt_idx_k")
            self.assertEqual(client.request[2], "market.index_snapshot")
            self.assertEqual(meta.dataset[0], "market.index_snapshot")
            self.assertEqual(audit.logs(run_id)[0]["source"], "promax")
            audit.db.close()


if __name__ == "__main__":
    unittest.main()
