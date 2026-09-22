# -*- coding: utf-8 -*-
"""stock_ml 公共层：因子/标签列定义、表结构、幂等迁移、配置体检。

原 stock_ml/__init__.py 是 1600 行单文件；这里拆出被 factors/backtest/门面共用的部分。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


FACTOR_COLUMNS = [
    "momentum20", "momentum60", "momentum120", "volatility20", "trend", "turnover20",
    "peTtm", "pb", "mktCap", "roe", "roa", "grossMargin", "revenueYoy", "ocfYoy",
    "netprofitYoy", "debtToAssets", "currentRatio", "drawdown60",
    "volumeRatio", "volSurge3", "volSurge5", "amountRatio5",
    "distHigh20", "breakout60", "volPriceCorr20", "turnoverRatio5", "amplitude20",
    "ma5Bias", "ma20Bias", "ma20Slope5", "rsi14", "macdDif", "macdDea", "macdHist", "bollingerPos20",
    "floatRatio", "psTtm", "dvTtm",
    "netMargin", "qSalesYoy", "assetsTurn", "roeYoy", "quickRatio",
]
# 行业轮动因子（独立表 stock_industry_factors，训练时 JOIN，零破坏原表）
INDUSTRY_FACTOR_COLUMNS = [
    "industry_mom5", "industry_mom20", "industry_rank5", "industry_rank20",
    "industry_excess5", "industry_excess20", "industry_limit_count", "industry_limit_ratio",
    "industry_amount_chg", "stock_vs_industry_mom20",
]
# 资金流向替代因子（独立表 stock_money_flow_factors，从量价数据计算，零破坏原表）
MONEY_FLOW_FACTOR_COLUMNS = [
    "money_flow_1d", "money_flow_5d", "money_flow_ratio_5d",
    "volume_price_div", "turnover_surge", "large_move_volume",
    "close_position", "close_position_5d", "up_volume_ratio",
]
LABEL_COLUMNS = ["forward_3", "forward_5", "forward_20"]
ML_MODEL_ID = "ml_walkforward"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_ml_factors (
  trade_date TEXT NOT NULL,
  code TEXT NOT NULL,
  close REAL,
  momentum20 REAL, momentum60 REAL, momentum120 REAL, volatility20 REAL,
  trend REAL, turnover20 REAL, peTtm REAL, pb REAL, mktCap REAL,
  roe REAL, roa REAL, grossMargin REAL, revenueYoy REAL, ocfYoy REAL,
  netprofitYoy REAL, debtToAssets REAL, currentRatio REAL, drawdown60 REAL,
  volumeRatio REAL, volSurge3 REAL, volSurge5 REAL, amountRatio5 REAL,
  distHigh20 REAL, breakout60 REAL, volPriceCorr20 REAL, turnoverRatio5 REAL,
  amplitude20 REAL, ma5Bias REAL, ma20Bias REAL, ma20Slope5 REAL,
  rsi14 REAL, macdDif REAL, macdDea REAL, macdHist REAL, bollingerPos20 REAL,
  floatRatio REAL, psTtm REAL, dvTtm REAL,
  netMargin REAL, qSalesYoy REAL, assetsTurn REAL, roeYoy REAL, quickRatio REAL,
  forward_3 REAL, forward_5 REAL, forward_20 REAL,
  PRIMARY KEY (trade_date, code)
);
CREATE TABLE IF NOT EXISTS stock_factor_registry (
  factor_name TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  source_table TEXT NOT NULL,
  value_column TEXT NOT NULL,
  default_direction INTEGER NOT NULL DEFAULT 1,
  enabled INTEGER NOT NULL DEFAULT 1,
  description TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stock_factor_ic_stats (
  run_id TEXT NOT NULL,
  factor_name TEXT NOT NULL,
  label TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  observations INTEGER NOT NULL,
  days INTEGER NOT NULL,
  coverage REAL NOT NULL,
  mean_rank_ic REAL,
  icir REAL,
  positive_rate REAL,
  abs_mean_rank_ic REAL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (run_id, factor_name, label)
);
CREATE INDEX IF NOT EXISTS idx_stock_factor_ic_stats_label ON stock_factor_ic_stats(label, computed_at);
CREATE TABLE IF NOT EXISTS stock_walkforward_runs (
  run_id TEXT PRIMARY KEY,
  generated_at TEXT NOT NULL,
  label TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  windows INTEGER NOT NULL,
  config TEXT NOT NULL,
  metrics TEXT NOT NULL,
  holdings TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'complete',
  -- 配置可观测性（2026-09-20）：config_sha256 是"实际生效配置"的规范化哈希，
  -- 相同哈希 == 完全相同的运行配置；config_yaml_sha256 是当次 YAML 文件哈希，
  -- 与前者不同即说明 YAML 被代码覆盖过。git_commit/git_dirty 记录服务启动时的
  -- 代码版本，用于回答"这次跑的是哪个提交"。
  config_sha256 TEXT,
  config_yaml_sha256 TEXT,
  git_commit TEXT,
  git_dirty INTEGER
);
-- selection_candidates 的建表语句原先在 quant_engine/factors/__init__.py 里（legacy 因子选股模块）。
-- legacy 退役并删除该模块后，这张表的唯一写入方就是本模块（walkforward 发布候选池），
-- 因此 DDL 随之搬到这里，避免新库上"表不存在"。
CREATE TABLE IF NOT EXISTS selection_candidates (
  trade_date TEXT NOT NULL,
  model_id TEXT NOT NULL,
  rank INTEGER NOT NULL,
  code TEXT NOT NULL,
  score REAL NOT NULL,
  reasons_json TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (trade_date, model_id, code)
);
CREATE INDEX IF NOT EXISTS idx_selection_rank ON selection_candidates(trade_date, model_id, rank);
"""
MIN_HISTORY = 120

