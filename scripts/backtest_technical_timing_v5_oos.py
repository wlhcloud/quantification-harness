"""
技术面择时V5 真正OOS（Out-of-Sample）回测框架

【问题背景】
原回测脚本直接加载最终模型 stock-wf-model.txt，该模型训练时已见过测试期数据，
属于 in-sample 回测，收益被高估。

【OOS方案】
按时间分段，每段只用该段之前的数据训练模型，然后在该段测试：
  - 段1（2024年）：用 2022-07 ~ 2023-12 数据训练
  - 段2（2025年）：用 2022-07 ~ 2024-12 数据训练
  - 段3（2026年）：用 2022-07 ~ 2025-12 数据训练

每段训练一个独立模型，测试期完全不参与训练，消除前视偏差。

【使用方式】
  python backtest_technical_timing_v5_oos.py

注意：训练3个模型约需10-20分钟，请耐心等待。
模型保存在 artifacts/stock-ml/oos_models/ 目录，可重复使用。
"""
import sqlite3
import lightgbm as lgb
import numpy as np
import pandas as pd
import time
import os
from pathlib import Path

t0 = time.time()
PROJECT = Path('D:/ProdProject/AI/quantification-harness')
ARTIFACT_DIR = PROJECT / 'artifacts' / 'stock-ml'
OOS_MODEL_DIR = ARTIFACT_DIR / 'oos_models'
OOS_MODEL_DIR.mkdir(parents=True, exist_ok=True)

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

# OOS分段定义：(测试期开始, 测试期结束, 训练截止日期)
OOS_SEGMENTS = [
    ('20240101', '20241231', '20231231'),
    ('20250101', '20251231', '20241231'),
    ('20260101', '20260911', '20251231'),
]

# 模型特征（与stock-ml.yaml一致）
FEATURE_NAMES = [
    'momentum20', 'momentum60', 'trend', 'drawdown60',
    'volumeRatio', 'volSurge3', 'volSurge5', 'amountRatio5', 'volPriceCorr20',
    'turnoverRatio5', 'amplitude20', 'peTtm', 'pb', 'psTtm', 'dvTtm', 'roe',
    'grossMargin', 'netMargin', 'revenueYoy', 'netprofitYoy', 'ocfYoy',
    'mktCap', 'volatility20', 'floatRatio',
    'industry_mom20', 'industry_rank20', 'industry_excess5', 'industry_excess20',
    'industry_amount_chg', 'turnover_surge', 'close_position_5d',
]
LABEL = 'forward_3'


