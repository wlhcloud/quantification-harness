# -*- coding: utf-8 -*-
"""Concentration diag: last N windows x 5 seeds, isolated dir."""
import sys, json, shutil
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
sys.path.insert(0, str(PROJECT / "services" / "quant-engine" / "src"))

import yaml
from quant_engine.stock_ml import run_walkforward

with open(PROJECT / "config" / "stock-ml.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

TEST_DIR = PROJECT / "artifacts" / "stock-ml" / "_concentration_test"
if TEST_DIR.exists():
    shutil.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True, exist_ok=True)

print("=== Concentration diag: 5 seeds x last 3 windows ===", flush=True)
result = run_walkforward(
    factors_path=PROJECT / "data" / "factors.db",
    market_path=PROJECT / "data" / "market.db",
    artifact_dir=TEST_DIR,
    cfg=cfg,
    progress=lambda p: None,
    cancelled=lambda: False,
    parameters={"maxWindows": 3},
)
print("\nmetrics:", json.dumps(result["metrics"], ensure_ascii=False))

stats_file = TEST_DIR / "stock-wf-window-stats.json"
if stats_file.exists():
    ws = json.loads(stats_file.read_text(encoding="utf-8"))["windows"]
    print("\n=== Per-window returns ===")
    rets = []
    for w in ws:
        r = w["windowReturn"]
        rets.append(r)
        flag = "  <== HIGH" if r > 0.15 else ("  [LOSS]" if r < -0.05 else "")
        print(f"  {w['testStart']}~{w['testEnd']} | ret={r*100:7.2f}% | IC={w['rankIc']}{flag}")
    rets_sorted = sorted(rets, reverse=True)
    total = 1.0
    for r in rets:
        total *= (1 + r)
    print(f"\nwindows={len(rets)}  compound_total={(total-1)*100:.2f}%")
    print(f"per-window: max={rets_sorted[0]*100:.2f}%  min={rets_sorted[-1]*100:.2f}%  positive={sum(1 for r in rets if r>0)}/{len(rets)}")
    t2 = 1.0
    for r in sorted(rets, reverse=True)[1:]:
        t2 *= (1 + r)
    print(f"exclude_top1_window_compound={(t2-1)*100:.2f}% (if collapses => concentrated/lucky)")

shutil.rmtree(TEST_DIR)
print("\n[cleanup] temp dir removed")
