import sqlite3
conn = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\factors.db')
print('表列表:')
for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
    print(' ', r[0])
print('\nstock_ml_factors最新日期:', conn.execute('SELECT MAX(trade_date) FROM stock_ml_factors').fetchone()[0])
print('stock_industry_factors最新日期:', conn.execute('SELECT MAX(trade_date) FROM stock_industry_factors').fetchone()[0])
print('stock_money_flow_factors最新日期:', conn.execute('SELECT MAX(trade_date) FROM stock_money_flow_factors').fetchone()[0])
conn.close()