def train_model(train_end_date: str, model_path: Path) -> lgb.Booster:
    """用指定截止日期之前的数据训练LightGBM模型"""
    print(f'  训练模型（数据截止 {train_end_date}）...')
    fdb = sqlite3.connect(str(PROJECT / 'data' / 'factors.db'))

    # 基础因子（stock_ml_factors表）
    base_features = [
        'momentum20', 'momentum60', 'trend', 'drawdown60',
        'volumeRatio', 'volSurge3', 'volSurge5', 'amountRatio5', 'volPriceCorr20',
        'turnoverRatio5', 'amplitude20', 'peTtm', 'pb', 'psTtm', 'dvTtm', 'roe',
        'grossMargin', 'netMargin', 'revenueYoy', 'netprofitYoy', 'ocfYoy',
        'mktCap', 'volatility20', 'floatRatio',
    ]
    df_factors = pd.read_sql_query(
        f'SELECT trade_date, code, {",".join(base_features)}, {LABEL} '
        f'FROM stock_ml_factors WHERE trade_date <= "{train_end_date}" AND {LABEL} IS NOT NULL',
        fdb
    )
    # 行业因子（独立表）
    df_industry = pd.read_sql_query(
        f'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
        f'industry_excess20, industry_amount_chg FROM stock_industry_factors '
        f'WHERE trade_date <= "{train_end_date}"',
        fdb
    )
    # 资金因子（独立表）
    df_money = pd.read_sql_query(
        f'SELECT trade_date, code, turnover_surge, close_position_5d '
        f'FROM stock_money_flow_factors WHERE trade_date <= "{train_end_date}"',
        fdb
    )
    fdb.close()

    df = df_factors.merge(df_industry, on=['trade_date', 'code'], how='left')
    df = df.merge(df_money, on=['trade_date', 'code'], how='left')

    # 过滤：排除ST/退市、最小市值等（简化版）
    df = df[~df['code'].str.startswith(EXCLUDE_CODE_PREFIX)]
    df = df[df['mktCap'] >= MIN_MKT_CAP]
    df = df.dropna(subset=[LABEL])

    print(f'    训练样本: {len(df)} 行')

    X = df[FEATURE_NAMES].values
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    y = df[LABEL].values

    # 训练/验证分割（最后40天做验证）
    dates = sorted(df['trade_date'].unique())
    if len(dates) > 40:
        val_start = dates[-40]
        train_mask = df['trade_date'] < val_start
        val_mask = ~train_mask
        X_train, y_train = X[train_mask], y[train_mask]
        X_val, y_val = X[val_mask], y[val_mask]
    else:
        X_train, y_train = X, y
        X_val, y_val = X, y

    # LightGBM参数（与stock-ml.yaml一致）
    params = {
        'objective': 'regression',
        'metric': 'mse',
        'n_estimators': 300,
        'learning_rate': 0.03,
        'num_leaves': 31,
        'min_data_in_leaf': 20,
        'subsample': 0.8,
        'colsample_bytree': 0.8,
        'random_state': 42,
        'verbose': -1,
    }

    model = lgb.LGBMRegressor(**params)
    model.fit(
        X_train, y_train,
        eval_set=[(X_val, y_val)],
        callbacks=[lgb.early_stopping(30, verbose=False)]
    )

    # 保存为Booster格式，并设置特征名
    booster = model.booster_
    booster.feature_name = lambda: FEATURE_NAMES  # 覆盖feature_name方法
    # 直接在dataset中设置特征名
    booster._feature_name = FEATURE_NAMES
    booster.save_model(str(model_path))
    print(f'    模型已保存: {model_path.name} (best_iteration={model.best_iteration_})')
    return booster


def load_or_train_model(segment_idx: int, train_end_date: str) -> lgb.Booster:
    """加载已有模型或训练新模型"""
    model_path = OOS_MODEL_DIR / f'oos_model_seg{segment_idx}_{train_end_date}.txt'
    if model_path.exists():
        print(f'  加载已有模型: {model_path.name}')
        return lgb.Booster(model_file=str(model_path))
    return train_model(train_end_date, model_path)


