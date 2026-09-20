"""股票 14:40 信号、当日尾盘成交模拟盘：真实跟踪 selection_candidates 信号。

纯增量模块（零破坏）：仅在 factors.db 新增 stock_sim_runs / stock_sim_daily /
stock_sim_positions 三张台账表，不修改既有 selection_candidates
及任何股票既有逻辑。执行假设（与（已退役的）月度回测 run_monthly 的成本口径一致）：
  - 信号：selection_candidates 中指定模型最新交易日的 TopN（rank 升序）
  - 成交：信号日收盘价作为 14:40 后尾盘成交代理，并计入尾盘滑点
  - 成本：买卖佣金 0.00008（万0.8）；卖出另收印花税 0.0005（万5，仅卖方）
  - 整手：A 股 100 股一手，向下取整
  - 换手：先卖出退出目标的持仓回笼现金，再对新增目标按可用现金等权整手买入；
    保留在目标内的持仓不动（最小换手等权）
  - 估值：信号日收盘价；NAV = (现金 + 持仓市值) / 初始资金
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..stock_ml.config_integrity import load_config_json, stamp_run_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_sim_runs (
  run_id TEXT PRIMARY KEY,
  model_id TEXT NOT NULL,
  top_n INTEGER NOT NULL,
  initial_capital REAL NOT NULL,
  commission_rate REAL NOT NULL DEFAULT 0.00008,
  stamp_duty_rate REAL NOT NULL DEFAULT 0.0005,
  slippage_rate REAL NOT NULL DEFAULT 0.004,
  execution_mode TEXT NOT NULL DEFAULT 'same_day_close',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL,
  last_signal_date TEXT,
  last_nav REAL,
  -- 配置可观测性（2026-09-20）：完整生效配置 + 指纹 + 代码版本。
  -- 此前只有下面那些扁平标量列，featuresOverride/publishGate/technicalTiming
  -- 等都没存，导致"这个模拟盘当初到底按什么配置跑的"无法重建。
  config_json TEXT,
  config_sha256 TEXT,
  config_yaml_sha256 TEXT,
  git_commit TEXT,
  git_dirty INTEGER
);CREATE TABLE IF NOT EXISTS stock_sim_daily (
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
  last_adj_factor REAL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, code)
);
"""

DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_COMMISSION = 0.00008  # 万0.8（双边）
DEFAULT_STAMP_DUTY = 0.0005  # 千0.5（仅卖出）
DEFAULT_SLIPPAGE = 0.004  # 10bp基础滑点 + 30bp尾盘价格漂移/冲击代理
LOT = 100  # A 股一手
DEFAULT_REBALANCE_DAYS = 1  # 非ML旧模拟盘保持每个信号调仓；ML由stock-ml配置覆盖为3
DEFAULT_HOLDING_BUFFER_RANK = 10
TAIL_INITIAL_STOP_PCT = 0.05
TAIL_TRAILING_PCT = 0.08


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # 零破坏迁移：旧表补 regime_filter 列（旧 run 默认 0=不择时）
    cols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_runs)")}
    if "regime_filter" not in cols:
        con.execute("ALTER TABLE stock_sim_runs ADD COLUMN regime_filter INTEGER NOT NULL DEFAULT 0")
    if "slippage_rate" not in cols:
        con.execute(f"ALTER TABLE stock_sim_runs ADD COLUMN slippage_rate REAL NOT NULL DEFAULT {DEFAULT_SLIPPAGE}")
    if "execution_mode" not in cols:
        # 存量普通模拟盘是旧T+1口径，禁止继续推进造成一条净值混合两套成交规则。
        con.execute("ALTER TABLE stock_sim_runs ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'legacy_t1_open'")
    for name, ddl in (
        ("rebalance_days", f"INTEGER NOT NULL DEFAULT {DEFAULT_REBALANCE_DAYS}"),
        ("holding_buffer_rank", f"INTEGER NOT NULL DEFAULT {DEFAULT_HOLDING_BUFFER_RANK}"),
        ("initial_stop_loss", f"REAL NOT NULL DEFAULT {TAIL_INITIAL_STOP_PCT}"),
        ("trailing_drawdown", f"REAL NOT NULL DEFAULT {TAIL_TRAILING_PCT}"),
        ("last_rebalance_date", "TEXT"),
        # 配置可观测性：旧 run 这几列为 NULL = "该次运行早于档案机制"（未知），
        # 不是"没有配置"——它们当时用的是上面那些标量列的默认值。
        ("config_json", "TEXT"),
        ("config_sha256", "TEXT"),
        ("config_yaml_sha256", "TEXT"),
        ("git_commit", "TEXT"),
        ("git_dirty", "INTEGER"),
    ):
        if name not in cols:
            con.execute(f"ALTER TABLE stock_sim_runs ADD COLUMN {name} {ddl}")
    # 零破坏迁移：positions 补 high_price（尾部移动止损基准价，旧仓默认=0 不触发）
    pcols = {r[1] for r in con.execute("PRAGMA table_info(stock_sim_positions)")}
    if "high_price" not in pcols:
        con.execute("ALTER TABLE stock_sim_positions ADD COLUMN high_price REAL NOT NULL DEFAULT 0")
    if "last_adj_factor" not in pcols:
        con.execute("ALTER TABLE stock_sim_positions ADD COLUMN last_adj_factor REAL")
    ccols = {r[1] for r in con.execute("PRAGMA table_info(selection_candidates)")}
    if ccols and "result_version" not in ccols:
        con.execute("ALTER TABLE selection_candidates ADD COLUMN result_version INTEGER NOT NULL DEFAULT 1")


