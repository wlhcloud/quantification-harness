"""清理快测写入正式库的临时walkforward记录"""
import sqlite3
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(PROJECT / "data" / "factors.db")
cur = con.execute("DELETE FROM stock_walkforward_runs WHERE run_id=?", ("stock-wf-20260911T063833Z",))
con.commit()
print("清理快测walkforward记录行数:", cur.rowcount)
# 确认最新记录
row = con.execute("SELECT run_id, generated_at FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
print("当前最新walkforward记录:", row)
con.close()
