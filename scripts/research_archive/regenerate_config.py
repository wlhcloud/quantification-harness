"""
重新生成干净的stock-ml.yaml配置文件，避免编码问题
"""
from pathlib import Path
import yaml

PROJECT = Path(r'D:\ProdProject\AI\quantification-harness')
CONFIG_PATH = PROJECT / 'config' / 'stock-ml.yaml'

# 定义配置
cfg = {
    'label': 'forward_3',
    'model': {
        'validationDays': 40,
        'testDays': 40,
        'nEstimators': 300,
        'learningRate': 0.03,
        'numLeaves': 31,
        'minDataInLeaf': 20,
        'labelGrades': 10,
        'labelRelative': True,
    },
    'featuresOverride': [
        # 动量/趋势
        'momentum20', 'momentum60', 'trend', 'drawdown60',
        # 量价/资金活跃度
        'volumeRatio', 'volSurge3', 'volSurge5', 'amountRatio5',
        'volPriceCorr20', 'turnoverRatio5', 'amplitude20',
        # 估值
        'peTtm', 'pb', 'psTtm', 'dvTtm',
        # 质量/成长
        'roe', 'grossMargin', 'netMargin', 'revenueYoy', 'netprofitYoy', 'ocfYoy',
        # 行业轮动
        'industry_mom20', 'industry_rank20', 'industry_excess5', 'industry_excess20', 'industry_amount_chg',
        # 资金流向
        'turnover_surge', 'close_position_5d',
        # 热点因子
        'rank_ret5', 'rank_ret10', 'rank_ret20',
        'rank_amount5', 'rank_amount10',
        'up_days', 'up_count5',
        'limit_up_count5', 'limit_up_count10',
        'relative_strength5', 'relative_strength10', 'relative_strength20',
        'rank_relative_strength5', 'rank_relative_strength10',
        'industry_rank_ret5', 'industry_rank_ret10', 'industry_rank_ret20',
        # 其他
        'mktCap', 'volatility20', 'floatRatio',
    ],
    'backtest': {
        'topN': 5,
        'rebalanceDays': 3,
        'commissionRate': 0.0003,
        'stampDutyRate': 0.0005,
        'slippageRate': 0.001,
        'initialCapital': 1000000,
        'regimeFilter': True,
        'excludeCodePrefix': ['920', '688'],
        'minMktCap': 200000,
        'requireMomentum20Positive': False,
        'minIndustryRank20': 0.7,
    },
    'walkforward': {
        'minTrain': 200,
        'validDays': 40,
        'testDays': 40,
        'stepDays': 40,
        'sampleRows': 250000,
    },
}

# 保存配置
with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
    yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

print(f'配置已保存: {CONFIG_PATH}')

# 验证
with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
    cfg2 = yaml.safe_load(f)

features = cfg2.get('featuresOverride', [])
print(f'\n验证成功!')
print(f'  label: {cfg2.get("label")}')
print(f'  特征总数: {len(features)}')
print(f'  基础因子: {len([f for f in features if f not in ["rank_ret5","rank_ret10","rank_ret20","rank_amount5","rank_amount10","up_days","up_count5","limit_up_count5","limit_up_count10","relative_strength5","relative_strength10","relative_strength20","rank_relative_strength5","rank_relative_strength10","industry_rank_ret5","industry_rank_ret10","industry_rank_ret20"]])}')
print(f'  热点因子: {len([f for f in features if f in ["rank_ret5","rank_ret10","rank_ret20","rank_amount5","rank_amount10","up_days","up_count5","limit_up_count5","limit_up_count10","relative_strength5","relative_strength10","relative_strength20","rank_relative_strength5","rank_relative_strength10","industry_rank_ret5","industry_rank_ret10","industry_rank_ret20"]])}')
print(f'  backtest.topN: {cfg2["backtest"]["topN"]}')
print(f'  backtest.rebalanceDays: {cfg2["backtest"]["rebalanceDays"]}')
