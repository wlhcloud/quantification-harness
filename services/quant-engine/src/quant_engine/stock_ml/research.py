# -*- coding: utf-8 -*-
"""Stock factor research helpers: registry + cross-sectional RankIC stats."""
from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np

from .common import (
    FACTOR_COLUMNS, INDUSTRY_FACTOR_COLUMNS, LABEL_COLUMNS, MONEY_FLOW_FACTOR_COLUMNS,
    SCHEMA, _assert_label_supported, _ensure_factors_columns, _now, connect,
)


FACTOR_CATEGORIES: dict[str, str] = {
    "momentum20": "momentum", "momentum60": "momentum", "momentum120": "momentum",
    "distHigh20": "momentum", "breakout60": "momentum",
    "trend": "trend", "ma5Bias": "trend", "ma20Bias": "trend", "ma20Slope5": "trend",
    "rsi14": "technical", "macdDif": "technical", "macdDea": "technical",
    "macdHist": "technical", "bollingerPos20": "technical",
    "volatility20": "risk", "drawdown60": "risk", "amplitude20": "risk",
    "turnover20": "liquidity", "volumeRatio": "liquidity", "volSurge3": "liquidity",
    "volSurge5": "liquidity", "amountRatio5": "liquidity", "turnoverRatio5": "liquidity",
    "volPriceCorr20": "liquidity", "floatRatio": "liquidity",
    "peTtm": "valuation", "pb": "valuation", "psTtm": "valuation", "dvTtm": "valuation",
    "mktCap": "size",
    "roe": "quality", "roa": "quality", "grossMargin": "quality", "netMargin": "quality",
    "revenueYoy": "growth", "netprofitYoy": "growth", "qSalesYoy": "growth",
    "ocfYoy": "quality", "debtToAssets": "leverage", "currentRatio": "quality",
    "assetsTurn": "quality", "roeYoy": "growth", "quickRatio": "quality",
}

NEGATIVE_DIRECTION = {"volatility20", "drawdown60", "peTtm", "pb", "psTtm", "debtToAssets", "mktCap"}


