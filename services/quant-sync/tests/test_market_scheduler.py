import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_sync.market_scheduler import MarketScheduler


class FakeTools:
    def __init__(self):
        self.states = {name: {"status": "idle"} for name in ("idx_mins", "rt_idx_k", "rt_idx_min")}
        self.started = []

    def start(self, tool_id, params, trigger_source="sync-service"):
        self.started.append((tool_id, params, trigger_source))


class MarketSchedulerTest(unittest.TestCase):
    def test_status_is_transparent_and_configured(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "market.db"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE trade_calendar(exchange TEXT,calendar_date TEXT,is_open INTEGER)")
            db.commit(); db.close()
            scheduler = MarketScheduler(FakeTools(), path, lambda: {"marketScheduler": {
                "enabled": True, "intervalSeconds": 60, "catchupEnabled": True}})
            state = scheduler.snapshot()
            self.assertTrue(state["enabled"])
            self.assertEqual(state["timezone"], "Asia/Shanghai")
            self.assertEqual(state["sessions"], ["09:30-11:30", "13:00-15:00"])

    def test_safe_start_marks_scheduler_trigger(self):
        tools = FakeTools()
        scheduler = MarketScheduler(tools, Path("unused.db"), lambda: {})
        self.assertTrue(scheduler._safe_start("rt_idx_min", {"freq": "1MIN"}))
        self.assertEqual(tools.started[0][2], "market-scheduler")


if __name__ == "__main__":
    unittest.main()
