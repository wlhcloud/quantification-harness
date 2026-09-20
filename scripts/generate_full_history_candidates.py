"""
生成全历史ML候选池数据（ml_walkforward Top20）
加载模型，对2024-01以来每个交易日的全市场股票打分，选Top20写入selection_candidates表
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time
from datetime import datetime, timezone

t0 = time.time()
PROJECT = "D:\\ProdProject\\AI\\quantification-harness"
MODEL_ID = "ml_walkforward"
TOP_N = 20

print("加载模型...")
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()
print(f"  特征数: {len(feature_names)}")

print("读取因子数据...")
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')

# 基础因子
df_factors = pd.read_sql_query(
    'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    'FROM stock_ml_factors WHERE trade_date >= "20240101"', fdb)
print(f"  基础因子: {len(df_factors)} 行")

# 行业因子
df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20240101"', fdb)
print(f"  行业因子: {len(df_industry)} 行")

# 资金因子
df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20240101"', fdb)
print(f"  资金因子: {len(df_money)} 行")

df_full = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df_full = df_full.merge(df_money, on=['trade_date', 'code'], how='left')
print(f"  合并后: {len(df_full)} 行, {df_full['trade_date'].nunique()} 个交易日")

print("模型打分...")
X = np.nan_to_num(df_full[feature_names].values, nan=0.0, posinf=0.0, neginf=0.0)
df_full['score'] = model.predict(X)
print(f"  打分完成, 耗时 {time.time()-t0:.1f}s")

# 选Top20
print("选Top20候选...")
df_full = df_full.sort_values(['trade_date', 'score'], ascending=[True, False])
df_full['rank'] = df_full.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_top = df_full[df_full['rank'] <= TOP_N].copy()
print(f"  Top20候选: {len(df_top)} 行")

# 写入selection_candidates表
print("写入selection_candidates表...")
computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# 先删除旧的ml_walkforward数据（保留最近5天的也删，统一重建）
cur = fdb.cursor()
deleted = cur.execute("DELETE FROM selection_candidates WHERE model_id=?", (MODEL_ID,)).rowcount
print(f"  删除旧数据: {deleted} 行")

# 批量插入
rows_to_insert = []
for _, row in df_top.iterrows():
    score_val = float(row['score']) * 100  # 放大100倍，与现有格式一致（0.16 -> 16.0）
    reasons = f'[{{"factor": "ml", "label": "机器学习预测", "score": {score_val:.4f}, "contribution": 0.2}}]'
    rows_to_insert.append((
        row['trade_date'], MODEL_ID, int(row['rank']), row['code'],
        round(score_val, 4), reasons, computed_at
    ))

cur.executemany(
    "INSERT INTO selection_candidates (trade_date, model_id, rank, code, score, reasons_json, computed_at) "
    "VALUES (?,?,?,?,?,?,?)", rows_to_insert)
fdb.commit()
print(f"  插入: {len(rows_to_insert)} 行")

# 验证
r = fdb.execute("SELECT MIN(trade_date), MAX(trade_date), COUNT(DISTINCT trade_date) FROM selection_candidates WHERE model_id=?", (MODEL_ID,)).fetchone()
print(f"\n验证: 日期范围 {r[0]} ~ {r[1]}, 共{r[2]}个交易日")

fdb.close()
print(f"总耗时: {time.time()-t0:.1f}s")
