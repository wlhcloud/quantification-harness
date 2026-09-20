"""技术面择时（V5）回测产物的只读汇总。

背景：前端「策略回测 → V5策略回测」页签原先写死一组数字（总收益 +1682.66%、年化 203.43%、
Sharpe 1.978、最大回撤 -28.15%），既无法复算也无法更新。这些数字其实来自
`scripts/backtest_technical_timing_v5*.py` 产出的 CSV（`artifacts/stock-ml/technical_timing_v5*_backtest.csv`）。

本模块直接读该 CSV 并现场计算指标与净值曲线，让页面展示"可复算"的结果：
  - totalReturn 优先取脚本自己记的 cum_return（最后一行的累计收益），与历史口径一致；
  - annualReturn / sharpe / maxDrawdown 由 value 序列现算（年化按 252 交易日折算）。
"""
from __future__ import annotations

import csv
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# variant -> 产物文件名（由 scripts/backtest_technical_timing_v5*.py 生成）
V5_VARIANTS: dict[str, str] = {
    "long": "technical_timing_v5_long_backtest.csv",
    "short": "technical_timing_v5_backtest.csv",
}


def summarize_v5_backtest(artifact_dir: Path, variant: str = "long") -> dict[str, Any]:
    """读取 V5 回测 CSV 并返回指标 + 净值曲线；文件缺失时返回 status='none'。"""
    if variant not in V5_VARIANTS:
        raise ValueError(f"variant 必须是 {sorted(V5_VARIANTS)} 之一")
    path = Path(artifact_dir) / V5_VARIANTS[variant]
    if not path.exists():
        return {"status": "none", "variant": variant,
                "expectedFile": str(path),
                "note": "尚未生成该变体的回测产物；运行 scripts/backtest_technical_timing_v5_long.py 后可见。"}

    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [row for row in csv.DictReader(fh) if row.get("value")]

    values: list[tuple[str, float]] = []
    cum_returns: list[float] = []
    for row in rows:
        try:
            values.append((str(row["date"]), float(row["value"])))
        except (KeyError, TypeError, ValueError):
            continue
        try:
            cum_returns.append(float(row.get("cum_return") or "nan"))
        except ValueError:
            cum_returns.append(float("nan"))

    if len(values) < 2:
        return {"status": "empty", "variant": variant, "sourceFile": path.name}

    returns = [values[i][1] / values[i - 1][1] - 1 for i in range(1, len(values))]
    initial_value = values[0][1]
    final_value = values[-1][1]
    total_return = final_value / initial_value - 1
    # 优先用脚本记录的 cum_return（口径与历史页面一致）
    if cum_returns and not math.isnan(cum_returns[-1]):
        total_return = cum_returns[-1]
    annual_return = (1 + total_return) ** (252 / len(returns)) - 1 if returns else 0.0
    stdev = statistics.pstdev(returns) if len(returns) > 1 else 0.0
    sharpe = (statistics.mean(returns) / stdev * math.sqrt(252)) if stdev else 0.0
    peak = values[0][1]
    max_drawdown = 0.0
    for _, value in values:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)

    return {
        "status": "ok",
        "variant": variant,
        "sourceFile": path.name,
        "generatedAt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "startDate": values[0][0],
        "endDate": values[-1][0],
        "tradingDays": len(returns),
        "initialValue": round(initial_value, 2),
        "finalValue": round(final_value, 2),
        "totalReturn": round(total_return, 6),
        "annualReturn": round(annual_return, 6),
        "sharpe": round(sharpe, 4),
        "maxDrawdown": round(max_drawdown, 6),
        "annualization": "252 交易日",
        "curve": [{"date": date, "value": round(value, 2)} for date, value in values],
        "note": "由 artifacts 下的回测 CSV 现场计算，可复算；非模拟盘台账。",
    }
