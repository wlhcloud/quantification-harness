"""诊断：查所有历史walkforward记录的特征注入位置"""
import sqlite3, json
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(PROJECT / "data" / "factors.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT run_id, generated_at, config, windows FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT 12"
).fetchall()
for r in rows:
    try:
        cfg = json.loads(r["config"])
        m = cfg.get("model", {})
        fo = m.get("featuresOverride")
        top_fo = cfg.get("featuresOverride")
        n = len(fo) if fo else (len(top_fo) if top_fo else 0)
        loc = "model" if fo else ("top" if top_fo else "NONE->ETF18")
        print(r["run_id"], "| win=", r["windows"], "| 特征数=", n, "位置=", loc,
              "| seed=", m.get("randomState"), "| ensemble=", cfg.get("ensembleSeeds"))
    except Exception as e:
        print(r["run_id"], "解析失败", e)
con.close()
