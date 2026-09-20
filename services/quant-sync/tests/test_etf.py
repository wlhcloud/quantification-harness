import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_sync.etf import EtfSync
from quant_sync.upstream import GatewayResult


class FakeMeta:
    def __init__(self):
        self.registered = []

    def register_dataset(self, *args, **kwargs):
        self.registered.append((args, kwargs))


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def call(self, api, params, route=None, dataset=None, timeout_ms=None):
        self.calls.append((api, params))
        rows = self.responses.get(api, [])
        return GatewayResult(source="fake", rows=rows, raw_rows=rows)


def make_universe_rows():
    return [
        {"ts_code": "510300.SH", "name": "华泰柏瑞沪深300ETF", "fund_type": "股票型",
         "list_date": 20120528.0, "delist_date": None, "management": "华泰柏瑞基金",
         "custodian": "建设银行", "invest_type": "被动指数型", "benchmark": "沪深300"},
        {"ts_code": "518880.SH", "name": "华安黄金易ETF", "fund_type": "股票型",
         "list_date": 20130729.0, "delist_date": None},
        {"ts_code": "511010.SH", "name": "国债ETF", "fund_type": "债券型",
         "list_date": 20130225.0, "delist_date": None},
        {"ts_code": "150234.SZ", "name": "申万传媒指数分级-B", "fund_type": "股票型",
         "list_date": 20150610.0, "delist_date": None},  # name has no ETF
        {"ts_code": "999999.SH", "name": "已退市ETF", "fund_type": "股票型",
         "list_date": 20100101.0, "delist_date": 20200101.0},  # delisted
        {"ts_code": "511880.SH", "name": "银华日利ETF", "fund_type": "货币型",
         "list_date": 20140101.0, "delist_date": None},  # money market excluded
    ]


class EtfSyncTest(unittest.TestCase):
    def _make(self, responses=None, db_name="market.db"):
        folder = tempfile.TemporaryDirectory()
        client = FakeClient(responses or {"fund_basic": make_universe_rows()})
        meta = FakeMeta()
        sync = EtfSync(Path(folder.name) / db_name, client, meta, audit=None)
        self.addCleanup(folder.cleanup)
        self.addCleanup(sync.db.close)
        return sync, client, meta

    def test_universe_upserts_and_marks_eligibility(self):
        sync, client, meta = self._make()
        state = sync.sync_universe()
        self.assertEqual(state["status"], "complete")
        rows = sync.db.execute("SELECT * FROM etf_metadata ORDER BY ts_code").fetchall()
        self.assertEqual(len(rows), 6)
        eligible = {r["ts_code"] for r in rows if r["eligible"] == 1}
        self.assertIn("510300.SH", eligible)
        self.assertIn("518880.SH", eligible)
        self.assertIn("511010.SH", eligible)          # bond ETF kept
        self.assertNotIn("150234.SZ", eligible)       # no ETF in name -> excluded
        self.assertNotIn("999999.SH", eligible)       # delisted -> excluded
        self.assertNotIn("511880.SH", eligible)       # money market -> excluded
        self.assertEqual(state["eligible"], 3)
        self.assertTrue(any("etf.universe" in str(a) for a, _ in meta.registered))

    def test_daily_requires_universe_first(self):
        sync, client, meta = self._make(responses={})
        with self.assertRaises(Exception) as ctx:
            sync.sync_daily("20260904")
        self.assertIn("ETF 池", str(ctx.exception))

    def test_daily_writes_only_universe_codes(self):
        sync, client, meta = self._make()
        sync.sync_universe()
        client.responses["fund_daily"] = [
            {"ts_code": "510300.SH", "trade_date": "20260904", "open": 4.6, "high": 4.7,
             "low": 4.59, "close": 4.68, "pre_close": 4.6, "change": 0.08, "pct_chg": 1.74,
             "vol": 1000.0, "amount": 4680.0},
            {"ts_code": "150234.SZ", "trade_date": "20260904", "close": 1.0},  # not in universe
        ]
        state = sync.sync_daily("20260904")
        self.assertEqual(state["status"], "complete")
        bars = sync.db.execute("SELECT * FROM etf_daily_bars").fetchall()
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["ts_code"], "510300.SH")
        self.assertEqual(bars[0]["close"], 4.68)

    def test_index_daily_writes_all_rows(self):
        sync, client, meta = self._make(responses={})
        client.responses["index_daily"] = [
            {"ts_code": "000300.SH", "trade_date": "20260904", "open": 4575.3, "close": 4548.05,
             "pct_chg": -0.1, "vol": 1.0, "amount": 2.0},
        ]
        state = sync.sync_index_daily("20260904")
        self.assertEqual(state["status"], "complete")
        rows = sync.db.execute("SELECT * FROM index_daily_bars").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts_code"], "000300.SH")

    def test_history_no_pending_marks_complete(self):
        sync, client, meta = self._make()
        sync.sync_universe()
        # no trade_cal response -> falls back to empty local calendar -> zero days
        sync.sync_daily_history("20260901", "20260906", 5, concurrency=2)
        self.assertEqual(sync.daily["status"], "complete")

    def test_trading_days_accepts_raw_cal_date_field(self):
        sync, client, meta = self._make()
        client.responses["trade_cal"] = [
            {"cal_date": "20260901", "is_open": 1, "exchange": "SSE"},
            {"cal_date": "20260902", "is_open": 1, "exchange": "SSE"},
            {"cal_date": "20260903", "is_open": 0, "exchange": "SSE"},
            {"cal_date": "20260904", "is_open": 1, "exchange": "SSE"},
        ]
        days = sync._trading_days("20260901", "20260904", 2)
        self.assertEqual(days, ["20260902", "20260904"])
        self.assertEqual(client.calls[-1][0], "trade_cal")


if __name__ == "__main__":
    unittest.main()