# ---------------------------------------------------------------- 信号 / 行情

def latest_signal(factors_con: sqlite3.Connection, model_id: str, top_n: int,
                  after_date: str | None = None) -> dict[str, Any]:
    """选股信号。after_date=None 取全局最早信号（首次逐期推进）；
    给 after_date 则取严格晚于它的最早信号（按时间顺序，不跳过中间信号）。"""
    if model_id == "ml_walkforward":
        source = ("selection_candidates c JOIN stock_walkforward_runs r ON r.run_id=c.source_run_id "
                  "AND r.is_published=1")
        version_clause = " AND c.result_version>=2"
        model_col, date_col = "c.model_id", "c.trade_date"
    else:
        source = "selection_candidates c"
        version_clause = ""
        model_col, date_col = "c.model_id", "c.trade_date"
    if after_date:
        row = factors_con.execute(
            f"SELECT MIN({date_col}) d FROM {source} WHERE {model_col}=? AND {date_col}>?" + version_clause,
            (model_id, after_date)
        ).fetchone()
    else:
        row = factors_con.execute(
            f"SELECT MIN({date_col}) d FROM {source} WHERE {model_col}=?" + version_clause, (model_id,)
        ).fetchone()
    signal_date = row["d"] if row else None
    if not signal_date:
        return {"signalDate": None, "targets": []}
    targets = [r["code"] for r in factors_con.execute(
        f"SELECT c.code FROM {source} WHERE {model_col}=? AND {date_col}=?" +
        version_clause + " ORDER BY c.rank LIMIT ?",
        (model_id, signal_date, top_n))]
    return {"signalDate": signal_date, "targets": targets}


def newest_signal_date(factors_con: sqlite3.Connection, model_id: str, top_n: int) -> dict[str, Any]:
    """全局最新信号（start 时展示当前候选规模用）。"""
    if model_id == "ml_walkforward":
        source = ("selection_candidates c JOIN stock_walkforward_runs r ON r.run_id=c.source_run_id "
                  "AND r.is_published=1")
        version_clause = " AND c.result_version>=2"
    else:
        source = "selection_candidates c"
        version_clause = ""
    row = factors_con.execute(
        f"SELECT MAX(c.trade_date) d FROM {source} WHERE c.model_id=?" + version_clause, (model_id,)
    ).fetchone()
    signal_date = row["d"] if row else None
    if not signal_date:
        return {"signalDate": None, "targets": []}
    targets = [r["code"] for r in factors_con.execute(
        f"SELECT c.code FROM {source} WHERE c.model_id=? AND c.trade_date=?" +
        version_clause + " ORDER BY c.rank LIMIT ?",
        (model_id, signal_date, top_n))]
    return {"signalDate": signal_date, "targets": targets}


