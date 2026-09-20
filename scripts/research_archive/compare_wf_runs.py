"""对比关键walkforward记录的配置差异"""
import sqlite3, json
from pathlib import Path

ROOT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(ROOT / "data/factors.db")
con.row_factory = sqlite3.Row

# 关键记录对比
key_runs = con.execute(
    "SELECT run_id, generated_at, label, start_date, end_date, windows, config, metrics "
    "FROM stock_walkforward_runs ORDER BY generated_at DESC"
).fetchall()

# 找出几条关键记录
targets = []
for r in key_runs:
    m = json.loads(r["metrics"])
    tr = m.get("totalReturn", 0)
    if tr > 1.0 or tr < -0.3 or r["windows"] == 7:
        targets.append(r)

print(f"=== 关键记录（收益>100%或<-30%或7窗口）共{len(targets)}条 ===\n")
for r in targets[:8]:
    cfg = json.loads(r["config"])
    m = json.loads(r["metrics"])
    print(f"--- {r['run_id']} ---")
    print(f"  时间: {r['generated_at'][:19]}")
    print(f"  label: {r['label']}, 窗口数: {r['windows']}, 区间: {r['start_date']} -> {r['end_date']}")
    print(f"  总收益: {m['totalReturn']*100:.2f}%, 基准: {m['benchmarkReturn']*100:.2f}%, 超额: {m['excessReturn']*100:.2f}%")
    print(f"  Sharpe: {m['sharpe']:.3f}, 回撤: {m['maxDrawdown']*100:.2f}%, IC: {m['avgRankIc']:.4f}")
    print(f"  config: minTrain={cfg.get('minTrain')}, validDays={cfg.get('validDays')}, "
          f"testDays={cfg.get('testDays')}, stepDays={cfg.get('stepDays')}, "
          f"topN={cfg.get('topN')}, rebalanceDays={cfg.get('rebalanceDays')}, "
          f"regimeFilter={cfg.get('regimeFilter')}")
    mc = cfg.get("model", {})
    print(f"  model: nEst={mc.get('nEstimators')}, lr={mc.get('learningRate')}, "
          f"leaves={mc.get('numLeaves')}, seed={mc.get('randomState')}, "
          f"subsample={mc.get('subsample')}, colsample={mc.get('colsampleBytree')}")
    print()

# 看看holdings里有什么（最新run）
latest = key_runs[0]
hold = json.loads(latest["holdings"])
print("=== 最新run的holdings ===")
print(json.dumps(hold, indent=2, ensure_ascii=False)[:500])

con.close()
