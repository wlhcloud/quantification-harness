# -*- coding: utf-8 -*-
"""ETF 抗熊验证（真实行为口径）：全区间(2021-04起，含2022全年熊市)。
- 合格池 = 当日有因子快照的 ETF 数，<MIN_POOL 则空仓（2022年初市场ETF不足，策略实际会空仓）
- 11 因子等权排名 top5，20日调仓（与线上 rebalanceDays 一致）
- 对比：无择时 vs 000300 MA20 择时（线上 regimeFilter bearWeight=0 等价）
说明：mom_accel/skew20/amount_share/ind_rs 方向按 direction=1（越高越好）假设。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20210402"
TOP_N = 5
REBAL_DAYS = 20
MIN_POOL = 20
MA_WINDOW = 20
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001
FACTORS = ["mom20", "mom60", "mom120", "ma_alignment", "vol_ratio", "amount_trend",
           "mom_accel", "skew20", "drawdown20", "amount_share", "ind_rs"]
FACTOR_DIRS = {"mom20": 1, "mom60": 1, "mom120": 1, "ma_alignment": 1, "vol_ratio": 1,
               "amount_trend": 1, "mom_accel": 1, "skew20": 1, "drawdown20": -1,
               "amount_share": 1, "ind_rs": 1}


def load():
    con = sqlite3.connect("data/etf-quant.db")
    rows = con.execute(
        "SELECT ts_code, trade_date, " + ",".join(FACTORS) +
        " FROM etf_factor_snapshots WHERE trade_date>=?", (START,)).fetchall()
    con.close()
    by_date = {}
    for r in rows:
        by_date.setdefault(r[1], []).append(r[0:1] + r[2:])
    m = sqlite3.connect("data/market.db")
    codes = sorted({r[0] for r in rows})
    bars = m.execute(
        "SELECT ts_code, trade_date, open, close FROM etf_daily_bars WHERE ts_code IN (%s) AND trade_date>=?"
        % ",".join("?" * len(codes)), codes + [START]).fetchall()
    bench = m.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date>=? ORDER BY trade_date",
        (START,)).fetchall()
    m.close()
    ohlc = {}
    for c, d, o, cl in bars:
        ohlc[(c, d)] = (o, cl)
    return by_date, ohlc, {d: c for d, c in bench}


def pick_top(recs):
    n = len(recs)
    codes = [r[0] for r in recs]
    scores = np.zeros(n, dtype=float)
    for j, f in enumerate(FACTORS):
        vals = np.asarray([r[1 + j] for r in recs], dtype=float)
        if FACTOR_DIRS.get(f, 1) == -1:
            vals = -vals
        order = np.argsort(vals, kind="stable")
        rank = np.empty(n, dtype=float)
        rank[order] = np.arange(n)
        rank[np.isnan(vals)] = n + 1
        scores += rank
    idx = np.argsort(scores, kind="stable")[:TOP_N]
    return [codes[i] for i in idx]


def run(by_date, ohlc, bench, regime=False, tag=""):
    dates = sorted(by_date)
    bdates = sorted(bench)
    closes = np.asarray([bench[d] for d in bdates], dtype=float)
    ma20 = {}
    for i, d in enumerate(bdates):
        if i + 1 >= MA_WINDOW:
            ma20[d] = float(np.mean(closes[i + 1 - MA_WINDOW:i + 1]))
    cash = 1_000_000.0
    holdings = {}
    nav_curve = []
    for i, day in enumerate(dates):
        recs = by_date.get(day, [])
        pool_ok = len(recs) >= MIN_POOL
        bear = bool(regime) and day in ma20 and bench.get(day, 0) < ma20[day]
        rebal = (i % REBAL_DAYS == 0)
        if (bear or not pool_ok) and holdings:
            for code in list(holdings):
                qty, bp = holdings[code]
                px = ohlc.get((code, day), (bp, bp))[1] if (code, day) in ohlc else bp
                cash += qty * px * (1 - COMM - STAMP - SLIP)
                del holdings[code]
        elif rebal and pool_ok:
            target = pick_top(recs)
            for code in list(holdings):
                qty, bp = holdings[code]
                px = ohlc.get((code, day), (bp, bp))[1] if (code, day) in ohlc else bp
                cash += qty * px * (1 - COMM - STAMP - SLIP)
                del holdings[code]
            budget = cash / len(target)
            for code in target:
                row = ohlc.get((code, day))
                if not row:
                    continue
                o = row[0]
                qty = int(budget / (o * (1 + COMM + SLIP)) // 100) * 100
                if qty <= 0:
                    continue
                cash -= qty * o * (1 + COMM + SLIP)
                holdings[code] = (qty, o)
        mv = 0.0
        for code, (qty, bp) in holdings.items():
            row = ohlc.get((code, day))
            if row:
                mv += qty * row[1]
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
    by_date, ohlc, bench = load()
    run(by_date, ohlc, bench, regime=False, tag="ETF动量 无择时      ")
    run(by_date, ohlc, bench, regime=True, tag="ETF动量 + MA20择时  ")
    b0 = bench[min(bench)]
    bn = bench[max(bench)]
    print(f"[基准沪深300]             累计 {(bn/b0-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