def _next_trade_date(mkt: sqlite3.Connection, after_date: str) -> str | None:
    row = mkt.execute(
        "SELECT MIN(trade_date) d FROM daily_bars WHERE trade_date>?", (after_date,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def _bar(mkt: sqlite3.Connection, code: str, trade_date: str) -> dict[str, float] | None:
    has_status = mkt.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='security_status_history'").fetchone()
    has_adjustment = mkt.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adjustment_factors'").fetchone()
    adj_select = ",a.adj_factor" if has_adjustment else ",NULL adj_factor"
    adj_join = (" LEFT JOIN adjustment_factors a ON a.code=d.code AND a.trade_date=d.trade_date "
                if has_adjustment else " ")
    if has_status:
        row = mkt.execute(
            "SELECT d.open,d.high,d.low,d.close,d.pct_chg,s.is_suspended,s.limit_up,s.limit_down" + adj_select + " "
            "FROM daily_bars d LEFT JOIN security_status_history s "
            "ON s.code=d.code AND s.trade_date=d.trade_date" + adj_join +
            "WHERE d.code=? AND d.trade_date=?",
            (code, trade_date)).fetchone()
    else:
        row = mkt.execute(
            "SELECT d.open,d.high,d.low,d.close,d.pct_chg,NULL is_suspended,NULL limit_up,NULL limit_down" +
            adj_select + " FROM daily_bars d" + adj_join + "WHERE d.code=? AND d.trade_date=?",
            (code, trade_date)).fetchone()
    if not row or row["open"] is None:
        return None
    return {k: (float(row[k]) if row[k] is not None else None)
            for k in ("open", "high", "low", "close", "pct_chg", "limit_up", "limit_down", "adj_factor")} | {
                "is_suspended": bool(row["is_suspended"])}


def _can_trade(bar: dict[str, Any] | None, side: str) -> bool:
    if not bar or bar.get("close") is None or bar.get("is_suspended"):
        return False
    close = float(bar["close"])
    if side == "buy" and bar.get("limit_up") is not None:
        return close < float(bar["limit_up"]) - 0.005
    if side == "sell" and bar.get("limit_down") is not None:
        return close > float(bar["limit_down"]) + 0.005
    pct = bar.get("pct_chg")
    if side == "buy" and pct is not None and float(pct) >= 4.8 and bar.get("high") == bar.get("close"):
        return False
    if side == "sell" and pct is not None and float(pct) <= -4.8 and bar.get("low") == bar.get("close"):
        return False
    return True


REGIME_INDEX = "000300.SH"  # 沪深300
REGIME_MA_WINDOW = 20


def _bear_regime(mkt: sqlite3.Connection, signal_date: str, window: int = REGIME_MA_WINDOW) -> bool:
    """信号日及之前沪深300 收盘 close < MA20 判熊市（无未来函数，与 ETF regimeFilter 同口径）。"""
    rows = [float(r[0]) for r in mkt.execute(
        "SELECT close FROM index_daily_bars WHERE ts_code=? AND trade_date<=? ORDER BY trade_date",
        (REGIME_INDEX, signal_date))]
    if len(rows) < window:
        return False  # 历史不足默认不判熊
    ma = sum(rows[-window:]) / window
    return rows[-1] < ma


# ---------------------------------------------------------------- 主流程

def list_runs(factors_path: Path, include_archived: bool = False) -> dict[str, Any]:
    """列出模拟盘 run（含净值序列，供前端多 run 切换/对比）。

    include_archived=False（默认）时过滤掉 status='archived' 的历史/实验盘：
    这些盘已不再推进（每日链只推进"最新 run"），留在下拉里会让人看到过期持仓。
    """
    con = connect(factors_path)
    try:
        ensure_schema(con)
        sql = ("SELECT run_id, model_id, top_n, initial_capital, regime_filter,execution_mode,status, created_at, "
               "last_signal_date, last_nav, config_json, config_sha256, git_commit, git_dirty FROM stock_sim_runs")
        params: tuple = ()
        if not include_archived:
            sql += " WHERE status<>'archived'"
        sql += " ORDER BY created_at"
        rows = con.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            dailies = [dict(x) for x in con.execute(
                "SELECT signal_date, trade_date, nav FROM stock_sim_daily "
                "WHERE run_id=? AND trade_date!='INIT' ORDER BY rowid", (r["run_id"],))]
            items.append({
                "runId": r["run_id"], "modelId": r["model_id"], "topN": r["top_n"],
                "initialCapital": r["initial_capital"], "regimeFilter": bool(r["regime_filter"]),
                "executionMode": r["execution_mode"], "legacy": r["execution_mode"] != "same_day_close",
                "status": r["status"], "createdAt": r["created_at"],
                "lastSignalDate": r["last_signal_date"], "lastNav": r["last_nav"],
                # 配置可观测性：完整生效配置 + 指纹 + 代码版本。
                # 旧 run 这几列为 NULL = 早于档案机制（未知），别读成"没有配置"。
                "config": load_config_json(r["config_json"]),
                "configSha256": r["config_sha256"],
                "gitCommit": r["git_commit"],
                "gitDirty": None if r["git_dirty"] is None else bool(r["git_dirty"]),
                "navSeries": [{"tradeDate": x["trade_date"], "nav": x["nav"]} for x in dailies],
            })
        return {"items": items}
    finally:
        con.close()


def start_sim(factors_path: Path, model_id: str = "balanced", top_n: int = 30,
              initial_capital: float = DEFAULT_CAPITAL,
              commission_rate: float = DEFAULT_COMMISSION,
              stamp_duty_rate: float = DEFAULT_STAMP_DUTY,
              slippage_rate: float = DEFAULT_SLIPPAGE,
              regime_filter: bool = False, rebalance_days: int = DEFAULT_REBALANCE_DAYS,
              holding_buffer_rank: int | None = None,
              initial_stop_loss: float = TAIL_INITIAL_STOP_PCT,
              trailing_drawdown: float = TAIL_TRAILING_PCT,
              snapshot_root: Path | None = None,
              source_config: dict[str, Any] | None = None,
              source_config_path: Path | None = None) -> dict[str, Any]:
    """创建模拟盘 run。

    ``snapshot_root`` 是配置快照的归档根（通常是 ``artifacts``）；为 None 时只写
    数据库档案、不落快照文件。

    ``source_config`` / ``source_config_path`` 是本次参数的来源配置（ML 模拟盘为
    ``stock-ml.yaml`` 的完整内容）。模拟盘的 topN、止损、择时开关都派生自它，只把
    解析后的数字写进各标量列，事后无法回答"这些数字是哪份配置给的"——因此整份冻结。
    """
    con = connect(factors_path)
    try:
        ensure_schema(con)
        signal = newest_signal_date(con, model_id, top_n)
        # 无信号时原先照常建 run，只会写一行 INIT（nav 恒 1.0、持仓 0），
        # 用户看到"创建成功"却永远不动账——这里直接拒绝，把问题暴露在创建时刻。
        if not signal["signalDate"]:
            raise ValueError(
                f"模型 {model_id} 当前没有可用的选股信号（selection_candidates 为空），"
                "无法建立模拟盘；请先运行该模型的选股/训练任务"
            )
        run_id = f"stock-sim-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
        effective_config = {
            "kind": "stock_sim",
            "modelId": model_id,
            "topN": top_n,
            "initialCapital": initial_capital,
            "commissionRate": commission_rate,
            "stampDutyRate": stamp_duty_rate,
            "slippageRate": slippage_rate,
            "regimeFilter": bool(regime_filter),
            "rebalanceDays": max(1, rebalance_days),
            # 实际写入表的生效值（holding_buffer_rank 为空时按 topN 取，与下表一致）
            "holdingBufferRank": max(top_n, holding_buffer_rank or top_n),
            "initialStopLoss": initial_stop_loss,
            "trailingDrawdown": trailing_drawdown,
            "executionMode": "same_day_close",
            "signalDate": signal["signalDate"],
        }
        if source_config is not None:
            # 冻结来源配置全文：模拟盘参数由它派生，缺了它就无法复核参数从何而来。
            effective_config["sourceConfig"] = source_config
        columns: dict[str, Any] = {}
        stamp_run_config(con, columns, effective_config, run_id=run_id,
                         generated_at=_now(), snapshot_root=snapshot_root,
                         yaml_path=source_config_path)
        con.execute(
            "INSERT INTO stock_sim_runs (run_id, model_id, top_n, initial_capital, commission_rate, "
            "stamp_duty_rate, slippage_rate, execution_mode, status, created_at, last_signal_date, last_nav, regime_filter,"
            "rebalance_days,holding_buffer_rank,initial_stop_loss,trailing_drawdown,last_rebalance_date,"
            "config_json,config_sha256,config_yaml_sha256,git_commit,git_dirty) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, model_id, top_n, initial_capital, commission_rate, stamp_duty_rate,
             slippage_rate, "same_day_close", "active", _now(), None, 1.0, 1 if regime_filter else 0,
             max(1, rebalance_days), max(top_n, holding_buffer_rank or top_n), initial_stop_loss,
             trailing_drawdown, None,
             columns["config_json"], columns["config_sha256"], columns["config_yaml_sha256"],
             columns["git_commit"], columns["git_dirty"]),
        )
        con.execute(
            "INSERT INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, market_value, "
            "position_count, note) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, "INIT", "INIT", 1.0, initial_capital, 0.0, 0,
             f"起点 · 当前最新信号 {signal['signalDate']}（Top{top_n} {len(signal['targets'])} 只）· 14:40信号/尾盘成交"
             + (" · 已开启沪深300 MA20 熊市空仓" if regime_filter else "")),
        )
        con.commit()
        return {"runId": run_id, "modelId": model_id, "topN": top_n,
                "signalDate": signal["signalDate"], "targetCount": len(signal["targets"]),
                "regimeFilter": regime_filter,
                "nav": 1.0, "cash": initial_capital, "marketValue": 0.0}
    finally:
        con.close()