def run_segment_backtest(model: lgb.Booster, test_start: str, test_end: str,
                         df_factors_full: pd.DataFrame, df_bars_full: pd.DataFrame) -> pd.DataFrame:
    """对单个OOS段执行回测，返回日度净值DataFrame"""
    feature_names = FEATURE_NAMES  # 直接用全局特征名，不依赖model.feature_name()

    # 筛选测试期数据（需要提前20天计算均线）
    lookback_start = pd.Timestamp(test_start) - pd.Timedelta(days=40)
    lookback_start_str = lookback_start.strftime('%Y%m%d')

    df = df_factors_full[
        (df_factors_full['trade_date'] >= lookback_start_str) &
        (df_factors_full['trade_date'] <= test_end)
    ].copy()

    # 合并行情
    df = df.merge(
        df_bars_full[['trade_date', 'code', 'open', 'high', 'low', 'close', 'volume',
                       'ma5', 'ma10', 'ma20', 'vol_ma5', 'high_20_prev',
                       'golden_cross', 'death_cross', 'vol_breakout']],
        on=['trade_date', 'code'], how='left', suffixes=('', '_bar')
    )
    if 'close_bar' in df.columns:
        df['close'] = df['close_bar'].fillna(df['close'])
        df = df.drop(columns=['close_bar'])

    # 只保留测试期
    df = df[df['trade_date'] >= test_start]
    if len(df) == 0:
        return pd.DataFrame()

    # 模型打分
    X = df[feature_names].values
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    df['score'] = model.predict(X)

    df = df.sort_values(['trade_date', 'score'], ascending=[True, False])
    df['rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first')
    df_candidates = df[df['rank'] <= TOP_N_CANDIDATES].copy()
    df_candidates = df_candidates[~df_candidates['code'].str.startswith(EXCLUDE_CODE_PREFIX)]
    df_candidates = df_candidates[df_candidates['mktCap'] >= MIN_MKT_CAP]

    # 回测模拟（T+1成交，与v5_long一致）
    dates = sorted(df_candidates['trade_date'].unique())
    holdings = {}
    cash = INITIAL_CAPITAL
    portfolio_values = []
    pending_sells = []
    pending_buys = []

    for date in dates:
        day_candidates = df_candidates[df_candidates['trade_date'] == date].copy()
        day_all = df[df['trade_date'] == date]

        # 执行待卖出
        for code, reason in pending_sells:
            if code not in holdings:
                continue
            pos = holdings[code]
            stock_data = day_all[day_all['code'] == code]
            if len(stock_data) > 0 and pd.notna(stock_data.iloc[0].get('open')):
                sell_price = stock_data.iloc[0]['open'] * (1 - SLIPPAGE)
            elif len(stock_data) > 0:
                sell_price = stock_data.iloc[0]['close'] * (1 - SLIPPAGE)
            else:
                continue
            proceeds = pos['shares'] * sell_price
            proceeds -= proceeds * (COMMISSION + STAMP_DUTY)
            cash += proceeds
            del holdings[code]
        pending_sells = []

        # 执行待买入
        for code, signal_type in pending_buys:
            if code in holdings or len(holdings) >= MAX_HOLDINGS:
                continue
            stock_data = day_all[day_all['code'] == code]
            if len(stock_data) == 0 or pd.isna(stock_data.iloc[0].get('open')):
                continue
            buy_price = stock_data.iloc[0]['open'] * (1 + SLIPPAGE)
            total_holdings_value = sum(
                p['shares'] * day_all[day_all['code'] == c]['close'].values[0]
                for c, p in holdings.items()
                if len(day_all[day_all['code'] == c]) > 0
            )
            target_value = (cash + total_holdings_value) / MAX_HOLDINGS
            shares = int(target_value / buy_price / LOT) * LOT
            if shares > 0 and shares * buy_price <= cash:
                cost = shares * buy_price
                cost += cost * COMMISSION
                cash -= cost
                holdings[code] = {
                    'shares': shares, 'buy_price': buy_price, 'buy_date': date,
                    'highest_price': buy_price,
                    'stop_loss_price': buy_price * (1 - INITIAL_STOP_LOSS)
                }
        pending_buys = []

        # 检查卖出信号
        for code, pos in holdings.items():
            stock_data = day_all[day_all['code'] == code]
            if len(stock_data) == 0:
                continue
            row = stock_data.iloc[0]
            current_price = row['close']
            if current_price > pos['highest_price']:
                pos['highest_price'] = current_price
            initial_stop_price = pos['buy_price'] * (1 - INITIAL_STOP_LOSS)
            trailing_stop_price = pos['highest_price'] * (1 - TRAILING_DRAWDOWN)
            current_stop_price = max(initial_stop_price, trailing_stop_price)
            pos['stop_loss_price'] = current_stop_price
            if current_price < current_stop_price:
                reason = 'trailing_stop_loss' if current_stop_price > initial_stop_price else 'initial_stop_loss'
                pending_sells.append((code, reason))
            elif pd.notna(row.get('death_cross')) and row['death_cross']:
                pending_sells.append((code, 'death_cross'))

        # 检查买入信号
        if len(holdings) + len(pending_buys) < MAX_HOLDINGS:
            buy_candidates = day_candidates.sort_values('score', ascending=False)
            for _, row in buy_candidates.iterrows():
                if len(holdings) + len(pending_buys) >= MAX_HOLDINGS:
                    break
                code = row['code']
                if code in holdings or code in [c for c, _ in pending_buys]:
                    continue
                is_golden = pd.notna(row.get('golden_cross')) and row['golden_cross']
                is_vol_breakout = pd.notna(row.get('vol_breakout')) and row['vol_breakout']
                golden_valid = is_golden and pd.notna(row.get('ma20')) and row['close'] > row['ma20']
                if golden_valid and is_vol_breakout:
                    pending_buys.append((code, 'both'))
                elif golden_valid:
                    pending_buys.append((code, 'golden_cross'))
                elif is_vol_breakout:
                    pending_buys.append((code, 'vol_breakout'))

        # 估值
        holdings_value = sum(
            pos['shares'] * day_all[day_all['code'] == code]['close'].values[0]
            for code, pos in holdings.items()
            if len(day_all[day_all['code'] == code]) > 0
        )
        total_value = cash + holdings_value
        portfolio_values.append((date, total_value, cash, len(holdings)))

    return pd.DataFrame(portfolio_values, columns=['date', 'value', 'cash', 'holdings'])


