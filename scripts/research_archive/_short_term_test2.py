# -*- coding: utf-8 -*-
"""短线玩法组合回测 v2（真实资金口径）：
选票同 v1（放量突破 20 日均线 + 涨幅2~7% + 价>5 → 量比×涨幅 top5/日，次日开盘买）
组合引擎：100 万现金、每笔固定 20% 仓位（现金不足则跳过）、T+1、
盘中 5m 触发止盈/止损（触发价成交）、到期收盘卖出、每日按收盘估值。
成本：佣金0.0003+印花0.0005+滑点0.001。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20240820"
TOP_N = 5
INIT = 1_000_000.0
POS_VALUE = 200_000.0  # 每笔 20% 仓位
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001


def load_daily():
    m = sqlite3.connect("data/market.db")
    rows = m.execute(
        "SELECT code, trade_date, open, close, pct_chg, volume FROM daily_bars WHERE trade_date>=? ORDER BY code, trade_date",
        (START,)).fetchall()
    m.close()
    ser = defaultdict(list)
    for c, d, o, cl, pct, vol in rows:
        ser[c].append((d, o, cl, pct, vol))
    return ser


def signal_on(ser, day, codes):
    out = []
    for c in codes:
        s = ser[c]
        idxs = [i for i, x in enumerate(s) if x[0] <= day]
        if len(idxs) < 25:
            continue
        i = idxs[-1]
        w = s[max(0, i - 24):i + 1]
        if len(w) < 21:
            continue
        d, o, cl, pct, vol = w[-1]
        if not (2 <= pct <= 7) or cl < 5:
            continue
        closes = np.asarray([x[2] for x in w], dtype=float)
        vols = np.asarray([x[4] for x in w], dtype=float)
        ma20 = float(np.mean(closes[-20:]))
        ma20_prev = float(np.mean(closes[-21:-1]))
        if not (cl > ma20 and closes[-2] <= ma20_prev):
            continue
        vol_ratio = vol / (np.mean(vols[-6:-1]) + 1e-9)
        if vol_ratio < 1.5:
            continue
        out.append((c, vol_ratio * pct))
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
        if d < START or day_idx[d] < 25:
            continue
        top = signal_on(ser, d, codes)
        if top:
            signal_days.append((d, top))
    print(f"信号日 {len(signal_days)} 天")

    # 预取 close：涉及的 code
    m = sqlite3.connect("data/market.db")
    close_map = defaultdict(dict)
    need = {c for _, tops in signal_days for c in tops}
    for c in need:
        for r in m.execute("SELECT trade_date, close FROM daily_bars WHERE code=?", (c,)):
            close_map[c][r[0]] = r[1]
    open_map = defaultdict(dict)
    for c in need:
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
        positions = {}  # code -> (qty, buy_px, buy_i, bars, bar_i)
        nav_curve = []
        per_trade = []
        n_tp = n_sl = n_due = 0
        sig_by_buy_day = defaultdict(list)  # 买入日 -> [code...]
        for sig_day, tops in signal_days:
            bi = day_idx[sig_day]
            if bi + 1 < len(trade_days):
                sig_by_buy_day[trade_days[bi + 1]].extend(tops)
        for i, d in enumerate(trade_days):
            # 1) 卖出：触发 / 到期
            for c in list(positions):
                qty, bp, buy_i, bars, bar_i = positions[c]
                # 推进 5m 到当日
                day_lo = d + " 15:05:00"
                j = bar_i
                exit_type = exit_px = None
                if tp and bars:
                    tp_px = bp * (1 + tp)
                    sl_px = bp * (1 - sl)
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
            # 2) 买入
            for c in sig_by_buy_day.get(d, []):
                if len(positions) >= 30:
                    break
                buy_px = open_map[c].get(d)
                if not buy_px or cash < POS_VALUE * 1.004:
                    continue
                qty = int(POS_VALUE / (buy_px * 1.004) // 100) * 100
                if qty <= 0:
                    continue
                # 预拉 5m：买入次日 ~ 到期日
                bi = day_idx[d]
                d0 = trade_days[bi + 1] if bi + 1 < len(trade_days) else d
                d1 = trade_days[min(bi + hold_days, len(trade_days) - 1)]
                bars = load_min(c, d0, d1) if tp else []
                cash -= qty * buy_px * 1.004
                positions[c] = (qty, buy_px, bi, bars, 0)
            # 3) 估值
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

    print("=" * 26, "对照组：3 天到期", "=" * 26)
    backtest(None, 0, 3, "3天到期   ")
    print("=" * 26, "你的玩法：止盈+4% 止损-2.5%", "=" * 26)
    backtest(0.04, 0.025, 3, "TP4/SL2.5 ")
    print("=" * 26, "变体：止盈+6% 止损-3% + 3天", "=" * 26)
    backtest(0.06, 0.03, 3, "TP6/SL3   ")
    print("=" * 26, "变体：止盈+8% 止损-4% + 5天", "=" * 26)
    backtest(0.08, 0.04, 5, "TP8/SL4   ")
    print("=" * 26, "变体：止盈+10% 止损-5% + 10天", "=" * 26)
    backtest(0.10, 0.05, 10, "TP10/SL5  ")


if __name__ == "__main__":
    main()
