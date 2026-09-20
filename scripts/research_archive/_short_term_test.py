# -*- coding: utf-8 -*-
"""短线玩法回测（B 方案）：放量突破 20 日均线选票 → 次日开盘买入 → 价位止盈止损 vs 到期卖出
- 选票（日线）：pct_chg 2~7%、close>MA20 且昨收<=昨MA20（突破）、量>1.5×5日均量、价>5元
  → 按 量比×涨幅 打分取当日 top5（等权 20%）
- 执行（5m）：买入次日开始盘中触发止盈/止损（T+1），或持有 N 天到期收盘卖
- 成本：佣金0.0003+印花0.0005+滑点0.001，买卖双边
- 口径：每笔独立等权 20%、净值连乘；数据 2024-08-20 起（分钟库边界）
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20240820"
TOP_N = 5
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
    """day 收盘后的突破信号：返回 (code, score) 列表"""
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
        if not (2 <= pct <= 7):
            continue
        if cl < 5:
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


def open_on(code, day):
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
    """5m 扫描 [d0,d1]，返回 ('tp'|'sl'|None, price)"""
    m = sqlite3.connect("data/minute.db")
    rows = m.execute(
        "SELECT high, low FROM minute_bars WHERE code=? AND trade_time>=? AND trade_time<=? ORDER BY trade_time",
        (code, d0 + " 09:15:00", d1 + " 15:05:00")).fetchall()
    m.close()
    tp_px = buy * (1 + tp)
    sl_px = buy * (1 - sl)
    for hi, lo in rows:
        if hi >= tp_px and lo <= sl_px:
            return ("tp", tp_px) if (tp_px - buy) <= (buy - sl_px) else ("sl", sl_px)
        if hi >= tp_px:
            return ("tp", tp_px)
        if lo <= sl_px:
            return ("sl", sl_px)
    return (None, None)


def main():
    ser = load_daily()
    codes = sorted(ser.keys())
    trade_days = sorted({x[0] for s in ser.values() for x in s})
    # 交易日 → index
    day_idx = {d: i for i, d in enumerate(trade_days)}
    print(f"股票 {len(codes)} 只, 交易日 {trade_days[0]}~{trade_days[-1]} ({len(trade_days)} 天)")

    # 信号日 = 有突破信号的交易日（20240820 后，避开前 25 天指标预热）
    signal_days = []
    for d in trade_days:
        if d < START or day_idx[d] < 25:
            continue
        top = signal_on(ser, d, codes)
        if top:
            signal_days.append((d, top))
    print(f"信号日 {len(signal_days)} 天, 信号 {sum(len(t) for _, t in signal_days)} 次")
    # 抽样打印最近 3 个信号日
    for d, top in signal_days[-3:]:
        print(f"  {d}: {top}")

    def backtest(tp, sl, hold_days, tag):
        trades = []
        for sig_day, tops in signal_days:
            bi = day_idx[sig_day]
            if bi + 1 >= len(trade_days):
                continue
            buy_day = trade_days[bi + 1]
            for c in tops:
                buy = open_on(c, buy_day)
                if not buy:
                    continue
                # 到期日 = 买入后第 hold_days 个交易日
                if bi + 1 + hold_days < len(trade_days):
                    due_day = trade_days[bi + 1 + hold_days]
                else:
                    due_day = trade_days[-1]
                if tp is None:
                    sell = close_on(c, due_day)
                    if not sell:
                        continue
                    trades.append((c, buy_day, "due", sell / buy - 1))
                    continue
                # 扫描区间：买入日次日 ~ due_day
                scan_d0 = trade_days[bi + 2] if bi + 2 < len(trade_days) else buy_day
                exit_type, exit_px = scan_minute(c, scan_d0, due_day, tp, sl, buy)
                if exit_type:
                    trades.append((c, buy_day, exit_type, exit_px / buy - 1))
                else:
                    sell = close_on(c, due_day)
                    if not sell:
                        continue
                    trades.append((c, buy_day, "due", sell / buy - 1))
        if not trades:
            print(f"[{tag}] 无交易")
            return
        # 每笔独立 20% 仓位 → 净值连乘
        nav = 1.0
        n_tp = n_sl = n_due = 0
        all_r = []
        for t in trades:
            r = t[3] - (COMM + SLIP) - (COMM + STAMP + SLIP)
            n_tp += t[2] == "tp"
            n_sl += t[2] == "sl"
            n_due += t[2] == "due"
            all_r.append(r)
            nav *= 1 + r * 0.2
        rets = np.asarray(all_r)
        win = float((rets > 0).mean())
        total = nav - 1
        days = len(signal_days)
        annual = (1 + total) ** (250.0 / days) - 1
        print(f"[{tag}] 信号 {len(trades)} 笔 | 止盈 {n_tp}/止损 {n_sl}/到期 {n_due} | 胜率 {win*100:.1f}% | "
              f"单笔均 {rets.mean()*100:+.2f}% | 累计 {total*100:+.1f}% | 年化 {annual*100:+.1f}%")

    print("=" * 28, "对照组：持有 3 天到期", "=" * 28)
    backtest(None, 0, 3, "3天到期")
    print("=" * 28, "你的玩法：止盈+4% 止损-2.5%", "=" * 28)
    backtest(0.04, 0.025, 3, "TP4/SL2.5/3天")
    print("=" * 28, "变体：止盈+6% 止损-3% + 3天到期", "=" * 28)
    backtest(0.06, 0.03, 3, "TP6/SL3/3天")
    print("=" * 28, "变体：止盈+8% 止损-4% + 5天到期", "=" * 28)
    backtest(0.08, 0.04, 5, "TP8/SL4/5天")
    print("=" * 28, "变体：止盈+10% 止损-5% + 10天到期", "=" * 28)
    backtest(0.10, 0.05, 10, "TP10/SL5/10天")


if __name__ == "__main__":
    main()
