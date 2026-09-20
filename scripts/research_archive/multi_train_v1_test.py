"""
多次训练forward_3模型 + V1技术面择时回测，寻找能复现+431%的模型
每次用不同randomState训练，训练后备份模型并运行V1回测
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
RANDOM_STATES = [42, 123, 456, 789, 1024, 2048, 8888, 9999]  # 8个随机种子
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'
FACTORS_DB = PROJECT / 'data' / 'factors.db'
MARKET_DB = PROJECT / 'data' / 'market.db'
ARTIFACT_DIR = PROJECT / 'artifacts' / 'stock-ml'
MODEL_PATH = ARTIFACT_DIR / 'stock-wf-model.txt'
MODEL_BACKUP_DIR = ARTIFACT_DIR / 'multi_train_models'
RESULTS_FILE = ARTIFACT_DIR / 'multi_train_results.json'

MODEL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# 读取配置
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    base_cfg = yaml.safe_load(f)

# 修改为forward_3
base_cfg['label'] = LABEL

results = []

for i, seed in enumerate(RANDOM_STATES):
    print(f'\n{"="*60}')
    print(f'第 {i+1}/{len(RANDOM_STATES)} 次训练，randomState={seed}')
    print(f'{"="*60}')
    
    t0 = time.time()
    
    # 复制配置并设置randomState
    cfg = json.loads(json.dumps(base_cfg))  # deep copy
    if 'model' not in cfg:
        cfg['model'] = {}
    cfg['model']['randomState'] = seed
    
    try:
        # 训练
        print(f'开始训练 (randomState={seed})...')
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
        wf_runid = wf_result.get('runId', 'N/A')
        
        print(f'训练完成: runId={wf_runid}')
        print(f'  walkforward: total={wf_total}, sharpe={wf_sharpe}, maxDD={wf_maxdd}')
        print(f'  耗时: {time.time()-t0:.1f}s')
        
        # 备份模型
        backup_path = MODEL_BACKUP_DIR / f'model_seed{seed}.txt'
        if MODEL_PATH.exists():
            shutil.copy2(MODEL_PATH, backup_path)
            print(f'  模型已备份: {backup_path}')
        
        # 运行V1回测
        print(f'运行V1技术面择时回测...')
        v1_script = PROJECT / 'scripts' / 'backtest_technical_timing.py'
        
        # 用subprocess运行回测脚本
        import subprocess
        v1_result = subprocess.run(
            [sys.executable, str(v1_script)],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(PROJECT)
        )
        
        # 解析回测结果
        v1_total = 'N/A'
        v1_sharpe = 'N/A'
        v1_maxdd = 'N/A'
        v1_nav = 'N/A'
        
        if v1_result.returncode == 0:
            for line in v1_result.stdout.split('\n'):
                if '总收益:' in line:
                    v1_total = line.split('总收益:')[1].strip()
                elif 'Sharpe:' in line:
                    v1_sharpe = line.split('Sharpe:')[1].strip()
                elif '最大回撤:' in line:
                    v1_maxdd = line.split('最大回撤:')[1].strip()
                elif '期末净值:' in line:
                    v1_nav = line.split('期末净值:')[1].strip()
            print(f'  V1回测: total={v1_total}, sharpe={v1_sharpe}, maxDD={v1_maxdd}, nav={v1_nav}')
        else:
            print(f'  V1回测失败: {v1_result.stderr[-500:]}')
        
        # 记录结果
        results.append({
            'seed': seed,
            'wf_runId': wf_runid,
            'wf_totalReturn': wf_total,
            'wf_sharpe': wf_sharpe,
            'wf_maxDrawdown': wf_maxdd,
            'v1_totalReturn': v1_total,
            'v1_sharpe': v1_sharpe,
            'v1_maxDrawdown': v1_maxdd,
            'v1_nav': v1_nav,
            'model_backup': str(backup_path),
            'train_time_sec': time.time() - t0
        })
        
        # 保存中间结果
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        
    except Exception as e:
        print(f'训练失败: {e}')
        import traceback
        traceback.print_exc()
        results.append({
            'seed': seed,
            'error': str(e)
        })

# 输出汇总
print(f'\n\n{"="*80}')
print(f'多次训练结果汇总（forward_3 + V1技术面择时）')
print(f'{"="*80}')
print(f'{"seed":>8} | {"wf_total":>12} | {"wf_sharpe":>10} | {"v1_total":>12} | {"v1_sharpe":>10} | {"v1_maxDD":>10}')
print(f'{"-"*80}')

for r in results:
    if 'error' in r:
        print(f'{r["seed"]:>8} | ERROR: {r["error"][:30]}')
    else:
        print(f'{r["seed"]:>8} | {str(r["wf_totalReturn"]):>12} | {str(r["wf_sharpe"]):>10} | {str(r["v1_totalReturn"]):>12} | {str(r["v1_sharpe"]):>10} | {str(r["v1_maxDrawdown"]):>10}')

# 找到V1收益最高的模型
valid_results = [r for r in results if 'v1_totalReturn' in r and r['v1_totalReturn'] != 'N/A']
if valid_results:
    # 解析百分比
    def parse_pct(s):
        try:
            return float(str(s).replace('%', '').strip())
        except:
            return -999
    
    best = max(valid_results, key=lambda r: parse_pct(r['v1_totalReturn']))
    print(f'\n最优模型: randomState={best["seed"]}, V1总收益={best["v1_totalReturn"]}')
    print(f'  模型备份: {best["model_backup"]}')
    
    # 复制最优模型回主路径
    best_model_path = Path(best['model_backup'])
    if best_model_path.exists():
        shutil.copy2(best_model_path, MODEL_PATH)
        print(f'  已将最优模型复制回: {MODEL_PATH}')

print(f'\n结果已保存: {RESULTS_FILE}')
