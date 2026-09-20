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


class DailyCoverageTest(unittest.TestCase):
    """逐日覆盖度：复权因子缺口必须在界面上可见。

    真实事故（2026-09-20）：区间补拉只写日线、不写复权因子，于是 9/16-9/18
    复权因子为 0，但"日线齐了"在界面上和"数据可用"长得一样。下游 ML 标签
    用 close×adj_factor 复权，缺了它算出来的标签是错的。
    """

    def _sync(self, directory: str) -> DailySync:
        return DailySync(Path(directory) / "market.db", object(), object())

    def _seed(self, sync: DailySync, date: str, bars: int, basic: int, status: int, adj: int) -> None:
        sync.db.execute("INSERT OR REPLACE INTO trade_calendar VALUES(?,?,?,?)",
                        ("SSE", date, 1, None))
        for i in range(bars):
            sync.db.execute(
                "INSERT OR REPLACE INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (f"{i:06d}.SZ", date, 1, 1, 1, 1, 1, 0, 0, 0, 0))
        for i in range(basic):
            sync.db.execute("INSERT OR REPLACE INTO daily_basic VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (f"{i:06d}.SZ", date, 0, 0, 0, 0, 0, 0, 0, 0, 0))
        for i in range(status):
            sync.db.execute(
                "INSERT OR REPLACE INTO security_status_history VALUES(?,?,?,?,?,?,?,?,?)",
                (f"{i:06d}.SZ", date, "x", "y", "L", 0, 0, None, None))
        for i in range(adj):
            sync.db.execute("INSERT OR REPLACE INTO adjustment_factors VALUES(?,?,?,?,?)",
                            (f"{i:06d}.SZ", date, 1.0, "tushare", "now"))
        sync.db.commit()

    def test_fully_covered_day_is_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260918", 100, 100, 100, 100)
            result = sync.coverage(5)
            self.assertEqual(len(result["items"]), 1)
            item = result["items"][0]
            self.assertTrue(item["complete"])
            self.assertEqual(item["missing"], [])
            self.assertFalse(item["adjustmentMissing"])
            self.assertTrue(result["latestComplete"])
            sync.db.close()

    def test_missing_adjustment_is_flagged_even_when_bars_present(self):
        """这正是那次事故的形状：日线齐全、复权为 0。"""
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260918", 100, 100, 100, 0)
            result = sync.coverage(5)
            item = result["items"][0]
            self.assertFalse(item["complete"])
            self.assertEqual(item["missing"], ["adjustments"])
            self.assertTrue(item["adjustmentMissing"])
            self.assertEqual(result["adjustmentGaps"], ["20260918"])
            self.assertFalse(result["latestComplete"])
            sync.db.close()

    def test_partial_adjustment_below_95_percent_is_a_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260918", 100, 100, 100, 94)   # 94 < 95
            self.assertTrue(sync.coverage(5)["items"][0]["adjustmentMissing"])
            sync.db.close()
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260918", 100, 100, 100, 95)   # 恰好 95 视为通过
            self.assertFalse(sync.coverage(5)["items"][0]["adjustmentMissing"])
            sync.db.close()

    def test_day_with_no_bars_at_all_is_reported(self):
        """整日缺失比覆盖不足更严重，必须出现在结果里而不是被静默跳过。"""
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260917", 100, 100, 100, 100)
            sync.db.execute("INSERT OR REPLACE INTO trade_calendar VALUES(?,?,?,?)",
                            ("SSE", "20260918", 1, None))
            sync.db.commit()
            result = sync.coverage(5)
            dates = [i["tradeDate"] for i in result["items"]]
            self.assertEqual(dates, ["20260917", "20260918"])
            missing_day = [i for i in result["items"] if i["tradeDate"] == "20260918"][0]
            self.assertEqual(missing_day["bars"], 0)
            self.assertFalse(missing_day["complete"])
            self.assertIn("bars", missing_day["missing"])
            self.assertIn("20260918", result["incomplete"])
            sync.db.close()

    def test_closed_days_are_not_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            self._seed(sync, "20260918", 100, 100, 100, 100)
            sync.db.execute("INSERT OR REPLACE INTO trade_calendar VALUES(?,?,?,?)",
                            ("SSE", "20260919", 0, None))   # 周六
            sync.db.commit()
            self.assertEqual([i["tradeDate"] for i in sync.coverage(5)["items"]], ["20260918"])
            sync.db.close()

    def test_empty_database_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            result = sync.coverage(5)
            self.assertEqual(result["items"], [])
            self.assertIsNone(result["latest"])
            self.assertIsNone(result["latestComplete"])
            self.assertEqual(result["adjustmentGaps"], [])
            sync.db.close()

    def test_days_parameter_is_clamped(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = self._sync(directory)
            for i in range(5):
                self._seed(sync, f"2026091{i}", 10, 10, 10, 10)
            self.assertEqual(len(sync.coverage(2)["items"]), 2)
            self.assertEqual(len(sync.coverage(0)["items"]), 1)     # 下限 1
            self.assertEqual(len(sync.coverage(9999)["items"]), 5)  # 上限被夹到可用天数
            sync.db.close()


if __name__ == "__main__":
    unittest.main()
