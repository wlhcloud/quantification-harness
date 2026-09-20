"""
计算 forward_3 标签（未来3个交易日收益率），写入 stock_ml_factors 表。
零破坏：只新增 forward_3 列，不修改原有数据。
"""
import sqlite3
import time
from collections import defaultdict

t0 = time.time()
fdb = sqlite3.connect('data/factors.db')
mdb = sqlite3.connect('data/market.db')

# 1. 加列（如果不存在）
cols = [c[1] for c in fdb.execute('PRAGMA table_info(stock_ml_factors)').fetchall()]
if 'forward_3' not in cols:
    print('新增 forward_3 列...')
    fdb.execute('ALTER TABLE stock_ml_factors ADD COLUMN forward_3 REAL')
    fdb.commit()
else:
    print('forward_3 列已存在')

# 2. 从 daily_bars 计算 forward_3
print('读取 daily_bars 收盘价数据...')
rows = mdb.execute('SELECT code, trade_date, close FROM daily_bars WHERE close IS NOT NULL ORDER BY code, trade_date').fetchall()
print(f'读取 {len(rows)} 行，耗时 {time.time()-t0:.1f}s')

# 3. 按 code 分组计算 forward_3
print('计算 forward_3...')
by_code = defaultdict(list)
for code, td, close in rows:
    by_code[code].append((td, close))

forward_3_map = {}  # (code, trade_date) -> forward_3
for code, data in by_code.items():
    for i in range(len(data) - 3):
        td, close = data[i]
        close_3 = data[i + 3][1]  # 3个交易日后
        if close and close > 0:
            forward_3_map[(code, td)] = (close_3 - close) / close

print(f'计算完成，共 {len(forward_3_map)} 个 forward_3 值，耗时 {time.time()-t0:.1f}s')

# 4. 批量更新 stock_ml_factors
print('更新 stock_ml_factors...')
batch = []
cnt = 0
for (code, td), f3 in forward_3_map.items():
    batch.append((f3, code, td))
    if len(batch) >= 10000:
        fdb.executemany('UPDATE stock_ml_factors SET forward_3=? WHERE code=? AND trade_date=?', batch)
        fdb.commit()
        cnt += len(batch)
        batch = []
        if cnt % 500000 == 0:
            print(f'  已更新 {cnt} 行...')

if batch:
    fdb.executemany('UPDATE stock_ml_factors SET forward_3=? WHERE code=? AND trade_date=?', batch)
    fdb.commit()
    cnt += len(batch)

print(f'更新完成，共更新 {cnt} 行，耗时 {time.time()-t0:.1f}s')

# 5. 验证
null_cnt = fdb.execute('SELECT COUNT(*) FROM stock_ml_factors WHERE forward_3 IS NULL').fetchone()[0]
total = fdb.execute('SELECT COUNT(*) FROM stock_ml_factors').fetchone()[0]
avg = fdb.execute('SELECT AVG(forward_3) FROM stock_ml_factors WHERE forward_3 IS NOT NULL').fetchone()[0]
print(f'验证: 总数={total}, forward_3非空={total-null_cnt}, NULL={null_cnt}, 平均={avg:.6f}')
print(f'总耗时 {time.time()-t0:.1f}s')

fdb.close()
mdb.close()
