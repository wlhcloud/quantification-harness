"""股票 ML 的辅助因子表：行业轮动因子 + 资金流向因子（服务内产出，job 化）。

迁移背景：这两张表原先只由 `scripts/compute_industry_factors.py` /
`scripts/compute_money_flow_factors.py` 在**服务之外**手工运行产出，而
`stock_ml.run_walkforward` 会 LEFT JOIN 它们取行业/资金流特征 —— 结果是：
表会静默过期（实测曾落后 2 个交易日），训练用的行业/资金流特征缺失且无任何告警，
且无法通过 job 观察进度/取消/超时。现在计算逻辑搬进引擎、由 job 驱动，
`aux_factor_freshness()` 供 walkforward 入口做新鲜度断言。脚本保留为薄封装。

原子换表：先写 `<表名>_staging`，再在一个事务里 DROP 旧表 + RENAME，避免重建过程中
表短暂缺失/半成品（walkforward 若正好在此时 JOIN 会静默丢特征）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

Progress = Callable[[float], None]
Cancelled = Callable[[], bool]


def _noop_progress(_value: float) -> None:
    return None


def _never_cancelled() -> bool:
    return False


def _swap_table(con: sqlite3.Connection, table: str, frame: pd.DataFrame, index_sql: str | None = None) -> int:
    """把 DataFrame 原子写进 table：先写 staging，再在同一事务里替换。"""
    staging = f"{table}_staging"
    frame.to_sql(staging, con, if_exists="replace", index=False)
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(f"ALTER TABLE {staging} RENAME TO {table}")
    if index_sql:
        con.execute(index_sql)
    con.commit()
    return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _point_in_time_industry(frame: pd.DataFrame, memberships: pd.DataFrame) -> pd.DataFrame:
    """Attach the industry valid on each row's trade date (no current-state backfill)."""
    joined = frame.merge(memberships, on="code", how="inner")
    valid = ((joined["trade_date"] >= joined["valid_from"])
             & (joined["valid_to"].isna() | (joined["trade_date"] <= joined["valid_to"])))
    joined = joined.loc[valid].copy()
    # Bad upstream overlaps must not duplicate a stock/date in factor aggregation.
    joined = joined.sort_values(["code", "trade_date", "valid_from"]).drop_duplicates(
        ["code", "trade_date"], keep="last")
    return joined


def _load_industry_data(market_path: Path, start_date: str) -> tuple:
    """加载个股行情、行业映射、沪深300、涨跌停数据"""
    mdb = sqlite3.connect(market_path)
    try:
        exists = mdb.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='industry_membership_history'"
        ).fetchone()
        if not exists:
            raise RuntimeError(
                "industry_membership_history 不存在；禁止使用 security_master 当前行业回填历史"
            )
        industry_map = pd.read_sql(
            "SELECT code,industry,valid_from,valid_to FROM industry_membership_history", mdb)
        print(f"历史行业成员: {len(industry_map)} 条, {industry_map['industry'].nunique()} 个行业")

        # 个股日线（只取需要的列，限制到最近2年以提高效率）
        bars = pd.read_sql("""SELECT trade_date, code, close, pct_chg, amount 
               FROM daily_bars 
               WHERE trade_date >= ?
               ORDER BY trade_date, code""", mdb, params=(start_date,))
        print(f"个股日线: {len(bars)} 条, {bars['trade_date'].min()} ~ {bars['trade_date'].max()}")

        # 沪深300
        hs300 = pd.read_sql("SELECT trade_date, close, pct_chg FROM index_daily_bars WHERE ts_code='000300.SH' AND trade_date >= ? ORDER BY trade_date", mdb, params=(start_date,))
        hs300 = hs300.rename(columns={'pct_chg': 'hs300_pct'})
        print(f"沪深300: {len(hs300)} 条")

        # 涨跌停
        limits = pd.read_sql(
            "SELECT trade_date, code, limit_type FROM limit_list WHERE trade_date >= ?", mdb,
            params=(start_date,))
    finally:
        # 原脚本在异常路径上不会关闭连接（Windows 上会一直占着 market.db 句柄）
        mdb.close()
    # 只算涨停(U=涨停, Z=涨停?)
    bars = _point_in_time_industry(bars, industry_map)
    limits = _point_in_time_industry(limits, industry_map)
    up_limits = limits[limits['limit_type'].isin(['U', 'Z'])]
    print(f"涨停记录: {len(up_limits)} 条")

    return industry_map, bars, hs300, up_limits

