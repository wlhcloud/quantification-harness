"""排查集成+220%收益来源：分窗口收益分布、收益集中度、持仓与单种子对比"""
import sqlite3, json
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(PROJECT / "data" / "factors.db")
con.row_factory = sqlite3.Row

ENS_RUN = "stock-wf-20260911T065736Z"   # 5种子集成
S42_RUN = "stock-wf-20260911T020518Z"   # 单种子42基线

def load(run_id):
    r = con.execute("SELECT * FROM stock_walkforward_runs WHERE run_id=?", (run_id,)).fetchone()
    return r

ens = load(ENS_RUN)
s42 = load(S42_RUN)
ens_m = json.loads(ens["metrics"])
s42_m = json.loads(s42["metrics"])
print("=== 顶层指标对比 ===")
for k in ["windows","totalReturn","benchmarkReturn","excessReturn","sharpe","maxDrawdown","avgRankIc"]:
    print(f"  {k}: 集成={ens_m.get(k)}  | seed42={s42_m.get(k)}")

# metrics里是否有分窗口明细
print("\n=== 集成metrics的所有key ===")
print(list(ens_m.keys()))
for k, v in ens_m.items():
    if isinstance(v, list):
        print(f"  {k}: list[{len(v)}] = {v}")
con.close()