# 代码实际读取的配置键（新增读取点时同步登记）。出现在 yaml 里但不在本集合 → _config_warnings 告警。
BACKTEST_CONFIG_KEYS = {
    "topN", "rebalanceDays", "holdingBufferRank", "maxPositionWeight", "initialCapital",
    "commissionRate", "stampDutyRate", "slippageRate",
    "executionMode", "snapshotTime", "snapshotSource",
    "excludeCodePrefix", "minMktCap", "requireMomentum20Positive", "minIndustryRank20",
    "regimeFilter", "maWindow", "bearWeight", "initialStopLoss", "trailingDrawdown",
    "technicalTiming",
}

# technicalTiming 段的键同样要登记：该段以前只校验顶层键名，段内写错键会静默失效
# （对象是 dict，_config_warnings 的顶层检查看不到里面）。
TECHNICAL_TIMING_CONFIG_KEYS = {
    "enabled", "candidateTopN", "goldenCross", "goldenCrossLookbackDays",
    "requireAboveMa20", "volumeBreakout", "volumeRatio", "highWindow",
    "deathCross", "deathCrossRequireBelowMa20",
    # 最小持有期：不足该交易日数的持仓忽略普通死叉退出（止损不受约束）。0 = 关闭。
    "minHoldingDays",
    # 卖出成交后的重新买入冷却期（交易日）；不延迟止损/死叉卖出。0 = 关闭。
    "reentryCooldownDays",
}

EXECUTION_MODE = "same_day_close"
SNAPSHOT_TIME = "14:40"


def _validate_execution_config(bt_cfg: dict[str, Any]) -> None:
    """股票策略只允许 14:40 信号、当日尾盘成交这一种生产口径。"""
    mode = str(bt_cfg.get("executionMode", EXECUTION_MODE))
    snapshot_time = str(bt_cfg.get("snapshotTime", SNAPSHOT_TIME))
    if mode != EXECUTION_MODE:
        raise ValueError(f"股票策略仅支持 executionMode={EXECUTION_MODE}，收到 {mode!r}")
    if snapshot_time != SNAPSHOT_TIME:
        raise ValueError(f"股票策略仅支持 snapshotTime={SNAPSHOT_TIME}，收到 {snapshot_time!r}")
# 股票池过滤（上市天数/ST/停牌/最小市值）实现在 build_factors 与回测函数内部，不由 yaml 的 universe 段驱动
UNIVERSE_CONFIG_KEYS: set[str] = set()

