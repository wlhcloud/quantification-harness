import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterator

from .models import JobRecord, JobStatus, JobType


class JobRepository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    parameters_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                )
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def decode(row: sqlite3.Row) -> JobRecord:
        """把库里的一行还原成 JobRecord。

        对**已退役**的 type/status 必须降级而不是抛错：`factor_mining` 随 legacy
        因子选股退役后，库里 15 条历史记录曾让 `GET /jobs?limit>=46` 直接 500
        （Starlette 的 500 是纯文本，前端 `response.json()` 还会再炸一次）。
        一条历史记录不该打挂整个列表接口，因此保留原始字符串原样返回。
        新提交的 job 仍受 `JobCreate.type: JobType` 严格校验，契约不受影响。
        """
        try:
            job_type: str = JobType(row["type"]).value
        except ValueError:
            job_type = str(row["type"])
        try:
            status: str = JobStatus(row["status"]).value
        except ValueError:
            status = str(row["status"])
        return JobRecord(
            id=row["id"], type=job_type, status=status,
            progress=row["progress"], parameters=json.loads(row["parameters_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"], cancel_requested=bool(row["cancel_requested"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        )

    def insert(self, job: JobRecord) -> None:
        with self.connect() as db:
            # JobType/JobStatus 都是 StrEnum（本身就是 str），JobRecord 也会把枚举
            # 归一成字符串；这里统一用 str() 取值，兼容两种入参形态。
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                job.id, str(job.type), str(job.status), job.progress,
                json.dumps(job.parameters, ensure_ascii=False), None, None, 0,
                job.created_at.isoformat(), None, None,
            ))

    def get(self, job_id: str) -> JobRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self.decode(row) if row else None

    def list(self, limit: int = 50) -> list[JobRecord]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self.decode(row) for row in rows]

    def recover_interrupted(self) -> int:
        """Mark jobs orphaned by a process restart as errors instead of leaving them running forever."""
        finished_at = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE jobs SET status=?,error=?,finished_at=? WHERE status IN (?,?)",
                (JobStatus.ERROR.value, "worker process restarted before completion", finished_at,
                 JobStatus.QUEUED.value, JobStatus.RUNNING.value),
            )
            return cursor.rowcount

    def cancel_requested(self, job_id: str) -> bool:
        """只读取消标记（比 get() 轻量：job 运行期间会反复轮询）。"""
        with self.connect() as db:
            row = db.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def update(self, job_id: str, **values: object) -> None:
        allowed = {"status", "progress", "result_json", "error", "cancel_requested", "started_at", "finished_at"}
        clean = {key: value for key, value in values.items() if key in allowed}
        if not clean:
            return
        sql = ",".join(f"{key}=?" for key in clean)
        encoded = [value.value if isinstance(value, JobStatus) else value for value in clean.values()]
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET {sql} WHERE id=?", (*encoded, job_id))
