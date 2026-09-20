import sqlite3
import json

# 查找quant-sync的数据库
import os
possible_dbs = [
    r'D:\ProdProject\AI\quantification-harness\services\quant-sync\data\sync.db',
    r'D:\ProdProject\AI\quantification-harness\data\sync.db',
    r'D:\ProdProject\AI\quantification-harness\services\quant-sync\quant_sync.db',
]

for db_path in possible_dbs:
    if os.path.exists(db_path):
        print(f'找到数据库: {db_path}')
        conn = sqlite3.connect(db_path)
        try:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            print(f'  表: {tables}')
            if 'data_source_configs' in tables:
                rows = conn.execute("SELECT id, label, protocol, base_url, token, enabled FROM data_source_configs").fetchall()
                for r in rows:
                    print(f'  数据源: id={r[0]}, label={r[1]}, protocol={r[2]}, base_url={r[3]}, token_len={len(r[4]) if r[4] else 0}, enabled={r[5]}')
        except Exception as e:
            print(f'  查询失败: {e}')
        conn.close()
    else:
        print(f'数据库不存在: {db_path}')

# 也检查环境变量
print('\n环境变量:')
for k, v in os.environ.items():
    if 'tushare' in k.lower() or 'ts_token' in k.lower() or 'TUSHARE' in k:
        print(f'  {k}={v[:10]}...' if len(v) > 10 else f'  {k}={v}')
