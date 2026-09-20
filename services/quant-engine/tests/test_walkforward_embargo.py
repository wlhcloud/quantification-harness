"""purge/embargo 与标签口径审计测试。

背景：
  - walkforward 的 train/valid/test 按日期切分但无 purge/embargo，forward_20 标签在 t 日用到
    t+20 的价格，紧邻 test 起点的训练样本会带入测试期信息。
    处理方式（按用户决策）：新增可配置 walkforward.embargoDays，**默认 0 保持既有数值不变**，
    并把重叠天数写进结果元数据（embargoDays / labelHorizonDays / labelOverlapDays）。
  - forward_* 目前用未复权 close 计算（daily_bars 存原始价），除权日会失真。
    处理方式（按用户决策）：不改数值，提供只读的口径审计函数与端点。
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from quant_engine import stock_ml
from quant_engine.etf_quant import ranker


def _synthetic_frame(days: int = 140, codes: int = 4, label: str = "forward_20") -> list[dict]:
    """与 load_factor_frame 口径一致：标签不可得的尾部行根本不进入训练集。"""
    dates = [str(d) for d in range(20260101, 20260101 + days)]
    horizon = int(label.rsplit("_", 1)[1])
    frame = []
    for i in range(codes):
        for j, d in enumerate(dates):
            if j + horizon >= days:
                continue
            frame.append({"ts_code": f"C{i}", "trade_date": d,
                          # 特征随时间与标的都变化：否则预测恒定、截面秩相关无法计算（rankIc 为 None）
                          "mom20": float(i) + j * 0.001, "mom60": float(i) * 0.5 + j * 0.001,
                          "mom120": float(i) * 0.25 + j * 0.001,
                          "ma_alignment": 1.0, "vol_ratio": float(i) * 0.01, "amount_trend": 0.0,
                          "drawdown20": 0.0, "amount_share": float(i) / codes,
                          label: 0.01 * (i + 1)})
    return frame


class EmbargoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _train(self, embargo_days: int | None, validation_days: int = 40, test_days: int = 40) -> dict:
        frame = _synthetic_frame()
        cfg = {"nEstimators": 10, "learningRate": 0.1, "numLeaves": 7, "minDataInLeaf": 5,
               "validationDays": validation_days, "testDays": test_days, "randomState": 42,
               "labelGrades": 5,
               "featuresOverride": ["mom20", "mom60", "mom120", "ma_alignment", "vol_ratio",
                                    "amount_trend", "drawdown20", "amount_share"]}
        if embargo_days is not None:
            cfg["embargoDays"] = embargo_days
        return ranker.train_ranker(frame, "forward_20", cfg, self.dir / f"seed{embargo_days}")

    def test_label_horizon_parsing(self):
        self.assertEqual(ranker._label_horizon("forward_20"), 20)
        self.assertEqual(ranker._label_horizon("forward_5"), 5)
        self.assertEqual(ranker._label_horizon("close"), 0)

    def test_default_is_zero_and_metadata_reports_overlap(self):
        res = self._train(embargo_days=None)  # 不配置 -> 默认 0，行为与历史基线一致
        self.assertEqual(res["embargoDays"], 0)
        self.assertEqual(res["labelHorizonDays"], 20)
        self.assertEqual(res["labelOverlapDays"], 20, "未配置 embargo 时必须如实报告 20 日重叠")
        self.assertIsInstance(res["ic"], float)

        embargoed = self._train(embargo_days=20)
        self.assertEqual(embargoed["embargoDays"], 20)
        self.assertEqual(embargoed["labelOverlapDays"], 0)
        # train 与 valid 的尾部被剔除；test 窗口不受影响
        self.assertLess(embargoed["trainRows"], res["trainRows"])
        self.assertLess(embargoed["validRows"], res["validRows"])
        self.assertEqual(embargoed["testRows"], res["testRows"])
        self.assertGreater(embargoed["validRows"], 0)
        self.assertGreater(embargoed["testRows"], 0)
        self.assertIsInstance(embargoed["ic"], float)

    def test_embargo_larger_than_validation_window_fails_loudly(self):
        with self.assertRaises(ValueError) as ctx:
            self._train(embargo_days=20, validation_days=10)
        self.assertIn("embargoDays=20 过大", str(ctx.exception))

    def test_apply_embargo_blocks_only_tail_dates(self):
        rows = [{"trade_date": d} for d in ("20260101", "20260102", "20260103", "20260104")]
        dates = ["20260101", "20260102", "20260103", "20260104"]
        kept = ranker._apply_embargo(rows, "20260105", dates, 2)
        self.assertEqual([r["trade_date"] for r in kept], ["20260101", "20260102"])
        self.assertEqual(ranker._apply_embargo(rows, "20260105", dates, 0), rows)


class LabelBasisAuditTest(unittest.TestCase):
    """审计函数只读：构造"除权"场景，验证它能发现未复权标签的失真。"""

    def _env(self, adj_jump: bool) -> tuple[Path, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        factors, market = root / "factors.db", root / "market.db"

        raw_closes = [10.0, 10.0, 10.0, 10.0, 10.0, 5.0, 5.0, 5.0, 5.0, 5.0]
        factors_adj = [1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 2.0] if adj_jump else [1.0] * 10
        dates = [f"202601{d:02d}" for d in range(1, 11)]

        con = sqlite3.connect(factors)
        con.executescript(stock_ml.SCHEMA)
        # forward_5 用「未复权 close」计算，与线上 build_factors 口径一致
        for i, date in enumerate(dates):
            forward = (raw_closes[i + 5] / raw_closes[i] - 1) if i + 5 < len(raw_closes) else None
            con.execute(
                "INSERT INTO stock_ml_factors(trade_date, code, close, forward_3, forward_5, forward_20) "
                "VALUES(?,?,?,?,?,?)", (date, "000001.SZ", raw_closes[i], None, forward, None))
        con.commit()
        con.close()

        m = sqlite3.connect(market)
        m.executescript("""
            CREATE TABLE daily_bars(code TEXT, trade_date TEXT, open REAL, high REAL, low REAL,
                close REAL, pre_close REAL, change REAL, pct_chg REAL, volume REAL, amount REAL);
            CREATE TABLE adjustment_factors(code TEXT, trade_date TEXT, adj_factor REAL, source TEXT, updated_at TEXT);
        """)
        for i, date in enumerate(dates):
            m.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      ("000001.SZ", date, raw_closes[i], raw_closes[i], raw_closes[i], raw_closes[i],
                       raw_closes[i], 0, 0, 1, 1))
            m.execute("INSERT INTO adjustment_factors VALUES(?,?,?,?,?)",
                      ("000001.SZ", date, factors_adj[i], "test", "2026-01-01T00:00:00Z"))
        m.commit()
        m.close()
        return factors, market

    def test_constant_factor_reports_zero_difference(self):
        factors, market = self._env(adj_jump=False)
        out = stock_ml.audit_label_price_basis(factors, market, "forward_5")
        self.assertGreater(out["comparedRows"], 0)
        self.assertEqual(out["rowsOverThreshold"], 0)
        self.assertEqual(out["maxAbsDiff"], 0.0)
        self.assertFalse(out["abnormalMaxDiff"])

    def test_ex_dividend_step_is_detected(self):
        factors, market = self._env(adj_jump=True)
        out = stock_ml.audit_label_price_basis(factors, market, "forward_5")
        self.assertGreater(out["comparedRows"], 0)
        self.assertGreaterEqual(out["rowsOverThreshold"], 1, "除权造成的标签失真未被发现")
        self.assertGreater(out["maxAbsDiff"], 0.4)
        self.assertIn("close*adj_factor", out["note"])

    def test_rejects_label_not_produced_by_service(self):
        factors, market = self._env(adj_jump=False)
        with self.assertRaises(ValueError):
            stock_ml.audit_label_price_basis(factors, market, "forward_7")

    def test_audit_does_not_modify_data(self):
        factors, market = self._env(adj_jump=True)
        con = sqlite3.connect(factors)
        before = con.execute("SELECT trade_date, forward_5 FROM stock_ml_factors ORDER BY trade_date").fetchall()
        con.close()
        stock_ml.audit_label_price_basis(factors, market, "forward_5")
        con = sqlite3.connect(factors)
        after = con.execute("SELECT trade_date, forward_5 FROM stock_ml_factors ORDER BY trade_date").fetchall()
        con.close()
        self.assertEqual(before, after, "审计函数不得修改标签")


if __name__ == "__main__":
    unittest.main()
