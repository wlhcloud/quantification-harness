-- Point-in-time research tables required for bias-controlled backtests.
-- These tables deliberately keep a date dimension; current security_master must
-- never be used as a historical universe.

CREATE TABLE IF NOT EXISTS security_status_history (
  code          TEXT NOT NULL,
  trade_date    TEXT NOT NULL,
  name          TEXT,
  industry      TEXT,
  list_status   TEXT,
  is_st         INTEGER NOT NULL DEFAULT 0,
  is_suspended  INTEGER NOT NULL DEFAULT 0,
  limit_up      REAL,
  limit_down    REAL,
  PRIMARY KEY (code, trade_date)
);

CREATE TABLE IF NOT EXISTS adjustment_factors (
  code          TEXT NOT NULL,
  trade_date    TEXT NOT NULL,
  adj_factor    REAL NOT NULL CHECK (adj_factor > 0),
  source        TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (code, trade_date)
);

CREATE INDEX IF NOT EXISTS idx_security_status_date
  ON security_status_history(trade_date, code);
CREATE INDEX IF NOT EXISTS idx_adjustment_factor_date
  ON adjustment_factors(trade_date, code);
