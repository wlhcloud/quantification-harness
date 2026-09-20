"""
技术面择时回测V5 长周期验证版（尾盘成交版）
ML Top20候选 + 金叉/放量突破买入 + 死叉/移动止损卖出

【成交口径】尾盘收盘价成交（模拟14:40决策+收盘前成交）
  - 买入价 = 收盘价 × (1 + 基础滑点 + 尾盘滑点)
  - 卖出价 = 收盘价 × (1 - 基础滑点 - 尾盘滑点)
  - 信号用收盘价计算（近似14:40信号，未考虑信号消失风险）

【买入条件】满足任一即可：
  a. 金叉：MA5上穿MA10 + 收盘价 > MA20
  b. 放量突破：成交量 > 5日均量2倍 + 收盘价创20日新高 + 收盘价 > MA20

【卖出条件】满足任一即可：
  a. 死叉：MA5下穿MA10
  b. 移动止损：当前价 < max(买入价×0.95, 持仓最高价×0.92)

回测期：2024-01-01 ~ 2026-09-11
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = 'D:\\ProdProject\\AI\\quantification-harness'

# ========== 策略参数（统一管理） ==========
INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
TOP_N_CANDIDATES = 20
INITIAL_STOP_LOSS = 0.05
TRAILING_DRAWDOWN = 0.08
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001
TAIL_SLIPPAGE = 0.003  # 尾盘额外滑点：模拟14:40决策到收盘的波动+成交冲击
LOT = 100
EXCLUDE_CODE_PREFIX = ('920', '688')
MIN_MKT_CAP = 200000
VOL_BREAKOUT_VOL_RATIO = 2.0
VOL_BREAKOUT_HIGH_WINDOW = 20

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
    'FROM stock_ml_factors WHERE trade_date >= "20230101"', fdb)
print(f'  基础因子: {len(df_factors)} 行')

df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20230101"', fdb)
print(f'  行业因子: {len(df_industry)} 行')

df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20230101"', fdb)
print(f'  资金因子: {len(df_money)} 行')

df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')
print(f'  合并后: {len(df)} 行, 耗时 {time.time()-t0:.1f}s')

# ========== 3. 读取行情数据，计算技术指标 ==========
print('读取行情数据并计算技术指标...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars WHERE trade_date >= "20230101" ORDER BY code, trade_date', mdb)
print(f'  行情数据: {len(df_bars)} 行')

df_bars = df_bars.sort_values(['code', 'trade_date'])
df_bars['ma5'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
df_bars['ma10'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
df_bars['ma20'] = df_bars.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
df_bars['vol_ma5'] = df_bars.groupby('code')['volume'].transform(lambda x: x.rolling(5).mean())
df_bars['high_20_prev'] = df_bars.groupby('code')['close'].transform(
    lambda x: x.shift(1).rolling(VOL_BREAKOUT_HIGH_WINDOW).max())

df_bars['ma5_prev'] = df_bars.groupby('code')['ma5'].shift(1)
df_bars['ma10_prev'] = df_bars.groupby('code')['ma10'].shift(1)
df_bars['golden_cross'] = (df_bars['ma5_prev'] <= df_bars['ma10_prev']) & (df_bars['ma5'] > df_bars['ma10'])
df_bars['death_cross'] = (df_bars['ma5_prev'] >= df_bars['ma10_prev']) & (df_bars['ma5'] < df_bars['ma10'])

# 涨停判断：收盘封涨停（收盘价==最高价 且 收盘价接近前收盘价×1.1）
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)
df_bars['limit_up_price'] = (df_bars['prev_close'] * 1.10).round(2)
df_bars['is_limit_up'] = (df_bars['close'] == df_bars['high']) & (abs(df_bars['close'] - df_bars['limit_up_price']) < 0.011)
# 跌停判断（卖出时参考）
df_bars['limit_down_price'] = (df_bars['prev_close'] * 0.90).round(2)
df_bars['is_limit_down'] = (df_bars['close'] == df_bars['low']) & (abs(df_bars['close'] - df_bars['limit_down_price']) < 0.011)

df_bars['vol_breakout'] = (
    (df_bars['volume'] > df_bars['vol_ma5'] * VOL_BREAKOUT_VOL_RATIO) &
    (df_bars['close'] >= df_bars['high_20_prev']) &
    (df_bars['close'] > df_bars['ma20']))

print(f'  技术指标计算完成, 耗时 {time.time()-t0:.1f}s')

# ========== 4. 合并因子和行情 ==========
print('合并因子和行情...')
df = df.merge(df_bars[['trade_date', 'code', 'open', 'high', 'low', 'close', 'volume',
                         'ma5', 'ma10', 'ma20', 'vol_ma5', 'high_20_prev',
                         'golden_cross', 'death_cross', 'vol_breakout',
                         'is_limit_up', 'is_limit_down']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))
if 'close_bar' in df.columns:
    df['close'] = df['close_bar'].fillna(df['close'])
    df = df.drop(columns=['close_bar'])
print(f'  合并后: {len(df)} 行')

df = df[df['trade_date'] >= '20240101']
df = df[df['trade_date'] <= '20260911']
print(f'  回测期: {len(df)} 行, {df["trade_date"].nunique()} 个交易日')

# ========== 5. 模型打分，选Top20候选 ==========
print('模型打分...')
X = df[feature_names].values
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)
print(f'  打分完成, 耗时 {time.time()-t0:.1f}s')

df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= TOP_N_CANDIDATES].copy()
print(f'  Top{TOP_N_CANDIDATES}候选: {len(df_candidates)} 行')

df_candidates = df_candidates[~df_candidates['code'].str.startswith(EXCLUDE_CODE_PREFIX)]
df_candidates = df_candidates[df_candidates['mktCap'] >= MIN_MKT_CAP]
print(f'  过滤后候选: {len(df_candidates)} 行')

golden_count = df_candidates['golden_cross'].sum()
vol_breakout_count = df_candidates['vol_breakout'].sum()
either_signal = df_candidates['golden_cross'] | df_candidates['vol_breakout']
print(f'  金叉信号: {golden_count} 次')
print(f'  放量突破信号: {vol_breakout_count} 次')
print(f'  买入信号合计(并集): {either_signal.sum()} 次')

# ========== 6. 回测模拟（尾盘收盘价成交） ==========
print('开始回测模拟（尾盘收盘价成交）...')

dates = sorted(df_candidates['trade_date'].unique())
print(f'  回测交易日: {len(dates)} 天')

holdings = {}
cash = INITIAL_CAPITAL
portfolio_values = []
trade_log = []
sell_reason_count = {'death_cross': 0, 'initial_stop_loss': 0, 'trailing_stop_loss': 0}
buy_signal_count = {'golden_cross': 0, 'vol_breakout': 0, 'both': 0}
trailing_stop_triggered_profit = []

for i, date in enumerate(dates):
    day_candidates = df_candidates[df_candidates['trade_date'] == date].copy()
    day_all = df[df['trade_date'] == date]

    # ===== 步骤1: 检查卖出信号，尾盘收盘价成交 =====
    codes_to_sell = []
    for code, pos in holdings.items():
        stock_data = day_all[day_all['code'] == code]
        if len(stock_data) == 0:
            continue
        row = stock_data.iloc[0]
        current_price = row['close']
        buy_price = pos['buy_price']

        # 跌停封板无法卖出（实盘限制），当天跳过卖出
        if pd.notna(row.get('is_limit_down')) and row['is_limit_down']:
            # 仍更新最高价，但不生成卖出信号
            if current_price > pos['highest_price']:
                pos['highest_price'] = current_price
            continue

        if current_price > pos['highest_price']:
            pos['highest_price'] = current_price

        initial_stop_price = buy_price * (1 - INITIAL_STOP_LOSS)
        trailing_stop_price = pos['highest_price'] * (1 - TRAILING_DRAWDOWN)
        current_stop_price = max(initial_stop_price, trailing_stop_price)
        pos['stop_loss_price'] = current_stop_price

        if current_price < current_stop_price:
            if abs(current_stop_price - initial_stop_price) < 1e-6:
                reason = 'initial_stop_loss'
            else:
                reason = 'trailing_stop_loss'
                trailing_stop_triggered_profit.append((current_price - buy_price) / buy_price)
            codes_to_sell.append((code, reason))
        elif pd.notna(row.get('death_cross')) and row['death_cross']:
            codes_to_sell.append((code, 'death_cross'))

    for code, reason in codes_to_sell:
        pos = holdings[code]
        stock_data = day_all[day_all['code'] == code]
        if len(stock_data) == 0:
            continue
        sell_price = stock_data.iloc[0]['close'] * (1 - SLIPPAGE - TAIL_SLIPPAGE)
        proceeds = pos['shares'] * sell_price
        proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
        cash += proceeds
        sell_reason_count[reason] = sell_reason_count.get(reason, 0) + 1
        trade_log.append({'date': date, 'code': code, 'action': 'sell', 'price': sell_price,
                         'shares': pos['shares'], 'reason': reason,
                         'buy_price': pos['buy_price'], 'highest_price': pos['highest_price'],
                         'stop_loss_price': pos['stop_loss_price']})
        del holdings[code]

    # ===== 步骤2: 检查买入信号，尾盘收盘价成交 =====
    if len(holdings) < MAX_HOLDINGS:
        buy_candidates = day_candidates.sort_values('score', ascending=False)
        for _, row in buy_candidates.iterrows():
            if len(holdings) >= MAX_HOLDINGS:
                break
            code = row['code']
            if code in holdings:
                continue

            # 涨停封板无法买入（实盘限制）
            if pd.notna(row.get('is_limit_up')) and row['is_limit_up']:
                continue

            is_golden = pd.notna(row.get('golden_cross')) and row['golden_cross']
            is_vol_breakout = pd.notna(row.get('vol_breakout')) and row['vol_breakout']
            golden_valid = is_golden and pd.notna(row.get('ma20')) and row['close'] > row['ma20']

            if golden_valid and is_vol_breakout:
                signal_type = 'both'
            elif golden_valid:
                signal_type = 'golden_cross'
            elif is_vol_breakout:
                signal_type = 'vol_breakout'
            else:
                continue

            total_holdings_value = sum(
                p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
                for c, p in holdings.items()
                if len(day_all[day_all['code'] == c]) > 0)
            target_value = (cash + total_holdings_value) / MAX_HOLDINGS
            buy_price = row['close'] * (1 + SLIPPAGE + TAIL_SLIPPAGE)
            shares = int(target_value / buy_price / LOT) * LOT

            if shares > 0 and shares * buy_price <= cash:
                cost = shares * buy_price
                cost += cost * COMMISSION
                cash -= cost
                initial_stop = buy_price * (1 - INITIAL_STOP_LOSS)
                holdings[code] = {
                    'shares': shares, 'buy_price': buy_price, 'buy_date': date,
                    'highest_price': buy_price, 'stop_loss_price': initial_stop}
                trade_log.append({'date': date, 'code': code, 'action': 'buy', 'price': buy_price,
                                 'shares': shares, 'signal': signal_type,
                                 'initial_stop_loss': initial_stop})
                buy_signal_count[signal_type] = buy_signal_count.get(signal_type, 0) + 1

    # ===== 步骤3: 估值 =====
    holdings_value = sum(
        pos['shares'] * day_all[day_all['code'] == code]['close'].values[0]
        for code, pos in holdings.items()
        if len(day_all[day_all['code'] == code]) > 0)
    total_value = cash + holdings_value
    portfolio_values.append((date, total_value, cash, len(holdings)))

    if (i + 1) % 100 == 0:
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

df_hs300 = pd.read_sql_query(
    'SELECT trade_date, close FROM index_daily_bars WHERE ts_code="000300.SH" AND trade_date >= "20240101" AND trade_date <= "20260911" ORDER BY trade_date', mdb)
benchmark_return = df_hs300['close'].iloc[-1] / df_hs300['close'].iloc[0] - 1 if len(df_hs300) > 0 else 0
excess_return = total_return - benchmark_return

df_result['year'] = df_result['date'].str[:4]
yearly_returns = {}
for year, group in df_result.groupby('year'):
    if len(group) > 1:
        yearly_returns[year] = group['value'].iloc[-1] / group['value'].iloc[0] - 1

df_trades = pd.DataFrame(trade_log)
buy_count = len(df_trades[df_trades['action'] == 'buy']) if len(df_trades) > 0 else 0
sell_count = len(df_trades[df_trades['action'] == 'sell']) if len(df_trades) > 0 else 0
trailing_count = sell_reason_count.get('trailing_stop_loss', 0)
initial_stop_count = sell_reason_count.get('initial_stop_loss', 0)
death_cross_count = sell_reason_count.get('death_cross', 0)
avg_trailing_profit = np.mean(trailing_stop_triggered_profit) if trailing_stop_triggered_profit else 0

if len(df_trades) > 0:
    sells = df_trades[df_trades['action'] == 'sell'].copy()
    if 'buy_price' in sells.columns:
        sells['profit'] = (sells['price'] - sells['buy_price']) / sells['buy_price']
        win_rate = (sells['profit'] > 0).mean()
        avg_win = sells[sells['profit'] > 0]['profit'].mean() if (sells['profit'] > 0).sum() > 0 else 0
        avg_loss = sells[sells['profit'] <= 0]['profit'].mean() if (sells['profit'] <= 0).sum() > 0 else 0
    else:
        win_rate = avg_win = avg_loss = 0
else:
    win_rate = avg_win = avg_loss = 0

df_result.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v5_long_backtest.csv', index=False)
df_trades.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v5_long_trades.csv', index=False)

print(f'\n{"="*70}')
print(f'技术面择时V5 长周期验证（尾盘成交版：放量突破+尾盘收盘价成交）')
print(f'回测期: 2024-01-01 ~ 2026-09-11')
print(f'{"="*70}')
print(f'总收益:     {total_return:.2%}')
print(f'年化收益:   {annual_return:.2%}')
print(f'超额收益:   {excess_return:.2%} (基准沪深300: {benchmark_return:.2%})')
print(f'Sharpe:     {sharpe:.3f}')
print(f'最大回撤:   {max_drawdown:.2%}')
print(f'交易日数:   {len(df_result)}')
print(f'期末持仓:   {len(holdings)}只')
print(f'期末净值:   {df_result["value"].iloc[-1]:.0f}')
print(f'\n--- 分年度收益 ---')
for year, ret in sorted(yearly_returns.items()):
    print(f'  {year}: {ret:.2%}')
print(f'\n--- 交易统计 ---')
print(f'买入次数:   {buy_count}')
print(f'卖出次数:   {sell_count}')
print(f'  死叉卖出:           {death_cross_count}次')
print(f'  初始止损卖出(亏5%): {initial_stop_count}次')
print(f'  移动止损卖出(回撤8%): {trailing_count}次')
if trailing_count > 0:
    print(f'  移动止损平均盈利: {avg_trailing_profit:.2%}')
print(f'\n--- 买入信号分布 ---')
print(f'  金叉买入:     {buy_signal_count.get("golden_cross", 0)}次')
print(f'  放量突破买入: {buy_signal_count.get("vol_breakout", 0)}次')
print(f'  两者同时:     {buy_signal_count.get("both", 0)}次')
print(f'\n--- 胜率统计 ---')
print(f'胜率:       {win_rate:.2%}')
print(f'平均盈利:   {avg_win:.2%}')
print(f'平均亏损:   {avg_loss:.2%}')
print(f'盈亏比:     {abs(avg_win/avg_loss):.2f}' if avg_loss != 0 else '盈亏比: N/A')
print(f'\n成交口径:   尾盘收盘价（买入+{SLIPPAGE+TAIL_SLIPPAGE:.1%}, 卖出-{SLIPPAGE+TAIL_SLIPPAGE:.1%}）')
print(f'买入条件:   金叉+close>MA20  OR  放量突破(量>5日均量2倍+创20日新高+close>MA20)')
print(f'移动止损:   初始止损{INITIAL_STOP_LOSS:.0%}, 回撤{TRAILING_DRAWDOWN:.0%}')
print(f'候选池:     Top{TOP_N_CANDIDATES}，排除{"/".join(EXCLUDE_CODE_PREFIX)}，最小市值{MIN_MKT_CAP/10000:.0f}亿')
print(f'总耗时:     {time.time()-t0:.1f}s')
print(f'\n结果已保存: artifacts/stock-ml/technical_timing_v5_long_backtest.csv')
print(f'交易记录:   artifacts/stock-ml/technical_timing_v5_long_trades.csv')

fdb.close()
mdb.close()
