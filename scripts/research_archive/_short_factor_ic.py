# -*- coding: utf-8 -*-
"""短线候选因子 IC 体检（2024-08 起，标签 = 未来 10 日收益）：
对每个因子，每交易日计算截面 Spearman IC，输出 IC 均值 / ICIR / 正占比。
候选：动量、量比、换手、开盘缺口、日内收益、市值、波动、MA20 偏离、放量突破得分、量价配合。
"""
import sqlite3
from collections import defaultdict
import numpy as np

START = "20240701"   # 含 20 天动量预热
IC_START = "20240820"
LABEL_DAYS = 10


def load():
    m = sqlite3.connect("data/market.db")
    rows = m.execute(
        "SELECT code, trade_date, open, close, pct_chg, volume, pre_close FROM daily_bars WHERE trade_date>=? ORDER BY code, trade_date",
        (START,)).fetchall()
    bmap = {}
    for r in m.execute("SELECT code, trade_date, turnover_rate, volume_ratio, total_mv FROM daily_basic WHERE trade_date>=?", (START,)):
        bmap[(r[0], r[1])] = (r[2], r[3], r[4])
    m.close()
    ser = defaultdict(list)
    for c, d, o, cl, pct, vol, pc in rows:
        b = bmap.get((c, d), (None, None, None))
        ser[c].append((d, o, cl, pct, vol, pc, b[0], b[1], b[2]))
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
    ra -= ra.mean()
    rb -= rb.mean()
    denom = np.sqrt((ra * ra).sum() * (rb * rb).sum())
    return float((ra * rb).sum() / denom) if denom > 0 else np.nan


def main():
    ser = load()
    # 每只股票：因子时间序列 + forward10
    daily = defaultdict(dict)  # date -> {factor: {code: val}}
    for c, s in ser.items():
        n = len(s)
        dates = [x[0] for x in s]
        closes = np.asarray([x[2] for x in s], dtype=float)
        opens = np.asarray([x[1] for x in s], dtype=float)
        pcts = np.asarray([x[3] for x in s], dtype=float)
        vols = np.asarray([x[4] for x in s], dtype=float)
        pcs = np.asarray([x[5] for x in s], dtype=float)
        turns = np.asarray([x[6] if x[6] is not None else np.nan for x in s], dtype=float)
        vrs = np.asarray([x[7] if x[7] is not None else np.nan for x in s], dtype=float)
        mvs = np.asarray([x[8] if x[8] is not None else np.nan for x in s], dtype=float)
        mom20 = np.full(n, np.nan)
        vol20 = np.full(n, np.nan)
        ma20 = np.full(n, np.nan)
        gap = np.full(n, np.nan)
        intra = np.full(n, np.nan)
        for i in range(n):
            if i >= 20:
                mom20[i] = closes[i] / closes[i - 20] - 1
                vol20[i] = float(np.std(np.diff(np.log(closes[i - 19:i + 1])), ddof=1)) * np.sqrt(252)
                ma20[i] = float(np.mean(closes[i - 19:i + 1]))
            if pcs[i] and pcs[i] > 0:
                gap[i] = opens[i] / pcs[i] - 1
            if opens[i] and opens[i] > 0:
                intra[i] = closes[i] / opens[i] - 1
        dist_ma20 = closes / ma20 - 1
        # 放量突破得分：突破且放量 -> 1，否则 0（近似信号强度：突破度×量比）
        breakout = np.full(n, np.nan)
        for i in range(20, n):
            if closes[i] > ma20[i] and closes[i - 1] <= ma20[i - 1] and 2 <= pcts[i] <= 7:
                vr_i = vrs[i] if not np.isnan(vrs[i]) else 1.0
                breakout[i] = vr_i * pcts[i]
            else:
                breakout[i] = 0.0
        pv = pcts * vrs
        for i in range(n - LABEL_DAYS):
            fwd = closes[i + LABEL_DAYS] / closes[i] - 1
            d = dates[i]
            if d < IC_START:
                continue
            F = daily[d]
            def put(k, v):
                if not np.isnan(v):
                    F.setdefault(k, {})[c] = float(v)
            put("mom20", mom20[i])
            put("vol_ratio", vrs[i])
            put("turnover", turns[i])
            put("gap_open", gap[i])
            put("intraday", intra[i])
            put("size", np.log(mvs[i]) if not np.isnan(mvs[i]) else np.nan)
            put("vol20", vol20[i])
            put("dist_ma20", dist_ma20[i])
            put("breakout", breakout[i])
            put("pv", pv[i])
            F.setdefault("fwd", {})[c] = fwd
    # 逐因子逐日 IC
    factors = ["mom20", "vol_ratio", "turnover", "gap_open", "intraday", "size", "vol20", "dist_ma20", "breakout", "pv"]
    print(f"因子 IC 体检（{IC_START}~ 每交易日截面, 标签=未来{LABEL_DAYS}日收益, 样本 {len(daily)} 天）")
    print("-" * 72)
    print(f"{'因子':<12}{'IC均值':>10}{'ICIR':>9}{'正占比':>9}{'|IC|>0.03占比':>14}")
    res = {}
    for f in factors:
        ics = []
        for d in sorted(daily):
            F = daily[d]
            if f not in F or "fwd" not in F:
                continue
            codes = [c for c in F[f] if c in F["fwd"]]
            if len(codes) < 30:
                continue
            ic = spearman([F[f][c] for c in codes], [F["fwd"][c] for c in codes])
            if not np.isnan(ic):
                ics.append(ic)
        ics = np.asarray(ics)
        ic_mean = float(ics.mean())
        icir = float(ics.mean() / (ics.std(ddof=1) + 1e-12))
        pos = float((ics > 0).mean())
        big = float((np.abs(ics) > 0.03).mean())
        res[f] = (ic_mean, icir, pos, big)
        print(f"{f:<12}{ic_mean*100:>+9.2f}%{icir:>+9.2f}{pos*100:>8.0f}%{big*100:>13.0f}%")
    print("-" * 72)
    print("正 IC=因子值越大未来 10 日越可能涨（动量有效）；负 IC=因子值越大越可能跌（反转有效）")


if __name__ == "__main__":
    main()
