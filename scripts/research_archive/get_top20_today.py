"""
用seed=42模型对指定日期的股票打分，选出Top20候选
输出：股票代码、名称、行业、分数、关键技术指标
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = r'D:\ProdProject\AI\quantification-harness'
TRADE_DATE = '20260910'
TOP_N = 20

# ========== 1. 加载模型 ==========
print('加载seed=42模型...')
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()
print(f'  特征数: {len(feature_names)}')

# ========== 2. 读取因子数据 ==========
print(f'读取{TRADE_DATE}因子数据...')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')
mdb = sqlite3.connect(f'{PROJECT}/data/market.db')

df_factors = pd.read_sql_query(
    f'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    f'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    f'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    f'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    f'FROM stock_ml_factors WHERE trade_date = "{TRADE_DATE}"',
    fdb
)
print(f'  基础因子: {len(df_factors)} 行')

df_industry = pd.read_sql_query(
    f'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    f'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date = "{TRADE_DATE}"',
    fdb
)

df_money = pd.read_sql_query(
    f'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date = "{TRADE_DATE}"',
    fdb
)

df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')

# ========== 3. 读取行情数据，计算均线 ==========
print('读取行情数据并计算均线...')
# 从因子数据里获取股票代码列表
codes = df_factors['code'].unique().tolist()
codes_str = ','.join([f"'{c}'" for c in codes])
df_bars = pd.read_sql_query(
    f'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars '
    f'WHERE code IN ({codes_str}) '
    f'AND trade_date >= "20260801" ORDER BY code, trade_date',
    mdb
)

df_bars = df_bars.sort_values(['code', 'trade_date'])
df_bars['ma5'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
df_bars['ma10'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
df_bars['ma20'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
df_bars['ma5_prev'] = df_bars.groupby('code')['ma5'].shift(1)
df_bars['ma10_prev'] = df_bars.groupby('code')['ma10'].shift(1)
df_bars['golden_cross'] = (df_bars['ma5_prev'] <= df_bars['ma10_prev']) & (df_bars['ma5'] > df_bars['ma10'])
df_bars['death_cross'] = (df_bars['ma5_prev'] >= df_bars['ma10_prev']) & (df_bars['ma5'] < df_bars['ma10'])
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)

# 只保留今天的数据
df_bars_today = df_bars[df_bars['trade_date'] == TRADE_DATE].copy()

# ========== 4. 合并因子和行情 ==========
df = df.merge(df_bars_today[['trade_date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount',
                               'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross', 'prev_close']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))

# ========== 5. 过滤股票池 ==========
print('过滤股票池...')
# 排除北交所、科创板
df = df[~df['code'].str.startswith(('920', '688'))]
# 市值>=20亿（mktCap单位是万元）
df = df[df['mktCap'] >= 200000]
# 排除ST股（名称含ST）
df_names = pd.read_sql_query('SELECT code, name, industry FROM security_master', mdb)
df = df.merge(df_names, on='code', how='left')
df = df[~df['name'].str.contains('ST', na=False)]
# 排除停牌（成交量为0）
df = df[df['volume'] > 0]
# 排除涨跌停（涨停买不进，跌停不考虑）
df['pct_change'] = (df['close'] - df['prev_close']) / df['prev_close'] * 100
df = df[(df['pct_change'] < 9.9) & (df['pct_change'] > -9.9)]

print(f'  过滤后股票池: {len(df)} 只')

# ========== 6. 模型打分 ==========
print('模型打分...')
X = df[feature_names].values
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)

# 排序，选TopN
df = df.sort_values('score', ascending=False)
df_top = df.head(TOP_N).copy()

# ========== 7. 输出结果 ==========
print(f'\n{"="*100}')
print(f'{TRADE_DATE} Top{TOP_N}候选股票（seed=42模型）')
print(f'{"="*100}')

print(f'\n{"排名":<4}{"代码":<12}{"名称":<10}{"行业":<12}{"分数":<8}{"收盘价":<8}{"涨跌幅":<8}{"MA5":<8}{"MA10":<8}{"MA20":<8}{"金叉":<6}{"市值(亿)":<10}')
print('-'*100)

for i, (_, row) in enumerate(df_top.iterrows(), 1):
    golden = '✓' if row['golden_cross'] else ' '
    mktcap_yi = row['mktCap'] / 10000  # 万元转亿元
    print(f'{i:<4}{row["code"]:<12}{row["name"]:<10}{str(row["industry"])[:10]:<12}{row["score"]:<8.4f}{row["close"]:<8.2f}{row["pct_change"]:<8.2f}{row["ma5"]:<8.2f}{row["ma10"]:<8.2f}{row["ma20"]:<8.2f}{golden:<6}{mktcap_yi:<10.1f}')

# 保存结果
df_top.to_csv(f'{PROJECT}/artifacts/stock-ml/top20_{TRADE_DATE}.csv', index=False, encoding='utf-8-sig')
print(f'\n结果已保存: artifacts/stock-ml/top20_{TRADE_DATE}.csv')

fdb.close()
mdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
