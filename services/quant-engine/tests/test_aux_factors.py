"""辅助因子表（行业轮动 / 资金流向）测试。

修复背景：这两张表原先只由 scripts/ 下的脚本在服务外手工产出，实测落后信号日 2 天，
而 walkforward 会 LEFT JOIN 它们 → 特征静默缺失且无告警。现在：
  - 计算逻辑进 `quant_engine.stock_ml.aux_factors`，由 job 驱动（stock_industry_factors /
    stock_money_flow_factors），脚本退化为薄封装；
  - 重建改为**原子换表**（先写 staging，再在一个事务里 DROP+RENAME），避免半成品被 JOIN；
  - `aux_factor_freshness()` 在 walkforward 入口做新鲜度断言。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from quant_engine.stock_ml.aux_factors import (
    _swap_table,
    aux_factor_freshness,
    build_industry_factors,
    build_money_flow_factors,
)

CODES = ["000001.SZ", "600000.SH", "600519.SH"]


def _dates(count: int) -> list[str]:
    start = datetime(2026, 6, 1)
    return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(count)]


def _market(root: Path, days: int = 40) -> Path:
    """合成市场库：个股日线 + 行业映射 + 沪深300 + 涨跌停（表结构对齐真实库）。"""
    path = root / "market.db"
    dates = _dates(days)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE daily_bars(code TEXT, trade_date TEXT, open REAL, high REAL, low REAL, close REAL,
            pre_close REAL, change REAL, pct_chg REAL, volume REAL, amount REAL);
        CREATE TABLE security_master(code TEXT, name TEXT, industry TEXT, list_status TEXT);
        CREATE TABLE industry_membership_history(
            code TEXT, industry TEXT, valid_from TEXT, valid_to TEXT
        );
        CREATE TABLE index_daily_bars(ts_code TEXT, trade_date TEXT, close REAL, pct_chg REAL);
        CREATE TABLE limit_list(trade_date TEXT, code TEXT, industry TEXT, limit_type TEXT);
    """)
    for index, code in enumerate(CODES):
        price = 10.0 + index * 5
        for day, date in enumerate(dates):
            price = max(1.0, price * (1 + (0.005 if day % 3 else -0.004)))
            con.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (code, date, price * 0.99, price * 1.02, price * 0.98, price,
                         price * 0.995, 0.0, 0.5, 1e6 + day, 1e7 + index * 1e6))
        con.execute("INSERT INTO security_master VALUES(?,?,?,?)",
                    (code, f"测试{index}", "银行" if index == 0 else "白酒", "L"))
        con.execute("INSERT INTO industry_membership_history VALUES(?,?,?,NULL)",
                    (code, "银行" if index == 0 else "白酒", dates[0]))
    bench = 4000.0
    for day, date in enumerate(dates):
        bench *= 1.001
        con.execute("INSERT INTO index_daily_bars VALUES('000300.SH',?,?,?)", (date, bench, 0.1))
    con.execute("INSERT INTO limit_list VALUES(?,?,?,?)", (dates[-1], CODES[1], "银行", "U"))
    con.commit()
    con.close()
    return path


