"""Overnight short-interval (T+1 entry / T+2 exit) minute-bar backtest."""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

from ..common import finite, now_iso, round6

SCHEMA = """
CREATE TABLE IF NOT EXISTS short_backtest_runs(run_id TEXT PRIMARY KEY,started_at TEXT NOT NULL,finished_at TEXT,status TEXT NOT NULL,params_json TEXT NOT NULL,summary_json TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS short_backtest_trades(run_id TEXT NOT NULL,signal_date TEXT NOT NULL,entry_date TEXT NOT NULL,exit_date TEXT NOT NULL,code TEXT NOT NULL,name TEXT,rank INTEGER,score REAL,entry_time TEXT,entry_price REAL,exit_time TEXT,exit_price REAL,exit_reason TEXT,gross_return REAL,net_return REAL,max_favorable REAL,max_adverse REAL,PRIMARY KEY(run_id,signal_date,code));
CREATE INDEX IF NOT EXISTS idx_short_trade_run ON short_backtest_trades(run_id,signal_date);
"""


def _date_iso(d: str) -> str:
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def select_points(points: list[dict], top_n: int) -> list[dict]:
    return sorted([p for p in points if math.isfinite(p["score"]) and p["amount"] >= 50000],
                  key=lambda p: (-p["score"], -p["amount"]))[:top_n]


