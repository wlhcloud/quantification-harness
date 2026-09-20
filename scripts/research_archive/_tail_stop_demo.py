# -*- coding: utf-8 -*-
"""尾部黑天鹅保护验证：6因子 top5 裸持有 vs 加尾部止损（T+1 真实口径：收盘触发→次日开盘卖出）。
方案A 移动止损 trailing：持仓期最高收盘回撤 >=12% 次日开盘卖
方案B 浮亏止损：相对成本浮亏 >=15% 次日开盘卖
方案C 双规则（A+B 谁先触发）
对比结论有效：四组同口径，且尾部止损用真实 T+1 实现（比 tp/sl 演示更严格）。
"""
import sqlite3
import numpy as np

START = "20250601"
BT_START = "20250618"
TOP_N = 5
COMMISSION, STAMP, SLIP = 0.0003, 0.0005, 0.001
FACTORS = ["dvTtm", "floatRatio", "debtToAssets", "ocfYoy", "netprofitYoy", "revenueYoy"]
REBAL_DAYS = 5
TRAILING_DD = 0.12
LOSS_DD = 0.15


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
    nf = len(FACTORS)
    scores = []
    for r in recs:
        code = r[0]
        s, cnt = 0.0, 0
        for j in range(nf):
            v = r[1 + j]
            if v is None:
                continue
            rank = sum(1 for r2 in recs if r2[1 + j] is not None and r2[1 + j] < v)
            s += rank
            cnt += 1
        scores.append((code, s / cnt if cnt else 1e18))
    scores.sort(key=lambda x: x[1])
    return [c for c, _ in scores[:TOP_N]]


def run(by_date, ohlc, trailing=None, loss=None, tag=""):
    dates = sorted(d for d in by_date if d >= BT_START)
    cash = 1_000_000.0
    holdings = {}
    high_close = {}
    pending = set()
    nav_curve = []
    n_trig = 0
    for i, day in enumerate(dates):
        # 0) 处理昨日尾部止损标记：今日开盘卖出（T+1 真实口径）
        if pending:
            for code in list(holdings):
                if code in pending:
                    qty, bp = holdings[code]
                    o, h, l, c = ohlc.get((code, day), (None, None, None, None))
                    if o:
                        cash += qty * o * (1 - COMMISSION - STAMP - SLIP)
                        del holdings[code]
                        high_close.pop(code, None)
            pending.clear()
        # 1) 调仓（5日）
        if i % REBAL_DAYS == 0:
            recs = by_date.get(day, [])
            target = pick_top(recs) if recs else [c for c in holdings]
            for code in list(holdings):
                qty, bp = holdings[code]
                o, h, l, c = ohlc.get((code, day), (None, None, None, None))
                px = c if c else bp
                cash += qty * px * (1 - COMMISSION - STAMP - SLIP)
                del holdings[code]
                high_close.pop(code, None)
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
                    high_close[code] = o
        # 2) 估值 + 收盘后判断尾部止损
        mv = 0.0
        for code, (qty, bp) in holdings.items():
            o, h, l, c = ohlc.get((code, day), (None, None, None, None))
            if c:
                mv += qty * c
                if c > high_close.get(code, bp):
                    high_close[code] = c
                hit = False
                if trailing and c <= high_close[code] * (1 - trailing):
                    hit = True
                if loss and c <= bp * (1 - loss):
                    hit = True
                if hit:
                    pending.add(code)
                    n_trig += 1
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
    print(f"[{tag}] 累计 {total*100:+.1f}%  年化 {annual*100:+.1f}%  Sharpe {sharpe:.2f}  回撤 {mdd*100:.1f}%  (止损触发 {n_trig} 次)")
    return navs


def main():
    by_date, ohlc, bench = load()
    run(by_date, ohlc, tag="裸持有(无尾部保护)  ")
    run(by_date, ohlc, trailing=TRAILING_DD, tag=f"移动止损{int(TRAILING_DD*100)}%       ")
    run(by_date, ohlc, loss=LOSS_DD, tag=f"浮亏止损{int(LOSS_DD*100)}%         ")
    run(by_date, ohlc, trailing=TRAILING_DD, loss=LOSS_DD, tag="双规则(先触发先卖)   ")
    b0 = bench[BT_START]
    bn = bench[max(bench)]
    print(f"[基准沪深300]       累计 {(bn/b0-1)*100:+.1f}%")


if __name__ == "__main__":
    main()