def _rank(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i + 1
        while j < len(order) and values[order[j]] == values[order[i]]:
            j += 1
        avg = (i + j - 1) / 2.0 + 1.0
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    pairs = [(float(x), float(y)) for x, y in zip(xs, ys)
             if x is not None and y is not None and math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(pairs) < 5:
        return None
    rx = _rank([p[0] for p in pairs])
    ry = _rank([p[1] for p in pairs])
    mx = sum(rx) / len(rx)
    my = sum(ry) / len(ry)
    cov = sum((x - mx) * (y - my) for x, y in zip(rx, ry))
    vx = sum((x - mx) ** 2 for x in rx)
    vy = sum((y - my) ** 2 for y in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def _factor_source(name: str) -> tuple[str, str]:
    if name in FACTOR_COLUMNS:
        return "stock_ml_factors", name
    if name in INDUSTRY_FACTOR_COLUMNS:
        return "stock_industry_factors", name
    if name in MONEY_FLOW_FACTOR_COLUMNS:
        return "stock_money_flow_factors", name
    return "stock_ml_factors", name


def _factor_category(name: str) -> str:
    if name.startswith("industry_") or name == "stock_vs_industry_mom20":
        return "industry"
    if name.startswith("money_flow") or name in {
        "volume_price_div", "turnover_surge", "large_move_volume",
        "close_position", "close_position_5d", "up_volume_ratio",
    }:
        return "money_flow"
    return FACTOR_CATEGORIES.get(name, "custom")


def build_factor_registry(factors_path, features: list[str] | None = None) -> dict[str, Any]:
    """Upsert known factors into stock_factor_registry and return grouped metadata."""
    names = list(dict.fromkeys(features or [
        *FACTOR_COLUMNS, *INDUSTRY_FACTOR_COLUMNS, *MONEY_FLOW_FACTOR_COLUMNS,
    ]))
    now = _now()
    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        for name in names:
            table, column = _factor_source(name)
            con.execute(
                "INSERT INTO stock_factor_registry "
                "(factor_name,category,source_table,value_column,default_direction,enabled,description,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(factor_name) DO UPDATE SET "
                "category=excluded.category,source_table=excluded.source_table,"
                "value_column=excluded.value_column,default_direction=excluded.default_direction,"
                "updated_at=excluded.updated_at",
                (name, _factor_category(name), table, column,
                 -1 if name in NEGATIVE_DIRECTION else 1, 1, "", now))
        con.commit()
        rows = con.execute(
            "SELECT factor_name,category,source_table,value_column,default_direction,enabled "
            "FROM stock_factor_registry ORDER BY category,factor_name").fetchall()
    finally:
        con.close()
    items = [dict(r) for r in rows]
    by_category: dict[str, int] = {}
    for row in items:
        by_category[row["category"]] = by_category.get(row["category"], 0) + 1
    return {"count": len(items), "categories": by_category, "items": items}


def compute_factor_ic_stats(
    factors_path,
    label: str = "forward_5",
    features: list[str] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_observations_per_day: int = 20,
    progress: Callable[[float], None] = lambda p: None,
    cancelled: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Compute per-factor daily cross-sectional RankIC and persist one summary run."""
    label = str(label)
    selected = list(dict.fromkeys(features or [
        *FACTOR_COLUMNS, *INDUSTRY_FACTOR_COLUMNS, *MONEY_FLOW_FACTOR_COLUMNS,
    ]))
    run_id = f"factor-ic-{_now().replace(':', '').replace('-', '')}"
    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        _assert_label_supported(con, label)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        available: list[str] = []
        missing: list[str] = []
        for factor in selected:
            table, column = _factor_source(factor)
            if table not in tables:
                missing.append(factor)
                continue
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if column in cols:
                available.append(factor)
            else:
                missing.append(factor)
        if not available:
            raise ValueError("没有可计算的因子列")
        build_factor_registry(factors_path, available)
        where = [f"a.{label} IS NOT NULL"]
        params: list[Any] = []
        if start_date:
            where.append("a.trade_date>=?")
            params.append(start_date)
        if end_date:
            where.append("a.trade_date<=?")
            params.append(end_date)
        joins = []
        select_cols = ["a.trade_date", f"a.{label} AS label_value"]
        if any(_factor_source(f)[0] == "stock_industry_factors" for f in available):
            joins.append("LEFT JOIN stock_industry_factors b ON a.trade_date=b.trade_date AND a.code=b.code")
        if any(_factor_source(f)[0] == "stock_money_flow_factors" for f in available):
            joins.append("LEFT JOIN stock_money_flow_factors c ON a.trade_date=c.trade_date AND a.code=c.code")
        for factor in available:
            table, column = _factor_source(factor)
            alias = "a" if table == "stock_ml_factors" else ("b" if table == "stock_industry_factors" else "c")
            select_cols.append(f"{alias}.{column} AS {factor}")
        sql = (
            "SELECT " + ",".join(select_cols) + " FROM stock_ml_factors a " +
            " ".join(joins) + " WHERE " + " AND ".join(where) + " ORDER BY a.trade_date,a.code"
        )
        rows = [dict(r) for r in con.execute(sql, params)]
        progress(35)
        if not rows:
            raise ValueError("无可计算的因子/标签数据")
        by_date: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_date.setdefault(row["trade_date"], []).append(row)
        span_dates = sorted(by_date)
        computed_at = _now()
        summaries: list[dict[str, Any]] = []
        for i, factor in enumerate(available):
            if cancelled():
                raise RuntimeError("factor research cancelled")
            ics: list[float] = []
            observations = 0
            non_null = 0
            for date in span_dates:
                day = by_date[date]
                xs, ys = [], []
                for row in day:
                    value = row.get(factor)
                    label_value = row.get("label_value")
                    if value is not None:
                        non_null += 1
                    if value is not None and label_value is not None:
                        xs.append(value)
                        ys.append(label_value)
                observations += len(xs)
                if len(xs) >= min_observations_per_day:
                    ic = _spearman(xs, ys)
                    if ic is not None:
                        direction = -1 if factor in NEGATIVE_DIRECTION else 1
                        ics.append(ic * direction)
            mean_ic = float(np.mean(ics)) if ics else None
            std_ic = float(np.std(ics, ddof=1)) if len(ics) > 1 else 0.0
            icir = (mean_ic / std_ic * math.sqrt(252)) if mean_ic is not None and std_ic > 1e-12 else None
            summary = {
                "factor": factor, "label": label, "observations": observations, "days": len(ics),
                "coverage": round(non_null / len(rows), 6) if rows else 0.0,
                "meanRankIc": round(mean_ic, 6) if mean_ic is not None else None,
                "icir": round(float(icir), 6) if icir is not None else None,
                "positiveRate": round(sum(1 for v in ics if v > 0) / len(ics), 6) if ics else None,
                "absMeanRankIc": round(abs(mean_ic), 6) if mean_ic is not None else None,
            }
            summaries.append(summary)
            con.execute(
                "INSERT OR REPLACE INTO stock_factor_ic_stats "
                "(run_id,factor_name,label,start_date,end_date,observations,days,coverage,"
                "mean_rank_ic,icir,positive_rate,abs_mean_rank_ic,computed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, factor, label, span_dates[0], span_dates[-1], observations, len(ics),
                 summary["coverage"], summary["meanRankIc"], summary["icir"],
                 summary["positiveRate"], summary["absMeanRankIc"], computed_at))
            progress(35 + 60 * (i + 1) / max(1, len(available)))
        con.commit()
    finally:
        con.close()
    summaries.sort(key=lambda r: (r["absMeanRankIc"] is None, -(r["absMeanRankIc"] or 0), r["factor"]))
    progress(100)
    return {
        "runId": run_id, "label": label, "startDate": span_dates[0], "endDate": span_dates[-1],
        "factorCount": len(available), "missingFactors": missing,
        "items": summaries,
    }


def latest_factor_research(factors_path, label: str | None = None, limit: int = 100) -> dict[str, Any]:
    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        params: list[Any] = []
        where = ""
        if label:
            if label not in LABEL_COLUMNS:
                raise ValueError(f"label 必须是 {LABEL_COLUMNS} 之一")
            where = "WHERE label=?"
            params.append(label)
        row = con.execute(
            "SELECT run_id FROM stock_factor_ic_stats " + where +
            " ORDER BY computed_at DESC LIMIT 1", params).fetchone()
        if not row:
            return {"runId": None, "count": 0, "items": []}
        run_id = row["run_id"]
        rows = con.execute(
            "SELECT factor_name factor,label,start_date startDate,end_date endDate,observations,days,"
            "coverage,mean_rank_ic meanRankIc,icir,positive_rate positiveRate,"
            "abs_mean_rank_ic absMeanRankIc,computed_at computedAt "
            "FROM stock_factor_ic_stats WHERE run_id=? "
            "ORDER BY abs_mean_rank_ic DESC NULLS LAST, factor_name LIMIT ?",
            (run_id, limit)).fetchall()
    finally:
        con.close()
    return {"runId": run_id, "count": len(rows), "items": [dict(r) for r in rows]}
