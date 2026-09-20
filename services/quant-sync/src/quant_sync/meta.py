from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .credentials import CredentialCodec


@dataclass
class DataSource:
    id: str
    protocol: str
    base_url: str
    token: str
    timeout_ms: int
    label: str = ""
    priority: int = 0


def ensure_meta_schema(meta_path: Path, contract_path: Path) -> bool:
    """按契约 DDL 自举 meta.db（幂等；返回是否找到并应用了契约）。

    原实现完全依赖手工执行 scripts/migrate_meta.py：全新环境或临时目录下 meta.db 没有表，
    MetaStore 读 data_source_configs / tool_source_routes 会直接报 "no such table"，
    SettingsStore 的目录写入也无处可落。contracts/schemas/meta.sql 全部是
    CREATE TABLE IF NOT EXISTS，对既有库无副作用。
    """
    if not contract_path.exists():
        return False
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(meta_path, timeout=10)
    try:
        db.executescript(contract_path.read_text(encoding="utf-8"))
        db.commit()
    finally:
        db.close()
    return True


class MetaStore:
    """Read-only access to meta.db: data sources, tool routes, dictionary mappings."""

    def __init__(self, path: Path, ttl: float = 10.0, credential_key: str = ""):
        self.path = path
        self.ttl = ttl
        self._cache: tuple | None = None
        self._cached_at = 0.0
        self.credentials = CredentialCodec(credential_key)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def _load(self) -> tuple:
        if self._cache is not None and time.monotonic() - self._cached_at < self.ttl:
            return self._cache
        db = self._connect()
        try:
            sources = [
                DataSource(
                    id=r["id"], protocol=r["protocol"], base_url=r["base_url"], token=self.credentials.decode(r["token"]),
                    timeout_ms=r["timeout_ms"], label=r["label"], priority=r["priority"],
                )
                for r in db.execute(
                    "SELECT id,protocol,base_url,token,timeout_ms,label,priority FROM data_source_configs "
                    "WHERE enabled=1 AND base_url<>'' AND token<>'' ORDER BY priority,id"
                )
            ]
            routes = {
                r["tool_id"]: (r["primary_source_id"], r["fallback_source_id"])
                for r in db.execute("SELECT tool_id,primary_source_id,fallback_source_id FROM tool_source_routes")
            }
            mappings: dict[tuple[str, str], list[tuple[str, str, str | None]]] = {}
            for r in db.execute(
                "SELECT dataset_id,source_id,source_field,standard_field,transform_expression "
                "FROM dictionary_mappings WHERE enabled=1"
            ):
                mappings.setdefault((r["dataset_id"], r["source_id"]), []).append(
                    (r["source_field"], r["standard_field"], r["transform_expression"])
                )
        finally:
            db.close()
        self._cache = (sources, routes, mappings)
        self._cached_at = time.monotonic()
        return self._cache

    def sources(self) -> list[DataSource]:
        return self._load()[0]

    def source(self, source_id: str) -> DataSource | None:
        for source in self.sources():
            if source.id == source_id:
                return source
        return None

    def tool_route(self, api: str) -> list[DataSource]:
        sources = self.sources()
        route = self._load()[1].get(api)
        if not route:
            return sources
        result: list[DataSource] = []
        for source_id in route:
            if source_id and (source := self.source(source_id)):
                result.append(source)
        if not result:
            return sources
        return result

    def mappings_for(self, dataset_id: str, source_id: str) -> list[tuple[str, str, str | None]]:
        return self._load()[2].get((dataset_id, source_id), [])

    def register_dataset(self, identifier: str, provider: str, frequency: str, latest_at: str | None,
                         row_count: int, status: str) -> None:
        """Upsert a dataset record into meta.db (Python owns the catalog)."""
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA busy_timeout=5000")
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "000Z"
            db.execute(
                "INSERT INTO datasets(id,provider,frequency,latest_at,row_count,status,updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET provider=excluded.provider,frequency=excluded.frequency,latest_at=excluded.latest_at,"
                "row_count=excluded.row_count,status=excluded.status,updated_at=excluded.updated_at",
                (identifier, provider, frequency, latest_at, row_count, status, now),
            )
            db.commit()
        finally:
            db.close()


def _bool(value: int) -> bool:
    return bool(value)