class SwapTableTest(unittest.TestCase):
    def test_replace_is_atomic_and_drops_staging(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        con = sqlite3.connect(Path(tmp.name) / "factors.db")
        con.execute("CREATE TABLE t(a INTEGER, b TEXT)")
        con.execute("INSERT INTO t VALUES(1,'old')")
        con.commit()
        import pandas as pd
        rows = _swap_table(con, "t", pd.DataFrame([{"a": 2, "b": "new"}, {"a": 3, "b": "new"}]))
        self.assertEqual(rows, 2)
        self.assertEqual(con.execute("SELECT b FROM t WHERE a=2").fetchone()[0], "new")
        self.assertIsNone(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name='t_staging'").fetchone(), "staging 表未清理")
        con.close()


class FreshnessTest(unittest.TestCase):
    def _factors(self, industry: str | None, money: str | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "factors.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE stock_ml_factors(trade_date TEXT, code TEXT)")
        con.execute("INSERT INTO stock_ml_factors VALUES('20260911','000001.SZ')")
        schemas = {
            "stock_industry_factors": (
                "trade_date TEXT, code TEXT, industry_mom5 REAL, industry_mom20 REAL, "
                "industry_rank5 REAL, industry_rank20 REAL, industry_excess5 REAL, "
                "industry_excess20 REAL, industry_limit_count REAL, industry_limit_ratio REAL, "
                "industry_amount_chg REAL, stock_vs_industry_mom20 REAL"),
            "stock_money_flow_factors": (
                "trade_date TEXT, code TEXT, money_flow_1d REAL, money_flow_5d REAL, "
                "money_flow_ratio_5d REAL, volume_price_div REAL, turnover_surge REAL, "
                "large_move_volume REAL, close_position REAL, close_position_5d REAL, "
                "up_volume_ratio REAL"),
        }
        for table, latest in (("stock_industry_factors", industry), ("stock_money_flow_factors", money)):
            if latest is None:
                continue
            con.execute(f"CREATE TABLE {table}({schemas[table]})")
            column_count = len(con.execute(f"PRAGMA table_info({table})").fetchall())
            con.execute(
                f"INSERT INTO {table} VALUES({','.join('?' * column_count)})",
                (latest, "000001.SZ", *([1.0] * (column_count - 2))))
        con.commit()
        con.close()
        return path

    def test_reports_lag_for_stale_tables(self):
        out = aux_factor_freshness(self._factors("20260909", "20260909"), None)
        self.assertEqual(out["signalDate"], "20260911")
        self.assertEqual(len(out["warnings"]), 2)
        self.assertTrue(all("落后信号日 20260911" in w for w in out["warnings"]))

    def test_no_warning_when_aligned(self):
        out = aux_factor_freshness(self._factors("20260911", "20260911"), None)
        self.assertEqual(out["warnings"], [])
        self.assertEqual(
            out["quality"]["stock_industry_factors"]["fields"]["industry_excess20"]["coverage"],
            1.0)

    def test_warns_when_latest_required_field_is_empty(self):
        path = self._factors("20260911", "20260911")
        con = sqlite3.connect(path)
        con.execute("UPDATE stock_industry_factors SET industry_excess20=NULL")
        con.commit()
        con.close()
        out = aux_factor_freshness(path, None, required_features=["industry_excess20"])
        self.assertEqual(
            out["quality"]["stock_industry_factors"]["fields"]["industry_excess20"]["coverage"],
            0.0)
        self.assertTrue(any("industry_excess20" in warning and "覆盖率仅" in warning
                            for warning in out["warnings"]))

    def test_warns_when_table_missing(self):
        out = aux_factor_freshness(self._factors(None, "20260911"), None)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("不存在", out["warnings"][0])


class BuilderSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.market = _market(self.root)
        self.factors = self.root / "factors.db"

    def test_money_flow_builder_writes_fresh_table(self):
        summary = build_money_flow_factors(self.market, self.factors)
        self.assertEqual(summary["table"], "stock_money_flow_factors")
        self.assertGreater(summary["rows"], 0)
        self.assertEqual(summary["endDate"], _dates(40)[-1])
        out = aux_factor_freshness(self.factors, None)
        # stock_ml_factors 不存在 → signal_date 为 None，但仍应报告两张表的状态
        self.assertIn("stock_money_flow_factors", out["tables"])
        self.assertEqual(out["tables"]["stock_money_flow_factors"], _dates(40)[-1])
        # 资金流因子的振幅改用真实 pre_close（此前回退到未分组的 close.shift(1)）
        con = sqlite3.connect(self.factors)
        columns = {r[1] for r in con.execute("PRAGMA table_info(stock_money_flow_factors)")}
        con.close()
        self.assertIn("large_move_volume", columns)

    def test_industry_builder_writes_table(self):
        summary = build_industry_factors(self.market, self.factors, start_date=_dates(40)[0])
        self.assertEqual(summary["table"], "stock_industry_factors")
        self.assertGreater(summary["rows"], 0)
        con = sqlite3.connect(self.factors)
        columns = {r[1] for r in con.execute("PRAGMA table_info(stock_industry_factors)")}
        con.close()
        self.assertIn("industry_mom20", columns)
        self.assertIn("stock_vs_industry_mom20", columns)

    def test_industry_builder_uses_point_in_time_membership(self):
        con = sqlite3.connect(self.market)
        dates = _dates(40)
        con.execute("UPDATE industry_membership_history SET valid_to=? WHERE code=?",
                    (dates[19], CODES[0]))
        con.execute("INSERT INTO industry_membership_history VALUES(?,?,?,NULL)",
                    (CODES[0], "白酒", dates[20]))
        con.commit()
        con.close()
        build_industry_factors(self.market, self.factors, start_date=dates[0])
        con = sqlite3.connect(self.factors)
        # 20日滚动预热后首个可用日，映射必须采用变更后的行业。
        row = con.execute(
            "SELECT code FROM stock_industry_factors WHERE code=? AND trade_date>=? LIMIT 1",
            (CODES[0], dates[20])).fetchone()
        con.close()
        self.assertIsNotNone(row)

    def test_industry_excess_uses_latest_known_benchmark_when_index_lags(self):
        con = sqlite3.connect(self.market)
        dates = _dates(40)
        con.execute("DELETE FROM index_daily_bars WHERE trade_date>?", (dates[-4],))
        con.commit()
        con.close()
        build_industry_factors(self.market, self.factors, start_date=dates[0])
        out = aux_factor_freshness(
            self.factors, dates[-1], required_features=["industry_excess5", "industry_excess20"])
        self.assertEqual(out["warnings"], [])
        q = out["quality"]["stock_industry_factors"]["fields"]
        self.assertEqual(q["industry_excess5"]["coverage"], 1.0)
        self.assertEqual(q["industry_excess20"]["coverage"], 1.0)

    def test_builders_honour_cancellation(self):
        with self.assertRaises(RuntimeError):
            build_money_flow_factors(self.market, self.factors, cancelled=lambda: True)


if __name__ == "__main__":
    unittest.main()
