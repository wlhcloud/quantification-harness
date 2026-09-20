import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from quant_sync.daily import DailySync


class DailyResearchDataTest(unittest.TestCase):
    def test_stores_point_in_time_status_and_adjustment(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = DailySync(Path(directory) / "market.db", object(), object())
            sync.db.execute("INSERT INTO security_master VALUES(?,?,?,?,?,?,?,?)",
                            ("000001.SZ", "平安银行", "深圳", "银行", "主板", "19910403", "L", "now"))
            bars = [{"code": "000001.SZ", "volume": 100}]
            limits = [{"ts_code": "000001.SZ", "up_limit": 12.0, "down_limit": 8.0}]
            adjustments = [{"ts_code": "000001.SZ", "adj_factor": 1.25}]
            sync._store_research_rows("20260105", bars, limits, adjustments, "now")
            status = sync.db.execute("SELECT * FROM security_status_history").fetchone()
            factor = sync.db.execute("SELECT adj_factor FROM adjustment_factors").fetchone()[0]
            self.assertEqual(status["code"], "000001.SZ")
            self.assertEqual(status["limit_up"], 12.0)
            self.assertEqual(factor, 1.25)
            sync.db.close()

    def test_paginates_capped_adjustment_results(self):
        class Client:
            def __init__(self):
                self.offsets = []

            def call(self, _api, params, dataset=None, timeout_ms=None):
                offset = int(params["offset"])
                self.offsets.append(offset)
                return SimpleNamespace(rows=([{"n": i} for i in range(5000)] if offset == 0 else [{"n": 5000}]))

        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            sync = DailySync(Path(directory) / "market.db", client, object())
            rows = sync._paged_rows("adj_factor", {"trade_date": "20260105"})
            self.assertEqual(len(rows), 5001)
            self.assertEqual(client.offsets, [0, 5000])
            sync.db.close()


if __name__ == "__main__":
    unittest.main()
