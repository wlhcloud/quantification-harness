import sqlite3
conn = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\market.db')
tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%minute%'").fetchall()]
print('分钟线相关表:', tables)
for t in tables:
    try:
        cnt = conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        max_date = conn.execute(f'SELECT MAX(trade_date) FROM {t}').fetchone()[0]
        print(f'  {t}: {cnt}行, 最新日期={max_date}')
    except Exception as e:
        print(f'  {t}: 查询失败 - {e}')
conn.close()
