"""诊断walkforward窗口划分：数据日期范围 + 不同参数下的窗口数"""
import sqlite3
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
con = sqlite3.connect(PROJECT / "data" / "factors.db")
# stock_ml_factors 日期范围
row = con.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM stock_ml_factors").fetchone()
print("stock_ml_factors 日期范围:", row[0], "->", row[1], " 交易日数:", row[2])

# 按不同参数算窗口数
n_dates = row[2]
for min_train, valid, step, test in [(200, 40, 40, 40), (200, 40, 20, 40), (120, 40, 20, 40), (120, 40, 40, 40)]:
    n_windows = max(1, (n_dates - min_train - valid) // step)
    print(f"minTrain={min_train} valid={valid} step={step} test={test} -> 窗口数={n_windows}")
con.close()
