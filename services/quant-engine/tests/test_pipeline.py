import math
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from quant_engine.pipeline import build_features, run_inference, train_model


class PipelineTest(unittest.TestCase):
    def test_feature_train_inference_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            market = root / "market.db"
            db = sqlite3.connect(market)
            db.executescript("""
                CREATE TABLE daily_bars(code TEXT,trade_date TEXT,open REAL,close REAL,pct_chg REAL,volume REAL);
                CREATE TABLE daily_basic(code TEXT,trade_date TEXT,turnover_rate REAL,pe_ttm REAL,pb REAL);
            """)
            for symbol_number, code in enumerate(("000001.SZ", "600000.SH", "830001.BJ")):
                price = 10.0 + symbol_number
                for day_number in range(130):
                    trade_date = (date(2025, 1, 1) + timedelta(days=day_number)).strftime("%Y%m%d")
                    change = 0.002 * math.sin(day_number / 5 + symbol_number) + 0.0005 * symbol_number
                    open_price = price
                    price *= 1 + change
                    db.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?)",
                               (code, trade_date, open_price, price, change * 100, 1000000))
                    db.execute("INSERT INTO daily_basic VALUES(?,?,?,?,?)",
                               (code, trade_date, 2 + symbol_number, 10 + symbol_number, 1 + symbol_number / 10))
            db.commit()
            db.close()
            progress = lambda _value: None
            active = lambda: False
            features = build_features(market, root / "features", {}, progress, active)
            self.assertGreater(features["rows"], 100)
            trained = train_model(root / "features", root / "artifacts",
                                  {"validationDays": 20, "testDays": 20, "nEstimators": 20}, progress, active)
            self.assertGreater(trained["trainRows"], 0)
            inferred = run_inference(root / "features", root / "artifacts", {"topN": 2}, progress, active)
            self.assertEqual(len(inferred["items"]), 2)


if __name__ == "__main__":
    unittest.main()
