"""分钟同步计数与超时传参测试。

修复背景：
  1. 失败项同时 +1 failed 和 +1 completed，导致 completed + failed > total，
     UI 出现"8/8 完成 + 8 失败"这类自相矛盾的进度；现在只计 failed。
  2. 分钟同步的上游超时覆盖原先写 TushareClient 的全局 _run_timeout，改为 MinuteSync.timeout_ms
     并沿调用链显式传参（分分钟启动由 _start_locks["minute"] 串行化，属性安全）。
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from quant_sync import minute as minute_module
from quant_sync.minute import MinuteSync
from quant_sync.upstream import PendingError, UpstreamError


class FakeMeta:
    def __init__(self):
        self.registered = []

    def register_dataset(self, *_args, **_kwargs):
        return None


class RecordingClient:
    """记录每次调用的 kwargs，并按配置决定 stk_mins 的行为。"""

    codes = [f"{n:06d}.SZ" for n in range(1, 5)]

    def __init__(self, stk_mins_error: Exception | None = None):
        self.stk_mins_error = stk_mins_error
        self.calls: list[tuple[str, dict]] = []

    def call(self, api, params, *_args, **kwargs):
        self.calls.append((api, kwargs))
        if api == "stock_basic":
            rows = [{"ts_code": code, "list_date": "20200101"} for code in self.codes]
        elif api == "trade_cal":
            rows = [{"cal_date": "20260105", "is_open": 1}]
        elif api == "stk_mins":
            if self.stk_mins_error is not None:
                raise self.stk_mins_error
            points = []
            current = datetime(2026, 1, 5, 9, 30)
            while current <= datetime(2026, 1, 5, 15, 0):
                if current.hour != 12 and not (current.hour == 13 and current.minute == 0):
                    points.append({"code": params["ts_code"],
                                   "trade_time": current.strftime("%Y-%m-%d %H:%M:%S"),
                                   "open": 10, "high": 10.1, "low": 9.9, "close": 10,
                                   "volume": 100, "amount": 1000})
                current += timedelta(minutes=5)
            rows = points
        else:
            raise AssertionError(api)
        return SimpleNamespace(rows=rows, source="fake")


class MinuteCounterTest(unittest.TestCase):
    def setUp(self):
        # pending 轮次之间有 time.sleep(30)、段重试有 backoff：测试里全部屏蔽，否则单个用例要跑 1 分钟
        patcher = mock.patch.object(minute_module.time_module, "sleep", lambda *_args, **_kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _sync(self, error: Exception | None) -> tuple[MinuteSync, RecordingClient]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        client = RecordingClient(error)
        sync = MinuteSync(Path(directory.name) / "minute.db", client, FakeMeta(), concurrency=4)
        self.addCleanup(sync.db.close)
        return sync, client

    def test_success_counts_completed_only(self):
        sync, _ = self._sync(None)
        sync.sync_all("20260105", "20260105", "5m", 0)
        self.assertEqual(sync.state["status"], "complete")
        self.assertEqual(sync.state["total"], len(RecordingClient.codes))
        self.assertEqual(sync.state["completed"], len(RecordingClient.codes))
        self.assertEqual(sync.state["failed"], 0)

    def test_pending_exhausted_counts_failed_not_completed(self):
        sync, _ = self._sync(PendingError("UPSTREAM_PENDING:5"))
        sync.sync_all("20260105", "20260105", "5m", 0)
        self.assertEqual(sync.state["status"], "error")
        self.assertEqual(sync.state["failed"], len(RecordingClient.codes))
        self.assertEqual(sync.state["completed"], 0, "失败不应同时计入 completed")
        self.assertLessEqual(sync.state["completed"] + sync.state["failed"], sync.state["total"])

    def test_generic_error_counts_failed_not_completed(self):
        sync, _ = self._sync(UpstreamError("boom"))
        sync.sync_all("20260105", "20260105", "5m", 0)
        self.assertEqual(sync.state["failed"], len(RecordingClient.codes))
        self.assertEqual(sync.state["completed"], 0)
        self.assertLessEqual(sync.state["completed"] + sync.state["failed"], sync.state["total"])

    def test_timeout_ms_is_forwarded_to_client_calls(self):
        sync, client = self._sync(None)
        sync.timeout_ms = 4200
        sync.sync_all("20260105", "20260105", "5m", 0)
        forwarded = [kwargs.get("timeout_ms") for _, kwargs in client.calls]
        self.assertTrue(forwarded)
        self.assertTrue(all(value == 4200 for value in forwarded),
                        f"未按运行传递超时：{forwarded}")


if __name__ == "__main__":
    unittest.main()
