# -*- coding: utf-8 -*-
"""时点对齐验证：gap_open 因子在不同买入时点下的 IC
标签A: open[t] -> close[t+10]（开盘买入）
标签B: close[t] -> close[t+10]（收盘买入）
若 A 不显著 / B 显著 -> 缺口因子只对收盘买入有效，开盘买入是陷阱。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20240701"
IC_START = "20240820"
LABEL_DAYS = 10


def load():
    m = sqlite3.connect("data/market.db")
    rows = m.execute(
        "SELECT code, trade_date, open, close FROM daily_bars WHERE trade_date>=? ORDER BY code, trade_date",
        (START,)).fetchall()
    m.close()
    ser = defaultdict(list)
    for c, d, o, cl in rows:
        ser[c].append((d, o, cl))
    return ser


def spearman(a, b):
    n = len(a)
    if n < 5:
        return np.nan
    def rank(x):
        order = np.argsort(np.asarray(x, dtype=float), kind="stable")
        r = np.empty(n, dtype=float)
        r[order] = np.arange(n)
        return r
    ra, rb = rank(a), rank(b)
    ra -= ra.mean(); rb -= rb.mean()
    denom = np.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / denom) if denom > 0 else np.nan


def main():
    ser = load()
    daily = defaultdict(lambda: {"gap": {}, "fwd_open": {}, "fwd_close": {}})
    for c, s in ser.items():
        n = len(s)
        for i in range(n - LABEL_DAYS):
            d, o, cl = s[i]
            if d < IC_START or i < 1 or s[i - 1][2] <= 0 or o <= 0:
                continue
            gap = o / s[i - 1][2] - 1
            fwd_open = s[i + LABEL_DAYS][2] / o - 1
            fwd_close = s[i + LABEL_DAYS][2] / cl - 1
            daily[d]["gap"][c] = gap
            daily[d]["fwd_open"][c] = fwd_open
            daily[d]["fwd_close"][c] = fwd_close
    print(f"gap_open 因子 IC（{IC_START}~, 样本 {len(daily)} 天）")
    print("-" * 60)
    for tag, key in [("开盘买入 open->t+10", "fwd_open"), ("收盘买入 close->t+10", "fwd_close")]:
        ics = []
        for d in sorted(daily):
            F = daily[d]
            codes = [c for c in F["gap"] if c in F[key]]
            if len(codes) < 30:
                continue
            ic = spearman([F["gap"][c] for c in codes], [F[key][c] for c in codes])
            if not np.isnan(ic):
                ics.append(ic)
        ics = np.asarray(ics)
        print(f"{tag:<28} IC {ics.mean()*100:+.2f}% | ICIR {ics.mean()/(ics.std(ddof=1)+1e-12):+.2f} | 正占比 {(ics>0).mean()*100:.0f}%")


if __name__ == "__main__":
    main()
