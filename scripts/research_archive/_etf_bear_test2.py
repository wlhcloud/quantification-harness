# -*- coding: utf-8 -*-
"""ETF 动量策略抗熊验证 v2（自算因子，绕过快照覆盖不足）：
从 etf_daily_bars（2021-01 起完整行情）现算动量/趋势/波动因子，全区间回测，拆年段。
- 池子：当前 111 只 + 每交易日要求近 20 日有行情（真实上市状态）
- top5 等权，20 日调仓，与线上同口径
- 对比：无择时 vs 000300 MA20 择时
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20210104"
TOP_N = 5
REBAL_DAYS = 20
MA_WINDOW = 20
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001


def load():
    e = sqlite3.connect("data/etf-quant.db")
    codes = [r[0] for r in e.execute("SELECT DISTINCT ts_code FROM etf_factor_snapshots")]
    e.close()
    m = sqlite3.connect("data/market.db")
    placeholders = ",".join("?" * len(codes))
    rows = m.execute(
        f"SELECT ts_code, trade_date, open, close, pct_chg, vol FROM etf_daily_bars "
        f"WHERE ts_code IN ({placeholders}) AND trade_date>=? ORDER BY ts_code, trade_date",
        codes + [START]).fetchall()
    bench = m.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date>=? ORDER BY trade_date",
        (START,)).fetchall()
    m.close()
    # 按 code 分组时间序列
    series = defaultdict(list)
    for c, d, o, cl, pct, vol in rows:
        series[c].append((d, o, cl, pct, vol))
    return series, {d: c for d, c in bench}


def factor_matrix(series, day, codes):
    """返回 (codes_sorted, F: n x 9 因子矩阵, ohlc dict)"""
    feat = []
    ohlc = {}
    valid = []
    for c in codes:
        s = series[c]
        idxs = [i for i, x in enumerate(s) if x[0] <= day]
        if len(idxs) < 20:
            continue
        i = idxs[-1]
        # 最近 120 天窗口数据
        w = s[max(0, i - 119):i + 1]
        if len(w) < 20:
            continue
        closes = np.asarray([x[2] for x in w], dtype=float)  # close
        pcts = np.asarray([x[3] for x in w], dtype=float)    # pct_chg
        vols = np.asarray([x[4] for x in w], dtype=float)    # vol
        if len(closes) < 20 or np.any(closes <= 0):
            continue
        rets = np.diff(np.log(closes))
        mom20 = closes[-1] / closes[-21] - 1 if len(closes) > 20 else np.nan
        mom60 = closes[-1] / closes[-61] - 1 if len(closes) > 60 else np.nan
        mom120 = closes[-1] / closes[0] - 1 if len(closes) >= 120 else np.nan
        vol20 = float(np.std(rets[-20:], ddof=1)) * np.sqrt(252) if len(rets) >= 20 else np.nan
        dd60 = float((closes[-60:] / np.maximum.accumulate(closes[-60:]) - 1).min()) if len(closes) >= 60 else np.nan
        vr = float(np.mean(vols[-5:]) / np.mean(vols[-20:])) if np.mean(vols[-20:]) > 0 else np.nan
        ma_al = 1.0 if (len(closes) >= 20 and closes[-1] > np.mean(closes[-10:]) > np.mean(closes[-20:])) else 0.0
        ohlc[(c, day)] = (w[-1][1], w[-1][2])  # open, close        feat.append([mom20, mom60, mom120, vol20, dd60, vr, ma_al, closes[-1], np.nanmean(pcts[-20:])])
        valid.append(c)
    return valid, np.asarray(feat, dtype=float), ohlc


def pick_top(feat, codes):
    if feat.ndim == 1:
        feat = feat.reshape(1, -1)
    n = len(codes)
    scores = np.zeros(n, dtype=float)
    # 方向：mom* / ma_al / vr / 量能 越高越好；vol20 / dd60 越低越好
    for j, sign in enumerate([1, 1, 1, -1, -1, 1, 1, 1, 1]):
        vals = feat[:, j] * sign
        order = np.argsort(vals, kind="stable")
        rank = np.empty(n, dtype=float)
        rank[order] = np.arange(n)
        rank[np.isnan(vals)] = n + 1
        scores += rank
    idx = np.argsort(scores, kind="stable")[:TOP_N]
    return [codes[i] for i in idx]


def run(series, bench, codes, regime=False, tag=""):
    dates = sorted(bench)
    closes = np.asarray([bench[d] for d in dates], dtype=float)
    ma20 = {}
    for i, d in enumerate(dates):
        if i + 1 >= MA_WINDOW:
            ma20[d] = float(np.mean(closes[i + 1 - MA_WINDOW:i + 1]))
    cash = 1_000_000.0
    holdings = {}
    nav_curve = []
    for i, day in enumerate(dates):
        bear = bool(regime) and day in ma20 and bench.get(day, 0) < ma20[day]
        rebal = (i % REBAL_DAYS == 0)
        codes_i, feat, ohlc = factor_matrix(series, day, codes)
        if (bear or len(codes_i) < 10) and holdings:
            for c in list(holdings):
                qty, bp = holdings[c]
                px = ohlc.get((c, day), (bp, bp))[1] if (c, day) in ohlc else bp
                cash += qty * px * (1 - COMM - STAMP - SLIP)
                del holdings[c]
        elif rebal and len(codes_i) >= 10:
            target = pick_top(feat, codes_i)
            for c in list(holdings):
                qty, bp = holdings[c]
                px = ohlc.get((c, day), (bp, bp))[1] if (c, day) in ohlc else bp
                cash += qty * px * (1 - COMM - STAMP - SLIP)
                del holdings[c]
            budget = cash / len(target)
            for c in target:
                row = ohlc.get((c, day))
                if not row:
                    continue
                o = row[0]
                qty = int(budget / (o * (1 + COMM + SLIP)) // 100) * 100
                if qty <= 0:
                    continue
                cash -= qty * o * (1 + COMM + SLIP)
                holdings[c] = (qty, o)
        mv = 0.0
        for c, (qty, bp) in holdings.items():
            row = ohlc.get((c, day))
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
    series, bench = load()
    codes = sorted(series.keys())
    run(series, bench, codes, regime=False, tag="ETF动量(自算因子) 无择时")
    run(series, bench, codes, regime=True, tag="ETF动量(自算因子) +MA20")
    b0 = bench[min(bench)]
    bn = bench[max(bench)]
    print(f"[基准沪深300]                 累计 {(bn/b0-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
