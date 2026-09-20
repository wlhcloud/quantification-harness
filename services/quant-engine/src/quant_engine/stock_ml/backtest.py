# -*- coding: utf-8 -*-
"""stock_ml 回测层：行情/基准加载、ensemble 模型加载、14:40尾盘回测、指定模型回测与推理。

从 stock_ml/__init__.py 拆出，行为不变；对外仍通过 `from quant_engine import stock_ml` 使用。
"""
from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .common import (
    FACTOR_COLUMNS, INDUSTRY_FACTOR_COLUMNS, MONEY_FLOW_FACTOR_COLUMNS, SCHEMA,
    EXECUTION_MODE, SNAPSHOT_TIME, _ensure_factors_columns, _validate_execution_config, connect,
)


# ---------------------------------------------------------------- 回测（14:40 信号、当日尾盘成交口径）

def _load_bars(market_path: Path, start_date: str | None = None,
               end_date: str | None = None) -> dict[str, dict[str, dict[str, float]]]:
    """code -> 日线及PIT交易状态；涨跌停价来自当日状态快照。"""
    mkt = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    mkt.row_factory = sqlite3.Row
    out: dict[str, dict[str, dict[str, float]]] = {}
    try:
        tables = {r[0] for r in mkt.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        has_adjustments = "adjustment_factors" in tables
        date_filter = " WHERE 1=1"
        params: list[str] = []
        if start_date:
            date_filter += " AND d.trade_date>=?"
            params.append(start_date)
        if end_date:
            date_filter += " AND d.trade_date<=?"
            params.append(end_date)
        adj_select = ",a.adj_factor" if has_adjustments else ",NULL adj_factor"
        adj_join = (" LEFT JOIN adjustment_factors a ON a.code=d.code AND a.trade_date=d.trade_date "
                    if has_adjustments else " ")
        if "security_status_history" in tables:
            sql = ("SELECT d.code,d.trade_date,d.open,d.high,d.low,d.close,d.volume,d.pct_chg,"
                   "s.is_suspended,s.limit_up,s.limit_down" + adj_select + " FROM daily_bars d "
                   "LEFT JOIN security_status_history s ON s.code=d.code AND s.trade_date=d.trade_date "
                   + adj_join +
                   date_filter +
                   "ORDER BY d.code,d.trade_date")
        else:
            sql = ("SELECT d.code,d.trade_date,d.open,d.high,d.low,d.close,d.volume,d.pct_chg,NULL is_suspended,"
                   "NULL limit_up,NULL limit_down" +
                   (",a.adj_factor FROM daily_bars d LEFT JOIN adjustment_factors a ON a.code=d.code AND a.trade_date=d.trade_date "
                    if has_adjustments else ",NULL adj_factor FROM daily_bars d ") +
                   date_filter +
                   "ORDER BY d.code,d.trade_date")
        for row in mkt.execute(sql, params):
            out.setdefault(row["code"], {})[row["trade_date"]] = dict(row)
    finally:
        mkt.close()
    return out


def _lookback_start_date(market_path: Path, start_date: str, trading_days: int = 25) -> str:
    """Return an indexed market date with enough PIT history for timing indicators."""
    con = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<?) "
            "ORDER BY trade_date DESC LIMIT 1 OFFSET ?", (start_date, max(0, trading_days - 1))).fetchone()
        return str(row[0]) if row else start_date
    finally:
        con.close()


def _add_technical_timing_signals(
        bars: dict[str, dict[str, dict[str, float]]], volume_ratio: float = 2.0,
        high_window: int = 20) -> None:
    """Precompute PIT MA cross and volume-breakout signals in-place.

    All rolling inputs end on the signal day.  The breakout reference high excludes
    the signal day, preventing today's close from being compared with itself.
    """
    marker = f"_timing_{volume_ratio:g}_{high_window}"
    for series in bars.values():
        dates = sorted(series)
        closes: list[float] = []
        volumes: list[float] = []
        golden_cross_age: int | None = None
        for i, day in enumerate(dates):
            bar = series[day]
            close = bar.get("close")
            volume = bar.get("volume")
            closes.append(float(close) if close is not None else float("nan"))
            volumes.append(float(volume) if volume is not None else float("nan"))
            if bar.get(marker):
                continue
            ma5 = float(np.mean(closes[i - 4:i + 1])) if i >= 4 else None
            ma10 = float(np.mean(closes[i - 9:i + 1])) if i >= 9 else None
            ma20 = float(np.mean(closes[i - 19:i + 1])) if i >= 19 else None
            prev_ma5 = float(np.mean(closes[i - 5:i])) if i >= 5 else None
            prev_ma10 = float(np.mean(closes[i - 10:i])) if i >= 10 else None
            golden = bool(prev_ma5 is not None and prev_ma10 is not None and
                          ma5 is not None and ma10 is not None and
                          prev_ma5 <= prev_ma10 and ma5 > ma10)
            death = bool(prev_ma5 is not None and prev_ma10 is not None and
                         ma5 is not None and ma10 is not None and
                         prev_ma5 >= prev_ma10 and ma5 < ma10)
            prior_volumes = volumes[max(0, i - 5):i]
            vol_ma5 = (float(np.mean(prior_volumes)) if len(prior_volumes) == 5 else None)
            prior_closes = closes[max(0, i - high_window):i]
            breakout = bool(
                close is not None and ma20 is not None and vol_ma5 is not None and vol_ma5 > 0 and
                len(prior_closes) == high_window and float(volume or 0) > vol_ma5 * volume_ratio and
                float(close) >= max(prior_closes) and float(close) > ma20)
            if golden:
                golden_cross_age = 0
            bar.update({"ma5": ma5, "ma10": ma10, "ma20": ma20,
                        "golden_cross": golden, "death_cross": death,
                        "golden_cross_age": golden_cross_age,
                        "volume_breakout": breakout, marker: True})
            if golden_cross_age is not None:
                golden_cross_age += 1


def _load_benchmark(market_path: Path, bench: str = "000300.SH") -> dict[str, float]:
    mkt = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    mkt.row_factory = sqlite3.Row
    try:
        return {r["trade_date"]: float(r["close"]) for r in mkt.execute(
            "SELECT trade_date, close FROM index_daily_bars WHERE ts_code=? ORDER BY trade_date", (bench,))}
    finally:
        mkt.close()


