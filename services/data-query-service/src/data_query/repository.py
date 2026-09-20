import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


class ReadOnlyRepository:
    def __init__(self, data_dir: Path, busy_timeout_ms: int = 1500):
        self.data_dir = data_dir
        self.busy_timeout_ms = busy_timeout_ms

    @contextmanager
    def connect(self, name: str) -> Iterator[sqlite3.Connection]:
        path = (self.data_dir / f"{name}.db").resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=self.busy_timeout_ms / 1000)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        try:
            yield db
        finally:
            db.close()

    def has_selection_candidates(self, model_id: str) -> bool:
        """是否存在该模型的选股候选（只读探测；供 /selection/models 决定是否显示动态模型）。

        原实现由 main.py 自己 sqlite3.connect(factors.db) 绕过本仓储的统一只读封装
        （缺 PRAGMA query_only / busy_timeout），这里收回统一入口。
        """
        try:
            with self.connect("factors") as db:
                row = db.execute(
                    "SELECT 1 FROM selection_candidates WHERE model_id=? LIMIT 1", (model_id,)).fetchone()
        except (sqlite3.Error, FileNotFoundError):
            return False
        return row is not None

    def minute_summary(self) -> list[dict[str, Any]]:
        with self.connect("minute") as db:
            result = []
            for freq, buckets in (("1m", 241), ("5m", 49)):
                latest_run = db.execute("SELECT MAX(total) total,MAX(started_at) started_at FROM minute_sync_runs WHERE freq=?", (freq,)).fetchone()
                complete = db.execute("""SELECT MIN(trade_date) start_date,MAX(trade_date) end_date,
                    SUM(CASE WHEN status='complete' THEN 1 ELSE 0 END) complete_days,
                    SUM(CASE WHEN status IN ('complete','incomplete','source_missing') THEN 1 ELSE 0 END) total_days,
                    SUM(CASE WHEN status IN ('incomplete','source_missing') THEN 1 ELSE 0 END) gap_cells,
                    COALESCE(SUM(CASE WHEN status='complete' THEN received ELSE 0 END),0) records,
                    MAX(updated_at) updated_at
                    FROM minute_sync_days WHERE freq=?""", (freq,)).fetchone()
                synchronized_symbols = db.execute("SELECT COUNT(DISTINCT code) count FROM minute_sync_days WHERE freq=?", (freq,)).fetchone()["count"]
                if complete["complete_days"] or complete["total_days"]:
                    result.append({"freq": freq, **dict(complete),
                                   "symbols": synchronized_symbols,
                                   "records": complete["records"],
                                   "updated_at": complete["updated_at"] or (latest_run["started_at"] if latest_run else None)})
        return result

    def daily_summary(self) -> dict[str, Any]:
        with self.connect("market") as db:
            row = db.execute("""SELECT MIN(trade_date) start_date,MAX(trade_date) end_date,
                COALESCE(SUM(bars_count),0) records,COUNT(*) days,MAX(completed_at) updated_at
                FROM history_sync_days""").fetchone()
            symbols = db.execute("SELECT COUNT(*) count FROM security_master WHERE list_status='L'").fetchone()["count"]
            if not symbols:
                symbols = db.execute("SELECT COUNT(*) count FROM security_master").fetchone()["count"]
        return {**dict(row), "symbols": symbols}

    def datasets(self) -> list[dict[str, Any]]:
        daily = self.daily_summary()
        minute = {row["freq"]: row for row in self.minute_summary()}
        market_symbols = daily.get("symbols") or 0
        reference_date = daily.get("end_date")
        # history_sync_days is the compact, authoritative daily run calendar.  Do
        # not derive the lag from millions of daily bar rows on every page load.
        with self.connect("market") as db:
            calendar = [row["trade_date"] for row in db.execute(
                "SELECT trade_date FROM history_sync_days ORDER BY trade_date").fetchall()]
        calendar_index = {trade_date: index for index, trade_date in enumerate(calendar)}

        def trading_day_lag(end_date: str | None) -> int | None:
            if not end_date or not reference_date:
                return None
            if end_date >= reference_date:
                return 0
            end_index, reference_index = calendar_index.get(end_date), calendar_index.get(reference_date)
            return reference_index - end_index if end_index is not None and reference_index is not None else None
        # These are product-level availability contracts.  They make the status a
        # decision users can audit, rather than a label inferred from row counts.
        sla = {
            "daily": (99.0, 99.0, 0),
            "minute-5m": (99.9, 99.0, 0),
            "minute-1m": (99.5, 95.0, 0),
        }

        def item(identifier: str, name: str, category: str, frequency: str, row: dict[str, Any]) -> dict[str, Any]:
            total = row.get("total_days") or row.get("days") or 0
            complete = row.get("complete_days", total)
            completeness = round(100 * complete / total, 2) if total else (100 if row.get("records") else 0)
            coverage = round(100 * row.get("symbols", 0) / market_symbols, 2) if market_symbols else 0
            completeness_target, coverage_target, max_lag = sla[identifier]
            lag = trading_day_lag(row.get("end_date"))
            # Completeness is the explicit SLA.  A non-zero historical gap is
            # still exposed in governance, but must not silently override a
            # declared 99.9% contract and turn a 99.99% dataset into "unready".
            meets_sla = (completeness >= completeness_target and coverage >= coverage_target
                         and (lag is None or lag <= max_lag))
            status = "ready" if meets_sla else ("syncing" if completeness < completeness_target else "degraded")
            return {"id": identifier, "name": name, "category": category, "frequency": frequency,
                    "start_date": row.get("start_date"), "end_date": row.get("end_date"),
                    "symbols": row.get("symbols", 0), "records": row.get("records", 0),
                    "completeness": completeness, "coverage": coverage,
                    "completeness_target": completeness_target, "coverage_target": coverage_target,
                    "trading_day_lag": lag, "max_trading_day_lag": max_lag,
                    "meets_sla": meets_sla, "updated_at": row.get("updated_at"), "status": status}
        result = [item("daily", "日线行情", "行情", "日线", daily)]
        for freq, label in (("5m", "5分钟行情"), ("1m", "1分钟行情")):
            if freq in minute: result.append(item(f"minute-{freq}", label, "行情", freq.replace("m", "分钟"), minute[freq]))
        # 原 alpha101 因子数据集条目随 legacy 因子快照退役一并移除：
        # 快照不再重建，留着只会让数据中心永久显示一条"数据质量风险"。
        return result

    def minute_quality(self, freq: str) -> dict[str, Any]:
        with self.connect("minute") as db:
            status_rows = db.execute("SELECT status,COUNT(*) count FROM minute_sync_days WHERE freq=? GROUP BY status", (freq,)).fetchall()
            source_rows = db.execute("SELECT COALESCE(source,'unknown') source,SUM(accepted) records FROM minute_sync_segments WHERE freq=? AND status='complete' GROUP BY source", (freq,)).fetchall()
        statuses = {row["status"]: row["count"] for row in status_rows}
        return {"frequency": freq, "complete_days": statuses.get("complete", 0),
                "incomplete_days": statuses.get("incomplete", 0), "sources": [dict(row) for row in source_rows],
                "source_missing_days": statuses.get("source_missing", 0),
                "not_applicable_days": statuses.get("not_applicable", 0),
                "non_trading_days": statuses.get("non_trading", 0),
                "duplicate_buckets": 0, "anomalous_ohlc": None}

    def minute_governance(self, freq: str, date_limit: int = 8, code_limit: int = 4) -> dict[str, Any]:
        expected = 241 if freq == "1m" else 49
        with self.connect("minute") as db:
            date_rows = db.execute("""SELECT DISTINCT trade_date FROM minute_sync_days
                WHERE freq=? ORDER BY trade_date DESC LIMIT ?""", (freq, date_limit)).fetchall()
            dates = sorted(row["trade_date"] for row in date_rows)
            if not dates:
                return {"frequency": freq, "expected_buckets": expected, "dates": [], "codes": [], "cells": [], "issues": []}
            placeholders = ",".join("?" for _ in dates)
            code_rows = db.execute(f"""SELECT code,MIN(CASE status WHEN 'incomplete' THEN 0 WHEN 'source_missing' THEN 1
                    WHEN 'non_trading' THEN 2 WHEN 'not_applicable' THEN 3 ELSE 4 END) priority
                FROM minute_sync_days WHERE freq=? AND trade_date IN ({placeholders})
                GROUP BY code ORDER BY priority,code LIMIT ?""", (freq, *dates, code_limit)).fetchall()
            codes = [row["code"] for row in code_rows]
            cells: list[dict[str, Any]] = []
            if codes:
                code_placeholders = ",".join("?" for _ in codes)
                cells = [dict(row) for row in db.execute(f"""SELECT code,trade_date,status,received
                    FROM minute_sync_days WHERE freq=? AND code IN ({code_placeholders})
                    AND trade_date IN ({placeholders})""", (freq, *codes, *dates)).fetchall()]
            issues = [dict(row) for row in db.execute("""SELECT status,
                    CASE WHEN status='not_applicable' THEN '' ELSE trade_date END trade_date,
                    COUNT(DISTINCT code) affected_symbols,
                    SUM(CASE WHEN received<? THEN ?-received ELSE 0 END) missing_buckets,
                    MIN(code) sample_code,MAX(updated_at) updated_at
                FROM minute_sync_days WHERE freq=? AND status IN ('incomplete','source_missing')
                GROUP BY status,CASE WHEN status='not_applicable' THEN '' ELSE trade_date END
                ORDER BY CASE status WHEN 'incomplete' THEN 0 WHEN 'source_missing' THEN 1
                    WHEN 'non_trading' THEN 2 ELSE 3 END,trade_date DESC LIMIT 20""",
                (expected, expected, freq)).fetchall()]
        return {"frequency": freq, "expected_buckets": expected, "dates": dates, "codes": codes,
                "cells": cells, "issues": issues}

    def minute_bars(self, code: str, freq: str, start: str, end: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        start_time = f"{start} 00:00:00" if len(start) == 10 else start
        end_time = f"{end} 23:59:59.999" if len(end) == 10 else end
        with self.connect("minute") as db:
            rows = db.execute("""SELECT code,trade_time,freq,open,high,low,close,volume,amount,source
                FROM minute_bars WHERE code=? AND trade_time>=? AND trade_time<=? AND freq=?
                ORDER BY trade_time DESC LIMIT ? OFFSET ?""", (code, start_time, end_time, freq, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def securities(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self.connect("market") as db:
            rows = db.execute("""SELECT code,name,industry,market,list_date FROM security_master
                WHERE list_status='L' AND (code LIKE ? OR name LIKE ?) ORDER BY code LIMIT ?""",
                (f"%{query}%", f"%{query}%", limit)).fetchall()
        return [dict(row) for row in rows]

    # ---- ETF / index (additive read models over the ETF data layer) ----
    def etf_universe(self, query: str, eligible_only: bool, limit: int) -> list[dict[str, Any]]:
        with self.connect("market") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "etf_metadata" not in tables:
                return []
            sql = """SELECT ts_code tsCode, name, management, custodian, fund_type fundType,
                     invest_type investType, benchmark, list_date listDate, found_date foundDate,
                     issue_date issueDate, delist_date delistDate, m_fee mFee, c_fee cFee,
                     p_value pValue, eligible FROM etf_metadata WHERE 1=1"""
            params: list[Any] = []
            if query:
                sql += " AND (ts_code LIKE ? OR name LIKE ?)"
                params += [f"%{query}%", f"%{query}%"]
            if eligible_only:
                sql += " AND eligible=1"
            sql += " ORDER BY eligible DESC, ts_code LIMIT ?"
            params.append(limit)
            return [dict(row) for row in db.execute(sql, params)]

    def etf_bars(self, ts_code: str, start: str, end: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        return self._daily_bars("etf_daily_bars", ts_code, start, end, limit, offset)

    def index_bars(self, ts_code: str, start: str, end: str, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        return self._daily_bars("index_daily_bars", ts_code, start, end, limit, offset)

    def _daily_bars(self, table: str, ts_code: str, start: str, end: str, limit: int,
                    offset: int = 0) -> list[dict[str, Any]]:
        with self.connect("market") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if table not in tables:
                return []
            sql = f"""SELECT ts_code tsCode, trade_date tradeDate, open, high, low, close,
                      pre_close preClose, change, pct_chg pctChg, vol, amount, source
                      FROM {table} WHERE ts_code=?"""
            params: list[Any] = [ts_code]
            if start:
                sql += " AND trade_date>=?"
                params.append(start)
            if end:
                sql += " AND trade_date<=?"
                params.append(end)
            sql += " ORDER BY trade_date DESC LIMIT ? OFFSET ?"
            params += [limit, offset]
            return [dict(row) for row in db.execute(sql, params)]

    def _homepage_selection(self, config: dict[str, Any]) -> dict[str, Any]:
        """首页「T+1 候选」用的选股结果。

        原实现硬编码 legacy 的 `balanced`，与选股页/股票模拟盘消费的 `ml_walkforward`
        并列展示两套互相矛盾的候选。legacy 因子选股已于 2026-09-18 退役
        （config/factor-models.yaml 只剩 ml_walkforward），这里只走 ML；
        无候选时返回空结果而不是报错。
        """
        if "ml_walkforward" not in (config.get("models") or {}):
            return {"items": [], "count": 0}
        if not self.has_selection_candidates("ml_walkforward"):
            return {"items": [], "count": 0}
        try:
            return self.selection(config, "ml_walkforward", 5)
        except (sqlite3.Error, FileNotFoundError, ValueError):
            return {"items": [], "count": 0}

    def market_overview(self, config: dict[str, Any], code: str = "000001.SH", freq: str = "1m") -> dict[str, Any]:
        """One auditable homepage read model assembled only from persisted platform data."""
        with self.connect("market") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            snapshots: list[dict[str, Any]] = []
            minutes: list[dict[str, Any]] = []
            exchange: list[dict[str, Any]] = []
            limits = {"tradeDate": None, "up": 0, "down": 0, "broken": 0}
            breadth = {"tradeDate": None, "up": 0, "down": 0, "flat": 0, "total": 0}
            if "index_snapshots" in tables:
                latest = db.execute("SELECT MAX(received_at) FROM index_snapshots").fetchone()[0]
                snapshots = [dict(row) for row in db.execute("""SELECT code,name,received_at receivedAt,open,high,low,close,
                    pre_close preClose,change,pct_chg pctChg,volume,amount,source FROM index_snapshots
                    WHERE received_at=? ORDER BY CASE code WHEN '000001.SH' THEN 1 WHEN '399001.SZ' THEN 2
                    WHEN '399006.SZ' THEN 3 WHEN '000688.SH' THEN 4 WHEN '000300.SH' THEN 5 ELSE 9 END""", (latest,))]
            if "index_minute_bars" in tables:
                latest_day = db.execute("SELECT MAX(substr(trade_time,1,10)) FROM index_minute_bars WHERE code=?", (code,)).fetchone()[0]
                if latest_day:
                    minutes = [dict(row) for row in db.execute("""SELECT code,freq,trade_time tradeTime,open,high,low,close,
                        volume,amount,source_updated_at sourceUpdatedAt,received_at receivedAt,source
                        FROM index_minute_bars WHERE code=? AND substr(trade_time,1,10)=? AND upper(freq)='1MIN' ORDER BY trade_time""", (code, latest_day))]
                    minutes = self._normalize_index_minutes(minutes)
                    if freq != "1m":
                        minutes = self._aggregate_index_minutes(minutes, int(freq.removesuffix("m")))
            if "daily_bars" in tables:
                day = db.execute("SELECT MAX(trade_date) FROM daily_bars").fetchone()[0]
                row = db.execute("""SELECT SUM(pct_chg>0) up,SUM(pct_chg<0) down,SUM(pct_chg=0) flat,COUNT(*) total
                    FROM daily_bars WHERE trade_date=?""", (day,)).fetchone()
                breadth = {"tradeDate": day, **{key: int(row[key] or 0) for key in ("up", "down", "flat", "total")}}
            if "exchange_daily_stats" in tables:
                day = db.execute("SELECT MAX(trade_date) FROM exchange_daily_stats").fetchone()[0]
                exchange = [dict(row) for row in db.execute("""SELECT trade_date tradeDate,market_code marketCode,
                    market_name marketName,exchange,company_count companyCount,amount,volume,total_mv totalMv,
                    float_mv floatMv,pe,turnover_rate turnoverRate,transaction_count transactionCount,source
                    FROM exchange_daily_stats WHERE trade_date=? AND market_code IN ('SH_MARKET','SZ_MARKET') ORDER BY exchange""", (day,))]
            if "limit_list" in tables:
                day = db.execute("SELECT MAX(trade_date) FROM limit_list").fetchone()[0]
                rows = db.execute("SELECT limit_type,COUNT(*) count FROM limit_list WHERE trade_date=? GROUP BY limit_type", (day,)).fetchall()
                counts = {row["limit_type"]: row["count"] for row in rows}
                limits = {"tradeDate": day, "up": counts.get("U", 0), "down": counts.get("D", 0), "broken": counts.get("Z", 0)}

        selection = self._homepage_selection(config)
        backtest = self.backtest_latest()
        # Homepage health is a snapshot, not a governance report. Reading the
        # catalog avoids scanning the multi-billion-row minute store on every load.
        datasets = [{"id": row["id"], "name": row["id"], "status": row["status"],
                     "meets_sla": row["status"] == "ready", "updated_at": row["updatedAt"]}
                    for row in self.meta_datasets()]
        with self.connect("sync") as db:
            tasks = [dict(row) for row in db.execute("""SELECT id,kind,tool_id toolId,status,total,completed,failed,
                received,written,source,started_at startedAt,finished_at finishedAt,error
                FROM sync_runs ORDER BY started_at DESC LIMIT 6""").fetchall()]
        return {"indices": snapshots, "minuteSeries": minutes, "breadth": breadth, "limits": limits,
                "exchange": exchange, "selection": selection, "backtest": backtest,
                "datasets": datasets, "tasks": tasks}

    @staticmethod
    def _normalize_index_minutes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Collapse second-level realtime samples onto one canonical minute."""
        selected: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["tradeTime"])[:16] + ":00"
            candidate = {**row, "tradeTime": key}
            if key not in selected or str(candidate.get("receivedAt") or "") >= str(selected[key].get("receivedAt") or ""):
                selected[key] = candidate
        return [selected[key] for key in sorted(selected)]

    @staticmethod
    def _aggregate_index_minutes(rows: list[dict[str, Any]], bucket_minutes: int) -> list[dict[str, Any]]:
        """Aggregate persisted 1-minute index bars without inventing points across gaps."""
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            trade_time = str(row["tradeTime"])
            current = datetime.fromisoformat(trade_time.replace(" ", "T"))
            anchor = current.replace(hour=9, minute=30, second=0) if current.hour < 12 else current.replace(hour=13, minute=0, second=0)
            bucket = max(0, int((current - anchor).total_seconds() // 60) // bucket_minutes)
            key = (anchor + timedelta(minutes=bucket * bucket_minutes)).strftime("%Y-%m-%d %H:%M:%S")
            buckets.setdefault(key, []).append(row)
        result: list[dict[str, Any]] = []
        for key, group in sorted(buckets.items()):
            first, last = group[0], group[-1]
            result.append({**last, "freq": f"{bucket_minutes}MIN", "tradeTime": key,
                           "open": first["open"], "high": max(row["high"] for row in group if row["high"] is not None),
                           "low": min(row["low"] for row in group if row["low"] is not None), "close": last["close"],
                           "volume": sum(row["volume"] or 0 for row in group), "amount": sum(row["amount"] or 0 for row in group)})
        return result

    # ---- meta.db read-only (v2, Python-owned metadata) ----

    @staticmethod
    def _bool(value: Any) -> bool:
        return bool(value)

    def meta_datasets(self) -> list[dict[str, Any]]:
        with self.connect("meta") as db:
            rows = db.execute("SELECT id,provider,frequency,latest_at,row_count,status,updated_at FROM datasets ORDER BY id").fetchall()
        return [{"id": r["id"], "provider": r["provider"], "frequency": r["frequency"],
                 "latestAt": r["latest_at"], "rowCount": r["row_count"], "status": r["status"],
                 "updatedAt": r["updated_at"]} for r in rows]

    def meta_data_sources(self) -> list[dict[str, Any]]:
        with self.connect("meta") as db:
            rows = db.execute("""SELECT id,label,protocol,base_url,CASE WHEN token<>'' THEN 1 ELSE 0 END has_token,
                enabled,priority,timeout_ms,notes,updated_at
                FROM data_source_configs ORDER BY priority,id""").fetchall()
        return [{"id": r["id"], "label": r["label"], "protocol": r["protocol"], "baseUrl": r["base_url"],
                 "token": "", "hasToken": bool(r["has_token"]), "enabled": self._bool(r["enabled"]), "priority": r["priority"],
                 "timeoutMs": r["timeout_ms"], "notes": r["notes"], "updatedAt": r["updated_at"]} for r in rows]

    def meta_tool_routes(self) -> list[dict[str, Any]]:
        with self.connect("meta") as db:
            rows = db.execute("""SELECT tool_id,label,primary_source_id,fallback_source_id,updated_at
                FROM tool_source_routes ORDER BY tool_id""").fetchall()
        return [{"toolId": r["tool_id"], "label": r["label"], "primarySourceId": r["primary_source_id"],
                 "fallbackSourceId": r["fallback_source_id"], "updatedAt": r["updated_at"]} for r in rows]

    def meta_dictionary(self) -> dict[str, list[dict[str, Any]]]:
        with self.connect("meta") as db:
            datasets = db.execute("SELECT id,name,category,frequency,description,updated_at FROM dictionary_datasets ORDER BY id").fetchall()
            fields = db.execute("""SELECT dataset_id,field_name,display_name,data_type,unit,nullable,is_primary_key,description,updated_at
                FROM dictionary_fields ORDER BY dataset_id,field_name""").fetchall()
            mappings = db.execute("""SELECT dataset_id,source_id,source_field,standard_field,transform_expression,enabled,updated_at
                FROM dictionary_mappings ORDER BY dataset_id,source_id,source_field""").fetchall()
            types = db.execute("SELECT code,name,description,is_system,enabled,updated_at FROM dictionary_types ORDER BY code").fetchall()
            items = db.execute("""SELECT dictionary_code,item_code,item_name,sort_order,is_system,enabled,description,updated_at
                FROM dictionary_items ORDER BY dictionary_code,sort_order,item_code""").fetchall()
        return {
            "datasets": [{"id": r["id"], "name": r["name"], "category": r["category"], "frequency": r["frequency"],
                          "description": r["description"], "updatedAt": r["updated_at"]} for r in datasets],
            "fields": [{"datasetId": r["dataset_id"], "fieldName": r["field_name"], "displayName": r["display_name"],
                        "dataType": r["data_type"], "unit": r["unit"], "nullable": self._bool(r["nullable"]),
                        "isPrimaryKey": self._bool(r["is_primary_key"]), "description": r["description"],
                        "updatedAt": r["updated_at"]} for r in fields],
            "mappings": [{"datasetId": r["dataset_id"], "sourceId": r["source_id"], "sourceField": r["source_field"],
                          "standardField": r["standard_field"], "transformExpression": r["transform_expression"],
                          "enabled": self._bool(r["enabled"]), "updatedAt": r["updated_at"]} for r in mappings],
            "types": [{"code": r["code"], "name": r["name"], "description": r["description"],
                       "isSystem": self._bool(r["is_system"]), "enabled": self._bool(r["enabled"]),
                       "updatedAt": r["updated_at"]} for r in types],
            "items": [{"dictionaryCode": r["dictionary_code"], "itemCode": r["item_code"], "itemName": r["item_name"],
                       "sortOrder": r["sort_order"], "isSystem": self._bool(r["is_system"]),
                       "enabled": self._bool(r["enabled"]), "description": r["description"],
                       "updatedAt": r["updated_at"]} for r in items],
        }

    # ---- selection / backtest reads (replaces TS plugin read APIs) ----
    # 注：原 `factor_diagnostics()` 与 `factor_latest()`（/factors/summary、/factors/latest）
    # 已于 2026-09-18 随 legacy 因子选股退役一并删除；其数据源 `factor_snapshots` 表
    # 也已 DROP 并 VACUUM（备份见 artifacts/api-audit/backup-factor-snapshots-*.json）。

    def _selection_rows(self, db: sqlite3.Connection, model: str, limit: int) -> list[sqlite3.Row]:
        """取指定模型最新候选日的候选行。

        这里修掉一个会让 /selection **对所有模型恒返回 0 条**的缺陷：
        日期子查询必须按 model_id 收敛。原写法 `(SELECT MAX(trade_date) FROM selection_candidates)`
        是全局最大值——实测 ml_walkforward 到 20260915，而 legacy 因子模型只到 20260911，
        于是查后者时 WHERE 把当天的行全部排除。

        历史上这里还 LEFT JOIN 过 `factor_snapshots` 取 name/industry/close；legacy 因子快照
        已于 2026-09-18 停更，该 JOIN 随之移除，名称改由 `_attach_security_names()` 从证券主档补。

        ML 模型的可见范围与引擎 `stock_ml.latest_candidates`（main.py /stock-ml/candidates）
        保持一致：只取 result_version>=2 且来源 run 已发布的候选，否则前端会显示
        模拟盘实际不会交易的标的。列/表不存在时自动降级，兼容旧库。
        """
        cols = {row[1] for row in db.execute("PRAGMA table_info(selection_candidates)")}
        has_runs = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_walkforward_runs'").fetchone() is not None
        use_run_scope = (
            model == "ml_walkforward" and has_runs
            and {"source_run_id", "result_version"} <= cols
        )
        if use_run_scope:
            source = ("selection_candidates c JOIN stock_walkforward_runs r "
                      "ON r.run_id=c.source_run_id AND r.is_published=1")
            scope = " AND c.result_version>=2"
        else:
            source = "selection_candidates c"
            scope = ""
        date_subquery = "SELECT MAX(trade_date) FROM selection_candidates WHERE model_id=?"
        return db.execute(
            "SELECT c.trade_date tradeDate,c.model_id modelId,c.rank,c.code,"
            "NULL name,NULL industry,NULL close,c.score,c.reasons_json reasons "
            f"FROM {source} "
            f"WHERE c.model_id=? AND c.trade_date=({date_subquery}){scope} "
            "ORDER BY c.rank LIMIT ?",
            (model, model, limit)).fetchall()

    def _attach_security_names(self, items: list[dict[str, Any]]) -> None:
        """用证券主档补齐候选的 name/industry。

        候选表本身只有代码（原实现靠同日 factor_snapshots 取名称，而 legacy 因子快照已停更），
        表格里只剩代码可读性很差，这里按 code 批量补一次。
        """
        missing = {item["code"] for item in items if not item.get("name")}
        if not missing:
            return
        try:
            with self.connect("market") as db:
                placeholders = ",".join("?" * len(missing))
                rows = db.execute(
                    f"SELECT code,name,industry FROM security_master WHERE code IN ({placeholders})",
                    tuple(missing)).fetchall()
        except (sqlite3.Error, FileNotFoundError):
            return
        names = {row["code"]: (row["name"], row["industry"]) for row in rows}
        for item in items:
            if item.get("name"):
                continue
            name, industry = names.get(item["code"], (None, None))
            item["name"] = name
            if item.get("industry") is None:
                item["industry"] = industry

    def selection(self, config: dict, model: str, limit: int) -> dict[str, Any]:
        definition = config["models"].get(model)
        if not definition:
            raise ValueError(f"unknown selection model: {model}; available: {', '.join(config['models'])}")
        with self.connect("factors") as db:
            rows = self._selection_rows(db, model, limit)
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # 只剩 reasons 是 JSON 列；metrics/factorScores 原来自 factor_snapshots，已随 legacy 移除
            raw = item.get("reasons")
            try:
                item["reasons"] = json.loads(raw) if raw else []
            except (TypeError, ValueError):
                item["reasons"] = []
            items.append(item)
        self._attach_security_names(items)
        return {"model": {"id": model, **definition}, "count": len(items), "items": items}

    def backtest_latest(self) -> dict[str, Any]:
        with self.connect("backtest") as db:
            row = db.execute("SELECT run_id runId,started_at startedAt,finished_at finishedAt,status,summary_json summary,error FROM backtest_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return {"run": None}
        return {"run": {**dict(row), "summary": json.loads(row["summary"]) if row["summary"] else None}}

    def backtest_periods(self, run_id: str | None) -> dict[str, Any]:
        with self.connect("backtest") as db:
            if not run_id:
                row = db.execute("SELECT run_id rid FROM backtest_runs WHERE status='complete' ORDER BY started_at DESC LIMIT 1").fetchone()
                run_id = row["rid"] if row else None
            if not run_id:
                return {"runId": None, "items": [], "layers": []}
            periods = db.execute("SELECT model_id modelId,signal_date signalDate,entry_date entryDate,exit_date exitDate,holdings,skipped,"
                                 "gross_return grossReturn,net_return netReturn,benchmark_return benchmarkReturn,turnover "
                                 "FROM backtest_periods WHERE run_id=? ORDER BY signal_date,model_id", (run_id,)).fetchall()
            layers = db.execute("SELECT model_id modelId,layer,AVG(net_return) averageReturn,COUNT(*) periods "
                                "FROM backtest_layers WHERE run_id=? GROUP BY model_id,layer ORDER BY model_id,layer", (run_id,)).fetchall()
        return {"runId": run_id, "items": [dict(r) for r in periods], "layers": [dict(r) for r in layers]}

    def short_latest(self) -> dict[str, Any]:
        with self.connect("backtest") as db:
            row = db.execute("SELECT run_id runId,started_at startedAt,finished_at finishedAt,status,params_json params,summary_json summary,error FROM short_backtest_runs ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            return {"run": None}
        return {"run": {**dict(row), "params": json.loads(row["params"]) if row["params"] else None,
                        "summary": json.loads(row["summary"]) if row["summary"] else None}}

    def short_trades(self, run_id: str | None, limit: int) -> dict[str, Any]:
        with self.connect("backtest") as db:
            if not run_id:
                row = db.execute("SELECT run_id rid FROM short_backtest_runs WHERE status='complete' ORDER BY started_at DESC LIMIT 1").fetchone()
                run_id = row["rid"] if row else None
            items = []
            if run_id:
                items = [dict(r) for r in db.execute("SELECT signal_date signalDate,entry_date entryDate,exit_date exitDate,code,name,rank,score,"
                    "entry_time entryTime,entry_price entryPrice,exit_time exitTime,exit_price exitPrice,exit_reason exitReason,"
                    "gross_return grossReturn,net_return netReturn,max_favorable maxFavorable,max_adverse maxAdverse "
                    "FROM short_backtest_trades WHERE run_id=? ORDER BY signal_date DESC,rank LIMIT ?", (run_id, limit)).fetchall()]
        return {"runId": run_id, "items": items}

    def stock_technical(self, codes: list[str]) -> dict[str, Any]:
        """批量计算股票技术指标：MA5/MA10/MA20、金叉/死叉、趋势、V5买入信号"""
        if not codes:
            return {"items": [], "count": 0}
        # 去重并转大写
        codes = [c.upper() for c in dict.fromkeys(codes)]
        result = []
        with self.connect("market") as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "daily_bars" not in tables:
                return {"items": [], "count": 0, "error": "daily_bars table not found"}
            for code in codes:
                # 获取最近30天日线数据（按日期降序）
                rows = db.execute(
                    "SELECT trade_date tradeDate, open, high, low, close, volume, amount "
                    "FROM daily_bars WHERE code=? ORDER BY trade_date DESC LIMIT 30",
                    (code,)
                ).fetchall()
                if len(rows) < 20:
                    result.append({"code": code, "error": "insufficient data", "dataDays": len(rows)})
                    continue
                # 转为按日期升序
                bars = [dict(r) for r in reversed(rows)]
                closes = [b["close"] for b in bars]
                # 计算最新的MA值
                ma5 = sum(closes[-5:]) / 5
                ma10 = sum(closes[-10:]) / 10
                ma20 = sum(closes[-20:]) / 20
                # 计算前一天的MA值（用于判断金叉死叉）
                prev_ma5 = sum(closes[-6:-1]) / 5
                prev_ma10 = sum(closes[-11:-1]) / 10
                # 金叉：昨天MA5<=MA10，今天MA5>MA10
                golden_cross = prev_ma5 <= prev_ma10 and ma5 > ma10
                # 死叉：昨天MA5>=MA10，今天MA5<MA10
                death_cross = prev_ma5 >= prev_ma10 and ma5 < ma10
                # 趋势：收盘价>MA20
                trend_up = closes[-1] > ma20
                # V5买入信号：金叉 + 收盘价>MA20
                v5_buy = golden_cross and trend_up
                # V5卖出信号：死叉
                v5_sell = death_cross
                # 最新价格和日期
                latest = bars[-1]
                result.append({
                    "code": code,
                    "tradeDate": latest["tradeDate"],
                    "close": latest["close"],
                    "open": latest["open"],
                    "high": latest["high"],
                    "low": latest["low"],
                    "volume": latest["volume"],
                    "amount": latest["amount"],
                    "ma5": round(ma5, 4),
                    "ma10": round(ma10, 4),
                    "ma20": round(ma20, 4),
                    "prevMa5": round(prev_ma5, 4),
                    "prevMa10": round(prev_ma10, 4),
                    "goldenCross": golden_cross,
                    "deathCross": death_cross,
                    "trendUp": trend_up,
                    "v5BuySignal": v5_buy,
                    "v5SellSignal": v5_sell,
                    "ma5AboveMa10": ma5 > ma10,
                    "closeAboveMa20": closes[-1] > ma20,
                })
        return {"items": result, "count": len(result)}
