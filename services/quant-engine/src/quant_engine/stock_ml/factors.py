# -*- coding: utf-8 -*-
"""stock_ml 因子构建：全历史横截面因子快照 + forward 标签（写 factors.db）。

从 stock_ml/__init__.py 拆出，行为不变；对外仍通过 `from quant_engine import stock_ml` 使用。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .common import (
    FACTOR_COLUMNS, LABEL_COLUMNS, SCHEMA, _ensure_factors_columns, _forward_labels,
    _rolling_corr, connect,
)


# ---------------------------------------------------------------- 因子构建

def _pit_eligible(trade_date: str, list_date: str, min_list_days: int,
                  day_status: dict[str, Any] | None, has_status_history: bool,
                  current_name: str = "") -> bool:
    """仅使用样本日可知信息判断股票池资格。"""
    if list_date and len(list_date) == 8 and list_date.isdigit():
        from datetime import datetime as _dt
        if (_dt.strptime(trade_date, "%Y%m%d") - _dt.strptime(list_date, "%Y%m%d")).days < min_list_days:
            return False
    if has_status_history:
        if day_status is None or day_status.get("is_st") or day_status.get("is_suspended"):
            return False
        if day_status.get("list_status") not in (None, "", "L"):
            return False
    elif any(p in current_name.upper() for p in ("ST", "退")):
        return False
    return True

def build_factors(market_path: Path, factors_path: Path, finance_path: Path | None = None,
                  parameters: dict[str, Any] | None = None,
                  progress: Callable[[float], None] = lambda p: None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """全历史横截面因子（daily_bars+daily_basic 窗口 SQL + financial_indicator 最近报告对齐）+ forward 标签。
    过滤与静态因子一致（history>=120、close>=2、20日均成交额>=5000万元、上市>=120天、排除 ST/退）。
    全量重建幂等；640 万行约 3-8 分钟。按 code 分批，内存可控。"""
    params = parameters or {}
    min_history = int(params.get("minHistory", 120))
    min_close = float(params.get("minClose", 2))
    min_amount = float(params.get("minAmount20", 50000))
    min_list_days = int(params.get("minListDays", 120))
    incremental = bool(params.get("incremental", False))
    con = connect(factors_path)
    mkt = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    mkt.row_factory = sqlite3.Row
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-200000")
        progress(3)
        # 0) 增量模式：只重建最新日（+150 日窗口缓冲），幂等
        rebuild_from: str | None = None
        if incremental:
            have = con.execute("SELECT MAX(trade_date) FROM stock_ml_factors").fetchone()[0]
            if have:
                idx = mkt.execute(
                    "SELECT trade_date FROM daily_bars WHERE trade_date>=? ORDER BY trade_date LIMIT 1",
                    (have,)).fetchone()
                if idx is None:
                    progress(100)
                    return {"incremental": True, "skipped": True, "latestDate": have}
                rebuild_from = have
        window_filter = ""
        win_params: tuple = ()
        if rebuild_from:
            # 从已有最大日向前 150 个交易日作为窗口缓冲（因子需要前 120 日）
            base = mkt.execute(
                "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 1 OFFSET 150",
                (rebuild_from,)).fetchone()
            cutoff = base[0] if base else rebuild_from
            window_filter = " WHERE d.trade_date >= ?"
            win_params = (cutoff,)
        # 1) 窗口因子（SQLite 侧，磁盘临时，避免内存峰值）
        WINDOW_SQL = """
        CREATE TEMP TABLE _win AS
        WITH base AS (
          SELECT d.code, d.trade_date, d.close, d.high, d.low, d.pct_chg, d.amount, d.volume,
                 b.turnover_rate, b.pe_ttm, b.pb, b.total_mv, b.volume_ratio, b.circ_mv, b.ps_ttm, b.dv_ttm
          FROM daily_bars d
          LEFT JOIN daily_basic b ON b.code=d.code AND b.trade_date=d.trade_date
        """ + window_filter + """)
        SELECT code, trade_date, close,
          EXP(SUM(CASE WHEN pct_chg IS NOT NULL AND pct_chg>-100 THEN LN(1+pct_chg/100.0) END)
              OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW))-1 momentum20,
          EXP(SUM(CASE WHEN pct_chg IS NOT NULL AND pct_chg>-100 THEN LN(1+pct_chg/100.0) END)
              OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW))-1 momentum60,
          EXP(SUM(CASE WHEN pct_chg IS NOT NULL AND pct_chg>-100 THEN LN(1+pct_chg/100.0) END)
              OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 119 PRECEDING AND CURRENT ROW))-1 momentum120,
          AVG(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) ma20,
          AVG(close) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) ma60,
          MAX(high) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) high60,
          AVG(pct_chg) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) m20,
          AVG(pct_chg*pct_chg) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) m20sq,
          AVG(turnover_rate) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) turnover20,
          AVG(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) amount20,
          pe_ttm peTtm, pb, total_mv mktCap,
          COALESCE(volume_ratio,
                   volume / NULLIF(AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING), 0)) volumeRatio,
          AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)
            / NULLIF(AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 23 PRECEDING AND CURRENT ROW), 0) volSurge3,
          AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
            / NULLIF(AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 24 PRECEDING AND CURRENT ROW), 0) volSurge5,
          amount / NULLIF(AVG(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 6 PRECEDING AND 1 PRECEDING), 0) amountRatio5,
          close / NULLIF(MAX(high) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) - 1 distHigh20,
          CASE WHEN close >= MAX(high) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) THEN 1 ELSE 0 END breakout60,
          AVG(turnover_rate) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
            / NULLIF(AVG(turnover_rate) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW), 0) turnoverRatio5,
          AVG((high - low) / NULLIF(close, 0)) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) amplitude20,
          circ_mv / NULLIF(total_mv, 0) floatRatio,
          ps_ttm psTtm, dv_ttm dvTtm,
          volume, pct_chg,
          ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date DESC) rn
        FROM base
        """
        mkt.execute("PRAGMA temp_store=FILE")
        mkt.execute(WINDOW_SQL, win_params)
        mkt.execute("CREATE INDEX _win_idx ON _win(code, trade_date)")
        mkt.commit()
        progress(12)
        # 2) 财务最近报告（ann_date<=trade_date）→ 内存：code -> [(ann_date, metrics...)]
        fin = None
        if finance_path:
            fin_con = sqlite3.connect(f"file:{finance_path.as_posix()}?mode=ro", uri=True)
            fin_con.row_factory = sqlite3.Row
            try:
                # 同一公告日可能同时披露年报和一季报。必须显式选择当日可知的
                # 最新报告期，不能依赖SQLite未承诺的同键返回顺序。
                fin_rows = fin_con.execute(
                    "WITH ranked AS (SELECT *,ROW_NUMBER() OVER ("
                    "PARTITION BY code,ann_date ORDER BY end_date DESC) rn "
                    "FROM financial_indicator WHERE ann_date IS NOT NULL) "
                    "SELECT code,ann_date,roe,netprofit_yoy,debt_to_assets,roa,gross_margin,"
                    "revenue_yoy,ocf_yoy,current_ratio,net_margin,q_sales_yoy,raw_json "
                    "FROM ranked WHERE rn=1 ORDER BY code,ann_date").fetchall()
            finally:
                fin_con.close()
            fin = {}
            for row in fin_rows:
                _extra = {}
                if row["raw_json"]:
                    try:
                        import json as _json
                        _raw = _json.loads(row["raw_json"])
                        _extra = {
                            "assetsTurn": _raw.get("assets_turn"),
                            "roeYoy": _raw.get("roe_yoy"),
                            "quickRatio": _raw.get("quick_ratio"),
                        }
                    except Exception:
                        pass
                fin.setdefault(row["code"], []).append((
                    row["ann_date"], row["roe"], row["netprofit_yoy"], row["debt_to_assets"],
                    row["roa"], row["gross_margin"], row["revenue_yoy"], row["ocf_yoy"], row["current_ratio"],
                    row["net_margin"], row["q_sales_yoy"],
                    _extra.get("assetsTurn"), _extra.get("roeYoy"), _extra.get("quickRatio")))
            progress(18)
        # 3) 上市日期（security_master.list_date）
        list_dates = {r["code"]: r["list_date"] for r in mkt.execute("SELECT code, list_date FROM security_master")}
        names = {r["code"]: (r["name"] or "") for r in mkt.execute("SELECT code, name FROM security_master")}
        market_tables = {r[0] for r in mkt.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_status_history = "security_status_history" in market_tables
        has_st_history = "stock_st_history" in market_tables
        has_adjustments = "adjustment_factors" in market_tables
        progress(20)
        # 4) 按 code 分批组装
        codes = [r["code"] for r in mkt.execute("SELECT DISTINCT code FROM _win")]
        history_counts = {r["code"]: r["n"] for r in mkt.execute("SELECT code, COUNT(*) n FROM daily_bars GROUP BY code")}
        all_dates = [r["trade_date"] for r in mkt.execute("SELECT DISTINCT trade_date FROM _win ORDER BY trade_date")]
        day_index = {d: i for i, d in enumerate(all_dates)}
        if incremental:
            # 增量：重插 _win 全部日期（150 日窗口缓冲，forward 标签在窗口内完整）
            if not all_dates:
                progress(100)
                return {"incremental": True, "skipped": True, "latestDate": rebuild_from}
            del_from = all_dates[0]
            con.execute("DELETE FROM stock_ml_factors WHERE trade_date>=?", (del_from,))
        else:
            con.execute("DELETE FROM stock_ml_factors")
        cols = ["trade_date", "code", "close", *FACTOR_COLUMNS, *LABEL_COLUMNS]
        batch: list[list] = []
        inserted = 0
        total_codes = len(codes)
        for ci, code in enumerate(codes):
            if cancelled():
                raise RuntimeError("stock_ml factor generation cancelled")
            rows = [dict(r) for r in mkt.execute("SELECT * FROM _win WHERE code=?", (code,))]
            rows.sort(key=lambda r: r["trade_date"])
            # daily_basic 通常晚于实时日线到达；使用截至样本日最近一次已知值，避免14:40当日
            # 市值/估值全空，同时保持严格PIT（绝不向后取未来值）。
            last_basic: dict[str, float] = {}
            for row in rows:
                for key in ("peTtm", "pb", "mktCap", "psTtm", "dvTtm", "floatRatio"):
                    if row.get(key) is not None:
                        last_basic[key] = row[key]
                    elif key in last_basic:
                        row[key] = last_basic[key]
            # 滚动量价相关（每只一次 O(n)）
            _corr20 = _rolling_corr([r.get("volume") for r in rows], [r.get("pct_chg") for r in rows])
            if history_counts.get(code, 0) < min_history:
                continue
            # 标签统一使用复权价，避免除权除息制造虚假收益；缺复权因子的日期不产出标签。
            adj_series = ([(r["trade_date"], float(r["adj_factor"])) for r in mkt.execute(
                "SELECT trade_date,adj_factor FROM adjustment_factors WHERE code=? ORDER BY trade_date", (code,))]
                if has_adjustments else [])
            ai = -1
            for row in rows:
                while ai + 1 < len(adj_series) and adj_series[ai + 1][0] <= row["trade_date"]:
                    ai += 1
                factor = adj_series[ai][1] if ai >= 0 else None
                row["label_close"] = (float(row["close"]) * factor
                                      if row.get("close") is not None and factor is not None else None)
            status_series = ([dict(r) for r in mkt.execute(
                "SELECT trade_date,is_st,is_suspended,list_status FROM security_status_history "
                "WHERE code=? ORDER BY trade_date", (code,))] if has_status_history else [])
            st_dates = ({r[0] for r in mkt.execute(
                "SELECT trade_date FROM stock_st_history WHERE code=?", (code,))}
                if has_st_history else set())
            si = -1
            name = names.get(code, "").upper()
            list_date = list_dates.get(code) or ""
            fin_series = fin.get(code, []) if fin else []
            fi = 0
            for i, r in enumerate(rows):
                # PIT股票池：上市天数和交易状态都按当前样本日判断，不能用当前名称回看历史。
                while si + 1 < len(status_series) and status_series[si + 1]["trade_date"] <= r["trade_date"]:
                    si += 1
                day_status = status_series[si] if si >= 0 else None
                if day_status is not None and has_st_history:
                    day_status = dict(day_status)
                    day_status["is_st"] = 1 if r["trade_date"] in st_dates else 0
                if not _pit_eligible(r["trade_date"], list_date, min_list_days,
                                     day_status, has_status_history, name):
                    continue
                close = r["close"]
                if close is None or close < min_close:
                    continue
                if r["amount20"] is None or r["amount20"] < min_amount:
                    continue
                while fi + 1 < len(fin_series) and fin_series[fi + 1][0] <= r["trade_date"]:
                    fi += 1
                fv = fin_series[fi] if fin_series and fin_series[fi][0] <= r["trade_date"] else None
                m20, m20sq = r["m20"], r["m20sq"]
                variance = max(0.0, m20sq - m20 * m20) if (m20 is not None and m20sq is not None) else None
                ma20, ma60, high60 = r["ma20"], r["ma60"], r["high60"]
                item = {
                    "trade_date": r["trade_date"], "code": code, "close": close,
                    "momentum20": r["momentum20"], "momentum60": r["momentum60"], "momentum120": r["momentum120"],
                    "trend": (ma20 / ma60 - 1) if (ma20 and ma60) else None,
                    "volatility20": (variance ** 0.5) * (250 ** 0.5) / 100 if variance is not None else None,
                    "turnover20": r["turnover20"],
                    "drawdown60": (close / high60 - 1) if (high60 and close) else None,
                    "peTtm": r["peTtm"], "pb": r["pb"], "mktCap": r["mktCap"],
                    "roe": fv[1] if fv else None, "netprofitYoy": fv[2] if fv else None,
                    "debtToAssets": fv[3] if fv else None, "roa": fv[4] if fv else None,
                    "grossMargin": fv[5] if fv else None, "revenueYoy": fv[6] if fv else None,
                    "ocfYoy": fv[7] if fv else None, "currentRatio": fv[8] if fv else None,
                    "volumeRatio": r["volumeRatio"], "volSurge3": r["volSurge3"],
                    "volSurge5": r["volSurge5"], "amountRatio5": r["amountRatio5"],
                    "distHigh20": r["distHigh20"], "breakout60": r["breakout60"],
                    "volPriceCorr20": _corr20[i], "turnoverRatio5": r["turnoverRatio5"],
                    "amplitude20": r["amplitude20"], "floatRatio": r["floatRatio"],
                    "psTtm": r["psTtm"], "dvTtm": r["dvTtm"],
                    "netMargin": fv[9] if fv else None, "qSalesYoy": fv[10] if fv else None,
                    "assetsTurn": fv[11] if fv else None, "roeYoy": fv[12] if fv else None,
                    "quickRatio": fv[13] if fv else None,
                    **_forward_labels(rows, i, r.get("label_close"), "label_close"),
                }
                batch.append([item.get(c) for c in cols])
            # 每只即插（1100 行/只 × 22 列，避免 batch 累积过大）
            if batch:
                con.executemany(
                    f"INSERT INTO stock_ml_factors ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})", batch)
                con.commit()
                inserted += len(batch)
                batch = []
            if (ci + 1) % 500 == 0:
                progress(20 + 70 * (ci + 1) / max(1, total_codes))
        if batch:
            con.executemany(
                f"INSERT INTO stock_ml_factors ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})", batch)
            con.commit()
            inserted += len(batch)
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_ml_factors_code_date ON stock_ml_factors(code, trade_date)")
        con.commit()
        progress(100)
        span = con.execute("SELECT MIN(trade_date), MAX(trade_date) FROM stock_ml_factors").fetchone()
        return {"pool": total_codes, "rows": inserted, "factors": FACTOR_COLUMNS,
                "startDate": span[0], "endDate": span[1]}
    finally:
        mkt.close()
        con.close()