def _exec_price(bars: dict[str, dict[str, dict[str, float]]], code: str, after_date: str) -> float | None:
    """信号日之后首个交易日开盘价（T+1 成交）。"""
    series = bars.get(code, {})
    for d in sorted(series):
        if d > after_date:
            return series[d]["open"]
    return None


def _close(bars: dict[str, dict[str, dict[str, float]]], code: str, date: str) -> float | None:
    s = bars.get(code, {}).get(date)
    return s["close"] if s else None


def _trading_day_gap(test_days: list[str], start: str, end: str) -> int:
    """两个交易日之间相隔多少个交易日；用于统计卖出被顺延了多久。"""
    try:
        return max(0, test_days.index(end) - test_days.index(start))
    except ValueError:
        return 0


def _is_stop_reason(reason: str) -> bool:
    """是否是止损类退出原因。

    止损原因有四个变体：initial_stop / initial_stop_gap / trailing_stop /
    trailing_stop_gap。不能简单用 `"stop" in reason` 之外的写法，也不能认为
    "initial_stop_gap" 一定含独立子串边界——统一在这里判定，避免各处口径漂移。
    """
    return reason.startswith("initial_stop") or reason.startswith("trailing_stop")


def _can_trade(bar: dict[str, Any] | None, side: str) -> bool:
    """尾盘保守成交约束：停牌、收盘封涨停不可买，收盘封跌停不可卖。"""
    if not bar or bar.get("close") is None or bar.get("is_suspended"):
        return False
    close = float(bar["close"])
    if side == "buy" and bar.get("limit_up") is not None:
        return close < float(bar["limit_up"]) - 0.005
    if side == "sell" and bar.get("limit_down") is not None:
        return close > float(bar["limit_down"]) + 0.005
    # 状态快照尚未同步时，以收盘封板形态保守兜底（最低5%覆盖ST限制）。
    pct = bar.get("pct_chg")
    if side == "buy" and pct is not None and float(pct) >= 4.8 and bar.get("high") == bar.get("close"):
        return False
    if side == "sell" and pct is not None and float(pct) <= -4.8 and bar.get("low") == bar.get("close"):
        return False
    return True


def _regime_filter_map(bench: dict[str, float], ma_win: int = 20) -> dict[str, bool]:
    """000300 收盘 < MA20 → 空头（False，空仓）；与 ETF 侧择时同口径。"""
    dates = sorted(bench)
    closes = [bench[d] for d in dates]
    m: dict[str, bool] = {}
    for i, d in enumerate(dates):
        lo = max(0, i - ma_win + 1)
        ma = sum(closes[lo:i + 1]) / (i + 1 - lo)
        m[d] = closes[i] >= ma
    return m


class EnsembleRanker:
    """多种子集成排序模型：包装 N 个不同 randomState 训练的 LightGBM Booster。

    支持两种集成方法：
    - mean_zscore: 各模型先横截面 z-score 标准化，再等权平均（默认）
    - median_zscore: 各模型先横截面 z-score 标准化，再取中位数（抗异常值，更稳健）
    对外暴露与 lgb.Booster 一致的 feature_name()/predict()，_run_window_backtest 无需改动。
    """

    def __init__(self, boosters: list[Any], seeds: list[int] | None = None, method: str = "mean_zscore"):
        if not boosters:
            raise ValueError("EnsembleRanker 至少需要一个模型")
        self.boosters = list(boosters)
        self.seeds = list(seeds) if seeds else list(range(len(boosters)))
        self.method = method if method in ("mean_zscore", "median_zscore") else "mean_zscore"

    def feature_name(self) -> list[str]:
        return list(self.boosters[0].feature_name())

    def predict(self, x: Any, *args: Any, **kwargs: Any) -> np.ndarray:
        """各模型先做横截面 z-score 标准化（消除分数尺度差异），再按 method 聚合。"""
        preds = []
        for b in self.boosters:
            p = np.asarray(b.predict(x, *args, **kwargs), dtype=float)
            mu, sd = float(np.mean(p)), float(np.std(p))
            preds.append((p - mu) / sd if sd > 1e-12 else np.zeros_like(p))
        stacked = np.stack(preds, axis=0)
        if self.method == "median_zscore":
            return np.median(stacked, axis=0)
        return np.mean(stacked, axis=0)

    def feature_importance(self, importance_type: str = "gain") -> np.ndarray:
        """特征重要性取 N 个模型的均值（gain 口径先归一化再平均）。"""
        imps = []
        for b in self.boosters:
            imp = np.asarray(b.feature_importance(importance_type=importance_type), dtype=float)
            total = imp.sum()
            imps.append(imp / total if total > 0 else imp)
        return np.mean(np.stack(imps, axis=0), axis=0)

    def save_models(self, dir_path: Path, base_name: str = "stock-wf-model") -> list[str]:
        """保存每个种子模型为独立文件，返回文件路径列表；主模型（第一个种子）额外存为 base_name.txt。"""
        dir_path = Path(dir_path)
        dir_path.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for i, (b, seed) in enumerate(zip(self.boosters, self.seeds)):
            p = dir_path / f"{base_name}_seed{seed}.txt"
            b.save_model(str(p))
            paths.append(str(p))
            if i == 0:
                main_path = dir_path / f"{base_name}.txt"
                b.save_model(str(main_path))
        return paths

    @property
    def n_models(self) -> int:
        return len(self.boosters)


def _load_ensemble_or_single(model_path: Path) -> Any:
    """加载模型：若同目录存在集成清单（*_ensemble_seeds.json）则加载为 EnsembleRanker，否则加载单模型。"""
    import lightgbm as lgb
    model_path = Path(model_path)
    manifest = model_path.parent / (model_path.stem + "_ensemble_seeds.json")
    if manifest.exists():
        try:
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            seed_files = meta.get("modelFiles") or []
            seeds = meta.get("seeds") or []
            method = meta.get("ensemble") or meta.get("method") or "mean_zscore"
            boosters = [lgb.Booster(model_file=f) for f in seed_files if Path(f).exists()]
            if len(boosters) >= 2:
                return EnsembleRanker(boosters, seeds[:len(boosters)], method=method)
        except Exception:
            pass  # 清单损坏则回退单模型
    return lgb.Booster(model_file=str(model_path))


