"""
检查加入热点因子后新模型的特征重要性
"""
import lightgbm as lgb
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
MODEL_PATH = PROJECT / 'artifacts' / 'stock-ml' / 'stock-wf-model.txt'

model = lgb.Booster(model_file=str(MODEL_PATH))
feat_cols = model.feature_name()
importance = model.feature_importance(importance_type='gain')

# 按重要性排序
pairs = sorted(zip(feat_cols, importance), key=lambda x: x[1], reverse=True)
total = sum(importance)

print(f'模型特征总数: {len(feat_cols)}')
print(f'\n特征重要性排名（gain）:')
print(f'{"排名":<4} {"特征名":<25} {"重要性":<12} {"占比":<8}')
print('-' * 55)

hotspot_features = {'rank_ret5', 'rank_ret10', 'rank_ret20', 'rank_amount5', 'rank_amount10',
                     'up_days', 'up_count5', 'limit_up_count5', 'limit_up_count10',
                     'relative_strength5', 'relative_strength10', 'relative_strength20',
                     'rank_relative_strength5', 'rank_relative_strength10',
                     'industry_rank_ret5', 'industry_rank_ret10', 'industry_rank_ret20'}

hotspot_total = 0
for i, (name, imp) in enumerate(pairs):
    pct = imp / total * 100
    is_hotspot = ' [热点]' if name in hotspot_features else ''
    print(f'{i+1:<4} {name:<25} {imp:<12.1f} {pct:<8.2f}{is_hotspot}')
    if name in hotspot_features:
        hotspot_total += pct

print(f'\n热点因子总占比: {hotspot_total:.2f}%')
print(f'基础因子总占比: {100 - hotspot_total:.2f}%')