def _compute_industry_factors(industry_map, bars, hs300, up_limits):
    """计算行业级因子"""
    # bars 已按 valid_from/valid_to 关联到当日有效行业。
    df = bars
    print(f"合并行业后: {len(df)} 条")
    
    # 按行业-日期聚合：等权平均涨跌幅、总成交额、股票数量
    ind_daily = df.groupby(['trade_date', 'industry']).agg(
        ind_pct=('pct_chg', 'mean'),
        ind_amount=('amount', 'sum'),
        ind_count=('code', 'nunique')
    ).reset_index()
    
    # 全市场每日总成交额
    total_amount = df.groupby('trade_date')['amount'].sum().reset_index()
    total_amount = total_amount.rename(columns={'amount': 'total_amount'})
    ind_daily = ind_daily.merge(total_amount, on='trade_date', how='left')
    ind_daily['amount_ratio'] = ind_daily['ind_amount'] / ind_daily['total_amount']
    
    # 按行业排序，计算滚动指标
    ind_daily = ind_daily.sort_values(['industry', 'trade_date']).reset_index(drop=True)
    
    # 行业动量
    ind_daily['ind_mom5'] = ind_daily.groupby('industry')['ind_pct'].transform(
        lambda x: (1 + x/100).rolling(5).apply(np.prod, raw=True) - 1
    ) * 100
    ind_daily['ind_mom20'] = ind_daily.groupby('industry')['ind_pct'].transform(
        lambda x: (1 + x/100).rolling(20).apply(np.prod, raw=True) - 1
    ) * 100
    
    # 行业成交额占比20日均
    ind_daily['amount_ratio_ma20'] = ind_daily.groupby('industry')['amount_ratio'].transform(
        lambda x: x.rolling(20).mean()
    )
    ind_daily['industry_amount_chg'] = (ind_daily['amount_ratio'] / ind_daily['amount_ratio_ma20'] - 1) * 100
    
    # 沪深300滚动收益。指数数据可能比个股日线晚到几个交易日（真实库曾出现
    # daily_bars=20260918、000300.SH=20260911），直接按日期 left join 会让最新日
    # industry_excess5/20 全空，进而阻断发布。这里仅对基准动量使用 asof 前向对齐：
    # 用样本日之前最近一个已知基准值，不向未来取数，保持 PIT。
    hs300_sorted = hs300.sort_values('trade_date')
    hs300_sorted['hs300_mom5'] = (1 + hs300_sorted['hs300_pct']/100).rolling(5).apply(np.prod, raw=True) - 1
    hs300_sorted['hs300_mom20'] = (1 + hs300_sorted['hs300_pct']/100).rolling(20).apply(np.prod, raw=True) - 1
    ind_daily['_trade_date_i'] = ind_daily['trade_date'].astype(int)
    hs300_for_merge = hs300_sorted[['trade_date', 'hs300_pct', 'hs300_mom5', 'hs300_mom20']].copy()
    hs300_for_merge['_trade_date_i'] = hs300_for_merge['trade_date'].astype(int)
    ind_daily = pd.merge_asof(
        ind_daily.sort_values('_trade_date_i'),
        hs300_for_merge.drop(columns=['trade_date']).sort_values('_trade_date_i'),
        on='_trade_date_i',
        direction='backward',
    ).sort_values(['industry', 'trade_date']).reset_index(drop=True)
    ind_daily = ind_daily.drop(columns=['_trade_date_i'])
    
    # 行业超额收益
    ind_daily['industry_excess5'] = ind_daily['ind_mom5'] - ind_daily['hs300_mom5'] * 100
    ind_daily['industry_excess20'] = ind_daily['ind_mom20'] - ind_daily['hs300_mom20'] * 100
    
    # 行业排名分位（每日，按行业涨幅排名）
    ind_daily['industry_rank5'] = ind_daily.groupby('trade_date')['ind_mom5'].transform(
        lambda x: x.rank(pct=True)
    )
    ind_daily['industry_rank20'] = ind_daily.groupby('trade_date')['ind_mom20'].transform(
        lambda x: x.rank(pct=True)
    )
    
    # 涨停统计
    up_daily = up_limits.groupby(['trade_date', 'industry']).size().reset_index(name='limit_count')
    ind_daily = ind_daily.merge(up_daily, on=['trade_date', 'industry'], how='left')
    ind_daily['industry_limit_count'] = ind_daily['limit_count'].fillna(0)
    ind_daily['industry_limit_ratio'] = ind_daily['industry_limit_count'] / ind_daily['ind_count'] * 100
    
    print(f"行业级因子: {len(ind_daily)} 条")
    print(f"日期范围: {ind_daily['trade_date'].min()} ~ {ind_daily['trade_date'].max()}")
    print(f"列: {list(ind_daily.columns)}")
    
    return ind_daily