def _run_window_backtest(frame: list[dict[str, Any]], model: Any, test_days: list[str],
                         bars: dict[str, dict[str, dict[str, float]]], bt_cfg: dict[str, Any],
                         progress: Callable[[float], None], start_progress: float, end_progress: float,
                         regime_map: dict[str, bool] | None = None,
                         _include_cost_free: bool = True) -> list[dict[str, Any]]:
    """单窗口回测：14:40 信号，以当日收盘价代理尾盘成交并在当日收盘估值。"""
    _validate_execution_config(bt_cfg)
    top_n = int(bt_cfg.get("topN", 5))
    rebalance_every = max(1, int(bt_cfg.get("rebalanceDays", 1)))
    commission = float(bt_cfg.get("commissionRate", 0.00008))
    stamp = float(bt_cfg.get("stampDutyRate", 0.0005))
    slip = float(bt_cfg.get("slippageRate", 0.004))
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in frame:
        by_date.setdefault(r["trade_date"], []).append(r)
    feat_cols = list(model.feature_name())
    cash = float(bt_cfg.get("initialCapital", 1_000_000))
    holdings: dict[str, float] = {}  # code -> shares
    entry_prices: dict[str, float] = {}
    entry_dates: dict[str, str] = {}
    last_prices: dict[str, float] = {}
    high_prices: dict[str, float] = {}
    holding_adj: dict[str, float] = {}
    # code -> 已持有交易日数（买入当日为 1，其后每过一个交易日 +1）。
    # 用于"最小持有期"退出约束，必须按交易日计而不是自然日。
    holding_age: dict[str, int] = {}
    # 已触发卖出信号、但因停牌/封跌停当日无法成交而顺延的仓位。
    # 原实现遇到 _can_trade=False 直接 `continue` 丢弃该次卖出，仓位既不在 target 里
    # 也不会再被卖出，只会在窗口末被动清零：账实不符，且换手/止损统计失真。
    # 现在改为挂账重试，直到真正卖掉为止（卖出信号一旦触发不撤销）。
    pending_sells: dict[str, dict[str, Any]] = {}
    deferred_sell_count = 0
    deferred_sell_days_total = 0
    max_deferred_sell_days = 0
    still_pending_at_window_end = 0
    daily: list[dict[str, Any]] = []
    # 股票池过滤（零破坏：只在回测选股时过滤，不影响因子表）
    exclude_prefixes = bt_cfg.get("excludeCodePrefix", ["920", "688"])  # 排除北交所、科创板
    min_mktcap = float(bt_cfg.get("minMktCap", 200_000))  # Tushare total_mv单位万元；20亿元=200000万元
    require_trend = bool(bt_cfg.get("requireMomentum20Positive", False))  # 只选近期上涨的股票（趋势跟踪，默认关闭）
    min_industry_rank = float(bt_cfg.get("minIndustryRank20", 0.0))  # 行业轮动约束：只选行业排名前N%的股票
    buffer_rank = max(top_n, int(bt_cfg.get("holdingBufferRank", top_n)))
    max_position_weight = min(1.0, max(0.0, float(bt_cfg.get("maxPositionWeight", 0.0))))
    initial_stop = max(0.0, float(bt_cfg.get("initialStopLoss", 0.0)))
    trailing_stop = max(0.0, float(bt_cfg.get("trailingDrawdown", 0.0)))
    timing_cfg = dict(bt_cfg.get("technicalTiming") or {})
    timing_enabled = bool(timing_cfg.get("enabled", False))
    candidate_top_n = max(top_n, int(timing_cfg.get("candidateTopN", 20)))
    golden_enabled = bool(timing_cfg.get("goldenCross", True))
    golden_lookback_days = max(1, int(timing_cfg.get("goldenCrossLookbackDays", 1)))
    breakout_enabled = bool(timing_cfg.get("volumeBreakout", True))
    require_above_ma20 = bool(timing_cfg.get("requireAboveMa20", True))
    death_enabled = bool(timing_cfg.get("deathCross", True))
    death_require_below_ma20 = bool(timing_cfg.get("deathCrossRequireBelowMa20", False))
    # 最小持有期（交易日）：新建仓不足该天数的持仓忽略"普通死叉"退出，但止损照常立即执行。
    # 目的只是消掉边缘反复交易，不阻止真正的趋势反转退出；0 = 关闭（与历史口径完全一致）。
    min_holding_days = max(0, int(timing_cfg.get("minHoldingDays", 0) or 0))
    if timing_enabled:
        _add_technical_timing_signals(
            bars, float(timing_cfg.get("volumeRatio", 2.0)),
            int(timing_cfg.get("highWindow", 20)))
    cumulative_cost = 0.0
    cumulative_commission = 0.0
    cumulative_stamp = 0.0
    cumulative_slippage = 0.0
    cumulative_stop_turnover = 0.0
    cumulative_rebalance_turnover = 0.0
    cumulative_turnover = 0.0
    trade_count = 0
    stop_trade_count = 0
    corporate_action_adjustments = 0
    for i, day in enumerate(test_days):
        # 持有天数按交易日递增：新的一天开始，账上每个仓位多持有一个交易日。
        for code in holdings:
            holding_age[code] = holding_age.get(code, 1) + 1
        # adjustment_factors 在除权除息日变化。按因子比例调整持仓股数，等价于
        # 复权总收益记账，避免原始 close 在除权日制造虚假亏损/止损。
        for code in list(holdings):
            bar = bars.get(code, {}).get(day) or {}
            current_adj = bar.get("adj_factor")
            previous_adj = holding_adj.get(code)
            if current_adj is not None and previous_adj not in (None, 0):
                ratio = float(current_adj) / float(previous_adj)
                if abs(ratio - 1.0) > 1e-12:
                    holdings[code] *= ratio
                    if code in entry_prices:
                        entry_prices[code] /= ratio
                    if code in high_prices:
                        high_prices[code] /= ratio
                    corporate_action_adjustments += 1
            if current_adj is not None:
                holding_adj[code] = float(current_adj)
        stopped: dict[str, tuple[float, str]] = {}
        for code in holdings:
            bar = bars.get(code, {}).get(day) or {}
            px = _close(bars, code, day)
            low = bar.get("low")
            open_px = bar.get("open")
            if px is None or low is None:
                continue
            thresholds: list[tuple[float, str]] = []
            if initial_stop > 0 and code in entry_prices:
                thresholds.append((entry_prices[code] * (1 - initial_stop), "initial_stop"))
            if trailing_stop > 0:
                # 今日止损线只使用截至上一交易日的最高价。日线无法判断
                # “先创新高还是先跌破”，若把当日 high 立即纳入会产生时序前视。
                thresholds.append((high_prices[code] * (1 - trailing_stop), "trailing_stop"))
            if thresholds:
                # 两条止损线同时存在时，更高的一条会先被触发；把实际生效的
                # 止损类型写进逐笔记录，避免硬止损和移动止损混为一谈。
                stop_px, stop_reason = max(thresholds, key=lambda item: item[0])
                if float(low) <= stop_px:
                    gap = open_px is not None and float(open_px) < stop_px
                    stopped[code] = (float(open_px) if gap else stop_px,
                                     f"{stop_reason}_gap" if gap else stop_reason)
        # 未触发止损的持仓，在本日结束后才更新移动止损参考高点。
        for code in holdings:
            if code in stopped:
                continue
            bar = bars.get(code, {}).get(day) or {}
            high = bar.get("high")
            if high is not None:
                high_prices[code] = max(high_prices.get(code, float(high)), float(high))
            close = bar.get("close")
            if close is not None:
                last_prices[code] = float(close)
        exit_reasons: dict[str, str] = {}
        if timing_enabled:
            for code in holdings:
                bar = bars.get(code, {}).get(day) or {}
                below_ma20 = (bar.get("ma20") is not None and bar.get("close") is not None and
                              float(bar["close"]) < float(bar["ma20"]))
                death_exit = (death_enabled and bool(bar.get("death_cross")) and
                              (below_ma20 or not death_require_below_ma20))
                if min_holding_days > 0 and death_exit:
                    # 持有天数不足则忽略普通死叉退出；止损在此之前已判定，不受本约束影响。
                    if int(holding_age.get(code, 0)) < min_holding_days:
                        death_exit = False
                if code not in stopped and death_exit:
                    exit_reasons[code] = "death_cross"
        buy_reasons: dict[str, str] = {}
        if timing_enabled:
            batch = by_date.get(day, [])
            target = [code for code in holdings if code not in stopped and code not in exit_reasons]
            if regime_map is not None and not regime_map.get(day, True):
                for code in target:
                    exit_reasons[code] = "regime_exit"
                target = []
            elif batch and len(target) < top_n:
                filtered = [r for r in batch
                            if not any(r["code"].startswith(p) for p in exclude_prefixes)
                            and (r.get("mktCap") is not None and r["mktCap"] >= min_mktcap)
                            and (not require_trend or (r.get("momentum20") is not None and r["momentum20"] > 0))
                            and (min_industry_rank <= 0 or (r.get("industry_rank20") is not None and r["industry_rank20"] >= min_industry_rank))]
                if filtered:
                    x = np.asarray([[r.get(f) for f in feat_cols] for r in filtered], dtype=float)
                    pred = np.asarray(model.predict(x), dtype=float)
                    ranked = [filtered[j]["code"] for j in np.argsort(-pred)][:candidate_top_n]
                    for code in ranked:
                        if code in target or code in stopped or code in exit_reasons or len(target) >= top_n:
                            continue
                        bar = bars.get(code, {}).get(day) or {}
                        above_ma20 = (bar.get("ma20") is not None and bar.get("close") is not None and
                                      float(bar["close"]) > float(bar["ma20"]))
                        cross_age = bar.get("golden_cross_age")
                        recent_golden = cross_age is not None and int(cross_age) < golden_lookback_days
                        golden = golden_enabled and recent_golden and (above_ma20 or not require_above_ma20)
                        breakout = breakout_enabled and bool(bar.get("volume_breakout"))
                        if not (golden or breakout):
                            continue
                        target.append(code)
                        buy_reasons[code] = ("golden_cross+volume_breakout" if golden and breakout
                                             else "golden_cross" if golden else "volume_breakout")
        elif (i % rebalance_every) == 0 or not holdings:
            batch = by_date.get(day, [])
            if batch:
                if regime_map is not None and not regime_map.get(day, True):
                    target: list[str] = []  # 空头：清仓持币
                else:
                    # 股票池过滤：排除北交所/科创板 + 最小市值 + 趋势过滤 + 行业轮动约束
                    filtered = [r for r in batch
                                if not any(r["code"].startswith(p) for p in exclude_prefixes)
                                and (r.get("mktCap") is not None and r["mktCap"] >= min_mktcap)
                                and (not require_trend or (r.get("momentum20") is not None and r["momentum20"] > 0))
                                and (min_industry_rank <= 0 or (r.get("industry_rank20") is not None and r["industry_rank20"] >= min_industry_rank))]
                    if not filtered:
                        target = []  # 风险过滤后为空就空仓，不允许静默回退到未过滤股票池
                    else:
                        x = np.asarray([[r.get(f) for f in feat_cols] for r in filtered], dtype=float)
                        pred = np.asarray(model.predict(x), dtype=float)
                        order = np.argsort(-pred)
                        ranked = [filtered[j]["code"] for j in order if filtered[j]["code"] not in stopped]
                        # 换手缓冲：老持仓仍在 buffer_rank 内就继续持有，只用高排名股票补空位。
                        # 这可避免第N名与第N+1名之间的噪声造成无意义双边交易。
                        buffer = set(ranked[:buffer_rank])
                        kept = [code for code in holdings if code in buffer]
                        target = kept + [code for code in ranked if code not in kept][:max(0, top_n - len(kept))]
            else:
                target = list(holdings)
        else:
            target = [code for code in holdings if code not in stopped]
        target = [code for code in target if code not in stopped and code not in exit_reasons]
        # 卖出信号一旦触发就不因当日卖不掉而撤销。三种触发路径（止损 / 死叉 / 择时空仓）
        # 在无法成交时统一登记为挂账卖出，并在后续交易日优先重试，直到真正卖掉。
        # 止损优先：同一根 K 线上止损价与收盘价的先后顺序无法判定，保守按止损处理。
        for code, (stop_px, stop_reason) in stopped.items():
            if code in holdings and code not in pending_sells:
                pending_sells[code] = {"reason": stop_reason, "price": stop_px, "since": day}
        for code, reason in exit_reasons.items():
            if code in holdings and code not in pending_sells:
                last_px = _close(bars, code, day)
                pending_sells[code] = {"reason": reason, "price": last_px, "since": day}
        # 挂账中的仓位一律排除在 target 之外：既不会被重新买回，也保证持仓只数始终 <= topN。
        # 注意：挂账只能在此处"新建"，绝不能在每日重复登记时覆盖 since，
        # 否则 since 会被刷新成当天，顺延天数永远算不出来（踩过一次）。
        target = [code for code in target if code not in pending_sells]
        # 14:40 信号、当日尾盘成交。历史日线没有 14:40 快照，close 只作为成交代理；
        # slippage 覆盖 14:40 到实际成交的漂移及冲击。
        sell_proceeds = 0.0
        day_turnover = 0.0
        day_cost = 0.0
        day_commission = 0.0
        day_stamp = 0.0
        day_slippage = 0.0
        day_trades: list[dict[str, Any]] = []
        pre_trade_equity = cash + sum(
            shares * (_close(bars, code, day) or last_prices.get(code, 0.0))
            for code, shares in holdings.items()
        )
        for code in list(holdings):
            if code in target:
                continue
            bar = bars.get(code, {}).get(day)
            close_px = _close(bars, code, day)
            stop_info = stopped.get(code)
            pending = pending_sells.get(code)
            deferred_days = 0
            if pending is not None:
                # 只要这个仓位带着更早的卖出信号，本次成交就是"顺延成交"，
                # 顺延天数按挂账首日到成交日计算（当日新触发止损也一样）。
                deferred_days = _trading_day_gap(test_days, pending["since"], day)
            if stop_info is not None:
                # 当日新触发止损：优先于更早的挂账信号，按止损价口径成交。
                exit_reason, px = stop_info[1], stop_info[0]
            elif pending is not None:
                # 更早触发但当时卖不掉的卖出信号，今天重试。
                # 成交价按"今天"重新计算，绝不能用触发日的陈旧价格记账：
                #   - 当日又触发止损（上面分支已覆盖）→ 按今日止损价
                #   - 否则是择时/死叉挂单 → 按今日收盘价卖出
                exit_reason, px = pending["reason"], close_px
            else:
                exit_reason = exit_reasons.get(code, "rebalance")
                px = close_px
            if px is None:
                continue
            if not _can_trade(bar, "sell"):
                if pending is None:
                    # 当日尝试但无法成交：登记挂账，下一交易日继续尝试。
                    pending_sells[code] = {"reason": exit_reason, "price": px, "since": day}
                    deferred_sell_count += 1
                continue
            # 判定是否止损类退出：用 startswith，不能用 `"stop" in reason`
            # （跳空变体是 "initial_stop_gap"，子串判断会造成口径漂移）。
            was_stopped = _is_stop_reason(exit_reason)
            shares = holdings.pop(code)
            entry_px = entry_prices.get(code)
            entry_day = entry_dates.get(code)
            entry_prices.pop(code, None)
            entry_dates.pop(code, None)
            last_prices.pop(code, None)
            high_prices.pop(code, None)
            holding_adj.pop(code, None)
            holding_age.pop(code, None)
            pending_sells.pop(code, None)
            if deferred_days > 0:
                deferred_sell_days_total += deferred_days
                max_deferred_sell_days = max(max_deferred_sell_days, deferred_days)
            sell_proceeds += shares * px * (1 - commission - stamp - slip)
            notional = shares * px
            day_turnover += notional
            commission_cost = notional * commission
            stamp_cost = notional * stamp
            slippage_cost = notional * slip
            day_commission += commission_cost
            day_stamp += stamp_cost
            day_slippage += slippage_cost
            day_cost += commission_cost + stamp_cost + slippage_cost
            if was_stopped:
                cumulative_stop_turnover += notional
            else:
                cumulative_rebalance_turnover += notional
            trade_count += 1
            if was_stopped:
                stop_trade_count += 1
            day_trades.append({"tradeDate": day, "side": "sell", "code": code,
                               "shares": shares, "price": px, "notional": notional,
                               "reason": exit_reason,
                               "entryDate": entry_day, "entryPrice": entry_px,
                               "grossPnl": (px - entry_px) * shares if entry_px is not None else None,
                               "commission": commission_cost, "stampDuty": stamp_cost,
                               "slippage": slippage_cost,
                               "deferredDays": deferred_days})
        cash += sell_proceeds
        buy_targets = [code for code in target if code not in holdings][:max(0, top_n - len(holdings))]
        # 未配置仓位上限时保留原有的等分可用现金行为。配置后按交易前组合净值
        # 限制每个新仓的初始名义金额；信号不足时剩余资金留在现金，不集中到单只股票。
        legacy_budget = cash / max(1, len(buy_targets))
        entry_budget = (pre_trade_equity * max_position_weight
                        if max_position_weight > 0 else legacy_budget)
        for code in buy_targets:
            if code in holdings:
                continue
            bar = bars.get(code, {}).get(day)
            px = _close(bars, code, day)
            if px is None or not _can_trade(bar, "buy"):
                continue
            # 为佣金和滑点预留现金，避免满额下单把现金余额打成负数。
            affordable = cash / (1 + commission + slip)
            shares = (min(entry_budget, affordable) / px) // 100 * 100
            if shares <= 0:
                continue
            holdings[code] = shares
            entry_prices[code] = px * (1 + slip)
            entry_dates[code] = day
            last_prices[code] = float(px)
            # 建仓发生在尾盘，不能用建仓前的当日最高价抬高移动止损基准。
            high_prices[code] = px
            if bar and bar.get("adj_factor") is not None:
                holding_adj[code] = float(bar["adj_factor"])
            # 买入当日计为持有第 1 个交易日。
            holding_age[code] = 1
            cash -= shares * px * (1 + commission + slip)
            notional = shares * px
            # 仓位口径自证：baselineEquity 是"窗口内交易前组合净值"，单仓上限即相对它计算。
            # 直接写进逐笔便于事后核验 maxPositionWeight 是否真的生效，无需再重放成交。
            position_weight = notional / pre_trade_equity if pre_trade_equity > 0 else 0.0
            day_turnover += notional
            commission_cost = notional * commission
            slippage_cost = notional * slip
            day_commission += commission_cost
            day_slippage += slippage_cost
            day_cost += commission_cost + slippage_cost
            cumulative_rebalance_turnover += notional
            trade_count += 1
            day_trades.append({"tradeDate": day, "side": "buy", "code": code,
                               "shares": shares, "price": px, "notional": notional,
                               "reason": buy_reasons.get(code, "rebalance"), "commission": commission_cost,
                               "stampDuty": 0.0, "slippage": slippage_cost,
                               "baselineEquity": round(pre_trade_equity, 2),
                               "positionWeight": round(position_weight, 6)})
        # 建仓后才发现的卖出信号必须补登记：止损评估发生在买入之前，若当日新买入的
        # 股票当天就触发止损/死叉，卖出循环不会碰它，若不补登记就会既没卖出、
        # 也没挂账重试（次日继续持有，等于静默忽略止损）。这里统一补挂账，
        # 次日按挂账逻辑优先卖出。
        for code in list(holdings):
            if code in pending_sells:
                continue
            if code in stopped:
                pending_sells[code] = {"reason": stopped[code][1], "price": stopped[code][0], "since": day}
            elif code in exit_reasons:
                pending_sells[code] = {"reason": exit_reasons[code],
                                       "price": _close(bars, code, day), "since": day}
        # 收盘估值
        market_value = 0.0
        for code, shares in holdings.items():
            c = _close(bars, code, day)
            if c is not None:
                last_prices[code] = float(c)
            # 停牌或单日行情缺失时，按最近一个可用收盘价估值，
            # 不得把仍在账上的持仓市值瞬间归零。
            market_value += shares * last_prices.get(code, 0.0)
        nav = (cash + market_value) / float(bt_cfg.get("initialCapital", 1_000_000))
        cumulative_cost += day_cost
        cumulative_commission += day_commission
        cumulative_stamp += day_stamp
        cumulative_slippage += day_slippage
        cumulative_turnover += day_turnover / pre_trade_equity if pre_trade_equity > 0 else 0.0
        daily.append({"tradeDate": day, "nav": nav, "signalTime": SNAPSHOT_TIME,
                      "executionMode": EXECUTION_MODE, "cost": day_cost,
                      "turnover": day_turnover / pre_trade_equity if pre_trade_equity > 0 else 0.0,
                      # 资金利用率口径：现金、持仓市值、持仓只数、交易前净值。
                      "cash": cash, "marketValue": market_value,
                      "preTradeEquity": pre_trade_equity, "holdings": len(holdings),
                      "cumulativeCost": cumulative_cost, "cumulativeTurnover": cumulative_turnover,
                      "cumulativeCommission": cumulative_commission,
                      "cumulativeStampDuty": cumulative_stamp,
                      "cumulativeSlippage": cumulative_slippage,
                      "cumulativeStopTurnover": cumulative_stop_turnover,
                      "cumulativeRebalanceTurnover": cumulative_rebalance_turnover,
                      "trades": day_trades,
                      "tradeCount": trade_count, "stopTradeCount": stop_trade_count,
                      "pendingSellCount": len(pending_sells),
                      "deferredSellCount": deferred_sell_count,
                      "deferredSellDays": deferred_sell_days_total,
                      "maxDeferredSellDays": max_deferred_sell_days,
                      "corporateActionAdjustments": corporate_action_adjustments})
        progress(start_progress + (end_progress - start_progress) * (i + 1) / max(1, len(test_days)))
    if _include_cost_free and daily and any((commission, stamp, slip)):
        no_cost_cfg = dict(bt_cfg)
        no_cost_cfg.update({"commissionRate": 0.0, "stampDutyRate": 0.0, "slippageRate": 0.0})
        no_cost_daily = _run_window_backtest(
            frame, model, test_days, bars, no_cost_cfg, lambda _: None,
            start_progress, end_progress, regime_map, _include_cost_free=False)
        for net_row, gross_row in zip(daily, no_cost_daily):
            net_row["noCostNav"] = gross_row["nav"]
    else:
        for row in daily:
            row["noCostNav"] = row["nav"]
    # 窗口结束时仍挂着未成交卖出信号的仓位：这些仓位是被动清理的，
    # 需要在结果里显式报出，避免"卖不掉"被静默吞掉。
    still_pending_at_window_end = len(pending_sells)
    for row in daily:
        row["pendingSellAtWindowEnd"] = still_pending_at_window_end
    return daily


