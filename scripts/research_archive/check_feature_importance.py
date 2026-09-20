"""
查看seed=42模型的特征列表和特征重要性
"""
import lightgbm as lgb
import pandas as pd

PROJECT = r'D:\ProdProject\AI\quantification-harness'

# 加载模型
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()
feature_importance = model.feature_importance(importance_type='gain')  # gain表示分裂带来的增益

print(f'模型特征总数: {len(feature_names)}')
print(f'\n{"="*80}')
print(f'因子重要性排名（按gain降序）')
print(f'{"="*80}')
print(f'{"排名":<4}{"因子名称":<25}{"重要性(gain)":<15}{"占比":<10}{"因子类别"}')
print('-'*80)

# 分类
def classify_factor(name):
    if name.startswith('industry_'):
        return '行业轮动'
    elif name in ['turnover_surge', 'close_position_5d']:
        return '资金流向'
    elif name in ['momentum20', 'momentum60', 'trend', 'drawdown60', 'volatility20']:
        return '趋势/动量'
    elif name in ['volumeRatio', 'volSurge3', 'volSurge5', 'amountRatio5', 'volPriceCorr20', 'turnoverRatio5', 'amplitude20']:
        return '量能/换手'
    elif name in ['peTtm', 'pb', 'psTtm', 'dvTtm', 'roe', 'grossMargin', 'netMargin', 'revenueYoy', 'netprofitYoy', 'ocfYoy']:
        return '基本面/估值'
    elif name in ['mktCap', 'floatRatio']:
        return '市值/流通'
    else:
        return '其他'

# 创建DataFrame并排序
df_imp = pd.DataFrame({
    'feature': feature_names,
    'importance': feature_importance
})
df_imp = df_imp.sort_values('importance', ascending=False).reset_index(drop=True)
df_imp['category'] = df_imp['feature'].apply(classify_factor)
df_imp['pct'] = df_imp['importance'] / df_imp['importance'].sum() * 100

for i, row in df_imp.iterrows():
    print(f'{i+1:<4}{row["feature"]:<25}{row["importance"]:<15.1f}{row["pct"]:<10.2f}{row["category"]}')

print(f'\n{"="*80}')
print(f'按因子类别汇总')
print(f'{"="*80}')
df_cat = df_imp.groupby('category').agg({
    'importance': 'sum',
    'feature': 'count',
    'pct': 'sum'
}).sort_values('importance', ascending=False)
print(f'{"类别":<15}{"因子数":<8}{"总重要性":<15}{"占比":<10}')
print('-'*50)
for cat, row in df_cat.iterrows():
    print(f'{cat:<15}{row["feature"]:<8}{row["importance"]:<15.1f}{row["pct"]:<10.2f}')

# 保存
df_imp.to_csv(f'{PROJECT}/artifacts/stock-ml/feature_importance_seed42.csv', index=False, encoding='utf-8-sig')
print(f'\n特征重要性已保存: artifacts/stock-ml/feature_importance_seed42.csv')