def _map_to_stocks(ind_daily, industry_map, bars):
    """把行业级因子映射回个股，并计算个股vs行业相对动量"""
    # 个股20日动量
    bars_sorted = bars.sort_values(['code', 'trade_date'])
    bars_sorted['stock_mom20'] = bars_sorted.groupby('code')['pct_chg'].transform(
        lambda x: (1 + x/100).rolling(20).apply(np.prod, raw=True) - 1
    ) * 100
    
    # bars 已带有当日有效行业，不能再连接当前行业快照。
    stock_df = bars_sorted
    
    # 合并行业因子
    factor_cols = ['trade_date', 'industry', 'ind_mom5', 'ind_mom20', 'industry_rank5', 'industry_rank20',
                    'industry_excess5', 'industry_excess20', 'industry_limit_count', 'industry_limit_ratio',
                    'industry_amount_chg']
    stock_df = stock_df.merge(ind_daily[factor_cols], on=['trade_date', 'industry'], how='left')
    
    # 个股vs行业相对动量
    stock_df['stock_vs_industry_mom20'] = stock_df['stock_mom20'] - stock_df['ind_mom20']
    
    # 重命名列
    stock_df = stock_df.rename(columns={
        'ind_mom5': 'industry_mom5',
        'ind_mom20': 'industry_mom20',
    })
    
    # 只保留需要的列
    out_cols = ['trade_date', 'code', 'industry_mom5', 'industry_mom20', 'industry_rank5', 'industry_rank20',
                'industry_excess5', 'industry_excess20', 'industry_limit_count', 'industry_limit_ratio',
                'industry_amount_chg', 'stock_vs_industry_mom20']
    result = stock_df[out_cols].copy()
    
    # 去掉全空行（前20天滚动窗口为空）
    result = result.dropna(subset=['industry_mom20'])
    
    print(f"个股级行业因子: {len(result)} 条")
    print(f"日期范围: {result['trade_date'].min()} ~ {result['trade_date'].max()}")
    print(f"股票数量: {result['code'].nunique()}")
    
    return result

def _save_industry_factors(result, factors_path: Path) -> int:
    """保存到 factors.db 的新表 stock_industry_factors"""
    fdb = sqlite3.connect(factors_path)
    
    _swap_table(fdb, 'stock_industry_factors', result,
                "CREATE INDEX IF NOT EXISTS idx_ind_code_date ON stock_industry_factors(code, trade_date)")
    
    cnt = fdb.execute("SELECT COUNT(*) FROM stock_industry_factors").fetchone()[0]
    md = fdb.execute("SELECT MIN(trade_date), MAX(trade_date) FROM stock_industry_factors").fetchone()
    nc = fdb.execute("SELECT COUNT(DISTINCT code) FROM stock_industry_factors").fetchone()[0]
    print(f"\n写入完成: {cnt} 条, {nc} 只股票, {md[0]} ~ {md[1]}")
    
    # 抽样验证
    sample = fdb.execute("""
        SELECT * FROM stock_industry_factors 
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_industry_factors)
        ORDER BY industry_mom5 DESC LIMIT 5
    """).fetchall()
    print("\n最新日期行业动量Top5:")
    for row in sample:
        def _display(value: Any, digits: int = 2) -> str:
            return "NA" if value is None else f"{float(value):.{digits}f}"
        print(f"  {row[1]}: ind_mom5={_display(row[2])}%, rank5={_display(row[4])}, "
              f"excess5={_display(row[6])}%, limit_cnt={_display(row[8], 0)}")

    fdb.close()
    return int(cnt)

