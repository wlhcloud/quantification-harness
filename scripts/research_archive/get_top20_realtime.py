"""
用Tushare rt_k实时日线接口拉取今天的盘中数据，
合并历史数据计算均线，然后用seed=42模型打分选出Top20候选
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import json
import urllib.request
import urllib.parse
import time

t0 = time.time()
PROJECT = r'D:\ProdProject\AI\quantification-harness'
TODAY = '20260911'
TOP_N = 20

# promax代理配置（x-api-key协议）
PROMAX_URL = 'https://pcd.mobcvb.cn/tushare/pro'
PROMAX_TOKEN = 'tsr_1FjRkziz3M7m0aLcTk0ZgnK03__xO3EYq0ZdwQqdwSE'

def fetch_rt_k(ts_code, max_retries=3):
    """用promax代理调用rt_k接口，带重试"""
    for attempt in range(max_retries):
        try:
            params = {"ts_code": ts_code}
            query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
            url = f"{PROMAX_URL}/rt_k?{query}"
            request = urllib.request.Request(url, headers={"X-API-Key": PROMAX_TOKEN})
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read().decode())
            if payload.get('code') != 0:
                raise Exception(f"rt_k error: {payload.get('msg')}")
            data = payload.get('data', {})
            fields = data.get('fields', [])
            items = data.get('items', [])
            return pd.DataFrame(items, columns=fields)
        except Exception as e:
            if attempt < max_retries - 1:
                wait = 5 * (attempt + 1)
                print(f'    第{attempt+1}次失败: {e}，{wait}秒后重试...')
                time.sleep(wait)
            else:
                raise e

# ========== 1. 加载模型 ==========
print('加载seed=42模型...')
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()
print(f'  特征数: {len(feature_names)}')

# ========== 2. 拉取今天的实时日线 ==========
print(f'\n拉取{TODAY}实时日线数据...')

# 分批拉取（避免一次拉太多）
rt_dfs = []
for ts_code_pattern in ['6*.SH', '0*.SZ', '3*.SZ']:
    try:
        print(f'  拉取 {ts_code_pattern}...')
        df_rt = fetch_rt_k(ts_code_pattern)
        if df_rt is not None and len(df_rt) > 0:
            rt_dfs.append(df_rt)
            print(f'    获取 {len(df_rt)} 条')
        time.sleep(3)  # 增加间隔避免429
    except Exception as e:
        print(f'    失败: {e}')

if not rt_dfs:
    print('ERROR: 未获取到实时数据')
    exit(1)

df_rt = pd.concat(rt_dfs, ignore_index=True)
print(f'\n实时数据总计: {len(df_rt)} 条')
print(f'  列: {list(df_rt.columns)}')

# 重命名列以匹配daily_bars
df_rt = df_rt.rename(columns={
    'ts_code': 'code',
    'pre_close': 'pre_close',
    'open': 'open',
    'high': 'high',
    'low': 'low',
    'close': 'close',
    'vol': 'volume',
    'amount': 'amount',
})
df_rt['trade_date'] = TODAY

# ========== 3. 读取历史行情数据，计算均线 ==========
print('\n读取历史行情数据并计算均线...')
mdb = sqlite3.connect(f'{PROJECT}/data/market.db')

# 获取所有股票代码
codes = df_rt['code'].unique().tolist()
codes_str = ','.join([f"'{c}'" for c in codes])

# 读取最近60天的历史数据
df_hist = pd.read_sql_query(
    f'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars '
    f'WHERE code IN ({codes_str}) AND trade_date >= "20260601" ORDER BY code, trade_date',
    mdb
)
print(f'  历史数据: {len(df_hist)} 行')

# 合并历史和实时数据
df_all = pd.concat([df_hist, df_rt[['trade_date', 'code', 'open', 'high', 'low', 'close', 'volume', 'amount']]], ignore_index=True)
df_all = df_all.sort_values(['code', 'trade_date'])

# 计算均线
df_all['ma5'] = df_all.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
df_all['ma10'] = df_all.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
df_all['ma20'] = df_all.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
df_all['ma5_prev'] = df_all.groupby('code')['ma5'].shift(1)
df_all['ma10_prev'] = df_all.groupby('code')['ma10'].shift(1)
df_all['golden_cross'] = (df_all['ma5_prev'] <= df_all['ma10_prev']) & (df_all['ma5'] > df_all['ma10'])
df_all['death_cross'] = (df_all['ma5_prev'] >= df_all['ma10_prev']) & (df_all['ma5'] < df_all['ma10'])
df_all['prev_close'] = df_all.groupby('code')['close'].shift(1)

# 只保留今天的数据
df_today = df_all[df_all['trade_date'] == TODAY].copy()
print(f'  今天有效数据: {len(df_today)} 行')

# ========== 4. 读取昨天的因子数据 ==========
print('\n读取昨天(20260910)的因子数据...')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')

df_factors = pd.read_sql_query(
    f'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    f'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    f'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    f'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    f'FROM stock_ml_factors WHERE trade_date = "20260910"',
    fdb
)
print(f'  基础因子: {len(df_factors)} 行')

df_industry = pd.read_sql_query(
    f'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    f'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date = "20260910"',
    fdb
)

df_money = pd.read_sql_query(
    f'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date = "20260910"',
    fdb
)

df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')

# 用今天的实时价格更新close
df = df.merge(df_today[['code', 'close', 'open', 'high', 'low', 'volume', 'amount',
                          'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross', 'prev_close']],
               on='code', how='inner', suffixes=('_factor', '_today'))

# 用今天的close替换因子里的close
df['close'] = df['close_today']

# ========== 5. 过滤股票池 ==========
print('\n过滤股票池...')
# 排除北交所、科创板
df = df[~df['code'].str.startswith(('920', '688'))]
# 市值>=20亿（mktCap单位是万元）
df = df[df['mktCap'] >= 200000]
# 排除ST股
df_names = pd.read_sql_query('SELECT code, name, industry FROM security_master', mdb)
df = df.merge(df_names, on='code', how='left')
df = df[~df['name'].str.contains('ST', na=False)]
# 排除停牌（成交量为0）
df = df[df['volume'] > 0]
# 排除涨跌停
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
print(f'\n{"="*110}')
print(f'{TODAY}（实时盘中）Top{TOP_N}候选股票（seed=42模型）')
print(f'{"="*110}')

print(f'\n{"排名":<4}{"代码":<12}{"名称":<10}{"行业":<12}{"分数":<8}{"现价":<8}{"涨跌幅":<8}{"MA5":<8}{"MA10":<8}{"MA20":<8}{"金叉":<6}{"市值(亿)":<10}')
print('-'*110)

for i, (_, row) in enumerate(df_top.iterrows(), 1):
    golden = '✓' if row['golden_cross'] else ' '
    mktcap_yi = row['mktCap'] / 10000
    print(f'{i:<4}{row["code"]:<12}{row["name"]:<10}{str(row["industry"])[:10]:<12}{row["score"]:<8.4f}{row["close"]:<8.2f}{row["pct_change"]:<8.2f}{row["ma5"]:<8.2f}{row["ma10"]:<8.2f}{row["ma20"]:<8.2f}{golden:<6}{mktcap_yi:<10.1f}')

# 保存结果
df_top.to_csv(f'{PROJECT}/artifacts/stock-ml/top20_{TODAY}_realtime.csv', index=False, encoding='utf-8-sig')
print(f'\n结果已保存: artifacts/stock-ml/top20_{TODAY}_realtime.csv')

fdb.close()
mdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
