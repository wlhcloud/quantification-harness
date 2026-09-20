"""诊断walkforward参数：yaml键名 vs 代码读取 vs 历史入库config"""
import sqlite3, json, sys
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
sys.path.insert(0, str(PROJECT / "services" / "quant-engine" / "src"))
import yaml

# 1. yaml配置
with open(PROJECT / "config" / "stock-ml.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)
wf_yaml = cfg.get("walkforward", {})
print("=== yaml walkforward 段 ===")
print(json.dumps(wf_yaml, ensure_ascii=False, indent=2))
print("yaml键名: minTrainDays=", wf_yaml.get("minTrainDays"), " stepDays=", wf_yaml.get("stepDays"), " testDays=", wf_yaml.get("testDays"))
print("代码读取键: minTrain=", wf_yaml.get("minTrain", 200), "(默认200)  stepDays=", wf_yaml.get("stepDays", 40), " testDays=", wf_yaml.get("testDays", 40))

# 2. 历史入库config
con = sqlite3.connect(PROJECT / "data" / "factors.db")
con.row_factory = sqlite3.Row
print("\n=== 历史walkforward记录的入库参数 ===")
rows = con.execute(
    "SELECT run_id, config FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT 5"
).fetchall()
for r in rows:
    c = json.loads(r["config"])
    print(r["run_id"], "| minTrain=", c.get("minTrain"), "stepDays=", c.get("stepDays"),
          "testDays=", c.get("testDays"), "validDays=", c.get("validDays"))
con.close()
