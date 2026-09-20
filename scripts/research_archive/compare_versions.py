import pandas as pd
import numpy as np
import os

PROJECT = r'D:\ProdProject\AI\quantification-harness'

versions = {
    'V1 基线(金叉+死叉+5%止损)': 'technical_timing_backtest.csv',
    'V2 放量突破买入': 'technical_timing_v2_backtest.csv',
    'V3 放量滞涨卖出': 'technical_timing_v3_backtest.csv',
    'V4 Top100+熊市空仓': 'technical_timing_v4_backtest.csv',
    'V5 移动止损': 'technical_timing_v5_backtest.csv',
    'V6 严格放量滞涨止盈': 'technical_timing_v6_backtest.csv',
    'V7 放宽放量滞涨止盈': 'technical_timing_v7_backtest.csv',
}

print(f'{"版本":<35} {"总收益":<10} {"年化":<10} {"Sharpe":<8} {"最大回撤":<10} {"交易日":<8} {"交易次数":<8}')
print('-' * 100)

results = []
for name, filename in versions.items():
    path = os.path.join(PROJECT, 'artifacts', 'stock-ml', filename)
    if not os.path.exists(path):
        print(f'{name:<35} 文件不存在')
        continue
    
    df = pd.read_csv(path)
    
    # 计算指标
    if 'nav' in df.columns:
        nav = df['nav'].values
    elif 'portfolio_value' in df.columns:
        nav = df['portfolio_value'].values / df['portfolio_value'].iloc[0]
    else:
        # 尝试找净值列
        nav_col = [c for c in df.columns if 'nav' in c.lower() or 'value' in c.lower() or 'equity' in c.lower()]
        if nav_col:
            nav = df[nav_col[0]].values
            nav = nav / nav[0]
        else:
            print(f'{name:<35} 找不到净值列: {df.columns.tolist()}')
            continue
    
    total_return = nav[-1] / nav[0] - 1
    n_days = len(nav)
    annual_return = (1 + total_return) ** (252 / n_days) - 1 if n_days > 0 else 0
    
    # 日收益率
    returns = np.diff(nav) / nav[:-1]
    sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if np.std(returns) > 0 else 0
    
    # 最大回撤
    peak = np.maximum.accumulate(nav)
    drawdown = (nav - peak) / peak
    max_drawdown = np.min(drawdown)
    
    # 交易次数
    trade_count = 0
    if 'trades' in df.columns:
        trade_count = int(df['trades'].iloc[-1]) if 'trades' in df.columns else 0
    # 从交易记录文件读取
    trade_file = filename.replace('backtest', 'trades')
    trade_path = os.path.join(PROJECT, 'artifacts', 'stock-ml', trade_file)
    if os.path.exists(trade_path):
        df_trades = pd.read_csv(trade_path)
        trade_count = len(df_trades)
    
    print(f'{name:<35} {total_return*100:>8.2f}% {annual_return*100:>8.2f}% {sharpe:>7.3f} {max_drawdown*100:>8.2f}% {n_days:>6} {trade_count:>6}')
    results.append({
        'version': name,
        'total_return': total_return,
        'annual_return': annual_return,
        'sharpe': sharpe,
        'max_drawdown': max_drawdown,
        'n_days': n_days,
        'trades': trade_count,
    })

print('\n' + '='*100)
print('结论：')
best = max(results, key=lambda x: x['total_return'])
print(f'  总收益最高: {best["version"]} ({best["total_return"]*100:.2f}%)')
best_sharpe = max(results, key=lambda x: x['sharpe'])
print(f'  Sharpe最高: {best_sharpe["version"]} ({best_sharpe["sharpe"]:.3f})')
best_mdd = min(results, key=lambda x: x['max_drawdown'])
print(f'  回撤最小: {best_mdd["version"]} ({best_mdd["max_drawdown"]*100:.2f}%)')
