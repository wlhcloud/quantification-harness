from __future__ import annotations

import unittest

import numpy as np

from quant_engine.stock_ml import _run_window_backtest
from quant_engine.stock_ml.backtest import _add_technical_timing_signals, _can_trade
from quant_engine.stock_ml.common import _validate_execution_config


class _Model:
    def feature_name(self):
        return ["momentum20"]

    def predict(self, x):
        return np.asarray(x[:, 0], dtype=float)


class SameDayCloseBacktestTest(unittest.TestCase):
    def test_pit_technical_cross_signals(self):
        dates = [f"202608{i:02d}" for i in range(1, 22)]
        bars = {
            "G": {d: {"close": c, "volume": 100.0} for d, c in zip(dates, [10.0] * 10 + [9.0] * 10 + [14.0])},
            "D": {d: {"close": c, "volume": 100.0} for d, c in zip(dates, [9.0] * 10 + [10.0] * 10 + [5.0])},
        }
        _add_technical_timing_signals(bars)
        self.assertTrue(bars["G"][dates[-1]]["golden_cross"])
        self.assertTrue(bars["D"][dates[-1]]["death_cross"])

    def test_technical_timing_waits_for_signal_before_buying(self):
        dates = [f"202608{i:02d}" for i in range(1, 22)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(dates, closes)}}
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in dates[-2:]]
        daily = _run_window_backtest(
            frame, _Model(), dates[-2:], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0,
             "technicalTiming": {"enabled": True, "candidateTopN": 20,
                                  "goldenCross": True, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True}},
            lambda _: None, 0, 100,
        )
        self.assertEqual(daily[0]["trades"], [])
        buy = next(t for t in daily[1]["trades"] if t["side"] == "buy")
        self.assertEqual(buy["reason"], "golden_cross")

    def test_recent_golden_cross_can_enter_within_configured_lookback(self):
        dates = [f"202608{i:02d}" for i in range(1, 23)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0, 14.0]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(dates, closes)}}
        frame = [{"trade_date": dates[-1], "code": "A", "momentum20": 2.0, "mktCap": 3e9}]
        daily = _run_window_backtest(
            frame, _Model(), [dates[-1]], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "goldenCrossLookbackDays": 3, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True}},
            lambda _: None, 0, 100,
        )
        buy = next(t for t in daily[-1]["trades"] if t["side"] == "buy")
        self.assertEqual(buy["reason"], "golden_cross")

    def test_death_cross_requires_below_ma20_when_configured(self):
        dates = [f"202609{i:02d}" for i in range(1, 23)]
        bars = {"A": {d: {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d in dates}}
        # Direct signal fixtures isolate the exit rule from moving-average construction.
        marker = "_timing_2_20"
        bars["A"][dates[-2]].update({marker: True, "golden_cross": True,
                                      "golden_cross_age": 0, "ma20": 9.0})
        bars["A"][dates[-1]].update({marker: True, "death_cross": True,
                                      "golden_cross_age": 1, "ma20": 9.0})
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in dates[-2:]]
        daily = _run_window_backtest(
            frame, _Model(), dates[-2:], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "goldenCrossLookbackDays": 3, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True,
                                  "deathCrossRequireBelowMa20": True}},
            lambda _: None, 0, 100,
        )
        self.assertFalse(any(t["side"] == "sell" for t in daily[-1]["trades"]))

    def test_technical_timing_death_cross_exits_without_same_day_rebuy(self):
        dates = [f"202609{i:02d}" for i in range(1, 24)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0, 5.0, 5.0]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(dates, closes)}}
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in dates[-4:]]
        daily = _run_window_backtest(
            frame, _Model(), dates[-4:], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "volumeBreakout": False, "requireAboveMa20": True,
                                  "deathCross": True}},
            lambda _: None, 0, 100,
        )
        sells = [t for row in daily for t in row["trades"] if t["side"] == "sell"]
        self.assertEqual(sells[-1]["reason"], "death_cross")
        self.assertFalse(any(t["side"] == "buy" for t in daily[-1]["trades"]))

    def test_trades_and_values_on_signal_day_close(self):
        frame = [
            {"trade_date": "20260901", "code": "A", "momentum20": 2.0, "mktCap": 3e9},
            {"trade_date": "20260901", "code": "B", "momentum20": 1.0, "mktCap": 3e9},
        ]
        bars = {
            "A": {
                "20260901": {"open": 9.0, "close": 10.0},
                # 若实现错误地读取下一日开盘，结果会显著不同。
                "20260902": {"open": 100.0, "close": 100.0},
            },
            "B": {"20260901": {"open": 20.0, "close": 20.0}},
        }
        daily = _run_window_backtest(
            frame, _Model(), ["20260901"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40",
             "topN": 1, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0},
            lambda _: None, 0, 100,
        )
        self.assertEqual(daily[0]["tradeDate"], "20260901")
        self.assertEqual(daily[0]["executionMode"], "same_day_close")
        self.assertEqual(daily[0]["signalTime"], "14:40")
        self.assertAlmostEqual(daily[0]["nav"], 1.0)

    def test_single_signal_respects_max_position_weight(self):
        frame = [{"trade_date": "20260901", "code": "A", "momentum20": 2.0, "mktCap": 3e9}]
        bars = {"A": {"20260901": {"open": 10.0, "high": 10.0, "low": 10.0,
                                      "close": 10.0, "volume": 100.0}}}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 5,
             "maxPositionWeight": 0.20, "initialCapital": 100_000,
             "commissionRate": 0.0, "stampDutyRate": 0.0, "slippageRate": 0.0},
            lambda _: None, 0, 100,
        )
        buy = next(t for t in daily[0]["trades"] if t["side"] == "buy")
        self.assertAlmostEqual(buy["notional"], 20_000.0)
        self.assertAlmostEqual(daily[0]["nav"], 1.0)

    def test_stop_blocked_by_limit_down_is_deferred_not_dropped(self):
        """止损触发日封跌停卖不掉时，卖出必须顺延重试，不能静默丢弃。

        回归背景：原实现在卖出循环里 `if px is None or not _can_trade(bar, "sell"): continue`，
        直接丢掉这一次卖出。仓位既不在 target 里、也不会再被卖出，只会在窗口末被动清零：
        账实不符（持仓凭空消失），且换手/止损统计失真。
        """
        # 20 日横盘后跳升：最后一日形成金叉并可成交（沿用仓库既有夹具模式）。
        hist = [f"202608{i:02d}" for i in range(1, 22)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0]
        test_days = [hist[-1], "20260901", "20260902", "20260903"]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(hist, closes)}}
        bars["A"][hist[-1]] = {"open": 14.0, "high": 14.0, "low": 10.1, "close": 14.0,
                               "volume": 100.0, "pct_chg": 0.0}
        # 09-01：盘中触及 9.5 止损（14.0×0.95=13.3），但收盘封跌停 -> 当日不可卖
        bars["A"]["20260901"] = {"open": 13.0, "high": 13.0, "low": 12.0, "close": 12.6,
                                 "volume": 100.0, "pct_chg": -10.0, "limit_down": 12.6}
        # 09-02：仍封跌停，继续顺延
        bars["A"]["20260902"] = {"open": 11.4, "high": 11.4, "low": 11.34, "close": 11.34,
                                 "volume": 100.0, "pct_chg": -10.0, "limit_down": 11.34}
        # 09-03：跌停打开，可以成交
        bars["A"]["20260903"] = {"open": 11.0, "high": 11.2, "low": 10.8, "close": 11.0,
                                 "volume": 100.0, "pct_chg": -3.0, "limit_down": 10.2}
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in (hist[-1], "20260901")]
        daily = _run_window_backtest(
            frame, _Model(), test_days, bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "rebalanceDays": 10,
             "initialStopLoss": 0.05, "trailingDrawdown": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "goldenCrossLookbackDays": 1, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True}},
            lambda _: None, 0, 100,
        )
        buys = [t for row in daily for t in row["trades"] if t["side"] == "buy"]
        self.assertEqual(len(buys), 1, "应先建仓一次")
        all_sells = [t for row in daily for t in row["trades"] if t["side"] == "sell"]
        self.assertEqual(len(all_sells), 1, "止损只应卖一次（顺延成交），不能重复或丢失")
        sell = all_sells[0]
        self.assertEqual(sell["tradeDate"], "20260903", "封跌停当日不可成交，应顺延到 09-03")
        # 顺延成交必须按成交日口径记账，不能用触发日的陈旧价格：
        # 09-01 尾盘以 14.0 建仓，同在 09-01 触发跳空止损（开 13.0 < 13.3）但封跌停卖不掉；
        # 09-02 仍封跌停；09-03 实际以当日收盘价 11.0 卖出。
        self.assertEqual(sell["reason"], "initial_stop_gap")
        self.assertAlmostEqual(sell["price"], 11.0, msg="顺延后按成交日收盘价记账")
        # 止损在建仓当日（09-01 尾盘买入即触发跳空止损，日内最低 12.0 < 13.3）就成立，
        # 但当日封跌停无法成交，故顺延到 09-03，间隔 2 个交易日。
        self.assertEqual(sell["deferredDays"], 2, "09-01 触发、09-03 成交，间隔 2 个交易日")
        # 挂账期间仓位必须仍然在账上，不能被静默清零
        self.assertEqual(daily[1]["holdings"], 1)
        self.assertGreaterEqual(daily[1]["pendingSellCount"], 1)
        self.assertGreaterEqual(daily[2]["pendingSellCount"], 1)
        self.assertEqual(daily[3]["pendingSellCount"], 0)
        self.assertEqual(daily[-1]["pendingSellAtWindowEnd"], 0)

    def test_pending_sell_is_not_bought_back_and_survives_window_end_report(self):
        """挂账卖出的仓位不得被重新买回；窗口末仍未卖掉时必须显式上报。"""
        hist = [f"202608{i:02d}" for i in range(1, 22)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0]
        test_days = [hist[-1], "20260901"]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(hist, closes)}}
        bars["A"][hist[-1]] = {"open": 14.0, "high": 14.0, "low": 10.1, "close": 14.0,
                               "volume": 100.0, "pct_chg": 0.0}
        # 止损触发且封跌停；窗口在此结束，卖出仍未成交
        bars["A"]["20260901"] = {"open": 13.0, "high": 13.0, "low": 12.0, "close": 12.6,
                                 "volume": 100.0, "pct_chg": -10.0, "limit_down": 12.6}
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in test_days]
        daily = _run_window_backtest(
            frame, _Model(), test_days, bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "rebalanceDays": 10,
             "initialStopLoss": 0.05, "trailingDrawdown": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "goldenCrossLookbackDays": 1, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True}},
            lambda _: None, 0, 100,
        )
        self.assertFalse(any(t["side"] == "sell" for row in daily for t in row["trades"]))
        self.assertEqual(len([t for row in daily for t in row["trades"] if t["side"] == "buy"]), 1,
                         "挂账卖出的仓位不得被重新买回")
        self.assertEqual(daily[-1]["pendingSellAtWindowEnd"], 1,
                         "窗口末仍未卖掉的挂账仓位必须显式上报，不能静默消失")

    def test_min_holding_days_suppresses_early_death_cross_but_not_stop(self):
        """最小持有期：不足天数的持仓忽略普通死叉退出，但止损必须立即执行。

        目的只是消掉边缘反复交易，不阻止真正的趋势反转退出。
        """
        dates = [f"202608{i:02d}" for i in range(1, 22)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0]
        # 金叉日在 dates[-1] 建仓；随后第 1 个交易日就出现死叉（应被忽略）
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(dates, closes)}}
        bars["A"][dates[-1]] = {"open": 14.0, "high": 14.0, "low": 10.1, "close": 14.0,
                                "volume": 100.0, "pct_chg": 0.0}
        # 次日死叉但价格仍在 MA20 上方。用 marker 直接注入信号，
        # 否则 _add_technical_timing_signals 会用均线重算并覆盖掉 death_cross。
        death_day = "20260901"
        follow = "20260902"
        bars["A"][death_day] = {"open": 14.0, "high": 14.2, "low": 13.9, "close": 14.1,
                                "volume": 100.0, "pct_chg": 0.0, "_timing_2_20": True,
                                "death_cross": True, "golden_cross": False, "ma20": 12.0}
        bars["A"][follow] = {"open": 14.1, "high": 14.3, "low": 14.0, "close": 14.2,
                             "volume": 100.0, "pct_chg": 0.0}
        test_days = [dates[-1], death_day, follow]
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in (dates[-1], death_day)]

        def run(min_hold: int):
            return _run_window_backtest(
                frame, _Model(), test_days, bars,
                {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
                 "initialCapital": 100_000, "commissionRate": 0.0,
                 "stampDutyRate": 0.0, "slippageRate": 0.0, "rebalanceDays": 10,
                 "initialStopLoss": 0.05, "trailingDrawdown": 0.0,
                 "technicalTiming": {"enabled": True, "goldenCross": True,
                                      "goldenCrossLookbackDays": 1, "volumeBreakout": False,
                                      "requireAboveMa20": True, "deathCross": True,
                                      "minHoldingDays": min_hold}},
                lambda _: None, 0, 100)

        # 关闭最小持有期：死叉当日就卖出
        base = run(0)
        base_sells = [t for row in base for t in row["trades"] if t["side"] == "sell"]
        self.assertEqual(len(base_sells), 1)
        self.assertEqual(base_sells[0]["reason"], "death_cross")

        # 开启 3 日最小持有期：建仓后第 1 日的死叉被忽略，不产生卖出
        held = run(3)
        self.assertFalse(any(t["side"] == "sell" for row in held for t in row["trades"]),
                         "持有不足 3 个交易日时，普通死叉不得触发退出")

    def test_min_holding_days_does_not_block_stop_loss(self):
        """最小持有期只约束普通死叉，止损不受影响。"""
        dates = [f"202608{i:02d}" for i in range(1, 22)]
        closes = [10.0] * 10 + [9.0] * 10 + [14.0]
        bars = {"A": {d: {"open": c, "high": c, "low": c, "close": c,
                            "volume": 100.0, "pct_chg": 0.0}
                      for d, c in zip(dates, closes)}}
        bars["A"][dates[-1]] = {"open": 14.0, "high": 14.0, "low": 10.1, "close": 14.0,
                                "volume": 100.0, "pct_chg": 0.0}
        # 建仓后第 1 个交易日即跌穿 5% 止损（13.3）
        bars["A"]["20260901"] = {"open": 13.0, "high": 13.0, "low": 12.8, "close": 12.9,
                                 "volume": 100.0, "pct_chg": -2.0, "limit_down": 12.6}
        test_days = [dates[-1], "20260901"]
        frame = [{"trade_date": dates[-1], "code": "A", "momentum20": 2.0, "mktCap": 3e9}]
        daily = _run_window_backtest(
            frame, _Model(), test_days, bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "rebalanceDays": 10,
             "initialStopLoss": 0.05, "trailingDrawdown": 0.0,
             "technicalTiming": {"enabled": True, "goldenCross": True,
                                  "goldenCrossLookbackDays": 1, "volumeBreakout": False,
                                  "requireAboveMa20": True, "deathCross": True,
                                  "minHoldingDays": 5}},
            lambda _: None, 0, 100)
        sells = [t for row in daily for t in row["trades"] if t["side"] == "sell"]
        self.assertEqual(len(sells), 1, "止损必须立即执行，不受最小持有期约束")
        self.assertTrue(sells[0]["reason"].startswith("initial_stop"))

    def test_reentry_cooldown_blocks_only_rebuy_for_full_trading_days(self):
        days = [f"2026100{i}" for i in range(1, 7)]
        bars = {"A": {day: {"open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0,
                             "volume": 100.0, "pct_chg": 0.0, "_timing_2_20": True,
                             "ma20": 9.0, "golden_cross": i != 1,
                             "golden_cross_age": 0 if i != 1 else None,
                             "death_cross": i == 1, "volume_breakout": False}
                      for i, day in enumerate(days)}}
        frame = [{"trade_date": day, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for day in days]

        def run(cooldown: int):
            return _run_window_backtest(
                frame, _Model(), days, bars,
                {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
                 "initialCapital": 100_000, "commissionRate": 0.0,
                 "stampDutyRate": 0.0, "slippageRate": 0.0,
                 "initialStopLoss": 0.0, "trailingDrawdown": 0.0,
                 "technicalTiming": {"enabled": True, "goldenCross": True,
                                      "goldenCrossLookbackDays": 1, "volumeBreakout": False,
                                      "requireAboveMa20": True, "deathCross": True,
                                      "reentryCooldownDays": cooldown}},
                lambda _: None, 0, 100)

        base = run(0)
        cooled = run(3)
        base_buys = [t["tradeDate"] for row in base for t in row["trades"] if t["side"] == "buy"]
        cooled_buys = [t["tradeDate"] for row in cooled for t in row["trades"] if t["side"] == "buy"]
        self.assertEqual(base_buys, [days[0], days[2]])
        self.assertEqual(cooled_buys, [days[0], days[5]])
        self.assertEqual(cooled[-1]["cooldownBlockedEntries"], 3)

    def test_other_execution_modes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "same_day_close"):
            _validate_execution_config({"executionMode": "next_day_open", "snapshotTime": "14:40"})

    def test_other_snapshot_times_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "14:40"):
            _validate_execution_config({"executionMode": "same_day_close", "snapshotTime": "15:00"})

    def test_limit_and_suspension_constraints(self):
        base = {"close": 10.0, "is_suspended": False, "limit_up": 10.0, "limit_down": 8.0}
        self.assertFalse(_can_trade(base, "buy"))
        self.assertTrue(_can_trade(base, "sell"))
        down = dict(base, close=8.0)
        self.assertFalse(_can_trade(down, "sell"))
        self.assertFalse(_can_trade(dict(base, is_suspended=True), "buy"))

    def test_trailing_stop_sells_after_eight_percent_drawdown(self):
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in ("20260901", "20260902", "20260903")]
        bars = {"A": {
            "20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
            "20260902": {"open": 11.0, "high": 11.0, "low": 11.0, "close": 11.0},
            "20260903": {"open": 10.1, "high": 10.1, "low": 10.1, "close": 10.1},
        }}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901", "20260902", "20260903"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "rebalanceDays": 10, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0,
             "initialStopLoss": 0.05, "trailingDrawdown": 0.08},
            lambda _: None, 0, 100,
        )
        sell = next(t for t in daily[-1]["trades"] if t["side"] == "sell")
        self.assertEqual(sell["reason"], "trailing_stop_gap")
        self.assertEqual(daily[-1]["stopTradeCount"], 1)
        self.assertAlmostEqual(daily[-1]["nav"], 1.01, places=3)

    def test_intraday_low_triggers_stop_even_if_close_recovers(self):
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in ("20260901", "20260902")]
        bars = {"A": {
            "20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
            "20260902": {"open": 10.0, "high": 10.2, "low": 9.4, "close": 10.1},
        }}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901", "20260902"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "rebalanceDays": 10, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "initialStopLoss": 0.05},
            lambda _: None, 0, 100,
        )
        sell = next(t for t in daily[-1]["trades"] if t["side"] == "sell")
        self.assertEqual(sell["reason"], "initial_stop")
        self.assertAlmostEqual(sell["price"], 9.5)
        self.assertAlmostEqual(daily[-1]["nav"], 0.95)

    def test_gap_below_stop_fills_at_open(self):
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in ("20260901", "20260902")]
        bars = {"A": {
            "20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
            "20260902": {"open": 9.0, "high": 9.3, "low": 8.8, "close": 9.2},
        }}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901", "20260902"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "rebalanceDays": 10, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "initialStopLoss": 0.05},
            lambda _: None, 0, 100,
        )
        sell = next(t for t in daily[-1]["trades"] if t["side"] == "sell")
        self.assertEqual(sell["reason"], "initial_stop_gap")
        self.assertAlmostEqual(sell["price"], 9.0)
        self.assertAlmostEqual(daily[-1]["nav"], 0.9)

    def test_records_cost_breakdown_and_exact_no_cost_nav(self):
        frame = [{"trade_date": "20260901", "code": "A", "momentum20": 2.0, "mktCap": 3e9}]
        bars = {"A": {"20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0}}}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "initialCapital": 100_000, "commissionRate": 0.001,
             "stampDutyRate": 0.0, "slippageRate": 0.0},
            lambda _: None, 0, 100,
        )
        self.assertLess(daily[-1]["nav"], daily[-1]["noCostNav"])
        self.assertGreater(daily[-1]["cumulativeCommission"], 0)
        self.assertEqual(daily[-1]["cumulativeStampDuty"], 0)
        self.assertTrue(daily[-1]["trades"])

    def test_corporate_action_adjusts_shares_before_valuation_and_stops(self):
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in ("20260901", "20260902")]
        bars = {"A": {
            "20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
                         "adj_factor": 1.0},
            # 1拆2：原始价格减半、复权因子翻倍；经济价值应保持不变。
            "20260902": {"open": 5.0, "high": 5.0, "low": 5.0, "close": 5.0,
                         "adj_factor": 2.0},
        }}
        daily = _run_window_backtest(
            frame, _Model(), ["20260901", "20260902"], bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "rebalanceDays": 10, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0, "initialStopLoss": 0.1},
            lambda _: None, 0, 100,
        )
        self.assertAlmostEqual(daily[-1]["nav"], 1.0)
        self.assertEqual(daily[-1]["stopTradeCount"], 0)
        self.assertEqual(daily[-1]["corporateActionAdjustments"], 1)

    def test_missing_bar_carries_last_price_instead_of_zeroing_position(self):
        dates = ("20260901", "20260902", "20260903")
        frame = [{"trade_date": d, "code": "A", "momentum20": 2.0, "mktCap": 3e9}
                 for d in dates]
        bars = {"A": {
            "20260901": {"open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0},
            # 09-02 停牌/行情缺失：持仓仍应按10元估值。
            "20260903": {"open": 10.5, "high": 11.0, "low": 10.5, "close": 11.0},
        }}
        daily = _run_window_backtest(
            frame, _Model(), list(dates), bars,
            {"executionMode": "same_day_close", "snapshotTime": "14:40", "topN": 1,
             "rebalanceDays": 10, "initialCapital": 100_000, "commissionRate": 0.0,
             "stampDutyRate": 0.0, "slippageRate": 0.0},
            lambda _: None, 0, 100,
        )
        self.assertAlmostEqual(daily[1]["nav"], 1.0)
        self.assertAlmostEqual(daily[2]["nav"], 1.1)


if __name__ == "__main__":
    unittest.main()