# ---------------------------------------------------------------- 指定模型回测

def find_model_path_by_run_id(run_id: str, artifact_dir: Path, factors_path: Path) -> str | None:
    """通过 run_id 查找对应的模型文件路径。优先级：runs目录 > 备份目录 > 当前目录"""
    artifact_dir = Path(artifact_dir)
    # 1. runs 目录（按run_id保存的模型）
    run_dir = artifact_dir / "runs" / run_id
    if (run_dir / "stock-wf-model.txt").exists():
        return str(run_dir / "stock-wf-model.txt")
    from . import latest_walkforward  # 延迟导入：避免与门面 __init__ 循环依赖

    # 2. 当前目录（最新模型），如果run_id是最新的
    latest = latest_walkforward(factors_path)
    if latest and latest.get("run", {}).get("runId") == run_id:
        if (artifact_dir / "stock-wf-model.txt").exists():
            return str(artifact_dir / "stock-wf-model.txt")
    # 3. 备份目录（按时间匹配）
    backups_dir = artifact_dir.parent.parent / "backups"
    if backups_dir.exists():
        for d in sorted(backups_dir.iterdir(), reverse=True):
            if d.is_dir() and (d / "models" / "stock-wf-model.txt").exists():
                # 检查备份的训练结果是否匹配run_id
                result_file = d / "models" / "ensemble_train_result.json"
                if result_file.exists():
                    try:
                        result = json.loads(result_file.read_text(encoding="utf-8"))
                        if result.get("runId") == run_id:
                            return str(d / "models" / "stock-wf-model.txt")
                    except Exception:
                        pass
    return None