FACTOR_MIGRATIONS = {
    "volumeRatio": "REAL", "volSurge3": "REAL", "volSurge5": "REAL", "amountRatio5": "REAL",
    "distHigh20": "REAL", "breakout60": "REAL", "volPriceCorr20": "REAL", "turnoverRatio5": "REAL",
    "amplitude20": "REAL", "floatRatio": "REAL", "psTtm": "REAL", "dvTtm": "REAL",
    "ma5Bias": "REAL", "ma20Bias": "REAL", "ma20Slope5": "REAL", "rsi14": "REAL",
    "macdDif": "REAL", "macdDea": "REAL", "macdHist": "REAL", "bollingerPos20": "REAL",
    "netMargin": "REAL", "qSalesYoy": "REAL", "assetsTurn": "REAL", "roeYoy": "REAL", "quickRatio": "REAL",
    # 标签列也走幂等迁移：config/stock-ml.yaml 的 label 必须是本服务产出的列，
    # 原 forward_3 由脚本 compute_forward_3.py 在服务外维护（已移入 scripts/research_archive/），
    # 现已收回服务内（见 LABEL_COLUMNS）。
    "forward_3": "REAL", "forward_5": "REAL", "forward_20": "REAL",
}


def _rolling_corr(a: list[float | None], b: list[float | None], win: int = 20) -> list[float | None]:
    """滚动 Pearson 相关（滑动窗口 O(n)，供 volPriceCorr20 使用）。"""
    n = len(a)
    out: list[float | None] = [None] * n
    s = s2 = t = t2 = st = 0.0
    cnt = 0
    for i in range(n):
        ai, bi = a[i], b[i]
        if ai is None or bi is None or (cnt >= win and (a[i - win] is None or b[i - win] is None)):
            s = s2 = t = t2 = st = 0.0
            cnt = 0
            out[i] = None
            continue
        s += ai; s2 += ai * ai; t += bi; t2 += bi * bi; st += ai * bi; cnt += 1
        if i >= win:
            a0, b0 = a[i - win], b[i - win]
            if a0 is None or b0 is None:
                s = s2 = t = t2 = st = 0.0
                cnt = 0
                out[i] = None
                continue
            s -= a0; s2 -= a0 * a0; t -= b0; t2 -= b0 * b0; st -= a0 * b0; cnt -= 1
        if cnt >= 2:
            num = cnt * st - s * t
            den = ((cnt * s2 - s * s) * (cnt * t2 - t * t)) ** 0.5
            out[i] = (num / den) if den > 0 else 0.0
    return out


def _ensure_factors_columns(con: sqlite3.Connection) -> None:
    """幂等迁移：存量表补新增因子列（CREATE IF NOT EXISTS 不会加列）。"""
    have = {r[1] for r in con.execute("PRAGMA table_info(stock_ml_factors)")}
    for col, typ in FACTOR_MIGRATIONS.items():
        if col not in have:
            con.execute(f"ALTER TABLE stock_ml_factors ADD COLUMN {col} {typ}")
    con.commit()


def _forward_labels(rows: list[Any], i: int, close: float,
                    price_key: str = "close") -> dict[str, float | None]:
    """t 日 forward收益标签；生产因子构建传入复权价格列，单元测试可沿用close。

    标签列全部由本服务产出（与 LABEL_COLUMNS 同名），horizon 从列名解析，
    新增标签只需扩展 LABEL_COLUMNS 与表结构。
    """
    out: dict[str, float | None] = {col: None for col in LABEL_COLUMNS}
    if not close:
        return out
    for col in LABEL_COLUMNS:
        horizon = int(col.rsplit("_", 1)[1])
        if i + horizon < len(rows):
            future = rows[i + horizon][price_key]
            if future:
                out[col] = future / close - 1
    return out


