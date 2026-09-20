"""ETF 模拟盘台账：真实跟踪 walkforward 信号，T+1 开盘价成交，佣金万2、ETF 无印花税。

纯增量模块（零破坏）：只新增 etf_sim_runs / etf_sim_daily / etf_sim_positions 三张表，
不修改任何既有表与逻辑。执行假设（与 docs/etf-quant-mvp.md 模拟盘口径一致）：
  - 成交价：信号日之后首个交易日的开盘价（T+1 开盘价），验证 gap/滑点影响
  - 成本：佣金 0.0002（万2），ETF 免印花税、免过户费
  - 空仓语义：bearRegime=True 时清仓转现金（与正式 walkforward 一致）
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS etf_sim_runs (
  run_id TEXT PRIMARY KEY,
  initial_capital REAL NOT NULL,
  start_date TEXT NOT NULL,
  commission_rate REAL NOT NULL DEFAULT 0.0002,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  last_signal_date TEXT,
  last_nav REAL
);
CREATE TABLE IF NOT EXISTS etf_sim_daily (
  run_id TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  nav REAL NOT NULL,
  cash REAL NOT NULL,
  market_value REAL NOT NULL,
  position_count INTEGER NOT NULL,
  bear_regime INTEGER NOT NULL,
  note TEXT,
  PRIMARY KEY (run_id, trade_date)
);
CREATE TABLE IF NOT EXISTS etf_sim_positions (
  run_id TEXT NOT NULL,
  ts_code TEXT NOT NULL,
  shares REAL NOT NULL,
  avg_cost REAL NOT NULL,
  last_price REAL NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, ts_code)
);
"""

DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_COMMISSION = 0.0002  # 万2


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- 信号与价格

def latest_signal(etf_db_path: Path) -> dict[str, Any]:
    """最新 walkforward 持仓信号（与 latest_walkforward 同源）。"""
    con = connect(etf_db_path)
    try:
        row = con.execute(
            "SELECT run_id,holdings FROM etf_walkforward_runs "
            "WHERE status='complete' ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {"runId": None, "signalDate": None, "bearRegime": False, "holdings": []}
        holdings = json.loads(row["holdings"] or "{}")
        return {
            "runId": row["run_id"],
            "signalDate": holdings.get("tradeDate"),
            "bearRegime": bool(holdings.get("bearRegime", False)),
            "holdings": list(holdings.get("holdings") or []),
        }
    finally:
        con.close()


def _exec_price(market_con: sqlite3.Connection, ts_code: str, after_date: str) -> dict[str, Any] | None:
    """信号日之后首个交易日 bar（T+1 成交价），兜底为最新 bar。返回 {date, open, close}。"""
    row = market_con.execute(
        "SELECT trade_date, open, close FROM etf_daily_bars "
        "WHERE ts_code=? AND trade_date>? ORDER BY trade_date LIMIT 1",
        (ts_code, after_date),
    ).fetchone()
    if row:
        return {"date": row["trade_date"], "open": row["open"], "close": row["close"], "t1": True}
    row = market_con.execute(
        "SELECT trade_date, open, close FROM etf_daily_bars "
        "WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
        (ts_code,),
    ).fetchone()
    if row:
        return {"date": row["trade_date"], "open": row["open"], "close": row["close"], "t1": False}
    return None


def _latest_close(market_con: sqlite3.Connection, ts_code: str) -> float | None:
    row = market_con.execute(
        "SELECT close FROM etf_daily_bars WHERE ts_code=? ORDER BY trade_date DESC LIMIT 1",
        (ts_code,),
    ).fetchone()
    return row["close"] if row else None


def _period_cash(con: sqlite3.Connection, run_id: str, initial_capital: float) -> float:
    """期初现金 = 上一期 etf_sim_daily.cash；首期无记录时回退到初始资金。

    修复记录：原实现每期都以 initial_capital 重置现金，并在建仓时按
    initial_capital*weight 重新买入，导致前期盈亏既不结转、也不影响后续仓位，NAV 永远无法复利累积。
    """
    row = con.execute(
        "SELECT cash FROM etf_sim_daily WHERE run_id=? ORDER BY trade_date DESC LIMIT 1", (run_id,)
    ).fetchone()
    return float(row["cash"]) if row else float(initial_capital)


# ---------------------------------------------------------------- 主流程

def start_sim(
    etf_db_path: Path,
    market_path: Path,
    initial_capital: float = DEFAULT_CAPITAL,
    start_date: str | None = None,
    commission_rate: float = DEFAULT_COMMISSION,
) -> dict[str, Any]:
    """创建模拟盘 run。起点默认取最新信号的因子日；当前信号为空仓则全现金起步。"""
    signal = latest_signal(etf_db_path)
    start = start_date or signal["signalDate"] or datetime.now().strftime("%Y%m%d")
    run_id = f"etf-sim-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    con = connect(etf_db_path)
    try:
        ensure_schema(con)
        con.execute(
            "INSERT INTO etf_sim_runs (run_id, initial_capital, start_date, commission_rate, status, created_at, "
            "last_signal_date, last_nav) VALUES (?,?,?,?,?,?,?,?)",
            # last_signal_date 留空：起点尚未建仓，首个 advance 必须允许执行（否则被幂等判断挡成 waiting）
            (run_id, initial_capital, start, commission_rate, "active", _now(), None, 1.0),
        )
        con.execute(
            "INSERT INTO etf_sim_daily (run_id, trade_date, nav, cash, market_value, position_count, bear_regime, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, start, 1.0, initial_capital, 0.0, 0, 1 if signal["bearRegime"] else 0,
             f"起点 · 信号日 {signal['signalDate']} · {'空仓防御' if signal['bearRegime'] else '持有信号'}"),
        )
        con.commit()
        return {"runId": run_id, "initialCapital": initial_capital, "startDate": start,
                "signalDate": signal["signalDate"], "bearRegime": signal["bearRegime"],
                "nav": 1.0, "cash": initial_capital, "marketValue": 0.0}
    finally:
        con.close()


