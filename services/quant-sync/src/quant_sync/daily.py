from __future__ import annotations

import re
import time as time_module
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .audit import AuditStore
from .meta import MetaStore
from .upstream import TushareClient, UpstreamError
from .util import now_iso, open_wal, sql_value

DATE_RE = re.compile(r"^\d{8}$")


class DailySync:
    def __init__(self, db_path: Path, client: TushareClient, meta: MetaStore, on_progress=None,
                 audit: AuditStore | None = None):
        self.client = client
        self.meta = meta
        self.on_progress = on_progress
        self.audit = audit
        self.db = open_wal(db_path)
        # 上游超时覆盖：按运行设置（daily 启动由 _start_locks["daily"] 串行化），
        # 不再改 TushareClient 的全局 _run_timeout（不同 kind 并发时会互相覆盖）
        self.timeout_ms: int | None = None
        self._create_schema()
        self.db.execute("DELETE FROM history_sync_days WHERE bars_count=0 OR basic_count=0")
        self.db.commit()
        self.history: dict[str, Any] = {"status": "idle", "totalDays": 0, "completedDays": 0,
                                        "failedDays": 0, "gapDates": [], "currentDate": None,
                                        "startedAt": None, "finishedAt": None, "error": None, "runId": None}
        latest = self.audit.latest(kind="daily.history") if self.audit else None
        if not latest and self.audit:
            grouped = lambda table: {r[0]: r[1] for r in self.db.execute(  # noqa: E731
                f"SELECT trade_date,COUNT(*) FROM {table} GROUP BY trade_date")}
            bar_counts = grouped("daily_bars")
            dates = sorted(bar_counts)
            if dates:
                basic_counts = grouped("daily_basic")
                status_counts = grouped("security_status_history")
                adjustment_counts = grouped("adjustment_factors")
                gaps = [date for date in dates if not (
                    bar_counts[date] and basic_counts.get(date, 0)
                    and status_counts.get(date, 0) >= bar_counts[date] * .95
                    and adjustment_counts.get(date, 0) >= bar_counts[date] * .95)]
                migrated = self.audit.start("daily.history", {"start_date": dates[0], "end_date": dates[-1],
                                                               "days": len(dates), "migrated": True})
                migrated_status = "complete_with_gaps" if gaps else "complete"
                finished = now_iso()
                self.audit.update(migrated, status=migrated_status, total=len(dates), completed=len(dates) - len(gaps),
                                  failed=len(gaps), finishedAt=finished,
                                  error=f"{len(gaps)} 个交易日质量未通过，需重试缺口" if gaps else None,
                                  qualityStatus="gaps" if gaps else "passed", qualityDetails={"gapDates": gaps})
                latest = self.audit.latest(kind="daily.history")
        if latest:
            quality = latest.get("qualityDetails") or {}
            self.history.update({"status": latest["status"], "totalDays": latest["total"],
                                 "completedDays": latest["completed"], "failedDays": latest["failed"],
                                 "gapDates": quality.get("gapDates", []), "startedAt": latest["startedAt"],
                                 "finishedAt": latest["finishedAt"], "error": latest["error"], "runId": latest["id"]})

    def _create_schema(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS security_master (
              code TEXT PRIMARY KEY, name TEXT, area TEXT, industry TEXT, market TEXT,
              list_date TEXT, list_status TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trade_calendar (
              exchange TEXT NOT NULL, calendar_date TEXT NOT NULL, is_open INTEGER NOT NULL,
              previous_open_date TEXT, PRIMARY KEY(exchange, calendar_date)
            );
            CREATE TABLE IF NOT EXISTS daily_bars (
              code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL, high REAL, low REAL,
              close REAL, pre_close REAL, change REAL, pct_chg REAL, volume REAL, amount REAL,
              PRIMARY KEY(code, trade_date)
            );
            CREATE TABLE IF NOT EXISTS daily_basic (
              code TEXT NOT NULL, trade_date TEXT NOT NULL, turnover_rate REAL, volume_ratio REAL,
              pe REAL, pe_ttm REAL, pb REAL, ps_ttm REAL, dv_ttm REAL, total_mv REAL, circ_mv REAL,
              PRIMARY KEY(code, trade_date)
            );
            CREATE TABLE IF NOT EXISTS history_sync_days (
              trade_date TEXT PRIMARY KEY, bars_count INTEGER NOT NULL, basic_count INTEGER NOT NULL,
              completed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS security_status_history (
              code TEXT NOT NULL, trade_date TEXT NOT NULL, name TEXT, industry TEXT, list_status TEXT,
              is_st INTEGER NOT NULL DEFAULT 0, is_suspended INTEGER NOT NULL DEFAULT 0,
              limit_up REAL, limit_down REAL, PRIMARY KEY(code, trade_date)
            );
            CREATE TABLE IF NOT EXISTS adjustment_factors (
              code TEXT NOT NULL, trade_date TEXT NOT NULL, adj_factor REAL NOT NULL,
              source TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(code, trade_date)
            );
            CREATE TABLE IF NOT EXISTS research_sync_quality (
              trade_date TEXT PRIMARY KEY, bars_count INTEGER NOT NULL, basic_count INTEGER NOT NULL,
              status_count INTEGER NOT NULL, adjustment_count INTEGER NOT NULL, status TEXT NOT NULL,
              error TEXT, updated_at TEXT NOT NULL
            );
        """)
        self.db.commit()

    def _count(self, table: str) -> int:
        return self.db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    @staticmethod
    def _coverage_rows(rows) -> list[dict]:
        """把 (trade_date, bars, basic, status, adj) 元组转成带判定的覆盖度条目。"""
        out: list[dict] = []
        for trade_date, bars, basic, status, adj in rows:
            bars = int(bars or 0)
            counts = {"bars": bars, "basic": int(basic or 0),
                      "status": int(status or 0), "adjustments": int(adj or 0)}
            # 零行必须显式判缺：不能只靠"< bars*0.95"——bars=0 时阈值也是 0，
            # 而 0 < 0 为假，整日缺失会被误判为"完整"（这个坑真踩到过）。
            missing = [name for name, value in counts.items()
                       if value <= 0 or value < bars * 0.95]
            item = {"tradeDate": trade_date, **counts}
            # 复权因子单独标注：它是标签计算的依据（forward_* 用 close×adj_factor 复权），
            # 缺了它下游算不出正确标签，但"日线齐了"在界面上和"数据可用"长得一样。
            item["missing"] = missing
            item["adjustmentMissing"] = "adjustments" in missing
            item["complete"] = not missing
            out.append(item)
        return out

    def coverage(self, days: int = 10) -> dict:
        """最近若干**交易日**的四项覆盖度（日线 / 估值 / 可交易状态 / 复权因子）。

        这是"数据到底能不能用"的唯一权威视图。此前逐日判定只写在
        research_sync_quality 表里、且只有单日同步路径会写它，区间补拉不写，
        于是 9/16-9/18 复权因子为 0 时，界面上完全看不出异常。
        """
        days = max(1, min(120, int(days)))
        calendar = [r["calendar_date"] for r in self.db.execute(
            "SELECT calendar_date FROM trade_calendar WHERE is_open=1 "
            "ORDER BY calendar_date DESC LIMIT ?", (days,)).fetchall()]
        if not calendar:
            return {"days": days, "items": [], "latest": None, "latestComplete": None,
                    "adjustmentGaps": []}
        placeholders = ",".join("?" for _ in calendar)
        sql = f"""
            SELECT b.trade_date,
                   COUNT(*) bars,
                   (SELECT COUNT(*) FROM daily_basic d WHERE d.trade_date=b.trade_date) basic,
                   (SELECT COUNT(*) FROM security_status_history s WHERE s.trade_date=b.trade_date) status,
                   (SELECT COUNT(*) FROM adjustment_factors a WHERE a.trade_date=b.trade_date) adj
            FROM daily_bars b WHERE b.trade_date IN ({placeholders})
            GROUP BY b.trade_date ORDER BY b.trade_date
        """
        found = {r["trade_date"]: r for r in self.db.execute(sql, tuple(calendar)).fetchall()}
        rows = []
        for d in sorted(calendar):
            r = found.get(d)
            # 完全没有日线的交易日也要显示出来（整日缺失比覆盖不足更严重）
            rows.append((d, r["bars"] if r else 0, r["basic"] if r else 0,
                         r["status"] if r else 0, r["adj"] if r else 0))
        items = self._coverage_rows(rows)
        latest = items[-1] if items else None
        return {
            "days": days,
            "items": items,
            "latest": latest,
            "latestComplete": bool(latest and latest["complete"]),
            "adjustmentGaps": [i["tradeDate"] for i in items if i["adjustmentMissing"]],
            "incomplete": [i["tradeDate"] for i in items if not i["complete"]],
        }

    def register_datasets(self, latest_bars: str | None = None, latest_basic: str | None = None) -> None:
        master_latest = self.db.execute("SELECT MAX(updated_at) latest FROM security_master").fetchone()["latest"]
        calendar_latest = self.db.execute("SELECT MAX(calendar_date) latest FROM trade_calendar").fetchone()["latest"]
        bars_latest = latest_bars or self.db.execute("SELECT MAX(trade_date) latest FROM daily_bars").fetchone()["latest"]
        basic_latest = latest_basic or self.db.execute("SELECT MAX(trade_date) latest FROM daily_basic").fetchone()["latest"]
        self.meta.register_dataset("market.security_master", "dsh-quant-sync", "event", master_latest, self._count("security_master"), "ready")
        self.meta.register_dataset("market.trade_calendar", "dsh-quant-sync", "daily", calendar_latest, self._count("trade_calendar"), "ready")
        self.meta.register_dataset("market.daily_bars", "dsh-quant-sync", "daily", bars_latest, self._count("daily_bars"), "ready" if self._count("daily_bars") else "missing")
        self.meta.register_dataset("valuation.daily", "dsh-quant-sync", "daily", basic_latest, self._count("daily_basic"), "ready" if self._count("daily_basic") else "missing")

    def sync(self, trade_date: str) -> dict[str, int]:
        if not DATE_RE.match(trade_date):
            raise UpstreamError("trade_date must be YYYYMMDD")
        now = now_iso()
        basic = self.client.call("stock_basic", {"list_status": "L"}, dataset="market.security_master").rows
        calendar = self.client.call("trade_cal", {"exchange": "SSE", "start_date": trade_date, "end_date": trade_date}, dataset="market.trade_calendar").rows
        bars = self.client.call("daily", {"trade_date": trade_date}, dataset="market.daily_bars").rows
        valuation = self.client.call("daily_basic", {"trade_date": trade_date}).rows
        limits = self._paged_rows("stk_limit", {"trade_date": trade_date})
        adjustments = self._paged_rows("adj_factor", {"trade_date": trade_date})

        self.db.execute("BEGIN")
        try:
            for r in basic:
                self.db.execute("INSERT INTO security_master(code,name,area,industry,market,list_date,list_status,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name,area=excluded.area,industry=excluded.industry,"
                    "market=excluded.market,list_date=excluded.list_date,list_status=excluded.list_status,updated_at=excluded.updated_at",
                    (sql_value(r.get("code")), sql_value(r.get("name")), sql_value(r.get("area")), sql_value(r.get("industry")),
                     sql_value(r.get("market")), sql_value(r.get("list_date")), sql_value(r.get("list_status") or "L"), now))
            for r in calendar:
                self.db.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?,?,?,?)",
                    (sql_value(r.get("exchange")), sql_value(r.get("calendar_date")), sql_value(r.get("is_open")), sql_value(r.get("previous_open_date"))))
            for r in bars:
                self.db.execute("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("code")), sql_value(r.get("trade_date")), sql_value(r.get("open")), sql_value(r.get("high")),
                     sql_value(r.get("low")), sql_value(r.get("close")), sql_value(r.get("pre_close")), sql_value(r.get("change")),
                     sql_value(r.get("pct_chg")), sql_value(r.get("volume")), sql_value(r.get("amount"))))
            for r in valuation:
                self.db.execute("INSERT OR REPLACE INTO daily_basic VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (sql_value(r.get("ts_code")), sql_value(r.get("trade_date")), sql_value(r.get("turnover_rate")), sql_value(r.get("volume_ratio")),
                     sql_value(r.get("pe")), sql_value(r.get("pe_ttm")), sql_value(r.get("pb")), sql_value(r.get("ps_ttm")),
                     sql_value(r.get("dv_ttm")), sql_value(r.get("total_mv")), sql_value(r.get("circ_mv"))))
            self._store_research_rows(trade_date, bars, limits, adjustments, now)
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.register_datasets(trade_date, trade_date)
        return {"securityMaster": self._count("security_master"), "tradeCalendar": self._count("trade_calendar"),
                "dailyBars": self._count("daily_bars"), "dailyBasic": self._count("daily_basic")}

    def _store_research_rows(self, trade_date: str, bars: list[dict], limits: list[dict],
                             adjustments: list[dict], updated_at: str) -> None:
        limit_by_code = {(r.get("code") or r.get("ts_code")): r for r in limits}
        master = {r["code"]: r for r in self.db.execute("SELECT code,name,industry,list_status FROM security_master")}
        for r in bars:
            code = sql_value(r.get("code") or r.get("ts_code"))
            if not code:
                continue
            security = master.get(code)
            limit = limit_by_code.get(code, {})
            name = security["name"] if security else None
            self.db.execute("INSERT OR REPLACE INTO security_status_history VALUES(?,?,?,?,?,?,?,?,?)",
                (code, trade_date, name, security["industry"] if security else None,
                 security["list_status"] if security else None, 1 if "ST" in (name or "").upper() else 0,
                 1 if not (sql_value(r.get("volume")) or 0) else 0,
                 sql_value(limit.get("up_limit")), sql_value(limit.get("down_limit"))))
        for r in adjustments:
            code = sql_value(r.get("code") or r.get("ts_code"))
            factor = sql_value(r.get("adj_factor"))
            if code and factor and factor > 0:
                self.db.execute("INSERT OR REPLACE INTO adjustment_factors VALUES(?,?,?,?,?)",
                                (code, trade_date, factor, "tushare", updated_at))

    def _fetch_day(self, trade_date: str, run_id: str | None = None) -> tuple[str, list[dict], list[dict], list[dict], list[dict]]:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                bars = self._request_rows("daily", {"trade_date": trade_date, "limit": "10000"},
                                          "market.daily_bars", run_id, trade_date, attempt)
                valuation = self._request_rows("daily_basic", {"trade_date": trade_date, "limit": "10000"},
                                               None, run_id, trade_date, attempt)
                limits = self._paged_rows("stk_limit", {"trade_date": trade_date}, run_id=run_id,
                                          trade_date=trade_date, attempt=attempt)
                adjustments = self._paged_rows("adj_factor", {"trade_date": trade_date}, run_id=run_id,
                                               trade_date=trade_date, attempt=attempt)
                return trade_date, bars, valuation, limits, adjustments
            except Exception as error:  # noqa: BLE001
                last_error = error
                if attempt < 3:
                    time_module.sleep(attempt * 1.5)
        raise last_error  # type: ignore[misc]

    def _request_rows(self, api: str, params: dict[str, str], dataset: str | None,
                      run_id: str | None, trade_date: str, attempt: int) -> list[dict]:
        try:
            result = self.client.call(api, params, dataset=dataset, timeout_ms=self.timeout_ms)
            if run_id and self.audit:
                self.audit.log(run_id, trade_date=trade_date, request_api=api, request_params=params,
                               attempt=attempt, received=len(result.rows), source=result.source)
            return result.rows
        except Exception as error:
            if run_id and self.audit:
                self.audit.log(run_id, level="error", trade_date=trade_date, request_api=api,
                               request_params=params, status="error", attempt=attempt, error=str(error))
            raise

    def _paged_rows(self, api: str, params: dict[str, str], page_size: int = 5000,
                    run_id: str | None = None, trade_date: str | None = None, attempt: int = 1) -> list[dict]:
        """Fetch APIs that silently cap a response at 5,000 rows."""
        rows: list[dict] = []
        offset = 0
        while True:
            request_params = {**params, "limit": str(page_size), "offset": str(offset)}
            page = self._request_rows(api, request_params, None, run_id, trade_date or params.get("trade_date", ""), attempt)
            rows.extend(page)
            if len(page) < page_size:
                return rows
            offset += page_size
            if offset > 50000:
                raise UpstreamError(f"{api} pagination exceeded safety limit")

    def sync_history(self, start_date: str, end_date: str, target_days: int, concurrency: int = 4) -> None:
        concurrency = max(1, min(16, int(concurrency)))
        run_id = self.audit.start("daily.history", {"start_date": start_date, "end_date": end_date,
                                                    "days": target_days, "concurrency": concurrency}) if self.audit else None
        self.history.update({"status": "running", "startedAt": now_iso(), "finishedAt": None,
                             "completedDays": 0, "failedDays": 0, "gapDates": [], "currentDate": None,
                             "error": None, "runId": run_id})
        if self.on_progress:
            self.on_progress()
        try:
            calendar = self.client.call("trade_cal", {"exchange": "SSE", "start_date": start_date, "end_date": end_date, "limit": "10000"},
                                        dataset="market.trade_calendar", timeout_ms=self.timeout_ms).rows
            days = sorted(str(r["calendar_date"]) for r in calendar if int(r["is_open"]) == 1)[-target_days:]
            self.db.execute("BEGIN")
            for r in calendar:
                self.db.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?,?,?,?)",
                    (sql_value(r.get("exchange")), sql_value(r.get("calendar_date")), sql_value(r.get("is_open")), sql_value(r.get("previous_open_date"))))
            self.db.commit()
            self.history["totalDays"] = len(days)
            bar_counts = {r[0]: r[1] for r in self.db.execute("SELECT trade_date,COUNT(*) FROM daily_bars GROUP BY trade_date").fetchall()}
            basic_counts = {r[0]: r[1] for r in self.db.execute("SELECT trade_date,COUNT(*) FROM daily_basic GROUP BY trade_date").fetchall()}
            status_counts = {r[0]: r[1] for r in self.db.execute("SELECT trade_date,COUNT(*) FROM security_status_history GROUP BY trade_date").fetchall()}
            adjustment_counts = {r[0]: r[1] for r in self.db.execute("SELECT trade_date,COUNT(*) FROM adjustment_factors GROUP BY trade_date").fetchall()}
            pending = [d for d in days if (bar_counts.get(d, 0) == 0 or basic_counts.get(d, 0) == 0
                       or status_counts.get(d, 0) < bar_counts.get(d, 0) * .95
                       or adjustment_counts.get(d, 0) < bar_counts.get(d, 0) * .95)]
            self.history["completedDays"] = len(days) - len(pending)
            received_total = 0
            written_total = 0
            if run_id:
                self.audit.update(run_id, total=len(days), completed=self.history["completedDays"],
                                  currentItem=pending[0] if pending else None)
            for offset in range(0, len(pending), concurrency):
                batch = pending[offset:offset + concurrency]
                with ThreadPoolExecutor(max_workers=min(concurrency, len(batch))) as pool:
                    fetched = list(pool.map(lambda date: self._fetch_day(date, run_id), batch))
                for trade_date, bars, valuation, limits, adjustments in fetched:
                    self.history["currentDate"] = trade_date
                    self.db.execute("BEGIN")
                    try:
                        for r in bars:
                            self.db.execute("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (sql_value(r.get("code")), sql_value(r.get("trade_date")), sql_value(r.get("open")), sql_value(r.get("high")),
                                 sql_value(r.get("low")), sql_value(r.get("close")), sql_value(r.get("pre_close")), sql_value(r.get("change")),
                                 sql_value(r.get("pct_chg")), sql_value(r.get("volume")), sql_value(r.get("amount"))))
                        for r in valuation:
                            self.db.execute("INSERT OR REPLACE INTO daily_basic VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (sql_value(r.get("ts_code")), sql_value(r.get("trade_date")), sql_value(r.get("turnover_rate")), sql_value(r.get("volume_ratio")),
                                 sql_value(r.get("pe")), sql_value(r.get("pe_ttm")), sql_value(r.get("pb")), sql_value(r.get("ps_ttm")),
                                 sql_value(r.get("dv_ttm")), sql_value(r.get("total_mv")), sql_value(r.get("circ_mv"))))
                        self._store_research_rows(trade_date, bars, limits, adjustments, now_iso())
                        self.db.execute("INSERT OR REPLACE INTO history_sync_days VALUES (?,?,?,?)", (trade_date, len(bars), len(valuation), now_iso()))
                        status_count = self.db.execute("SELECT COUNT(*) FROM security_status_history WHERE trade_date=?", (trade_date,)).fetchone()[0]
                        adjustment_count = self.db.execute("SELECT COUNT(*) FROM adjustment_factors WHERE trade_date=?", (trade_date,)).fetchone()[0]
                        # 判定收敛到 _coverage_rows（与 /sync/daily/coverage 同一套口径），
                        # 避免两处各写一份"什么算通过"而产生互相矛盾的结论。
                        judged = self._coverage_rows(
                            [(trade_date, len(bars), len(valuation), status_count, adjustment_count)])[0]
                        valid = bool(bars and valuation) and judged["complete"]
                        error = None if valid else (f"质量未通过: bars={len(bars)}, basic={len(valuation)}, "
                                                    f"status={status_count}, adjustment={adjustment_count}；"
                                                    f"缺: {','.join(judged['missing']) or '—'}")
                        self.db.execute("INSERT OR REPLACE INTO research_sync_quality VALUES(?,?,?,?,?,?,?,?)",
                                        (trade_date, len(bars), len(valuation), status_count, adjustment_count,
                                         "passed" if valid else "gap", error, now_iso()))
                        self.db.commit()
                    except Exception:
                        self.db.rollback()
                        raise
                    if valid:
                        self.history["completedDays"] += 1
                    else:
                        self.history["failedDays"] += 1
                        self.history["gapDates"].append(trade_date)
                    received_total += len(bars) + len(valuation) + len(limits) + len(adjustments)
                    written_total += len(bars) + len(valuation) + status_count + adjustment_count
                    if run_id:
                        self.audit.log(run_id, level="warning" if not valid else "info", trade_date=trade_date,
                                       request_api="quality-check", status="gap" if not valid else "passed",
                                       received=len(bars) + len(valuation) + len(limits) + len(adjustments),
                                       written=len(bars) + len(valuation) + status_count + adjustment_count,
                                       source="local-validation", error=error,
                                       details={"bars": len(bars), "basic": len(valuation), "limits": len(limits),
                                                "status": status_count, "adjustments": adjustment_count})
                        self.audit.update(run_id, completed=self.history["completedDays"], failed=self.history["failedDays"],
                                          currentItem=trade_date, received=received_total, written=written_total,
                                          source="tushare")
                    if self.on_progress:
                        self.on_progress()
            latest_date = days[-1] if days else None
            self.register_datasets(latest_date, latest_date)
            final_status = "complete_with_gaps" if self.history["failedDays"] else "complete"
            error = (f"{self.history['failedDays']} 个交易日质量未通过，需重试缺口" if self.history["failedDays"] else None)
            self.history.update({"status": final_status, "finishedAt": now_iso(), "currentDate": None, "error": error})
            if run_id:
                self.audit.update(run_id, status=final_status, completed=self.history["completedDays"],
                                  failed=self.history["failedDays"], finishedAt=self.history["finishedAt"], error=error,
                                  qualityStatus="gaps" if self.history["failedDays"] else "passed",
                                  qualityDetails={"gapDates": self.history["gapDates"]})
        except Exception as error:  # noqa: BLE001
            self.history.update({"status": "error", "finishedAt": now_iso(), "error": str(error)})
            if run_id:
                self.audit.update(run_id, status="error", finishedAt=self.history["finishedAt"], error=str(error),
                                  completed=self.history["completedDays"], failed=max(1, self.history["failedDays"]),
                                  qualityStatus="failed", qualityDetails={"gapDates": self.history["gapDates"]})
        finally:
            if self.on_progress:
                self.on_progress()
