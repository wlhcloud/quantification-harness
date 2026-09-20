"""Per-upstream-tool sync: each tool (stock_basic/trade_cal/daily/daily_basic/4x finance/stk_mins)
has its own trigger, state, and progress. stk_mins delegates to the minute engine."""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .audit import AuditStore
from .meta import MetaStore
from .upstream import GatewayResult, TushareClient
from .util import now_iso, sql_value

CATALOG: list[dict[str, str]] = [
    {"id": "rt_idx_k", "label": "指数实时日线", "table": "index_snapshots", "kind": "market-snapshot"},
    {"id": "rt_idx_min", "label": "指数实时分钟", "table": "index_minute_bars", "kind": "market-snapshot"},
    {"id": "idx_mins", "label": "指数当日分钟补齐", "table": "index_minute_bars", "kind": "market-snapshot"},
    {"id": "stock_basic", "label": "证券主档", "table": "security_master", "kind": "market-snapshot"},
    {"id": "trade_cal", "label": "交易日历", "table": "trade_calendar", "kind": "market-calendar"},
    {"id": "daily", "label": "日线行情", "table": "daily_bars", "kind": "market-range"},
    {"id": "daily_basic", "label": "每日估值", "table": "daily_basic", "kind": "market-range"},
    {"id": "daily_info", "label": "沪深市场交易统计", "table": "exchange_daily_stats", "kind": "market-range"},
    {"id": "sz_daily_info", "label": "深圳市场交易概况", "table": "sz_daily_stats", "kind": "market-range"},
    {"id": "limit_list_d", "label": "涨跌停与炸板", "table": "limit_list", "kind": "market-range"},
    {"id": "fina_indicator", "label": "财务指标", "table": "financial_indicator", "kind": "finance"},
    {"id": "income", "label": "利润表", "table": "income_statement", "kind": "finance"},
    {"id": "balancesheet", "label": "资产负债表", "table": "balance_sheet", "kind": "finance"},
    {"id": "cashflow", "label": "现金流量表", "table": "cashflow_statement", "kind": "finance"},
    {"id": "stk_mins", "label": "分钟线", "table": "minute_bars", "kind": "minute"},
]
MARKET_DB_TOOLS = {"stock_basic", "trade_cal", "daily", "daily_basic", "daily_info", "sz_daily_info", "limit_list_d"}
FINANCE_DB_TOOLS = {"fina_indicator", "income", "balancesheet", "cashflow"}
FINANCE_RAW_API = {"fina_indicator": "fina_indicator", "income": "income", "balancesheet": "balancesheet", "cashflow": "cashflow"}
MAJOR_INDEX_CODES = "000001.SH,399001.SZ,399006.SZ,000688.SH,000300.SH"


def _latest_open_day(client: TushareClient) -> str:
    now = datetime.now()
    today = now.strftime("%Y%m%d")
    after = now.hour > 15 or (now.hour == 15 and now.minute >= 10)
    end = today if after else (datetime.strptime(today, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    start = (datetime.strptime(end, "%Y%m%d") - timedelta(days=10)).strftime("%Y%m%d")
    res = client.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "limit": "100"})
    days = sorted(str(r["cal_date"]) for r in res.rows if int(r["is_open"]) == 1)
    return days[-1] if days else end


def _open_days(client: TushareClient, start: str, end: str) -> list[str]:
    res = client.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "limit": "10000"})
    return sorted(str(r["cal_date"]) for r in res.rows if int(r["is_open"]) == 1)


def _market_conn(path: Path):
    import sqlite3
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    return db