def build_industry_factors(market_path: Path, factors_path: Path, *,
                           start_date: str = "20240101",
                           progress: Progress = _noop_progress,
                           cancelled: Cancelled = _never_cancelled) -> dict[str, Any]:
    """重建 stock_industry_factors（个股级行业轮动因子）。"""
    progress(5)
    industry_map, bars, hs300, up_limits = _load_industry_data(market_path, start_date)
    if cancelled():
        raise RuntimeError("stock_industry_factors cancelled")
    progress(35)
    ind_daily = _compute_industry_factors(industry_map, bars, hs300, up_limits)
    progress(60)
    result = _map_to_stocks(ind_daily, industry_map, bars)
    if cancelled():
        raise RuntimeError("stock_industry_factors cancelled")
    progress(80)
    rows = _save_industry_factors(result, factors_path)
    progress(100)
    return {"table": "stock_industry_factors", "rows": int(rows),
            "codes": int(result["code"].nunique()),
            "startDate": str(result["trade_date"].min()),
            "endDate": str(result["trade_date"].max())}


def _build_money_flow_factors_pandas(market_path: Path, factors_path: Path, start_date: str = "20240101", progress: Progress = _noop_progress, cancelled: Cancelled = _never_cancelled) -> dict[str, Any]:
    progress(5)

    mdb = sqlite3.connect(market_path)

    # 加载个股日线（最近2年）
    print("\n[1/3] 加载个股日线...")
    df = pd.read_sql("""SELECT trade_date, code, open, high, low, close, pre_close, volume, amount, pct_chg
           FROM daily_bars WHERE trade_date >= ? ORDER BY code, trade_date""", mdb, params=(start_date,))
    print(f"  {len(df)} 条, {df['code'].nunique()} 只股票, {df['trade_date'].min()} ~ {df['trade_date'].max()}")
    mdb.close()

    print("\n[2/3] 计算资金流向因子...")
    df = df.sort_values(['code', 'trade_date']).reset_index(drop=True)

    # 基础指标
    df['range'] = df['high'] - df['low']
    df['body'] = df['close'] - df['open']
    df['is_up'] = (df['close'] > df['open']).astype(int)

    # 1. 单日资金流向：(收-开)/开 * 成交量（放量涨=正流入，放量跌=负流出）
    df['money_flow_1d'] = (df['body'] / df['open'].replace(0, np.nan)) * df['volume']

    # 2. 5日资金流向累计
    df['money_flow_5d'] = df.groupby('code')['money_flow_1d'].transform(
        lambda x: x.rolling(5).sum()
    )

    # 3. 5日资金流向强度 = 5日资金流向 / 5日成交额
    df['amount_5d'] = df.groupby('code')['amount'].transform(lambda x: x.rolling(5).sum())
    df['money_flow_ratio_5d'] = (df['money_flow_5d'] / df['amount_5d'].replace(0, np.nan)) * 100

    # 4. 量价背离：价格5日涨跌幅为正但成交量5日均量 < 20日均量 → 顶背离(负)
    #    价格5日涨跌幅为负但成交量5日均量 < 20日均量 → 底背离(正)
    df['price_chg_5d'] = df.groupby('code')['close'].transform(lambda x: x.pct_change(5) * 100)
    df['vol_ma5'] = df.groupby('code')['volume'].transform(lambda x: x.rolling(5).mean())
    df['vol_ma20'] = df.groupby('code')['volume'].transform(lambda x: x.rolling(20).mean())
    df['vol_ratio'] = df['vol_ma5'] / df['vol_ma20'].replace(0, np.nan)
    # 背离得分：价涨量缩 = -1（顶背离），价跌量缩 = +1（底背离），其他 = 0
    conditions = [
        (df['price_chg_5d'] > 0) & (df['vol_ratio'] < 0.9),
        (df['price_chg_5d'] < 0) & (df['vol_ratio'] < 0.9),
    ]
    choices = [-1.0, 1.0]
    df['volume_price_div'] = np.select(conditions, choices, default=0.0)

    # 5. 换手率突变：近5日平均换手率 / 近20日平均换手率 - 1
    #    用 amount/mktCap 近似换手率（没有流通股本数据，用成交额相对变化代理）
    df['turnover_5d'] = df.groupby('code')['amount'].transform(lambda x: x.rolling(5).mean())
    df['turnover_20d'] = df.groupby('code')['amount'].transform(lambda x: x.rolling(20).mean())
    df['turnover_surge'] = (df['turnover_5d'] / df['turnover_20d'].replace(0, np.nan) - 1) * 100

    # 6. 大振幅+大成交量：当日振幅 * 量比（资金博弈激烈程度）
    #    修复：原先 SELECT 不含 pre_close，回退分支用未按 code 分组的 close.shift(1)，
    #    会把上一只股票的收盘价当成当只的 pre_close（每只首行串味）。现在直接取 pre_close。
    if 'pre_close' in df.columns:
        df['amplitude'] = (df['range'] / df['pre_close'].replace(0, np.nan)) * 100
    else:
        df['amplitude'] = (df['range'] / df.groupby('code')['close'].shift(1).replace(0, np.nan)) * 100
    df['volume_ratio'] = df['volume'] / df['vol_ma20'].replace(0, np.nan)
    df['large_move_volume'] = df['amplitude'] * df['volume_ratio']

    # 7. 收盘价在K线中的位置：(收-低)/(高-低)，高=收盘强（资金尾盘抢筹）
    df['close_position'] = np.where(
        df['range'] > 0,
        (df['close'] - df['low']) / df['range'],
        0.5
    )

    # 8. 5日平均收盘位置
    df['close_position_5d'] = df.groupby('code')['close_position'].transform(
        lambda x: x.rolling(5).mean()
    )

    # 9. 上涨日成交量占比：近5日上涨日成交量 / 近5日总成交量
    df['up_volume'] = df['is_up'] * df['volume']
    df['up_volume_5d'] = df.groupby('code')['up_volume'].transform(lambda x: x.rolling(5).sum())
    df['total_volume_5d'] = df.groupby('code')['volume'].transform(lambda x: x.rolling(5).sum())
    df['up_volume_ratio'] = (df['up_volume_5d'] / df['total_volume_5d'].replace(0, np.nan)) * 100

    # 去掉前20天（滚动窗口为空）
    result = df.dropna(subset=['money_flow_ratio_5d', 'turnover_surge', 'close_position_5d']).copy()

    # 只保留需要的列
    out_cols = ['trade_date', 'code', 'money_flow_1d', 'money_flow_5d', 'money_flow_ratio_5d',
                'volume_price_div', 'turnover_surge', 'large_move_volume',
                'close_position', 'close_position_5d', 'up_volume_ratio']
    result = result[out_cols]

    # 替换 inf/nan
    result = result.replace([np.inf, -np.inf], np.nan)

    print(f"  计算完成: {len(result)} 条, {result['code'].nunique()} 只股票")
    print(f"  日期范围: {result['trade_date'].min()} ~ {result['trade_date'].max()}")

    print("\n[3/3] 写入数据库...")
    if cancelled():
        raise RuntimeError("stock_money_flow_factors cancelled")
    progress(80)
    fdb = sqlite3.connect(factors_path)
    progress(85)
    rows = _swap_table(fdb, 'stock_money_flow_factors', result,
                       "CREATE INDEX IF NOT EXISTS idx_mf_code_date ON stock_money_flow_factors(code, trade_date)")

    cnt = fdb.execute("SELECT COUNT(*) FROM stock_money_flow_factors").fetchone()[0]
    nc = fdb.execute("SELECT COUNT(DISTINCT code) FROM stock_money_flow_factors").fetchone()[0]
    print(f"  写入完成: {cnt} 条, {nc} 只股票")

    # 抽样验证
    sample = fdb.execute("""
        SELECT code, money_flow_ratio_5d, turnover_surge, close_position_5d, up_volume_ratio
        FROM stock_money_flow_factors
        WHERE trade_date = (SELECT MAX(trade_date) FROM stock_money_flow_factors)
        ORDER BY money_flow_ratio_5d DESC LIMIT 5
    """).fetchall()
    print("\n  最新日期资金流入Top5:")
    for row in sample:
        print(f"    {row[0]}: mf_ratio={row[1]:.2f}% turnover_surge={row[2]:.1f}% close_pos={row[3]:.2f} up_vol={row[4]:.1f}%")

    fdb.close()
    progress(100)
    return {"table": "stock_money_flow_factors", "rows": int(rows),
            "codes": int(result["code"].nunique()),
            "startDate": str(result["trade_date"].min()),
            "endDate": str(result["trade_date"].max())}


