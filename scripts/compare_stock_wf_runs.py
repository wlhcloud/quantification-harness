"""两个 stock walk-forward run 的并排对比（含发布门槛判定）。

用法:
  python scripts/compare_stock_wf_runs.py --base <runId|dir> --cand <runId|dir> --base-label A --cand-label B
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("QUANT_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_stock_wf_run import RunAnalysis  # noqa: E402

RUNS = ROOT / "artifacts" / "stock-ml" / "runs"


def resolve(ref: str) -> Path:
    p = Path(ref)
    if p.is_dir():
        return p
    d = RUNS / ref
    if d.is_dir():
        return d
    raise SystemExit(f"找不到 run: {ref}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--cand", required=True)
    ap.add_argument("--base-label", default="base")
    ap.add_argument("--cand-label", default="cand")
    args = ap.parse_args()

    def build(ref: str) -> RunAnalysis:
        d = resolve(ref)
        a = RunAnalysis(d)
        bt = (a.cfg.get("backtest") or {})
        a.max_position_weight = float(bt.get("maxPositionWeight") or 0.0)
        return a

    A, B = build(args.base), build(args.cand)
    sa, sb = A.summary(), B.summary()

    rows = [
        ("累计收益", "totalReturn", "pct"),
        ("年化收益", "annualReturn", "pct"),
        ("Sharpe", "sharpe", "num3"),
        ("最大回撤", "maxDrawdown", "pct"),
        ("年化换手(倍)", "annualTurnover", "num2"),
        ("交易成本(元)", "totalCost", "money"),
        ("成交笔数", "orderCount", "int"),
        ("止损卖出", "stopTradeCount", "int"),
        ("已平仓笔数", "roundTrips", "int"),
        ("胜率", "winRate", "pct"),
        ("ProfitFactor", "profitFactor", "num3"),
        ("已平仓毛损益(元)", "netClosedPnl", "money"),
        ("持仓中位(天)", "medianHoldDays", "num0"),
        ("平均持仓(只)", "avgHoldings", "num2"),
        ("空仓日占比", "cashOnlyDays", "pct"),
        ("满5只日占比", "fullTopNDays", "pct"),
        ("平均现金比", "avgCashRatio", "pct"),
        ("单笔建仓均值%净值", "meanBuyWeight", "pct"),
        ("单笔建仓P95%净值", "p95BuyWeight", "pct"),
        ("单笔建仓最大%净值", "maxBuyWeight", "pct"),
        ("盈利窗口", "positiveWindowRate", "pct"),
        ("窗口均值", "meanWindow", "pct"),
        ("窗口中位", "medianWindow", "pct"),
        ("最好窗口", "bestWindow", "pct"),
        ("最差窗口", "worstWindow", "pct"),
        ("窗口标准差", "stdWindow", "pct"),
    ]

    def fmt(kind: str, v) -> str:
        if v is None:
            return "-"
        if kind == "pct":
            return f"{v * 100:.2f}%"
        if kind == "num3":
            return f"{v:.3f}"
        if kind == "num2":
            return f"{v:.2f}"
        if kind == "num0":
            return f"{v:.0f}"
        if kind == "money":
            return f"{v:,.0f}"
        if kind == "int":
            return f"{int(v)}"
        return str(v)

    print(f"\n{'指标':<22}{args.base_label:>20}{args.cand_label:>20}{'差异':>16}")
    print("-" * 80)
    for name, key, kind in rows:
        va, vb = sa.get(key), sb.get(key)
        diff = ""
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            if kind == "pct":
                diff = f"{(vb - va) * 100:+.2f}pp"
            elif kind in ("money", "int", "num0"):
                diff = f"{vb - va:+,.0f}"
            else:
                diff = f"{vb - va:+.3f}"
        print(f"{name:<22}{fmt(kind, va):>20}{fmt(kind, vb):>20}{diff:>16}")

    print(f"\n年度收益 {'':<10}{args.base_label:>14}{args.cand_label:>14}")
    for y in sorted(set(sa["perYear"]) | set(sb["perYear"])):
        a_v, b_v = sa["perYear"].get(y), sb["perYear"].get(y)
        print(f"  {y}: {'' if a_v is None else f'{a_v * 100:>13.2f}%'}"
              f"{'' if b_v is None else f'{b_v * 100:>14.2f}%'}")

    print(f"\n卖出原因归因 {'':<8}{args.base_label:>16}{args.cand_label:>20}")
    reasons = sorted(set(sa["byReason"]) | set(sb["byReason"]))
    for r in reasons:
        x, y = sa["byReason"].get(r), sb["byReason"].get(r)
        xs = f"{x['n']}笔/{x['pnl']:,.0f}" if x else "-"
        ys = f"{y['n']}笔/{y['pnl']:,.0f}" if y else "-"
        print(f"  {r:<18}{xs:>22}{ys:>26}")

    print("\n发布门槛（publishGate）:")
    gate = A.cfg.get("publishGate") or {}
    if not gate:
        import json
        import sqlite3
        con = sqlite3.connect(str(ROOT / "data" / "factors.db"))
        row = con.execute("SELECT config FROM stock_walkforward_runs WHERE run_id=?", (B.run_id,)).fetchone()
        con.close()
        gate = json.loads(row[0]).get("publishGate", {}) if row else {}
    checks = [
        ("窗口数", "windows", "minWindows", lambda v, t: v >= t, "{:.0f}"),
        ("Sharpe", "sharpe", "minSharpe", lambda v, t: v >= t, "{:.3f}"),
        ("累计超额", "excessReturn", "minExcessReturn", lambda v, t: v >= t, "{:.2%}"),
        ("最大回撤", "maxDrawdown", "maxDrawdown", lambda v, t: v >= -t, "{:.2%}"),
        ("年化收益", "annualReturn", "minNetAnnualReturn", lambda v, t: v >= t, "{:.2%}"),
        ("年化换手", "annualTurnover", "maxAnnualTurnover", lambda v, t: v <= t, "{:.2f}"),
        ("盈利窗口率", "positiveWindowRate", "minPositiveWindowRate", lambda v, t: v >= t, "{:.2%}"),
    ]
    for label, mkey, gkey, ok, f in checks:
        t = gate.get(gkey)
        va = (A.metrics or {}).get(mkey, sa.get(mkey))
        vb = (B.metrics or {}).get(mkey, sb.get(mkey))
        if t is None or va is None or vb is None:
            continue
        print(f"  {label:<12} 门槛 {f.format(t):>10}   {args.base_label}: "
              f"{'PASS' if ok(va, t) else 'FAIL'} {f.format(va):>10}   {args.cand_label}: "
              f"{'PASS' if ok(vb, t) else 'FAIL'} {f.format(vb):>10}")


if __name__ == "__main__":
    main()
