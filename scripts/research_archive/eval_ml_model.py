"""专业评估股票ML模型：拉取所有walkforward记录和窗口级数据"""
import sqlite3, json, sys
from pathlib import Path

ROOT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(ROOT / "data/factors.db")
con.row_factory = sqlite3.Row

# 1. 表结构
cols = con.execute("PRAGMA table_info(stock_walkforward_runs)").fetchall()
print("表字段:", [c["name"] for c in cols])
print()

# 2. 所有历史run
rows = con.execute(
    "SELECT run_id, generated_at, label, start_date, end_date, windows, status, metrics, config "
    "FROM stock_walkforward_runs ORDER BY generated_at DESC"
).fetchall()
print(f"=== 历史walkforward记录（共{len(rows)}条）===")
for r in rows:
    m = json.loads(r["metrics"])
    cfg = json.loads(r["config"])
    nfeat = len(cfg.get("model", {}).get("features", [])) if "features" in cfg.get("model", {}) else "?"
    print(f"{r['generated_at'][:19]} | {r['run_id'][-15:]} | {r['label']:12s} | "
          f"窗口{r['windows']:2d} | 总收益{m.get('totalReturn',0)*100:8.2f}% | "
          f"基准{m.get('benchmarkReturn',0)*100:7.2f}% | 超额{m.get('excessReturn',0)*100:7.2f}% | "
          f"Sharpe {m.get('sharpe',0):6.3f} | 回撤{m.get('maxDrawdown',0)*100:7.2f}% | "
          f"IC {m.get('avgRankIc',0):.4f} | seed={cfg.get('model',{}).get('randomState','?')}")
print()

# 3. 最新run的窗口级数据（如果metrics里有windowStats）
latest = rows[0]
m = json.loads(latest["metrics"])
print("=== 最新run的metrics所有key ===")
print(list(m.keys()))
print()

# 看看有没有窗口级数据
for key in ["windowStats", "windows_detail", "windowReturns", "icSeries", "dailyReturns"]:
    if key in m:
        print(f"找到窗口级数据: {key}, 长度={len(m[key]) if isinstance(m[key], list) else 'dict'}")
        if isinstance(m[key], list) and len(m[key]) > 0:
            print("前3个:", json.dumps(m[key][:3], ensure_ascii=False, indent=2))

con.close()
