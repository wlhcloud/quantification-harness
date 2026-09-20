"""
技术面择时模拟盘 V5：ML Top20候选 + 金叉/放量突破买入 + 死叉/移动止损卖出
独立模块，零破坏：复用 stock_sim_* 表，用 model_id='ml_technical_timing_v5' 标识
策略逻辑（与回测V5一致）：
1. 候选池：selection_candidates 中 ml_walkforward 模型最新交易日的 Top20
2. 买入信号（满足任一即可）：
   a. 金叉：MA5上穿MA10 + 收盘价 > MA20（趋势向上）
   b. 放量突破：成交量 > 5日均量2倍 + 收盘价创20日新高 + 收盘价 > MA20
3. 卖出信号（满足任一即可）：
   a. 死叉：MA5下穿MA10
   b. 移动止损：当前价 < max(买入价×0.90, 持仓最高价×0.85)
      - 初始止损：最多亏10%
      - 盈利后：从最高点回撤15%卖出
4. 每天14:40检查信号，当日尾盘成交，最多持有5只，等权分配
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT = Path("D:/ProdProject/AI/quantification-harness")
FACTORS_DB = PROJECT / "data/factors.db"
MARKET_DB = PROJECT / "data/market.db"

MODEL_ID = "ml_technical_timing_v5"  # V5移动止损，独立model_id不破坏V1
CANDIDATE_MODEL = "ml_walkforward"  # 候选池来源模型
TOP_N_CANDIDATES = 20  # 候选池大小
MAX_HOLDINGS = 5  # 最大持仓数
INITIAL_STOP_LOSS = 0.10  # 初始止损10%
TRAILING_DRAWDOWN = 0.15  # 移动止损回撤15%（从最高点回撤15%卖出）
COMMISSION = 0.00008  # 万0.8佣金
STAMP_DUTY = 0.0005  # 千0.5印花税（仅卖出）
SLIPPAGE = 0.001  # 基础滑点0.1%
TAIL_SLIPPAGE = 0.003  # 尾盘额外滑点0.3%（模拟14:40决策到收盘的波动+成交冲击）
LOT = 100  # A股一手
INITIAL_CAPITAL = 1_000_000.0
# 候选池过滤（与回测统一）
EXCLUDE_CODE_PREFIX = ('920', '688')  # 排除北交所、科创板
MIN_MKT_CAP = 200000  # 最小市值20亿（单位：万元，200000万=20亿）


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
    if "execution_mode" not in cols:
        con.execute("ALTER TABLE stock_sim_runs ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'legacy_t1_open'")
    # 不重写历史V5运行：旧运行可能消费过v1候选，应保持legacy并与新口径隔离。
    pcols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_positions)")}
    if "high_price" not in pcols:
        con.execute("ALTER TABLE stock_sim_positions ADD COLUMN high_price REAL NOT NULL DEFAULT 0")
    ccols = {r[1] for r in con.execute("PRAGMA table_info(selection_candidates)")}
    if ccols and "result_version" not in ccols:
        con.execute("ALTER TABLE selection_candidates ADD COLUMN result_version INTEGER NOT NULL DEFAULT 1")


def get_or_create_run(con: sqlite3.Connection) -> str:
    """获取或创建V5移动止损模拟盘run"""
    row = con.execute(
        "SELECT run_id FROM stock_sim_runs WHERE model_id=? AND status='active' "
        "AND execution_mode='same_day_close' ORDER BY created_at DESC LIMIT 1",
        (MODEL_ID,)
    ).fetchone()
    if row:
        return row["run_id"]
    # 创建新run
    run_id = f"tech-sim-v5-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    con.execute(
        "INSERT INTO stock_sim_runs (run_id, model_id, top_n, initial_capital, commission_rate, "
        "stamp_duty_rate, execution_mode, status, created_at, last_signal_date, last_nav, regime_filter) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, MODEL_ID, MAX_HOLDINGS, INITIAL_CAPITAL, COMMISSION, STAMP_DUTY,
         "same_day_close", "active", _now(), None, 1.0, 0)
    )
    con.execute(
        "INSERT INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, market_value, "
        "position_count, note) VALUES (?,?,?,?,?,?,?,?)",
        (run_id, "INIT", "INIT", 1.0, INITIAL_CAPITAL, 0.0, 0,
         "V5移动止损模拟盘启动 · 候选池ml_walkforward Top20 · 金叉买入/死叉或移动止损卖出")
    )
    con.commit()
    return run_id


def get_candidates(con: sqlite3.Connection, trade_date: str) -> list[str]:
    """获取指定交易日的候选股票（ml_walkforward Top20），并统一过滤：排除北交所/科创板、最小市值20亿"""
    row = con.execute(
        "SELECT MAX(trade_date) d FROM selection_candidates WHERE model_id=? AND trade_date<=? AND result_version>=2",
        (CANDIDATE_MODEL, trade_date)
    ).fetchone()
    if not row or not row["d"]:
        return []
    signal_date = row["d"]
    # JOIN因子表获取mktCap，统一过滤：排除920/688 + 最小市值20亿（与回测口径一致）
    targets = [r["code"] for r in con.execute(
        "SELECT sc.code FROM selection_candidates sc "
        "LEFT JOIN stock_ml_factors f ON sc.trade_date=f.trade_date AND sc.code=f.code "
        "WHERE sc.model_id=? AND sc.trade_date=? AND sc.result_version>=2 "
        "AND NOT (sc.code LIKE '920%' OR sc.code LIKE '688%') "
        "AND (f.mktCap IS NULL OR f.mktCap >= ?) "
        "ORDER BY sc.rank LIMIT ?",
        (CANDIDATE_MODEL, signal_date, MIN_MKT_CAP, TOP_N_CANDIDATES))]
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
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM daily_bars WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 11",
        (code, trade_date))]
    if len(rows) < 11:
        return False
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
        "SELECT open, high, low, close, volume FROM daily_bars WHERE code=? AND trade_date=?", (code, trade_date)
    ).fetchone()
    if not row or row["open"] is None:
        return None
    return {"open": float(row["open"]), "high": float(row["high"]), "low": float(row["low"]),
            "close": float(row["close"]), "volume": float(row["volume"])}


def compute_vol_ma5(mkt: sqlite3.Connection, code: str, trade_date: str) -> float | None:
    """计算指定交易日的5日均量（不含当日，用前5日）"""
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT volume FROM daily_bars WHERE code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 5",
        (code, trade_date))]
    if len(rows) < 5:
        return None
    return sum(rows) / 5


def check_20day_high(mkt: sqlite3.Connection, code: str, trade_date: str, close: float) -> bool:
    """检查收盘价是否创20日新高（与前19个交易日的最高收盘价比较）"""
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM daily_bars WHERE code=? AND trade_date<? ORDER BY trade_date DESC LIMIT 19",
        (code, trade_date))]
    if len(rows) < 19:
        return False
    return close >= max(rows)


def check_vol_breakout(mkt: sqlite3.Connection, code: str, trade_date: str) -> bool:
    """检查放量突破：成交量>5日均量2倍 + 收盘价创20日新高 + 收盘价>MA20"""
    bar = get_bar(mkt, code, trade_date)
    if bar is None:
        return False
    vol_ma5 = compute_vol_ma5(mkt, code, trade_date)
    if vol_ma5 is None or vol_ma5 <= 0:
        return False
    # 成交量 > 5日均量 × 2
    if bar["volume"] <= vol_ma5 * 2.0:
        return False
    # 收盘价创20日新高
    if not check_20day_high(mkt, code, trade_date, bar["close"]):
        return False
    # 收盘价 > MA20
    ma20 = compute_ma(mkt, code, trade_date, 20)
    if ma20 is None or bar["close"] <= ma20:
        return False
    return True


def is_limit_up(mkt: sqlite3.Connection, code: str, trade_date: str) -> bool:
    """按PIT状态表的当日涨停价判断，兼容ST/创业板/北交所等不同限制。"""
    bar = get_bar(mkt, code, trade_date)
    if bar is None:
        return False
    status = mkt.execute(
        "SELECT is_suspended,limit_up FROM security_status_history WHERE code=? AND trade_date=?",
        (code, trade_date)).fetchone()
    if status is None or status[0] or status[1] is None:
        return False
    return float(bar["close"]) >= float(status[1]) - 0.005


def is_limit_down(mkt: sqlite3.Connection, code: str, trade_date: str) -> bool:
    """按PIT状态表的当日跌停价判断。"""
    bar = get_bar(mkt, code, trade_date)
    if bar is None:
        return False
    status = mkt.execute(
        "SELECT is_suspended,limit_down FROM security_status_history WHERE code=? AND trade_date=?",
        (code, trade_date)).fetchone()
    if status is None or status[0] or status[1] is None:
        return False
    return float(bar["close"]) <= float(status[1]) + 0.005


def calc_stop_loss_price(buy_price: float, high_price: float) -> float:
    """计算移动止损线：max(初始止损价, 最高价×(1-回撤比例))"""
    initial_stop = buy_price * (1 - INITIAL_STOP_LOSS)
    trailing_stop = high_price * (1 - TRAILING_DRAWDOWN)
    return max(initial_stop, trailing_stop)


def advance_technical_timing_v5() -> dict[str, Any]:
    """推进V5移动止损模拟盘：检查最新交易日的技术面信号，尾盘收盘价成交（模拟14:40决策+收盘前成交）"""
    con = connect(FACTORS_DB)
    mkt = sqlite3.connect(f"file:{MARKET_DB.resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    mkt.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        run_id = get_or_create_run(con)
        run = con.execute("SELECT * FROM stock_sim_runs WHERE run_id=?", (run_id,)).fetchone()

        # 获取最新交易日（数据截止日）
        latest_row = mkt.execute("SELECT MAX(trade_date) d FROM daily_bars").fetchone()
        latest_date = latest_row["d"] if latest_row else None
        if not latest_date:
            return {"ok": False, "error": "无行情数据"}

        # 获取已推进的最后一个信号日
        last_daily = con.execute(
            "SELECT signal_date FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1",
            (run_id,)
        ).fetchone()
        last_signal_date = last_daily["signal_date"] if last_daily else None
        # 初始记录的signal_date为'INIT'，视为未推进
        if last_signal_date == "INIT":
            last_signal_date = None

        # 确定本次要推进的信号日：last_signal_date之后的第一个交易日
        if last_signal_date is None:
            signal_row = mkt.execute(
                "SELECT MIN(trade_date) d FROM daily_bars WHERE trade_date>='20240101'").fetchone()
        else:
            signal_row = mkt.execute(
                "SELECT MIN(trade_date) d FROM daily_bars WHERE trade_date>?", (last_signal_date,)).fetchone()
        signal_date = signal_row["d"] if signal_row else None

        if signal_date is None or signal_date > latest_date:
            return {"ok": False, "waiting": True,
                    "message": f"已推进到 {last_signal_date}，无新交易日可推进"}

        # 用信号日作为本次处理日期（尾盘成交：信号日收盘价成交）
        latest_date = signal_date

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

        # ========== 1. 检查卖出信号（死叉或移动止损），尾盘收盘价成交 ==========
        sold = []
        for code in list(positions):
            bar = get_bar(mkt, code, latest_date)
            if bar is None:
                continue
            buy_price = float(positions[code]["avg_cost"])
            high_price = float(positions[code]["high_price"])
            current_price = bar["close"]

            # 跌停封板无法卖出（实盘限制），当天跳过
            if is_limit_down(mkt, code, latest_date):
                if current_price > high_price:
                    positions[code]["high_price"] = current_price
                continue

            # 更新持仓最高价
            if current_price > high_price:
                high_price = current_price
                positions[code]["high_price"] = high_price

            # 计算移动止损线
            stop_price = calc_stop_loss_price(buy_price, high_price)

            # 移动止损：当前价 < 止损线
            if current_price < stop_price:
                if abs(stop_price - buy_price * (1 - INITIAL_STOP_LOSS)) < 1e-6:
                    reason = "initial_stop_loss"
                else:
                    reason = "trailing_stop_loss"
                # 尾盘收盘价卖出（加尾盘滑点）
                sell_price = current_price * (1 - SLIPPAGE - TAIL_SLIPPAGE)
                shares = int(positions[code]["shares"])
                proceeds = shares * sell_price
                fee = proceeds * (COMMISSION + STAMP_DUTY)
                cash += proceeds - fee
                sold.append({"code": code, "shares": shares, "price": round(sell_price, 4),
                             "fee": round(fee, 2), "reason": reason,
                             "buyPrice": buy_price, "highPrice": high_price,
                             "stopPrice": round(stop_price, 4)})
                del positions[code]
                continue

            # 死叉
            if check_death_cross(mkt, code, latest_date):
                sell_price = current_price * (1 - SLIPPAGE - TAIL_SLIPPAGE)
                shares = int(positions[code]["shares"])
                proceeds = shares * sell_price
                fee = proceeds * (COMMISSION + STAMP_DUTY)
                cash += proceeds - fee
                sold.append({"code": code, "shares": shares, "price": round(sell_price, 4),
                             "fee": round(fee, 2), "reason": "death_cross"})
                del positions[code]

        # ========== 2. 检查买入信号（金叉/放量突破），尾盘收盘价成交 ==========
        bought = []
        if len(positions) < MAX_HOLDINGS:
            for code in candidates:
                if len(positions) >= MAX_HOLDINGS:
                    break
                if code in positions:
                    continue

                # 买入信号（满足任一即可）：
                # a. 金叉 + 收盘价 > MA20
                # b. 放量突破（量>5日均量2倍 + 创20日新高 + close>MA20）
                bar = get_bar(mkt, code, latest_date)
                # 涨停封板无法买入（实盘限制）
                if bar is not None and is_limit_up(mkt, code, latest_date):
                    continue
                golden_valid = False
                vol_breakout_valid = False
                if bar is not None:
                    if check_golden_cross(mkt, code, latest_date):
                        ma20 = compute_ma(mkt, code, latest_date, 20)
                        if ma20 is not None and bar["close"] > ma20:
                            golden_valid = True
                    if check_vol_breakout(mkt, code, latest_date):
                        vol_breakout_valid = True

                if not golden_valid and not vol_breakout_valid:
                    continue

                if golden_valid and vol_breakout_valid:
                    signal_type = "both"
                elif golden_valid:
                    signal_type = "golden_cross"
                else:
                    signal_type = "vol_breakout"

                # 尾盘收盘价买入（加尾盘滑点）
                buy_price = bar["close"] * (1 + SLIPPAGE + TAIL_SLIPPAGE)

                # 等权分配
                total_assets = cash + sum(
                    int(p["shares"]) * float(get_bar(mkt, c, latest_date)["close"])
                    for c, p in positions.items() if get_bar(mkt, c, latest_date)
                )
                budget = total_assets / MAX_HOLDINGS
                shares = int((budget / buy_price) // LOT * LOT)
                if shares <= 0:
                    continue
                cost = shares * buy_price
                fee = cost * COMMISSION
                if cash < cost + fee:
                    continue
                cash -= cost + fee
                positions[code] = {"shares": shares, "avg_cost": buy_price,
                                   "last_price": bar["close"], "high_price": bar["close"]}
                bought.append({"code": code, "shares": shares, "price": round(buy_price, 4),
                               "fee": round(fee, 2), "tradeDate": latest_date,
                               "signal": signal_type,
                               "initialStop": round(buy_price * (1 - INITIAL_STOP_LOSS), 4)})

        # ========== 3. 估值（当日收盘价） ==========
        market_value = 0.0
        for code, pos in positions.items():
            bar = get_bar(mkt, code, latest_date)
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
        note = (f"V5移动止损 · 信号{latest_date} · 尾盘成交 · "
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
            (run_id, latest_date, latest_date, nav, cash, market_value, len(positions), note))
        con.execute(
            "UPDATE stock_sim_runs SET last_signal_date=?, last_nav=? WHERE run_id=?",
            (latest_date, nav, run_id))
        con.commit()

        return {
            "ok": True, "runId": run_id, "modelId": MODEL_ID,
            "signalDate": latest_date, "tradeDate": latest_date,
            "executionMode": "same_day_close", "snapshotTime": "14:40",
            "snapshotSource": "close_proxy",
            "nav": round(nav, 6), "cash": round(cash, 2), "marketValue": round(market_value, 2),
            "totalReturn": round(nav - 1.0, 6),
            "positions": [{"code": k, "shares": v["shares"], "avgCost": v["avg_cost"],
                           "lastPrice": v["last_price"], "highPrice": v["high_price"],
                           "stopPrice": round(calc_stop_loss_price(v["avg_cost"], v["high_price"]), 4)}
                          for k, v in positions.items()],
            "trades": {"sold": sold, "bought": bought},
            "candidateCount": len(candidates),
            "strategy": "V5移动止损：金叉/放量突破买入 + 死叉/移动止损(初始10%/回撤15%)卖出",
        }
    finally:
        mkt.close()
        con.close()


def get_status() -> dict[str, Any]:
    """获取V5移动止损模拟盘状态"""
    con = connect(FACTORS_DB)
    try:
        ensure_schema(con)
        row = con.execute(
            "SELECT * FROM stock_sim_runs WHERE model_id=? AND status='active' "
            "AND execution_mode='same_day_close' ORDER BY created_at DESC LIMIT 1",
            (MODEL_ID,)
        ).fetchone()
        if not row:
            return {"run": None, "message": "V5移动止损模拟盘尚未创建"}
        positions = [dict(r) for r in con.execute(
            "SELECT code, shares, avg_cost avgCost, last_price lastPrice, high_price highPrice FROM stock_sim_positions "
            "WHERE run_id=? ORDER BY code", (row["run_id"],))]
        # 计算每只持仓的止损线
        for p in positions:
            p["stopPrice"] = round(calc_stop_loss_price(p["avgCost"], p["highPrice"]), 4)
        last = con.execute(
            "SELECT signal_date signalDate, trade_date tradeDate, nav, cash, market_value marketValue, "
            "position_count positionCount, note FROM stock_sim_daily WHERE run_id=? "
            "ORDER BY rowid DESC LIMIT 1", (row["run_id"],)).fetchone()
        nav_series = [dict(r) for r in con.execute(
            "SELECT trade_date tradeDate, nav FROM stock_sim_daily WHERE run_id=? AND trade_date!='INIT' ORDER BY rowid",
            (row["run_id"],))]
        return {
            "run": dict(row), "positions": positions, "latest": dict(last) if last else None,
            "navSeries": nav_series,
            "totalReturn": round((float(row["last_nav"]) - 1.0), 6) if row["last_nav"] is not None else None,
            "strategy": "V5移动止损：金叉/放量突破买入 + 死叉/移动止损(初始10%/回撤15%)卖出",
        }
    finally:
        con.close()


if __name__ == "__main__":
    import json
    result = advance_technical_timing_v5()
    print(json.dumps(result, ensure_ascii=False, indent=2))
