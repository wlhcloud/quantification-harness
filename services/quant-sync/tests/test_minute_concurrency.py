import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from quant_sync.minute import MinuteSync


class FakeMeta:
    def register_dataset(self, *_args, **_kwargs):
        return None


class FakeClient:
    codes = [f"{number:06d}.SZ" for number in range(1, 9)]

    def call(self, api, params, *_args, **_kwargs):
        if api == "stock_basic":
            rows = [{"ts_code": code, "list_date": "20200101"} for code in self.codes]
        elif api == "trade_cal":
            rows = [{"cal_date": "20260105", "is_open": 1}]
        elif api == "stk_mins":
            points = []
            current = datetime(2026, 1, 5, 9, 30)
            while current <= datetime(2026, 1, 5, 15, 0):
                if (current.hour == 11 and current.minute <= 30) or current.hour < 11 or current.hour >= 13:
                    if current.hour != 12 and not (current.hour == 13 and current.minute == 0):
                        points.append({"code": params["ts_code"], "trade_time": current.strftime("%Y-%m-%d %H:%M:%S"),
                                       "open": 10, "high": 10.1, "low": 9.9, "close": 10, "volume": 100, "amount": 1000})
                current += timedelta(minutes=5)
            rows = points
        else:
            raise AssertionError(api)
        return SimpleNamespace(rows=rows, source="fake")


class MinuteConcurrencyTest(unittest.TestCase):
    def test_parallel_workers_serialize_sqlite_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            sync = MinuteSync(Path(directory) / "minute.db", FakeClient(), FakeMeta(), concurrency=4)
            sync.sync_all("20260105", "20260105", "5m", 0)
            self.assertEqual(sync.state["status"], "complete")
            self.assertEqual(sync.state["completed"], len(FakeClient.codes))
            self.assertEqual(sync.state["failed"], 0)
            sync.db.close()


if __name__ == "__main__":
    unittest.main()
