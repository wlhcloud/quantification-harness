"""
计算 forward_1 标签（未来1个交易日收益率），写入 stock_ml_factors 表。
零破坏：只新增 forward_1 列，不修改原有数据。
"""
import sqlite3
import time

t0 = time.time()
fdb = sqlite3.connect('data/factors.db')
mdb = sqlite3.connect('data/market.db')

# 1. 加列（如果不存在）
cols = [c[1] for c in fdb.execute('PRAGMA table_info(stock_ml_factors)').fetchall()]
if 'forward_1' not in cols:
    print('新增 forward_1 列...')
    fdb.execute('ALTER TABLE stock_ml_factors ADD COLUMN forward_1 REAL')
    fdb.commit()
else:
    print('forward_1 列已存在')

# 2. 从 daily_bars 计算 forward_1
print('读取 daily_bars 收盘价数据...')
# 只需要 code, trade_date, close
rows = mdb.execute('SELECT code, trade_date, close FROM daily_bars WHERE close IS NOT NULL ORDER BY code, trade_date').fetchall()
print(f'读取 {len(rows)} 行，耗时 {time.time()-t0:.1f}s')

# 3. 按 code 分组计算 forward_1
print('计算 forward_1...')
from collections import defaultdict
by_code = defaultdict(list)
for code, td, close in rows:
    by_code[code].append((td, close))

# 计算每只股票每天的 forward_1
forward_1_map = {}  # (code, trade_date) -> forward_1
for code, data in by_code.items():
    # data 已按 trade_date 排序
    for i in range(len(data) - 1):
        td, close = data[i]
        next_close = data[i + 1][1]
        if close and close > 0:
            forward_1_map[(code, td)] = (next_close - close) / close

print(f'计算完成，共 {len(forward_1_map)} 个 forward_1 值，耗时 {time.time()-t0:.1f}s')

# 4. 批量更新 stock_ml_factors
print('更新 stock_ml_factors...')
# 分批更新，每批10000条
batch = []
cnt = 0
for (code, td), f1 in forward_1_map.items():
    batch.append((f1, code, td))
    if len(batch) >= 10000:
        fdb.executemany('UPDATE stock_ml_factors SET forward_1=? WHERE code=? AND trade_date=?', batch)
        fdb.commit()
        cnt += len(batch)
        batch = []
        if cnt % 100000 == 0:
            print(f'  已更新 {cnt} 行...')

if batch:
    fdb.executemany('UPDATE stock_ml_factors SET forward_1=? WHERE code=? AND trade_date=?', batch)
    fdb.commit()
    cnt += len(batch)

print(f'更新完成，共更新 {cnt} 行，耗时 {time.time()-t0:.1f}s')

# 5. 验证
null_cnt = fdb.execute('SELECT COUNT(*) FROM stock_ml_factors WHERE forward_1 IS NULL').fetchone()[0]
total = fdb.execute('SELECT COUNT(*) FROM stock_ml_factors').fetchone()[0]
avg = fdb.execute('SELECT AVG(forward_1) FROM stock_ml_factors WHERE forward_1 IS NOT NULL').fetchone()[0]
print(f'验证: 总数={total}, forward_1非空={total-null_cnt}, NULL={null_cnt}, 平均={avg:.6f}')
print(f'总耗时 {time.time()-t0:.1f}s')

fdb.close()
mdb.close()
