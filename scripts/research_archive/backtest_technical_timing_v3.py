"""
技术面择时回测V3：多因子Top20候选 + 金叉买入 + 死叉/止损/放量滞涨卖出
策略逻辑：
1. 用已训练ML模型对每个交易日股票打分，选Top20候选
2. 买入信号：MA5上穿MA10（金叉）+ 收盘价 > MA20（趋势向上）
3. 卖出信号（满足任一即可）：
   a. 死叉：MA5下穿MA10
   b. 止损：亏损达5%
   c. 放量滞涨：成交量 > 5日均量2倍 + (股价涨幅 < 1% 或 收阴线)
4. 每天检查信号，最多持有5只，等权分配
回测期：2025-06-01 ~ 2026-08-31
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = 'D:\\ProdProject\\AI\\quantification-harness'

# ========== 1. 加载模型 ==========
print('加载模型...')
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()
print(f'  特征数: {len(feature_names)}')

# ========== 2. 读取因子数据 ==========
print('读取因子数据...')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')
mdb = sqlite3.connect(f'{PROJECT}/data/market.db')

df_factors = pd.read_sql_query(
    'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    'FROM stock_ml_factors WHERE trade_date >= "20250101"',
    fdb
)
print(f'  基础因子: {len(df_factors)} 行')

df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20250101"',
    fdb
)
print(f'  行业因子: {len(df_industry)} 行')

df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20250101"',
    fdb
)
print(f'  资金因子: {len(df_money)} 行')

df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')
print(f'  合并后: {len(df)} 行, 耗时 {time.time()-t0:.1f}s')

# ========== 3. 读取行情数据，计算均线和放量滞涨指标 ==========
print('读取行情数据并计算均线...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars WHERE trade_date >= "20250101" ORDER BY code, trade_date',
    mdb
)
print(f'  行情数据: {len(df_bars)} 行')

df_bars = df_bars.sort_values(['code', 'trade_date'])
df_bars['ma5'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
df_bars['ma10'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
df_bars['ma20'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
df_bars['vol_ma5'] = df_bars.groupby('code')['volume'].transform(lambda x: x.rolling(5).mean())

# 前一日收盘价和涨跌幅
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)
df_bars['daily_return'] = (df_bars['close'] - df_bars['prev_close']) / df_bars['prev_close']

# 金叉死叉
df_bars['ma5_prev'] = df_bars.groupby('code')['ma5'].shift(1)
df_bars['ma10_prev'] = df_bars.groupby('code')['ma10'].shift(1)
df_bars['golden_cross'] = (df_bars['ma5_prev'] <= df_bars['ma10_prev']) & (df_bars['ma5'] > df_bars['ma10'])
df_bars['death_cross'] = (df_bars['ma5_prev'] >= df_bars['ma10_prev']) & (df_bars['ma5'] < df_bars['ma10'])

# 放量滞涨：成交量 > 5日均量2倍 + (股价涨幅 < 1% 或 收阴线)
df_bars['volume_surge'] = df_bars['volume'] > df_bars['vol_ma5'] * 2
df_bars['price_stagnant'] = (df_bars['daily_return'] < 0.01) | (df_bars['close'] < df_bars['open'])
df_bars['volume_stagnation'] = df_bars['volume_surge'] & df_bars['price_stagnant']

print(f'  均线和放量滞涨指标计算完成, 耗时 {time.time()-t0:.1f}s')

# ========== 4. 合并因子和行情 ==========
print('合并因子和行情...')
df = df.merge(df_bars[['trade_date', 'code', 'ma5', 'ma10', 'ma20', 'vol_ma5',
                         'golden_cross', 'death_cross', 'volume', 'volume_stagnation',
                         'daily_return', 'open', 'prev_close']],
               on=['trade_date', 'code'], how='left')
print(f'  合并后: {len(df)} 行')

df = df[df['trade_date'] >= '20250601']
df = df[df['trade_date'] <= '20260831']
print(f'  回测期: {len(df)} 行, {df["trade_date"].nunique()} 个交易日')

# ========== 5. 模型打分，选Top20候选 ==========
print('模型打分...')
X = df[feature_names].values
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)
print(f'  打分完成, 耗时 {time.time()-t0:.1f}s')

df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= 20].copy()
print(f'  Top20候选: {len(df_candidates)} 行')

df_candidates = df_candidates[~df_candidates['code'].str.startswith(('920', '688'))]
df_candidates = df_candidates[df_candidates['mktCap'] >= 200000]
print(f'  过滤后候选: {len(df_candidates)} 行')

# 统计信号
golden_count = df_candidates['golden_cross'].sum()
stagnation_count = df_candidates['volume_stagnation'].sum()
print(f'  金叉信号: {golden_count} 次, 放量滞涨信号: {stagnation_count} 次')

# ========== 6. 回测模拟 ==========
print('开始回测模拟...')

INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
STOP_LOSS = 0.05
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001

dates = sorted(df_candidates['trade_date'].unique())
print(f'  回测交易日: {len(dates)} 天')

holdings = {}
cash = INITIAL_CAPITAL
portfolio_values = []
trade_log = []
sell_reason_count = {'death_cross': 0, 'stop_loss': 0, 'volume_stagnation': 0}

for i, date in enumerate(dates):
    day_candidates = df_candidates[df_candidates['trade_date'] == date].copy()
    
    # 1. 检查卖出信号（死叉/止损/放量滞涨）
    codes_to_sell = []
    for code, pos in holdings.items():
        stock_data = day_candidates[day_candidates['code'] == code]
        if len(stock_data) == 0:
            stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        
        if len(stock_data) > 0:
            row = stock_data.iloc[0]
            current_price = row['close']
            buy_price = pos['buy_price']
            
            # 止损
            if current_price < buy_price * (1 - STOP_LOSS):
                codes_to_sell.append((code, 'stop_loss'))
            # 死叉
            elif pd.notna(row.get('death_cross')) and row['death_cross']:
                codes_to_sell.append((code, 'death_cross'))
            # 放量滞涨
            elif pd.notna(row.get('volume_stagnation')) and row['volume_stagnation']:
                codes_to_sell.append((code, 'volume_stagnation'))
    
    for code, reason in codes_to_sell:
        pos = holdings[code]
        stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        if len(stock_data) > 0:
            sell_price = stock_data.iloc[0]['close'] * (1 - SLIPPAGE)
            proceeds = pos['shares'] * sell_price
            proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
            cash += proceeds
            sell_reason_count[reason] = sell_reason_count.get(reason, 0) + 1
            trade_log.append({'date': date, 'code': code, 'action': 'sell', 'price': sell_price,
                             'shares': pos['shares'], 'reason': reason})
            del holdings[code]
    
    # 2. 检查买入信号（金叉 + 收盘价 > MA20）
    if len(holdings) < MAX_HOLDINGS:
        buy_candidates = day_candidates.sort_values('score', ascending=False)
        for _, row in buy_candidates.iterrows():
            if len(holdings) >= MAX_HOLDINGS:
                break
            code = row['code']
            if code in holdings:
                continue
            
            if (pd.notna(row.get('golden_cross')) and row['golden_cross'] and
                pd.notna(row.get('ma20')) and row['close'] > row['ma20']):
                
                total_holdings_value = sum(
                    p['shares'] * df[(df['trade_date'] == date) & (df['code'] == c)]['close'].values[0]
                    for c, p in holdings.items()
                    if len(df[(df['trade_date'] == date) & (df['code'] == c)]) > 0
                )
                target_value = (cash + total_holdings_value) / MAX_HOLDINGS
                buy_price = row['close'] * (1 + SLIPPAGE)
                shares = int(target_value / buy_price / 100) * 100
                
                if shares > 0 and shares * buy_price <= cash:
                    cost = shares * buy_price
                    cost += cost * COMMISSION
                    cash -= cost
                    holdings[code] = {'shares': shares, 'buy_price': buy_price, 'buy_date': date}
                    trade_log.append({'date': date, 'code': code, 'action': 'buy', 'price': buy_price,
                                     'shares': shares, 'signal': 'golden_cross'})
    
    # 3. 计算当日净值
    holdings_value = 0
    for code, pos in holdings.items():
        stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        if len(stock_data) > 0:
            holdings_value += pos['shares'] * stock_data.iloc[0]['close']
    
    total_value = cash + holdings_value
    portfolio_values.append((date, total_value, cash, len(holdings)))
    
    if (i + 1) % 50 == 0:
        print(f'  进度: {i+1}/{len(dates)}, 净值: {total_value:.0f}, 持仓: {len(holdings)}只')

# ========== 7. 计算回测指标 ==========
print('\n计算回测指标...')
df_result = pd.DataFrame(portfolio_values, columns=['date', 'value', 'cash', 'holdings'])
df_result['return'] = df_result['value'].pct_change()
df_result['cum_return'] = df_result['value'] / INITIAL_CAPITAL - 1

total_return = df_result['cum_return'].iloc[-1]
annual_return = (1 + total_return) ** (252 / len(df_result)) - 1
sharpe = df_result['return'].mean() / df_result['return'].std() * np.sqrt(252) if df_result['return'].std() > 0 else 0
max_drawdown = (df_result['value'] / df_result['value'].cummax() - 1).min()

# 基准
df_hs300 = pd.read_sql_query(
    'SELECT trade_date, close FROM index_daily_bars WHERE ts_code="000300.SH" AND trade_date >= "20250601" AND trade_date <= "20260831" ORDER BY trade_date',
    mdb
)
benchmark_return = df_hs300['close'].iloc[-1] / df_hs300['close'].iloc[0] - 1 if len(df_hs300) > 0 else 0
excess_return = total_return - benchmark_return

# 交易统计
df_trades = pd.DataFrame(trade_log)
buy_count = len(df_trades[df_trades['action'] == 'buy']) if len(df_trades) > 0 else 0
sell_count = len(df_trades[df_trades['action'] == 'sell']) if len(df_trades) > 0 else 0

# 保存结果
df_result.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v3_backtest.csv', index=False)
df_trades.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v3_trades.csv', index=False)

print(f'\n{"="*60}')
print(f'技术面择时V3回测结果（金叉买入 + 死叉/止损/放量滞涨卖出，2025-06 ~ 2026-08）')
print(f'{"="*60}')
print(f'总收益:     {total_return:.2%}')
print(f'年化收益:   {annual_return:.2%}')
print(f'超额收益:   {excess_return:.2%} (基准沪深300: {benchmark_return:.2%})')
print(f'Sharpe:     {sharpe:.3f}')
print(f'最大回撤:   {max_drawdown:.2%}')
print(f'交易日数:   {len(df_result)}')
print(f'期末持仓:   {len(holdings)}只')
print(f'期末净值:   {df_result["value"].iloc[-1]:.0f}')
print(f'买入次数:   {buy_count}')
print(f'卖出次数:   {sell_count}')
print(f'  死叉卖出:   {sell_reason_count.get("death_cross", 0)}次')
print(f'  止损卖出:   {sell_reason_count.get("stop_loss", 0)}次')
print(f'  放量滞涨卖出: {sell_reason_count.get("volume_stagnation", 0)}次')
print(f'总耗时:     {time.time()-t0:.1f}s')
print(f'\n结果已保存: artifacts/stock-ml/technical_timing_v3_backtest.csv')
print(f'交易记录:   artifacts/stock-ml/technical_timing_v3_trades.csv')

fdb.close()
mdb.close()
