"""
V7 策略回测：持有3天 + 移动止盈 + 硬止损 + 时间止损
策略逻辑：
1. 用ML模型对每个交易日股票打分，选Top20候选
2. 买入信号：MA5上穿MA10（金叉）+ 收盘价 > MA20（趋势向上）
3. 卖出信号（满足任一即可）：
   a. 移动止盈：盈利后从持仓最高价回撤8%卖出
   b. 硬止损：亏损达5%无条件卖出
   c. 时间止损：持有满3个交易日还没达到止盈，卖出
   d. 死叉：MA5下穿MA10卖出
4. 每天检查信号，最多持有5只，等权分配
5. 考虑涨跌停限制：涨停买不进，跌停卖不出
回测期：2025-06-01 ~ 2026-08-31
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = r'D:\ProdProject\AI\quantification-harness'

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
    'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars WHERE trade_date >= "20250101" ORDER BY code, trade_date',
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
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)

# ========== 4. 合并因子和行情 ==========
df = df.merge(df_bars[['trade_date', 'code', 'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross',
                         'open', 'high', 'low', 'close', 'volume', 'prev_close']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))

df = df[df['trade_date'] >= '20250601']
df = df[df['trade_date'] <= '20260831']

# ========== 5. 模型打分，选Top20候选 ==========
print('模型打分...')
X = df[feature_names].values
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)

df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= 20].copy()
df_candidates = df_candidates[~df_candidates['code'].str.startswith(('920', '688'))]
df_candidates = df_candidates[df_candidates['mktCap'] >= 200000]
print(f'  过滤后候选: {len(df_candidates)} 行')

# ========== 6. V7回测模拟 ==========
print('开始V7回测模拟...')

INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
HARD_STOP_LOSS = 0.05  # 硬止损5%
TRAILING_DRAWDOWN = 0.08  # 移动止盈：从最高点回撤8%
MAX_HOLD_DAYS = 3  # 最多持有3个交易日
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001
LIMIT_UP = 0.099
LIMIT_DOWN = -0.099

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
            open_price = row['open']
            high_price = row['high']
            low_price = row['low']
            buy_price = pos['buy_price']
            prev_close = row['prev_close']
            
            # 更新持仓期间最高价（用当天的high）
            if high_price > pos['highest_price']:
                pos['highest_price'] = high_price
            
            # 计算持有天数（交易日数）
            hold_days = pos['hold_days'] + 1
            
            # 判断是否跌停（开盘跌停卖不出去）
            limit_down_today = False
            if pd.notna(prev_close) and prev_close > 0:
                open_pct = (open_price - prev_close) / prev_close
                if open_pct <= LIMIT_DOWN:
                    limit_down_today = True
            
            # 卖出条件判断
            sell_reason = None
            sell_price = None
            
            # a. 硬止损：亏损达5%（用当天最低价判断是否触发止损）
            if low_price < buy_price * (1 - HARD_STOP_LOSS):
                # 止损价 = buy_price * 0.95
                stop_price = buy_price * (1 - HARD_STOP_LOSS)
                if not limit_down_today or low_price > stop_price:
                    # 如果不是跌停，或者跌停价高于止损价，可以按止损价卖出
                    sell_reason = 'hard_stop_loss'
                    sell_price = min(stop_price, open_price) * (1 - SLIPPAGE)  # 假设按止损价或开盘价卖出
            
            # b. 移动止盈：盈利后从最高点回撤8%
            if sell_reason is None and pos['highest_price'] > buy_price * (1 + 0.01):
                trailing_stop_price = pos['highest_price'] * (1 - TRAILING_DRAWDOWN)
                if low_price < trailing_stop_price and not limit_down_today:
                    sell_reason = 'trailing_stop'
                    sell_price = trailing_stop_price * (1 - SLIPPAGE)
            
            # c. 时间止损：持有满3天
            if sell_reason is None and hold_days >= MAX_HOLD_DAYS:
                if not limit_down_today:
                    sell_reason = 'time_stop'
                    sell_price = current_price * (1 - SLIPPAGE)
            
            # d. 死叉
            if sell_reason is None and pd.notna(row.get('death_cross')) and row['death_cross']:
                if not limit_down_today:
                    sell_reason = 'death_cross'
                    sell_price = current_price * (1 - SLIPPAGE)
            
            if sell_reason is not None and sell_price is not None:
                codes_to_sell.append((code, sell_reason, sell_price))
            else:
                # 没卖出，更新持有天数
                pos['hold_days'] = hold_days
    
    # 执行卖出
    for code, reason, sell_price in codes_to_sell:
        pos = holdings[code]
        proceeds = pos['shares'] * sell_price
        proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
        cash += proceeds
        
        pnl = proceeds - pos['cost']
        pnl_pct = pnl / pos['cost']
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
            'hold_days': pos['hold_days'],
            'sell_reason': reason,
            'highest_price': pos['highest_price']
        })
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
            
            close_price = row['close']
            prev_close = row['prev_close']
            
            # 判断是否涨停（涨停买不进）
            if pd.notna(prev_close) and prev_close > 0:
                close_pct = (close_price - prev_close) / prev_close
                if close_pct >= LIMIT_UP:
                    continue
            
            if (pd.notna(row.get('golden_cross')) and row['golden_cross'] and
                pd.notna(row.get('ma20')) and close_price > row['ma20']):
                
                holdings_value = sum(p['shares'] * df[(df['trade_date'] == date) & (df['code'] == c)]['close'].values[0]
                              for c, p in holdings.items() if len(df[(df['trade_date'] == date) & (df['code'] == c)]) > 0)
                target_value = (cash + holdings_value) / MAX_HOLDINGS
                buy_price = close_price * (1 + SLIPPAGE)
                shares = int(target_value / buy_price / 100) * 100
                
                if shares > 0 and shares * buy_price <= cash:
                    cost = shares * buy_price
                    cost += cost * COMMISSION
                    cash -= cost
                    holdings[code] = {
                        'shares': shares,
                        'buy_price': buy_price,
                        'buy_date': date,
                        'cost': cost,
                        'highest_price': buy_price,
                        'hold_days': 0
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
print('V7 策略回测结果（持有3天 + 移动止盈 + 硬止损 + 时间止损）')
print('='*70)

df_trades = pd.DataFrame(trades)
df_portfolio = pd.DataFrame(portfolio_daily)

total_return = df_portfolio['value'].iloc[-1] / INITIAL_CAPITAL - 1
print(f'\n总收益: {total_return:.2%}')
print(f'期末净值: {df_portfolio["value"].iloc[-1]:.0f}元 (初始{INITIAL_CAPITAL}元)')
print(f'总交易次数: {len(df_trades)}')

if len(df_trades) > 0:
    # 胜率
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
    
    # 最赚钱的股票
    print(f'\n--- 最赚钱的10只股票 ---')
    stock_pnl = df_trades.groupby('code')['pnl'].agg(['sum', 'count', 'mean']).sort_values('sum', ascending=False)
    for code, row in stock_pnl.head(10).iterrows():
        print(f'  {code}: 总盈亏{row["sum"]:.0f}元, 交易{row["count"]}次')
    
    # 卖出原因分布
    print(f'\n--- 卖出原因分布 ---')
    reason_dist = df_trades['sell_reason'].value_counts()
    for reason, count in reason_dist.items():
        reason_pnl = df_trades[df_trades['sell_reason'] == reason]['pnl'].sum()
        print(f'  {reason}: {count}次, 总盈亏{reason_pnl:.0f}元')
    
    # 持仓天数分布
    print(f'\n--- 持仓天数分布 ---')
    print(f'  平均持仓: {df_trades["hold_days"].mean():.1f}天')
    print(f'  中位数持仓: {df_trades["hold_days"].median():.1f}天')
    
    # 分年度收益
    print(f'\n--- 分年度收益 ---')
    df_portfolio['year'] = df_portfolio['date'].str[:4]
    for year in sorted(df_portfolio['year'].unique()):
        year_data = df_portfolio[df_portfolio['year'] == year]
        year_return = year_data['value'].iloc[-1] / year_data['value'].iloc[0] - 1
        print(f'  {year}: {year_return:.2%}')
    
    # 分季度收益
    print(f'\n--- 分季度收益 ---')
    df_portfolio['quarter'] = df_portfolio['date'].str[:4] + 'Q' + ((df_portfolio['date'].str[4:6].astype(int) - 1) // 3 + 1).astype(str)
    for q in sorted(df_portfolio['quarter'].unique()):
        q_data = df_portfolio[df_portfolio['quarter'] == q]
        q_return = q_data['value'].iloc[-1] / q_data['value'].iloc[0] - 1
        print(f'  {q}: {q_return:.2%}')

# 保存交易记录
df_trades.to_csv(f'{PROJECT}/artifacts/stock-ml/v7_trade_analysis.csv', index=False)
print(f'\n交易记录已保存: artifacts/stock-ml/v7_trade_analysis.csv')

fdb.close()
mdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
