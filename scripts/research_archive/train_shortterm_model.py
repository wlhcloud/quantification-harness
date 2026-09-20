"""
短线增强版模型训练脚本
独立训练，不影响现有10种子模型
配置: config/stock-ml-shortterm.yaml (37特征)
输出: artifacts/stock-ml-shortterm/
"""
import sys
import time
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(r"D:\ProdProject\AI\quantification-harness")

# 添加quant-engine源码路径
sys.path.insert(0, str(PROJECT_ROOT / "services" / "quant-engine" / "src"))

import yaml
from quant_engine.stock_ml import run_walkforward

def main():
    # 路径配置
    factors_path = PROJECT_ROOT / "data" / "factors.db"
    market_path = PROJECT_ROOT / "data" / "market.db"
    config_path = PROJECT_ROOT / "config" / "stock-ml-shortterm.yaml"
    artifact_dir = PROJECT_ROOT / "artifacts" / "stock-ml-shortterm"

    # 创建输出目录
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # 加载配置
    print(f"加载配置: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    features = cfg.get("featuresOverride", [])
    print(f"特征数: {len(features)}")
    print(f"特征列表: {', '.join(features)}")
    print()

    # 训练参数
    model_cfg = cfg.get("model", {})
    wf_cfg = cfg.get("walkforward", {})
    print(f"模型: nEstimators={model_cfg.get('nEstimators')}, "
          f"learningRate={model_cfg.get('learningRate')}, "
          f"randomStates={model_cfg.get('randomStates')}")
    print(f"Walkforward: minTrain={wf_cfg.get('minTrain')}, "
          f"stepDays={wf_cfg.get('stepDays')}, "
          f"testDays={wf_cfg.get('testDays')}, "
          f"maxWindows={wf_cfg.get('maxWindows', '全部')}")
    print()

    # 进度回调
    start_time = time.time()
    def progress(p):
        elapsed = time.time() - start_time
        print(f"  进度: {p*100:.1f}% (已用时 {elapsed/60:.1f} 分钟)")

    # 开始训练
    print("=" * 60)
    print("开始训练短线增强版模型...")
    print("=" * 60)
    print()

    result = run_walkforward(
        factors_path=factors_path,
        market_path=market_path,
        artifact_dir=artifact_dir,
        cfg=cfg,
        progress=progress,
        cancelled=lambda: False,
    )

    # 输出结果
    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print("训练完成!")
    print("=" * 60)
    print(f"耗时: {elapsed/60:.1f} 分钟")
    print(f"runId: {result.get('runId')}")
    print()

    metrics = result.get("metrics", {})
    print("=== 回测指标 ===")
    print(f"  总收益: {metrics.get('totalReturn', 0)*100:.2f}%")
    print(f"  基准收益: {metrics.get('benchmarkReturn', 0)*100:.2f}%")
    print(f"  超额收益: {metrics.get('excessReturn', 0)*100:.2f}%")
    print(f"  年化收益: {metrics.get('annualReturn', 0)*100:.2f}%")
    print(f"  Sharpe: {metrics.get('sharpe', 0):.3f}")
    print(f"  最大回撤: {metrics.get('maxDrawdown', 0)*100:.2f}%")
    print(f"  avgRankIC: {metrics.get('avgRankIc', 0):.4f}")
    print(f"  窗口数: {metrics.get('windows', 0)}")
    print()

    # 保存结果摘要
    import json
    summary = {
        "runId": result.get("runId"),
        "elapsedMinutes": elapsed / 60,
        "features": features,
        "featureCount": len(features),
        "config": str(config_path),
        "metrics": metrics,
    }
    summary_path = artifact_dir / "train_result_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"结果摘要已保存: {summary_path}")
    print()

    # 对比现有模型
    print("=== 与现有10种子模型对比 ===")
    print(f"  现有模型(10种子中位数): 总收益 +118.93%, Sharpe 0.475, 回撤 -36.67%, avgRankIC 0.0458")
    print(f"  短线增强版(单种子):      总收益 {metrics.get('totalReturn', 0)*100:.2f}%, Sharpe {metrics.get('sharpe', 0):.3f}, 回撤 {metrics.get('maxDrawdown', 0)*100:.2f}%, avgRankIC {metrics.get('avgRankIc', 0):.4f}")
    print()

    if metrics.get('totalReturn', 0) > 1.0 and metrics.get('sharpe', 0) > 0.4:
        print("✅ 短线增强版表现不错! 可以考虑训练多种子集成版本。")
    else:
        print("⚠️  短线增强版表现一般，需要调整因子或参数。")

if __name__ == "__main__":
    main()
