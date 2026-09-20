"""IC / RankIC / ICIR research on the ETF factor frame (M2, additive).

RankIC is computed cross-sectionally per trade date (Spearman correlation
between factor value and forward return), then aggregated over the window.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

import numpy as np

from .factors import FACTOR_NAMES


def _spearman(a: list[float], b: list[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None
             and math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(pairs) < 5:
        return None
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    return _rank_corr(xs, ys)


def _rank_corr(xs: list[float], ys: list[float]) -> float:
    def rank(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks

    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mean_x, mean_y = sum(rx) / n, sum(ry) / n
    cov = sum((rx[i] - mean_x) * (ry[i] - mean_y) for i in range(n))
    var_x = sum((v - mean_x) ** 2 for v in rx)
    var_y = sum((v - mean_y) ** 2 for v in ry)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def run_research(frame: list[dict[str, Any]], label: str, min_days: int) -> dict[str, Any]:
    """Aggregate per-date RankIC into per-factor stats + a top-factors ranking."""
    by_date: dict[str, list[dict[str, Any]]] = {}
    for row in frame:
        by_date.setdefault(row["trade_date"], []).append(row)

    factor_ics: dict[str, list[float]] = {f: [] for f in FACTOR_NAMES}
    for date in sorted(by_date):
        rows = by_date[date]
        ys = [r[label] for r in rows]
        for factor in FACTOR_NAMES:
            xs = [r.get(factor) for r in rows]
            ic = _spearman(xs, ys)
            if ic is not None:
                factor_ics[factor].append(ic)

    results: list[dict[str, Any]] = []
    for factor in FACTOR_NAMES:
        ics = factor_ics[factor]
        if len(ics) < max(1, min_days):
            continue
        mean_ic = sum(ics) / len(ics)
        std = statistics.pstdev(ics) if len(ics) > 1 else 0.0
        icir = mean_ic / std if std > 0 else 0.0
        positive = sum(1 for v in ics if v > 0) / len(ics)
        results.append({"factor": factor, "ic": round(mean_ic, 4), "rankIc": round(mean_ic, 4),
                        "icir": round(icir, 4), "icPositive": round(positive, 4),
                        "days": len(ics), "icStd": round(std, 4)})
    results.sort(key=lambda r: abs(r["rankIc"]), reverse=True)

    dates = sorted(by_date)
    return {
        "label": label,
        "windowStart": dates[0] if dates else None,
        "windowEnd": dates[-1] if dates else None,
        "days": len(dates),
        "factorCount": len(results),
        "results": results,
    }
