"""Persistence for the ETF Quant MVP (etf-quant.db, additive; no stock tables touched)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS etf_factor_snapshots (
  ts_code TEXT NOT NULL, trade_date TEXT NOT NULL,
  mom20 REAL, mom60 REAL, mom120 REAL, rs20 REAL, rs60 REAL,
  trend_quality REAL, ma_alignment REAL, vol_ratio REAL, amount_trend REAL,
  volatility20 REAL, drawdown60 REAL, log_amount20 REAL,
  rs60_vol_adj REAL, mom_accel REAL, skew20 REAL, drawdown20 REAL, amount_share REAL,
  ind_rs REAL,
  forward_5 REAL, forward_20 REAL,
  PRIMARY KEY(ts_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_etf_snap_date ON etf_factor_snapshots(trade_date);

CREATE TABLE IF NOT EXISTS etf_ic_research (
  run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, label TEXT NOT NULL,
  window_start TEXT, window_end TEXT, days INTEGER, results TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS etf_model_runs (
  run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, label TEXT NOT NULL,
  train_start TEXT, train_end TEXT, valid_start TEXT, valid_end TEXT,
  test_start TEXT, test_end TEXT, rank_ic REAL, ic_mean REAL, icir REAL,
  model_path TEXT, params TEXT, status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS etf_backtest_runs (
  run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, label TEXT NOT NULL,
  start_date TEXT, end_date TEXT, top_n INTEGER, rebalance_days INTEGER,
  cost_bps INTEGER, total_return REAL, benchmark_return REAL, excess_return REAL,
  annual_return REAL, annual_vol REAL, sharpe REAL, max_drawdown REAL,
  turnover_rate REAL, trades INTEGER, status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS etf_backtest_daily (
  run_id TEXT NOT NULL, trade_date TEXT NOT NULL, nav REAL, benchmark REAL,
  PRIMARY KEY(run_id, trade_date)
);

CREATE TABLE IF NOT EXISTS etf_backtest_trades (
  run_id TEXT NOT NULL, rebalance_date TEXT NOT NULL, ts_code TEXT NOT NULL,
  weight REAL, entry_price REAL, exit_date TEXT, exit_price REAL,
  ret REAL, cost REAL
);

CREATE TABLE IF NOT EXISTS etf_walkforward_runs (
  run_id TEXT PRIMARY KEY, generated_at TEXT NOT NULL, label TEXT NOT NULL,
  start_date TEXT, end_date TEXT, windows INTEGER,
  config TEXT NOT NULL, metrics TEXT NOT NULL, holdings TEXT NOT NULL,
  status TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS etf_walkforward_daily (
  run_id TEXT NOT NULL, trade_date TEXT NOT NULL, nav REAL, benchmark REAL,
  PRIMARY KEY(run_id, trade_date)
);

CREATE TABLE IF NOT EXISTS etf_industry_map (
  ts_code TEXT PRIMARY KEY, industry TEXT, index_code TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.isolation_level = None  # autocommit; explicit BEGIN/COMMIT in writers
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    con.commit()
    return con


def write_factor_snapshots(con: sqlite3.Connection, frame) -> int:
    rows = frame.to_dict("records")
    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM etf_factor_snapshots")  # derived table: full rebuild per run
        written = 0
        for r in rows:
            con.execute(
                """INSERT OR REPLACE INTO etf_factor_snapshots(ts_code,trade_date,mom20,mom60,mom120,rs20,rs60,
                   trend_quality,ma_alignment,vol_ratio,amount_trend,volatility20,drawdown60,log_amount20,
                   rs60_vol_adj,mom_accel,skew20,drawdown20,amount_share,ind_rs,
                   forward_5,forward_20) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["ts_code"], r["trade_date"], r.get("mom20"), r.get("mom60"), r.get("mom120"),
                 r.get("rs20"), r.get("rs60"), r.get("trend_quality"), r.get("ma_alignment"),
                 r.get("vol_ratio"), r.get("amount_trend"), r.get("volatility20"), r.get("drawdown60"),
                 r.get("log_amount20"), r.get("rs60_vol_adj"), r.get("mom_accel"), r.get("skew20"),
                 r.get("drawdown20"), r.get("amount_share"), r.get("ind_rs"),
                 r.get("forward_5"), r.get("forward_20")))
            written += 1
        con.commit()
    except Exception:
        con.rollback()
        raise
    return written


def load_factor_frame(con: sqlite3.Connection, label: str, min_days: int) -> list[dict]:
    """Cross-sectional frame: one row per (ts_code, trade_date), forward label attached."""
    rows = con.execute("""SELECT ts_code,trade_date,mom20,mom60,mom120,rs20,rs60,trend_quality,
       ma_alignment,vol_ratio,amount_trend,volatility20,drawdown60,log_amount20,
       rs60_vol_adj,mom_accel,skew20,drawdown20,amount_share,ind_rs,forward_5,forward_20
       FROM etf_factor_snapshots WHERE %s IS NOT NULL ORDER BY trade_date, ts_code""" % label).fetchall()
    return [dict(r) for r in rows]