def _config_warnings(cfg: dict[str, Any]) -> list[str]:
    """配置体检：报出 backtest/universe 段里代码不会读取的键（防止配置静默失效）。

    修复背景：config/stock-ml.yaml 曾写 transactionCostBps/slippageBps 与整个 universe 段，
    而代码读的是 commissionRate/stampDutyRate/slippageRate，且从不读 universe 段 ——
    配置看起来生效、实际被静默忽略。现在改为显式告警，并且 get_model_config 只回报有效值。
    """
    warnings: list[str] = []
    for section, known in (("backtest", BACKTEST_CONFIG_KEYS), ("universe", UNIVERSE_CONFIG_KEYS)):
        block = cfg.get(section) or {}
        if not isinstance(block, dict):
            warnings.append(f"{section} 段不是键值表，已按空处理")
            continue
        unknown = sorted(str(k) for k in block if k not in known)
        if unknown:
            warnings.append(f"{section} 段存在代码不读取的键（不会生效）：{', '.join(unknown)}")
    timing = (cfg.get("backtest") or {}) if isinstance(cfg.get("backtest"), dict) else {}
    timing = timing.get("technicalTiming")
    if isinstance(timing, dict):
        unknown_timing = sorted(str(k) for k in timing if k not in TECHNICAL_TIMING_CONFIG_KEYS)
        if unknown_timing:
            warnings.append(
                f"backtest.technicalTiming 段存在代码不读取的键（不会生效）：{', '.join(unknown_timing)}")
    return warnings


def _assert_label_supported(con: sqlite3.Connection, label: str) -> None:
    """标签契约校验：配置的 label 必须是本服务产出的列且在表中存在。

    原实现只把 label 拼进 SQL，若该列由服务外脚本维护（历史 forward_3）或不存在，
    就会静默使用陈旧标签训练，或抛出难以理解的 SQL 错误。
    """
    if label not in LABEL_COLUMNS:
        raise ValueError(f"配置的 label={label!r} 不是本服务产出的标签列；可选 {LABEL_COLUMNS}")
    label_cols = {r[1] for r in con.execute("PRAGMA table_info(stock_ml_factors)")}
    if label not in label_cols:
        raise ValueError(f"stock_ml_factors 缺少标签列 {label}；请先重建特征（stock_ml_factors job）")


def _ensure_walkforward_columns(con: sqlite3.Connection) -> None:
    """幂等迁移：stock_walkforward_runs表补is_published和notes字段。"""
    have = {r[1] for r in con.execute("PRAGMA table_info(stock_walkforward_runs)")}
    if "is_published" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN is_published INTEGER NOT NULL DEFAULT 0")
    if "notes" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN notes TEXT DEFAULT ''")
    if "result_version" not in have:
        # v1=旧T+1/旧NAV口径；v2=14:40 same_day_close、复权标签、PIT股票池。
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN result_version INTEGER NOT NULL DEFAULT 1")
    if "model_path" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN model_path TEXT")
    if "model_sha256" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN model_sha256 TEXT")
    # 配置可观测性（2026-09-20）：把"这次跑的是什么配置、哪个代码版本"钉进运行记录。
    # 在此之前只能靠人肉 diff config JSON 反推，且无法回指代码提交。
    # 旧记录这两列为 NULL —— 表示"该次运行早于指纹机制"，属未知，不是"没有配置"。
    if "config_sha256" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN config_sha256 TEXT")
    if "config_yaml_sha256" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN config_yaml_sha256 TEXT")
    if "git_commit" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN git_commit TEXT")
    if "git_dirty" not in have:
        con.execute("ALTER TABLE stock_walkforward_runs ADD COLUMN git_dirty INTEGER")
    con.commit()


def _ensure_selection_columns(con: sqlite3.Connection) -> None:
    """候选池记录来源run和结果口径；存量记录自动标记v1 legacy。"""
    have = {r[1] for r in con.execute("PRAGMA table_info(selection_candidates)")}
    if not have:
        return
    if "source_run_id" not in have:
        con.execute("ALTER TABLE selection_candidates ADD COLUMN source_run_id TEXT")
    if "result_version" not in have:
        con.execute("ALTER TABLE selection_candidates ADD COLUMN result_version INTEGER NOT NULL DEFAULT 1")
    con.commit()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def load_config(path: Path) -> dict[str, Any]:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
