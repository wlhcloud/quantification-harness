"""ETF / index daily sync — additive module; the stock pipeline is untouched.

Owns (in market.db):
  etf_metadata      — ETF universe master (fund_basic, market='E')
  etf_daily_bars    — ETF daily bars (fund_daily)
  index_daily_bars  — index daily bars (index_daily)

All schema creation uses CREATE TABLE IF NOT EXISTS; existing tables and code
paths (daily_bars / security_master / minute / finance) are never modified.
"""
from __future__ import annotations

import re
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .meta import MetaStore
from .upstream import TushareClient, UpstreamError
from .util import now_iso, open_wal, sql_value

DATE_RE = re.compile(r"^\d{8}$")
ETF_NAME_RE = re.compile(r"ETF", re.IGNORECASE)
EXCLUDED_FUND_TYPES = {"货币型"}


def _date_str(value: Any) -> str | None:
    """Tushare returns YYYYMMDD floats (20150610.0) or None; normalize to 'YYYYMMDD'."""
    if value is None or value == "":
        return None
    try:
        text = str(int(float(value)))
    except (TypeError, ValueError):
        return None
    return text if len(text) == 8 else None


class EtfSync:
    """ETF universe + daily bars + index daily bars sync against Tushare channels."""

    def __init__(self, db_path: Path, client: TushareClient, meta: MetaStore,
                 audit: AuditStore | None = None):
        self.client = client
        self.meta = meta
        self.audit = audit
        self.db_path = db_path
        self.db = open_wal(db_path)
        # 超时覆盖不走对象属性：ETF 的 daily / index 两个入口各持一把锁但共用本对象，
        # 属性会被并发覆盖；改为把 timeout_ms 作为参数沿调用链传入（见 sync_*_history）。
        self._create_schema()
        self.universe = {"status": "idle", "count": 0, "eligible": 0, "excluded": 0,
                         "latestAt": None, "startedAt": None, "finishedAt": None, "error": None, "runId": None}
        self.daily = {"status": "idle", "totalDays": 0, "completedDays": 0, "failedDays": 0,
                      "gapDates": [], "currentDate": None, "written": 0,
                      "startedAt": None, "finishedAt": None, "error": None, "runId": None}
        self.index = {"status": "idle", "totalDays": 0, "completedDays": 0, "failedDays": 0,
                      "gapDates": [], "currentDate": None, "written": 0,
                      "startedAt": None, "finishedAt": None, "error": None, "runId": None}
        self._restore_state()

    # ---- schema (additive only) ----
    def _create_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS etf_metadata (
              ts_code TEXT PRIMARY KEY, name TEXT, management TEXT, custodian TEXT,
              fund_type TEXT, invest_type TEXT, benchmark TEXT,
              list_date TEXT, found_date TEXT, issue_date TEXT, delist_date TEXT,
              m_fee REAL, c_fee REAL, p_value REAL, duration_year REAL, market TEXT,
              eligible INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS etf_daily_bars (
              ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL,
              close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
              source TEXT, PRIMARY KEY(ts_code, trade_date)
            );
            CREATE TABLE IF NOT EXISTS index_daily_bars (
              ts_code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL,
              close REAL, pre_close REAL, change REAL, pct_chg REAL, vol REAL, amount REAL,
              source TEXT, PRIMARY KEY(ts_code, trade_date)
            );
            CREATE INDEX IF NOT EXISTS idx_etf_daily_date ON etf_daily_bars(trade_date);
            CREATE INDEX IF NOT EXISTS idx_index_daily_date ON index_daily_bars(trade_date);
        """)
        self.db.commit()

    def _restore_state(self) -> None:
        latest = self.audit.latest(kind="etf.universe") if self.audit else None
        if latest:
            self.universe.update({"status": latest["status"], "count": latest.get("total") or 0,
                                  "latestAt": latest.get("finishedAt"), "error": latest.get("error"),
                                  "runId": latest["id"]})
        for key, kind, table in (("daily", "etf.daily", "etf_daily_bars"),
                                 ("index", "etf.index", "index_daily_bars")):
            state = self.daily if key == "daily" else self.index
            latest = self.audit.latest(kind=kind) if self.audit else None
            if latest:
                details = latest.get("qualityDetails") or {}
                state.update({"status": latest["status"], "totalDays": latest["total"],
                              "completedDays": latest["completed"], "failedDays": latest["failed"],
                              "gapDates": details.get("gapDates", []), "runId": latest["id"],
                              "startedAt": latest["startedAt"], "finishedAt": latest["finishedAt"],
                              "error": latest["error"]})
            else:
                row = self.db.execute(
                    "SELECT COUNT(*) c, MAX(trade_date) latest FROM " + table).fetchone()
                if row and row["c"]:
                    state.update({"status": "complete", "totalDays": 1, "completedDays": 1,
                                  "latestDate": row["latest"]})

    # ---- helpers ----
    def _count(self, table: str) -> int:
        return self.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    def _is_eligible(self, row: dict[str, Any]) -> bool:
        name = row.get("name") or ""
        if not ETF_NAME_RE.search(name):
            return False
        if not _date_str(row.get("list_date")):
            return False
        if _date_str(row.get("delist_date")):
            return False
        if (row.get("fund_type") or "") in EXCLUDED_FUND_TYPES:
            return False
        return True

    def _upsert_universe(self, rows: list[dict[str, Any]], updated_at: str) -> int:
        written = 0
        for row in rows:
            self.db.execute(
                """INSERT INTO etf_metadata(ts_code,name,management,custodian,fund_type,invest_type,benchmark,
                     list_date,found_date,issue_date,delist_date,m_fee,c_fee,p_value,duration_year,market,
                     eligible,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(ts_code) DO UPDATE SET name=excluded.name,management=excluded.management,
                     custodian=excluded.custodian,fund_type=excluded.fund_type,invest_type=excluded.invest_type,
                     benchmark=excluded.benchmark,list_date=excluded.list_date,found_date=excluded.found_date,
                     issue_date=excluded.issue_date,delist_date=excluded.delist_date,m_fee=excluded.m_fee,
                     c_fee=excluded.c_fee,p_value=excluded.p_value,duration_year=excluded.duration_year,
                     market=excluded.market,eligible=excluded.eligible,updated_at=excluded.updated_at""",
                (sql_value(row.get("ts_code")), sql_value(row.get("name")), sql_value(row.get("management")),
                 sql_value(row.get("custodian")), sql_value(row.get("fund_type")), sql_value(row.get("invest_type")),
                 sql_value(row.get("benchmark")), _date_str(row.get("list_date")), _date_str(row.get("found_date")),
                 _date_str(row.get("issue_date")), _date_str(row.get("delist_date")),
                 sql_value(row.get("m_fee")), sql_value(row.get("c_fee")), sql_value(row.get("p_value")),
                 sql_value(row.get("duration_year")), sql_value(row.get("market")),
                 1 if self._is_eligible(row) else 0, updated_at))
            written += 1
        return written

    # ---- universe ----
    def sync_universe(self) -> dict[str, Any]:
        now = now_iso()
        run_id = self.audit.start("etf.universe", {"market": "E", "source": "fund_basic"}) if self.audit else None
        self.universe.update({"status": "running", "startedAt": now, "finishedAt": None, "error": None, "runId": run_id})
        try:
            result = self.client.call("fund_basic", {"market": "E", "limit": "6000"})
            rows = result.rows
            self.db.execute("BEGIN")
            try:
                written = self._upsert_universe(rows, now)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            total = self._count("etf_metadata")
            eligible = self.db.execute("SELECT COUNT(*) c FROM etf_metadata WHERE eligible=1").fetchone()["c"]
            excluded = total - eligible
            latest_date = self.db.execute("SELECT MAX(list_date) latest FROM etf_metadata").fetchone()["latest"]
            self.register_datasets()
            if self.audit:
                self.audit.update(run_id, status="complete", total=total, completed=written,
                                  received=len(rows), source=result.source,
                                  qualityStatus="passed", qualityDetails={"eligible": eligible, "excluded": excluded},
                                  finishedAt=now_iso())
            self.universe.update({"status": "complete", "count": total, "eligible": eligible, "excluded": excluded,
                                  "latestAt": latest_date, "finishedAt": now_iso()})
            return dict(self.universe)
        except Exception as error:  # noqa: BLE001
            self.universe.update({"status": "error", "error": str(error), "finishedAt": now_iso()})
            if self.audit:
                self.audit.update(run_id, status="error", failed=1, error=str(error),
                                  qualityStatus="failed", finishedAt=now_iso())
            raise UpstreamError(str(error)) from error

    # ---- daily bars ----
    def _eligible_codes(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT ts_code FROM etf_metadata WHERE eligible=1")}

    def _paged(self, api: str, params: dict[str, str], page_size: int = 5000,
               timeout_ms: int | None = None) -> tuple[list[dict[str, Any]], str]:
        rows: list[dict[str, Any]] = []
        source = ""
        offset = 0
        while True:
            request_params = {**params, "limit": str(page_size), "offset": str(offset)}
            result = self.client.call(api, request_params, timeout_ms=timeout_ms)
            source = result.source
            rows.extend(result.rows)
            if len(result.rows) < page_size:
                return rows, source
            offset += page_size
            if offset > 100000:
                raise UpstreamError(f"{api} pagination exceeded safety limit")

    def sync_daily(self, trade_date: str) -> dict[str, Any]:
        if not DATE_RE.match(trade_date):
            raise UpstreamError("trade_date must be YYYYMMDD")
        codes = self._eligible_codes()
        if not codes:
            raise UpstreamError("ETF 池为空或无可交易 ETF，请先同步 ETF 池 (POST /api/v1/sync/etf/universe)")
        now = now_iso()
        run_id = self.audit.start("etf.daily", {"trade_date": trade_date}) if self.audit else None
        self.daily.update({"status": "running", "currentDate": trade_date, "startedAt": now,
                           "finishedAt": None, "error": None, "runId": run_id})
        try:
            rows, source = self._paged("fund_daily", {"trade_date": trade_date})
            written = 0
            self.db.execute("BEGIN")
            try:
                for row in rows:
                    code = row.get("ts_code")
                    if code not in codes:
                        continue
                    self.db.execute(
                        """INSERT OR REPLACE INTO etf_daily_bars(ts_code,trade_date,open,high,low,close,
                           pre_close,change,pct_chg,vol,amount,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sql_value(code), sql_value(row.get("trade_date")), sql_value(row.get("open")),
                         sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                         sql_value(row.get("pre_close")), sql_value(row.get("change")), sql_value(row.get("pct_chg")),
                         sql_value(row.get("vol")), sql_value(row.get("amount")), source))
                    written += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.register_datasets()
            if self.audit:
                self.audit.update(run_id, status="complete", total=1, completed=1, written=written,
                                  received=len(rows), source=source, qualityStatus="passed",
                                  qualityDetails={"date": trade_date}, finishedAt=now_iso())
            self.daily.update({"status": "complete", "completedDays": 1, "totalDays": 1,
                               "currentDate": trade_date, "written": written, "latestDate": trade_date,
                               "finishedAt": now_iso()})
            return dict(self.daily)
        except Exception as error:  # noqa: BLE001
            self.daily.update({"status": "error", "currentDate": trade_date, "error": str(error), "finishedAt": now_iso()})
            if self.audit:
                self.audit.update(run_id, status="error", failed=1, error=str(error),
                                  qualityStatus="failed", finishedAt=now_iso())
            raise UpstreamError(str(error)) from error

    def _trading_days(self, start_date: str, end_date: str, target_days: int,
                      timeout_ms: int | None = None) -> list[str]:
        try:
            calendar = self.client.call("trade_cal", {"exchange": "SSE", "start_date": start_date,
                                                      "end_date": end_date, "limit": "10000"},
                                        dataset="market.trade_calendar", timeout_ms=timeout_ms).rows
            days = sorted(str(r.get("calendar_date") or r.get("cal_date")) for r in calendar
                          if int(r.get("is_open") or 0) == 1)[-target_days:]
            return days
        except UpstreamError:
            rows = self.db.execute(
                "SELECT calendar_date FROM trade_calendar WHERE exchange='SSE' AND is_open=1 "
                "AND calendar_date BETWEEN ? AND ? ORDER BY calendar_date", (start_date, end_date)).fetchall()
            return [r[0] for r in rows][-target_days:]

    def sync_daily_history(self, start_date: str, end_date: str, target_days: int,
                           concurrency: int = 4, timeout_ms: int | None = None) -> None:
        concurrency = max(1, min(16, int(concurrency)))
        run_id = self.audit.start("etf.daily", {"start_date": start_date, "end_date": end_date,
                                                "days": target_days, "concurrency": concurrency}) if self.audit else None
        self.daily.update({"status": "running", "startedAt": now_iso(), "finishedAt": None,
                           "completedDays": 0, "failedDays": 0, "gapDates": [], "currentDate": None,
                           "error": None, "runId": run_id})
        try:
            codes = self._eligible_codes()
            if not codes:
                raise UpstreamError("ETF 池为空或无可交易 ETF，请先同步 ETF 池 (POST /api/v1/sync/etf/universe)")
            days = self._trading_days(start_date, end_date, target_days, timeout_ms=timeout_ms)
            self.daily["totalDays"] = len(days)
            existing = {r[0] for r in self.db.execute("SELECT DISTINCT trade_date FROM etf_daily_bars")}
            pending = [d for d in days if d not in existing]
            self.daily["completedDays"] = len(days) - len(pending)
            if self.audit:
                self.audit.update(run_id, total=len(days), completed=self.daily["completedDays"],
                                  currentItem=pending[0] if pending else None)
            if not pending:
                self._finish_daily(run_id, len(days), 0, [], None)
                return

            def fetch_day(trade_date: str) -> tuple[str, list[dict[str, Any]], str, int]:
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        rows, source = self._paged("fund_daily", {"trade_date": trade_date}, timeout_ms=timeout_ms)
                        written = 0
                        # 每个 worker 用独立连接：共享连接上的并发 BEGIN 会互相打断，甚至被另一线程的
                        # rollback 回滚掉已写入的数据（open_wal 的 WAL+busy_timeout 负责单写者串行）
                        wcon = open_wal(self.db_path)
                        try:
                            wcon.execute("BEGIN")
                            try:
                                for row in rows:
                                    code = row.get("ts_code")
                                    if code not in codes:
                                        continue
                                    wcon.execute(
                                        """INSERT OR REPLACE INTO etf_daily_bars(ts_code,trade_date,open,high,low,close,
                                           pre_close,change,pct_chg,vol,amount,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                        (sql_value(code), sql_value(row.get("trade_date")), sql_value(row.get("open")),
                                         sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                                         sql_value(row.get("pre_close")), sql_value(row.get("change")), sql_value(row.get("pct_chg")),
                                         sql_value(row.get("vol")), sql_value(row.get("amount")), source))
                                    written += 1
                                wcon.commit()
                            except Exception:
                                wcon.rollback()
                                raise
                        finally:
                            wcon.close()
                        if self.audit:
                            self.audit.log(run_id, trade_date=trade_date, request_api="fund_daily",
                                           request_params={"trade_date": trade_date}, attempt=attempt,
                                           received=len(rows), written=written, source=source, status="passed")
                        return trade_date, rows, source, written
                    except Exception as error:  # noqa: BLE001
                        last_error = error
                        if attempt < 3:
                            time_module.sleep(attempt * 1.5)
                if self.audit:
                    self.audit.log(run_id, level="error", trade_date=trade_date, request_api="fund_daily",
                                   request_params={"trade_date": trade_date}, status="error",
                                   error=str(last_error))
                raise last_error  # type: ignore[misc]

            failed: list[tuple[str, str]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(fetch_day, d): d for d in pending}
                for future, day in sorted(futures.items(), key=lambda kv: kv[1]):
                    try:
                        _, _, _, written = future.result()
                        self.daily["completedDays"] += 1
                        self.daily["written"] = (self.daily.get("written") or 0) + written
                        self.daily["currentDate"] = day
                    except Exception as error:  # noqa: BLE001
                        self.daily["failedDays"] += 1
                        failed.append((day, str(error)))
            gap_dates = [d for d, _ in failed]
            self._finish_daily(run_id, len(days), len(failed), gap_dates,
                               f"{len(failed)} 个交易日失败" if failed else None)
        except Exception as error:  # noqa: BLE001
            self.daily.update({"status": "error", "error": str(error), "finishedAt": now_iso()})
            if self.audit:
                self.audit.update(run_id, status="error", failed=1, error=str(error),
                                  qualityStatus="failed", finishedAt=now_iso())

    def _finish_daily(self, run_id: str | None, total: int, failed: int,
                      gap_dates: list[str], error: str | None) -> None:
        self.register_datasets()
        status = "complete_with_gaps" if gap_dates else "complete"
        self.daily.update({"status": status, "gapDates": gap_dates, "error": error, "finishedAt": now_iso()})
        if self.audit:
            self.audit.update(run_id, status=status, total=total, completed=total - failed, failed=failed,
                              qualityStatus="gaps" if gap_dates else "passed",
                              qualityDetails={"gapDates": gap_dates}, finishedAt=now_iso())

    # ---- index daily bars ----
    def sync_index_daily(self, trade_date: str) -> dict[str, Any]:
        if not DATE_RE.match(trade_date):
            raise UpstreamError("trade_date must be YYYYMMDD")
        now = now_iso()
        run_id = self.audit.start("etf.index", {"trade_date": trade_date}) if self.audit else None
        self.index.update({"status": "running", "currentDate": trade_date, "startedAt": now,
                           "finishedAt": None, "error": None, "runId": run_id})
        try:
            rows, source = self._paged("index_daily", {"trade_date": trade_date})
            written = 0
            self.db.execute("BEGIN")
            try:
                for row in rows:
                    self.db.execute(
                        """INSERT OR REPLACE INTO index_daily_bars(ts_code,trade_date,open,high,low,close,
                           pre_close,change,pct_chg,vol,amount,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sql_value(row.get("ts_code")), sql_value(row.get("trade_date")), sql_value(row.get("open")),
                         sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                         sql_value(row.get("pre_close")), sql_value(row.get("change")), sql_value(row.get("pct_chg")),
                         sql_value(row.get("vol")), sql_value(row.get("amount")), source))
                    written += 1
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.register_datasets()
            if self.audit:
                self.audit.update(run_id, status="complete", total=1, completed=1, written=written,
                                  received=len(rows), source=source, qualityStatus="passed",
                                  qualityDetails={"date": trade_date}, finishedAt=now_iso())
            self.index.update({"status": "complete", "completedDays": 1, "totalDays": 1,
                               "currentDate": trade_date, "written": written, "latestDate": trade_date,
                               "finishedAt": now_iso()})
            return dict(self.index)
        except Exception as error:  # noqa: BLE001
            self.index.update({"status": "error", "currentDate": trade_date, "error": str(error),
                               "finishedAt": now_iso()})
            if self.audit:
                self.audit.update(run_id, status="error", failed=1, error=str(error),
                                  qualityStatus="failed", finishedAt=now_iso())
            raise UpstreamError(str(error)) from error

    def sync_index_history(self, start_date: str, end_date: str, target_days: int,
                           concurrency: int = 4, timeout_ms: int | None = None) -> None:
        concurrency = max(1, min(16, int(concurrency)))
        run_id = self.audit.start("etf.index", {"start_date": start_date, "end_date": end_date,
                                                "days": target_days, "concurrency": concurrency}) if self.audit else None
        self.index.update({"status": "running", "startedAt": now_iso(), "finishedAt": None,
                           "completedDays": 0, "failedDays": 0, "gapDates": [], "currentDate": None,
                           "error": None, "runId": run_id})
        try:
            days = self._trading_days(start_date, end_date, target_days, timeout_ms=timeout_ms)
            self.index["totalDays"] = len(days)
            existing = {r[0] for r in self.db.execute("SELECT DISTINCT trade_date FROM index_daily_bars")}
            pending = [d for d in days if d not in existing]
            self.index["completedDays"] = len(days) - len(pending)
            if self.audit:
                self.audit.update(run_id, total=len(days), completed=self.index["completedDays"],
                                  currentItem=pending[0] if pending else None)
            if not pending:
                self._finish_index(run_id, len(days), 0, [], None)
                return

            def fetch_day(trade_date: str) -> tuple[str, list[dict[str, Any]], str, int]:
                last_error: Exception | None = None
                for attempt in range(1, 4):
                    try:
                        rows, source = self._paged("index_daily", {"trade_date": trade_date}, timeout_ms=timeout_ms)
                        written = 0
                        # 同上：worker 内不得在共享连接上开事务
                        wcon = open_wal(self.db_path)
                        try:
                            wcon.execute("BEGIN")
                            try:
                                for row in rows:
                                    wcon.execute(
                                        """INSERT OR REPLACE INTO index_daily_bars(ts_code,trade_date,open,high,low,close,
                                           pre_close,change,pct_chg,vol,amount,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                                        (sql_value(row.get("ts_code")), sql_value(row.get("trade_date")), sql_value(row.get("open")),
                                         sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                                         sql_value(row.get("pre_close")), sql_value(row.get("change")), sql_value(row.get("pct_chg")),
                                         sql_value(row.get("vol")), sql_value(row.get("amount")), source))
                                    written += 1
                                wcon.commit()
                            except Exception:
                                wcon.rollback()
                                raise
                        finally:
                            wcon.close()
                        if self.audit:
                            self.audit.log(run_id, trade_date=trade_date, request_api="index_daily",
                                           request_params={"trade_date": trade_date}, attempt=attempt,
                                           received=len(rows), written=written, source=source, status="passed")
                        return trade_date, rows, source, written
                    except Exception as error:  # noqa: BLE001
                        last_error = error
                        if attempt < 3:
                            time_module.sleep(attempt * 1.5)
                if self.audit:
                    self.audit.log(run_id, level="error", trade_date=trade_date, request_api="index_daily",
                                   request_params={"trade_date": trade_date}, status="error",
                                   error=str(last_error))
                raise last_error  # type: ignore[misc]

            failed: list[tuple[str, str]] = []
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {pool.submit(fetch_day, d): d for d in pending}
                for future, day in sorted(futures.items(), key=lambda kv: kv[1]):
                    try:
                        _, _, _, written = future.result()
                        self.index["completedDays"] += 1
                        self.index["written"] = (self.index.get("written") or 0) + written
                        self.index["currentDate"] = day
                    except Exception as error:  # noqa: BLE001
                        self.index["failedDays"] += 1
                        failed.append((day, str(error)))
            gap_dates = [d for d, _ in failed]
            self._finish_index(run_id, len(days), len(failed), gap_dates,
                               f"{len(failed)} 个交易日失败" if failed else None)
        except Exception as error:  # noqa: BLE001
            self.index.update({"status": "error", "error": str(error), "finishedAt": now_iso()})
            if self.audit:
                self.audit.update(run_id, status="error", failed=1, error=str(error),
                                  qualityStatus="failed", finishedAt=now_iso())

    def _finish_index(self, run_id: str | None, total: int, failed: int,
                      gap_dates: list[str], error: str | None) -> None:
        self.register_datasets()
        status = "complete_with_gaps" if gap_dates else "complete"
        self.index.update({"status": status, "gapDates": gap_dates, "error": error, "finishedAt": now_iso()})
        if self.audit:
            self.audit.update(run_id, status=status, total=total, completed=total - failed, failed=failed,
                              qualityStatus="gaps" if gap_dates else "passed",
                              qualityDetails={"gapDates": gap_dates}, finishedAt=now_iso())

    # ---- catalog ----
    def register_datasets(self) -> None:
        def latest(table: str, date_col: str) -> str | None:
            row = self.db.execute(f"SELECT MAX({date_col}) latest FROM {table}").fetchone()
            return row["latest"] if row and row["latest"] else None

        self.meta.register_dataset("etf.universe", "dsh-quant-sync", "event",
                                   latest("etf_metadata", "updated_at"), self._count("etf_metadata"),
                                   "ready" if self._count("etf_metadata") else "missing")
        self.meta.register_dataset("etf.daily_bars", "dsh-quant-sync", "daily",
                                   latest("etf_daily_bars", "trade_date"), self._count("etf_daily_bars"),
                                   "ready" if self._count("etf_daily_bars") else "missing")
        self.meta.register_dataset("index.daily_bars", "dsh-quant-sync", "daily",
                                   latest("index_daily_bars", "trade_date"), self._count("index_daily_bars"),
                                   "ready" if self._count("index_daily_bars") else "missing")
