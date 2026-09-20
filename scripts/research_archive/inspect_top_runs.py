import sqlite3, json

con = sqlite3.connect(r'D:\ProdProject\AI\quantification-harness\data\factors.db')
cur = con.cursor()

# 查看最牛逼的几版详细配置
top_runs = [
    'stock-wf-20260911T065736Z',  # 5种子均值 +220.78%
    'stock-wf-20260910T104245Z',  # 7窗口 forward_3 +163.24% Sharpe1.856
    'stock-wf-20260911T140458Z',  # 10种子中位数 +118.93% (当前正式)
    'stock-wf-20260911T020518Z',  # seed42单模型 +34.92%
]

for run_id in top_runs:
    cur.execute('SELECT run_id, generated_at, label, windows, config, metrics, holdings FROM stock_walkforward_runs WHERE run_id = ?', (run_id,))
    row = cur.fetchone()
    if row:
        rid, gen_at, label, windows, config_json, metrics_json, holdings_json = row
        print(f'\n{"="*80}')
        print(f'run_id: {rid}')
        print(f'时间: {gen_at}')
        print(f'标签: {label}, 窗口数: {windows}')
        print(f'{"-"*80}')
        
        # 指标
        m = json.loads(metrics_json) if metrics_json else {}
        print(f'总收益: {m.get("totalReturn", "?")*100:.2f}%' if isinstance(m.get("totalReturn"), (int,float)) else f'总收益: {m.get("totalReturn", "?")}')
        print(f'基准收益: {m.get("benchmarkReturn", "?")}')
        print(f'超额收益: {m.get("excessReturn", "?")}')
        print(f'年化收益: {m.get("annualReturn", "?")}')
        print(f'Sharpe: {m.get("sharpe", "?")}')
        print(f'最大回撤: {m.get("maxDrawdown", "?")}')
        print(f'avgRankIC: {m.get("avgRankIc", "?")}')
        
        # 配置
        print(f'\n--- 配置 ---')
        cfg = json.loads(config_json) if config_json else {}
        print(f'topN: {cfg.get("topN")}')
        print(f'rebalanceDays: {cfg.get("rebalanceDays")}')
        print(f'ensembleSeeds: {cfg.get("ensembleSeeds")}')
        print(f'ensembleMethod: {cfg.get("ensembleMethod")}')
        print(f'minTrain: {cfg.get("minTrain")}')
        print(f'testDays: {cfg.get("testDays")}')
        print(f'stepDays: {cfg.get("stepDays")}')
        print(f'regimeFilter: {cfg.get("regimeFilter")}')
        
        model_cfg = cfg.get('model', {})
        print(f'\n模型参数:')
        print(f'  nEstimators: {model_cfg.get("nEstimators")}')
        print(f'  learningRate: {model_cfg.get("learningRate")}')
        print(f'  numLeaves: {model_cfg.get("numLeaves")}')
        print(f'  featuresOverride: {len(model_cfg.get("featuresOverride", []))} 个特征')
        if model_cfg.get('featuresOverride'):
            print(f'    特征列表: {", ".join(model_cfg["featuresOverride"])}')
        
        # 最新持仓
        print(f'\n--- 最新持仓 ---')
        h = json.loads(holdings_json) if holdings_json else {}
        print(f'持仓日期: {h.get("tradeDate")}')
        holdings = h.get('holdings', [])
        for i, stock in enumerate(holdings[:10]):
            if isinstance(stock, dict):
                print(f'  {i+1}. {stock.get("code", stock.get("ts_code", "?"))} - 分数: {stock.get("score", stock.get("predictedReturn", "?"))}')
            else:
                print(f'  {i+1}. {stock}')

con.close()
