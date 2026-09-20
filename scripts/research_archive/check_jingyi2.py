import sqlite3
conn = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\market.db')
df = conn.execute("SELECT trade_date, open, high, low, close FROM daily_bars WHERE code='002295.SZ' AND trade_date >= '20260908' ORDER BY trade_date").fetchall()
print('精艺股份 002295.SZ:')
for r in df:
    print(f'  {r[0]}: 开{r[1]:.2f} 高{r[2]:.2f} 低{r[3]:.2f} 收{r[4]:.2f}')
conn.close()
