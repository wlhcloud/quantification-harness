"""
计算热点相关因子，保存到factors.db的stock_hotspot_factors表
热点因子设计：
1. 个股涨幅排名（全市场百分位）
2. 个股成交额排名
3. 连续上涨天数
4. 涨停次数
5. 相对强度（相对沪深300超额收益）
6. 行业短期热度排名
"""
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
import time

t0 = time.time()
PROJECT = r'D:\ProdProject\AI\quantification-harness'

# ========== 1. 读取数据 ==========
print('读取行情数据...')
mdb = sqlite3.connect(f'{PROJECT}/data/market.db')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')

# 读取股票基本信息（行业）
df_sec = pd.read_sql_query('SELECT code, name, industry FROM security_master', mdb)
print(f'  股票数: {len(df_sec)}')

# 读取沪深300指数数据（用于相对强度）
df_idx = pd.read_sql_query(
    'SELECT trade_date, close FROM index_daily_bars WHERE ts_code="000300.SH" ORDER BY trade_date',
    mdb
)
df_idx['idx_ret5'] = df_idx['close'].pct_change(5)
df_idx['idx_ret10'] = df_idx['close'].pct_change(10)
df_idx['idx_ret20'] = df_idx['close'].pct_change(20)
print(f'  沪深300数据: {len(df_idx)} 行')

# 读取股票日线数据（最近250个交易日，足够计算20日因子）
df_bars = pd.read_sql_query(
    f'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars '
    f'WHERE trade_date >= "20250901" ORDER BY code, trade_date',
    mdb
)
print(f'  日线数据: {len(df_bars)} 行')

mdb.close()

# ========== 2. 计算个股技术指标 ==========
print('\n计算个股技术指标...')
df_bars = df_bars.sort_values(['code', 'trade_date'])

# 涨跌幅
df_bars['ret1'] = df_bars.groupby('code')['close'].pct_change(1)
df_bars['ret5'] = df_bars.groupby('code')['close'].pct_change(5)
df_bars['ret10'] = df_bars.groupby('code')['close'].pct_change(10)
df_bars['ret20'] = df_bars.groupby('code')['close'].pct_change(20)

# 平均成交额
df_bars['amount5'] = df_bars.groupby('code')['amount'].transform(lambda x: x.rolling(5).mean())
df_bars['amount10'] = df_bars.groupby('code')['amount'].transform(lambda x: x.rolling(10).mean())

# 连续上涨天数
def calc_up_days(series):
    """计算连续上涨天数"""
    up = (series > 0).astype(int)
    # 连续计数
    result = []
    count = 0
    for v in up:
        if v == 1:
            count += 1
        else:
            count = 0
        result.append(count)
    return result

df_bars['up_days'] = df_bars.groupby('code')['ret1'].transform(calc_up_days)

# 近5日上涨天数
df_bars['up_count5'] = df_bars.groupby('code')['ret1'].transform(
    lambda x: (x > 0).rolling(5).sum()
)

# 涨停次数（涨幅>=9.8%算涨停）
df_bars['is_limit_up'] = (df_bars['ret1'] >= 0.098).astype(int)
df_bars['limit_up_count5'] = df_bars.groupby('code')['is_limit_up'].transform(
    lambda x: x.rolling(5).sum()
)
df_bars['limit_up_count10'] = df_bars.groupby('code')['is_limit_up'].transform(
    lambda x: x.rolling(10).sum()
)

# 合并沪深300指数收益
df_bars = df_bars.merge(df_idx[['trade_date', 'idx_ret5', 'idx_ret10', 'idx_ret20']], on='trade_date', how='left')

# 相对强度（超额收益）
df_bars['relative_strength5'] = df_bars['ret5'] - df_bars['idx_ret5']
df_bars['relative_strength10'] = df_bars['ret10'] - df_bars['idx_ret10']
df_bars['relative_strength20'] = df_bars['ret20'] - df_bars['idx_ret20']

# ========== 3. 计算全市场排名因子（百分位，0-1，越高越热） ==========
print('计算全市场排名因子...')

def rank_percentile(series):
    """计算百分位排名，0-1，越高越好"""
    return series.rank(pct=True)

