import tempfile
import unittest
from pathlib import Path

from quant_engine.models import JobRecord, JobStatus, JobType, utc_now
from quant_engine.repository import JobRepository


class JobRepositoryTest(unittest.TestCase):
    def test_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JobRepository(Path(directory) / "jobs.db")
            expected = JobRecord(
                id="features-test", type=JobType.FEATURES, status=JobStatus.QUEUED,
                parameters={"trade_date": "20260901"}, created_at=utc_now(),
            )
            repository.insert(expected)
            actual = repository.get(expected.id)
            self.assertIsNotNone(actual)
            self.assertEqual(actual.type, JobType.FEATURES)
            self.assertEqual(actual.parameters, expected.parameters)

    def test_recover_interrupted_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JobRepository(Path(directory) / "jobs.db")
            job = JobRecord(id="backtest-interrupted", type=JobType.BACKTEST,
                            status=JobStatus.QUEUED, created_at=utc_now())
            repository.insert(job)
            self.assertEqual(repository.recover_interrupted(), 1)
            recovered = repository.get(job.id)
            self.assertEqual(recovered.status, JobStatus.ERROR)
            self.assertIn("restarted", recovered.error)


class RetiredJobTypeTest(unittest.TestCase):
    """已退役的 job 类型必须仍可读出——一条历史记录不该打挂整个列表接口。

    真实事故（2026-09-20）：legacy 因子选股退役后 JobType 删掉了 `factor_mining`，
    但库里还留着 15 条该类型的历史记录。`decode` 对它们抛 ValueError，导致
    `GET /api/v1/jobs?limit=50` 直接 500（这 15 条最高排在按时间倒序的第 46 位，
    所以 limit<=45 正常、limit=50 必炸）。Starlette 的 500 是纯文本，
    前端 `response.json()` 还会再抛一次，用户看到的是
    "读取计算任务失败：Unexpected token 'I', "Internal S"... is not valid JSON"。
    """

    def _seed_legacy_row(self, repository: JobRepository, job_id: str, job_type: str) -> None:
        """绕过 JobRecord 直接写库，模拟"类型已从枚举里删除"的历史记录。"""
        with repository.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,type,status,progress,parameters_json,result_json,error,"
                "cancel_requested,created_at,started_at,finished_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, job_type, "complete", 100, "{}", '{"ok":true}', None, 0,
                 utc_now().isoformat(), None, utc_now().isoformat()))

    def test_retired_type_is_listed_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JobRepository(Path(directory) / "jobs.db")
            self._seed_legacy_row(repository, "factor_mining-legacy-1", "factor_mining")
            rows = repository.list(50)          # 不得抛异常
            self.assertEqual(len(rows), 1)
            # 原样保留，便于识别这是已退役类型（不是伪造一个合法类型）
            self.assertEqual(rows[0].type, "factor_mining")
            self.assertEqual(rows[0].result, {"ok": True})

    def test_retired_type_does_not_hide_valid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JobRepository(Path(directory) / "jobs.db")
            self._seed_legacy_row(repository, "factor_mining-legacy-2", "factor_mining")
            good = JobRecord(id="features-1", type=JobType.FEATURES,
                             status=JobStatus.COMPLETE, created_at=utc_now())
            repository.insert(good)
            rows = repository.list(50)
            self.assertEqual(len(rows), 2, "合法记录不能被退役记录挤掉")
            self.assertIn("features-1", [r.id for r in rows])

    def test_submitting_retired_type_is_still_rejected(self) -> None:
        """只放宽读取；提交侧 JobCreate.type 仍是严格 JobType。"""
        from pydantic import ValidationError

        from quant_engine.models import JobCreate

        with self.assertRaises(ValidationError):
            JobCreate(type="factor_mining")

    def test_unknown_status_is_also_tolerated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = JobRepository(Path(directory) / "jobs.db")
            self._seed_legacy_row(repository, "legacy-status-1", "features")
            with repository.connect() as db:
                db.execute("UPDATE jobs SET status=? WHERE id=?", ("some_removed_status", "legacy-status-1"))
            rows = repository.list(50)
            self.assertEqual(rows[0].status, "some_removed_status")

    def test_label_helper_covers_every_current_type(self) -> None:
        """标签表与枚举不能漂移：每个 JobType 都必须有显示名。"""
        from quant_engine.models import JOB_TYPE_LABELS, UNKNOWN_JOB_TYPE_LABEL, job_type_label

        for member in JobType:
            self.assertIn(member.value, JOB_TYPE_LABELS, f"{member.value} 缺少显示名")
        self.assertEqual(job_type_label("factor_mining"), UNKNOWN_JOB_TYPE_LABEL)
        self.assertEqual(job_type_label(JobType.STOCK_WALKFORWARD), JOB_TYPE_LABELS["stock_walkforward"])
        self.assertEqual(job_type_label(None), UNKNOWN_JOB_TYPE_LABEL)


if __name__ == "__main__":
    unittest.main()
