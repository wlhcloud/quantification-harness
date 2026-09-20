import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine.data_gate import check_backtest_readiness


class BacktestDataGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.market = root / "market.db"
        self.finance = root / "finance.db"
        self.minute = root / "minute.db"
        db = sqlite3.connect(self.market)
        try:
            db.executescript("""
                CREATE TABLE daily_bars(code TEXT, trade_date TEXT);
                CREATE TABLE security_status_history(code TEXT, trade_date TEXT);
            """)
            dates = ["20260102", "20260105", "20260106", "20260107", "20260108"]
            db.executemany("INSERT INTO daily_bars VALUES('000001.SZ',?)", ((date,) for date in dates))
            db.executemany("INSERT INTO security_status_history VALUES('000001.SZ',?)", ((date,) for date in dates))
            db.commit()
        finally:
            db.close()
        db = sqlite3.connect(self.finance)
        try:
            db.execute("CREATE TABLE financial_indicator(code TEXT)")
            db.commit()
        finally:
            db.close()
        db = sqlite3.connect(self.minute)
        try:
            db.executescript("""
                CREATE TABLE minute_bars(code TEXT, trade_time TEXT, freq TEXT);
                CREATE TABLE minute_sync_days(code TEXT, freq TEXT, trade_date TEXT, status TEXT);
                INSERT INTO minute_bars VALUES('000001.SZ','2026-01-05 09:35:00','5m');
            """)
            db.commit()
        finally:
            db.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _insert_days(self, dates: list[str]) -> None:
        db = sqlite3.connect(self.minute)
        try:
            db.executemany(
                "INSERT INTO minute_sync_days VALUES(?, '5m', ?, 'complete')",
                ((f"{index:06d}.SZ", date) for date in dates for index in range(20)),
            )
            db.commit()
        finally:
            db.close()

    def test_rejects_whole_trading_days_missing_from_ledger(self) -> None:
        self._insert_days(["20260106", "20260107"])
        result = check_backtest_readiness("short", self.market, self.finance, self.minute,
                                          "20260102", "20260106")
        self.assertFalse(result["allowed"])
        self.assertIn("5 分钟交易日覆盖", result["blockedReasons"])

    def test_accepts_when_every_required_execution_day_is_tracked(self) -> None:
        self._insert_days(["20260105", "20260106", "20260107", "20260108"])
        result = check_backtest_readiness("short", self.market, self.finance, self.minute,
                                          "20260102", "20260106")
        self.assertTrue(result["allowed"])


if __name__ == "__main__":
    unittest.main()
