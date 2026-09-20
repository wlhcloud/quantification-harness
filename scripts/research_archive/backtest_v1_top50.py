"""
V1 Top50候选池回测：ML Top50候选 + 金叉死叉 + 5%止损
对比Top20，看看扩大候选池后的收益和稳定性
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = r'D:\ProdProject\AI\quantification-harness'
TOP_N = 50  # 候选池大小

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

df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20250101"',
    fdb
)

df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20250101"',
    fdb
)

df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')

# ========== 3. 读取行情数据，计算均线 ==========
print('读取行情数据并计算均线...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, close, volume, amount, high, low, open FROM daily_bars WHERE trade_date >= "20250101" ORDER BY code, trade_date',
    mdb
)

df_bars = df_bars.sort_values(['code', 'trade_date'])
df_bars['ma5'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
df_bars['ma10'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
df_bars['ma20'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
df_bars['ma5_prev'] = df_bars.groupby('code')['ma5'].shift(1)
df_bars['ma10_prev'] = df_bars.groupby('code')['ma10'].shift(1)
df_bars['golden_cross'] = (df_bars['ma5_prev'] <= df_bars['ma10_prev']) & (df_bars['ma5'] > df_bars['ma10'])
df_bars['death_cross'] = (df_bars['ma5_prev'] >= df_bars['ma10_prev']) & (df_bars['ma5'] < df_bars['ma10'])

# ========== 4. 合并因子和行情 ==========
df = df.merge(df_bars[['trade_date', 'code', 'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross', 'volume', 'high', 'low', 'open']],
               on=['trade_date', 'code'], how='left')

df = df[df['trade_date'] >= '20250601']
df = df[df['trade_date'] <= '20260831']

# ========== 5. 模型打分，选TopN候选 ==========
print(f'模型打分，选Top{TOP_N}候选...')
X = df[feature_names].values
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)

df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= TOP_N].copy()
df_candidates = df_candidates[~df_candidates['code'].str.startswith(('920', '688'))]
df_candidates = df_candidates[df_candidates['mktCap'] >= 200000]
print(f'  过滤后候选: {len(df_candidates)} 行')

# ========== 6. V1回测模拟 ==========
print(f'开始V1回测模拟（Top{TOP_N}候选）...')

INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
STOP_LOSS = 0.05
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001

dates = sorted(df_candidates['trade_date'].unique())
holdings = {}
cash = INITIAL_CAPITAL
trades = []
portfolio_daily = []

for i, date in enumerate(dates):
    day_candidates = df_candidates[df_candidates['trade_date'] == date].copy()
    
    # 1. 检查卖出信号
    codes_to_sell = []
    for code, pos in holdings.items():
        stock_data = day_candidates[day_candidates['code'] == code]
        if len(stock_data) == 0:
            stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        
        if len(stock_data) > 0:
            row = stock_data.iloc[0]
            current_price = row['close']
            buy_price = pos['buy_price']
            
            if current_price < buy_price * (1 - STOP_LOSS):
                codes_to_sell.append((code, 'stop_loss', current_price))
            elif pd.notna(row.get('death_cross')) and row['death_cross']:
                codes_to_sell.append((code, 'death_cross', current_price))
    
    # 执行卖出
    for code, reason, sell_price_raw in codes_to_sell:
        pos = holdings[code]
        sell_price = sell_price_raw * (1 - SLIPPAGE)
        proceeds = pos['shares'] * sell_price
        proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
        cash += proceeds
        
        pnl = proceeds - pos['cost']
        pnl_pct = pnl / pos['cost']
        hold_days = (pd.to_datetime(date) - pd.to_datetime(pos['buy_date'])).days
        trades.append({
            'code': code,
            'buy_date': pos['buy_date'],
            'buy_price': pos['buy_price'],
            'sell_date': date,
            'sell_price': sell_price,
            'shares': pos['shares'],
            'cost': pos['cost'],
            'proceeds': proceeds,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'hold_days': hold_days,
            'sell_reason': reason
        })
        del holdings[code]
    
    # 2. 检查买入信号
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
                
                holdings_value = sum(p['shares'] * df[(df['trade_date'] == date) & (df['code'] == c)]['close'].values[0]
                              for c, p in holdings.items() if len(df[(df['trade_date'] == date) & (df['code'] == c)]) > 0)
                target_value = (cash + holdings_value) / MAX_HOLDINGS
                buy_price = row['close'] * (1 + SLIPPAGE)
                shares = int(target_value / buy_price / 100) * 100
                
                if shares > 0 and shares * buy_price <= cash:
                    cost = shares * buy_price
                    cost += cost * COMMISSION
                    cash -= cost
                    holdings[code] = {
                        'shares': shares,
                        'buy_price': buy_price,
                        'buy_date': date,
                        'cost': cost
                    }
    
    # 3. 计算当日净值
    holdings_value = 0
    for code, pos in holdings.items():
        stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        if len(stock_data) > 0:
            holdings_value += pos['shares'] * stock_data.iloc[0]['close']
    
    total_value = cash + holdings_value
    portfolio_daily.append({'date': date, 'value': total_value, 'cash': cash, 'holdings_count': len(holdings)})

# ========== 7. 回测结果分析 ==========
print('\n' + '='*70)
print(f'V1 Top{TOP_N}候选池 回测结果')
print('='*70)

df_trades = pd.DataFrame(trades)
df_portfolio = pd.DataFrame(portfolio_daily)

total_return = df_portfolio['value'].iloc[-1] / INITIAL_CAPITAL - 1
print(f'\n总收益: {total_return:.2%}')
print(f'期末净值: {df_portfolio["value"].iloc[-1]:.0f}元 (初始{INITIAL_CAPITAL}元)')
print(f'总交易次数: {len(df_trades)}')

if len(df_trades) > 0:
    win_trades = df_trades[df_trades['pnl'] > 0]
    lose_trades = df_trades[df_trades['pnl'] <= 0]
    win_rate = len(win_trades) / len(df_trades)
    print(f'胜率: {win_rate:.2%} ({len(win_trades)}胜 / {len(lose_trades)}负)')
    
    avg_win = win_trades['pnl'].mean() if len(win_trades) > 0 else 0
    avg_lose = abs(lose_trades['pnl'].mean()) if len(lose_trades) > 0 else 0
    profit_loss_ratio = avg_win / avg_lose if avg_lose > 0 else float('inf')
    print(f'平均盈利: {avg_win:.0f}元, 平均亏损: {avg_lose:.0f}元, 盈亏比: {profit_loss_ratio:.2f}')
    
    # 利润集中度
    df_trades_sorted = df_trades.sort_values('pnl', ascending=False)
    total_pnl = df_trades['pnl'].sum()
    
    print(f'\n--- 利润集中度分析 ---')
    for top_n in [1, 3, 5, 10, 20]:
        if len(df_trades_sorted) >= top_n:
            top_pnl = df_trades_sorted.head(top_n)['pnl'].sum()
            pct = top_pnl / total_pnl if total_pnl != 0 else 0
            print(f'  前{top_n}笔交易贡献: {top_pnl:.0f}元 ({pct:.1%} of 总盈亏)')
    
    # 卖出原因分布
    print(f'\n--- 卖出原因分布 ---')
    reason_dist = df_trades['sell_reason'].value_counts()
    for reason, count in reason_dist.items():
        reason_pnl = df_trades[df_trades['sell_reason'] == reason]['pnl'].sum()
        print(f'  {reason}: {count}次, 总盈亏{reason_pnl:.0f}元')
    
    # 分年度收益
    print(f'\n--- 分年度收益 ---')
    df_portfolio['year'] = df_portfolio['date'].str[:4]
    for year in sorted(df_portfolio['year'].unique()):
        year_data = df_portfolio[df_portfolio['year'] == year]
        year_return = year_data['value'].iloc[-1] / year_data['value'].iloc[0] - 1
        print(f'  {year}: {year_return:.2%}')

fdb.close()
mdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
