# -*- coding: utf-8 -*-
"""短线因子换代 v4：开盘缺口因子（IC +2.31% 唯一显著为正）vs 放量突破（IC≈0）
选票：gap = open/pre_close - 1，筛选 gap>=阈值、价>5、成交额>=5千万 → gap 最大 top5/日
买入：信号日开盘价（集合竞价价成交）；T+1；卖出对比 3天到期 / TP4-SL2.5 / TP10-SL5-10天。
组合引擎同 v3（100万、20%仓位/笔、5m触发、到期收盘卖、每日收盘估值）。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20240820"
TOP_N = 5
INIT = 1_000_000.0
POS_VALUE = 200_000.0
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001
GAP_MIN = 0.01   # 缺口 >= 1%


def load_daily():
    m = sqlite3.connect("data/market.db")
    rows = m.execute(
        "SELECT code, trade_date, open, close, pre_close, amount FROM daily_bars WHERE trade_date>=? ORDER BY code, trade_date",
        (START,)).fetchall()
    m.close()
    ser = defaultdict(list)
    for c, d, o, cl, pc, amt in rows:
        ser[c].append((d, o, cl, pc, amt))
    return ser


def signal_on(ser, day, codes):
    out = []
    for c in codes:
        s = ser[c]
        idxs = [i for i, x in enumerate(s) if x[0] <= day]
        if not idxs:
            continue
        i = idxs[-1]
        d, o, cl, pc, amt = s[i]
        if i < 1:
            continue
        prev_close = s[i - 1][2]
        if cl < 5 or amt is None or amt < 5e4:   # amount 单位千元, 5e4=5000万成交额
            continue
        if prev_close and prev_close > 0:
            gap = o / prev_close - 1
        else:
            continue
        if gap < GAP_MIN:
            continue
        out.append((c, gap))
    out.sort(key=lambda x: -x[1])
    return [c for c, _ in out[:TOP_N]]


def main():
    ser = load_daily()
    codes = sorted(ser.keys())
    trade_days = sorted({x[0] for s in ser.values() for x in s})
    day_idx = {d: i for i, d in enumerate(trade_days)}
    print(f"股票 {len(codes)} 只, 交易日 {trade_days[0]}~{trade_days[-1]} ({len(trade_days)} 天)")

    signal_days = []
    for d in trade_days:
        top = signal_on(ser, d, codes)
        if top:
            signal_days.append((d, top))
    print(f"信号日 {len(signal_days)} 天, 缺口阈值 {GAP_MIN*100:.0f}%")
    if signal_days:
        print("  最近信号:", signal_days[-1][1])

    m = sqlite3.connect("data/market.db")
    need = {c for _, tops in signal_days for c in tops}
    close_map, open_map = defaultdict(dict), defaultdict(dict)
    for c in need:
        for r in m.execute("SELECT trade_date, close FROM daily_bars WHERE code=?", (c,)):
            close_map[c][r[0]] = r[1]
        for r in m.execute("SELECT trade_date, open FROM daily_bars WHERE code=?", (c,)):
            open_map[c][r[0]] = r[1]
    m.close()

    def load_min(code, d0, d1):
        mm = sqlite3.connect("data/minute.db")
        rows = mm.execute(
            "SELECT trade_time, high, low FROM minute_bars WHERE code=? AND trade_time>=? AND trade_time<=? ORDER BY trade_time",
            (code, d0 + " 09:15:00", d1 + " 15:05:00")).fetchall()
        mm.close()
        return rows

    def backtest(tp, sl, hold_days, tag):
        cash = INIT
        positions = {}
        nav_curve = []
        per_trade = []
        n_tp = n_sl = n_due = 0
        plan = []
        for sig_day, tops in signal_days:
            si = day_idx[sig_day]
            for c in tops:
                plan.append((sig_day, si, c))
        plan.sort(key=lambda x: (x[0], x[2]))
        for i, d in enumerate(trade_days):
            for c in list(positions):
                qty, bp, buy_i, bars, bar_i = positions[c]
                day_lo = d + " 15:05:00"
                exit_type = exit_px = None
                if tp and bars:
                    tp_px = bp * (1 + tp)
                    sl_px = bp * (1 - sl)
                    j = bar_i
                    while j < len(bars) and bars[j][0] <= day_lo:
                        hi, lo = bars[j][1], bars[j][2]
                        if hi >= tp_px and lo <= sl_px:
                            exit_type = "tp" if (tp_px - bp) <= (bp - sl_px) else "sl"
                            exit_px = tp_px if exit_type == "tp" else sl_px
                            break
                        if hi >= tp_px:
                            exit_type, exit_px = "tp", tp_px
                            break
                        if lo <= sl_px:
                            exit_type, exit_px = "sl", sl_px
                            break
                        j += 1
                    bar_i = j
                if exit_type:
                    cash += qty * exit_px * (1 - COMM - STAMP - SLIP)
                    per_trade.append(exit_px / bp - 1)
                    n_tp += exit_type == "tp"
                    n_sl += exit_type == "sl"
                    del positions[c]
                    continue
                if i - buy_i >= hold_days:
                    px = close_map[c].get(d)
                    if px:
                        cash += qty * px * (1 - COMM - STAMP - SLIP)
                        per_trade.append(px / bp - 1)
                        n_due += 1
                    del positions[c]
                    continue
                positions[c] = (qty, bp, buy_i, bars, bar_i)
            if len(positions) < 30:
                for c in [p[2] for p in plan if p[0] == d]:
                    if len(positions) >= 30:
                        break
                    buy_px = open_map[c].get(d)
                    if not buy_px or cash < POS_VALUE * 1.004:
                        continue
                    qty = int(POS_VALUE / (buy_px * 1.004) // 100) * 100
                    if qty <= 0:
                        continue
                    bi = day_idx[d]
                    d0 = trade_days[bi + 1] if bi + 1 < len(trade_days) else d
                    d1 = trade_days[min(bi + hold_days, len(trade_days) - 1)]
                    bars = load_min(c, d0, d1) if tp else []
                    cash -= qty * buy_px * 1.004
                    positions[c] = (qty, buy_px, bi, bars, 0)
            mv = 0.0
            for c, (qty, bp, buy_i, bars, bar_i) in positions.items():
                px = close_map[c].get(d)
                if px:
                    mv += qty * px
            nav_curve.append((d, cash + mv))
        navs = np.asarray([v for _, v in nav_curve], dtype=float)
        total = navs[-1] / navs[0] - 1
        rets = navs[1:] / navs[:-1] - 1
        annual = (1 + total) ** (250.0 / len(navs)) - 1
        vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0
        sharpe = annual / vol if vol > 0 else 0
        peak, mdd = -np.inf, 0.0
        for v in navs:
            peak = max(peak, v)
            mdd = min(mdd, v / peak - 1)
        wr = float((np.asarray(per_trade) > 0).mean()) if per_trade else 0
        print(f"[{tag}] 笔数 {len(per_trade)} | 止盈 {n_tp}/止损 {n_sl}/到期 {n_due} | 胜率 {wr*100:.1f}% | "
              f"累计 {total*100:+.1f}% | 年化 {annual*100:+.1f}% | Sharpe {sharpe:.2f} | 回撤 {mdd*100:.1f}%")

    print("=" * 26, "开盘缺口因子（新）", "=" * 26)
    backtest(None, 0, 3, "缺口/3天到期   ")
    backtest(0.04, 0.025, 3, "缺口/TP4-SL2.5  ")
    backtest(0.10, 0.05, 10, "缺口/TP10-SL5   ")
    print("对照（放量突破因子, v3 结果）: 开盘买/TP10-SL5/10天 +38.4% | 3天到期 -49.4%")


if __name__ == "__main__":
    main()