def run_short(backtest_path: Path, market_path: Path, minute_path: Path, params: dict[str, Any],
              cancelled: Callable[[], bool] | None = None) -> dict[str, Any]:
    run_id = f"short-{int(__import__('time').time() * 1000)}"
    started_at = now_iso()
    backtest_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(backtest_path, timeout=30)
    db.isolation_level = None
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("ATTACH DATABASE ? AS market", (str(market_path),))
    db.execute("ATTACH DATABASE ? AS minute", (str(minute_path),))
    db.executescript(SCHEMA)
    db.execute("INSERT INTO short_backtest_runs(run_id,started_at,status,params_json) VALUES(?,?,?,?)",
               (run_id, started_at, "running", json.dumps(params, ensure_ascii=False)))
    start, end = str(params["startDate"]), str(params["endDate"])
    if len(start) != 8 or len(end) != 8 or start > end:
        raise ValueError("startDate/endDate must be an ordered YYYYMMDD range")
    top_n = max(1, min(200, int(params.get("topN", 20))))
    take_profit = max(0.0, float(params.get("takeProfit", 0.03)))
    stop_loss = max(0.0, float(params.get("stopLoss", 0.02)))
    minimum_expected = float(params.get("minimumExpected", 0.0))
    weak_exit_time = str(params.get("weakExitTime", "14:30"))
    commission = max(0.0, float(params.get("commissionRate", 0.00008)))
    stamp = max(0.0, float(params.get("stampDutyRate", 0.0005)))
    slippage = max(0.0, float(params.get("slippageRate", 0.001)))
    try:
        all_dates = [r[0] for r in db.execute("SELECT DISTINCT trade_date tradeDate FROM market.daily_bars WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date", (start, end)).fetchall()]
        calendar = [r[0] for r in db.execute("SELECT DISTINCT trade_date tradeDate FROM market.daily_bars WHERE trade_date>=? ORDER BY trade_date", (start,)).fetchall()]
        calendar_index = {d: i for i, d in enumerate(calendar)}
        if len(all_dates) < 3:
            raise ValueError("回测区间交易日不足")
        preload_start = (db.execute("SELECT trade_date tradeDate FROM market.daily_bars WHERE trade_date<? ORDER BY trade_date DESC LIMIT 10", (start,)).fetchone() or {"tradeDate": start})["tradeDate"]
        rows = db.execute("SELECT code,trade_date tradeDate,open,high,low,close,amount,pct_chg pctChg FROM market.daily_bars WHERE trade_date>=? AND trade_date<=? ORDER BY code,trade_date", (preload_start, end)).fetchall()
        series: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            series.setdefault(row["code"], []).append(row)
        securities = {r["code"]: r for r in db.execute("SELECT code,name,list_date listDate FROM market.security_master").fetchall()}
        trades: list[dict[str, Any]] = []
        skipped_no_minute = 0
        for signal_date in all_dates:
            # 取消/超时检查点：逐信号日推进，长回测可被中断（异常由 JobManager 记为 cancelled/超时）
            if cancelled and cancelled():
                raise RuntimeError("job cancelled")
            ci = calendar_index.get(signal_date)
            if ci is None or ci + 2 >= len(calendar):
                continue
            entry_date = calendar[ci + 1]
            exit_date = calendar[ci + 2]
            signal_status = {r["code"]: r for r in db.execute(
                "SELECT code,is_st,is_suspended FROM market.security_status_history WHERE trade_date=?", (signal_date,)
            ).fetchall()}
            entry_status = {r["code"]: r for r in db.execute(
                "SELECT code,is_st,is_suspended FROM market.security_status_history WHERE trade_date=?", (entry_date,)
            ).fetchall()}
            points = []
            for code, lst in series.items():
                i = next((j for j, b in enumerate(lst) if b["tradeDate"] == signal_date), -1)
                if i < 5:
                    continue
                now, d3, d5 = lst[i], lst[i - 3], lst[i - 5]
                if not finite(now["close"]) or not finite(d3["close"]) or not finite(d5["close"]) or d3["close"] == 0 or d5["close"] == 0:
                    continue
                status = signal_status.get(code)
                if status and (status["is_st"] or status["is_suspended"]):
                    continue
                momentum3 = now["close"] / d3["close"] - 1
                momentum5 = now["close"] / d5["close"] - 1
                close_position = (now["close"] - now["low"]) / (now["high"] - now["low"]) if (finite(now["high"]) and finite(now["low"]) and now["high"] != now["low"]) else 0.5
                amount = now["amount"] or 0
                score = momentum3 * 0.45 + momentum5 * 0.25 + ((now["pctChg"] or 0) / 100) * 0.15 + (close_position - 0.5) * 0.15
                points.append({"code": code, "score": score, "amount": amount})
            chosen = select_points(points, top_n)
            for rank, candidate in enumerate(chosen):
                code = candidate["code"]
                status = entry_status.get(code)
                if status and (status["is_st"] or status["is_suspended"]):
                    continue
                entry_bars = db.execute("SELECT trade_time tradeTime,open,high,low,close,volume FROM minute.minute_bars WHERE code=? AND freq='5m' AND trade_time>=? AND trade_time<? ORDER BY trade_time", (code, f"{_date_iso(entry_date)} 09:30:00", f"{_date_iso(entry_date)} 15:01:00")).fetchall()
                exit_bars = db.execute("SELECT trade_time tradeTime,open,high,low,close,volume FROM minute.minute_bars WHERE code=? AND freq='5m' AND trade_time>=? AND trade_time<? ORDER BY trade_time", (code, f"{_date_iso(exit_date)} 09:30:00", f"{_date_iso(exit_date)} 15:01:00")).fetchall()
                first = next((b for b in entry_bars if finite(b["close"]) and (b["volume"] or 0) > 0), None)
                if not first or not exit_bars:
                    skipped_no_minute += 1
                    continue
                entry_price = first["close"] * (1 + slippage)
                take = entry_price * (1 + take_profit)
                stop = entry_price * (1 - stop_loss)
                chosen_exit = None
                reason = "close"
                exit_price = 0.0
                max_favorable = -math.inf
                max_adverse = math.inf
                for bar in exit_bars:
                    if not finite(bar["close"]) or (bar["volume"] or 0) <= 0:
                        continue
                    if finite(bar["high"]):
                        max_favorable = max(max_favorable, bar["high"] / entry_price - 1)
                    if finite(bar["low"]):
                        max_adverse = min(max_adverse, bar["low"] / entry_price - 1)
                    if finite(bar["open"]) and bar["open"] <= stop:
                        chosen_exit, exit_price, reason = bar, bar["open"] * (1 - slippage), "stop_gap"
                        break
                    if finite(bar["open"]) and bar["open"] >= take:
                        chosen_exit, exit_price, reason = bar, bar["open"] * (1 - slippage), "take_gap"
                        break
                    hit_stop = finite(bar["low"]) and bar["low"] <= stop
                    hit_take = finite(bar["high"]) and bar["high"] >= take
                    if hit_stop:
                        chosen_exit, exit_price, reason = bar, stop * (1 - slippage), "stop_loss"
                        break
                    if hit_take:
                        chosen_exit, exit_price, reason = bar, take * (1 - slippage), "take_profit"
                        break
                    hm = bar["tradeTime"][11:16]
                    if hm >= weak_exit_time and bar["close"] / entry_price - 1 < minimum_expected:
                        chosen_exit, exit_price, reason = bar, bar["close"] * (1 - slippage), "weak_exit"
                        break
                if not chosen_exit:
                    chosen_exit = next((b for b in reversed(exit_bars) if finite(b["close"])), None)
                    if not chosen_exit:
                        continue
                    exit_price = chosen_exit["close"] * (1 - slippage)
                gross = exit_price / entry_price - 1
                net = gross - commission * 2 - stamp
                trades.append({"signalDate": signal_date, "entryDate": entry_date, "exitDate": exit_date, "code": code,
                               "name": securities.get(code)["name"] if securities.get(code) else None, "rank": rank + 1,
                               "score": round6(candidate["score"]), "entryTime": first["tradeTime"], "entryPrice": round6(entry_price, 4),
                               "exitTime": chosen_exit["tradeTime"], "exitPrice": round6(exit_price, 4), "exitReason": reason,
                               "grossReturn": round6(gross), "netReturn": round6(net),
                               "maxFavorable": round6(max_favorable if math.isfinite(max_favorable) else 0),
                               "maxAdverse": round6(max_adverse if math.isfinite(max_adverse) else 0)})

        db.execute("BEGIN")
        try:
            for t in trades:
                db.execute("INSERT INTO short_backtest_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (run_id, t["signalDate"], t["entryDate"], t["exitDate"], t["code"], t["name"], t["rank"], t["score"],
                     t["entryTime"], t["entryPrice"], t["exitTime"], t["exitPrice"], t["exitReason"], t["grossReturn"],
                     t["netReturn"], t["maxFavorable"], t["maxAdverse"]))
            db.commit()
        except Exception:
            db.rollback()
            raise

        equity, peak, max_drawdown = 1.0, 1.0, 0.0
        daily: dict[str, list[float]] = {}
        for t in trades:
            daily.setdefault(t["signalDate"], []).append(t["netReturn"])
        for lst in daily.values():
            r = sum(lst) / len(lst)
            equity *= 1 + r
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity / peak - 1)
        wins = [t for t in trades if t["netReturn"] > 0]
        losses = [t for t in trades if t["netReturn"] <= 0]
        avg = lambda lst: sum(lst) / len(lst) if lst else 0.0
        reasons = {r: sum(1 for t in trades if t["exitReason"] == r) for r in ["take_profit", "take_gap", "stop_loss", "stop_gap", "weak_exit", "close"]}
        finished_at = now_iso()
        summary = {"runId": run_id, "startedAt": started_at, "finishedAt": finished_at, "startDate": start, "endDate": end,
                   "signalDays": len(daily), "trades": len(trades), "skippedNoMinute": skipped_no_minute,
                   "winRate": round6(len(wins) / max(1, len(trades))), "averageReturn": round6(avg([t["netReturn"] for t in trades])),
                   "profitLossRatio": round6(abs(avg([t["netReturn"] for t in wins]) / min(-1e-9, avg([t["netReturn"] for t in losses])))),
                   "totalReturn": round6(equity - 1), "maxDrawdown": round6(max_drawdown), "reasons": reasons, "params": params}
        db.execute("UPDATE short_backtest_runs SET finished_at=?,status=?,summary_json=? WHERE run_id=?", (finished_at, "complete", json.dumps(summary, ensure_ascii=False), run_id))
        db.commit()
        return summary
    except Exception:
        db.execute("UPDATE short_backtest_runs SET finished_at=?,status=?,error=? WHERE run_id=?", (now_iso(), "error", "see traceback", run_id))
        db.commit()
        raise
    finally:
        db.close()
