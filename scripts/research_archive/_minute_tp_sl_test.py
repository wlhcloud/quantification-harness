# -*- coding: utf-8 -*-
"""价位止盈止损验证（A 方案：挂在现有 6 因子信号上，用 5 分钟数据触发）：
- 信号：6 因子等权 top5、20 交易日调仓（与 _long_window_test 同口径）
- 买入：信号日次日开盘价（T+1）
- 卖出：5m 数据逐 bar 扫描 —— 盘中 high>=buy*(1+tp) 止盈、low<=buy*(1-sl) 止损
  （买入当日 T+1 不触发，从买入次日开始；同日双触发按保守取低收益）
- 未触发：持有至下一调仓日以收盘价卖出
- 成本：佣金 0.0003 + 印花 0.0005 + 滑点 0.001（与全区间体检一致）
- 数据：factors.db(stock_ml_factors) + market.db(daily_bars) + minute.db(minute_bars, 5m, 2024-08-20 起)
"""
import sqlite3
import numpy as np

MINUTE_START = "20240820"
TOP_N = 5
REBAL = 20
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001
FACTORS = ["dvTtm", "floatRatio", "debtToAssets", "ocfYoy", "netprofitYoy", "revenueYoy"]


def load():
    f = sqlite3.connect("data/factors.db")
    rows = f.execute(
        f"SELECT trade_date, code, {','.join(FACTORS)} FROM stock_ml_factors WHERE trade_date>=?",
        (MINUTE_START,)).fetchall()
    f.close()
    # 交易日历
    dates = sorted({r[0] for r in rows})
    # code 每交易日因子
    feat_by_day = {}
    for r in rows:
        d, c = r[0], r[1]
        feat_by_day.setdefault(d, []).append((c, r[2:]))
    return dates, feat_by_day


def pick_top(day_rows):
    codes = [x[0] for x in day_rows]
    f = np.asarray([x[1] for x in day_rows], dtype=float)
    n = len(codes)
    scores = np.zeros(n, dtype=float)
    for j in range(f.shape[1]):
        vals = f[:, j]
        order = np.argsort(vals, kind="stable")
        rank = np.empty(n, dtype=float)
        rank[order] = np.arange(n)
        rank[np.isnan(vals)] = n + 1
        scores += rank
    idx = np.argsort(scores, kind="stable")[:TOP_N]
    return [codes[i] for i in idx]


def open_on(code, day):
    """code 在 day 的开盘价（T+1 买入执行价）"""
    m = sqlite3.connect("data/market.db")
    r = m.execute("SELECT open FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
    m.close()
    return r[0] if r else None


def close_on(code, day):
    m = sqlite3.connect("data/market.db")
    r = m.execute("SELECT close FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
    m.close()
    return r[0] if r else None


def scan_minute(code, d0, d1, tp, sl, buy):
    """扫描 code 在 [d0, d1] 的 5m 数据（从 d0 当天起，d0 为买入日次日），
    返回 (exit_type, exit_price) exit_type: 'tp'|'sl'|None"""
    m = sqlite3.connect("data/minute.db")
    rows = m.execute(
        "SELECT trade_time, high, low FROM minute_bars WHERE code=? AND trade_time>=? AND trade_time<=? ORDER BY trade_time",
        (code, d0 + " 09:15:00", d1 + " 15:05:00")).fetchall()
    m.close()
    tp_px = buy * (1 + tp)
    sl_px = buy * (1 - sl)
    for t, hi, lo in rows:
        if hi >= tp_px and lo <= sl_px:
            # 同日双触发：保守取收益低者
            return ("tp", tp_px) if (tp_px - buy) <= (buy - sl_px) else ("sl", sl_px)
        if hi >= tp_px:
            return ("tp", tp_px)
        if lo <= sl_px:
            return ("sl", sl_px)
    return (None, None)


def main():
    dates, feat_by_day = load()
    # 调仓日：从首个 >= MINUTE_START 的交易日开始，每 20 日
    rebal_idx = [i for i, d in enumerate(dates) if d >= MINUTE_START][0::REBAL]
    # 信号序列：(信号日, [买入code...])
    signals = []
    for i in rebal_idx:
        d = dates[i]
        day_rows = feat_by_day.get(d, [])
        if len(day_rows) < 10:
            continue
        top = pick_top(day_rows)
        # 买入日 = 下一交易日
        if i + 1 < len(dates):
            signals.append((dates[i + 1], top, dates[min(i + REBAL, len(dates) - 1)]))
    print(f"信号批次: {len(signals)} 批, 交易日 {dates[0]}~{dates[-1]}")

    def backtest(tp, sl, tag):
        batches = []
        for buy_day, codes, next_rebal_day in signals:
            batch = []
            for c in codes:
                buy = open_on(c, buy_day)
                if not buy:
                    continue
                bi = dates.index(buy_day) if buy_day in dates else None
                if bi is None or bi + 1 >= len(dates):
                    continue
                scan_start = dates[bi + 1]
                scan_end = next_rebal_day
                exit_type, exit_px = scan_minute(c, scan_start, scan_end, tp, sl, buy)
                if exit_type:
                    ret = exit_px / buy - 1
                    batch.append((c, buy_day, exit_type, ret))
                else:
                    sell = close_on(c, next_rebal_day)
                    if not sell:
                        continue
                    batch.append((c, buy_day, "hold", sell / buy - 1))
            if batch:
                batches.append(batch)
        if not batches:
            print(f"[{tag}] 无交易")
            return
        # 批次净值：每批 5 只等权 → 批次收益 → 连乘
        nav = 1.0
        n_tp = n_sl = n_hold = 0
        all_rets = []
        for b in batches:
            rets = np.asarray([t[3] for t in b]) - (COMM + STAMP + SLIP)
            for t, r in zip(b, rets):
                n_tp += t[2] == "tp"
                n_sl += t[2] == "sl"
                n_hold += t[2] == "hold"
                all_rets.append(r)
            nav *= 1 + float(np.mean(rets))
        rets = np.asarray(all_rets)
        win = float((rets > 0).mean())
        total = nav - 1
        days = len(batches) * REBAL
        annual = (1 + total) ** (250.0 / days) - 1
        print(f"[{tag}] 批次 {len(batches)} | 止盈 {n_tp} / 止损 {n_sl} / 持有到期 {n_hold} | "
              f"胜率 {win*100:.1f}% | 单笔均 {rets.mean()*100:+.2f}% | 累计 {total*100:+.1f}% | 年化 {annual*100:+.1f}%")
        return total

    print("=" * 30, "基准：不设止盈止损（持有 20 日）", "=" * 30)
    backtest(99.0, 0.0, "基准硬拿  ")
    print("=" * 30, "参数扫描：止盈 x 止损", "=" * 30)
    for tp in [0.03, 0.04, 0.06, 0.08]:
        for sl in [0.02, 0.025, 0.03, 0.05]:
            backtest(tp, sl, f"TP{tp*100:.1f}%/SL{sl*100:.1f}%")
    print("=" * 30, "你的例子 TP+4%/SL-2.5%", "=" * 30)
    backtest(0.04, 0.025, "TP4%/SL2.5%")


if __name__ == "__main__":
    main()
