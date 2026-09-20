"""
技术面择时V5 成交口径对比回测

同样的买入/卖出信号，分别用两种成交口径回测：
  A. T+1开盘价成交（当前实盘口径，保守可信）
  B. 尾盘收盘价成交（模拟14:40决策+收盘前成交，收盘价±滑点）

目的：量化"尾盘成交"比"T+1开盘"好多少，判断是否值得拉分钟数据做精确回测。

注意：B口径是近似——信号用收盘价计算（假设14:40信号=收盘信号），
     成交价用收盘价加额外滑点（模拟14:40到收盘的波动）。
     不考虑"14:40信号成立但收盘信号消失"的情况，实际会比B更差一些。
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time

t0 = time.time()
PROJECT = 'D:\\ProdProject\\AI\\quantification-harness'

# ========== 策略参数 ==========
INITIAL_CAPITAL = 1000000
MAX_HOLDINGS = 5
TOP_N_CANDIDATES = 20
INITIAL_STOP_LOSS = 0.05
TRAILING_DRAWDOWN = 0.08
COMMISSION = 0.0003
STAMP_DUTY = 0.0005
SLIPPAGE = 0.001
LOT = 100
EXCLUDE_CODE_PREFIX = ('920', '688')
MIN_MKT_CAP = 200000
VOL_BREAKOUT_VOL_RATIO = 2.0
VOL_BREAKOUT_HIGH_WINDOW = 20
# 尾盘成交额外滑点：模拟14:40决策后到收盘的波动+成交冲击
# 买入时比收盘价高0.3%，卖出时比收盘价低0.3%
TAIL_SLIPPAGE = 0.003

# ========== 1. 加载模型 ==========
print('加载模型...')
model = lgb.Booster(model_file=f'{PROJECT}/artifacts/stock-ml/stock-wf-model.txt')
feature_names = model.feature_name()

# ========== 2. 读取数据 ==========
print('读取因子数据...')
fdb = sqlite3.connect(f'{PROJECT}/data/factors.db')
mdb = sqlite3.connect(f'{PROJECT}/data/market.db')

df_factors = pd.read_sql_query(
    'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
    'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
    'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
    'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
    'FROM stock_ml_factors WHERE trade_date >= "20230101"', fdb)
df_industry = pd.read_sql_query(
    'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
    'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20230101"', fdb)
df_money = pd.read_sql_query(
    'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20230101"', fdb)
df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
df = df.merge(df_money, on=['trade_date', 'code'], how='left')

# ========== 3. 计算技术指标 ==========
print('计算技术指标...')
df_bars = pd.read_sql_query(
    'SELECT trade_date, code, open, high, low, close, volume FROM daily_bars WHERE trade_date >= "20230101" ORDER BY code, trade_date', mdb)
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

df = df.merge(df_bars[['trade_date', 'code', 'open', 'close', 'volume',
                         'ma5', 'ma10', 'ma20', 'golden_cross', 'death_cross', 'vol_breakout']],
               on=['trade_date', 'code'], how='left', suffixes=('', '_bar'))
if 'close_bar' in df.columns:
    df['close'] = df['close_bar'].fillna(df['close'])
    df = df.drop(columns=['close_bar'])

df = df[(df['trade_date'] >= '20240101') & (df['trade_date'] <= '20260911')]
print(f'回测期: {len(df)} 行, {df["trade_date"].nunique()} 个交易日')

# ========== 4. 模型打分选候选 ==========
print('模型打分...')
X = np.nan_to_num(df[feature_names].values, nan=0.0, posinf=0.0, neginf=0.0)
df['score'] = model.predict(X)
df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
df_candidates = df[df['rank'] <= TOP_N_CANDIDATES].copy()
df_candidates = df_candidates[~df_candidates['code'].str.startswith(EXCLUDE_CODE_PREFIX)]
df_candidates = df_candidates[df_candidates['mktCap'] >= MIN_MKT_CAP]
print(f'候选: {len(df_candidates)} 行')

# ========== 5. 双口径回测 ==========
print('开始双口径回测...')
dates = sorted(df_candidates['trade_date'].unique())

def make_portfolio():
    return {'holdings': {}, 'cash': INITIAL_CAPITAL, 'values': []}

port_t1 = make_portfolio()   # T+1开盘成交
port_tail = make_portfolio() # 尾盘收盘价成交

# T+1口径的待执行订单
pending_sells_t1 = []
pending_buys_t1 = []

def exec_sell_t1(port, code, reason, day_all):
    """T+1开盘卖出"""
    pos = port['holdings'].get(code)
    if not pos:
        return
    stock = day_all[day_all['code'] == code]
    if len(stock) == 0 or pd.isna(stock.iloc[0].get('open')):
        return
    price = stock.iloc[0]['open'] * (1 - SLIPPAGE)
    proceeds = pos['shares'] * price
    proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
    port['cash'] += proceeds
    del port['holdings'][code]

def exec_buy_t1(port, code, day_all):
    """T+1开盘买入"""
    if code in port['holdings'] or len(port['holdings']) >= MAX_HOLDINGS:
        return
    stock = day_all[day_all['code'] == code]
    if len(stock) == 0 or pd.isna(stock.iloc[0].get('open')):
        return
    price = stock.iloc[0]['open'] * (1 + SLIPPAGE)
    hv = sum(p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
             for c, p in port['holdings'].items() if len(day_all[day_all['code'] == c]) > 0)
    target = (port['cash'] + hv) / MAX_HOLDINGS
    shares = int(target / price / LOT) * LOT
    if shares > 0 and shares * price <= port['cash']:
        cost = shares * price
        cost += cost * COMMISSION
        port['cash'] -= cost
        port['holdings'][code] = {
            'shares': shares, 'buy_price': price, 'highest_price': price,
            'stop_loss_price': price * (1 - INITIAL_STOP_LOSS)}

def exec_sell_tail(port, code, reason, day_all):
    """尾盘收盘价卖出（加额外滑点）"""
    pos = port['holdings'].get(code)
    if not pos:
        return
    stock = day_all[day_all['code'] == code]
    if len(stock) == 0:
        return
    price = stock.iloc[0]['close'] * (1 - SLIPPAGE - TAIL_SLIPPAGE)
    proceeds = pos['shares'] * price
    proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
    port['cash'] += proceeds
    del port['holdings'][code]

def exec_buy_tail(port, code, day_all):
    """尾盘收盘价买入（加额外滑点）"""
    if code in port['holdings'] or len(port['holdings']) >= MAX_HOLDINGS:
        return
    stock = day_all[day_all['code'] == code]
    if len(stock) == 0:
        return
    price = stock.iloc[0]['close'] * (1 + SLIPPAGE + TAIL_SLIPPAGE)
    hv = sum(p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
             for c, p in port['holdings'].items() if len(day_all[day_all['code'] == c]) > 0)
    target = (port['cash'] + hv) / MAX_HOLDINGS
    shares = int(target / price / LOT) * LOT
    if shares > 0 and shares * price <= port['cash']:
        cost = shares * price
        cost += cost * COMMISSION
        port['cash'] -= cost
        port['holdings'][code] = {
            'shares': shares, 'buy_price': price, 'highest_price': price,
            'stop_loss_price': price * (1 - INITIAL_STOP_LOSS)}

def update_and_check_sell(port, code, day_all):
    """更新最高价，检查卖出信号，返回(是否卖出, 原因)"""
    pos = port['holdings'].get(code)
    if not pos:
        return False, None
    stock = day_all[day_all['code'] == code]
    if len(stock) == 0:
        return False, None
    row = stock.iloc[0]
    cur = row['close']
    if cur > pos['highest_price']:
        pos['highest_price'] = cur
    init_stop = pos['buy_price'] * (1 - INITIAL_STOP_LOSS)
    trail_stop = pos['highest_price'] * (1 - TRAILING_DRAWDOWN)
    cur_stop = max(init_stop, trail_stop)
    pos['stop_loss_price'] = cur_stop
    if cur < cur_stop:
        reason = 'trailing_stop' if cur_stop > init_stop else 'initial_stop'
        return True, reason
    if pd.notna(row.get('death_cross')) and row['death_cross']:
        return True, 'death_cross'
    return False, None

def check_buy_signal(row):
    """检查买入信号，返回信号类型或None"""
    is_golden = pd.notna(row.get('golden_cross')) and row['golden_cross']
    is_vb = pd.notna(row.get('vol_breakout')) and row['vol_breakout']
    golden_valid = is_golden and pd.notna(row.get('ma20')) and row['close'] > row['ma20']
    if golden_valid and is_vb:
        return 'both'
    elif golden_valid:
        return 'golden_cross'
    elif is_vb:
        return 'vol_breakout'
    return None

for i, date in enumerate(dates):
    day_cand = df_candidates[df_candidates['trade_date'] == date].copy()
    day_all = df[df['trade_date'] == date]

    # ===== T+1口径：执行待订单 =====
    for code, reason in pending_sells_t1:
        exec_sell_t1(port_t1, code, reason, day_all)
    pending_sells_t1 = []
    for code, _ in pending_buys_t1:
        exec_buy_t1(port_t1, code, day_all)
    pending_buys_t1 = []

    # ===== 两个口径共用：检查卖出信号（用今日收盘价） =====
    # T+1口径：生成待卖出（明日开盘执行）
    for code in list(port_t1['holdings'].keys()):
        sell, reason = update_and_check_sell(port_t1, code, day_all)
        if sell:
            pending_sells_t1.append((code, reason))
    # 尾盘口径：直接用今日收盘价卖出
    for code in list(port_tail['holdings'].keys()):
        sell, reason = update_and_check_sell(port_tail, code, day_all)
        if sell:
            exec_sell_tail(port_tail, code, reason, day_all)

    # ===== 两个口径共用：检查买入信号 =====
    buy_codes = []
    if len(port_t1['holdings']) + len(pending_buys_t1) < MAX_HOLDINGS:
        for _, row in day_cand.sort_values('score', ascending=False).iterrows():
            if len(port_t1['holdings']) + len(pending_buys_t1) >= MAX_HOLDINGS:
                break
            code = row['code']
            if code in port_t1['holdings'] or code in [c for c, _ in pending_buys_t1]:
                continue
            sig = check_buy_signal(row)
            if sig:
                buy_codes.append(code)
                pending_buys_t1.append((code, sig))
    # 尾盘口径：直接用今日收盘价买入
    if len(port_tail['holdings']) < MAX_HOLDINGS:
        for _, row in day_cand.sort_values('score', ascending=False).iterrows():
            if len(port_tail['holdings']) >= MAX_HOLDINGS:
                break
            code = row['code']
            if code in port_tail['holdings']:
                continue
            sig = check_buy_signal(row)
            if sig:
                exec_buy_tail(port_tail, code, day_all)

    # ===== 估值 =====
    def nav(port):
        hv = sum(p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
                 for c, p in port['holdings'].items() if len(day_all[day_all['code'] == c]) > 0)
        return port['cash'] + hv
    port_t1['values'].append((date, nav(port_t1)))
    port_tail['values'].append((date, nav(port_tail)))

    if (i + 1) % 100 == 0:
        print(f'  进度 {i+1}/{len(dates)}: T+1={port_t1["values"][-1][1]:.0f} 尾盘={port_tail["values"][-1][1]:.0f}')

# ========== 6. 结果对比 ==========
def calc_metrics(values, label):
    df_v = pd.DataFrame(values, columns=['date', 'value'])
    df_v['return'] = df_v['value'].pct_change()
    df_v['cum'] = df_v['value'] / INITIAL_CAPITAL - 1
    total = df_v['cum'].iloc[-1]
    annual = (1 + total) ** (252 / len(df_v)) - 1
    sharpe = df_v['return'].mean() / df_v['return'].std() * np.sqrt(252) if df_v['return'].std() > 0 else 0
    mdd = (df_v['value'] / df_v['value'].cummax() - 1).min()
    return {'label': label, 'total': total, 'annual': annual, 'sharpe': sharpe, 'mdd': mdd, 'days': len(df_v)}

m_t1 = calc_metrics(port_t1['values'], 'T+1开盘')
m_tail = calc_metrics(port_tail['values'], '尾盘收盘')

# 基准
df_hs300 = pd.read_sql_query(
    'SELECT trade_date, close FROM index_daily_bars WHERE ts_code="000300.SH" AND trade_date >= "20240101" AND trade_date <= "20260911" ORDER BY trade_date', mdb)
bench_ret = df_hs300['close'].iloc[-1] / df_hs300['close'].iloc[0] - 1 if len(df_hs300) > 0 else 0

fdb.close()
mdb.close()

print(f'\n{"="*65}')
print(f'成交口径对比回测（2024-01-01 ~ 2026-09-11，同样信号）')
print(f'{"="*65}')
print(f'{"指标":<12} {"T+1开盘":>12} {"尾盘收盘":>12} {"差异":>12}')
print(f'{"-"*65}')
print(f'{"总收益":<12} {m_t1["total"]:>11.2%} {m_tail["total"]:>11.2%} {m_tail["total"]-m_t1["total"]:>+11.2%}')
print(f'{"年化收益":<12} {m_t1["annual"]:>11.2%} {m_tail["annual"]:>11.2%} {m_tail["annual"]-m_t1["annual"]:>+11.2%}')
print(f'{"Sharpe":<12} {m_t1["sharpe"]:>12.3f} {m_tail["sharpe"]:>12.3f} {m_tail["sharpe"]-m_t1["sharpe"]:>+12.3f}')
print(f'{"最大回撤":<12} {m_t1["mdd"]:>11.2%} {m_tail["mdd"]:>11.2%} {m_tail["mdd"]-m_t1["mdd"]:>+11.2%}')
print(f'{"交易日":<12} {m_t1["days"]:>12} {m_tail["days"]:>12}')
print(f'{"-"*65}')
print(f'沪深300基准: {bench_ret:.2%}')
print(f'\n尾盘额外滑点假设: 买入+{TAIL_SLIPPAGE:.1%}, 卖出-{TAIL_SLIPPAGE:.1%}')
print(f'注：尾盘口径假设14:40信号=收盘信号（未考虑信号消失风险）')
print(f'    实际14:40策略表现会比"尾盘收盘"口径更差一些')
print(f'总耗时: {time.time()-t0:.1f}s')