def main():
    print('=' * 70)
    print('技术面择时V5 真正OOS回测（按时间分段训练，消除模型前视偏差）')
    print('=' * 70)

    # 预加载全量行情数据（计算技术指标）
    print('\n预加载行情数据并计算技术指标...')
    mdb = sqlite3.connect(str(PROJECT / 'data' / 'market.db'))
    df_bars_full = pd.read_sql_query(
        'SELECT trade_date, code, open, high, low, close, volume, amount '
        'FROM daily_bars WHERE trade_date >= "20220101" ORDER BY code, trade_date',
        mdb
    )
    mdb.close()
    print(f'  行情数据: {len(df_bars_full)} 行')

    df_bars_full = df_bars_full.sort_values(['code', 'trade_date'])
    df_bars_full['ma5'] = df_bars_full.groupby('code')['close'].transform(lambda x: x.rolling(5).mean())
    df_bars_full['ma10'] = df_bars_full.groupby('code')['close'].transform(lambda x: x.rolling(10).mean())
    df_bars_full['ma20'] = df_bars_full.groupby('code')['close'].transform(lambda x: x.rolling(20).mean())
    df_bars_full['vol_ma5'] = df_bars_full.groupby('code')['volume'].transform(lambda x: x.rolling(5).mean())
    df_bars_full['high_20_prev'] = df_bars_full.groupby('code')['close'].transform(
        lambda x: x.shift(1).rolling(VOL_BREAKOUT_HIGH_WINDOW).max()
    )
    df_bars_full['ma5_prev'] = df_bars_full.groupby('code')['ma5'].shift(1)
    df_bars_full['ma10_prev'] = df_bars_full.groupby('code')['ma10'].shift(1)
    df_bars_full['golden_cross'] = (df_bars_full['ma5_prev'] <= df_bars_full['ma10_prev']) & (df_bars_full['ma5'] > df_bars_full['ma10'])
    df_bars_full['death_cross'] = (df_bars_full['ma5_prev'] >= df_bars_full['ma10_prev']) & (df_bars_full['ma5'] < df_bars_full['ma10'])
    df_bars_full['vol_breakout'] = (
        (df_bars_full['volume'] > df_bars_full['vol_ma5'] * VOL_BREAKOUT_VOL_RATIO) &
        (df_bars_full['close'] >= df_bars_full['high_20_prev']) &
        (df_bars_full['close'] > df_bars_full['ma20'])
    )
    print(f'  技术指标计算完成, 耗时 {time.time()-t0:.1f}s')

    # 预加载全量因子数据
    print('\n预加载因子数据...')
    fdb = sqlite3.connect(str(PROJECT / 'data' / 'factors.db'))
    df_factors_full = pd.read_sql_query(
        'SELECT trade_date, code, close, momentum20, momentum60, trend, drawdown60, '
        'volumeRatio, volSurge3, volSurge5, amountRatio5, volPriceCorr20, turnoverRatio5, '
        'amplitude20, peTtm, pb, psTtm, dvTtm, roe, grossMargin, netMargin, revenueYoy, '
        'netprofitYoy, ocfYoy, mktCap, volatility20, floatRatio '
        'FROM stock_ml_factors WHERE trade_date >= "20220101"',
        fdb
    )
    df_industry = pd.read_sql_query(
        'SELECT trade_date, code, industry_mom20, industry_rank20, industry_excess5, '
        'industry_excess20, industry_amount_chg FROM stock_industry_factors WHERE trade_date >= "20220101"',
        fdb
    )
    df_money = pd.read_sql_query(
        'SELECT trade_date, code, turnover_surge, close_position_5d FROM stock_money_flow_factors WHERE trade_date >= "20220101"',
        fdb
    )
    fdb.close()
    df_factors_full = df_factors_full.merge(df_industry, on=['trade_date', 'code'], how='left')
    df_factors_full = df_factors_full.merge(df_money, on=['trade_date', 'code'], how='left')
    print(f'  因子数据: {len(df_factors_full)} 行, 耗时 {time.time()-t0:.1f}s')

    # 逐段OOS回测
    all_results = []
    segment_stats = []
    for idx, (test_start, test_end, train_end) in enumerate(OOS_SEGMENTS):
        print(f'\n--- OOS段 {idx+1}/{len(OOS_SEGMENTS)}: {test_start} ~ {test_end} (训练截止 {train_end}) ---')
        model = load_or_train_model(idx, train_end)
        df_seg = run_segment_backtest(model, test_start, test_end, df_factors_full, df_bars_full)
        if len(df_seg) > 0:
            seg_return = df_seg['value'].iloc[-1] / df_seg['value'].iloc[0] - 1
            seg_max_dd = (df_seg['value'] / df_seg['value'].cummax() - 1).min()
            print(f'  段收益: {seg_return:.2%}, 最大回撤: {seg_max_dd:.2%}, 交易日: {len(df_seg)}')
            segment_stats.append({
                'segment': idx + 1, 'start': test_start, 'end': test_end,
                'return': seg_return, 'max_drawdown': seg_max_dd, 'days': len(df_seg)
            })
            all_results.append(df_seg)

    # 拼接全周期结果
    if all_results:
        df_all = pd.concat(all_results, ignore_index=True)
        df_all['return'] = df_all['value'].pct_change()
        df_all['cum_return'] = df_all['value'] / INITIAL_CAPITAL - 1

        total_return = df_all['cum_return'].iloc[-1]
        annual_return = (1 + total_return) ** (252 / len(df_all)) - 1
        sharpe = df_all['return'].mean() / df_all['return'].std() * np.sqrt(252) if df_all['return'].std() > 0 else 0
        max_drawdown = (df_all['value'] / df_all['value'].cummax() - 1).min()

        # 保存
        df_all.to_csv(ARTIFACT_DIR / 'technical_timing_v5_oos_backtest.csv', index=False)

        print(f'\n{"="*70}')
        print(f'OOS回测汇总（2024-01-01 ~ 2026-09-11）')
        print(f'{"="*70}')
        print(f'总收益:     {total_return:.2%}')
        print(f'年化收益:   {annual_return:.2%}')
        print(f'Sharpe:     {sharpe:.3f}')
        print(f'最大回撤:   {max_drawdown:.2%}')
        print(f'交易日数:   {len(df_all)}')
        print(f'\n分段收益:')
        for s in segment_stats:
            print(f'  段{s["segment"]} ({s["start"]}~{s["end"]}): {s["return"]:.2%}, 回撤{s["max_drawdown"]:.2%}')
        print(f'\n结果已保存: artifacts/stock-ml/technical_timing_v5_oos_backtest.csv')
        print(f'模型目录:   {OOS_MODEL_DIR}')
        print(f'总耗时:     {time.time()-t0:.1f}s')


if __name__ == '__main__':
    main()
