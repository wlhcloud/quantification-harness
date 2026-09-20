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


if __name__ == "__main__":
    unittest.main()
