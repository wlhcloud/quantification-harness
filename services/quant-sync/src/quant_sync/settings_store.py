"""meta.db settings CRUD (data sources, tool routes, dictionary). Python owns these writes."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .upstream import TushareClient
from .credentials import CredentialCodec


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"


def _b(value) -> int:
    return 1 if value else 0


def _public_source(item: dict) -> dict:
    return {**item, "token": "", "hasToken": bool(item.get("token"))}


class SettingsStore:
    def __init__(self, meta_path: Path, client: TushareClient, credential_key: str = ""):
        self.path = meta_path
        self.client = client
        self.credentials = CredentialCodec(credential_key)
        if credential_key:
            self._migrate_credentials()

    def _migrate_credentials(self) -> None:
        db = self._conn()
        try:
            rows = db.execute("SELECT id,token FROM data_source_configs WHERE token<>''").fetchall()
            for source_id, token in rows:
                encoded = self.credentials.encode(token)
                if encoded != token:
                    db.execute("UPDATE data_source_configs SET token=? WHERE id=?", (encoded, source_id))
            db.commit()
        finally:
            db.close()

    def _conn(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        return db

    def ensure_market_snapshot_metadata(self) -> None:
        """Install homepage-market routes and dictionaries without overwriting operator choices."""
        db = self._conn()
        now = _now()
        routes = [
            ("rt_idx_k", "指数实时日线", "promax", None),
            ("rt_idx_min", "指数实时分钟", "promax", None),
            ("idx_mins", "指数当日分钟补齐", "promax", None),
            ("daily_info", "沪深市场交易统计", "official", "rds"),
            ("sz_daily_info", "深圳市场交易概况", "official", "rds"),
            ("limit_list_d", "涨跌停与炸板", "rds", "promax"),
        ]
        datasets = {
            "market.index_snapshot": ("大盘实时快照", "realtime", "交易所指数实时日线快照", {
                "ts_code": "code", "name": "name", "trade_time": "trade_time", "open": "open", "high": "high",
                "low": "low", "close": "close", "pre_close": "pre_close", "vol": "volume", "amount": "amount",
            }, ["promax"]),
            "market.index_minute": ("大盘实时分钟", "1m", "交易所指数实时分钟行情", {
                "ts_code": "code", "freq": "freq", "trade_time": "trade_time", "open": "open", "high": "high",
                "low": "low", "close": "close", "vol": "volume", "amount": "amount", "updated_at": "source_updated_at",
            }, ["promax"]),
            "market.exchange_daily": ("沪深市场交易统计", "daily", "交易所及板块每日成交统计", {
                "trade_date": "trade_date", "ts_code": "market_code", "ts_name": "market_name", "exchange": "exchange",
                "com_count": "company_count", "amount": "amount", "vol": "volume", "total_mv": "total_mv",
                "float_mv": "float_mv", "pe": "pe", "tr": "turnover_rate", "trans_count": "transaction_count",
            }, ["official", "rds", "promax"]),
            "market.sz_daily": ("深圳市场交易概况", "daily", "深圳市场分类成交统计", {
                "trade_date": "trade_date", "ts_code": "market_type", "count": "security_count", "amount": "amount",
                "vol": "volume", "total_mv": "total_mv", "float_mv": "float_mv",
            }, ["official", "rds", "promax"]),
            "market.limit_list": ("涨跌停与炸板", "daily", "A股涨跌停、炸板与连板明细", {
                "trade_date": "trade_date", "ts_code": "code", "name": "name", "industry": "industry",
                "close": "close", "pct_chg": "pct_chg", "amount": "amount", "limit": "limit_type",
                "limit_times": "limit_times", "open_times": "open_times", "first_time": "first_time",
                "last_time": "last_time", "fd_amount": "sealed_amount",
            }, ["rds", "promax"]),
        }
        try:
            db.execute("BEGIN")
            db.execute("DELETE FROM tool_source_routes WHERE tool_id='rt_k'")
            for tool_id, label, primary, fallback in routes:
                db.execute(
                    "INSERT OR IGNORE INTO tool_source_routes(tool_id,label,primary_source_id,fallback_source_id,updated_at) VALUES(?,?,?,?,?)",
                    (tool_id, label, primary, fallback, now),
                )
            for dataset_id, (name, frequency, description, mappings, sources) in datasets.items():
                db.execute(
                    "INSERT INTO dictionary_datasets(id,name,category,frequency,description,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,frequency=excluded.frequency,description=excluded.description,updated_at=excluded.updated_at",
                    (dataset_id, name, "market", frequency, description, now),
                )
                standard_fields = set(mappings.values()) | {"received_at", "source"}
                for field in standard_fields:
                    dtype = "datetime" if field.endswith("_at") or field == "trade_time" else ("string" if field in {"code", "name", "industry", "freq", "source", "exchange", "market_code", "market_name", "market_type", "limit_type", "first_time", "last_time"} else "number")
                    db.execute(
                        "INSERT OR IGNORE INTO dictionary_fields(dataset_id,field_name,display_name,data_type,unit,nullable,is_primary_key,description,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (dataset_id, field, field, dtype, None, 1, 0, description, now),
                    )
                for source_id in sources:
                    for source_field, standard_field in mappings.items():
                        db.execute(
                            "INSERT OR IGNORE INTO dictionary_mappings(dataset_id,source_id,source_field,standard_field,transform_expression,enabled,updated_at) VALUES(?,?,?,?,?,?,?)",
                            (dataset_id, source_id, source_field, standard_field, None, 1, now),
                        )
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        self.client.meta._cache = None

    # ---- data sources ----
    def replace_sources(self, items: list[dict]) -> list[dict]:
        db = self._conn()
        try:
            existing_tokens = {row[0]: row[1] for row in db.execute("SELECT id,token FROM data_source_configs")}
            db.execute("BEGIN")
            db.execute("DELETE FROM data_source_configs")
            for it in items:
                token = self.credentials.encode(it.get("token") or existing_tokens.get(it["id"], ""))
                db.execute("INSERT INTO data_source_configs(id,label,protocol,base_url,token,enabled,priority,timeout_ms,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (it["id"], it["label"], it["protocol"], it["baseUrl"], token, _b(it.get("enabled", True)),
                     it.get("priority", 100), it.get("timeoutMs", 15000), it.get("notes"), _now()))
            db.commit()
        finally:
            db.close()
        return [_public_source({**item, "token": item.get("token") or existing_tokens.get(item["id"], "")}) for item in items]

    def upsert_source(self, item: dict) -> dict:
        db = self._conn()
        try:
            existing = db.execute("SELECT token FROM data_source_configs WHERE id=?", (item["id"],)).fetchone()
            token = self.credentials.encode(item.get("token") or (existing[0] if existing else ""))
            db.execute("INSERT INTO data_source_configs(id,label,protocol,base_url,token,enabled,priority,timeout_ms,notes,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET label=excluded.label,protocol=excluded.protocol,base_url=excluded.base_url,token=excluded.token,"
                "enabled=excluded.enabled,priority=excluded.priority,timeout_ms=excluded.timeout_ms,notes=excluded.notes,updated_at=excluded.updated_at",
                (item["id"], item["label"], item["protocol"], item["baseUrl"], token, _b(item.get("enabled", True)),
                 item.get("priority", 100), item.get("timeoutMs", 15000), item.get("notes"), _now()))
            db.commit()
        finally:
            db.close()
        return _public_source({**item, "token": token})

    def delete_source(self, source_id: str) -> None:
        db = self._conn()
        try:
            db.execute("BEGIN")
            db.execute("DELETE FROM data_source_configs WHERE id=?", (source_id,))
            db.execute("DELETE FROM tool_source_routes WHERE primary_source_id=? OR fallback_source_id=?", (source_id, source_id))
            db.commit()
        finally:
            db.close()

    def test_source(self, source_id: str) -> dict:
        db = self._conn()
        db.row_factory = sqlite3.Row
        try:
            row = db.execute("SELECT id,protocol,base_url,token,timeout_ms FROM data_source_configs WHERE id=?", (source_id,)).fetchone()
        finally:
            db.close()
        if not row:
            raise ValueError(f"source {source_id} not found")
        base = (row["base_url"] or "").rstrip("/")
        token = self.credentials.decode(row["token"] or "")
        params = {"exchange": "SSE", "start_date": "20250701", "end_date": "20250701"}
        start = time.time()
        try:
            if row["protocol"] == "official":
                body = json.dumps({"api_name": "trade_cal", "token": token, "params": params, "fields": ""}).encode()
                request = urllib.request.Request(base, data=body, headers={"content-type": "application/json"}, method="POST")
            else:
                endpoint = "trade-cal" if row["id"] == "rds" else "trade_cal"
                query = urllib.parse.urlencode(params)
                request = urllib.request.Request(f"{base}/{endpoint}?{query}", headers={"X-API-Key": token})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
            latency = int((time.time() - start) * 1000)
            ok = payload.get("code") == 0
            return {"ok": ok, "latencyMs": latency, "message": "ok" if ok else (payload.get("msg") or f"code {payload.get('code')}")}
        except Exception as error:  # noqa: BLE001
            return {"ok": False, "latencyMs": int((time.time() - start) * 1000), "message": str(error)}

    # ---- tool routes ----
    def replace_routes(self, items: list[dict]) -> list[dict]:
        db = self._conn()
        try:
            db.execute("BEGIN")
            db.execute("DELETE FROM tool_source_routes")
            for it in items:
                db.execute("INSERT INTO tool_source_routes(tool_id,label,primary_source_id,fallback_source_id,updated_at) VALUES(?,?,?,?,?)",
                    (it["toolId"], it["label"], it["primarySourceId"], it.get("fallbackSourceId"), _now()))
            db.commit()
        finally:
            db.close()
        return items

    def upsert_route(self, item: dict) -> dict:
        db = self._conn()
        try:
            db.execute("INSERT INTO tool_source_routes(tool_id,label,primary_source_id,fallback_source_id,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(tool_id) DO UPDATE SET label=excluded.label,primary_source_id=excluded.primary_source_id,"
                "fallback_source_id=excluded.fallback_source_id,updated_at=excluded.updated_at",
                (item["toolId"], item["label"], item["primarySourceId"], item.get("fallbackSourceId"), _now()))
            db.commit()
        finally:
            db.close()
        return item

    # ---- dictionary ----
    def save_dictionary(self, kind: str, item: dict) -> None:
        db = self._conn()
        try:
            if kind == "datasets":
                db.execute("INSERT INTO dictionary_datasets(id,name,category,frequency,description,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET name=excluded.name,category=excluded.category,frequency=excluded.frequency,description=excluded.description,updated_at=excluded.updated_at",
                    (item["id"], item["name"], item["category"], item["frequency"], item.get("description"), _now()))
            elif kind == "fields":
                db.execute("INSERT INTO dictionary_fields(dataset_id,field_name,display_name,data_type,unit,nullable,is_primary_key,description,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(dataset_id,field_name) DO UPDATE SET display_name=excluded.display_name,data_type=excluded.data_type,unit=excluded.unit,nullable=excluded.nullable,is_primary_key=excluded.is_primary_key,description=excluded.description,updated_at=excluded.updated_at",
                    (item["datasetId"], item["fieldName"], item["displayName"], item["dataType"], item.get("unit"), _b(item.get("nullable", True)), _b(item.get("isPrimaryKey", False)), item.get("description"), _now()))
            elif kind == "mappings":
                db.execute("INSERT INTO dictionary_mappings(dataset_id,source_id,source_field,standard_field,transform_expression,enabled,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(dataset_id,source_id,source_field) DO UPDATE SET standard_field=excluded.standard_field,transform_expression=excluded.transform_expression,enabled=excluded.enabled,updated_at=excluded.updated_at",
                    (item["datasetId"], item["sourceId"], item["sourceField"], item["standardField"], item.get("transformExpression"), _b(item.get("enabled", True)), _now()))
            else:
                raise ValueError(f"unknown dictionary kind {kind}")
            db.commit()
        finally:
            db.close()

    def delete_dictionary(self, kind: str, key: dict) -> None:
        db = self._conn()
        try:
            if kind == "datasets":
                db.execute("DELETE FROM dictionary_datasets WHERE id=?", (key["id"],))
            elif kind == "fields":
                db.execute("DELETE FROM dictionary_fields WHERE dataset_id=? AND field_name=?", (key["datasetId"], key["fieldName"]))
            elif kind == "mappings":
                db.execute("DELETE FROM dictionary_mappings WHERE dataset_id=? AND source_id=? AND source_field=?", (key["datasetId"], key["sourceId"], key["sourceField"]))
            else:
                raise ValueError(f"unknown dictionary kind {kind}")
            db.commit()
        finally:
            db.close()

    def save_base(self, kind: str, item: dict) -> None:
        db = self._conn()
        try:
            if kind == "types":
                db.execute("INSERT INTO dictionary_types(code,name,description,is_system,enabled,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET name=excluded.name,description=excluded.description,is_system=excluded.is_system,enabled=excluded.enabled,updated_at=excluded.updated_at",
                    (item["code"], item["name"], item.get("description"), _b(item.get("isSystem", False)), _b(item.get("enabled", True)), _now()))
            elif kind == "items":
                db.execute("INSERT INTO dictionary_items(dictionary_code,item_code,item_name,sort_order,is_system,enabled,description,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(dictionary_code,item_code) DO UPDATE SET item_name=excluded.item_name,sort_order=excluded.sort_order,is_system=excluded.is_system,enabled=excluded.enabled,description=excluded.description,updated_at=excluded.updated_at",
                    (item["dictionaryCode"], item["itemCode"], item["itemName"], item.get("sortOrder", 100), _b(item.get("isSystem", False)), _b(item.get("enabled", True)), item.get("description"), _now()))
            else:
                raise ValueError(f"unknown base dictionary kind {kind}")
            db.commit()
        finally:
            db.close()

    def delete_base(self, kind: str, key: dict) -> None:
        db = self._conn()
        try:
            if kind == "types":
                db.execute("DELETE FROM dictionary_types WHERE code=?", (key["code"],))
            elif kind == "items":
                db.execute("DELETE FROM dictionary_items WHERE dictionary_code=? AND item_code=?", (key["dictionaryCode"], key["itemCode"]))
            else:
                raise ValueError(f"unknown base dictionary kind {kind}")
            db.commit()
        finally:
            db.close()
