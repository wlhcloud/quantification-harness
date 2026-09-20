from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .util import now_iso, open_wal


class AuditStore:
    """Persistent, service-wide sync run ledger used by both the API and UI."""

    def __init__(self, db_path: Path):
        self.db = open_wal(db_path)
        self._lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sync_runs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, tool_id TEXT,
              status TEXT NOT NULL, parameters TEXT NOT NULL,
              total INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
              failed INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0,
              received INTEGER NOT NULL DEFAULT 0, written INTEGER NOT NULL DEFAULT 0,
              source TEXT, quality_status TEXT, quality_details TEXT, current_item TEXT,
              started_at TEXT NOT NULL, finished_at TEXT, error TEXT,
              trigger_source TEXT NOT NULL DEFAULT 'sync-service'
            );
            CREATE INDEX IF NOT EXISTS idx_sync_runs_started ON sync_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sync_runs_tool ON sync_runs(tool_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS sync_run_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
              level TEXT NOT NULL, code TEXT, trade_date TEXT, request_api TEXT,
              request_params TEXT, status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 1,
              received INTEGER NOT NULL DEFAULT 0, written INTEGER NOT NULL DEFAULT 0,
              source TEXT, error TEXT, details TEXT,
              FOREIGN KEY(run_id) REFERENCES sync_runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sync_logs_run ON sync_run_logs(run_id,id DESC);
        """)
        self.db.commit()
        self.recover_interrupted()

    def recover_interrupted(self) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE sync_runs SET status='error',finished_at=?,error=COALESCE(error,'服务重启，任务执行已中断') "
                "WHERE status IN ('starting','running')", (now_iso(),)
            )
            self.db.commit()

    def start(self, kind: str, parameters: dict[str, Any], tool_id: str | None = None,
              trigger_source: str = "sync-service") -> str:
        run_id = f"{kind.replace('.', '-')}-{int(time.time() * 1000)}-{secrets.token_hex(3)}"
        with self._lock:
            self.db.execute(
                "INSERT INTO sync_runs(id,kind,tool_id,status,parameters,started_at,trigger_source) VALUES(?,?,?,?,?,?,?)",
                (run_id, kind, tool_id, "running", json.dumps(parameters, ensure_ascii=False), now_iso(), trigger_source),
            )
            self.db.commit()
        return run_id

    def update(self, run_id: str, **patch: Any) -> None:
        columns = {
            "status": "status", "total": "total", "completed": "completed", "failed": "failed",
            "skipped": "skipped", "received": "received", "written": "written", "source": "source",
            "qualityStatus": "quality_status", "qualityDetails": "quality_details",
            "currentItem": "current_item", "finishedAt": "finished_at", "error": "error",
        }
        assignments, values = [], []
        for key, value in patch.items():
            if key not in columns:
                continue
            if key == "qualityDetails" and value is not None:
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{columns[key]}=?")
            values.append(value)
        if not assignments:
            return
        with self._lock:
            self.db.execute(f"UPDATE sync_runs SET {','.join(assignments)} WHERE id=?", (*values, run_id))
            self.db.commit()

    def latest(self, *, kind: str | None = None, tool_id: str | None = None) -> dict[str, Any] | None:
        where, value = ("kind=?", kind) if kind else (("tool_id=?", tool_id) if tool_id else ("1=1", None))
        params = () if value is None else (value,)
        with self._lock:
            row = self.db.execute(f"SELECT * FROM sync_runs WHERE {where} ORDER BY started_at DESC LIMIT 1", params).fetchone()
        return self._json(row) if row else None

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self.db.execute("SELECT * FROM sync_runs WHERE id=?", (run_id,)).fetchone()
        return self._json(row) if row else None

    def list(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._json(row) for row in rows]

    def log(self, run_id: str, *, level: str = "info", code: str | None = None,
            trade_date: str | None = None, request_api: str | None = None,
            request_params: dict[str, Any] | None = None, status: str = "complete", attempt: int = 1,
            received: int = 0, written: int = 0, source: str | None = None,
            error: str | None = None, details: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.db.execute(
                "INSERT INTO sync_run_logs(run_id,timestamp,level,code,trade_date,request_api,request_params,status,attempt,received,written,source,error,details) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, now_iso(), level, code, trade_date, request_api,
                 json.dumps(request_params or {}, ensure_ascii=False), status, attempt, received, written, source, error,
                 json.dumps(details, ensure_ascii=False) if details is not None else None),
            )
            self.db.commit()

    def logs(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM sync_run_logs WHERE run_id=? ORDER BY id DESC LIMIT ?",
                                   (run_id, limit)).fetchall()
        items = []
        for row in rows:
            def decode(value, fallback):
                try:
                    return json.loads(value) if value else fallback
                except (TypeError, ValueError):
                    return fallback
            items.append({"id": f"audit-{row['id']}", "timestamp": row["timestamp"], "level": row["level"],
                          "code": row["code"], "tradeDate": row["trade_date"], "requestApi": row["request_api"],
                          "requestParams": decode(row["request_params"], {}), "status": row["status"],
                          "attempt": row["attempt"], "received": row["received"], "written": row["written"],
                          "source": row["source"], "error": row["error"], "details": decode(row["details"], None)})
        return items

    @staticmethod
    def _json(row) -> dict[str, Any]:
        def decode(value, fallback):
            try:
                return json.loads(value) if value else fallback
            except (TypeError, ValueError):
                return fallback
        return {
            "id": row["id"], "kind": row["kind"], "toolId": row["tool_id"], "status": row["status"],
            "parameters": decode(row["parameters"], {}), "total": row["total"], "completed": row["completed"],
            "failed": row["failed"], "skipped": row["skipped"], "received": row["received"],
            "written": row["written"], "source": row["source"], "qualityStatus": row["quality_status"],
            "qualityDetails": decode(row["quality_details"], None), "currentItem": row["current_item"],
            "startedAt": row["started_at"], "finishedAt": row["finished_at"], "error": row["error"],
            "triggerSource": row["trigger_source"], "notificationStatus": None,
        }
