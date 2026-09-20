"""仓位口径改动的归因分解。

目的：把"收益下滑"和"换手下降"拆成可解释的两部分，避免把一切归因于仓位上限。

口径：每个 walk-forward 窗口都以 initialCapital 独立起步。
  grossExposure(t)  = 1 - 现金/交易前净值        （当日实际投出去的资本比例）
  deployedReturn    = 期末净值^(1/交易日数) - 1   （资金全投口径下的年化）
  cashDrag          = annualReturn - deployedReturn
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\ProdProject\AI\quantification-harness")
sys.path.insert(0, str(ROOT / "scripts"))
from analyze_stock_wf_run import RunAnalysis  # noqa: E402

RUNS = ROOT / "artifacts" / "stock-ml" / "runs"


def stats(a: RunAnalysis) -> dict:
    s = a.summary()
    tl = a.position_replay()
    n = len(tl)
    gross = [max(0.0, 1.0 - r["cashRatio"]) for r in tl]
    exposure = sum(gross) / n
    annual = s["annualReturn"]
    buys = [t for t in a.trades if t["side"] == "buy"]
    sells_stop = [t for t in a.trades if t["side"] == "sell" and "stop" in str(t["reason"])]
    sells_death = [t for t in a.trades if t["side"] == "sell" and t["reason"] == "death_cross"]
    notionals = sorted(t["notional"] for t in buys)
    # 每单位 Gross 暴露赚到的累计收益：用于区分"投得少"与"投得差"。
    gain_per_exposure = s["totalReturn"] / exposure if exposure > 0 else 0.0
    return {
        "runId": a.run_id,
        "avgExposure": exposure,
        "avgCashRatio": sum(r["cashRatio"] for r in tl) / n,
        "gainPerExposure": gain_per_exposure,
        "annual": annual,
        "sharpe": s["sharpe"],
        "holdingsHist": dict(sorted(Counter(r["holdings"] for r in tl).items())),
        "buyDays": sum(1 for r in tl if r["buyCount"] > 0),
        "sellDays": sum(1 for r in tl if r["sellCount"] > 0),
        "multiBuyDays": sum(1 for r in tl if r["buyCount"] >= 2),
        "buyNotionalMedian": notionals[len(notionals) // 2] if notionals else 0,
        "buyNotionalP95": notionals[int(0.95 * (len(notionals) - 1))] if notionals else 0,
        "buys": len(buys),
        "stopSells": len(sells_stop),
        "deathSells": len(sells_death),
        "stopSellNotional": sum(t["notional"] for t in sells_stop),
        "deathSellNotional": sum(t["notional"] for t in sells_death),
        "buyNotional": sum(t["notional"] for t in buys),
        "turnover": s["annualTurnover"],
        "totalReturn": s["totalReturn"],
        "mdd": s["maxDrawdown"],
        "positiveWindowRate": s["positiveWindowRate"],
        "stdWindow": s["stdWindow"],
    }


def main() -> None:
    refs = sys.argv[1:] or ["stock-wf-20260916T145538Z", "stock-wf-20260917T145359Z"]
    out = {}
    for ref in refs:
        a = RunAnalysis(RUNS / ref)
        bt = (a.cfg.get("backtest") or {})
        a.max_position_weight = float(bt.get("maxPositionWeight") or 0.0)
        out[ref] = stats(a)

    keys = [
        ("累计收益", "totalReturn", "pct"), ("年化收益", "annual", "pct"),
        ("平均Gross暴露", "avgExposure", "pct"), ("平均现金比", "avgCashRatio", "pct"),
        ("每单位暴露赚取收益", "gainPerExposure", "num3"),
        ("Sharpe", "sharpe", "num3"), ("最大回撤", "mdd", "pct"),
        ("年化换手", "turnover", "num2"),
        ("买入笔数", "buys", "int"), ("止损卖出笔数", "stopSells", "int"),
        ("死叉卖出笔数", "deathSells", "int"),
        ("有买入的交易日", "buyDays", "int"), ("同日多笔买入日", "multiBuyDays", "int"),
        ("买入名义中位(元)", "buyNotionalMedian", "money"),
        ("买入名义P95(元)", "buyNotionalP95", "money"),
        ("买入名义合计(元)", "buyNotional", "money"),
        ("止损卖出名义(元)", "stopSellNotional", "money"),
        ("死叉卖出名义(元)", "deathSellNotional", "money"),
    ]

    def f(kind, v):
        if kind == "pct":
            return f"{v * 100:.2f}%"
        if kind == "num3":
            return f"{v:.3f}"
        if kind == "num2":
            return f"{v:.2f}"
        if kind == "money":
            return f"{v:,.0f}"
        return f"{int(v)}"

    ids = list(out)
    print(f"\n{'指标':<20}" + "".join(f"{i[-14:]:>22}" for i in ids))
    print("-" * (20 + 22 * len(ids)))
    for name, key, kind in keys:
        print(f"{name:<20}" + "".join(f"{f(kind, out[i][key]):>22}" for i in ids))
    print(f"\n{'持仓只数分布(交易日数)':<20}" + "".join(f"{str(out[i]['holdingsHist']):>22}" for i in ids))

    print("\n换手分解（按买入名义额，万元）:")
    for i in ids:
        d = out[i]
        print(f"  {i[-14:]}: 买入合计 {d['buyNotional']/1e4:8.1f} | 止损卖出 {d['stopSellNotional']/1e4:8.1f} | "
              f"死叉卖出 {d['deathSellNotional']/1e4:8.1f} | 年化换手 {d['turnover']:.2f}")

    (ROOT / "artifacts" / "stock-ml" / "position-sizing-decomposition.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n[written] artifacts/stock-ml/position-sizing-decomposition.json")


if __name__ == "__main__":
    main()
