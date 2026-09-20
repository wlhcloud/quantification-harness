# -*- coding: utf-8 -*-
"""gap_open 因子十分位单调性验证（收盘买入视角，未来10日）：
若第10组(最大缺口)不是收益最高 -> top5 选票天然选错，IC 正来自中间区间。
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


def main():
    ser = load()
    daily = defaultdict(lambda: {"gap": {}, "fwd": {}})
    for c, s in ser.items():
        n = len(s)
        for i in range(n - LABEL_DAYS):
            d, o, cl = s[i]
            if d < IC_START or i < 1 or s[i - 1][2] <= 0 or o <= 0:
                continue
            daily[d]["gap"][c] = o / s[i - 1][2] - 1
            daily[d]["fwd"][c] = s[i + LABEL_DAYS][2] / cl - 1
    # 十分位组合收益（等权, 收盘买入, 未来10日, 每10日换仓重叠滚动简化：逐日组均收益汇总）
    dec_ret = [0.0] * 10
    dec_n = [0] * 10
    for d in sorted(daily):
        F = daily[d]
        codes = [c for c in F["gap"] if c in F["fwd"]]
        if len(codes) < 100:
            continue
        gaps = np.asarray([F["gap"][c] for c in codes])
        fwds = np.asarray([F["fwd"][c] for c in codes])
        order = np.argsort(gaps)
        n = len(codes)
        for k in range(10):
            seg = order[k * n // 10:(k + 1) * n // 10]
            if len(seg) == 0:
                continue
            dec_ret[k] += float(fwds[seg].mean())
            dec_n[k] += 1
    print(f"gap 十分位（第1组=最小缺口, 第10组=最大缺口, 收盘买未来10日, {IC_START}~）")
    print("-" * 60)
    for k in range(10):
        mean = dec_ret[k] / dec_n[k] if dec_n[k] else 0
        print(f"  D{k+1:<2} 缺口均值区间 | 未来10日收益 {mean*100:+.2f}% (样本 {dec_n[k]} 天)")
    # 再算 top10% 与 bottom10%
    t1 = dec_ret[9] / dec_n[9] if dec_n[9] else 0
    t10 = dec_ret[0] / dec_n[0] if dec_n[0] else 0
    print("-" * 60)
    print(f"  Top 10% 组 {t1*100:+.2f}%  vs  Bottom 10% 组 {t10*100:+.2f}%")


if __name__ == "__main__":
    main()
