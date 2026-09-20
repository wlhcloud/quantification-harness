"""ETF factor computation (M2, additive; stock pipeline untouched).

Reads etf_daily_bars / index_daily_bars from market.db and computes a
cross-sectional factor frame at each trade date. All features are known at
close of trade_date; forward returns are label-only (never features).

Factor definitions follow AI_QUANT_ETF_SYSTEM_DESIGN.md §33-36.
"""
from __future__ import annotations

import math
import re
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

FACTOR_NAMES = ["mom20", "mom60", "mom120", "rs20", "rs60", "trend_quality", "ma_alignment",
                "vol_ratio", "amount_trend", "volatility20", "drawdown60", "log_amount20",
                "rs60_vol_adj", "mom_accel", "skew20", "drawdown20", "amount_share", "ind_rs"]
LABEL_NAMES = ["forward_5", "forward_20"]


def _load_bars(market_path: Path, codes: list[str]) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """SELECT ts_code, trade_date, close, pct_chg, vol, amount
               FROM etf_daily_bars WHERE ts_code IN (%s) ORDER BY ts_code, trade_date"""
            % ",".join("?" * len(codes)), codes).fetchall()
    finally:
        con.close()
    frame = pd.DataFrame(rows, columns=["ts_code", "trade_date", "close", "pct_chg", "vol", "amount"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame["pct_chg"] = pd.to_numeric(frame["pct_chg"], errors="coerce")
    frame["vol"] = pd.to_numeric(frame["vol"], errors="coerce")
    frame["amount"] = pd.to_numeric(frame["amount"], errors="coerce")
    return frame


def _load_benchmark(market_path: Path, bench: str) -> pd.DataFrame:
    con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT trade_date, close FROM index_daily_bars WHERE ts_code=? ORDER BY trade_date", (bench,)).fetchall()
    finally:
        con.close()
    frame = pd.DataFrame(rows, columns=["trade_date", "close"])
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    return frame.set_index("trade_date")["close"].sort_index()


# ---- M11/T7: ETF -> CSI industry mapping + industry momentum factor (additive) ----
_BROAD_KEYWORDS = ["上证50", "沪深300", "中证500", "中证1000", "中证2000", "国证2000",
                   "创业板", "科创板", "科创", "A500", "红利", "自由现金流", "国债",
                   "债券", "货币", "现金", "央企", "国企", "质量", "价值", "成长",
                   "港股通", "恒生", "沪港深", "MSCI", "金", "贵金属", "商品", "AAA",
                   "综合", "指数增强", "ETF联接", "龙头企业", "200", "500", "1000", "800",
                   "300", "50", "A股", "宽基"]
_INDUSTRY_KEYWORDS = [
    ("能源", ["能源", "煤炭", "石油", "油气", "石化", "资源", "原油", "天然气"]),
    ("材料", ["材料", "化工", "钢铁", "有色", "金属", "建材", "新材料", "稀土", "锂矿", "铜", "铝"]),
    ("工业", ["工业", "机械", "工程机械", "机器人", "军工", "航天", "航空", "国防", "制造",
              "新能源车", "电池", "光伏", "风电", "储能", "电气", "电力设备", "汽车"]),
    ("可选消费", ["可选消费", "家电", "零售", "旅游", "酒店", "传媒", "游戏", "影视", "家居", "智能驾驶"]),
    ("主要消费", ["主要消费", "消费", "白酒", "食品", "饮料", "农业", "养殖", "畜牧", "粮食", "乳业", "猪肉", "酒"]),
    ("医药", ["医药", "医疗", "生物", "创新药", "疫苗", "医美", "中药", "器械", "药"]),
    ("金融地产", ["金融", "银行", "证券", "保险", "地产", "房地产", "非银", "券商"]),
    ("信息", ["信息", "科技", "软件", "芯片", "半导体", "电子", "通信", "计算机", "人工智能",
              "大数据", "云计算", "互联网", "5G", "软件服务", "智能"]),
    ("电信", ["电信", "运营商"]),
    ("公用", ["公用", "电力", "环保", "水务", "燃气"]),
]
_INDUSTRY_INDEX = {"能源": "000928.SH", "材料": "000929.CSI", "工业": "000930.CSI",
                   "可选消费": "000931.CSI", "主要消费": "000932.SH", "医药": "000933.SH",
                   "金融地产": "000934.SH", "信息": "000935.SH", "电信": "000936.CSI",
                   "公用": "000937.CSI"}


def _map_industry(name: str) -> str | None:
    n = (name or "").upper()
    if any(k in n for k in _BROAD_KEYWORDS):
        return None
    for ind, keys in _INDUSTRY_KEYWORDS:
        if any(k in n for k in keys):
            return ind
    return None


def _industry_rs(market_path: Path, bench_code: str = "000300.SH") -> dict[str, pd.Series]:
    """Per-industry relative strength vs benchmark: rel / rel.shift(60) - rel / rel.shift(20)."""
    bench = _load_benchmark(market_path, bench_code)
    out: dict[str, pd.Series] = {}
    con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    try:
        for ind, idx_code in _INDUSTRY_INDEX.items():
            rows = con.execute(
                "SELECT trade_date, close FROM index_daily_bars WHERE ts_code=? ORDER BY trade_date",
                (idx_code,)).fetchall()
            if not rows:
                continue
            idx = pd.DataFrame(rows, columns=["trade_date", "close"]).set_index("trade_date")["close"]
            rel = (idx.reindex(bench.index).ffill() / bench)
            out[ind] = (rel.shift(60) / rel - rel.shift(20) / rel)
    finally:
        con.close()
    return out


def eligible_pool(market_path: Path, pool_cfg: dict[str, Any]) -> list[str]:
    """Eligible ETF codes ranked by average daily amount (liquidity-first)."""
    con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    try:
        rows = con.execute(
            """SELECT e.ts_code, e.name, COALESCE(AVG(b.amount),0) AS amt
               FROM etf_metadata e
               LEFT JOIN etf_daily_bars b ON b.ts_code=e.ts_code
               WHERE e.eligible=1
               GROUP BY e.ts_code
               ORDER BY amt DESC, e.ts_code""").fetchall()
    finally:
        con.close()
    patterns = [re.compile(p) for p in pool_cfg.get("excludeNamePatterns", [])]
    codes = [code for code, name, _ in rows if not any(p.search(name or "") for p in patterns)]
    return codes[: int(pool_cfg.get("maxEligible", 200))]


def compute_factors(market_path: Path, codes: list[str], bench_code: str = "000300.SH",
                    pool_cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """Return one row per (ts_code, trade_date) with factors + forward labels."""
    pool_cfg = pool_cfg or {}
    min_history = int(pool_cfg.get("minHistoryDays", 60))
    bars = _load_bars(market_path, codes)
    if bars.empty:
        return bars
    bench = _load_benchmark(market_path, bench_code)
    bars = bars.sort_values(["ts_code", "trade_date"])

    out: list[pd.DataFrame] = []
    min_amount20 = float(pool_cfg.get("minAvgAmount20", 0))
    for code, group in bars.groupby("ts_code", sort=False):
        group = group.reset_index(drop=True)
        close = group["close"]
        ret = group["pct_chg"] / 100.0
        vol = group["vol"]
        amount = group["amount"]

        frame = pd.DataFrame({"ts_code": code, "trade_date": group["trade_date"], "close": close.values})
        frame["mom20"] = close / close.shift(20) - 1
        frame["mom60"] = close / close.shift(60) - 1
        frame["mom120"] = close / close.shift(120) - 1
        frame["trend_quality"] = close / close.rolling(60).mean() - 1
        frame["ma_alignment"] = ((close > close.rolling(5).mean()) & (close.rolling(5).mean() > close.rolling(20).mean())
                                 & (close.rolling(20).mean() > close.rolling(60).mean())).astype(float)
        frame["vol_ratio"] = vol / vol.rolling(20).mean() - 1
        frame["amount_trend"] = amount.rolling(5).mean() / amount.rolling(20).mean() - 1
        frame["volatility20"] = ret.rolling(20).std() * math.sqrt(250)
        frame["drawdown60"] = close.rolling(60).max() / close - 1
        frame["log_amount20"] = np.log(amount.rolling(20).mean() + 1)
        # relative strength vs benchmark
        frame["bench_ret20"] = _bench_ret(bench, group["trade_date"], 20)
        frame["bench_ret60"] = _bench_ret(bench, group["trade_date"], 60)
        frame["rs20"] = (1 + frame["mom20"]) / (1 + frame["bench_ret20"]) - 1
        frame["rs60"] = (1 + frame["mom60"]) / (1 + frame["bench_ret60"]) - 1
        # M7 extra factors (additive; safe denominators)
        frame["rs60_vol_adj"] = frame["rs60"] / (frame["volatility20"] + 1e-9)
        frame["mom_accel"] = frame["mom20"] - frame["mom60"]
        frame["skew20"] = ret.rolling(20).skew()
        frame["drawdown20"] = close.rolling(20).max() / close - 1
        frame["_amount20"] = amount.rolling(20).mean()
        # forward labels (close-to-close, label only)
        frame["forward_5"] = close.shift(-5) / close - 1
        frame["forward_20"] = close.shift(-20) / close - 1
        # pool filter: require min history and min average amount
        frame["_history"] = group.index
        frame["_avg_amount20"] = amount.rolling(20).mean()
        mask = (frame["_history"] >= min_history - 1)
        if min_amount20 > 0:
            mask &= (frame["_avg_amount20"] >= min_amount20)
        mask &= frame["close"].notna()
        frame = frame.loc[mask].drop(columns=["_history", "_avg_amount20"])
        out.append(frame)

    result = pd.concat(out, ignore_index=True)
    result = result.drop(columns=[c for c in result.columns if c.startswith("bench_ret")])
    # cross-sectional share of 20d average amount (liquidity dominance within pool)
    result["amount_share"] = result["_amount20"] / result.groupby("trade_date")["_amount20"].transform("sum")
    result = result.drop(columns=["_amount20"])
    # M11/T7: industry momentum factor (relative strength of the ETF's CSI industry vs benchmark)
    ind_rs = _industry_rs(market_path, bench_code)
    name_map: dict[str, str | None] = {}
    if ind_rs:
        con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
        try:
            for row in con.execute("SELECT ts_code, name FROM etf_metadata"):
                name_map[row[0]] = _map_industry(row[1])
        finally:
            con.close()
    series = []
    for code, g in result.groupby("ts_code", sort=False):
        ind = name_map.get(code)
        if ind and ind in ind_rs:
            g = g.copy()
            g["ind_rs"] = g["trade_date"].map(ind_rs[ind])
        else:
            g = g.copy()
            g["ind_rs"] = float("nan")
        series.append(g)
    if series:
        result = pd.concat(series, ignore_index=True)
    return result


def _bench_ret(bench: pd.Series, dates: pd.Series, window: int) -> pd.Series:
    aligned = bench.reindex(dates.astype(str).tolist())
    return (aligned / aligned.shift(window) - 1).reset_index(drop=True)