def build_money_flow_factors(market_path: Path, factors_path: Path, start_date: str = "20210101",
                             progress: Progress = _noop_progress,
                             cancelled: Cancelled = _never_cancelled) -> dict[str, Any]:
    """用 SQLite 窗口函数重建资金流因子，避免全表载入 Pandas 导致 OOM/超时。"""
    progress(5)
    con = sqlite3.connect(factors_path, timeout=60)
    staging = "stock_money_flow_factors_staging"
    try:
        con.execute("ATTACH DATABASE ? AS market", (str(Path(market_path)),))

        def _check_cancel() -> int:
            return 1 if cancelled() else 0

        # 每约十万条 VM 指令检查一次取消，长 SQL 不再两小时无响应。
        con.set_progress_handler(_check_cancel, 100_000)
        con.execute(f"DROP TABLE IF EXISTS {staging}")
        con.execute(f"""
            CREATE TABLE {staging} AS
            WITH base AS (
              SELECT trade_date,code,open,high,low,close,pre_close,volume,amount,
                     CASE WHEN open IS NOT NULL AND open<>0
                          THEN (close-open)/open*volume END money_flow_1d,
                     CASE WHEN high<>low THEN (close-low)/(high-low) ELSE 0.5 END close_position,
                     CASE WHEN close>open THEN volume ELSE 0 END up_volume,
                     ROW_NUMBER() OVER (PARTITION BY code ORDER BY trade_date) rn
              FROM market.daily_bars WHERE trade_date>=?
            ), rolls AS (
              SELECT *,
                SUM(money_flow_1d) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) money_flow_5d,
                SUM(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) amount_5d,
                LAG(close,5) OVER (PARTITION BY code ORDER BY trade_date) close_5d,
                AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) vol_ma5,
                AVG(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) vol_ma20,
                AVG(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) turnover_5d,
                AVG(amount) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) turnover_20d,
                AVG(close_position) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) close_position_5d,
                SUM(up_volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) up_volume_5d,
                SUM(volume) OVER (PARTITION BY code ORDER BY trade_date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) total_volume_5d
              FROM base
            ), calc AS (
              SELECT trade_date,code,money_flow_1d,money_flow_5d,
                money_flow_5d/NULLIF(amount_5d,0)*100 money_flow_ratio_5d,
                CASE
                  WHEN close_5d IS NOT NULL AND close>close_5d AND vol_ma5/NULLIF(vol_ma20,0)<0.9 THEN -1.0
                  WHEN close_5d IS NOT NULL AND close<close_5d AND vol_ma5/NULLIF(vol_ma20,0)<0.9 THEN 1.0
                  ELSE 0.0 END volume_price_div,
                (turnover_5d/NULLIF(turnover_20d,0)-1)*100 turnover_surge,
                ((high-low)/NULLIF(pre_close,0)*100)*(volume/NULLIF(vol_ma20,0)) large_move_volume,
                close_position,close_position_5d,
                up_volume_5d/NULLIF(total_volume_5d,0)*100 up_volume_ratio,
                rn
              FROM rolls
            )
            SELECT trade_date,code,money_flow_1d,money_flow_5d,money_flow_ratio_5d,
                   volume_price_div,turnover_surge,large_move_volume,close_position,
                   close_position_5d,up_volume_ratio
            FROM calc WHERE rn>=20
        """, (start_date,))
        progress(80)
        if cancelled():
            raise RuntimeError("stock_money_flow_factors cancelled")
        con.set_progress_handler(None, 0)
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        con.execute("DROP TABLE IF EXISTS stock_money_flow_factors")
        con.execute(f"ALTER TABLE {staging} RENAME TO stock_money_flow_factors")
        con.execute("CREATE INDEX idx_mf_code_date ON stock_money_flow_factors(code,trade_date)")
        con.commit()
        row = con.execute(
            "SELECT COUNT(*) rows,COUNT(DISTINCT code) codes,MIN(trade_date) start_date,MAX(trade_date) end_date "
            "FROM stock_money_flow_factors").fetchone()
        progress(100)
        return {"table": "stock_money_flow_factors", "rows": int(row[0]), "codes": int(row[1]),
                "startDate": str(row[2]), "endDate": str(row[3])}
    except sqlite3.OperationalError as error:
        if cancelled() and "interrupt" in str(error).lower():
            raise RuntimeError("stock_money_flow_factors cancelled") from error
        raise
    finally:
        con.set_progress_handler(None, 0)
        con.close()