def advance_sim(etf_db_path: Path, market_path: Path, run_id: str) -> dict[str, Any]:
    """用最新信号推进模拟盘：T+1 开盘价成交目标持仓，按最新收盘估值入账。"""
    con = connect(etf_db_path)
    mkt = sqlite3.connect(f"file:{Path(market_path).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    mkt.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        run = con.execute("SELECT * FROM etf_sim_runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            return {"ok": False, "error": f"run 不存在: {run_id}"}
        signal = latest_signal(etf_db_path)
        # 幂等：无新信号（或信号日与上次入账相同）时返回 waiting，不重复记账
        if not signal["signalDate"]:
            return {"ok": False, "waiting": True, "runId": run_id, "message": "暂无最新信号（walkforward 未产出持仓）"}
        if run["last_signal_date"] == signal["signalDate"] and run["last_nav"] is not None:
            return {"ok": False, "waiting": True, "runId": run_id, "message": f"信号未更新（{signal['signalDate']}），等待下一期"}
        target = list(dict.fromkeys(signal["holdings"] or []))  # 去重保序：重复标的会摊薄目标权重
        positions = {r["ts_code"]: dict(r) for r in con.execute(
            "SELECT ts_code, shares, avg_cost, last_price FROM etf_sim_positions WHERE run_id=?", (run_id,))}

        # 1) 期初现金从上期结转（原实现每期重置为 initial_capital，NAV 无法复利）
        initial_capital = float(run["initial_capital"])
        commission = float(run["commission_rate"])
        cash = _period_cash(con, run_id, initial_capital)
        signal_date = signal["signalDate"] or ""

        # 每个涉及标的的 T+1 成交 bar（无 T+1 行情时 _exec_price 兜底最新 bar 并标注 t1=False）
        prices: dict[str, dict[str, Any]] = {}
        for code in sorted(set(positions) | set(target)):
            bar = _exec_price(mkt, code, signal_date)
            if bar is not None:
                prices[code] = bar

        # 2) 卖出调出目标的持仓（按 T+1 开盘价成交；无行情则保留，不再静默丢弃）
        sold_detail: list[dict[str, Any]] = []
        for code in list(positions):
            if code in target:
                continue
            bar = prices.get(code)
            if bar is None:
                continue
            price = float(bar["open"])
            shares = positions[code]["shares"]
            proceeds = shares * price
            fee = proceeds * commission
            cash += proceeds - fee
            sold_detail.append({"tsCode": code, "shares": shares, "price": price, "fee": fee,
                                "proceeds": proceeds, "tradeDate": bar["date"], "reason": "调出目标"})
            del positions[code]

        # 3) 目标等权调仓：按「当前权益 / N」分配，保留标的只做差额调整（不清仓重买）
        tradable = [code for code in target if code in prices]
        equity = cash + sum(
            positions[code]["shares"] * float(prices[code]["open"]) if code in prices
            else positions[code]["shares"] * float(positions[code]["last_price"])
            for code in positions
        )
        per_name = equity / len(tradable) if tradable else 0.0
        buy_detail: list[dict[str, Any]] = []
        for code in tradable:
            price = float(prices[code]["open"])
            desired = int(per_name / price / 100) * 100  # 100 份整手
            held = int(positions[code]["shares"]) if code in positions else 0
            delta = desired - held
            if delta > 0:
                cost = delta * price
                fee = cost * commission
                if cash < cost + fee:  # 现金不足时按可用现金下调（整手），不再整笔跳过
                    delta = int((cash / (1.0 + commission)) / price / 100) * 100
                    if delta <= 0:
                        continue
                    cost = delta * price
                    fee = cost * commission
                cash -= cost + fee
                prev_cost = float(positions[code]["avg_cost"]) if code in positions else price
                total_shares = held + delta
                positions[code] = {"shares": total_shares,
                                   "avg_cost": (held * prev_cost + delta * price) / total_shares,
                                   "last_price": price}
                buy_detail.append({"tsCode": code, "shares": delta, "price": price, "fee": fee,
                                   "tradeDate": prices[code]["date"], "t1Price": prices[code]["t1"]})
            elif delta < 0:
                shares = -delta
                proceeds = shares * price
                fee = proceeds * commission
                cash += proceeds - fee
                positions[code]["shares"] = held - shares
                sold_detail.append({"tsCode": code, "shares": shares, "price": price, "fee": fee,
                                    "proceeds": proceeds, "tradeDate": prices[code]["date"],
                                    "reason": "超配减仓"})

        # 4) 估值：持仓按 T+1 收盘价重估，无行情沿用上次估值
        for code, pos in positions.items():
            bar = prices.get(code)
            if bar is not None:
                pos["last_price"] = float(bar["close"])
        market_value = sum(float(p["shares"]) * float(p["last_price"]) for p in positions.values())

        # 5) 落账：只重写本 run 的持仓表（保留无法定价的仓位）
        con.execute("DELETE FROM etf_sim_positions WHERE run_id=?", (run_id,))
        for code, pos in positions.items():
            con.execute(
                "INSERT INTO etf_sim_positions (run_id, ts_code, shares, avg_cost, last_price, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, code, pos["shares"], pos["avg_cost"], pos["last_price"], _now()),
            )
        exec_dates = [bar["date"] for bar in prices.values()]
        val_date = max(exec_dates) if exec_dates else (signal_date or run["start_date"])
        nav = (cash + market_value) / initial_capital
        note = f"信号 {signal['signalDate']} · {'空仓防御' if signal['bearRegime'] else f'持仓 {len(target)} 只'} · T+1 开盘成交"
        con.execute(
            "INSERT OR REPLACE INTO etf_sim_daily (run_id, trade_date, nav, cash, market_value, position_count, "
            "bear_regime, note) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, val_date, nav, cash, market_value, len(positions), 1 if signal["bearRegime"] else 0, note),
        )
        con.execute("UPDATE etf_sim_runs SET last_signal_date=?, last_nav=?, status='active' WHERE run_id=?",
                    (signal["signalDate"], nav, run_id))
        con.commit()
        return {
            "ok": True, "runId": run_id, "signalDate": signal["signalDate"], "bearRegime": signal["bearRegime"],
            "nav": round(nav, 6), "cash": round(cash, 2), "marketValue": round(market_value, 2),
            "positions": [{"tsCode": code, "shares": pos["shares"], "avgCost": round(float(pos["avg_cost"]), 6),
                           "lastPrice": float(pos["last_price"])}
                          for code, pos in sorted(positions.items())],
            "trades": {"sold": sold_detail, "bought": buy_detail},
            "valuationDate": val_date,
        }
    finally:
        mkt.close()
        con.close()


def sim_status(etf_db_path: Path, run_id: str | None = None) -> dict[str, Any]:
    con = connect(etf_db_path)
    try:
        ensure_schema(con)
        if run_id is None:
            row = con.execute("SELECT * FROM etf_sim_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT * FROM etf_sim_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"run": None}
        positions = [dict(r) for r in con.execute(
            "SELECT ts_code, shares, avg_cost, last_price FROM etf_sim_positions WHERE run_id=? ORDER BY ts_code",
            (row["run_id"],))]
        last = con.execute(
            "SELECT trade_date tradeDate, nav, cash, market_value marketValue, position_count positionCount, "
            "bear_regime bearRegime, note FROM etf_sim_daily WHERE run_id=? ORDER BY trade_date DESC LIMIT 1",
            (row["run_id"],)).fetchone()
        return {
            "run": dict(row),
            "positions": positions,
            "latest": dict(last) if last else None,
            "totalReturn": round((float(row["last_nav"]) - 1.0), 6) if row["last_nav"] else None,
        }
    finally:
        con.close()


def sim_history(etf_db_path: Path, run_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    con = connect(etf_db_path)
    try:
        ensure_schema(con)
        if run_id is None:
            row = con.execute("SELECT run_id FROM etf_sim_runs ORDER BY created_at DESC LIMIT 1").fetchone()
            run_id = row["run_id"] if row else None
        if not run_id:
            return {"runId": None, "items": []}
        rows = con.execute(
            "SELECT trade_date tradeDate, nav, cash, market_value marketValue, position_count positionCount, "
            "bear_regime bearRegime, note FROM etf_sim_daily WHERE run_id=? ORDER BY trade_date DESC LIMIT ?",
            (run_id, limit)).fetchall()
        return {"runId": run_id, "items": [dict(r) for r in reversed(rows)]}
    finally:
        con.close()
