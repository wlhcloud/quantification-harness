"""
技术面择时回测V5（尾盘成交版 + 涨跌停过滤）
ML Top20候选 + 金叉/放量突破买入 + 死叉/移动止损卖出
成交口径：尾盘收盘价成交（模拟14:40决策+收盘前成交）
实盘限制：涨停封板无法买入，跌停封板无法卖出
回测期：2025-06-01 ~ 2026-08-31
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = 'D:\\ProdProject\\AI\\quantification-harness'

INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
TOP_N_CANDIDATES = 20
INITIAL_STOP_LOSS = 0.05
TRAILING_DRAWDOWN = 0.08
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001
TAIL_SLIPPAGE = 0.003
LOT = 100
EXCLUDE_CODE_PREFIX = ('920', '688')
MIN_MKT_CAP = 200000
VOL_BREAKOUT_VOL_RATIO = 2.0
VOL_BREAKOUT_HIGH_WINDOW = 20

print('加载模型...')
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()

print('读取因子数据...')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')
mkt = sqlite3.connect(f'{PROJECT}/data/market.db')

df_factors = pd.read_sql_query(
    'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    'FROM stock_ml_factors WHERE trade_date >= "20250101"', fdb)
df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20250101"', fdb)
df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20250101"', fdb)
df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')

print('计算技术指标...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, open, high, low, close, volume, amount FROM daily_bars WHERE trade_date >= "20250101" ORDER BY code, trade_date', mdb)
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
df_bars['vol_breakout'] = (
    (df_bars['volume'] > df_bars['vol_ma5'] * VOL_BREAKOUT_VOL_RATIO) &
    (df_bars['close'] >= df_bars['high_20_prev']) &
    (df_bars['close'] > df_bars['ma20']))
# 涨跌停判断
df_bars['prev_close'] = df_bars.groupby('code')['close'].shift(1)
df_bars['limit_up_price'] = (df_bars['prev_close'] * 1.10).round(2)
df_bars['is_limit_up'] = (df_bars['close'] == df_bars['high']) & (abs(df_bars['close'] - df_bars['limit_up_price']) < 0.011)
df_bars['limit_down_price'] = (df_bars['prev_close'] * 0.90).round(2)
df_bars['is_limit_down'] = (df_bars['close'] == df_bars['low']) & (abs(df_bars['close'] - df_bars['limit_down_price']) < 0.011)

df = df.merge(df_bars[['trade_date', 'code', 'open', 'close', 'volume',
                         'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross', 'vol_breakout',
                         'is_limit_up', 'is_limit_down']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))
if 'close_bar' in df.columns:
    df['close'] = df['close_bar'].fillna(df['close'])
    df = df.drop(columns=['close_bar'])

df = df[(df['trade_date'] >= '20250601') & (df['trade_date'] <= '20260831')]
print(f'回测期: {len(df)} 行, {df["trade_date"].nunique()} 个交易日')

print('模型打分...')
X = np.nan_to_num(df[feature_names].values, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)
df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= TOP_N_CANDIDATES].copy()
df_candidates = df_candidates[~df_candidates['code'].str.startswith(EXCLUDE_CODE_PREFIX)]
df_candidates = df_candidates[df_candidates['mktCap'] >= MIN_MKT_CAP]
print(f'候选: {len(df_candidates)} 行, 金叉{df_candidates["golden_cross"].sum()}次, 放量突破{df_candidates["vol_breakout"].sum()}次')

print('开始回测模拟（尾盘收盘价成交 + 涨跌停过滤）...')
dates = sorted(df_candidates['trade_date'].unique())
holdings = {}
cash = INITIAL_CAPITAL
portfolio_values = []
trade_log = []
sell_reason_count = {'death_cross': 0, 'initial_stop_loss': 0, 'trailing_stop_loss': 0}
buy_signal_count = {'golden_cross': 0, 'vol_breakout': 0, 'both': 0}
limit_up_skipped = 0
limit_down_skipped = 0

for i, date in enumerate(dates):
    day_cand = df_candidates[df_candidates['trade_date'] == date].copy()
    day_all = df[df['trade_date'] == date]

    # 卖出
    for code, pos in list(holdings.items()):
        stock = day_all[day_all['code'] == code]
        if len(stock) == 0:
            continue
        row = stock.iloc[0]
        cur = row['close']
        # 跌停封板无法卖出
        if pd.notna(row.get('is_limit_down')) and row['is_limit_down']:
            if cur > pos['highest_price']:
                pos['highest_price'] = cur
            limit_down_skipped += 1
            continue
        if cur > pos['highest_price']:
            pos['highest_price'] = cur
        init_stop = pos['buy_price'] * (1 - INITIAL_STOP_LOSS)
        trail_stop = pos['highest_price'] * (1 - TRAILING_DRAWDOWN)
        cur_stop = max(init_stop, trail_stop)
        pos['stop_loss_price'] = cur_stop
        reason = None
        if cur < cur_stop:
            reason = 'trailing_stop_loss' if cur_stop > init_stop else 'initial_stop_loss'
        elif pd.notna(row.get('death_cross')) and row['death_cross']:
            reason = 'death_cross'
        if reason:
            sell_price = cur * (1 - SLIPPAGE - TAIL_SLIPPAGE)
            proceeds = pos['shares'] * sell_price
            proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
            cash += proceeds
            sell_reason_count[reason] += 1
            trade_log.append({'date': date, 'code': code, 'action': 'sell', 'price': sell_price,
                             'shares': pos['shares'], 'reason': reason,
                             'buy_price': pos['buy_price'], 'highest_price': pos['highest_price']})
            del holdings[code]

    # 买入
    if len(holdings) < MAX_HOLDINGS:
        for _, row in day_cand.sort_values('score', ascending=False).iterrows():
            if len(holdings) >= MAX_HOLDINGS:
                break
            code = row['code']
            if code in holdings:
                continue
            # 涨停封板无法买入
            if pd.notna(row.get('is_limit_up')) and row['is_limit_up']:
                limit_up_skipped += 1
                continue
            is_golden = pd.notna(row.get('golden_cross')) and row['golden_cross']
            is_vb = pd.notna(row.get('vol_breakout')) and row['vol_breakout']
            golden_valid = is_golden and pd.notna(row.get('ma20')) and row['close'] > row['ma20']
            if golden_valid and is_vb:
                sig = 'both'
            elif golden_valid:
                sig = 'golden_cross'
            elif is_vb:
                sig = 'vol_breakout'
            else:
                continue
            hv = sum(p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
                     for c, p in holdings.items() if len(day_all[day_all['code'] == c]) > 0)
            target = (cash + hv) / MAX_HOLDINGS
            buy_price = row['close'] * (1 + SLIPPAGE + TAIL_SLIPPAGE)
            shares = int(target / buy_price / LOT) * LOT
            if shares > 0 and shares * buy_price <= cash:
                cost = shares * buy_price
                cost += cost * COMMISSION
                cash -= cost
                holdings[code] = {'shares': shares, 'buy_price': buy_price, 'buy_date': date,
                                  'highest_price': buy_price,
                                  'stop_loss_price': buy_price * (1 - INITIAL_STOP_LOSS)}
                trade_log.append({'date': date, 'code': code, 'action': 'buy', 'price': buy_price,
                                 'shares': shares, 'signal': sig})
                buy_signal_count[sig] = buy_signal_count.get(sig, 0) + 1

    # 估值
    hv = sum(p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
             for c, p in holdings.items() if len(day_all[day_all['code'] == c]) > 0)
    portfolio_values.append((date, cash + hv, cash, len(holdings)))
    if (i + 1) % 50 == 0:
        print(f'  进度: {i+1}/{len(dates)}, 净值: {cash+hv:.0f}, 持仓: {len(holdings)}只')

df_result = pd.DataFrame(portfolio_values, columns=['date', 'value', 'cash', 'holdings'])
df_result['return'] = df_result['value'].pct_change()
df_result['cum_return'] = df_result['value'] / INITIAL_CAPITAL - 1
total_return = df_result['cum_return'].iloc[-1]
annual_return = (1 + total_return) ** (252 / len(df_result)) - 1
sharpe = df_result['return'].mean() / df_result['return'].std() * np.sqrt(252) if df_result['return'].std() > 0 else 0
max_drawdown = (df_result['value'] / df_result['value'].cummax() - 1).min()

df_hs300 = pd.read_sql_query(
    'SELECT trade_date, close FROM index_daily_bars WHERE ts_code="000300.SH" AND trade_date >= "20250601" AND trade_date <= "20260831" ORDER BY trade_date', mkt)
benchmark_return = df_hs300['close'].iloc[-1] / df_hs300['close'].iloc[0] - 1 if len(df_hs300) > 0 else 0

df_trades = pd.DataFrame(trade_log)
buy_count = len(df_trades[df_trades['action'] == 'buy']) if len(df_trades) > 0 else 0
sell_count = len(df_trades[df_trades['action'] == 'sell']) if len(df_trades) > 0 else 0

df_result.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v5_backtest.csv', index=False)
df_trades.to_csv(f'{PROJECT}/artifacts/stock-ml/technical_timing_v5_trades.csv', index=False)

print(f'\n{"="*60}')
print(f'技术面择时V5回测（尾盘成交版 + 涨跌停过滤）')
print(f'回测期: 2025-06-01 ~ 2026-08-31')
print(f'{"="*60}')
print(f'总收益:     {total_return:.2%}')
print(f'年化收益:   {annual_return:.2%}')
print(f'超额收益:   {total_return-benchmark_return:.2%} (基准: {benchmark_return:.2%})')
print(f'Sharpe:     {sharpe:.3f}')
print(f'最大回撤:   {max_drawdown:.2%}')
print(f'买入: {buy_count}次 (金叉{buy_signal_count.get("golden_cross",0)}/放量突破{buy_signal_count.get("vol_breakout",0)}/同时{buy_signal_count.get("both",0)})')
print(f'卖出: {sell_count}次 (死叉{sell_reason_count["death_cross"]}/初始止损{sell_reason_count["initial_stop_loss"]}/移动止损{sell_reason_count["trailing_stop_loss"]})')
print(f'涨停跳过买入: {limit_up_skipped}次, 跌停跳过卖出: {limit_down_skipped}次')
print(f'成交口径:   尾盘收盘价（滑点{SLIPPAGE+TAIL_SLIPPAGE:.1%}）+ 涨跌停过滤')
print(f'总耗时:     {time.time()-t0:.1f}s')

fdb.close()
mkt.close()
