"""Cost-aware portfolio and execution simulation."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

from ..common import compact_date, finite, now_iso, ratio, round6

MONTHLY_SCHEMA = """
CREATE TABLE IF NOT EXISTS backtest_runs(run_id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,config_json TEXT NOT NULL,summary_json TEXT,error TEXT,
  -- 配置指纹（2026-09-20）：config_json 已是完整配置，故不再加重复的 config_json 列。
  config_sha256 TEXT,config_yaml_sha256 TEXT,git_commit TEXT,git_dirty INTEGER);
CREATE TABLE IF NOT EXISTS backtest_periods(run_id TEXT NOT NULL,model_id TEXT NOT NULL,signal_date TEXT NOT NULL,entry_date TEXT NOT NULL,exit_date TEXT NOT NULL,holdings INTEGER,skipped INTEGER,gross_return REAL,net_return REAL,benchmark_return REAL,turnover REAL,PRIMARY KEY(run_id,model_id,signal_date));
CREATE TABLE IF NOT EXISTS backtest_layers(run_id TEXT NOT NULL,model_id TEXT NOT NULL,signal_date TEXT NOT NULL,layer INTEGER NOT NULL,count INTEGER,net_return REAL,PRIMARY KEY(run_id,model_id,signal_date,layer));
"""

SHORT_SCHEMA = """
CREATE TABLE IF NOT EXISTS short_backtest_runs(run_id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,params_json TEXT NOT NULL,summary_json TEXT,error TEXT,
  -- 配置指纹（2026-09-20）：params_json 已是完整配置。
  config_sha256 TEXT,config_yaml_sha256 TEXT,git_commit TEXT,git_dirty INTEGER);
