"""快速验证多种子集成walkforward：只跑1个窗口、2个种子，验证训练/回测/保存/加载全链路
使用独立临时artifact目录，不污染正式模型。"""
import sys, json, time, shutil
from pathlib import Path

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
sys.path.insert(0, str(PROJECT / "services" / "quant-engine" / "src"))

import yaml
from quant_engine.stock_ml import run_walkforward, _load_ensemble_or_single, EnsembleRanker

with open(PROJECT / "config" / "stock-ml.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

cfg["model"]["randomStates"] = [42, 123]  # 只测2个种子
parameters = {"maxWindows": 1}

# 独立临时目录，不污染正式模型
TEST_DIR = PROJECT / "artifacts" / "stock-ml" / "_ensemble_test"
if TEST_DIR.exists():
    shutil.rmtree(TEST_DIR)
TEST_DIR.mkdir(parents=True, exist_ok=True)

print("=== 快速集成测试：2种子 x 1窗口（独立临时目录）===")
print("featuresOverride 特征数:", len(cfg.get("featuresOverride", [])))
t0 = time.time()
result = run_walkforward(
    factors_path=PROJECT / "data" / "factors.db",
    market_path=PROJECT / "data" / "market.db",
    artifact_dir=TEST_DIR,
    cfg=cfg,
    progress=lambda p: None,
    cancelled=lambda: False,
    parameters=parameters,
)
print(f"\n训练+回测耗时: {time.time()-t0:.1f}s")
print("runId:", result["runId"])
print("metrics:", json.dumps(result["metrics"], ensure_ascii=False))
print("holdings:", json.dumps(result["holdings"], ensure_ascii=False))

print("\n=== 验证集成模型文件 ===")
manifest = TEST_DIR / "stock-wf-model_ensemble_seeds.json"
if manifest.exists():
    meta = json.loads(manifest.read_text(encoding="utf-8"))
    print("集成清单: seeds=", meta["seeds"], "method=", meta["ensemble"], "文件数=", len(meta["modelFiles"]))
else:
    print("[FAIL] 集成清单不存在")

m = _load_ensemble_or_single(TEST_DIR / "stock-wf-model.txt")
print("加载类型:", type(m).__name__)
if isinstance(m, EnsembleRanker):
    print("[OK] 集成模型 n_models=", m.n_models, "seeds=", m.seeds)
    print("[OK] 特征数=", len(m.feature_name()), "前3=", m.feature_name()[:3])
    import numpy as np
    x = np.random.rand(8, len(m.feature_name()))
    p = m.predict(x)
    print("[OK] 集成预测形状=", p.shape)
else:
    print("[FAIL] 加载的是单模型，不是集成模型；特征数=", len(m.feature_name()))

# 清理临时目录（注释掉以便排查错误）
# shutil.rmtree(TEST_DIR)
print("\n[保留] 临时测试目录:", TEST_DIR)
