"""
训练加入热点因子的新模型（seed=42），然后运行V1回测对比效果
"""
import sys
import os
import json
import time
import shutil
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
sys.path.insert(0, str(PROJECT / 'services' / 'quant-engine' / 'src'))

from quant_engine.stock_ml import run_walkforward
import yaml

# ========== 配置 ==========
LABEL = 'forward_3'
SEED = 42
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'
FACTORS_DB = PROJECT / 'data' / 'factors.db'
MARKET_DB = PROJECT / 'data' / 'market.db'
ARTIFACT_DIR = PROJECT / 'artifacts' / 'stock-ml'
MODEL_PATH = ARTIFACT_DIR / 'stock-wf-model.txt'
RESULTS_FILE = ARTIFACT_DIR / 'hotspot_model_results.json'

ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

# 读取配置
with open(CONFIG_PATH, 'r', encoding='utf-8', errors='ignore') as f:
    cfg = yaml.safe_load(f)

# 修改为forward_3
cfg['label'] = LABEL

# 设置randomState
if 'model' not in cfg:
    cfg['model'] = {}
cfg['model']['randomState'] = SEED

# 打印特征数
features = cfg.get('featuresOverride', [])
print(f'特征总数: {len(features)}')
hotspot_features = [f for f in features if f.startswith('rank_') or f.startswith('up_') or f.startswith('limit_up') or f.startswith('relative_strength') or f.startswith('industry_rank_ret')]
print(f'热点因子数: {len(hotspot_features)}')
print(f'热点因子: {hotspot_features}')

print(f'\n{"="*60}')
print(f'开始训练加入热点因子的新模型 (randomState={SEED})')
print(f'{"="*60}')

t0 = time.time()

try:
    # 训练
    wf_result = run_walkforward(
        factors_path=FACTORS_DB,
        market_path=MARKET_DB,
        artifact_dir=ARTIFACT_DIR,
        cfg=cfg,
        progress=lambda p: None,
        cancelled=lambda: False
    )
    
    wf_metrics = wf_result.get('metrics', {})
    wf_total = wf_metrics.get('totalReturn', 'N/A')
    wf_sharpe = wf_metrics.get('sharpe', 'N/A')
    wf_maxdd = wf_metrics.get('maxDrawdown', 'N/A')
    wf_ic = wf_metrics.get('avgRankIc', 'N/A')
    wf_runid = wf_result.get('runId', 'N/A')
    
    print(f'\n训练完成: runId={wf_runid}')
    print(f'  walkforward指标:')
    print(f'    总收益: {wf_total}')
    print(f'    Sharpe: {wf_sharpe}')
    print(f'    最大回撤: {wf_maxdd}')
    print(f'    avgRankIC: {wf_ic}')
    print(f'  耗时: {time.time()-t0:.1f}s')
    
    # 备份新模型
    new_model_backup = ARTIFACT_DIR / f'stock-wf-model_hotspot_seed{SEED}.txt'
    if MODEL_PATH.exists():
        shutil.copy2(MODEL_PATH, new_model_backup)
        print(f'  新模型已备份: {new_model_backup}')
    
    # 运行V1回测
    print(f'\n运行V1技术面择时回测...')
    v1_script = PROJECT / 'scripts' / 'backtest_technical_timing.py'
    
    import subprocess
    v1_result = subprocess.run(
        [sys.executable, str(v1_script)],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=str(PROJECT)
    )
    
    # 解析回测结果
    v1_output = v1_result.stdout
    print(v1_output[-2000:] if len(v1_output) > 2000 else v1_output)
    
    # 保存结果
    results = {
        'seed': SEED,
        'label': LABEL,
        'features_count': len(features),
        'hotspot_features_count': len(hotspot_features),
        'hotspot_features': hotspot_features,
        'walkforward': {
            'runId': wf_runid,
            'totalReturn': wf_total,
            'sharpe': wf_sharpe,
            'maxDrawdown': wf_maxdd,
            'avgRankIc': wf_ic,
        },
        'v1_backtest_output': v1_output[-3000:] if len(v1_output) > 3000 else v1_output,
        'train_time_seconds': time.time() - t0,
    }
    
    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    
    print(f'\n结果已保存: {RESULTS_FILE}')
    
except Exception as e:
    print(f'\n训练失败: {e}')
    import traceback
    traceback.print_exc()
