import sqlite3
conn = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\market.db')
print('20260911 daily_bars行数:', conn.execute('SELECT COUNT(*) FROM daily_bars WHERE trade_date="20260911"').fetchone()[0])
print('20260910 daily_bars行数:', conn.execute('SELECT COUNT(*) FROM daily_bars WHERE trade_date="20260910"').fetchone()[0])
conn.close()
