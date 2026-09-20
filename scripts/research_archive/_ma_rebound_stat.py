# -*- coding: utf-8 -*-
"""统计验证：跌破 N 日均线后，次日/3日/5日是否大概率反弹。
事件定义（首次跌破）：前收 >= 前 MA_N 且 今收 < 今 MA_N。
按 000300 相对自身 MA20 分牛(多)/熊(空)市场。
"""
import sqlite3

WINDOWS = [2, 5, 10, 20, 60]


def load_regime():
    con = sqlite3.connect("data/market.db")
    rows = con.execute(
        "SELECT trade_date, close FROM index_daily_bars WHERE ts_code='000300.SH' ORDER BY trade_date").fetchall()
    con.close()
    closes = {d: c for d, c in rows}
    dates = sorted(closes)
    regime = {}
    n = len(dates)
    for i, d in enumerate(dates):
        if i < 19:
            regime[d] = 1
            continue
        ma = sum(closes[dates[j]] for j in range(i - 19, i + 1)) / 20.0
        regime[d] = 1 if closes[d] >= ma else 0
    return regime


def stat(events, regime):
    """events: list of (trade_date, r1, r3, r5)。返回汇总 dict。"""
    out = {}
    for tag, mask in [("全市场", None), ("牛市(000300>MA20)", 1), ("熊市(000300<MA20)", 0)]:
        if mask is None:
            sel = events
        else:
            sel = [e for e in events if regime.get(e[0], 1) == mask]
        if not sel:
            out[tag] = None
            continue
        n = len(sel)
        up1 = sum(1 for e in sel if e[1] > 0) / n
        mean1 = sum(e[1] for e in sel) / n
        med1 = sorted(e[1] for e in sel)[n // 2]
        up3 = sum(1 for e in sel if e[2] > 0) / n
        mean3 = sum(e[2] for e in sel) / n
        up5 = sum(1 for e in sel if e[3] > 0) / n
        mean5 = sum(e[3] for e in sel) / n
        out[tag] = dict(n=n, up1=up1, mean1=mean1, med1=med1, up3=up3, mean3=mean3, up5=up5, mean5=mean5)
    return out


def main():
    regime = load_regime()
    con = sqlite3.connect("data/factors.db")
    # 全样本基准：任意交易日的次日上涨概率（不分事件）
    base = con.execute("""
        WITH f AS (
          SELECT code, trade_date, close,
                 LEAD(close) OVER (PARTITION BY code ORDER BY trade_date) AS nc
          FROM stock_ml_factors
        ) SELECT trade_date, (nc/close-1) FROM f WHERE nc IS NOT NULL""").fetchall()
    nb = len(base)
    upb = sum(1 for r in base if r[1] > 0) / nb
    meanb = sum(r[1] for r in base) / nb
    print(f"全样本基准(无事件)：{nb} 次，次日上涨概率 {upb*100:.1f}%，平均收益 {meanb*100:+.2f}%\n")

    for N in WINDOWS:
        q = f"""
        WITH m AS (
          SELECT code, trade_date, close,
                 AVG(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN {N-1} PRECEDING AND CURRENT ROW) AS ma
          FROM stock_ml_factors
        ),
        f AS (
          SELECT code, trade_date, close, ma,
                 LAG(close) OVER (PARTITION BY code ORDER BY trade_date) AS pc,
                 LAG(ma) OVER (PARTITION BY code ORDER BY trade_date) AS pma,
                 LEAD(close) OVER (PARTITION BY code ORDER BY trade_date) AS nc,
                 LEAD(close,3) OVER (PARTITION BY code ORDER BY trade_date) AS nc3,
                 LEAD(close,5) OVER (PARTITION BY code ORDER BY trade_date) AS nc5
          FROM m
        )
        SELECT trade_date, (nc/close-1), (nc3/close-1), (nc5/close-1)
        FROM f WHERE ma IS NOT NULL AND pma IS NOT NULL AND pc >= pma AND close < ma
                AND nc IS NOT NULL AND nc3 IS NOT NULL AND nc5 IS NOT NULL
        """
        events = con.execute(q).fetchall()
        res = stat(events, regime)
        print(f"=== 跌破 {N} 日线（首次跌破）===")
        for tag, r in res.items():
            if r is None:
                print(f"  {tag}: 无样本")
                continue
            print(f"  {tag}: 事件 {r['n']:,} 次 | 次日上涨概率 {r['up1']*100:.1f}% 平均 {r['mean1']*100:+.2f}% 中位 {r['med1']*100:+.2f}% | "
                  f"3日上涨概率 {r['up3']*100:.1f}%(平均 {r['mean3']*100:+.2f}%) | 5日上涨概率 {r['up5']*100:.1f}%(平均 {r['mean5']*100:+.2f}%)")
        print()
    con.close()


if __name__ == "__main__":
    main()