# 按日期分组计算排名
df_bars['rank_ret5'] = df_bars.groupby('trade_date')['ret5'].transform(rank_percentile)
df_bars['rank_ret10'] = df_bars.groupby('trade_date')['ret10'].transform(rank_percentile)
df_bars['rank_ret20'] = df_bars.groupby('trade_date')['ret20'].transform(rank_percentile)
df_bars['rank_amount5'] = df_bars.groupby('trade_date')['amount5'].transform(rank_percentile)
df_bars['rank_amount10'] = df_bars.groupby('trade_date')['amount10'].transform(rank_percentile)
df_bars['rank_relative_strength5'] = df_bars.groupby('trade_date')['relative_strength5'].transform(rank_percentile)
df_bars['rank_relative_strength10'] = df_bars.groupby('trade_date')['relative_strength10'].transform(rank_percentile)

# ========== 4. 计算行业短期热度因子 ==========
print('计算行业短期热度因子...')

# 合并行业信息
df_bars = df_bars.merge(df_sec[['code', 'industry']], on='code', how='left')

# 按行业+日期计算行业平均收益
df_industry = df_bars.groupby(['trade_date', 'industry']).agg({
    'ret5': 'mean',
    'ret10': 'mean',
    'ret20': 'mean',
    'amount5': 'mean'
}).reset_index()
df_industry = df_industry.rename(columns={
    'ret5': 'ind_ret5',
    'ret10': 'ind_ret10',
    'ret20': 'ind_ret20',
    'amount5': 'ind_amount5'
})

# 按日期计算行业排名
df_industry['industry_rank_ret5'] = df_industry.groupby('trade_date')['ind_ret5'].transform(rank_percentile)
df_industry['industry_rank_ret10'] = df_industry.groupby('trade_date')['ind_ret10'].transform(rank_percentile)
df_industry['industry_rank_ret20'] = df_industry.groupby('trade_date')['ind_ret20'].transform(rank_percentile)

# 合并回个股
df_bars = df_bars.merge(df_industry[['trade_date', 'industry', 'industry_rank_ret5', 'industry_rank_ret10', 'industry_rank_ret20']], 
                          on=['trade_date', 'industry'], how='left')

# ========== 5. 保存热点因子 ==========
print('\n保存热点因子...')

# 选择需要保存的列
hotspot_cols = [
    'trade_date', 'code',
    # 涨幅排名
    'rank_ret5', 'rank_ret10', 'rank_ret20',
    # 成交额排名
    'rank_amount5', 'rank_amount10',
    # 连续上涨
    'up_days', 'up_count5',
    # 涨停次数
    'limit_up_count5', 'limit_up_count10',
    # 相对强度
    'relative_strength5', 'relative_strength10', 'relative_strength20',
    'rank_relative_strength5', 'rank_relative_strength10',
    # 行业短期热度
    'industry_rank_ret5', 'industry_rank_ret10', 'industry_rank_ret20'
]

df_hotspot = df_bars[hotspot_cols].copy()

# 只保留有完整数据的行（20日窗口期之后）
df_hotspot = df_hotspot.dropna(subset=['rank_ret20', 'relative_strength20'])

print(f'  热点因子行数: {len(df_hotspot)}')
print(f'  日期范围: {df_hotspot["trade_date"].min()} ~ {df_hotspot["trade_date"].max()}')

# 保存到factors.db
df_hotspot.to_sql('stock_hotspot_factors', fdb, if_exists='replace', index=False)

# 创建索引
fdb.execute('CREATE INDEX IF NOT EXISTS idx_hotspot_date ON stock_hotspot_factors(trade_date)')
fdb.execute('CREATE INDEX IF NOT EXISTS idx_hotspot_code ON stock_hotspot_factors(code)')
fdb.execute('CREATE INDEX IF NOT EXISTS idx_hotspot_date_code ON stock_hotspot_factors(trade_date, code)')
fdb.commit()

print(f'\n热点因子已保存到 factors.db 的 stock_hotspot_factors 表')
print(f'  共 {len(hotspot_cols)-2} 个热点因子:')
for col in hotspot_cols[2:]:
    print(f'    - {col}')

fdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