class ToolSync:
    def __init__(self, client: TushareClient, market_path: Path, finance_path: Path, meta: MetaStore,
                 audit: AuditStore, on_notify: Callable[[dict], None] | None = None):
        self.client = client
        self.market_path = market_path
        self.finance_path = finance_path
        self.meta = meta
        self.audit = audit
        self.on_notify = on_notify
        self._lock = threading.Lock()
        self.states: dict[str, dict[str, Any]] = {
            t["id"]: {"status": "idle", "startedAt": None, "finishedAt": None, "total": 0, "done": 0,
                      "error": None, "params": {}, "runId": None, "received": 0, "written": 0,
                      "source": None, "qualityStatus": None} for t in CATALOG
        }
        for tool in CATALOG:
            latest = self.audit.latest(tool_id=tool["id"])
            if latest:
                self.states[tool["id"]].update({
                    "status": latest["status"], "startedAt": latest["startedAt"], "finishedAt": latest["finishedAt"],
                    "total": latest["total"], "done": latest["completed"], "error": latest["error"],
                    "params": latest["parameters"], "runId": latest["id"], "received": latest["received"],
                    "written": latest["written"], "source": latest["source"], "qualityStatus": latest["qualityStatus"],
                })

    def status_all(self) -> list[dict[str, Any]]:
        out = []
        for t in CATALOG:
            s = dict(self.states[t["id"]])
            out.append({"toolId": t["id"], "label": t["label"], "table": t["table"], "kind": t["kind"], **s})
        return out

    def _touch(self, tool_id: str, **patch: Any) -> None:
        self.states[tool_id].update(patch)
        state = self.states[tool_id]
        if state.get("runId"):
            audit_patch = dict(patch)
            if "done" in audit_patch:
                audit_patch["completed"] = audit_patch.pop("done")
            self.audit.update(state["runId"], **audit_patch)
        if self.on_notify:
            self.on_notify({"type": "tool", "tool": tool_id, "state": self.states[tool_id], "label": next(t["label"] for t in CATALOG if t["id"] == tool_id)})

    def start(self, tool_id: str, params: dict[str, Any], trigger_source: str = "sync-service") -> None:
        if tool_id not in self.states:
            raise ValueError(f"unknown tool {tool_id}")
        with self._lock:
            if self.states[tool_id]["status"] in ("running", "starting"):
                raise RuntimeError(f"{tool_id} already running")
            run_id = self.audit.start(tool_id, params, tool_id=tool_id, trigger_source=trigger_source)
            self._touch(tool_id, runId=run_id, status="running", startedAt=now_iso(), finishedAt=None,
                        total=0, done=0, error=None, params=params, received=0, written=0,
                        source=None, qualityStatus="pending")
        threading.Thread(target=self._run, args=(tool_id, params), daemon=True).start()

    def _run(self, tool_id: str, params: dict[str, Any]) -> None:
        try:
            kind = next(t["kind"] for t in CATALOG if t["id"] == tool_id)
            if kind == "market-snapshot":
                if tool_id in {"rt_idx_k", "rt_idx_min"}:
                    self._sync_realtime_index(tool_id, params)
                elif tool_id == "idx_mins":
                    self._sync_index_history(params)
                else:
                    self._sync_stock_basic(tool_id)
            elif kind in ("market-calendar", "market-range"):
                self._sync_market_range(tool_id, params)
            elif kind == "finance":
                self._sync_finance(tool_id, params)
            else:
                raise ValueError(f"tool {tool_id} kind {kind} not handled here (use minute engine)")
            self._touch(tool_id, status="complete", finishedAt=now_iso(), qualityStatus="passed")
        except Exception as error:  # noqa: BLE001
            run_id = self.states[tool_id].get("runId")
            if run_id:
                self.audit.log(run_id, level="error", request_api=tool_id,
                               request_params=params, status="error", error=str(error))
            self._touch(tool_id, status="error", finishedAt=now_iso(), error=str(error), qualityStatus="failed", failed=1)

    # ---- stock_basic ----
    def _sync_realtime_index(self, tool_id: str, params: dict[str, Any] | None = None) -> None:
        minute = tool_id == "rt_idx_min"
        freq = str((params or {}).get("freq") or "1MIN").upper()
        # freq 只对分钟工具（rt_idx_min）有意义：日线工具 rt_idx_k 的请求参数里
        # 根本不带它（见下方 request_params）。此前这里的校验是**无条件**的，
        # 于是从"上游工具"卡片点 rt_idx_k 的同步按钮必然 400
        # （"index minute freq must be 1MIN/..."），该入口一次都不可能成功。
        if minute and freq not in {"1MIN", "5MIN", "15MIN", "30MIN", "60MIN"}:
            raise ValueError("index minute freq must be 1MIN/5MIN/15MIN/30MIN/60MIN")
        request_params = {"ts_code": MAJOR_INDEX_CODES, **({"freq": freq} if minute else {})}
        dataset = "market.index_minute" if minute else "market.index_snapshot"
        try:
            res = self.client.call(tool_id, request_params, dataset=dataset)
        except Exception:
            # Promax occasionally rejects or returns an empty multi-symbol realtime
            # request while every constituent symbol remains available. Fall back to
            # auditable per-symbol reads instead of turning a transient gateway quirk
            # into a blank homepage.
            parts = [self.client.call(tool_id, {"ts_code": code, **({"freq": freq} if minute else {})}, dataset=dataset)
                     for code in MAJOR_INDEX_CODES.split(",")]
            res = GatewayResult(source=parts[0].source,
                                rows=[row for part in parts for row in part.rows],
                                raw_rows=[row for part in parts for row in part.raw_rows])
        if not res.rows:
            parts = [self.client.call(tool_id, {"ts_code": code, **({"freq": freq} if minute else {})}, dataset=dataset)
                     for code in MAJOR_INDEX_CODES.split(",")]
            res = GatewayResult(source=parts[0].source,
                                rows=[row for part in parts for row in part.rows],
                                raw_rows=[row for part in parts for row in part.raw_rows])
        if not res.rows:
            raise ValueError(f"{tool_id} returned zero rows for the configured major indices")
        db = _market_conn(self.market_path)
        received_at = now_iso()
        try:
            if minute:
                db.execute("""CREATE TABLE IF NOT EXISTS index_minute_bars(
                    code TEXT NOT NULL,freq TEXT NOT NULL,trade_time TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,
                    volume REAL,amount REAL,source_updated_at TEXT,received_at TEXT NOT NULL,source TEXT NOT NULL,raw_json TEXT,
                    PRIMARY KEY(code,freq,trade_time))""")
            else:
                db.execute("""CREATE TABLE IF NOT EXISTS index_snapshots(
                    code TEXT NOT NULL,name TEXT,received_at TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL NOT NULL,
                    pre_close REAL,change REAL,pct_chg REAL,volume REAL,amount REAL,num REAL,source TEXT NOT NULL,raw_json TEXT,
                    PRIMARY KEY(code,received_at))""")
                db.execute("CREATE INDEX IF NOT EXISTS idx_index_snapshots_latest ON index_snapshots(code,received_at DESC)")
            db.execute("BEGIN")
            for index, row in enumerate(res.rows):
                raw = res.raw_rows[index] if index < len(res.raw_rows) else row
                if minute:
                    trade_time = str(sql_value(row.get("trade_time")) or "")
                    if len(trade_time) >= 16:
                        trade_time = trade_time[:16] + ":00"
                    db.execute("INSERT OR REPLACE INTO index_minute_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        sql_value(row.get("code")), sql_value(row.get("freq") or freq), trade_time,
                        sql_value(row.get("open")), sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                        sql_value(row.get("volume")), sql_value(row.get("amount")), sql_value(row.get("source_updated_at")),
                        received_at, res.source, json.dumps(raw, ensure_ascii=False),
                    ))
                else:
                    close = sql_value(row.get("close"))
                    pre_close = sql_value(row.get("pre_close"))
                    change = (float(close) - float(pre_close)) if close is not None and pre_close not in (None, 0) else None
                    pct_chg = (change / float(pre_close) * 100) if change is not None else None
                    db.execute("INSERT OR REPLACE INTO index_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        sql_value(row.get("code")), sql_value(row.get("name")), received_at,
                        sql_value(row.get("open")), sql_value(row.get("high")), sql_value(row.get("low")), close,
                        pre_close, change, pct_chg, sql_value(row.get("volume")), sql_value(row.get("amount")),
                        None, res.source, json.dumps(raw, ensure_ascii=False),
                    ))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self.meta.register_dataset(dataset, res.source, "1m" if minute else "realtime", received_at, len(res.rows), "ready")
        self.audit.log(self.states[tool_id]["runId"], request_api=tool_id, request_params=request_params,
                       received=len(res.rows), written=len(res.rows), source=res.source,
                       details={"receivedAt": received_at, "codes": [row.get("code") for row in res.rows]})
        self._touch(tool_id, total=1, done=1, received=len(res.rows), written=len(res.rows), source=res.source)

    def _sync_index_history(self, params: dict[str, Any]) -> None:
        """Backfill today's complete index minute path through Promax idx_mins."""
        freq = str(params.get("freq") or "1min").lower()
        if freq not in {"1min", "5min", "15min", "30min", "60min"}:
            raise ValueError("idx_mins freq must be 1min/5min/15min/30min/60min")
        day = str(params.get("trade_date") or datetime.now().strftime("%Y%m%d"))
        start = f"{day[:4]}-{day[4:6]}-{day[6:]} 09:30:00"
        end = f"{day[:4]}-{day[4:6]}-{day[6:]} 15:00:00"
        parts = []
        for code in MAJOR_INDEX_CODES.split(","):
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    part = self.client.call("idx_mins", {"ts_code": code, "freq": freq,
                           "start_date": start, "end_date": end}, route="promax-only",
                           dataset="market.index_minute")
                    parts.append(part)
                    self.audit.log(self.states["idx_mins"]["runId"], code=code, trade_date=day,
                                   request_api="idx_mins", request_params={"ts_code": code, "freq": freq,
                                   "start_date": start, "end_date": end}, status="complete", attempt=attempt,
                                   received=len(part.rows), written=len(part.rows), source=part.source)
                    last_error = None
                    break
                except Exception as error:  # retain every failed request in the visible audit log
                    last_error = error
                    self.audit.log(self.states["idx_mins"]["runId"], level="warning", code=code,
                                   trade_date=day, request_api="idx_mins",
                                   request_params={"ts_code": code, "freq": freq, "start_date": start, "end_date": end},
                                   status="retrying" if attempt < 3 else "error", attempt=attempt, error=str(error))
                    if attempt < 3:
                        time.sleep(attempt)
            if last_error is not None:
                raise RuntimeError(f"idx_mins {code} failed after 3 attempts: {last_error}")
        rows = [row for part in parts for row in part.rows]
        raw_rows = [row for part in parts for row in part.raw_rows]
        if not rows:
            raise ValueError("idx_mins returned zero rows")
        db = _market_conn(self.market_path)
        received_at = now_iso()
        try:
            db.execute("""CREATE TABLE IF NOT EXISTS index_minute_bars(
                code TEXT NOT NULL,freq TEXT NOT NULL,trade_time TEXT NOT NULL,open REAL,high REAL,low REAL,close REAL,
                volume REAL,amount REAL,source_updated_at TEXT,received_at TEXT NOT NULL,source TEXT NOT NULL,raw_json TEXT,
                PRIMARY KEY(code,freq,trade_time))""")
            db.execute("BEGIN")
            for index, row in enumerate(rows):
                raw = raw_rows[index] if index < len(raw_rows) else row
                db.execute("INSERT OR REPLACE INTO index_minute_bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    sql_value(row.get("code")), freq.upper(), sql_value(row.get("trade_time")),
                    sql_value(row.get("open")), sql_value(row.get("high")), sql_value(row.get("low")), sql_value(row.get("close")),
                    sql_value(row.get("volume")), sql_value(row.get("amount")), sql_value(row.get("trade_time")),
                    received_at, parts[0].source, json.dumps(raw, ensure_ascii=False)))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self.meta.register_dataset("market.index_minute", parts[0].source, freq, day, len(rows), "ready")
        self.audit.log(self.states["idx_mins"]["runId"], trade_date=day, request_api="idx_mins",
                       request_params={"codes": MAJOR_INDEX_CODES, "freq": freq, "start_date": start, "end_date": end},
                       received=len(rows), written=len(rows), source=parts[0].source,
                       details={"codes": MAJOR_INDEX_CODES.split(","), "completeDayBackfill": True})
        self._touch("idx_mins", total=5, done=5, received=len(rows), written=len(rows), source=parts[0].source)

    def _sync_stock_basic(self, tool_id: str) -> None:
        res = self.client.call("stock_basic", {"list_status": "L"}, dataset="market.security_master")
        db = _market_conn(self.market_path)
        now = now_iso()
        try:
            db.execute("BEGIN")
            for r in res.rows:
                db.execute("INSERT INTO security_master(code,name,area,industry,market,list_date,list_status,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name,area=excluded.area,industry=excluded.industry,market=excluded.market,"
                    "list_date=excluded.list_date,list_status=excluded.list_status,updated_at=excluded.updated_at",
                    (sql_value(r.get("code")), sql_value(r.get("name")), sql_value(r.get("area")), sql_value(r.get("industry")),
                     sql_value(r.get("market")), sql_value(r.get("list_date")), sql_value(r.get("list_status") or "L"), now))
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self.audit.log(self.states[tool_id]["runId"], request_api="stock_basic",
                       request_params={"list_status": "L"}, received=len(res.rows), written=len(res.rows), source=res.source)
        self._touch(tool_id, total=1, done=1, received=len(res.rows), written=len(res.rows), source=res.source)

    # ---- trade_cal / daily / daily_basic ----
    def _sync_market_range(self, tool_id: str, params: dict[str, Any]) -> None:
        end = params.get("end_date") or _latest_open_day(self.client)
        start = params.get("start_date") or end
        if tool_id == "trade_cal":
            res = self.client.call("trade_cal", {"exchange": "SSE", "start_date": start, "end_date": end, "limit": "10000"},
                                   dataset="market.trade_calendar")
            db = _market_conn(self.market_path)
            try:
                db.execute("BEGIN")
                for r in res.rows:
                    db.execute("INSERT OR REPLACE INTO trade_calendar VALUES (?,?,?,?)",
                        (sql_value(r.get("exchange")), sql_value(r.get("calendar_date")), sql_value(r.get("is_open")), sql_value(r.get("previous_open_date"))))
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()
            self.audit.log(self.states[tool_id]["runId"], request_api="trade_cal",
                           request_params={"exchange": "SSE", "start_date": start, "end_date": end},
                           received=len(res.rows), written=len(res.rows), source=res.source)
            self._touch(tool_id, total=1, done=1, received=len(res.rows), written=len(res.rows), source=res.source)
            return
        if tool_id in {"daily_info", "sz_daily_info", "limit_list_d"}:
            self._sync_homepage_daily(tool_id, start, end)
            return
        days = _open_days(self.client, start, end)
        self._touch(tool_id, total=len(days), done=0)
        dataset = {"daily": "market.daily_bars", "daily_basic": "valuation.daily"}[tool_id]
        api = tool_id
        db = _market_conn(self.market_path)
        received = 0
        sources: set[str] = set()
        try:
            for day in days:
                res = self.client.call(api, {"trade_date": day, "limit": "10000"}, dataset=dataset)
                received += len(res.rows)
                sources.add(res.source)
                db.execute("BEGIN")
                try:
                    for r in res.rows:
                        if tool_id == "daily":
                            db.execute("INSERT OR REPLACE INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (sql_value(r.get("code")), sql_value(r.get("trade_date")), sql_value(r.get("open")), sql_value(r.get("high")),
                                 sql_value(r.get("low")), sql_value(r.get("close")), sql_value(r.get("pre_close")), sql_value(r.get("change")),
                                 sql_value(r.get("pct_chg")), sql_value(r.get("volume")), sql_value(r.get("amount"))))
                        else:
                            db.execute("INSERT OR REPLACE INTO daily_basic VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (sql_value(r.get("code")), sql_value(r.get("trade_date")), sql_value(r.get("turnover_rate")), sql_value(r.get("volume_ratio")),
                                 sql_value(r.get("pe")), sql_value(r.get("pe_ttm")), sql_value(r.get("pb")), sql_value(r.get("ps_ttm")),
                                 sql_value(r.get("dv_ttm")), sql_value(r.get("total_mv")), sql_value(r.get("circ_mv"))))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
                self.audit.log(self.states[tool_id]["runId"], trade_date=day, request_api=api,
                               request_params={"trade_date": day, "limit": "10000"}, received=len(res.rows),
                               written=len(res.rows), source=res.source)
                self._touch(tool_id, done=self.states[tool_id]["done"] + 1, received=received,
                            written=received, source=",".join(sorted(sources)))
        finally:
            db.close()

    def _sync_homepage_daily(self, tool_id: str, start: str, end: str) -> None:
        days = _open_days(self.client, start, end)
        self._touch(tool_id, total=len(days), done=0)
        dataset = {"daily_info": "market.exchange_daily", "sz_daily_info": "market.sz_daily",
                   "limit_list_d": "market.limit_list"}[tool_id]
        db = _market_conn(self.market_path)
        db.execute("""CREATE TABLE IF NOT EXISTS exchange_daily_stats(
            trade_date TEXT,market_code TEXT,market_name TEXT,exchange TEXT,company_count INTEGER,amount REAL,volume REAL,
            total_mv REAL,float_mv REAL,pe REAL,turnover_rate REAL,transaction_count REAL,source TEXT,raw_json TEXT,
            PRIMARY KEY(trade_date,market_code,exchange))""")
        db.execute("""CREATE TABLE IF NOT EXISTS sz_daily_stats(
            trade_date TEXT,market_type TEXT,security_count INTEGER,amount REAL,volume REAL,total_mv REAL,float_mv REAL,
            source TEXT,raw_json TEXT,PRIMARY KEY(trade_date,market_type))""")
        db.execute("""CREATE TABLE IF NOT EXISTS limit_list(
            trade_date TEXT,code TEXT,name TEXT,industry TEXT,close REAL,pct_chg REAL,amount REAL,limit_type TEXT,
            limit_times INTEGER,open_times INTEGER,first_time TEXT,last_time TEXT,sealed_amount REAL,source TEXT,raw_json TEXT,
            PRIMARY KEY(trade_date,code,limit_type))""")
        received = 0
        sources: set[str] = set()
        try:
            for day in days:
                res = self.client.call(tool_id, {"trade_date": day}, dataset=dataset)
                received += len(res.rows)
                sources.add(res.source)
                db.execute("BEGIN")
                for index, row in enumerate(res.rows):
                    raw = res.raw_rows[index] if index < len(res.raw_rows) else row
                    raw_json = json.dumps(raw, ensure_ascii=False)
                    if tool_id == "daily_info":
                        db.execute("INSERT OR REPLACE INTO exchange_daily_stats VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                            sql_value(row.get("trade_date")), sql_value(row.get("market_code")), sql_value(row.get("market_name")),
                            sql_value(row.get("exchange")), sql_value(row.get("company_count")), sql_value(row.get("amount")),
                            sql_value(row.get("volume")), sql_value(row.get("total_mv")), sql_value(row.get("float_mv")),
                            sql_value(row.get("pe")), sql_value(row.get("turnover_rate")), sql_value(row.get("transaction_count")), res.source, raw_json))
                    elif tool_id == "sz_daily_info":
                        db.execute("INSERT OR REPLACE INTO sz_daily_stats VALUES(?,?,?,?,?,?,?,?,?)", (
                            sql_value(row.get("trade_date")), sql_value(row.get("market_type")), sql_value(row.get("security_count")),
                            sql_value(row.get("amount")), sql_value(row.get("volume")), sql_value(row.get("total_mv")),
                            sql_value(row.get("float_mv")), res.source, raw_json))
                    else:
                        db.execute("INSERT OR REPLACE INTO limit_list VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                            sql_value(row.get("trade_date")), sql_value(row.get("code")), sql_value(row.get("name")), sql_value(row.get("industry")),
                            sql_value(row.get("close")), sql_value(row.get("pct_chg")), sql_value(row.get("amount")), sql_value(row.get("limit_type")),
                            sql_value(row.get("limit_times")), sql_value(row.get("open_times")), sql_value(row.get("first_time")),
                            sql_value(row.get("last_time")), sql_value(row.get("sealed_amount")), res.source, raw_json))
                db.commit()
                self.audit.log(self.states[tool_id]["runId"], trade_date=day, request_api=tool_id,
                               request_params={"trade_date": day}, received=len(res.rows), written=len(res.rows), source=res.source)
                self._touch(tool_id, done=self.states[tool_id]["done"] + 1, received=received, written=received,
                            source=",".join(sorted(sources)))
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self.meta.register_dataset(dataset, ",".join(sorted(sources)), "daily", days[-1] if days else None,
                                   received, "ready" if received else "missing")

    # ---- finance tools ----
    def _sync_finance(self, tool_id: str, params: dict[str, Any]) -> None:
        api = FINANCE_RAW_API[tool_id]
        dataset = {"fina_indicator": "fundamentals.indicator", "income": "fundamentals.income",
                   "balancesheet": "fundamentals.balance_sheet", "cashflow": "fundamentals.cashflow"}[tool_id]
        code = params.get("code") or ""
        if not code:
            # verified upstream: official/rds/promax all reject whole-market finance by period (require ts_code)
            raise ValueError(f"{tool_id}: 上游仅支持按单只股票(ts_code)查询财务数据；请选择股票")
        db = _market_conn(self.finance_path)
        try:
            res = self.client.call(api, {"ts_code": code, "start_date": params.get("start_date") or "20240101"}, dataset=dataset)
            self._write_finance(db, tool_id, res.rows, res.raw_rows, res.source)
            self.audit.log(self.states[tool_id]["runId"], code=code, request_api=api,
                           request_params={"ts_code": code, "start_date": params.get("start_date") or "20240101"},
                           received=len(res.rows), written=len(res.rows), source=res.source)
            self._touch(tool_id, total=1, done=1, received=len(res.rows), written=len(res.rows), source=res.source)
        finally:
            db.close()

    def _write_finance(self, db, tool_id: str, rows: list[dict], raw_rows: list[dict], source: str) -> None:
        db.execute("BEGIN")
        try:
            if tool_id == "fina_indicator":
                for k, r in enumerate(rows):
                    raw = raw_rows[k] if k < len(raw_rows) else r
                    db.execute("INSERT OR REPLACE INTO financial_indicator VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("roe")),
                         sql_value(r.get("roa")), sql_value(r.get("gross_margin")), sql_value(r.get("net_margin")), sql_value(r.get("debt_to_assets")),
                         sql_value(r.get("current_ratio")), sql_value(r.get("netprofit_yoy")), sql_value(r.get("revenue_yoy")),
                         sql_value(r.get("q_sales_yoy")), sql_value(r.get("ocf_yoy")), json.dumps(raw, ensure_ascii=False), source))
            elif tool_id == "income":
                for k, r in enumerate(rows):
                    raw = raw_rows[k] if k < len(raw_rows) else r
                    db.execute("INSERT OR REPLACE INTO income_statement VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("revenue")), sql_value(r.get("total_revenue")), sql_value(r.get("net_income")), sql_value(r.get("net_income_parent")),
                         sql_value(r.get("operate_profit")), json.dumps(raw, ensure_ascii=False), source))
            elif tool_id == "balancesheet":
                for k, r in enumerate(rows):
                    raw = raw_rows[k] if k < len(raw_rows) else r
                    db.execute("INSERT OR REPLACE INTO balance_sheet VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("total_assets")), sql_value(r.get("total_liab")), sql_value(r.get("total_equity")),
                         sql_value(r.get("money_cap")), sql_value(r.get("accounts_receiv")), sql_value(r.get("inventories")),
                         sql_value(r.get("goodwill")), json.dumps(raw, ensure_ascii=False), source))
            else:
                for k, r in enumerate(rows):
                    raw = raw_rows[k] if k < len(raw_rows) else r
                    db.execute("INSERT OR REPLACE INTO cashflow_statement VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sql_value(r.get("code")), sql_value(r.get("end_date")), sql_value(r.get("ann_date")), sql_value(r.get("report_type")),
                         sql_value(r.get("net_profit")), sql_value(r.get("operating_cashflow")), sql_value(r.get("investing_cashflow")),
                         sql_value(r.get("financing_cashflow")), sql_value(r.get("free_cashflow")), sql_value(r.get("cash_end")),
                         json.dumps(raw, ensure_ascii=False), source))
            db.commit()
        except Exception:
            db.rollback()
            raise


def _last_quarter() -> str:
    now = datetime.now()
    q = (now.month - 1) // 3 - 1
    y = now.year
    if q < 0:
        q += 4
        y -= 1
    month = (q + 1) * 3
    day = "31" if q in (0, 3) else "30"
    return f"{y}{month:02d}{day}"
