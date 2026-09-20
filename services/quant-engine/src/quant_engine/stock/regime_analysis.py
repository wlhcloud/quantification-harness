"""股票策略离线诊断：① 亏损归因（空转期/成本/毛收益）；② 叠加指数 MA20 熊市空仓择时。

纯增量、只读分析：不修改 backtest.db / factors.db 中任何既有结果，只读取
backtest_periods 与 index_daily_bars，复算一条「叠加沪深300 MA20 择时」后的对照净值，
验证 ETF 已成功的 regimeFilter{maWindow=20, bearWeight=0} 迁移到股票是否有效。

无未来函数：每期只用【信号日当日及之前】的指数收盘判断 close < MA20 -> 熊市空仓。
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any

MA_WINDOW = 20
BENCHMARK_INDEX = "000300.SH"  # 沪深300


def _conn(path: Path, ro: bool = True) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{Path(path).resolve().as_posix()}{'?mode=ro' if ro else ''}", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _regime_map(market_path: Path, signal_dates: list[str], window: int = MA_WINDOW) -> dict[str, int]:
    """返回 {信号日: 1=熊市(close<MA20) 0=牛市}。只用信号日及之前的指数收盘。"""
    mkt = _conn(market_path)
    try:
        bars = {r["trade_date"]: float(r["close"]) for r in mkt.execute(
            "SELECT trade_date, close FROM index_daily_bars WHERE ts_code=? ORDER BY trade_date",
            (BENCHMARK_INDEX,))}
    finally:
        mkt.close()
    ordered = sorted(bars)
    out: dict[str, int] = {}
    for sd in signal_dates:
        hist = [d for d in ordered if d <= sd]
        if len(hist) < window:
            out[sd] = 0  # 历史不足，默认不判熊（保守保持原仓位）
            continue
        window_days = hist[-window:]
        ma = sum(bars[d] for d in window_days) / window
        out[sd] = 1 if bars[sd] < ma else 0
    return out


def _metrics(period_rets: list[float]) -> dict[str, float]:
    nav = 1.0
    curve = [1.0]
    for r in period_rets:
        nav *= (1.0 + r)
        curve.append(nav)
    total = nav - 1.0
    n = len(period_rets)
    annual = (nav ** (12.0 / n) - 1.0) if n > 0 else 0.0
    peak = curve[0]
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    if n > 1:
        mean = sum(period_rets) / n
        var = sum((x - mean) ** 2 for x in period_rets) / (n - 1)
        sharpe = (mean / math.sqrt(var) * math.sqrt(12)) if var > 0 else 0.0
    else:
        sharpe = 0.0
    win = sum(1 for r in period_rets if r > 0) / n if n else 0.0
    return {"totalReturn": total, "annualReturn": annual, "maxDrawdown": mdd,
            "sharpe": sharpe, "winRate": win, "finalNav": nav, "periods": n}


def analyze(backtest_path: Path, market_path: Path, run_id: str | None = None) -> dict[str, Any]:
    con = _conn(backtest_path)
    try:
        if run_id is None:
            row = con.execute("SELECT run_id FROM backtest_runs ORDER BY rowid DESC LIMIT 1").fetchone()
            run_id = row["run_id"]
        models = [r["model_id"] for r in con.execute(
            "SELECT DISTINCT model_id FROM backtest_periods WHERE run_id=? ORDER BY model_id", (run_id,))]
        result: dict[str, Any] = {"runId": run_id, "maWindow": MA_WINDOW, "models": {}}
        for mid in models:
            periods = [dict(r) for r in con.execute(
                "SELECT signal_date, holdings, gross_return, net_return, turnover FROM backtest_periods "
                "WHERE run_id=? AND model_id=? ORDER BY signal_date", (run_id, mid))]
            if not periods:
                continue
            regime = _regime_map(market_path, [p["signal_date"] for p in periods])
            raw_rets, timed_rets = [], []
            zero_spin = 0           # holdings=0 的空转期
            cost_drag = 0.0         # 成本拖累合计（gross-net）
            bear_n = 0
            for p in periods:
                g, n = float(p["gross_return"]), float(p["net_return"])
                raw_rets.append(n)
                cost_drag += (g - n)
                if int(p["holdings"]) == 0:
                    zero_spin += 1
                is_bear = regime.get(p["signal_date"], 0) == 1
                if is_bear:
                    bear_n += 1
                # 熊市空仓（收益0、不产生换手成本）；牛市保持原净收益
                timed_rets.append(0.0 if is_bear else n)
            attrib = {
                "periods": len(periods),
                "zeroHoldingPeriods": zero_spin,
                "bearPeriods": bear_n,
                "bullPeriods": len(periods) - bear_n,
                "costDragTotal": round(cost_drag, 4),
                "avgGrossPerPeriod": round(sum(float(p["gross_return"]) for p in periods) / len(periods), 5),
            }
            result["models"][mid] = {
                "attribution": attrib,
                "raw": _metrics(raw_rets),
                "timed": _metrics(timed_rets),
            }
        return result
    finally:
        con.close()


if __name__ == "__main__":
    import json
    root = Path(__file__).resolve().parents[5]
    out = analyze(root / "data" / "backtest.db", root / "data" / "market.db")
    print(json.dumps(out, ensure_ascii=False, indent=2))
