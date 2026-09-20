"""
V6 隔日卖出策略回测（T+1超短线）
策略逻辑：
1. 每天用ML模型对股票打分，选Top20候选
2. 买入信号：当天收盘价买入（模拟下午2:30买入）
3. 卖出信号：第二天开盘价卖出（最多持有1天）
4. 每天检查，最多持有5只，等权分配
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

# ========== 3. 读取行情数据 ==========
print('读取行情数据...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars WHERE trade_date >= "20250101" ORDER BY code, trade_date',
    mdb
)

# 计算前一天收盘价（用于判断涨跌停）
df_bars = df_bars.sort_values(['code', 'trade_date'])
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)

# 合并因子和行情
df = df.merge(df_bars[['trade_date', 'code', 'open', 'high', 'low', 'close', 'volume', 'prev_close']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))

df = df[df['trade_date'] >= '20250601']
df = df[df['trade_date'] <= '20260831']

# ========== 4. 模型打分，选Top20候选 ==========
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

# ========== 5. V6回测模拟（隔日卖出） ==========
print('开始V6回测模拟（隔日卖出）...')

INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001
LIMIT_UP = 0.099  # 涨停阈值（ST股是5%，这里统一用10%）
LIMIT_DOWN = -0.099  # 跌停阈值

dates = sorted(df_candidates['trade_date'].unique())
holdings = {}  # {code: {shares, buy_price, buy_date, cost}}
cash = INITIAL_CAPITAL
trades = []
portfolio_daily = []

for i, date in enumerate(dates):
    day_candidates = df_candidates[df_candidates['trade_date'] == date].copy()
    
    # 1. 先卖出：昨天买入的今天开盘价卖出
    codes_to_sell = []
    for code, pos in holdings.items():
        # 昨天买入的，今天卖出
        stock_data = df[(df['trade_date'] == date) & (df['code'] == code)]
        if len(stock_data) > 0:
            row = stock_data.iloc[0]
            open_price = row['open']
            prev_close = row['prev_close']
            
            # 判断是否跌停（开盘跌停卖不出去）
            if pd.notna(prev_close) and prev_close > 0:
                open_pct = (open_price - prev_close) / prev_close
                if open_pct <= LIMIT_DOWN:
                    # 开盘跌停，卖不出去，继续持有到明天
                    continue
            
            # 正常卖出（开盘价 - 滑点）
            sell_price = open_price * (1 - SLIPPAGE)
            codes_to_sell.append((code, 'next_day_open', sell_price))
    
    # 执行卖出
    for code, reason, sell_price in codes_to_sell:
        pos = holdings[code]
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
    
    # 2. 再买入：今天收盘价买入（模拟2:30买入）
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
                    continue  # 涨停买不进
            
            # 计算买入价格（收盘价 + 滑点）
            buy_price = close_price * (1 + SLIPPAGE)
            
            # 等权分配
            holdings_value = sum(p['shares'] * df[(df['trade_date'] == date) & (df['code'] == c)]['close'].values[0]
                          for c, p in holdings.items() if len(df[(df['trade_date'] == date) & (df['code'] == c)]) > 0)
            target_value = (cash + holdings_value) / MAX_HOLDINGS
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

# ========== 6. 回测结果分析 ==========
print('\n' + '='*70)
print('V6 隔日卖出策略 回测结果')
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

# 保存交易记录
df_trades.to_csv(f'{PROJECT}/artifacts/stock-ml/v6_trade_analysis.csv', index=False)
print(f'\n交易记录已保存: artifacts/stock-ml/v6_trade_analysis.csv')

fdb.close()
mdb.close()
print(f'\n总耗时: {time.time()-t0:.1f}s')
