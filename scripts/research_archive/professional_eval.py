"""股票ML模型专业评估：种子稳定性、IC分布、收益归因"""
import sqlite3, json, numpy as np
from pathlib import Path

ROOT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(ROOT / "data/factors.db")
con.row_factory = sqlite3.Row

# 取31特征模型的3个种子记录（forward_3, 24窗口）
rows = con.execute(
    "SELECT run_id, generated_at, label, windows, config, metrics "
    "FROM stock_walkforward_runs WHERE label='forward_3' AND windows=24 ORDER BY generated_at"
).fetchall()

print("=== forward_3 / 24窗口 全部记录 ===")
seed_data = {}
for r in rows:
    cfg = json.loads(r["config"])
    m = json.loads(r["metrics"])
    seed = cfg.get("model", {}).get("randomState")
    nfeat = cfg.get("model", {}).get("nEstimators")
    print(f"{r['generated_at'][:19]} | seed={seed} | 总收益{m['totalReturn']*100:7.2f}% | "
          f"Sharpe {m['sharpe']:6.3f} | 回撤{m['maxDrawdown']*100:7.2f}% | IC {m['avgRankIc']:.4f} | "
          f"年化{m['annualReturn']*100:6.2f}%")
    if seed in (42, 123, 456):
        seed_data[seed] = m

print()
print("=== 种子稳定性分析（forward_3, 24窗口, 4年）===")
returns = [seed_data[s]["totalReturn"] for s in seed_data]
sharpes = [seed_data[s]["sharpe"] for s in seed_data]
drawdowns = [seed_data[s]["maxDrawdown"] for s in seed_data]
ics = [seed_data[s]["avgRankIc"] for s in seed_data]
print(f"种子: {list(seed_data.keys())}")
print(f"总收益: {[f'{r*100:.2f}%' for r in returns]}")
print(f"  均值: {np.mean(returns)*100:.2f}%, 标准差: {np.std(returns)*100:.2f}%")
print(f"  最优: {max(returns)*100:.2f}%, 最差: {min(returns)*100:.2f}%, 极差: {(max(returns)-min(returns))*100:.2f}pct")
print(f"Sharpe: {sharpes}, 均值: {np.mean(sharpes):.3f}")
print(f"最大回撤: {[f'{d*100:.2f}%' for d in drawdowns]}")
print(f"avgRankIC: {ics}, 均值: {np.mean(ics):.4f}")

# V1回测 vs walkforward对比
print()
print("=== V1回测（有前视偏差）vs Walkforward（严格滚动）===")
v1 = {42: {"total": 4.3145, "sharpe": 1.709, "mdd": -0.4119},
      123: {"total": 0.0222, "sharpe": 0.190, "mdd": -0.1640}}
for seed in (42, 123):
    wf = seed_data.get(seed, {})
    print(f"seed={seed}:")
    print(f"  V1回测(前视):  总收益{v1[seed]['total']*100:7.2f}%  Sharpe {v1[seed]['sharpe']:.3f}  回撤{v1[seed]['mdd']*100:.2f}%")
    print(f"  Walkforward:   总收益{wf.get('totalReturn',0)*100:7.2f}%  Sharpe {wf.get('sharpe',0):.3f}  回撤{wf.get('maxDrawdown',0)*100:.2f}%")
    print(f"  虚增倍数: 收益{v1[seed]['total']/max(abs(wf.get('totalReturn',0.01)),0.01):.1f}x")

# 特征重要性
print()
print("=== 特征重要性（31特征seed=42模型）===")
import lightgbm as lgb
model = lgb.Booster(model_file=str(ROOT / "artifacts/stock-ml/stock-wf-model.txt"))
names = model.feature_name()
gain = model.feature_importance(importance_type="gain")
total_gain = sum(gain)
items = sorted(zip(names, gain), key=lambda x: x[1], reverse=True)
cum = 0
for i, (name, val) in enumerate(items):
    pct = val/total_gain*100
    cum += pct
    if i < 10 or pct > 2:
        print(f"  {i+1:2d}. {name:20s} {pct:6.2f}%  累计{cum:6.2f}%")
print(f"  前5因子累计重要性: {sum(v for _,v in items[:5])/total_gain*100:.2f}%")
print(f"  前10因子累计重要性: {sum(v for _,v in items[:10])/total_gain*100:.2f}%")

con.close()
