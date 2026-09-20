# -*- coding: utf-8 -*-
"""演示回测：6因子 top5 裸持有 vs 加止盈止损(涨3%止盈/-5%止损)。
假设：T+1 次日开盘调仓；持仓期内每日用 high/low 判断触发；触发按止盈/止损价成交。
（演示用途：调仓以当日 open 近似次日 open，两组同口径，对比结论有效）
"""
import sqlite3
import numpy as np

START = "20250601"
BT_START = "20250618"
TOP_N = 5
COMMISSION, STAMP, SLIP = 0.0003, 0.0005, 0.001
TAKE_PROFIT = 0.03
STOP_LOSS = 0.05
FACTORS = ["dvTtm", "floatRatio", "debtToAssets", "ocfYoy", "netprofitYoy", "revenueYoy"]
REBAL_DAYS = 5


def load():
    con = sqlite3.connect("data/factors.db")
    facts = con.execute(
        "SELECT trade_date, code, " + ",".join(FACTORS) +
        " FROM stock_ml_factors WHERE trade_date>=?", (START,)).fetchall()
    con.close()
    by_date = {}
    for row in facts:
        by_date.setdefault(row[0], []).append(row[1:])
    con = sqlite3.connect("data/market.db")
    bars = con.execute(
        "SELECT code, trade_date, open, high, low, close FROM daily_bars WHERE trade_date>=?",
        (START,)).fetchall()
    bench = con.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date>=? ORDER BY trade_date",
        (START,)).fetchall()
    con.close()
    ohlc = {}
    for code, d, o, h, l, c in bars:
        ohlc[(code, d)] = (o, h, l, c)
    return by_date, ohlc, {d: c for d, c in bench}


def pick_top(recs):
    """recs: list of (code, f1..f6)。逐因子升序排名(越小越好)取平均，选 topN。"""
    nf = len(FACTORS)
    scores = []
    for r in recs:
        code = r[0]
        s = 0.0
        cnt = 0
        for j in range(nf):
            v = r[1 + j]
            if v is None:
                continue
            # 该因子下比 v 更小的数量 = 名次（升序）
            rank = sum(1 for r2 in recs if r2[1 + j] is not None and r2[1 + j] < v)
            s += rank
            cnt += 1
        scores.append((code, s / cnt if cnt else 1e18))
    scores.sort(key=lambda x: x[1])
    return [c for c, _ in scores[:TOP_N]]


def run(by_date, ohlc, tp=None, sl=None, tag=""):
    dates = sorted(d for d in by_date if d >= BT_START)
    cash = 1_000_000.0
    holdings = {}
    nav_curve = []
    for i, day in enumerate(dates):
        # 1) 止盈/止损触发（当日 high/low vs 买入价）
        for code in list(holdings):
            qty, bp = holdings[code]
            o, h, l, c = ohlc.get((code, day), (None, None, None, None))
            if o is None:
                continue
            if tp and h is not None and h >= bp * (1 + tp):
                cash += qty * bp * (1 + tp) * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
            elif sl and l is not None and l <= bp * (1 - sl):
                cash += qty * bp * (1 - sl) * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
        # 2) 调仓
        if i % REBAL_DAYS == 0:
            recs = by_date.get(day, [])
            target = pick_top(recs) if recs else [c for c in holdings]
            for code in list(holdings):
                qty, bp = holdings[code]
                o, h, l, c = ohlc.get((code, day), (None, None, None, None))
                px = c if c else bp
                cash += qty * px * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
            if target:
                budget = cash / len(target)
                for code in target:
                    o, h, l, c = ohlc.get((code, day), (None, None, None, None))
                    if not o:
                        continue
                    qty = int(budget / (o * (1 + COMMISSION + SLIP)) // 100) * 100
                    if qty <= 0:
                        continue
                    cash -= qty * o * (1 + COMMISSION + SLIP)
                    holdings[code] = (qty, o)
        # 3) 估值
        mv = 0.0
        for code, (qty, bp) in holdings.items():
            o, h, l, c = ohlc.get((code, day), (None, None, None, None))
            if c:
                mv += qty * c
        nav_curve.append((day, cash + mv))
    navs = np.asarray([v for _, v in nav_curve], dtype=float)
    total = navs[-1] / navs[0] - 1
    rets = navs[1:] / navs[:-1] - 1
    annual = (1 + total) ** (252.0 / len(navs)) - 1
    vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0
    sharpe = annual / vol if vol > 0 else 0
    peak, mdd = -np.inf, 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    print(f"[{tag}] 累计 {total*100:+.1f}%  年化 {annual*100:+.1f}%  Sharpe {sharpe:.2f}  回撤 {mdd*100:.1f}%")
    return navs


def main():
    by_date, ohlc, bench = load()
    run(by_date, ohlc, tp=None, sl=None, tag="裸持有(无规则)      ")
    run(by_date, ohlc, tp=TAKE_PROFIT, sl=None, tag="涨3%止盈          ")
    run(by_date, ohlc, tp=None, sl=STOP_LOSS, tag="跌5%止损          ")
    run(by_date, ohlc, tp=TAKE_PROFIT, sl=STOP_LOSS, tag="涨3%+跌5%双规则  ")
    b0 = bench[BT_START]
    bn = bench[max(bench)]
    print(f"[基准沪深300]       累计 {(bn/b0-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
