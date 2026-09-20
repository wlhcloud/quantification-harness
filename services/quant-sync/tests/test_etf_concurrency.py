"""ETF 历史行情并发落库测试。

修复背景：sync_daily_history / sync_index_history 的 worker 在**共享连接**上直接
BEGIN/commit/rollback。多 worker 并发时会出现
  - "cannot start a transaction within a transaction"（失败的交易日被计入 gapDates）
  - 一个线程的 rollback 把另一个线程刚写入的数据一起回滚（静默丢数据）
修复：每个 worker 使用独立连接落库（open_wal 的 WAL + busy_timeout 保证单写者串行）。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from quant_sync.etf import EtfSync
from quant_sync.upstream import GatewayResult

DAYS = [f"202609{d:02d}" for d in range(1, 13)]   # 12 个交易日
CODES = [f"51030{i}.SH" for i in range(6)]        # 6 只 ETF
ROWS_PER_CALL = 60                                # 放大写事务时长，让并发窗口稳定出现


class FakeMeta:
    def register_dataset(self, *args, **kwargs):
        return None


class FakeClient:
    """trade_cal 给出交易日；fund_daily / index_daily 按 trade_date 返回当日行情。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, api, params, route=None, dataset=None, timeout_ms=None):
        self.calls.append((api, {"params": params, "timeout_ms": timeout_ms}))
        if api == "trade_cal":
            rows = [{"calendar_date": d, "cal_date": d, "is_open": 1, "exchange": "SSE"} for d in DAYS]
            return GatewayResult(source="fake", rows=rows, raw_rows=rows)
        day = str(params.get("trade_date") or "")
        if api == "fund_daily":
            time.sleep(0.02)  # 模拟网络延迟，确保多个 worker 的事务真正重叠
            rows = [{"ts_code": CODES[i % len(CODES)], "trade_date": day, "open": 4.0, "high": 4.1,
                     "low": 3.9, "close": 4.05, "pre_close": 4.0, "change": 0.05, "pct_chg": 1.25,
                     "vol": 1000.0, "amount": 4050.0} for i in range(ROWS_PER_CALL)]
            return GatewayResult(source="fake", rows=rows, raw_rows=rows)
        if api == "index_daily":
            time.sleep(0.02)
            rows = [{"ts_code": f"00090{i}.SH", "trade_date": day, "open": 4500.0, "high": 4550.0,
                     "low": 4480.0, "close": 4520.0, "pre_close": 4500.0, "change": 20.0,
                     "pct_chg": 0.44, "vol": 1.0, "amount": 2.0} for i in range(ROWS_PER_CALL)]
            return GatewayResult(source="fake", rows=rows, raw_rows=rows)
        return GatewayResult(source="fake", rows=[], raw_rows=[])


class ConcurrentHistoryWriteTest(unittest.TestCase):
    def _make(self) -> EtfSync:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        sync = EtfSync(Path(folder.name) / "market.db", FakeClient(), FakeMeta(), audit=None)
        self.addCleanup(sync.db.close)
        for code in CODES:
            sync.db.execute(
                "INSERT OR REPLACE INTO etf_metadata(ts_code,name,eligible,updated_at) VALUES(?,?,1,?)",
                (code, f"测试ETF{code}", "2026-09-01T00:00:00Z"))
        sync.db.commit()
        return sync

    def test_timeout_ms_is_forwarded_through_history(self):
        sync = self._make()
        sync.sync_daily_history(DAYS[0], DAYS[-1], len(DAYS), concurrency=2, timeout_ms=3300)
        forwarded = [kwargs["timeout_ms"] for _, kwargs in sync.client.calls]
        self.assertTrue(forwarded, "未发生任何上游调用")
        self.assertTrue(all(value == 3300 for value in forwarded),
                        f"ETF 历史同步未按调用传递超时：{set(forwarded)}")

    def test_etf_daily_history_writes_every_day_under_concurrency(self):
        sync = self._make()
        sync.sync_daily_history(DAYS[0], DAYS[-1], len(DAYS), concurrency=4)

        self.assertEqual(sync.daily["status"], "complete", sync.daily.get("error"))
        self.assertEqual(sync.daily["failedDays"], 0, f"并发写失败：{sync.daily['gapDates']}")
        self.assertEqual(sync.daily["gapDates"], [])
        days = {r[0] for r in sync.db.execute("SELECT DISTINCT trade_date FROM etf_daily_bars")}
        self.assertEqual(days, set(DAYS), "并发落库丢数据：部分交易日没有写入")
        # 每日按 ts_code 去重后应覆盖全部 CODES
        codes = {r[0] for r in sync.db.execute("SELECT DISTINCT ts_code FROM etf_daily_bars")}
        self.assertEqual(codes, set(CODES))

    def test_index_daily_history_writes_every_day_under_concurrency(self):
        sync = self._make()
        sync.sync_index_history(DAYS[0], DAYS[-1], len(DAYS), concurrency=4)

        self.assertEqual(sync.index["status"], "complete", sync.index.get("error"))
        self.assertEqual(sync.index["failedDays"], 0, f"并发写失败：{sync.index['gapDates']}")
        days = {r[0] for r in sync.db.execute("SELECT DISTINCT trade_date FROM index_daily_bars")}
        self.assertEqual(days, set(DAYS), "并发落库丢数据：部分交易日没有写入")


if __name__ == "__main__":
    unittest.main()
