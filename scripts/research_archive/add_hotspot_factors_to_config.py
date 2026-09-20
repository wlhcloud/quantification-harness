"""
修改stock-ml.yaml配置，添加热点因子到featuresOverride
"""
import yaml
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'

# 读取配置（用utf-8编码，忽略错误）
with open(CONFIG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
    cfg = yaml.safe_load(f)

# 热点因子列表
hotspot_factors = [
    'rank_ret5', 'rank_ret10', 'rank_ret20',
    'rank_amount5', 'rank_amount10',
    'up_days', 'up_count5',
    'limit_up_count5', 'limit_up_count10',
    'relative_strength5', 'relative_strength10', 'relative_strength20',
    'rank_relative_strength5', 'rank_relative_strength10',
    'industry_rank_ret5', 'industry_rank_ret10', 'industry_rank_ret20',
]

# 获取当前featuresOverride
current_features = cfg.get('featuresOverride', [])
print(f'当前特征数: {len(current_features)}')

# 检查是否已经有热点因子
existing_hotspot = [f for f in current_features if f in hotspot_factors]
if existing_hotspot:
    print(f'已存在热点因子: {existing_hotspot}')
else:
    # 在close_position_5d之后插入热点因子
    if 'close_position_5d' in current_features:
        idx = current_features.index('close_position_5d') + 1
        new_features = current_features[:idx] + hotspot_factors + current_features[idx:]
    else:
        new_features = current_features + hotspot_factors
    
    cfg['featuresOverride'] = new_features
    print(f'新特征数: {len(new_features)}')
    print(f'新增热点因子: {len(hotspot_factors)}个')

# 保存配置
with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f'\n配置已保存: {CONFIG_PATH}')

# 验证
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    cfg2 = yaml.safe_load(f)
features = cfg2.get('featuresOverride', [])
print(f'\n验证: 特征总数={len(features)}')
print(f'热点因子: {[f for f in features if f in hotspot_factors]}')
