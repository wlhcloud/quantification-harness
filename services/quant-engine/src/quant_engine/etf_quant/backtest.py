"""Top-N equal-weight ETF portfolio backtest (M2, additive).

Signal: cross-sectional model score known at close of the rebalance date.
Trade: buy top-N at close of rebalance date, hold until next rebalance date,
sell at close. Turnover cost charged on weight changes (costBps, one side per
bundle). Benchmark: index_daily_bars (沪深300). Test-window only by default
so the backtest is out-of-sample relative to model fitting.
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from .factors import FACTOR_NAMES


def _annualize(ret: float, days: int) -> float:
    if days <= 0:
        return 0.0
    return (1 + ret) ** (252.0 / days) - 1 if ret > -1 else -1.0


def run_backtest(frame: list[dict[str, Any]], model, bars: pd.DataFrame, bench: pd.Series,
                 cfg: dict[str, Any], start_date: str | None = None, end_date: str | None = None,
                 progress=lambda p: None) -> dict[str, Any]:
    top_n = int(cfg.get("topN", 5))
    weight_mode = str(cfg.get("weightMode", "equal"))
    rebalance_days = int(cfg.get("rebalanceDays", 20))
    cost_bps = int(cfg.get("costBps", 3))
    score_mode = str(cfg.get("scoreMode", "model"))
    regime_cfg = cfg.get("regimeFilter") or {}
    regime_enabled = bool(regime_cfg.get("enabled", False))
    regime_ma = int(regime_cfg.get("maWindow", 20))
    bear_weight = float(regime_cfg.get("bearWeight", 0.5))

    # scores: model prediction (default) or a single factor column
    if score_mode.startswith("factor:"):
        factor = score_mode.split(":", 1)[1]
        scores = np.asarray([r.get(factor, float("nan")) for r in frame], dtype=float)
    else:
        feat_cols = list(model.feature_name()) if hasattr(model, "feature_name") else FACTOR_NAMES
        x = np.array([[r.get(f) for f in feat_cols] for r in frame], dtype=float)
        scores = np.asarray(model.predict(x), dtype=float)
    df = pd.DataFrame({"ts_code": [r["ts_code"] for r in frame],
                       "trade_date": [r["trade_date"] for r in frame], "score": scores})

    dates = sorted(df["trade_date"].unique())
    if start_date:
        dates = [d for d in dates if d >= start_date]
    if end_date:
        dates = [d for d in dates if d <= end_date]
    if len(dates) < rebalance_days:
        raise ValueError("回测窗口不足一个调仓周期")

    rebalance_dates = dates[::rebalance_days]
    if dates[-1] not in rebalance_dates:
        rebalance_dates.append(dates[-1])

    price = bars.pivot_table(index="trade_date", columns="ts_code", values="close", aggfunc="last")
    price = price.sort_index()
    price = price.reindex(index=sorted(set(dates) | set(rebalance_dates)))

    # benchmark aligned to backtest dates
    bench_aligned = bench.reindex(price.index)
    bench_aligned = bench_aligned.ffill()
    bench_returns = bench_aligned / bench_aligned.shift(1) - 1
    bench_cum = (1 + bench_returns.fillna(0.0)).cumprod()
    regime_bear: pd.Series | None = None
    if regime_enabled:
        if regime_cfg.get("signal") == "dualma":
            # fast MA below slow MA AND price below slow MA -> bear (additive option, default off)
            ma_fast = bench_aligned.rolling(int(regime_cfg.get("fastWindow", 10))).mean()
            ma_slow = bench_aligned.rolling(int(regime_cfg.get("slowWindow", 30))).mean()
            regime_bear = ((bench_aligned < ma_slow) & (ma_fast < ma_slow)).astype(bool)
        else:
            ma = bench_aligned.rolling(regime_ma).mean()
            regime_bear = (bench_aligned < ma).astype(bool)

    daily: list[dict[str, str | float]] = []
    trades: list[dict[str, Any]] = []
    holdings: list[str] = []
    hold_weights: dict[str, float] = {}
    nav = 1.0
    prev_day = None
    turnover_sum = 0.0
    trade_count = 0

    def _weights(scores: pd.Series) -> dict[str, float]:
        """Position weights inside the top-N basket: equal / score-linear / score-softmax."""
        if weight_mode == "equal":
            return {c: 1.0 / len(scores) for c in scores.index}
        s = scores.to_numpy(dtype=float)
        if weight_mode == "score_softmax":
            e = np.exp(s - s.max())
            w = e / e.sum()
        else:  # score_lin: gentle linear tilt, min weight = 0.1x base
            w = (s - s.min()) + 0.1
            w = w / w.sum()
        return dict(zip(scores.index, w))

    for i, day in enumerate(dates):
        day_row = price.loc[day]
        if prev_day is not None and prev_day in price.index:
            # daily P&L of current holdings (weighted by weightMode), scaled in bear regime
            day_returns = (day_row[holdings] / price.loc[prev_day, holdings] - 1).replace(
                [np.inf, -np.inf], np.nan).fillna(0.0)
            scale = 1.0
            if regime_bear is not None and bool(regime_bear.get(day, False)):
                scale = bear_weight
            if len(day_returns):
                wvec = np.asarray([hold_weights.get(h, 0.0) for h in holdings], dtype=float)
                if wvec.sum() > 0:
                    wvec = wvec / wvec.sum()
                    portfolio_ret = float(np.average(day_returns.to_numpy(dtype=float), weights=wvec)) * scale
                else:
                    portfolio_ret = float(day_returns.mean()) * scale
            else:
                portfolio_ret = 0.0
            nav *= (1 + portfolio_ret)
        if day in rebalance_dates:
            day_scores = df[df["trade_date"] == day].set_index("ts_code")["score"].dropna()
            if len(day_scores) >= top_n:
                new_holdings = day_scores.nlargest(top_n).index.tolist()
                # turnover cost on weight changes
                old_weights = {h: 1.0 / len(holdings) for h in holdings} if holdings else {}
                new_weights = _weights(day_scores.loc[new_holdings])
                all_codes = set(old_weights) | set(new_weights)
                turnover = sum(abs(new_weights.get(c, 0.0) - old_weights.get(c, 0.0)) for c in all_codes)
                cost = turnover * cost_bps / 10000.0
                nav *= (1 - cost)
                turnover_sum += turnover
                trade_count += 1
                for code in new_holdings:
                    if code in day_row and pd.notna(day_row[code]):
                        trades.append({"rebalanceDate": day, "tsCode": code, "weight": round(new_weights[code], 6),
                                       "entryPrice": float(day_row[code])})
                holdings = new_holdings
                hold_weights = new_weights
        bench_ret = float(bench_returns.get(day, 0.0)) if pd.notna(bench_returns.get(day, np.nan)) else 0.0
        daily.append({"tradeDate": day, "nav": round(nav, 6),
                      "benchmark": round(float(bench_cum.get(day, 1.0)), 6)})
        prev_day = day
        progress(10 + 80 * (i + 1) / max(1, len(dates)))

    # exit prices for trades
    for t in trades:
        t_date = t["rebalanceDate"]
        pos = rebalance_dates.index(t_date) if t_date in rebalance_dates else len(rebalance_dates) - 1
        exit_date = rebalance_dates[pos + 1] if pos + 1 < len(rebalance_dates) else None
        t["exitDate"] = exit_date
        if exit_date and exit_date in price.index and t["tsCode"] in price.columns:
            exit_price = price.loc[exit_date, t["tsCode"]]
            if pd.notna(exit_price) and t["entryPrice"]:
                t["exitPrice"] = float(exit_price)
                t["ret"] = round(exit_price / t["entryPrice"] - 1 - cost_bps / 10000.0, 6)
                t["cost"] = round(cost_bps / 10000.0, 6)
            else:
                t["exitPrice"] = None
                t["ret"] = None
                t["cost"] = None
        else:
            t["exitPrice"] = None
            t["ret"] = None
            t["cost"] = None

    navs = [d["nav"] for d in daily]
    total_return = navs[-1] / navs[0] - 1 if navs else 0.0
    days = len(daily)
    rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))] if len(navs) > 1 else []
    annual_ret = _annualize(total_return, days)
    annual_vol = float(np.std(rets, ddof=1) * math.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = annual_ret / annual_vol if annual_vol > 0 else 0.0
    peak = -math.inf
    max_dd = 0.0
    for v in navs:
        peak = max(peak, v)
        max_dd = min(max_dd, v / peak - 1)
    bench_total = float(bench_aligned.iloc[-1] / bench_aligned.iloc[0] - 1) if bench_aligned.notna().sum() > 1 else 0.0
    turnover_rate = turnover_sum * (252.0 / max(1, days))
    progress(100)

    return {"totalReturn": round(total_return, 6), "benchmarkReturn": round(bench_total, 6),
            "excessReturn": round(total_return - bench_total, 6),
            "annualReturn": round(annual_ret, 6), "annualVol": round(annual_vol, 6),
            "sharpe": round(sharpe, 4), "maxDrawdown": round(max_dd, 6),
            "turnoverRate": round(turnover_rate, 4), "trades": trade_count,
            "days": days, "startDate": daily[0]["tradeDate"] if daily else None,
            "endDate": daily[-1]["tradeDate"] if daily else None,
            "daily": daily, "tradesDetail": trades}
