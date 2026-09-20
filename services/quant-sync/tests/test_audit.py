import tempfile
import unittest
from pathlib import Path

from quant_sync.audit import AuditStore


class AuditStoreTest(unittest.TestCase):
    def test_persists_metrics_and_recovers_interrupted_run(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sync.db"
            store = AuditStore(path)
            run_id = store.start("daily", {"trade_date": "20260904"}, tool_id="daily")
            store.update(run_id, total=1, completed=1, received=5556, written=5556,
                         source="test-source", qualityStatus="passed")
            store.log(run_id, code="000001.SZ", request_api="daily", request_params={"trade_date": "20260904"},
                      received=5556, written=5556, source="test-source")
            self.assertEqual(store.logs(run_id)[0]["code"], "000001.SZ")
            store.db.close()

            restored = AuditStore(path)
            run = restored.latest(tool_id="daily")
            self.assertEqual(run["status"], "error")
            self.assertEqual(run["received"], 5556)
            self.assertEqual(run["parameters"]["trade_date"], "20260904")
            self.assertIn("服务重启", run["error"])
            restored.db.close()


if __name__ == "__main__":
    unittest.main()
