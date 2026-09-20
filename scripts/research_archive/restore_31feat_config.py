"""
恢复31特征的配置文件（不含热点因子）
"""
import yaml
from pathlib import Path

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'

# 31个基础特征（和seed=42神级模型一致）
FEATURES = [
    "momentum20", "momentum60", "trend", "drawdown60",
    "volumeRatio", "volSurge3", "volSurge5", "amountRatio5",
    "volPriceCorr20", "turnoverRatio5", "amplitude20",
    "peTtm", "pb", "psTtm", "dvTtm",
    "roe", "grossMargin", "netMargin", "revenueYoy", "netprofitYoy", "ocfYoy",
    "industry_mom20", "industry_rank20", "industry_excess5", "industry_excess20", "industry_amount_chg",
    "turnover_surge", "close_position_5d",
    "mktCap", "volatility20", "floatRatio",
]

config = {
    "label": "forward_3",
    "featuresOverride": FEATURES,
    "model": {
        "nEstimators": 300,
        "learningRate": 0.03,
        "numLeaves": 31,
        "minDataInLeaf": 20,
        "subsample": 0.8,
        "colsampleBytree": 0.8,
        "randomState": 42,
        "earlyStopping": 30,
        "validationDays": 40,
        "testDays": 40,
        "labelGrades": 10,
    },
    "walkforward": {
        "minTrainDays": 120,
        "stepDays": 20,
        "testDays": 40,
    },
    "backtest": {
        "topN": 5,
        "rebalanceDays": 3,
        "transactionCostBps": 15,
        "slippageBps": 5,
    },
    "universe": {
        "minMarketCap": 200000,
        "maxMarketCap": 5000000,
        "excludeST": True,
        "excludeSuspended": True,
        "minListDays": 60,
    },
}

with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
    yaml.dump(config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f'配置已恢复: {CONFIG_PATH}')
print(f'  label: {config["label"]}')
print(f'  特征总数: {len(FEATURES)}')
print(f'  backtest.topN: {config["backtest"]["topN"]}')
print(f'  backtest.rebalanceDays: {config["backtest"]["rebalanceDays"]}')

# 验证
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    loaded = yaml.safe_load(f)
print(f'\n验证成功! 特征数: {len(loaded["featuresOverride"])}')