def backtest_with_model(factors_path: Path, market_path: Path, model_path: Path,
                         cfg: dict[str, Any], start_date: str | None = None,
                         end_date: str | None = None,
                         progress: Callable[[float], None] = lambda p: None) -> dict[str, Any]:
    """用指定模型对指定时间段进行回测。返回 {metrics, daily, holdings}"""
    import numpy as np

    def _norm_date(d: str | None) -> str | None:
        """统一日期格式为YYYYMMDD（因子表使用的格式）"""
        if d is None:
            return None
        return d.replace("-", "").replace("/", "")

    start_date = _norm_date(start_date)
    end_date = _norm_date(end_date)

    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"模型文件不存在: {model_path}")

    # 加载模型
    model = _load_ensemble_or_single(model_path)

    # 解析配置
    label = str(cfg.get("label", "forward_3"))
    bt_cfg = dict(cfg.get("backtest") or {})
    wf_cfg = dict(cfg.get("walkforward") or {})
    bt_cfg.update(wf_cfg.get("backtest") or {})
    if "rebalanceDays" in wf_cfg:
        bt_cfg["rebalanceDays"] = int(wf_cfg["rebalanceDays"])
    if "topN" in cfg:
        bt_cfg["topN"] = int(cfg["topN"])

    # 加载因子数据
    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        has_industry = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_industry_factors'"
        ).fetchone() is not None
        has_money_flow = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_money_flow_factors'"
        ).fetchone() is not None
        base_cols = ",".join([f"a.{c}" for c in FACTOR_COLUMNS])
        extra_cols = []
        joins = []
        if has_industry:
            extra_cols.extend([f"b.{c}" for c in INDUSTRY_FACTOR_COLUMNS])
            joins.append("LEFT JOIN stock_industry_factors b ON a.trade_date=b.trade_date AND a.code=b.code")
        if has_money_flow:
            mf_alias = "c" if has_industry else "b"
            extra_cols.extend([f"{mf_alias}.{c}" for c in MONEY_FLOW_FACTOR_COLUMNS])
            joins.append(f"LEFT JOIN stock_money_flow_factors {mf_alias} ON a.trade_date={mf_alias}.trade_date AND a.code={mf_alias}.code")
        where = "WHERE a.momentum20 IS NOT NULL"
        params: list[Any] = []
        if start_date:
            where += " AND a.trade_date >= ?"
            params.append(start_date)
        if end_date:
            where += " AND a.trade_date <= ?"
            params.append(end_date)
        if extra_cols:
            sql = (
                "SELECT a.trade_date, a.code, " + base_cols + "," +
                ",".join(extra_cols) +
                " FROM stock_ml_factors a " + " ".join(joins) +
                " " + where + " ORDER BY a.trade_date, a.code"
            )
        else:
            sql = (
                "SELECT trade_date, code, " + ",".join(FACTOR_COLUMNS) +
                " FROM stock_ml_factors " + where + " ORDER BY trade_date, code"
            )
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    frame = [dict(r) for r in rows]
    if not frame:
        raise ValueError("无因子数据，请检查日期范围")

    dates = sorted({r["trade_date"] for r in frame})
    bars = _load_bars(market_path)
    bench = _load_benchmark(market_path)
    regime_map = _regime_filter_map(bench) if bt_cfg.get("regimeFilter", False) else None

    progress(20)

    # 运行回测
    daily = _run_window_backtest(
        frame, model, dates, bars, bt_cfg,
        lambda p: progress(20 + p * 70), 20, 90, regime_map
    )

    progress(92)

    # 计算指标
    navs = np.asarray([d["nav"] for d in daily], dtype=float)
    if len(navs) < 2:
        raise ValueError("回测数据不足")

    span_start, span_end = daily[0]["tradeDate"], daily[-1]["tradeDate"]
    bench_slice = [bench[d] for d in sorted(bench) if span_start <= d <= span_end]
    bench_total = (bench_slice[-1] / bench_slice[0] - 1) if len(bench_slice) > 1 else 0.0
    total = float(navs[-1] - 1)
    no_cost_total = float(daily[-1].get("noCostNav", daily[-1]["nav"]) - 1)
    rets = np.diff(np.concatenate(([1.0], navs))) / np.concatenate(([1.0], navs))[:-1]
    annual = (1 + total) ** (252.0 / len(navs)) - 1
    vol = float(np.std(rets, ddof=1) * math.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = annual / vol if vol > 0 else 0.0
    peak, mdd = -np.inf, 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)

    metrics = {
        "totalReturn": round(float(total), 4),
        "benchmarkReturn": round(float(bench_total), 4),
        "excessReturn": round(float(total - bench_total), 4),
        "annualReturn": round(float(annual), 4),
        "sharpe": round(float(sharpe), 3),
        "maxDrawdown": round(float(mdd), 4),
        "noCostTotalReturn": round(no_cost_total, 4),
        "costReturnDrag": round(no_cost_total - total, 4),
        "startDate": span_start,
        "endDate": span_end,
        "tradingDays": len(daily),
        "modelFile": str(model_path),
        "transactionCost": round(float(daily[-1].get("cumulativeCost", 0.0)), 2),
        "transactionCostRate": round(float(daily[-1].get("cumulativeCost", 0.0)) / float(bt_cfg.get("initialCapital", 1_000_000)), 4),
        "annualTransactionCostRate": round(
            float(daily[-1].get("cumulativeCost", 0.0)) /
            float(bt_cfg.get("initialCapital", 1_000_000)) * 252.0 / len(daily), 4),
        "commissionCost": round(float(daily[-1].get("cumulativeCommission", 0.0)), 2),
        "stampDutyCost": round(float(daily[-1].get("cumulativeStampDuty", 0.0)), 2),
        "slippageCost": round(float(daily[-1].get("cumulativeSlippage", 0.0)), 2),
        "stopTurnover": round(float(daily[-1].get("cumulativeStopTurnover", 0.0)), 2),
        "rebalanceTurnover": round(float(daily[-1].get("cumulativeRebalanceTurnover", 0.0)), 2),
        "annualTurnover": round(float(daily[-1].get("cumulativeTurnover", 0.0)) * 252.0 / len(daily), 4),
        "tradeCount": int(daily[-1].get("tradeCount", 0)),
        "stopTradeCount": int(daily[-1].get("stopTradeCount", 0)),
    }

    # 最新持仓
    holdings = []
    if daily:
        last_day = daily[-1]["tradeDate"]
        by_date: dict[str, list[dict[str, Any]]] = {}
        for r in frame:
            by_date.setdefault(r["trade_date"], []).append(r)
        batch = by_date.get(last_day, [])
        if batch:
            feat_cols = list(model.feature_name())
            filtered = [r for r in batch
                        if not any(r["code"].startswith(p) for p in bt_cfg.get("excludeCodePrefix", ["920", "688"]))
                        and (r.get("mktCap") is not None and r["mktCap"] >= float(bt_cfg.get("minMktCap", 200_000)))]
            if filtered:
                x = np.asarray([[r.get(f) for f in feat_cols] for r in filtered], dtype=float)
                pred = np.asarray(model.predict(x), dtype=float)
                order = np.argsort(-pred)
                top_n = int(bt_cfg.get("topN", 5))
                for j in order[:top_n]:
                    holdings.append({
                        "code": filtered[j]["code"],
                        "score": round(float(pred[j]), 4),
                        "rank": j + 1,
                    })

    progress(100)

    return {
        "metrics": metrics,
        "daily": daily,
        "holdings": holdings,
    }


