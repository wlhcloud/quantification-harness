from __future__ import annotations

import re
import sqlite3
import threading
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .meta import MetaStore
from .upstream import PendingError, TushareClient, UpstreamError
from .util import now_iso, open_wal, sql_value

CODE_RE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$")
DATE_RE = re.compile(r"^\d{8}$")
MIN_COMPLETE_BUCKETS = {"1m": 241, "5m": 49}


def in_session(trade_time: str) -> bool:
    hm = trade_time[11:16]
    return ("09:30" <= hm <= "11:30") or ("13:00" < hm <= "15:00")


def normalize_trade_time(trade_time: str, freq: str) -> str:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2})", trade_time)
    if not match:
        return trade_time
    minute = int(match.group(3))
    bucket = (minute // 5) * 5 if freq == "5m" else minute
    return f"{match.group(1)} {match.group(2)}:{bucket:02d}:00.000"


def to_iso_date(date: str) -> str:
    return f"{date[:4]}-{date[4:6]}-{date[6:]}"


def add_days(date: str, days: int) -> str:
    d = datetime(int(date[:4]), int(date[4:6]), int(date[6:])) + timedelta(days=days)
    return d.strftime("%Y%m%d")


class MinuteSync:
    def __init__(self, db_path: Path, client: TushareClient, meta: MetaStore,
                 concurrency: int = 10, one_minute_segment_days: int = 30, five_minute_segment_days: int = 120,
                 on_progress=None):
        self.client = client
        self.meta = meta
        self.on_progress = on_progress
        self.concurrency = max(1, min(25, int(concurrency)))
        self.segment_days = {
            "1m": max(1, min(45, int(one_minute_segment_days))),
            "5m": max(1, min(180, int(five_minute_segment_days))),
        }
        self.db = open_wal(db_path)
        self.db_path = db_path
        # 上游超时覆盖：按运行设置（minute 的启动由 _start_locks["minute"] 串行化），
        # 不再改 TushareClient 的全局 _run_timeout（并发 kind 之间会互相覆盖）。
        self.timeout_ms: int | None = None
        # sqlite3 connections are not transaction-safe across worker threads.
        # Network requests remain parallel, while every shared connection access
        # is serialized into a short critical section.
        self._write_lock = threading.RLock()
        self._create_schema()
        self._migrate_ledger()
        self._recover_running()
        self.state = self._load_latest_run()
        # Dataset metadata is refreshed after each successful sync. Recounting
        # millions of ledger rows here delayed the health endpoint at startup.

    # ---- schema & startup ----
    def _create_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS minute_bars(
              code TEXT, trade_time TEXT, freq TEXT, open REAL, high REAL, low REAL,
              close REAL, volume REAL, amount REAL, source TEXT,
              PRIMARY KEY(code, trade_time, freq)
            );
            CREATE TABLE IF NOT EXISTS minute_sync_segments(
              code TEXT NOT NULL, freq TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
              accepted INTEGER NOT NULL DEFAULT 0, source TEXT, last_error TEXT, updated_at TEXT NOT NULL,
              PRIMARY KEY(code, freq, start_date, end_date)
            );
            CREATE TABLE IF NOT EXISTS minute_sync_days(
              code TEXT NOT NULL, freq TEXT NOT NULL, trade_date TEXT NOT NULL,
              status TEXT NOT NULL, received INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
              PRIMARY KEY(code,freq,trade_date)
            );
            CREATE TABLE IF NOT EXISTS minute_sync_runs(
              run_id TEXT PRIMARY KEY, start_date TEXT NOT NULL, end_date TEXT NOT NULL, freq TEXT NOT NULL,
              status TEXT NOT NULL, total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
              started_at TEXT NOT NULL, finished_at TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS minute_sync_run_items(
              run_id TEXT NOT NULL, code TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
              error TEXT, updated_at TEXT NOT NULL, PRIMARY KEY(run_id, code)
            );
            CREATE INDEX IF NOT EXISTS idx_minute_sync_items_status ON minute_sync_run_items(run_id, status);
            CREATE INDEX IF NOT EXISTS idx_minute_sync_days_quality ON minute_sync_days(freq, status, trade_date);
            CREATE INDEX IF NOT EXISTS idx_minute_sync_days_freq_code ON minute_sync_days(freq, code);
        """)
        self.db.commit()

    def _migrate_ledger(self) -> None:
        day_count = self.db.execute("SELECT COUNT(*) c FROM minute_sync_days").fetchone()[0]
        if day_count == 0:
            self.db.execute("""
                INSERT OR IGNORE INTO minute_sync_days(code,freq,trade_date,status,received,updated_at)
                SELECT code,freq,REPLACE(SUBSTR(trade_time,1,10),'-',''),'complete',COUNT(*),?
                FROM minute_bars GROUP BY code,freq,REPLACE(SUBSTR(trade_time,1,10),'-','')
            """, (now_iso(),))
            # This correction belongs to the one-time legacy ledger import.
            # New per-day writes already classify completeness in _sync_range.
            self.db.execute("UPDATE minute_sync_days SET status='incomplete' WHERE freq='1m' AND status='complete' AND received<?", (MIN_COMPLETE_BUCKETS["1m"],))
            self.db.execute("UPDATE minute_sync_days SET status='incomplete' WHERE freq='5m' AND status='complete' AND received<?", (MIN_COMPLETE_BUCKETS["5m"],))
        # Do not manufacture a data-quality incident from a fixed historical
        # exception.  Older versions wrote these 2024-11-28 BJ rows as
        # source_missing at every startup, independently of the latest sync.
        # Keep them visible as incomplete until a real retry writes buckets.
        self.db.execute("UPDATE minute_sync_days SET status='incomplete',updated_at=? WHERE freq='5m' AND trade_date='20241128' AND code LIKE '92%.BJ' AND status='source_missing'", (now_iso(),))
        # Pre-listing cells are derived from stock_basic.list_date inside
        # sync_all for every processed security; startup performs no full-table
        # rewrite and contains no code-specific exception.
        self.db.commit()

    def _recover_running(self) -> None:
        at = now_iso()
        self.db.execute("UPDATE minute_sync_run_items SET status='error',error=COALESCE(error,'interrupted by process restart'),updated_at=? WHERE status='running'", (at,))
        self.db.execute("UPDATE minute_sync_runs SET status='error',finished_at=?,error=COALESCE(error,'interrupted by process restart') WHERE status='running'", (at,))
        self.db.commit()

    def _load_latest_run(self) -> dict[str, Any]:
        row = self.db.execute("""SELECT run_id runId,start_date startDate,end_date endDate,freq,status,total,completed,failed,skipped,
            started_at startedAt,finished_at finishedAt,error FROM minute_sync_runs ORDER BY started_at DESC LIMIT 1""").fetchone()
        if row:
            return {k: row[k] for k in row.keys()}
        return {"status": "idle", "runId": None, "total": 0, "completed": 0, "failed": 0, "skipped": 0,
                "current": None, "startDate": None, "endDate": None, "freq": None, "startedAt": None,
                "finishedAt": None, "error": None}

    def register_dataset(self, freq: str) -> None:
        row = self.db.execute("SELECT COALESCE(SUM(received),0) count,MAX(trade_date) latest FROM minute_sync_days WHERE freq=? AND status='complete'", (freq,)).fetchone()
        latest_at = f"{to_iso_date(row['latest'])} 15:00:00" if row["latest"] else None
        self.meta.register_dataset(f"market.minute_bars.{freq}", "dsh-quant-sync", freq, latest_at, row["count"], "ready" if row["count"] else "missing")

    # ---- helpers ----
    def _segments_for(self, start: str, end: str, freq: str) -> list[tuple[str, str]]:
        calendar_days = self.segment_days[freq] - 1
        result: list[tuple[str, str]] = []
        cursor = start
        while cursor <= end:
            segment_end = min(add_days(cursor, calendar_days), end)
            result.append((cursor, segment_end))
            cursor = add_days(segment_end, 1)
        return result

    def _trading_days(self, start: str, end: str) -> list[str]:
        result = self.client.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "limit": "10000"},
                                  timeout_ms=self.timeout_ms)
        return sorted(str(r["cal_date"]) for r in result.rows if int(r["is_open"]) == 1)

    def _missing_day_groups(self, code: str, freq: str, days: list[str]) -> list[dict[str, Any]]:
        if not days:
            return []
        terminal = set()
        with self._write_lock:
            completed = self.db.execute("""SELECT trade_date FROM minute_sync_days
                WHERE code=? AND freq=? AND trade_date>=? AND trade_date<=?
                AND ((status='complete' AND received>=?) OR status IN ('source_missing','not_applicable','non_trading'))""",
                (code, freq, days[0], days[-1], MIN_COMPLETE_BUCKETS[freq])).fetchall()
        for (trade_date,) in completed:
            terminal.add(trade_date)
        missing = [day for day in days if day not in terminal]
        day_index = {day: i for i, day in enumerate(days)}
        max_calendar_days = self.segment_days[freq] - 1
        groups: list[dict[str, Any]] = []
        for day in missing:
            if groups:
                current = groups[-1]
                previous_day = current["days"][-1]
                consecutive = day_index.get(day) == (day_index.get(previous_day, -2) + 1)
                if not consecutive or day > add_days(current["start"], max_calendar_days):
                    groups.append({"start": day, "end": day, "days": [day]})
                else:
                    current["end"] = day
                    current["days"].append(day)
            else:
                groups.append({"start": day, "end": day, "days": [day]})
        return groups

    # ---- sync primitives ----
    def _sync_segment(self, code: str, start: str, end: str, freq: str, days: list[str]) -> dict[str, Any]:
        result = self.client.call("stk_mins", {
            "ts_code": code,
            "freq": "1min" if freq == "1m" else "5min",
            "start_date": f"{to_iso_date(start)} 09:30:00",
            "end_date": f"{to_iso_date(end)} 15:00:00",
        }, "rds-first", dataset="market.minute_bars.5m" if freq == "5m" else "market.minute_bars.1m",
            timeout_ms=self.timeout_ms)
        normalized: dict[str, dict[str, Any]] = {}
        for row in result.rows:
            raw_time = str(row.get("trade_time") or "")
            if not raw_time or not in_session(raw_time):
                continue
            time = normalize_trade_time(raw_time, freq)
            previous = normalized.get(time)
            if previous is None or raw_time == time:
                normalized[time] = row
        accepted = 0
        incomplete_days: list[str] = []
        with self._write_lock:
            self.db.execute("BEGIN")
            try:
                bars = [(sql_value(row.get("code") or row.get("ts_code")), time, freq,
                         sql_value(row.get("open")), sql_value(row.get("high")), sql_value(row.get("low")),
                         sql_value(row.get("close")), sql_value(row.get("volume") or row.get("vol")),
                         sql_value(row.get("amount")), result.source)
                        for time, row in normalized.items()]
                self.db.executemany("INSERT OR IGNORE INTO minute_bars VALUES (?,?,?,?,?,?,?,?,?,?)", bars)
                accepted = len(bars)
                updated_at = now_iso()
                for day in days:
                    buckets = self.db.execute("SELECT COUNT(*) c FROM minute_bars WHERE code=? AND freq=? AND trade_time>=? AND trade_time<?",
                        (code, freq, f"{to_iso_date(day)} 00:00:00", f"{to_iso_date(add_days(day, 1))} 00:00:00")).fetchone()["c"]
                    complete = buckets >= MIN_COMPLETE_BUCKETS[freq]
                    self.db.execute("INSERT INTO minute_sync_days(code,freq,trade_date,status,received,updated_at) VALUES(?,?,?,?,?,?) "
                        "ON CONFLICT(code,freq,trade_date) DO UPDATE SET status=excluded.status,received=excluded.received,updated_at=excluded.updated_at",
                        (code, freq, day, "complete" if complete else "incomplete", buckets, updated_at))
                    if not complete:
                        incomplete_days.append(f"{day}({buckets}/{MIN_COMPLETE_BUCKETS[freq]})")
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
        if incomplete_days:
            raise UpstreamError(f"incomplete minute buckets: {', '.join(incomplete_days)}")
        return {"received": len(result.rows), "accepted": accepted, "source": result.source}

    def _sync_range(self, code: str, start: str, end: str, freq: str, register_after: bool = True,
                    known_trading_days: list[str] | None = None) -> dict[str, Any]:
        if not CODE_RE.match(code) or not DATE_RE.match(start) or not DATE_RE.match(end) or start > end:
            raise UpstreamError("code, start_date/end_date, or freq is invalid")
        received = 0
        accepted = 0
        sources: set[str] = set()
        requested_segments = self._segments_for(start, end, freq)
        days = known_trading_days if known_trading_days is not None else self._trading_days(start, end)
        missing_segments = self._missing_day_groups(code, freq, days)
        skipped_segments = len(requested_segments) if not missing_segments else 0
        for segment in missing_segments:
            last_error = ""
            for attempt in range(1, 4):
                try:
                    result = self._sync_segment(code, segment["start"], segment["end"], freq, segment["days"])
                    received += result["received"]
                    accepted += result["accepted"]
                    if result.get("source"):
                        sources.add(result["source"])
                    with self._write_lock:
                        self.db.execute("INSERT INTO minute_sync_segments(code,freq,start_date,end_date,status,attempts,received,accepted,source,last_error,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,freq,start_date,end_date) DO UPDATE SET "
                            "status=excluded.status,attempts=minute_sync_segments.attempts+1,received=excluded.received,"
                            "accepted=excluded.accepted,source=excluded.source,last_error=excluded.last_error,updated_at=excluded.updated_at",
                            (code, freq, segment["start"], segment["end"], "complete", 1, result["received"], result["accepted"], result["source"], None, now_iso()))
                        self.db.commit()
                    last_error = ""
                    break
                except Exception as error:
                    last_error = str(error)
                    if last_error.startswith("UPSTREAM_PENDING:"):
                        raise
                    with self._write_lock:
                        self.db.execute("INSERT INTO minute_sync_segments(code,freq,start_date,end_date,status,attempts,received,accepted,source,last_error,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(code,freq,start_date,end_date) DO UPDATE SET "
                            "status=excluded.status,attempts=minute_sync_segments.attempts+1,received=excluded.received,"
                            "accepted=excluded.accepted,source=excluded.source,last_error=excluded.last_error,updated_at=excluded.updated_at",
                            (code, freq, segment["start"], segment["end"], "error", 1, 0, 0, None, last_error, now_iso()))
                        self.db.commit()
                    if attempt < 3:
                        time_module.sleep(attempt * 2)
            if last_error:
                raise UpstreamError(f"{code} {segment['start']}-{segment['end']}: {last_error}")
        if register_after:
            self.register_dataset(freq)
        return {"code": code, "freq": freq, "received": received, "accepted": accepted,
                "skippedSegments": skipped_segments, "source": ",".join(sorted(sources)) or "local-ledger"}

    def _save_run_progress(self) -> None:
        if not self.state.get("runId"):
            return
        with self._write_lock:
            self.db.execute("UPDATE minute_sync_runs SET status=?,completed=?,failed=?,skipped=?,finished_at=?,error=? WHERE run_id=?",
                (self.state["status"], self.state["completed"], self.state["failed"], self.state["skipped"],
                 self.state["finishedAt"], self.state["error"], self.state["runId"]))
            self.db.commit()
        if self.on_progress:
            self.on_progress()

    def sync_all(self, start: str, end: str, freq: str, limit: int, retry_run_id: str | None = None) -> None:
        started_at = now_iso()
        run_id = f"minute-{int(time_module.time() * 1000)}-{__import__('secrets').token_hex(3)}"
        self.state.update({"status": "running", "runId": run_id, "total": 0, "completed": 0, "failed": 0,
                           "skipped": 0, "current": None, "startDate": start, "endDate": end, "freq": freq,
                           "startedAt": started_at, "finishedAt": None, "error": None})
        if self.on_progress:
            self.on_progress()
        try:
            list_dates: dict[str, str] = {}
            master = self.client.call("stock_basic", {"list_status": "L", "limit": "10000"},
                                      timeout_ms=self.timeout_ms)
            for row in master.rows:
                list_dates[str(row.get("ts_code") or row.get("code") or "")] = str(row.get("list_date") or "")
            if retry_run_id:
                codes = [r[0] for r in self.db.execute("SELECT code FROM minute_sync_run_items WHERE run_id=? AND status='error' ORDER BY code", (retry_run_id,)).fetchall()]
            else:
                codes = [str(row.get("ts_code") or "") for row in master.rows if CODE_RE.match(str(row.get("ts_code") or ""))]
                if limit > 0:
                    codes = codes[:limit]
            self.state["total"] = len(codes)
            self.db.execute("INSERT INTO minute_sync_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, start, end, freq, "running", len(codes), 0, 0, 0, started_at, None, None))
            for code in codes:
                self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=CASE WHEN excluded.status='running' THEN minute_sync_run_items.attempts+1 ELSE minute_sync_run_items.attempts END,error=excluded.error,updated_at=excluded.updated_at",
                    (run_id, code, "pending", 0, None, started_at))
            self.db.commit()
            open_days = self._trading_days(start, end)
            open_day_set = set(open_days)
            ledger_dates = [r[0] for r in self.db.execute("SELECT DISTINCT trade_date FROM minute_sync_days WHERE freq=? AND trade_date>=? AND trade_date<=? AND status='incomplete'", (freq, start, end)).fetchall()]
            for trade_date in ledger_dates:
                if trade_date not in open_day_set:
                    self.db.execute("UPDATE minute_sync_days SET status='non_trading',updated_at=? WHERE freq=? AND trade_date=? AND status='incomplete'", (now_iso(), freq, trade_date))
            self.db.commit()

            round_codes = codes
            for round_no in range(1, 4):
                if not round_codes:
                    break
                cursor = 0
                cursor_lock = threading.Lock()
                deferred: list[str] = []

                def worker() -> None:
                    nonlocal cursor
                    while True:
                        with cursor_lock:
                            if cursor >= len(round_codes):
                                return
                            code = round_codes[cursor]
                            cursor += 1
                        self.state["current"] = code
                        with self._write_lock:
                            self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=minute_sync_run_items.attempts+1,error=excluded.error,updated_at=excluded.updated_at",
                                (run_id, code, "running", round_no, None, now_iso()))
                            self.db.commit()
                        try:
                            list_date = list_dates.get(code)
                            listed_on = list_date if DATE_RE.match(list_date or "") else None
                            if listed_on:
                                with self._write_lock:
                                    self.db.execute("UPDATE minute_sync_days SET status='not_applicable',updated_at=? WHERE code=? AND freq=? AND trade_date<? AND status='incomplete'",
                                        (now_iso(), code, freq, listed_on))
                                    self.db.commit()
                            effective_days = [d for d in open_days if d >= listed_on] if listed_on else open_days
                            result = self._sync_range(code, start, end, freq, register_after=False, known_trading_days=effective_days)
                            if result["skippedSegments"] == len(self._segments_for(start, end, freq)):
                                with self._write_lock:
                                    self.state["skipped"] += 1
                            with self._write_lock:
                                self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                                    "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                                    (run_id, code, "complete", round_no, None, now_iso()))
                                self.db.commit()
                                self.state["completed"] += 1
                        except PendingError:
                            if round_no < 3:
                                with self._write_lock:
                                    deferred.append(code)
                                    self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                                        "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                                        (run_id, code, "pending", round_no, "上游暂未就绪，已进入下一轮", now_iso()))
                                    self.db.commit()
                            else:
                                with self._write_lock:
                                    # 只计 failed：原实现同时 +1 completed，导致 completed+failed > total
                                    # （UI 上会出现"8/8 完成 + 8 失败"这种自相矛盾的进度）
                                    self.state["failed"] += 1
                                    self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                                        "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                                        (run_id, code, "error", round_no, "上游连续 3 轮未在 10 秒内返回数据", now_iso()))
                                    self.db.commit()
                        except Exception as error:
                            with self._write_lock:
                                self.state["failed"] += 1
                                self.db.execute("INSERT INTO minute_sync_run_items(run_id,code,status,attempts,error,updated_at) VALUES(?,?,?,?,?,?) "
                                    "ON CONFLICT(run_id,code) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,error=excluded.error,updated_at=excluded.updated_at",
                                    (run_id, code, "error", round_no, str(error), now_iso()))
                                self.db.commit()
                        self._save_run_progress()

                with ThreadPoolExecutor(max_workers=min(self.concurrency, len(round_codes))) as pool:
                    list(pool.map(lambda _: worker(), range(min(self.concurrency, len(round_codes)))))
                round_codes = deferred
                if round_codes and round_no < 3:
                    time_module.sleep(30)

            self.state["status"] = "error" if self.state["failed"] else "complete"
            self.state["current"] = None
            self.state["finishedAt"] = now_iso()
            self.state["error"] = f"{self.state['failed']} 只股票同步未完成，可稍后重试" if self.state["failed"] else None
            self._save_run_progress()
            self.register_dataset(freq)
        except Exception as error:
            self.state["status"] = "error"
            self.state["finishedAt"] = now_iso()
            self.state["error"] = str(error)
            self._save_run_progress()

    # ---- date resolution ----
    def _latest_open_day(self) -> str:
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        after_close = now.hour > 15 or (now.hour == 15 and now.minute >= 10)
        end = today if after_close else add_days(today, -1)
        start = add_days(end, -45)
        result = self.client.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "limit": "100"},
                                  timeout_ms=self.timeout_ms)
        days = sorted(str(r["cal_date"]) for r in result.rows if int(r["is_open"]) == 1)
        return days[-1] if days else ""

    def resolve_dates(self, raw_start: str, raw_end: str) -> tuple[str, str]:
        start, end = raw_start, raw_end
        if not DATE_RE.match(start) or not DATE_RE.match(end):
            latest = self._latest_open_day()
            start = start or latest
            end = end or start or latest
        if not DATE_RE.match(start) or not DATE_RE.match(end) or start > end:
            raise UpstreamError("start_date/end_date invalid (YYYYMMDD, start<=end)")
        return start, end

    def sync_single(self, code: str, start: str, end: str, freq: str) -> dict[str, Any]:
        return self._sync_range(code, start, end, freq)

    # ---- ledger queries for HTTP (dedicated read-only connections: never share self.db across threads) ----
    def _read_conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def runs(self, limit: int = 100) -> list[dict[str, Any]]:
        db = self._read_conn()
        try:
            rows = db.execute("SELECT run_id id,freq,status,total,completed,failed,skipped,started_at startedAt,finished_at finishedAt,error,start_date startDate,end_date endDate FROM minute_sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        finally:
            db.close()
        items = []
        for r in rows:
            freq = r["freq"] or ""
            items.append({"id": r["id"], "kind": "minute." + freq, "status": r["status"],
                          "parameters": {"start_date": r["startDate"], "end_date": r["endDate"], "freq": freq},
                          "total": r["total"], "completed": r["completed"], "failed": r["failed"], "skipped": r["skipped"],
                          "currentItem": None, "startedAt": r["startedAt"], "finishedAt": r["finishedAt"],
                          "error": r["error"] if isinstance(r["error"], str) else (None if r["error"] is None else str(r["error"])),
                          "triggerSource": "sync-service", "notificationStatus": None})
        return items

    def failures(self, run_id, limit: int = 100) -> dict[str, Any]:
        db = self._read_conn()
        try:
            rid = run_id or self.state.get("runId") or (db.execute("SELECT run_id rid FROM minute_sync_runs ORDER BY started_at DESC LIMIT 1").fetchone() or {}).get("rid")
            items = []
            if rid:
                items = [dict(r) for r in db.execute("SELECT code,error,attempts,updated_at updatedAt FROM minute_sync_run_items WHERE run_id=? AND status='error' ORDER BY code LIMIT ?", (rid, limit)).fetchall()]
        finally:
            db.close()
        return {"runId": rid, "items": items}

    def details(self, run_id, limit: int = 100) -> dict[str, Any]:
        db = self._read_conn()
        try:
            rid = run_id or self.state.get("runId") or (db.execute("SELECT run_id rid FROM minute_sync_runs ORDER BY started_at DESC LIMIT 1").fetchone() or {}).get("rid")
            if not rid:
                return {"run": None, "counts": {}, "items": [], "dayLedger": {"completed": 0, "zeroRows": 0}}
            run = db.execute("SELECT run_id runId,start_date startDate,end_date endDate,freq,status,total,completed,failed,skipped,started_at startedAt,finished_at finishedAt,error FROM minute_sync_runs WHERE run_id=?", (rid,)).fetchone()
            if not run:
                return {"run": None, "counts": {}, "items": [], "dayLedger": {"completed": 0, "zeroRows": 0}}
            counts = {r[0]: r[1] for r in db.execute("SELECT status,COUNT(*) FROM minute_sync_run_items WHERE run_id=? GROUP BY status", (rid,)).fetchall()}
            items = [dict(r) for r in db.execute("SELECT code,status,attempts,error,updated_at updatedAt FROM minute_sync_run_items WHERE run_id=? AND status<>'complete' ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'running' THEN 1 ELSE 2 END,code LIMIT ?", (rid, limit)).fetchall()]
            day = db.execute("SELECT COUNT(*) completed,SUM(CASE WHEN received=0 THEN 1 ELSE 0 END) zeroRows FROM minute_sync_days WHERE freq=? AND trade_date>=? AND trade_date<=? AND status='complete'", (run["freq"], run["startDate"], run["endDate"])).fetchone()
            return {"run": dict(run), "counts": counts, "items": items, "dayLedger": {"completed": day["completed"], "zeroRows": day["zeroRows"] or 0}}
        finally:
            db.close()