def aux_factor_freshness(factors_path: Path, signal_date: str | None, *,
                         max_lag_days: int = 0,
                         required_features: list[str] | None = None) -> dict[str, Any]:
    """辅助因子表的新鲜度断言（供 walkforward 入口与只读端点使用）。

    返回 {signalDate, tables: {表名: 最新日期}, warnings: [...]}。
    `max_lag_days=0` 表示要求与信号日同日；落后则给出可见告警（不阻塞训练，
    因为模型对 NaN 有处理，但必须让人知道特征缺了）。
    """
    feature_set = set(required_features or [])
    industry_features = {
        "industry_mom5", "industry_mom20", "industry_rank5", "industry_rank20",
        "industry_excess5", "industry_excess20", "industry_limit_count",
        "industry_limit_ratio", "industry_amount_chg", "stock_vs_industry_mom20",
    }
    money_flow_features = {
        "money_flow_1d", "money_flow_5d", "money_flow_ratio_5d", "volume_price_div",
        "turnover_surge", "large_move_volume", "close_position", "close_position_5d",
        "up_volume_ratio",
    }
    tables = {}
    if required_features is None or feature_set.intersection({
        *industry_features,
    }):
        tables["stock_industry_factors"] = (
            "行业轮动因子", industry_features if required_features is None
            else feature_set.intersection(industry_features))
    if required_features is None or feature_set.intersection({
        *money_flow_features,
    }):
        tables["stock_money_flow_factors"] = (
            "资金流向因子", money_flow_features if required_features is None
            else feature_set.intersection(money_flow_features))
    latest: dict[str, str | None] = {}
    quality: dict[str, Any] = {}
    warnings: list[str] = []
    con = sqlite3.connect(f"file:{Path(factors_path).as_posix()}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        if signal_date is None:
            # 未显式给信号日时，以 stock_ml_factors 的最新日期为基准（表不存在则跳过基准）
            has_ref = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_ml_factors'").fetchone()
            if has_ref:
                row = con.execute("SELECT MAX(trade_date) d FROM stock_ml_factors").fetchone()
                signal_date = row["d"] if row else None
        for table, (label, expected_features) in tables.items():
            exists = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
            if not exists:
                latest[table] = None
                warnings.append(f"{label}表 {table} 不存在：模型相关特征将全为空")
                continue
            row = con.execute(f"SELECT COUNT(*) n, MAX(trade_date) d FROM {table}").fetchone()
            latest[table] = row["d"]
            if not row["n"]:
                warnings.append(f"{label}表 {table} 为空：模型相关特征将全为空")
                continue
            if signal_date and row["d"] and row["d"] < signal_date:
                warnings.append(
                    f"{label}表 {table} 最新 {row['d']} 落后信号日 {signal_date}"
                    f"（可在计算面重跑 stock_industry_factors / stock_money_flow_factors）")
            check_date = signal_date if signal_date and row["d"] >= signal_date else row["d"]
            columns = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
            missing_columns = sorted(expected_features - columns)
            available = sorted(expected_features.intersection(columns))
            q: dict[str, Any] = {"tradeDate": check_date, "rows": 0, "fields": {}}
            if missing_columns:
                warnings.append(f"{label}表 {table} 缺少必需字段：{', '.join(missing_columns)}")
            if check_date:
                count_row = con.execute(
                    f"SELECT COUNT(*) n FROM {table} WHERE trade_date=?", (check_date,)).fetchone()
                q["rows"] = int(count_row["n"] or 0)
                if not q["rows"]:
                    warnings.append(f"{label}表 {table} 在信号日 {check_date} 没有数据")
                elif available:
                    expressions = ",".join(
                        f'SUM(CASE WHEN "{field}" IS NOT NULL THEN 1 ELSE 0 END) AS "{field}"'
                        for field in available)
                    coverage_row = con.execute(
                        f"SELECT {expressions} FROM {table} WHERE trade_date=?", (check_date,)).fetchone()
                    for field in available:
                        non_null = int(coverage_row[field] or 0)
                        coverage = non_null / q["rows"]
                        q["fields"][field] = {"nonNull": non_null, "coverage": round(coverage, 6)}
                        if coverage < 0.95:
                            warnings.append(
                                f"{label}字段 {field} 在 {check_date} 覆盖率仅 {coverage:.1%}"
                                f"（{non_null}/{q['rows']}）")
            quality[table] = q
    finally:
        con.close()
    return {"signalDate": signal_date, "tables": latest, "quality": quality,
            "warnings": warnings}
