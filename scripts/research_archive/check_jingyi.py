import sqlite3
import pandas as pd

mdb = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\market.db')

# 查精艺股份（002295.SZ）近期走势
df = pd.read_sql_query(
    "SELECT trade_date, open, high, low, close, volume, amount FROM daily_bars WHERE code='002295.SZ' AND trade_date >= '20260901' ORDER BY trade_date",
    mdb
)
print('精艺股份 002295.SZ 近期走势:')
print(df.to_string(index=False))

# 计算涨跌幅
df['pct_change'] = df['close'].pct_change() * 100
print('\n涨跌幅:')
for _, row in df.iterrows():
    print(f"  {row['trade_date']}: 开{row['open']:.2f} 高{row['high']:.2f} 低{row['low']:.2f} 收{row['close']:.2f} 涨跌{row['pct_change']:.2f}%")

mdb.close()
