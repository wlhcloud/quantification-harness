"""
技术面择时模拟盘：多因子Top20候选 + MA5/MA10金叉死叉 + 5%止损
独立模块，零破坏：复用 stock_sim_* 表，用 model_id='ml_technical_timing' 标识
策略逻辑：
1. 候选池：selection_candidates 中 ml_walkforward 模型最新交易日的 Top20
2. 买入信号：MA5上穿MA10（金叉）+ 收盘价 > MA20（趋势向上）
3. 卖出信号：MA5下穿MA10（死叉）或 收盘价 < 买入价×0.95（止损5%）
4. 每天检查信号，T+1开盘成交，最多持有5只，等权分配
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path("D:/ProdProject/AI/quantification-harness")
FACTORS_DB = PROJECT / "data/factors.db"
MARKET_DB = PROJECT / "data/market.db"

MODEL_ID = "ml_technical_timing"
CANDIDATE_MODEL = "ml_walkforward"  # 候选池来源模型
TOP_N_CANDIDATES = 20  # 候选池大小
MAX_HOLDINGS = 5  # 最大持仓数
STOP_LOSS = 0.05  # 5%止损
COMMISSION = 0.00008  # 万0.8佣金
STAMP_DUTY = 0.0005  # 千0.5印花税（仅卖出）
LOT = 100  # A股一手
INITIAL_CAPITAL = 1_000_000.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    """复用 stock_sim_* 表结构（已存在则跳过）"""
    con.executescript("""
    CREATE TABLE IF NOT EXISTS stock_sim_runs (
      run_id TEXT PRIMARY KEY,
      model_id TEXT NOT NULL,
      top_n INTEGER NOT NULL,
      initial_capital REAL NOT NULL,
      commission_rate REAL NOT NULL DEFAULT 0.00008,
      stamp_duty_rate REAL NOT NULL DEFAULT 0.0005,
      status TEXT NOT NULL DEFAULT 'active',
      created_at TEXT NOT NULL,
      last_signal_date TEXT,
      last_nav REAL,
      regime_filter INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS stock_sim_daily (
      run_id TEXT NOT NULL,
      signal_date TEXT NOT NULL,
      trade_date TEXT NOT NULL,
      nav REAL NOT NULL,
      cash REAL NOT NULL,
      market_value REAL NOT NULL,
      position_count INTEGER NOT NULL,
      note TEXT,
      PRIMARY KEY (run_id, signal_date)
    );
    CREATE TABLE IF NOT EXISTS stock_sim_positions (
      run_id TEXT NOT NULL,
      code TEXT NOT NULL,
      shares INTEGER NOT NULL,
      avg_cost REAL NOT NULL,
      last_price REAL NOT NULL,
      high_price REAL NOT NULL DEFAULT 0,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (run_id, code)
    );
    """)
    # 零破坏迁移：补列
    cols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_runs)")}
    if "regime_filter" not in cols:
        con.execute("ALTER TABLE stock_sim_runs ADD COLUMN regime_filter INTEGER NOT NULL DEFAULT 0")
    pcols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_positions)")}
    if "high_price" not in pcols:
        con.execute("ALTER TABLE stock_sim_positions ADD COLUMN high_price REAL NOT NULL DEFAULT 0")


def get_or_create_run(con: sqlite3.Connection) -> str:
    """获取或创建技术面择时模拟盘run"""
    row = con.execute(
        "SELECT run_id FROM stock_sim_runs WHERE model_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
        (MODEL_ID,)
    ).fetchone()
    if row:
        return row["run_id"]
    # 创建新run
    run_id = f"tech-sim-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    con.execute(
        "INSERT INTO stock_sim_runs (run_id, model_id, top_n, initial_capital, commission_rate, "
        "stamp_duty_rate, status, created_at, last_signal_date, last_nav, regime_filter) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, MODEL_ID, MAX_HOLDINGS, INITIAL_CAPITAL, COMMISSION, STAMP_DUTY,
         "active", _now(), None, 1.0, 0)
    )
    con.execute(
        "INSERT INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, market_value, "
        "position_count, note) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, "INIT", "INIT", 1.0, INITIAL_CAPITAL, 0.0, 0,
         "技术面择时模拟盘启动 · 候选池ml_walkforward Top20 · 金叉买入/死叉或5%止损卖出")
    )
    con.commit()
    return run_id


def get_candidates(con: sqlite3.Connection, trade_date: str) -> list[str]:
    """获取指定交易日的候选股票（ml_walkforward Top20）"""
    # 找小于等于trade_date的最新信号日
    row = con.execute(
        "SELECT MAX(trade_date) d FROM selection_candidates WHERE model_id=? AND trade_date<=?",
        (CANDIDATE_MODEL, trade_date)
    ).fetchone()
    if not row or not row["d"]:
        return []
    signal_date = row["d"]
    targets = [r["code"] for r in con.execute(
        "SELECT code FROM selection_candidates WHERE model_id=? AND trade_date=? ORDER BY rank LIMIT ?",
        (CANDIDATE_MODEL, signal_date, TOP_N_CANDIDATES))]
    return targets


def compute_ma(mkt: sqlite3.Connection, code: str, trade_date: str, window: int) -> float | None:
    """计算指定交易日的N日均线"""
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM daily_bars WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT ?",
        (code, trade_date, window))]
    if len(rows) < window:
        return None
    return sum(rows) / window


def check_golden_cross(mkt: sqlite3.Connection, code: str, trade_date: str) -> bool:
    """检查金叉：MA5上穿MA10（昨日MA5<=MA10，今日MA5>MA10）"""
    # 获取最近11个交易日的收盘价
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM daily_bars WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 11",
        (code, trade_date))]
    if len(rows) < 11:
        return False
    # rows[0]=今日, rows[1]=昨日, ...
    # 今日MA5 = rows[0:5]均值, 昨日MA5 = rows[1:6]均值
    # 今日MA10 = rows[0:10]均值, 昨日MA10 = rows[1:11]均值
    ma5_today = sum(rows[0:5]) / 5
    ma5_yesterday = sum(rows[1:6]) / 5
    ma10_today = sum(rows[0:10]) / 10
    ma10_yesterday = sum(rows[1:11]) / 10
    return ma5_yesterday <= ma10_yesterday and ma5_today > ma10_today


def check_death_cross(mkt: sqlite3.Connection, code: str, trade_date: str) -> bool:
    """检查死叉：MA5下穿MA10"""
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM daily_bars WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 11",
        (code, trade_date))]
    if len(rows) < 11:
        return False
    ma5_today = sum(rows[0:5]) / 5
    ma5_yesterday = sum(rows[1:6]) / 5
    ma10_today = sum(rows[0:10]) / 10
    ma10_yesterday = sum(rows[1:11]) / 10
    return ma5_yesterday >= ma10_yesterday and ma5_today < ma10_today


def get_bar(mkt: sqlite3.Connection, code: str, trade_date: str) -> dict[str, float] | None:
    row = mkt.execute(
        "SELECT open, close FROM daily_bars WHERE code=? AND trade_date=?", (code, trade_date)
    ).fetchone()
    if not row or row["open"] is None:
        return None
    return {"open": float(row["open"]), "close": float(row["close"])}


def advance_technical_timing() -> dict[str, Any]:
    """推进技术面择时模拟盘：检查最新交易日的技术面信号，T+1开盘成交"""
    con = connect(FACTORS_DB)
    mkt = sqlite3.connect(f"file:{MARKET_DB.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    mkt.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        run_id = get_or_create_run(con)
        run = con.execute("SELECT * FROM stock_sim_runs WHERE run_id=?", (run_id,)).fetchone()

        # 获取最新交易日
        latest_row = mkt.execute("SELECT MAX(trade_date) d FROM daily_bars").fetchone()
        latest_date = latest_row["d"] if latest_row else None
        if not latest_date:
            return {"ok": False, "error": "无行情数据"}

        # 检查是否已经推进过该日期
        last_daily = con.execute(
            "SELECT signal_date FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,)
        ).fetchone()
        last_signal_date = last_daily["signal_date"] if last_daily else None
        if last_signal_date == latest_date:
            return {"ok": False, "waiting": True,
                    "message": f"信号日 {latest_date} 已推进，等待下一交易日"}

        # T+1交易日（信号日的下一个交易日）
        t1_row = mkt.execute(
            "SELECT MIN(trade_date) d FROM daily_bars WHERE trade_date>?", (latest_date,)
        ).fetchone()
        t1 = t1_row["d"] if t1_row else None
        if not t1:
            return {"ok": False, "waiting": True,
                    "message": f"信号日 {latest_date} 的T+1行情尚未生成，等待下一交易日"}

        # 获取当前持仓
        positions = {r["code"]: dict(r) for r in con.execute(
            "SELECT code, shares, avg_cost, last_price, high_price FROM stock_sim_positions WHERE run_id=?",
            (run_id,))}

        # 获取现金
        last_cash = con.execute(
            "SELECT cash FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)
        ).fetchone()
        cash = float(last_cash["cash"]) if last_cash else INITIAL_CAPITAL

        # 获取候选池
        candidates = get_candidates(con, latest_date)
        if not candidates:
            return {"ok": False, "error": f"无候选池数据（{latest_date}）"}

        # ========== 1. 检查卖出信号（死叉或止损） ==========
        sold = []
        for code in list(positions):
            bar = get_bar(mkt, code, latest_date)
            if bar is None:
                continue
            buy_price = float(positions[code]["avg_cost"])
            current_price = bar["close"]

            # 止损：亏损达5%
            if current_price < buy_price * (1 - STOP_LOSS):
                tbar = get_bar(mkt, code, t1)
                if tbar is None:
                    continue
                shares = int(positions[code]["shares"])
                proceeds = shares * tbar["open"]
                fee = proceeds * (COMMISSION + STAMP_DUTY)
                cash += proceeds - fee
                sold.append({"code": code, "shares": shares, "price": tbar["open"],
                             "fee": round(fee, 2), "reason": "stop_loss"})
                del positions[code]
                continue

            # 死叉
            if check_death_cross(mkt, code, latest_date):
                tbar = get_bar(mkt, code, t1)
                if tbar is None:
                    continue
                shares = int(positions[code]["shares"])
                proceeds = shares * tbar["open"]
                fee = proceeds * (COMMISSION + STAMP_DUTY)
                cash += proceeds - fee
                sold.append({"code": code, "shares": shares, "price": tbar["open"],
                             "fee": round(fee, 2), "reason": "death_cross"})
                del positions[code]

        # ========== 2. 检查买入信号（金叉 + 收盘价>MA20） ==========
        bought = []
        if len(positions) < MAX_HOLDINGS:
            # 按候选池顺序检查（ml_walkforward排名靠前的优先）
            for code in candidates:
                if len(positions) >= MAX_HOLDINGS:
                    break
                if code in positions:
                    continue

                # 金叉
                if not check_golden_cross(mkt, code, latest_date):
                    continue

                # 收盘价 > MA20
                ma20 = compute_ma(mkt, code, latest_date, 20)
                bar = get_bar(mkt, code, latest_date)
                if ma20 is None or bar is None or bar["close"] <= ma20:
                    continue

                # T+1开盘买入
                tbar = get_bar(mkt, code, t1)
                if tbar is None:
                    continue

                # 等权分配（按当前总资产/最大持仓数）
                total_assets = cash + sum(
                    int(p["shares"]) * float(get_bar(mkt, c, latest_date)["close"])
                    for c, p in positions.items() if get_bar(mkt, c, latest_date)
                )
                budget = total_assets / MAX_HOLDINGS
                shares = int((budget / tbar["open"]) // LOT * LOT)
                if shares <= 0:
                    continue
                cost = shares * tbar["open"]
                fee = cost * COMMISSION
                if cash < cost + fee:
                    continue
                cash -= cost + fee
                positions[code] = {"shares": shares, "avg_cost": tbar["open"],
                                   "last_price": tbar["close"], "high_price": tbar["open"]}
                bought.append({"code": code, "shares": shares, "price": round(tbar["open"], 4),
                               "fee": round(fee, 2), "tradeDate": t1})

        # ========== 3. 估值（T+1收盘价） ==========
        market_value = 0.0
        for code, pos in positions.items():
            bar = get_bar(mkt, code, t1)
            if bar:
                pos["last_price"] = bar["close"]
                if pos["high_price"] <= 0 or bar["close"] > pos["high_price"]:
                    pos["high_price"] = bar["close"]
            market_value += int(pos["shares"]) * float(pos["last_price"])

        # ========== 4. 落账 ==========
        con.execute("DELETE FROM stock_sim_positions WHERE run_id=?", (run_id,))
        for code, pos in positions.items():
            con.execute(
                "INSERT INTO stock_sim_positions (run_id, code, shares, avg_cost, last_price, high_price, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (run_id, code, pos["shares"], pos["avg_cost"], pos["last_price"], pos["high_price"], _now()))

        nav = (cash + market_value) / INITIAL_CAPITAL
        note = (f"技术面择时 · 信号{latest_date} · T+1 {t1} · "
                f"卖{len(sold)} 买{len(bought)} · 持{len(positions)}只 · "
                f"候选池{len(candidates)}只")
        if sold:
            reasons = {}
            for s in sold:
                reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
            note += f" · 卖出原因:{reasons}"

        con.execute(
            "INSERT OR REPLACE INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, "
            "market_value, position_count, note) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, latest_date, t1, nav, cash, market_value, len(positions), note))
        con.execute(
            "UPDATE stock_sim_runs SET last_signal_date=?, last_nav=? WHERE run_id=?",
            (latest_date, nav, run_id))
        con.commit()

        return {
            "ok": True, "runId": run_id, "signalDate": latest_date, "tradeDate": t1,
            "nav": round(nav, 6), "cash": round(cash, 2), "marketValue": round(market_value, 2),
            "positions": [{"code": k, "shares": v["shares"], "avgCost": v["avg_cost"],
                           "lastPrice": v["last_price"]} for k, v in positions.items()],
            "trades": {"sold": sold, "bought": bought},
            "candidateCount": len(candidates),
        }
    finally:
        mkt.close()
        con.close()


def get_status() -> dict[str, Any]:
    """获取技术面择时模拟盘状态"""
    con = connect(FACTORS_DB)
    try:
        ensure_schema(con)
        row = con.execute(
            "SELECT * FROM stock_sim_runs WHERE model_id=? AND status='active' ORDER BY created_at DESC LIMIT 1",
            (MODEL_ID,)
        ).fetchone()
        if not row:
            return {"run": None, "message": "技术面择时模拟盘尚未创建"}
        positions = [dict(r) for r in con.execute(
            "SELECT code, shares, avg_cost avgCost, last_price lastPrice FROM stock_sim_positions "
            "WHERE run_id=? ORDER BY code", (row["run_id"],))]
        last = con.execute(
            "SELECT signal_date signalDate, trade_date tradeDate, nav, cash, market_value marketValue, "
            "position_count positionCount, note FROM stock_sim_daily WHERE run_id=? "
            "ORDER BY rowid DESC LIMIT 1", (row["run_id"],)).fetchone()
        # 净值序列
        nav_series = [dict(r) for r in con.execute(
            "SELECT trade_date tradeDate, nav FROM stock_sim_daily WHERE run_id=? AND trade_date!='INIT' ORDER BY rowid",
            (row["run_id"],))]
        return {
            "run": dict(row), "positions": positions, "latest": dict(last) if last else None,
            "navSeries": nav_series,
            "totalReturn": round((float(row["last_nav"]) - 1.0), 6) if row["last_nav"] is not None else None
        }
    finally:
        con.close()


if __name__ == "__main__":
    import json
    result = advance_technical_timing()
    print(json.dumps(result, ensure_ascii=False, indent=2))