def predict_with_model(factors_path: Path, model_path: Path, cfg: dict[str, Any],
                       top_n: int = 10, trade_date: str | None = None) -> dict[str, Any]:
    """用指定模型对最新（或指定）交易日进行选股预测。返回 {tradeDate, modelFile, count, items}"""
    import numpy as np

    def _norm_date(d: str | None) -> str | None:
        if d is None:
            return None
        return d.replace("-", "").replace("/", "")

    trade_date = _norm_date(trade_date)
    model_path = Path(model_path)
    if not model_path.exists():
        raise FileNotFoundError(f"模型文件不存在: {model_path}")

    # 加载模型
    model = _load_ensemble_or_single(model_path)
    feat_cols = list(model.feature_name())

    # 解析配置
    bt_cfg = dict(cfg.get("backtest") or {})
    wf_cfg = dict(cfg.get("walkforward") or {})
    bt_cfg.update(wf_cfg.get("backtest") or {})
    if "topN" in cfg:
        bt_cfg["topN"] = int(cfg["topN"])
    exclude_prefixes = bt_cfg.get("excludeCodePrefix", ["920", "688"])
    min_mktcap = float(bt_cfg.get("minMktCap", 200_000))
    require_trend = bool(bt_cfg.get("requireMomentum20Positive", False))
    min_industry_rank = float(bt_cfg.get("minIndustryRank20", 0.0))

    # 加载因子数据（最新一天或指定日期）
    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        has_industry = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_industry_factors'"
        ).fetchone() is not None
        has_money_flow = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_money_flow_factors'"
        ).fetchone() is not None
        base_cols = ",".join([f"a.{c}" for c in FACTOR_COLUMNS])
        extra_cols = []
        joins = []
        if has_industry:
            extra_cols.extend([f"b.{c}" for c in INDUSTRY_FACTOR_COLUMNS])
            joins.append("LEFT JOIN stock_industry_factors b ON a.trade_date=b.trade_date AND a.code=b.code")
        if has_money_flow:
            mf_alias = "c" if has_industry else "b"
            extra_cols.extend([f"{mf_alias}.{c}" for c in MONEY_FLOW_FACTOR_COLUMNS])
            joins.append(f"LEFT JOIN stock_money_flow_factors {mf_alias} ON a.trade_date={mf_alias}.trade_date AND a.code={mf_alias}.code")

        # 找最新交易日
        if not trade_date:
            date_row = con.execute("SELECT MAX(trade_date) d FROM stock_ml_factors WHERE momentum20 IS NOT NULL").fetchone()
            if not date_row or not date_row["d"]:
                return {"tradeDate": None, "modelFile": str(model_path), "count": 0, "items": []}
            trade_date = date_row["d"]

        where = "WHERE a.momentum20 IS NOT NULL AND a.trade_date = ?"
        params: list[Any] = [trade_date]
        if extra_cols:
            sql = (
                "SELECT a.trade_date, a.code, " + base_cols + "," +
                ",".join(extra_cols) +
                " FROM stock_ml_factors a " + " ".join(joins) +
                " " + where + " ORDER BY a.code"
            )
        else:
            sql = (
                "SELECT trade_date, code, " + ",".join(FACTOR_COLUMNS) +
                " FROM stock_ml_factors " + where + " ORDER BY code"
            )
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    frame = [dict(r) for r in rows]
    if not frame:
        return {"tradeDate": trade_date, "modelFile": str(model_path), "count": 0, "items": []}

    # 股票池过滤
    filtered = [r for r in frame
                if not any(r["code"].startswith(p) for p in exclude_prefixes)
                and (r.get("mktCap") is not None and r["mktCap"] >= min_mktcap)
                and (not require_trend or (r.get("momentum20") is not None and r["momentum20"] > 0))
                and (min_industry_rank <= 0 or (r.get("industry_rank20") is not None and r["industry_rank20"] >= min_industry_rank))]
    if not filtered:
        return {"tradeDate": trade_date, "modelFile": str(model_path), "featureCount": len(feat_cols),
                "candidateCount": 0, "count": 0, "items": [],
                "warning": "风险过滤后无合格股票，保持空仓"}

    # 模型预测
    x = np.asarray([[r.get(f) for f in feat_cols] for r in filtered], dtype=float)
    pred = np.asarray(model.predict(x), dtype=float)
    order = np.argsort(-pred)

    items = []
    for rank, idx in enumerate(order[:top_n], start=1):
        r = filtered[idx]
        items.append({
            "rank": rank,
            "code": r["code"],
            "score": float(pred[idx]),
            "momentum20": r.get("momentum20"),
            "industryRank20": r.get("industry_rank20"),
            "mktCap": r.get("mktCap"),
        })

    return {
        "tradeDate": trade_date,
        "modelFile": str(model_path),
        "featureCount": len(feat_cols),
        "candidateCount": len(filtered),
        "count": len(items),
        "items": items,
    }
