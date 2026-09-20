"""完整多种子集成walkforward训练：5种子[42,123,456,789,2026] x 全量24窗口
训练完成后正式模型即为集成模型（stock-wf-model.txt + 各种子模型 + 清单）。
训练前自动备份当前单模型。"""
import sys, json, time, shutil
from pathlib import Path
from datetime import datetime

PROJECT = Path(r"D:\ProdProject\AI\quantification-harness")
sys.path.insert(0, str(PROJECT / "services" / "quant-engine" / "src"))

import yaml
from quant_engine.stock_ml import run_walkforward, _load_ensemble_or_single, EnsembleRanker

with open(PROJECT / "config" / "stock-ml.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

seeds = cfg["model"].get("randomStates", [42])
print("=" * 70)
print(f"完整集成训练：{len(seeds)}种子 {seeds} x 全量窗口")
print(f"特征数: {len(cfg.get('featuresOverride', []))}, label: {cfg.get('label')}")
print("开始时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("=" * 70, flush=True)

# 训练前备份当前单模型
art = PROJECT / "artifacts" / "stock-ml"
backup_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
pre_model = art / "stock-wf-model.txt"
if pre_model.exists():
    bak = art / f"stock-wf-model_pre_ensemble_{backup_tag}.txt"
    shutil.copy(pre_model, bak)
    print(f"[备份] 当前单模型 -> {bak.name}", flush=True)

last_pct = [-1]
def prog(p):
    pct = int(p)
    if pct >= last_pct[0] + 10:
        last_pct[0] = pct
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 进度 {pct}%", flush=True)

t0 = time.time()
result = run_walkforward(
    factors_path=PROJECT / "data" / "factors.db",
    market_path=PROJECT / "data" / "market.db",
    artifact_dir=art,
    cfg=cfg,
    progress=prog,
    cancelled=lambda: False,
    parameters={},  # 全量窗口，不限制maxWindows
)
elapsed = time.time() - t0
print("\n" + "=" * 70)
print(f"训练完成，耗时 {elapsed/60:.1f} 分钟")
print("结束时间:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
print("runId:", result["runId"])
print("metrics:", json.dumps(result["metrics"], ensure_ascii=False, indent=2))
print("holdings:", json.dumps(result["holdings"], ensure_ascii=False))

# 验证集成模型
m = _load_ensemble_or_single(art / "stock-wf-model.txt")
print("\n=== 集成模型验证 ===")
print("类型:", type(m).__name__)
if isinstance(m, EnsembleRanker):
    print(f"[OK] 集成模型 n_models={m.n_models} seeds={m.seeds} 特征数={len(m.feature_name())}")
else:
    print("[WARN] 加载为单模型，特征数=", len(m.feature_name()))

# 保存结果摘要
summary = {
    "runId": result["runId"],
    "seeds": seeds,
    "elapsedMin": round(elapsed / 60, 1),
    "metrics": result["metrics"],
    "holdings": result["holdings"],
    "finishedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
}
(art / "ensemble_train_result.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
print("\n结果摘要已保存: artifacts/stock-ml/ensemble_train_result.json")
