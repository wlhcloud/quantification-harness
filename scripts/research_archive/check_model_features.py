"""
检查当前模型的特征列表
"""
import lightgbm as lgb
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
MODEL_PATH = PROJECT / 'artifacts' / 'stock-ml' / 'stock-wf-model.txt'

if MODEL_PATH.exists():
    model = lgb.Booster(model_file=str(MODEL_PATH))
    feat_cols = model.feature_name()
    print(f'模型特征总数: {len(feat_cols)}')
    print(f'\n特征列表:')
    for i, f in enumerate(feat_cols):
        print(f'  {i+1}. {f}')
    
    # 检查是否有mom20
    if 'mom20' in feat_cols:
        print(f'\n*** 发现异常特征: mom20 ***')
else:
    print(f'模型文件不存在: {MODEL_PATH}')
