"""股票 ML walk-forward 单 run 详细分析器。

用法:
  python scripts/analyze_stock_wf_run.py --run-dir artifacts/stock-ml/runs/<run_id> [--label Top20-基线]

输出: 收益/风险/换手/交易归因/卖出原因/年度稳定性/滚动窗口质量/资金利用率,
以及仓位上限与可用资金约束的自洽性核验 (maxPositionWeight 生效性反证)。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("QUANT_ROOT") or Path(__file__).resolve().parents[1])
FACTORS_DB = ROOT / "data" / "factors.db"
RUNS_DIR = ROOT / "artifacts" / "stock-ml" / "runs"


def _load_run_metrics(run_id: str) -> tuple[dict, dict]:
    con = sqlite3.connect(str(FACTORS_DB))
    con.row_factory = sqlite3.Row
    try:
        row = con.execute(
            "SELECT config, metrics FROM stock_walkforward_runs WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        con.close()
    if row is None:
        return {}, {}
    return json.loads(row["config"]), json.loads(row["metrics"])


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def _sharpe(daily_rets: list[float]) -> float:
    if len(daily_rets) < 2:
        return 0.0
    vol = _std(daily_rets) * math.sqrt(252)
    if vol <= 0:
        return 0.0
    nav = 1.0
    for r in daily_rets:
        nav *= (1 + r)
    annual = nav ** (252.0 / len(daily_rets)) - 1
    return annual / vol


def _max_drawdown(navs: list[float]) -> float:
    peak, mdd = -math.inf, 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


class RunAnalysis:
    """口径说明：每个 walk-forward 窗口都以 initialCapital 独立起步，窗口之间只用
    收益率相乘衔接。因此逐笔仓位的分母必须是"本窗口起始净值"，不能用全期累计净值。
    """

    def __init__(self, run_dir: Path, initial_capital: float = 1_000_000.0,
                 max_position_weight: float = 0.0):
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self.initial_capital = initial_capital
        self.max_position_weight = max_position_weight
        self.cfg, self.metrics = _load_run_metrics(self.run_id)
        nav_doc = json.loads((self.run_dir / "stock-wf-daily-nav.json").read_text(encoding="utf-8"))
        self.daily = nav_doc["daily"]
        tmp = [json.loads(l) for l in
               (self.run_dir / "stock-wf-trades.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
        self.trades = [t for t in tmp if "side" in t]      # 逐笔
        self.days = [d for d in tmp if "side" not in d]    # 每日汇总行（旧格式才有）
        ws = json.loads((self.run_dir / "stock-wf-window-stats.json").read_text(encoding="utf-8"))
        self.windows = ws.get("windows", [])
        self.window_start_nav: dict[str, float] = {}
        nav_by_date = {d["tradeDate"]: float(d["nav"]) for d in self.daily}
        for w in self.windows:
            start = w["testStart"]
            # 窗口起始净值 = 上一窗口终值（首个窗口为初始净值）
            self.window_start_nav[start] = nav_by_date.get(start, 1.0)
        self._window_of_day = {}
        for w in self.windows:
            for d in self.daily:
                if w["testStart"] <= d["tradeDate"] <= w["testEnd"]:
                    self._window_of_day[d["tradeDate"]] = w["testStart"]

    def position_replay(self) -> list[dict]:
        """按窗口重放成交：输出每日 pre-trade 净值、持仓数、现金比与买入占净值比。

        每窗口以 initialCapital 独立起步；卖出先于买入；窗口之间只做收益率衔接，
        不把上一窗口的现金带入下一窗口。持仓市值在每个成交价上重估，收盘后再按
        当日真实净值校正现金口径。
        """
        cash = self.initial_capital
        positions: dict[str, float] = {}          # code -> shares
        prices: dict[str, float] = {}             # code -> 最近成交/估值价
        by_day: dict[str, list[dict]] = defaultdict(list)
        for t in self.trades:
            by_day[t["tradeDate"]].append(t)
        out: list[dict] = []
        current_window = None
        for day in self.daily:
            d = day["tradeDate"]
            wstart = self._window_of_day.get(d)
            if wstart != current_window:
                current_window = wstart
                cash = self.initial_capital
                positions, prices = {}, {}
            rows = by_day.get(d, [])
            buys = [t for t in rows if t["side"] == "buy"]
            sells = [t for t in rows if t["side"] == "sell"]
            pre_equity = cash + sum(sh * prices.get(c, 0.0) for c, sh in positions.items())
            for t in sells:
                positions.pop(t["code"], None)
                prices.pop(t["code"], None)
                cash += float(t["notional"]) - float(t.get("commission", 0.0)) \
                        - float(t.get("stampDuty", 0.0)) - float(t.get("slippage", 0.0))
            buy_weights = []
            for t in buys:
                positions[t["code"]] = positions.get(t["code"], 0.0) + float(t["shares"])
                prices[t["code"]] = float(t["price"])
                cash -= float(t["notional"]) + float(t.get("commission", 0.0)) + float(t.get("slippage", 0.0))
                if pre_equity > 0:
                    buy_weights.append(float(t["notional"]) / pre_equity)
            day_nav = float(day["nav"])
            end_equity = self.initial_capital * day_nav
            out.append({
                "tradeDate": d,
                "windowStart": wstart,
                "preTradeEquity": pre_equity,
                "cashAfter": cash,
                "holdings": len(positions),
                "buyCount": len(buys),
                "sellCount": len(sells),
                "buyWeights": buy_weights,
                "cashRatio": cash / pre_equity if pre_equity > 0 else 0.0,
                "dayNav": day_nav,
                "endEquity": end_equity,
            })
            # 用当日真实净值校正现金口径（持仓按成交价计价，其余涨跌体现在现金缩放上）。
            if pre_equity > 0:
                scale = end_equity / pre_equity
                cash *= scale
                for c in list(prices):
                    prices[c] *= scale
        return out

    # ---------------------------------------------------------------- 指标
    def summary(self) -> dict:
        navs = [float(d["nav"]) for d in self.daily]
        rets = [navs[i] / navs[i - 1] - 1 for i in range(1, len(navs))]
        tl = self.position_replay()
        trades = self.trades
        round_trips = [t for t in trades if t["side"] == "sell" and t.get("grossPnl") is not None]
        wins = [t for t in round_trips if t["grossPnl"] > 0]
        losses = [t for t in round_trips if t["grossPnl"] <= 0]
        gross_win = sum(t["grossPnl"] for t in wins)
        gross_loss = -sum(t["grossPnl"] for t in losses)
        hold_days = []
        for t in round_trips:
            if t.get("entryDate"):
                hold_days.append((_ordinal(t["tradeDate"]) - _ordinal(t["entryDate"])))
        by_reason: dict[str, dict] = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0})
        for t in round_trips:
            r = by_reason[t.get("reason", "?")]
            r["n"] += 1
            r["pnl"] += t["grossPnl"]
            r["wins"] += 1 if t["grossPnl"] > 0 else 0
        # 换手/成本：优先用 run 元数据，其次由逐笔成交汇总。old 格式的 daily 行不含
        # turnover/cost 字段，必须从成交推导，否则会静默得到 0。
        gross_traded = sum(float(t["notional"]) for t in trades)
        total_cost = sum(float(t.get("commission", 0.0)) + float(t.get("stampDuty", 0.0))
                         + float(t.get("slippage", 0.0)) for t in trades)
        meta_turnover = self.metrics.get("annualTurnover")
        meta_cost = self.metrics.get("transactionCost")
        annual_turnover = float(meta_turnover) if meta_turnover is not None \
            else (gross_traded / self.initial_capital) * 252.0 / len(self.daily)
        total_turnover = float(meta_turnover) * len(self.daily) / 252.0 if meta_turnover is not None \
            else gross_traded / self.initial_capital
        total_cost = float(meta_cost) if meta_cost is not None else total_cost
        buy_weights = [w for row in tl for w in row["buyWeights"]]
        sell_stop = [t for t in trades if t["side"] == "sell" and "stop" in str(t.get("reason", ""))]
        # 顺延成交（触发卖出信号时停牌/封跌停，无法当日成交）的规模。
        # 这是判断"卖不掉"到底影响多大的直接依据：若长期接近 0，
        # 说明降换手不能指望"延后卖出"，别再为此做整轮实验。
        deferred_sells = [t for t in trades if int(t.get("deferredDays") or 0) > 0]
        sell_notionals = [float(t["notional"]) for t in trades if t["side"] == "sell"]
        deferred_notional = sum(float(t["notional"]) for t in deferred_sells)
        # 换手拆分：止损/移动止损触发的卖出 vs 常规调仓，两者对应的降换手手段完全不同。
        stop_notional = sum(float(t["notional"]) for t in trades
                            if t["side"] == "sell" and "stop" in str(t.get("reason", "")))
        rebalance_notional = sum(float(t["notional"]) for t in trades) - stop_notional
        meta = self.metrics
        stop_notional = float(meta.get("stopTurnover", stop_notional) or stop_notional)
        rebalance_notional = float(meta.get("rebalanceTurnover", rebalance_notional) or rebalance_notional)
        # 年度收益：navs 在窗口边界会重置（每窗口独立从 1.0 起步），必须先用窗口内
        # 相对涨跌还原真实收益曲线，再做年内复利，否则窗口边界会制造天文数字。
        window_local_ret = []
        for i, d in enumerate(self.daily):
            wstart = self._window_of_day.get(d["tradeDate"])
            if i == 0 or (wstart and self._window_of_day.get(self.daily[i - 1]["tradeDate"]) != wstart):
                window_local_ret.append(0.0)          # 窗口首日按窗口起始净值起算
            else:
                window_local_ret.append(navs[i] / navs[i - 1] - 1)
        per_year: dict[str, float] = {}
        for d, r in zip(self.daily, window_local_ret):
            y = d["tradeDate"][:4]
            per_year[y] = (per_year.get(y, 1.0)) * (1 + r)
        for y in per_year:
            per_year[y] -= 1.0
        wrets = [float(w["windowReturn"]) for w in self.windows]
        return {
            "runId": self.run_id,
            "days": len(navs),
            "span": f"{self.daily[0]['tradeDate']}..{self.daily[-1]['tradeDate']}",
            "totalReturn": navs[-1] - 1,
            "annualReturn": navs[-1] ** (252.0 / len(navs)) - 1,
            "sharpe": _sharpe(rets),
            "maxDrawdown": _max_drawdown(navs),
            "daysPositive": sum(1 for r in rets if r > 0) / len(rets) if rets else 0.0,
            "annualTurnover": annual_turnover,
            "totalTurnover": total_turnover,
            "grossTraded": gross_traded,
            "stopTurnover": stop_notional,
            "rebalanceTurnover": rebalance_notional,
            "stopTurnoverShare": stop_notional / (stop_notional + rebalance_notional)
            if (stop_notional + rebalance_notional) > 0 else 0.0,
            "deferredSellCount": len(deferred_sells),
            "deferredSellNotional": deferred_notional,
            "deferredSellTurnoverShare": deferred_notional / sum(sell_notionals) if sell_notionals else 0.0,
            "maxDeferredDays": max((int(t.get("deferredDays") or 0) for t in trades), default=0),
            "totalCost": total_cost,
            "tradeCount": len(trades),
            "stopTradeCount": len(sell_stop),
            "orderCount": len(trades),
            "roundTrips": len(round_trips),
            "winRate": len(wins) / len(round_trips) if round_trips else 0.0,
            "profitFactor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "grossWin": gross_win,
            "grossLoss": -gross_loss,
            "netClosedPnl": gross_win - gross_loss,
            "medianHoldDays": _median(hold_days),
            "meanHoldDays": sum(hold_days) / len(hold_days) if hold_days else 0.0,
            "byReason": {k: dict(v, winRate=(v["wins"] / v["n"] if v["n"] else 0.0))
                         for k, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["n"])},
            "avgHoldings": sum(r["holdings"] for r in tl) / len(tl),
            "cashOnlyDays": sum(1 for r in tl if r["holdings"] == 0) / len(tl),
            "fullTopNDays": sum(1 for r in tl if r["holdings"] >= 5) / len(tl),
            "avgCashRatio": sum(r["cashRatio"] for r in tl) / len(tl),
            "singleHoldingDays": sum(1 for r in tl if r["holdings"] == 1) / len(tl),
            "maxBuyWeight": max(buy_weights) if buy_weights else 0.0,
            "p95BuyWeight": _pct(buy_weights, 0.95),
            "meanBuyWeight": sum(buy_weights) / len(buy_weights) if buy_weights else 0.0,
            "buysOverCap": sum(1 for w in buy_weights if self.max_position_weight > 0
                               and w > self.max_position_weight * 1.02),
            "buyCount": len(buy_weights),
            "perYear": per_year,
            "windows": len(self.windows),
            "winWindows": sum(1 for w in wrets if w > 0),
            "positiveWindowRate": (sum(1 for w in wrets if w > 0) / len(wrets)) if wrets else 0.0,
            "meanWindow": sum(wrets) / len(wrets) if wrets else 0.0,
            "medianWindow": _median(wrets),
            "bestWindow": max(wrets) if wrets else 0.0,
            "worstWindow": min(wrets) if wrets else 0.0,
            "stdWindow": _std(wrets),
            "windowReturns": wrets,
        }


def _ordinal(day: str) -> int:
    import datetime
    return datetime.date(int(day[:4]), int(day[4:6]), int(day[6:8])).toordinal()


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[idx]


def print_report(a: RunAnalysis, label: str) -> dict:
    s = a.summary()
    print(f"\n{'=' * 78}\n{label}  runId={s['runId']}\n{'=' * 78}")
    print(f"区间 {s['span']}  交易日 {s['days']}  窗口 {s['windows']}")
    print(f"累计收益 {s['totalReturn'] * 100:8.2f}%   年化 {s['annualReturn'] * 100:8.2f}%   "
          f"Sharpe {s['sharpe']:.3f}   最大回撤 {s['maxDrawdown'] * 100:7.2f}%")
    print(f"年化换手 {s['annualTurnover']:8.2f} 倍（门槛 30）   总换手 {s['totalTurnover']:.2f}   "
          f"交易成本 {s['totalCost']:,.0f} 元")
    print(f"换手拆分：止损卖出 {s['stopTurnover']:,.0f} 元（{s['stopTurnoverShare'] * 100:.1f}%） / "
          f"常规调仓 {s['rebalanceTurnover']:,.0f} 元（{(1 - s['stopTurnoverShare']) * 100:.1f}%）")
    print(f"顺延成交：{s['deferredSellCount']} 笔（占卖出名义额 {s['deferredSellTurnoverShare'] * 100:.2f}%，"
          f"最长顺延 {s['maxDeferredDays']} 个交易日）")
    print(f"成交笔数 {s['orderCount']}（含止损卖出 {s['stopTradeCount']}）   已平仓 {s['roundTrips']} 笔")
    print(f"胜率 {s['winRate'] * 100:5.1f}%   PF {s['profitFactor']:.3f}   "
          f"已平仓毛损益 {s['netClosedPnl']:,.0f} 元   持仓中位 {s['medianHoldDays']:.0f} 天 / 均值 {s['meanHoldDays']:.2f} 天")
    print(f"资金利用：平均持仓 {s['avgHoldings']:.2f} 只  空仓日 {s['cashOnlyDays'] * 100:.1f}%  "
          f"满5只日 {s['fullTopNDays'] * 100:.1f}%  平均现金比 {s['avgCashRatio'] * 100:.1f}%")
    cap = a.max_position_weight
    cap_txt = f"{cap * 100:.0f}%" if cap > 0 else "未设上限(旧等分现金逻辑)"
    print(f"单笔建仓占净值：均值 {s['meanBuyWeight'] * 100:.2f}%  P95 {s['p95BuyWeight'] * 100:.2f}%  "
          f"最大 {s['maxBuyWeight'] * 100:.2f}%   (上限 {cap_txt}, 越界笔数 {s['buysOverCap']}/{s['buyCount']})")
    print("\n卖出原因归因：")
    print(f"  {'原因':<22}{'笔数':>6}{'毛损益(元)':>16}{'胜率':>9}")
    for reason, r in s["byReason"].items():
        print(f"  {reason:<22}{r['n']:>6}{r['pnl']:>16,.0f}{r['winRate'] * 100:>8.1f}%")
    print("\n年度收益：")
    for y, v in s["perYear"].items():
        print(f"  {y}: {v * 100:8.2f}%")
    print(f"\n窗口质量：盈利 {s['winWindows']}/{s['windows']}（{s['positiveWindowRate'] * 100:.1f}%，门槛 55%）"
          f"  均值 {s['meanWindow'] * 100:.2f}%  中位 {s['medianWindow'] * 100:.2f}%  "
          f"最好 {s['bestWindow'] * 100:.2f}%  最差 {s['worstWindow'] * 100:.2f}%  标准差 {s['stdWindow'] * 100:.2f}%")
    return s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--label", default=None)
    ap.add_argument("--max-position-weight", type=float, default=None,
                    help="覆盖上限（默认从 run-metadata.json 读取）")
    args = ap.parse_args()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        # 支持三种写法：绝对路径 / 相对 ROOT 的路径 / 直接给 runId（在 runs 目录下）
        if run_dir.is_dir():
            run_dir = run_dir.resolve()
        elif (ROOT / run_dir).is_dir():
            run_dir = ROOT / run_dir
        else:
            run_dir = RUNS_DIR / args.run_dir
    cap = args.max_position_weight
    cfg, _ = _load_run_metrics(run_dir.name)
    if cap is None:
        bt = (cfg.get("backtest") or {})
        cap = float(bt.get("maxPositionWeight") or 0.0)
    a = RunAnalysis(run_dir, max_position_weight=cap)
    label = args.label or run_dir.name
    s = print_report(a, label)
    out = ROOT / "artifacts" / "stock-ml" / f"analysis-{run_dir.name}.json"
    out.write_text(json.dumps({"label": label, "summary": s,
                               "config": cfg, "metrics": a.metrics},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[written] {out}")


if __name__ == "__main__":
    main()