CREATE TABLE IF NOT EXISTS short_backtest_trades(run_id TEXT NOT NULL,signal_date TEXT NOT NULL,entry_date TEXT NOT NULL,exit_date TEXT NOT NULL,code TEXT NOT NULL,name TEXT,rank INTEGER,score REAL,entry_time TEXT,entry_price REAL,exit_time TEXT,exit_price REAL,exit_reason TEXT,gross_return REAL,net_return REAL,max_favorable REAL,max_adverse REAL,PRIMARY KEY(run_id,signal_date,code));
CREATE INDEX IF NOT EXISTS idx_short_trade_run ON short_backtest_trades(run_id,signal_date);
"""


def rebalance_dates(dates: list[str]) -> list[str]:
    output: list[str] = []
    month = ""
    for i in range(120, len(dates) - 2):
        next_month = dates[i + 1][:6]
        if next_month != month:
            if month:
                output.append(dates[i])
            month = next_month
    return output


def annualised(period_returns: list[float], annual_risk_free: float = 0.0) -> dict[str, float]:
    equity, peak, max_drawdown = 1.0, 1.0, 0.0
    for value in period_returns:
        equity *= 1 + value
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity / peak - 1)
    years = max(len(period_returns) / 12, 1 / 12)
    annual_return = equity ** (1 / years) - 1
    mean = sum(period_returns) / max(1, len(period_returns))
    variance = sum((v - mean) ** 2 for v in period_returns) / max(1, len(period_returns) - 1)
    volatility = math.sqrt(variance * 12)
    monthly_risk_free = (1 + annual_risk_free) ** (1 / 12) - 1
    excess_mean = mean - monthly_risk_free
    return {"totalReturn": round6(equity - 1), "annualReturn": round6(annual_return),
            "volatility": round6(volatility), "sharpe": round6(excess_mean / (variance ** 0.5) * math.sqrt(12)) if variance else 0,
            "maxDrawdown": round6(max_drawdown),
            "winRate": round6(sum(1 for v in period_returns if v > 0) / max(1, len(period_returns)))}


def run_monthly(backtest_path: Path, market_path: Path, finance_path: Path, model_config: dict,
                top_n: int = 30, commission: float = 0.0003, stamp: float = 0.0005,
                slippage: float = 0.001, annual_risk_free: float = 0.0,
                progress: Callable[[float], None] | None = None,
                cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    started_at = now_iso()
    run_id = f"bt-{int(__import__('time').time() * 1000)}"
    backtest_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(backtest_path, timeout=30)
    db.isolation_level = None
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("ATTACH DATABASE ? AS market", (str(market_path),))
    db.execute("ATTACH DATABASE ? AS finance", (str(finance_path),))
    db.executescript(MONTHLY_SCHEMA)
    # 幂等迁移：旧库补指纹列（旧 run 保持 NULL = 早于档案机制）。
    from ..stock_ml.config_integrity import ensure_config_columns, stamp_run_config
    ensure_config_columns(db, "backtest_runs", skip=("config_json",))
    effective_cfg = {"kind": "backtest_monthly", "topN": top_n, "commission": commission,
                     "stamp": stamp, "slippage": slippage,
                     "annualRiskFreeRate": annual_risk_free, "rebalance": "monthly"}
    columns: dict[str, Any] = {}
    # config_json 列已是完整配置，故只补指纹，避免同一份配置存两遍。
    stamp_run_config(db, columns, effective_cfg, run_id=run_id, generated_at=started_at,
                     include_config_json=False)
    db.execute("INSERT INTO backtest_runs(run_id,started_at,status,config_json,"
               "config_sha256,config_yaml_sha256,git_commit,git_dirty) VALUES(?,?,?,?,?,?,?,?)",
               (run_id, started_at, "running", json.dumps(effective_cfg),
                columns["config_sha256"], columns["config_yaml_sha256"],
                columns["git_commit"], columns["git_dirty"]))
    try:
        bars = db.execute("""SELECT b.code,b.trade_date tradeDate,b.open,b.high,b.low,b.close,b.pre_close,b.volume,b.amount,b.pct_chg pctChg,
                                  a.adj_factor adjFactor
                           FROM market.daily_bars b
                           LEFT JOIN market.adjustment_factors a ON a.code=b.code AND a.trade_date=b.trade_date
                           ORDER BY b.code,b.trade_date""").fetchall()
        dates = [r[0] for r in db.execute("SELECT DISTINCT trade_date tradeDate FROM market.daily_bars ORDER BY trade_date").fetchall()]
        signals = rebalance_dates(dates)
        if len(signals) < 2:
            raise ValueError("insufficient history for monthly backtest")
        by_code: dict[str, list[sqlite3.Row]] = {}
        for bar in bars:
            by_code.setdefault(bar["code"], []).append(bar)
        # Adjustment factors are recorded with each historical session.  Multiplying
        # raw prices by that session's factor makes return calculations continuous
        # while preserving the factor snapshot used by this run for auditability.
        adjusted_prices: dict[tuple[str, str], dict[str, float | None]] = {}
        for series in by_code.values():
            for bar in series:
                adjustment = finite(bar["adjFactor"])
                bar_adj_open = finite(bar["open"])
                bar_adj_close = finite(bar["close"])
                # sqlite.Row is immutable, so keep derived prices in a side mapping.
                bar_key = (bar["code"], bar["tradeDate"])
                adjusted_prices[bar_key] = {
                    "open": bar_adj_open * adjustment if bar_adj_open is not None and adjustment is not None else None,
                    "close": bar_adj_close * adjustment if bar_adj_close is not None and adjustment is not None else None,
                }
        pos_by_code = {code: {b["tradeDate"]: i for i, b in enumerate(lst)} for code, lst in by_code.items()}
        securities = {r["code"]: r for r in db.execute("SELECT code,name,industry,list_date listDate FROM market.security_master").fetchall()}
        date_index = {d: i for i, d in enumerate(dates)}
        name_patterns = [p.upper() for p in model_config["filters"]["excludeNamePatterns"]]
        periods: list[dict[str, Any]] = []
        layers: list[dict[str, Any]] = []
        previous_holdings: dict[str, set[str]] = {model_id: set() for model_id in model_config["models"]}
        has_status_history = db.execute(
            "SELECT 1 FROM market.sqlite_master WHERE type='table' AND name='security_status_history'"
        ).fetchone() is not None
        status_covered_signals = 0

        for signal_number, signal_date in enumerate(signals):
            if cancelled and cancelled():
                raise RuntimeError("backtest cancelled")
            if progress:
                progress(10 + 80 * signal_number / max(1, len(signals)))
            signal_index = date_index[signal_date]
            entry_date = dates[signal_index + 1]
            next_signal = signals[signals.index(signal_date) + 1] if signals.index(signal_date) + 1 < len(signals) else None
            exit_signal_index = date_index[next_signal] if next_signal else len(dates) - 2
            exit_date = dates[exit_signal_index + 1]
            status_by_code = {}
            entry_status_by_code = {}
            exit_status_by_code = {}
            if has_status_history:
                for target, day in ((status_by_code, signal_date), (entry_status_by_code, entry_date),
                                    (exit_status_by_code, exit_date)):
                    target.update({r["code"]: r for r in db.execute(
                        "SELECT code,is_st,is_suspended,limit_up,limit_down FROM market.security_status_history WHERE trade_date=?",
                        (day,),
                    ).fetchall()})
            basic_rows = {r["code"]: r for r in db.execute("SELECT code,turnover_rate turnover20,pe_ttm peTtm,pb,total_mv mktCap FROM market.daily_basic WHERE trade_date=?", (signal_date,)).fetchall()}
            finance_rows = {r["code"]: r for r in db.execute("SELECT f.code,f.roe,f.netprofit_yoy netprofitYoy,f.debt_to_assets debtToAssets,f.roa,f.gross_margin grossMargin,f.revenue_yoy revenueYoy,f.ocf_yoy ocfYoy,f.current_ratio currentRatio FROM finance.financial_indicator f JOIN (SELECT code,MAX(ann_date||end_date) latest FROM finance.financial_indicator WHERE ann_date<=? GROUP BY code)x ON x.code=f.code AND x.latest=f.ann_date||f.end_date", (signal_date,)).fetchall()}
            points: list[dict[str, Any]] = []
            for code, series in by_code.items():
                i = pos_by_code[code].get(signal_date, -1)
                if i < 120:
                    continue
                now = series[i]
                security = securities.get(code)
                historical_status = status_by_code.get(code)
                basic = basic_rows.get(code)
                fina = finance_rows.get(code)
                window20 = series[i - 19: i + 1]
                window60 = series[i - 59: i + 1]
                returns = [finite(x["pctChg"]) for x in window20]
                returns = [x for x in returns if x is not None]
                mean = sum(returns) / max(1, len(returns))
                square = sum(r * r for r in returns) / max(1, len(returns))
                closes20 = [adjusted_prices[(code, x["tradeDate"])]["close"] for x in window20]
                closes60 = [adjusted_prices[(code, x["tradeDate"])]["close"] for x in window60]
                if any(x is None for x in closes20 + closes60):
                    continue
                ma20 = sum(closes20) / 20
                ma60 = sum(closes60) / 60
                high60 = max(closes60)
                amount20 = sum(x["amount"] or 0 for x in window20) / 20
                metrics = {
                    "momentum20": ratio(adjusted_prices[(code, now["tradeDate"])]["close"], adjusted_prices[(code, series[i - 20]["tradeDate"])]["close"]),
                    "momentum60": ratio(adjusted_prices[(code, now["tradeDate"])]["close"], adjusted_prices[(code, series[i - 60]["tradeDate"])]["close"]),
                    "momentum120": ratio(adjusted_prices[(code, now["tradeDate"])]["close"], adjusted_prices[(code, series[i - 120]["tradeDate"])]["close"]),
                    "trend": ma20 / ma60 - 1 if ma60 else None,
                    "volatility20": math.sqrt(max(0, square - mean ** 2)) * math.sqrt(250) / 100,
                    "drawdown60": adjusted_prices[(code, now["tradeDate"])]["close"] / high60 - 1 if math.isfinite(high60) else None,
                    "roe": finite(fina["roe"]) if fina else None,
                    "netprofitYoy": finite(fina["netprofitYoy"]) if fina else None,
                    "debtToAssets": finite(fina["debtToAssets"]) if fina else None,
                    "roa": finite(fina["roa"]) if fina else None,
                    "grossMargin": finite(fina["grossMargin"]) if fina else None,
                    "revenueYoy": finite(fina["revenueYoy"]) if fina else None,
                    "ocfYoy": finite(fina["ocfYoy"]) if fina else None,
                    "currentRatio": finite(fina["currentRatio"]) if fina else None,
                    "mktCap": finite(basic["mktCap"]) if basic else None,
                    "peTtm": finite(basic["peTtm"]) if basic else None,
                    "pb": finite(basic["pb"]) if basic else None,
                    "turnover20": finite(basic["turnover20"]) if basic else None,
                }
                # Keep historical bar constituents even when they disappeared from the
                # current security master; otherwise delisted names create survivorship bias.
                list_days = (__import__("datetime").datetime.strptime(signal_date, "%Y%m%d") -
                             __import__("datetime").datetime.strptime(series[0]["tradeDate"], "%Y%m%d")).days
                if security and security["listDate"] and len(security["listDate"]) == 8:
                    list_days = (__import__("datetime").datetime.strptime(signal_date, "%Y%m%d") - __import__("datetime").datetime.strptime(security["listDate"], "%Y%m%d")).days
                eligible = (i >= 120 and list_days >= model_config["filters"]["minListDays"]
                            and (now["close"] or 0) >= model_config["filters"]["minClose"] and amount20 >= model_config["filters"]["minAmount20"]
                            and not (historical_status and (historical_status["is_st"] or historical_status["is_suspended"]))
                            and not any(p in (security["name"] or "").upper() if security else False for p in name_patterns))
                points.append({"code": code, "name": security["name"] if security else None, "industry": security["industry"] if security else None,
                               "metrics": metrics, "scores": {}, "eligible": eligible})

            if points and len(status_by_code) >= len(points) * 0.9:
                status_covered_signals += 1

            eligible = [p for p in points if p["eligible"]]
            for key, definition in model_config["factors"].items():
                vals = []
                for point in eligible:
                    value = point["metrics"].get(key)
                    if value is None or (definition.get("positiveOnly") and value <= 0):
                        continue
                    vals.append((point, value))
                vals.sort(key=lambda x: x[1])
                denom = max(1, len(vals) - 1)
                for index, (point, _v) in enumerate(vals):
                    pct = index / denom
                    point["scores"][key] = round6((pct if definition["direction"] == 1 else 1 - pct) * 100, 2)
                for point in points:
                    if key not in point["scores"]:
                        point["scores"][key] = None

            def returns_for(items, model_id, layer=None, charge_costs=False):
                skipped = 0
                values = []
                executed: set[str] = set()
                for point in items:
                    series = by_code[point["code"]]
                    pos = pos_by_code[point["code"]]
                    entry = series[pos[entry_date]] if entry_date in pos else None
                    exit_bar = series[pos[exit_date]] if exit_date in pos else None
                    entry_open = adjusted_prices.get((point["code"], entry_date), {}).get("open")
                    exit_open = adjusted_prices.get((point["code"], exit_date), {}).get("open")
                    entry_status = entry_status_by_code.get(point["code"])
                    exit_status = exit_status_by_code.get(point["code"])
                    entry_limit_locked = bool(entry and entry_status and entry_status["limit_up"] is not None
                                              and entry["low"] >= entry_status["limit_up"])
                    exit_limit_locked = bool(exit_bar and exit_status and exit_status["limit_down"] is not None
                                             and exit_bar["high"] <= exit_status["limit_down"])
                    # A zero-volume/one-price bar is not assumed tradable.
                    if (not entry or not entry_open or not exit_bar or not exit_open
                            or (entry["high"] == entry["low"] and not entry["volume"])
                            or (entry_status and entry_status["is_suspended"])
                            or (exit_status and exit_status["is_suspended"])
                            or entry_limit_locked or exit_limit_locked):
                        skipped += 1
                        continue
                    values.append(exit_open / entry_open - 1)
                    executed.add(point["code"])
                gross = sum(values) / max(1, len(values))
                current = executed
                old = previous_holdings.get(model_id, set()) if charge_costs else set()
                denominator = max(1, len(current), len(old))
                buy_weight = len(current - old) / denominator
                sell_weight = len(old - current) / denominator
                costs = commission * (buy_weight + sell_weight) + stamp * sell_weight + slippage * (buy_weight + sell_weight)
                net = gross - costs if values or old else 0.0
                if charge_costs:
                    previous_holdings[model_id] = current
                if layer:
                    layers.append({"modelId": model_id, "signalDate": signal_date, "layer": layer, "count": len(values), "netReturn": round6(net)})
                return {"gross": gross, "net": net, "skipped": skipped, "count": len(values),
                        "turnover": (buy_weight + sell_weight) / 2, "cost": costs}

            benchmark = returns_for(eligible, "benchmark")
            for model_id, model in model_config["models"].items():
                total_weight = sum(model["weights"].values())
                for point in eligible:
                    weighted, used = 0.0, 0.0
                    for factor, weight in model["weights"].items():
                        score = point["scores"].get(factor)
                        if score is not None:
                            weighted += score * weight
                            used += weight
                    point["score"] = weighted / used if used else 0
                    point["coverage"] = used / total_weight
                ranked = [p for p in eligible if p["coverage"] >= model["minCoverage"] and p["score"] >= model["minScore"]]
                ranked.sort(key=lambda p: -p["score"])
                selected = ranked[: min(top_n, model["maxCandidates"])]
                result = returns_for(selected, model_id, charge_costs=True)
                periods.append({"modelId": model_id, "signalDate": signal_date, "entryDate": entry_date, "exitDate": exit_date,
                                "holdings": result["count"], "skipped": result["skipped"], "grossReturn": round6(result["gross"]),
                                "netReturn": round6(result["net"]), "benchmarkReturn": round6(benchmark["gross"]),
                                "turnover": round6(result["turnover"])})
                layer_size = math.ceil(len(ranked) / 5)
                for layer in range(1, 6):
                    returns_for(ranked[(layer - 1) * layer_size: layer * layer_size], model_id, layer)

        db.execute("BEGIN")
        try:
            for row in periods:
                db.execute("INSERT INTO backtest_periods VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, row["modelId"], row["signalDate"], row["entryDate"], row["exitDate"], row["holdings"],
                     row["skipped"], row["grossReturn"], row["netReturn"], row["benchmarkReturn"], row["turnover"]))
            for row in layers:
                db.execute("INSERT INTO backtest_layers VALUES(?,?,?,?,?,?)",
                    (run_id, row["modelId"], row["signalDate"], row["layer"], row["count"], row["netReturn"]))
            db.commit()
        except Exception:
            db.rollback()
            raise

        summaries = []
        for model_id, model in model_config["models"].items():
            rows = [p for p in periods if p["modelId"] == model_id]
            stats = annualised([p["netReturn"] for p in rows], annual_risk_free)
            bench = annualised([p["benchmarkReturn"] for p in rows], annual_risk_free)
            summaries.append({"modelId": model_id, "label": model["label"], **stats,
                              "benchmarkTotalReturn": bench["totalReturn"],
                              "excessReturn": round6(stats["totalReturn"] - bench["totalReturn"]),
                              "periods": len(rows),
                              "averageHoldings": round6(sum(p["holdings"] for p in rows) / max(1, len(rows)), 2)})
        finished_at = now_iso()
        model_count = len(model_config["models"])
        summary = {"runId": run_id, "startedAt": started_at, "finishedAt": finished_at,
                   "firstSignalDate": signals[0], "lastExitDate": periods[-1]["exitDate"],
                   "models": summaries, "periodCount": len(periods) / model_count, "topN": top_n,
                   "benchmark": "eligible_equal_weight",
                   "costs": {"commissionRate": commission, "stampDutyRate": stamp, "slippageRate": slippage},
                   "pointInTimeCoverage": round6(status_covered_signals / max(1, len(signals))),
                   "dataQualityWarnings": ([] if status_covered_signals == len(signals) else
                       ["历史 ST/停牌/涨跌停覆盖不完整；结果仅供研究，不得用于实盘下单"])}
        db.execute("UPDATE backtest_runs SET finished_at=?,status=?,summary_json=? WHERE run_id=?", (finished_at, "complete", json.dumps(summary, ensure_ascii=False), run_id))
        db.commit()
        return {"runId": run_id, "startedAt": started_at, "finishedAt": finished_at,
                "firstSignalDate": signals[0], "lastExitDate": periods[-1]["exitDate"],
                "models": model_count, "periods": len(periods), "topN": top_n,
                "costs": {"commissionRate": commission, "stampDutyRate": stamp}}
    except Exception:
        was_cancelled = bool(cancelled and cancelled())
        db.execute("UPDATE backtest_runs SET finished_at=?,status=?,error=? WHERE run_id=?",
                   (now_iso(), "cancelled" if was_cancelled else "error", None if was_cancelled else "see job error", run_id))
        db.commit()
        raise
    finally:
        db.close()
