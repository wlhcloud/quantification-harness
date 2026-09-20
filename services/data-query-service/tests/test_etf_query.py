import sqlite3
import tempfile
import unittest
from pathlib import Path

from data_query.repository import ReadOnlyRepository


def seed_market(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE etf_metadata (ts_code TEXT PRIMARY KEY, name TEXT, management TEXT,
          custodian TEXT, fund_type TEXT, invest_type TEXT, benchmark TEXT, list_date TEXT,
          found_date TEXT, issue_date TEXT, delist_date TEXT, m_fee REAL, c_fee REAL,
          p_value REAL, eligible INTEGER, updated_at TEXT);
        CREATE TABLE etf_daily_bars (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
          close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT);
        CREATE TABLE index_daily_bars (ts_code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
          close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL, source TEXT);
        INSERT INTO etf_metadata VALUES ('510300.SH','沪深300ETF','华泰柏瑞基金','建设银行',
          '股票型','被动指数型','沪深300','20120528','20120504','20120426',NULL,0.5,0.1,1.0,1,'2026-09-07');
        INSERT INTO etf_metadata VALUES ('150234.SZ','分级基金B','某基金','某行',
          '股票型','分级','指数','20150610',NULL,NULL,NULL,NULL,NULL,NULL,0,'2026-09-07');
        INSERT INTO etf_daily_bars VALUES ('510300.SH','20260904',4.637,4.672,4.599,4.616,4.621,-0.005,-0.11,8414655.43,3905672.999,'fake');
        INSERT INTO etf_daily_bars VALUES ('510300.SH','20260903',4.62,4.65,4.6,4.637,4.6,0.037,0.8,9000000.0,4100000.0,'fake');
        INSERT INTO index_daily_bars VALUES ('000300.SH','20260904',4575.3,4580.2,4540.1,4548.05,4552.58,-4.53,-0.0995,1.0,2.0,'fake');
    """)
    con.commit()
    con.close()


class EtfQueryTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        seed_market(Path(folder.name) / "market.db")
        self.repo = ReadOnlyRepository(Path(folder.name), 1500)

    def test_universe_filters_by_query_and_eligible(self):
        rows = self.repo.etf_universe("300", False, 20)
        self.assertEqual([r["tsCode"] for r in rows], ["510300.SH"])
        rows = self.repo.etf_universe("", True, 20)
        self.assertEqual(len(rows), 1)  # only eligible=1
        self.assertEqual(rows[0]["name"], "沪深300ETF")
        self.assertEqual(rows[0]["eligible"], 1)

    def test_etf_bars_descending_with_date_window(self):
        rows = self.repo.etf_bars("510300.SH", "", "", 20)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tradeDate"], "20260904")  # DESC order
        rows = self.repo.etf_bars("510300.SH", "20260901", "20260903", 20)
        self.assertEqual([r["tradeDate"] for r in rows], ["20260903"])

    def test_index_bars(self):
        rows = self.repo.index_bars("000300.SH", "", "", 20)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["close"], 4548.05)

    def test_missing_tables_return_empty(self):
        # a market.db without ETF tables -> graceful empty
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        con = sqlite3.connect(Path(folder.name) / "market.db")
        con.execute("CREATE TABLE daily_bars (x TEXT)")
        con.commit()
        con.close()
        repo = ReadOnlyRepository(Path(folder.name), 1500)
        self.assertEqual(repo.etf_universe("", True, 10), [])
        self.assertEqual(repo.etf_bars("510300.SH", "", "", 10), [])


if __name__ == "__main__":
    unittest.main()
