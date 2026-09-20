"""stock_ml 标签契约测试。

修复背景：config/stock-ml.yaml 配的 label=forward_3，而服务只产出 forward_5/forward_20，
forward_3 由服务外脚本 compute_forward_3.py（已移入 scripts/research_archive/）直接 UPDATE 维护
→ 训练标签不可复现、
可能静默使用陈旧列。现在 forward_3 收回服务内产出，并在 walkforward 入口做契约校验。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine import stock_ml


class ForwardLabelTest(unittest.TestCase):
    def test_forward_labels_compute_each_horizon(self):
        rows = [{"close": c} for c in (10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0)]
        out = stock_ml._forward_labels(rows, 0, 10.0)
        self.assertAlmostEqual(out["forward_3"], 13.0 / 10.0 - 1)
        self.assertAlmostEqual(out["forward_5"], 15.0 / 10.0 - 1)
        self.assertIsNone(out["forward_20"], "未来窗口不足时必须为 None，不能造标签")

    def test_forward_labels_use_only_past_and_future_close(self):
        rows = [{"close": c} for c in (10.0, 10.0, 10.0, 20.0)]
        out = stock_ml._forward_labels(rows, 0, 10.0)
        self.assertAlmostEqual(out["forward_3"], 1.0)
        # 同一行只依赖 close[t] 与 close[t+N]：换掉其它行不改变结果
        rows2 = [{"close": c} for c in (10.0, 99.0, 99.0, 20.0)]
        self.assertEqual(stock_ml._forward_labels(rows2, 0, 10.0)["forward_3"], out["forward_3"])

    def test_forward_labels_guard_zero_close(self):
        rows = [{"close": 0.0}, {"close": 1.0}]
        self.assertEqual(set(stock_ml._forward_labels(rows, 0, 0.0).values()), {None})

    def test_forward_labels_support_adjusted_price_basis(self):
        rows = [{"close": 10.0, "adj": 10.0}, {"close": 9.0, "adj": 11.0},
                {"close": 8.0, "adj": 12.0}, {"close": 7.0, "adj": 13.0}]
        out = stock_ml._forward_labels(rows, 0, 10.0, "adj")
        self.assertAlmostEqual(out["forward_3"], 0.3)

    def test_schema_and_migrations_expose_all_label_columns(self):
        for col in stock_ml.LABEL_COLUMNS:
            self.assertIn(f"{col} REAL", stock_ml.SCHEMA)
            self.assertIn(col, stock_ml.FACTOR_MIGRATIONS, f"{col} 缺少存量表幂等迁移")


class LabelContractTest(unittest.TestCase):
    def _con(self) -> sqlite3.Connection:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        con = sqlite3.connect(Path(tmp.name) / "factors.db")
        self.addCleanup(con.close)  # LIFO：先关连接再删临时目录（Windows 上文件占用会导致清理失败）
        con.executescript(stock_ml.SCHEMA)
        return con

    def test_service_owned_labels_are_accepted(self):
        con = self._con()
        for col in stock_ml.LABEL_COLUMNS:
            stock_ml._assert_label_supported(con, col)  # 不应抛错

    def test_service_external_label_is_rejected(self):
        con = self._con()
        with self.assertRaises(ValueError) as ctx:
            stock_ml._assert_label_supported(con, "forward_7")
        self.assertIn("forward_7", str(ctx.exception))

    def test_missing_label_column_is_rejected(self):
        con = self._con()
        con.execute("DROP TABLE stock_ml_factors")
        con.execute("CREATE TABLE stock_ml_factors (trade_date TEXT, code TEXT)")
        with self.assertRaises(ValueError) as ctx:
            stock_ml._assert_label_supported(con, "forward_3")
        self.assertIn("缺少标签列", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
