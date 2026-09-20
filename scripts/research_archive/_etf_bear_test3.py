# -*- coding: utf-8 -*-
"""ETF 动量策略抗熊验证 v3（动态历史池，真实可执行口径）：
- 池子：每交易日取【当时实际有行情】且近20日均成交额>200万(元)的 ETF（含已清盘/退市），
  排除货币/现金类；这才是 2022 年真实可买的池子。
- 因子自算：mom20/60/120、volatility20(负向)、drawdown60(负向)、vol_ratio、ma_alignment
- top5 等权，20 日调仓；对比 无择时 vs 000300 MA20 择时
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20210104"
TOP_N = 5
REBAL_DAYS = 20
MA_WINDOW = 20
MIN_AMOUNT20 = 200.0  # 万元口径（etf_daily_bars.amount 为万元，线上阈值 2,000,000 元）
COMM, STAMP, SLIP = 0.0003, 0.0005, 0.001


def load():
    m = sqlite3.connect("data/market.db")
    # 排除货币/现金类
    try:
        bad = {r[0] for r in m.execute(
            "SELECT ts_code FROM etf_metadata WHERE name LIKE '%货币%' OR name LIKE '%现金%' "
            "OR name LIKE '%国债%' OR name LIKE '%逆回购%' OR name LIKE '%货基%'")}
    except Exception:
        bad = set()
    rows = m.execute(
        "SELECT ts_code, trade_date, open, close, amount FROM etf_daily_bars WHERE trade_date>=? "
        "ORDER BY ts_code, trade_date", (START,)).fetchall()
    bench = m.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date>=? ORDER BY trade_date",
        (START,)).fetchall()
    m.close()
    series = defaultdict(list)
    for c, d, o, cl, amt in rows:
        if c in bad:
            continue
        series[c].append((d, o, cl, amt))
    return series, {d: c for d, c in bench}


def pool_on(series, day, codes):
    """当日可交易池：有行情、近20日成交额均值过线。返回 (codes, dict[(c,day)]->(o,c))"""
    out, ohlc = [], {}
    for c in codes:
        s = series[c]
        lo, hi = 0, len(s) - 1
        # 二分找 <= day 的最后一个
        if not s or s[0][0] > day:
            continue
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if s[mid][0] <= day:
                lo = mid
            else:
                hi = mid - 1
        i = lo
        w = s[max(0, i - 19):i + 1]
        if len(w) < 20:
            continue
        amt = np.mean([x[3] for x in w])
        if amt < MIN_AMOUNT20:
            continue
        out.append(c)
        ohlc[(c, day)] = (w[-1][1], w[-1][2])
    return out, ohlc


def factor_on(series, day, codes):
    """返回 (codes, feat n x 7)"""
    feat, valid = [], []
    for c in codes:
        s = series[c]
        lo, hi = 0, len(s) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if s[mid][0] <= day:
                lo = mid
            else:
                hi = mid - 1
        i = lo
        w = s[max(0, i - 119):i + 1]
        if len(w) < 20:
            continue
        closes = np.asarray([x[2] for x in w], dtype=float)
        if np.any(np.isnan(closes)) or np.any(closes <= 0):
            continue
        rets = np.diff(np.log(closes))
        mom20 = closes[-1] / closes[-21] - 1 if len(closes) > 20 else np.nan
        mom60 = closes[-1] / closes[-61] - 1 if len(closes) > 60 else np.nan
        mom120 = closes[-1] / closes[0] - 1 if len(closes) >= 120 else np.nan
        vol20 = float(np.std(rets[-20:], ddof=1)) * np.sqrt(252) if len(rets) >= 20 else np.nan
        dd60 = float((closes[-60:] / np.maximum.accumulate(closes[-60:]) - 1).min()) if len(closes) >= 60 else np.nan
        vr = float(np.mean([x[3] for x in w[-5:]]) / max(np.mean([x[3] for x in w[-20:]]), 1e-9)) if len(w) >= 20 else np.nan
        ma_al = 1.0 if (len(closes) >= 20 and closes[-1] > np.mean(closes[-10:]) > np.mean(closes[-20:])) else 0.0
        feat.append([mom20, mom60, mom120, vol20, dd60, vr, ma_al])
        valid.append(c)
    if not feat:
        return [], np.zeros((0, 7))
    return valid, np.asarray(feat, dtype=float)


def pick_top(feat, codes):
    n = len(codes)
    scores = np.zeros(n, dtype=float)
    for j, sign in enumerate([1, 1, 1, -1, -1, 1, 1]):
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
        codes_i, ohlc = pool_on(series, day, codes)
        if (bear or len(codes_i) < 10) and holdings:
            for c in list(holdings):
                qty, bp = holdings[c]
                px = ohlc.get((c, day), (bp, bp))[1]
                cash += qty * px * (1 - COMM - STAMP - SLIP)
                del holdings[c]
        elif i % REBAL_DAYS == 0 and len(codes_i) >= 10:
            fc, feat = factor_on(series, day, codes)
            if len(fc) >= 10:
                target = pick_top(feat, fc)
                for c in list(holdings):
                    qty, bp = holdings[c]
                    px = ohlc.get((c, day), (bp, bp))[1]
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
    print(f"历史池总代码数: {len(codes)}")
    run(series, bench, codes, regime=False, tag="ETF动量(动态池) 无择时  ")
    run(series, bench, codes, regime=True, tag="ETF动量(动态池) +MA20   ")
    b0 = bench[min(bench)]
    bn = bench[max(bench)]
    print(f"[基准沪深300]             累计 {(bn/b0-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