def advance_sim(factors_path: Path, market_path: Path, run_id: str) -> dict[str, Any]:
    con = connect(factors_path)
    mkt = sqlite3.connect(f"file:{Path(market_path).resolve().as_posix()}?mode=ro", uri=True, timeout=30)
    mkt.row_factory = sqlite3.Row
    try:
        ensure_schema(con)
        run = con.execute("SELECT * FROM stock_sim_runs WHERE run_id=?", (run_id,)).fetchone()
        if not run:
            return {"ok": False, "error": f"run 不存在: {run_id}"}
        if run["execution_mode"] != "same_day_close":
            return {"ok": False, "legacy": True,
                    "message": "该模拟盘使用旧T+1口径，已停止推进；请新建same_day_close模拟盘"}
        signal = latest_signal(con, run["model_id"], max(run["top_n"], run["holding_buffer_rank"]),
                               run["last_signal_date"])
        signal_date = signal["signalDate"]
        ranked = signal["targets"]
        if not signal_date:
            return {"ok": False, "waiting": True,
                    "message": "没有晚于上次处理日的新选股信号，等待每日因子链更新"}
        trade_date = signal_date
        if not mkt.execute("SELECT 1 FROM daily_bars WHERE trade_date=? LIMIT 1", (trade_date,)).fetchone():
            return {"ok": False, "waiting": True, "signalDate": signal_date,
                    "message": f"信号 {signal_date} 的尾盘成交代理行情尚未生成"}

        positions = {r["code"]: dict(r) for r in con.execute(
            "SELECT code, shares, avg_cost, last_price, high_price, last_adj_factor "
            "FROM stock_sim_positions WHERE run_id=?",
            (run_id,))}
        for p in positions.values():
            p["high_price"] = float(p["high_price"]) if p["high_price"] else 0.0
        last_rebalance = run["last_rebalance_date"]
        elapsed = (mkt.execute(
            "SELECT COUNT(DISTINCT trade_date) FROM daily_bars WHERE trade_date>? AND trade_date<=?",
            (last_rebalance, signal_date)).fetchone()[0] if last_rebalance else run["rebalance_days"])
        rebalance_due = not positions or elapsed >= int(run["rebalance_days"])

        # 现有现金：从最近一条 daily 取（比用 initial 更准确，承接历次调仓后的余额）
        last_cash = con.execute(
            "SELECT cash FROM stock_sim_daily WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)
        ).fetchone()
        cash = float(last_cash["cash"]) if last_cash else float(run["initial_capital"])
        comm, stamp = float(run["commission_rate"]), float(run["stamp_duty_rate"])
        slip = float(run["slippage_rate"])

        # 复权因子变化时同步调整持仓数量和成本基准，避免除权日造成模拟盘
        # 净值断崖或误触移动止损。新买入仍严格按100股整手。
        for code, pos in positions.items():
            bar = _bar(mkt, code, signal_date)
            current_adj = bar.get("adj_factor") if bar else None
            previous_adj = pos.get("last_adj_factor")
            if current_adj is not None and previous_adj not in (None, 0):
                ratio = float(current_adj) / float(previous_adj)
                if abs(ratio - 1.0) > 1e-12:
                    pos["shares"] = float(pos["shares"]) * ratio
                    pos["avg_cost"] = float(pos["avg_cost"]) / ratio
                    pos["high_price"] = float(pos["high_price"]) / ratio
            if current_adj is not None:
                pos["last_adj_factor"] = float(current_adj)

        # 尾部移动止损：14:40代理信号触发后，当日尾盘卖出。
        tail_sold: list[dict[str, Any]] = []
        for code in list(positions):
            hp = positions[code]["high_price"]
            if hp <= 0:
                continue  # 旧仓无基准价，不触发
            bar = _bar(mkt, code, signal_date)
            if bar is None or bar["close"] is None:
                continue
            stop_price = max(
                float(positions[code]["avg_cost"]) * (1 - float(run["initial_stop_loss"])),
                hp * (1 - float(run["trailing_drawdown"])),
            )
            if bar["close"] <= stop_price:
                tbar = _bar(mkt, code, trade_date)
                if not _can_trade(tbar, "sell"):  # 停牌/封跌停：保留到下期再试
                    continue
                shares = float(positions[code]["shares"])
                sell_price = tbar["close"] * (1 - slip)
                proceeds = shares * sell_price
                fee = proceeds * (comm + stamp)
                cash += proceeds - fee
                tail_sold.append({"code": code, "shares": shares, "price": sell_price,
                                  "fee": round(fee, 2), "reason": "tail_stop"})
                del positions[code]

        if rebalance_due:
            buffer = set(ranked[:int(run["holding_buffer_rank"])])
            kept = [code for code in positions if code in buffer]
            target = kept + [code for code in ranked if code not in kept][
                :max(0, int(run["top_n"]) - len(kept))]
        else:
            target = list(positions)

        # 熊市择时（regime_filter=1 且信号日沪深300 close<MA20）：清仓转现金，不买入
        bear = rebalance_due and bool(run["regime_filter"]) and _bear_regime(mkt, signal_date)
        if bear:
            sold: list[dict[str, Any]] = []
            for code in list(positions):
                bar = _bar(mkt, code, trade_date)
                if not _can_trade(bar, "sell"):
                    continue
                shares = float(positions[code]["shares"])
                sell_price = bar["close"] * (1 - slip)
                proceeds = shares * sell_price
                fee = proceeds * (comm + stamp)
                cash += proceeds - fee
                sold.append({"code": code, "shares": shares, "price": sell_price, "fee": round(fee, 2)})
                del positions[code]
            market_value = 0.0
            nav = cash / float(run["initial_capital"])
            note = f"信号 {signal_date} 14:40 · 沪深300 MA20 熊市空仓 · 尾盘清仓 {len(sold)} 只"
            con.execute("DELETE FROM stock_sim_positions WHERE run_id=?", (run_id,))
            con.execute(
                "INSERT OR REPLACE INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, "
                "market_value, position_count, note) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, signal_date, trade_date, nav, cash, 0.0, 0, note))
            con.execute(
                "UPDATE stock_sim_runs SET last_signal_date=?, last_nav=?, last_rebalance_date=? WHERE run_id=?",
                (signal_date, nav, signal_date, run_id))
            con.commit()
            return {"ok": True, "runId": run_id, "signalDate": signal_date, "tradeDate": trade_date,
                    "bearRegime": True, "nav": round(nav, 6), "cash": round(cash, 2),
                    "marketValue": 0.0, "positions": [],
                    "trades": {"sold": sold, "bought": []}}

        # 1) 卖出：不在新目标里的持仓，当日尾盘成交（佣金 + 印花税 + 滑点）
        sold: list[dict[str, Any]] = []
        for code in list(positions):
            if code in target:
                continue
            bar = _bar(mkt, code, trade_date)
            if not _can_trade(bar, "sell"):  # 当日停牌/封跌停：保留到下期
                continue
            shares = float(positions[code]["shares"])
            sell_price = bar["close"] * (1 - slip)
            proceeds = shares * sell_price
            fee = proceeds * (comm + stamp)
            cash += proceeds - fee
            sold.append({"code": code, "shares": shares, "price": sell_price, "fee": round(fee, 2)})
            del positions[code]

        # 2) 买入：新目标里尚未持有的，按可用现金等权整手尾盘买入
        new_codes = [c for c in target if c not in positions]
        buy: list[dict[str, Any]] = []
        # 预算预留买入佣金，避免最后一只因费用导致整笔跳过。
        budget_each = (cash / len(new_codes) / (1 + comm)) if new_codes else 0.0
        for code in new_codes:
            bar = _bar(mkt, code, trade_date)
            if not _can_trade(bar, "buy"):  # 当日停牌/封涨停：本期放弃
                continue
            buy_price = bar["close"] * (1 + slip)
            shares = int((budget_each / buy_price) // LOT * LOT)
            if shares <= 0:
                continue
            cost = shares * buy_price
            fee = cost * comm
            if cash < cost + fee:
                continue
            cash -= cost + fee
            positions[code] = {"shares": shares, "avg_cost": buy_price, "last_price": bar["close"],
                               "high_price": bar["open"], "last_adj_factor": bar.get("adj_factor")}
            buy.append({"code": code, "shares": shares, "price": round(buy_price, 4),
                        "fee": round(fee, 2), "tradeDate": trade_date})

        # 3) 全部持仓按当日收盘估值，并滚动更新移动止损基准价 high_price
        market_value = 0.0
        for code, pos in positions.items():
            bar = _bar(mkt, code, trade_date)
            if bar:
                pos["last_price"] = bar["close"]
                if pos["high_price"] <= 0 or bar["close"] > pos["high_price"]:
                    pos["high_price"] = bar["close"]
            market_value += float(pos["shares"]) * float(pos["last_price"])

        # 4) 落账
        con.execute("DELETE FROM stock_sim_positions WHERE run_id=?", (run_id,))
        for code, pos in positions.items():
            con.execute(
                "INSERT INTO stock_sim_positions (run_id, code, shares, avg_cost, last_price, high_price, "
                "last_adj_factor, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, code, pos["shares"], pos["avg_cost"], pos["last_price"], pos["high_price"],
                 pos.get("last_adj_factor"), _now()))
        nav = (cash + market_value) / float(run["initial_capital"])
        note = f"信号 {signal_date} 14:40 · 当日尾盘调仓 · 卖 {len(sold) + len(tail_sold)} 买 {len(buy)} · 持 {len(positions)} 只"
        if tail_sold:
            note += f" · 移动止损 {len(tail_sold)} 只"
        con.execute(
            "INSERT OR REPLACE INTO stock_sim_daily (run_id, signal_date, trade_date, nav, cash, "
            "market_value, position_count, note) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, signal_date, trade_date, nav, cash, market_value, len(positions), note))
        con.execute(
            "UPDATE stock_sim_runs SET last_signal_date=?, last_nav=?, last_rebalance_date=? WHERE run_id=?",
            (signal_date, nav, signal_date if rebalance_due else last_rebalance, run_id))
        con.commit()
        return {
            "ok": True, "runId": run_id, "signalDate": signal_date, "tradeDate": trade_date,
            "executionMode": "same_day_close", "snapshotTime": "14:40", "snapshotSource": "close_proxy",
            "bearRegime": False,
            "nav": round(nav, 6), "cash": round(cash, 2), "marketValue": round(market_value, 2),
            "positions": [{"code": k, "shares": v["shares"], "avgCost": v["avg_cost"],
                           "lastPrice": v["last_price"]} for k, v in positions.items()],
            "trades": {"sold": tail_sold + sold, "bought": buy, "tailStopped": tail_sold},
        }
    finally:
        mkt.close()
        con.close()


def sim_status(factors_path: Path, run_id: str | None = None) -> dict[str, Any]:
    con = connect(factors_path)
    try:
        ensure_schema(con)
        if run_id is None:
            row = con.execute("SELECT * FROM stock_sim_runs ORDER BY created_at DESC LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT * FROM stock_sim_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"run": None}
        positions = [dict(r) for r in con.execute(
            "SELECT code, shares, avg_cost avgCost, last_price lastPrice, high_price highPrice, "
            "last_adj_factor lastAdjFactor FROM stock_sim_positions "
            "WHERE run_id=? ORDER BY code", (row["run_id"],))]
        last = con.execute(
            "SELECT signal_date signalDate, trade_date tradeDate, nav, cash, market_value marketValue, "
            "position_count positionCount, note FROM stock_sim_daily WHERE run_id=? "
            "ORDER BY rowid DESC LIMIT 1", (row["run_id"],)).fetchone()
        run = dict(row)
        # 配置可观测性：原始 config_json 列对使用者不友好，这里解析成对象并附指纹。
        run["config"] = load_config_json(row["config_json"])
        run["configSha256"] = row["config_sha256"]
        run["gitCommit"] = row["git_commit"]
        run["gitDirty"] = None if row["git_dirty"] is None else bool(row["git_dirty"])
        return {"run": run, "positions": positions, "latest": dict(last) if last else None,
                "totalReturn": round((float(row["last_nav"]) - 1.0), 6) if row["last_nav"] is not None else None}
    finally:
        con.close()


def sim_history(factors_path: Path, run_id: str | None = None, limit: int = 500) -> dict[str, Any]:
    con = connect(factors_path)
    try:
        ensure_schema(con)
        if run_id is None:
            row = con.execute("SELECT run_id FROM stock_sim_runs ORDER BY created_at DESC LIMIT 1").fetchone()
            run_id = row["run_id"] if row else None
        if not run_id:
            return {"runId": None, "items": []}
        rows = con.execute(
            "SELECT signal_date signalDate, trade_date tradeDate, nav, cash, market_value marketValue, "
            "position_count positionCount, note FROM stock_sim_daily WHERE run_id=? "
            "ORDER BY rowid DESC LIMIT ?", (run_id, limit)).fetchall()
        return {"runId": run_id, "items": [dict(r) for r in reversed(rows)]}
    finally:
        con.close()
