# -*- coding: utf-8 -*-
"""6因子抗熊验证（流式版，低内存）：全区间(20210802起，含2022熊市)固定6因子打分 top5 回测，拆年段。
对比：裸跑 vs 沪深300 MA20 择时（与线上 regimeFilter 同口径）。
逐日从 SQLite 拉当日因子与行情，O(单日规模) 内存，适合小内存机器。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20210802"
TOP_N = 5
COMMISSION, STAMP, SLIP = 0.0003, 0.0005, 0.001
FACTORS = ["dvTtm", "floatRatio", "debtToAssets", "ocfYoy", "netprofitYoy", "revenueYoy"]
REBAL_DAYS = 5
MA_WINDOW = 20


def load():
    fcon = sqlite3.connect("data/factors.db")
    mcon = sqlite3.connect("data/market.db")
    dates = [r[0] for r in fcon.execute(
        "SELECT DISTINCT trade_date FROM stock_ml_factors WHERE trade_date>=? ORDER BY trade_date", (START,))]
    bench_rows = mcon.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date>=? ORDER BY trade_date",
        (START,)).fetchall()
    bench = {d: c for d, c in bench_rows}
    return fcon, mcon, dates, bench


def pick_top(recs):
    nf = len(FACTORS)
    n = len(recs)
    codes = [r[0] for r in recs]
    scores = np.zeros(n, dtype=float)
    for j in range(nf):
        vals = np.asarray([r[1 + j] for r in recs], dtype=float)
        order = np.argsort(vals, kind="stable")
        rank = np.empty(n, dtype=float)
        rank[order] = np.arange(n)
        rank[np.isnan(vals)] = n + 1
        scores += rank
    idx = np.argsort(scores, kind="stable")[:TOP_N]
    return [codes[i] for i in idx]


def run(fcon, mcon, dates, bench, regime=False, tag=""):
    cash = 1_000_000.0
    holdings = {}
    nav_curve = []
    closes = np.asarray([bench[d] for d in sorted(bench)], dtype=float)
    bdates = sorted(bench)
    ma20 = {}
    for i, d in enumerate(bdates):
        if i + 1 >= MA_WINDOW:
            ma20[d] = float(np.mean(closes[i + 1 - MA_WINDOW:i + 1]))
    for i, day in enumerate(dates):
        bear = bool(regime) and day in ma20 and bench.get(day, 0) < ma20[day]
        if bear:
            for code in list(holdings):
                qty, bp = holdings[code]
                row = mcon.execute("SELECT close FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
                px = row[0] if row else bp
                cash += qty * px * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
        elif i % REBAL_DAYS == 0:
            recs = fcon.execute(
                "SELECT code, " + ",".join(FACTORS) + " FROM stock_ml_factors WHERE trade_date=?",
                (day,)).fetchall()
            target = pick_top(recs) if recs else [c for c in holdings]
            for code in list(holdings):
                qty, bp = holdings[code]
                row = mcon.execute("SELECT close FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
                px = row[0] if row else bp
                cash += qty * px * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
            if target:
                budget = cash / len(target)
                for code in target:
                    row = mcon.execute("SELECT open FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
                    if not row:
                        continue
                    o = row[0]
                    qty = int(budget / (o * (1 + COMMISSION + SLIP)) // 100) * 100
                    if qty <= 0:
                        continue
                    cash -= qty * o * (1 + COMMISSION + SLIP)
                    holdings[code] = (qty, o)
        mv = 0.0
        for code, (qty, bp) in holdings.items():
            row = mcon.execute("SELECT close FROM daily_bars WHERE code=? AND trade_date=?", (code, day)).fetchone()
            if row:
                mv += qty * row[0]
        nav_curve.append((day, cash + mv))
    navs = np.asarray([v for _, v in nav_curve], dtype=float)
    years = defaultdict(list)
    for d, v in nav_curve:
        years[d[:4]].append(v)
    print(f"[{tag}]")
    print(f"  总区间 {dates[0]}~{dates[-1]} 累计 {(navs[-1]/navs[0]-1)*100:+.1f}%")
    for y in sorted(years):
        seg = np.asarray(years[y])
        print(f"    {y} 年: {(seg[-1]/seg[0]-1)*100:+.1f}%")
    total = navs[-1] / navs[0] - 1
    rets = navs[1:] / navs[:-1] - 1
    annual = (1 + total) ** (252.0 / len(navs)) - 1
    vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0
    sharpe = annual / vol if vol > 0 else 0
    peak, mdd = -np.inf, 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    print(f"  年化 {annual*100:+.1f}%  Sharpe {sharpe:.2f}  最大回撤 {mdd*100:.1f}%")


def main():
    fcon, mcon, dates, bench = load()
    run(fcon, mcon, dates, bench, regime=False, tag="6因子 裸跑(无择时)  ")
    run(fcon, mcon, dates, bench, regime=True, tag="6因子 + MA20择时  ")
    b0 = bench[min(bench)]
    bn = bench[max(bench)]
    print(f"[基准沪深300]         累计 {(bn/b0-1)*100:+.1f}%")
    fcon.close()
    mcon.close()


if __name__ == "__main__":
    main()
