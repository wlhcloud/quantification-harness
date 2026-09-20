"""quant-engine HTTP 层测试（此前零覆盖）。

隔离方式：在导入 main 之前把 QUANT_ENGINE_PROJECT_ROOT 指到临时目录
（pydantic-settings 的 env_prefix=QUANT_ENGINE_ 直接生效），
这样模块级构造的 JobRepository / settings 全部落在临时目录，不碰真实 data/。
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="qe-http-", ignore_cleanup_errors=True)
os.environ["QUANT_ENGINE_PROJECT_ROOT"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from quant_engine import main  # noqa: E402
from quant_engine.models import JobStatus  # noqa: E402


def _wait_job_terminal(client: TestClient, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    body: dict = {}
    while time.time() < deadline:
        body = client.get(f"/api/v1/jobs/{job_id}").json()
        if body["status"] in {JobStatus.COMPLETE.value, JobStatus.ERROR.value, JobStatus.CANCELLED.value}:
            return body
        time.sleep(0.05)
    raise AssertionError(f"job 未在预期时间内进入终态：{body.get('status')}")


class EngineHttpTest(unittest.TestCase):
    client: TestClient

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_jobs_list_and_lookup_round_trip(self):
        """列表接口可用，且提交的 job 能在列表/详情里查到（不假设初始为空：用例顺序不固定）。"""
        listing = self.client.get("/api/v1/jobs")
        self.assertEqual(listing.status_code, 200)
        self.assertIsInstance(listing.json(), list)
        created = self.client.post("/api/v1/jobs", json={"type": "features", "parameters": {"probe": True}})
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["id"]
        self.assertIn(job_id, [item["id"] for item in self.client.get("/api/v1/jobs").json()])
        detail = self.client.get(f"/api/v1/jobs/{job_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["parameters"], {"probe": True})

    def test_unknown_job_type_is_rejected(self):
        response = self.client.post("/api/v1/jobs", json={"type": "not_a_job"})
        self.assertEqual(response.status_code, 422)  # pydantic 枚举校验

    def test_backtest_gate_blocks_without_force(self):
        """数据准入未通过时拒绝；带风险运行必须给 ≥10 字原因（这里锁死 422 契约）。

        月度回测已在 2026-09-18 随 legacy 因子模型退役（见下一个用例），
        因此这里把"软失败 gate"和"存在带权重模型"两个前提显式注入，
        保证测的是"强制运行必须写足原因"这条契约本身。
        """
        original_gate = main.check_backtest_readiness
        original_models = main._weighted_model_ids
        original_block = main._monthly_backtest_block
        main._weighted_model_ids = lambda: ["balanced"]
        main._monthly_backtest_block = lambda: None  # 避开"legacy 已退役"硬阻断
        main.check_backtest_readiness = lambda kind, market, finance, minute, start=None, end=None: {
            "kind": kind, "range": None, "allowed": False, "checkedAt": "test",
            "checks": [{"id": "test-soft-fail", "label": "测试用软失败", "status": "fail", "detail": "数据不完整"}],
            "blockedReasons": ["测试用：数据不完整"], "forcePolicy": "带风险运行需 ≥10 字原因",
        }
        self.addCleanup(lambda: (setattr(main, "check_backtest_readiness", original_gate),
                                 setattr(main, "_weighted_model_ids", original_models),
                                 setattr(main, "_monthly_backtest_block", original_block)))

        blocked = self.client.post("/api/v1/jobs", json={"type": "backtest", "parameters": {"kind": "monthly"}})
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("gate", blocked.json()["detail"])
        short = self.client.post("/api/v1/jobs", json={
            "type": "backtest", "parameters": {"kind": "monthly", "forceDataGate": True, "forceReason": "太短"}})
        self.assertEqual(short.status_code, 422)

    def test_monthly_backtest_is_retired_with_legacy_models(self):
        """legacy 因子选股退役后，月度（因子）回测必须直接判不通过，且不可 force 绕过。

        不这么做的后果是 `run_monthly` 走到 `used / total_weight` 时因空权重除零崩溃
        （ml_walkforward 的 weights 为空）。测试环境没有 config/，因此显式注入"配置可读但没有带权重模型"。
        """
        original_gate = main.check_backtest_readiness
        original_block = main._monthly_backtest_block
        main._monthly_backtest_block = lambda: {
            "id": "monthly-backtest-retired", "label": "月度因子回测可用性", "status": "fail",
            "detail": "legacy 因子选股已退役（config/factor-models.yaml 只剩 ml_walkforward）",
        }
        self.addCleanup(lambda: (setattr(main, "check_backtest_readiness", original_gate),
                                 setattr(main, "_monthly_backtest_block", original_block)))

        readiness = self.client.get("/api/v1/backtest/readiness?kind=monthly")
        self.assertEqual(readiness.status_code, 200)
        body = readiness.json()
        self.assertFalse(body["allowed"])
        self.assertTrue(body["hardBlocked"])
        self.assertEqual([c["id"] for c in body["checks"]], ["monthly-backtest-retired"])

        blocked = self.client.post("/api/v1/jobs", json={"type": "backtest", "parameters": {"kind": "monthly"}})
        self.assertEqual(blocked.status_code, 409)
        forced = self.client.post("/api/v1/jobs", json={
            "type": "backtest",
            "parameters": {"kind": "monthly", "forceDataGate": True, "forceReason": "我有充分理由要强行跑一次"}})
        self.assertEqual(forced.status_code, 409, "硬阻断不允许通过 forceDataGate 绕过")

    def test_real_config_has_no_legacy_models(self):
        """锁死退役事实：真实 config 里只剩 ml_walkforward，且它没有权重（不参与加权回测）。"""
        import json as _json
        config_path = Path(__file__).resolve().parents[3] / "config" / "factor-models.yaml"
        self.assertTrue(config_path.exists(), f"缺少模型配置：{config_path}")
        config = _json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(list(config["models"]), ["ml_walkforward"],
                         "legacy 因子模型应已退役，只保留 ml_walkforward")
        self.assertEqual(config["models"]["ml_walkforward"]["weights"], {})
        self.assertIn("_retired", config, "退役说明应留在配置里，便于事后追溯")

    def test_submitted_job_reaches_terminal_state(self):
        """端到端：提交 → 轮询 → 终态（临时目录里没有 market.db，因此会以 error 收尾）。"""
        created = self.client.post("/api/v1/jobs", json={"type": "features", "parameters": {}})
        self.assertEqual(created.status_code, 202)
        job_id = created.json()["id"]
        deadline = time.time() + 30
        status = None
        while time.time() < deadline:
            status = self.client.get(f"/api/v1/jobs/{job_id}").json()["status"]
            if status in {JobStatus.COMPLETE.value, JobStatus.ERROR.value, JobStatus.CANCELLED.value}:
                break
            time.sleep(0.05)
        self.assertIn(status, {JobStatus.COMPLETE.value, JobStatus.ERROR.value},
                      f"job 未在预期时间内进入终态：{status}")

    def test_short_backtest_is_disabled_by_default(self):
        """股票 5 分钟线已清除：短线回测准入必须直接判不通过（且不可 force 绕过）。"""
        self.assertFalse(main.settings.short_backtest_enabled)
        readiness = self.client.get("/api/v1/backtest/readiness?kind=short&start_date=20260101&end_date=20260131")
        self.assertEqual(readiness.status_code, 200)
        body = readiness.json()
        self.assertFalse(body["allowed"])
        self.assertTrue(any("短线回测已停用" in reason for reason in body["blockedReasons"]))
        self.assertIn("不可通过 forceDataGate 绕过", body["forcePolicy"])

        blocked = self.client.post("/api/v1/jobs", json={
            "type": "backtest",
            "parameters": {"kind": "short", "forceDataGate": True,
                           "forceReason": "临时验证停用开关是否可被绕过"}})
        self.assertEqual(blocked.status_code, 409, "hardBlocked 的准入不应被 forceDataGate 绕过")

    def test_monthly_readiness_is_unaffected(self):
        readiness = self.client.get("/api/v1/backtest/readiness?kind=monthly")
        self.assertEqual(readiness.status_code, 200)
        body = readiness.json()
        self.assertFalse(any("短线回测已停用" in reason for reason in body["blockedReasons"]),
                         "停用短线不应影响月度回测的准入结论")

    def test_missing_job_returns_404(self):
        self.assertEqual(self.client.get("/api/v1/jobs/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/v1/jobs/nope/cancel").status_code, 404)

    def test_stock_ml_backtest_and_predict_are_jobified(self):
        """原先是同步端点（点击后全程转圈）；现在也提供 job 化入口，前端可展示进度。"""
        for job_type in ("stock_ml_backtest", "stock_ml_predict"):
            with self.subTest(job_type=job_type):
                created = self.client.post("/api/v1/jobs", json={"type": job_type, "parameters": {"topN": 5}})
                self.assertEqual(created.status_code, 202)
                # 临时目录里没有模型文件 → 会以 error 收尾（说明真的执行到了 handler，而不是被拒）
                done = _wait_job_terminal(self.client, created.json()["id"])
                self.assertIn(done["status"], {"error", "complete"})

    def test_technical_timing_backtest_endpoint(self):
        response = self.client.get("/api/v1/stock/technical-timing/backtest/latest?variant=long")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["variant"], "long")
        self.assertIn(body["status"], {"ok", "none", "empty"})
        self.assertEqual(self.client.get("/api/v1/stock/technical-timing/backtest/latest?variant=v9").status_code, 422)

    def test_api_key_guard_covers_write_endpoints_only(self):
        """设置 QUANT_ENGINE_API_KEY 后：写操作必须带 X-API-Key，读操作仍开放。"""
        original = main.settings.api_key
        main.settings.api_key = "secret-key"
        try:
            self.assertEqual(self.client.get("/api/v1/jobs").status_code, 200)
            self.assertEqual(self.client.post("/api/v1/jobs", json={"type": "features"}).status_code, 401)
            allowed = self.client.post("/api/v1/jobs", json={"type": "features"},
                                       headers={"X-API-Key": "secret-key"})
            self.assertEqual(allowed.status_code, 202)
            self.assertEqual(self.client.post("/api/v1/jobs", json={"type": "features"},
                                              headers={"X-API-Key": "wrong"}).status_code, 401)
        finally:
            main.settings.api_key = original


if __name__ == "__main__":
    unittest.main()
