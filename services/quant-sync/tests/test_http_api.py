"""quant-sync HTTP 层测试（此前零覆盖）。

隔离方式（导入 main 之前设置）：
  QUANT_SYNC_PROJECT_ROOT      -> 临时目录（meta/market/minute/sync 库全部落临时目录）
  QUANT_SYNC_SCHEDULER_ENABLED -> 0，避免导入时启动行情调度线程（交易时段会自动发起真实上游同步）

用例刻意只走"校验即返回 / 无上游调用"的端点，测试进程不联网。
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="qs-http-", ignore_cleanup_errors=True)
os.environ["QUANT_SYNC_PROJECT_ROOT"] = _TMP.name
os.environ["QUANT_SYNC_SCHEDULER_ENABLED"] = "0"

# 模拟一个"已部署"的项目根：契约 DDL 必须在 project_root/contracts 下（服务据此自举 meta.db）
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT_DIR = Path(_TMP.name) / "contracts" / "schemas"
_CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
shutil.copy2(_REPO_ROOT / "contracts" / "schemas" / "meta.sql", _CONTRACT_DIR / "meta.sql")

from fastapi.testclient import TestClient  # noqa: E402

from quant_sync import main  # noqa: E402


class SyncHttpTest(unittest.TestCase):
    client: TestClient

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(main.app)

    def test_scheduler_is_disabled_in_tests(self):
        self.assertFalse(main.settings.scheduler_enabled)
        self.assertEqual(main.market_scheduler.state["status"], "stopped")

    def test_health(self):
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        # 只读接口不得回传上游 token 原文
        self.assertNotIn("token", {k.lower() for k in body.keys()})

    def test_health_reports_minute_capability(self):
        """前端靠这个字段决定是否禁用分钟同步入口——缺失会让页面继续暴露必 410 的按钮。"""
        body = self.client.get("/api/v1/health").json()
        self.assertIn("minuteSyncEnabled", body)
        self.assertEqual(body["minuteSyncEnabled"], main.settings.minute_sync_enabled)
        original = main.settings.minute_sync_enabled
        main.settings.minute_sync_enabled = not original
        self.addCleanup(lambda: setattr(main.settings, "minute_sync_enabled", original))
        self.assertEqual(self.client.get("/api/v1/health").json()["minuteSyncEnabled"], not original)

    def test_readonly_endpoints_are_available(self):
        for path in ("/api/v1/records", "/api/v1/sources", "/api/v1/routes",
                     "/api/v1/sync/minute/all/status", "/api/v1/sync/daily/history/status"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

    def test_daily_history_rejects_bad_dates(self):
        bad = self.client.post("/api/v1/sync/daily/history?start_date=2026-01-01&end_date=20260131")
        self.assertEqual(bad.status_code, 400)
        reversed_range = self.client.post("/api/v1/sync/daily/history?start_date=20260201&end_date=20260101")
        self.assertEqual(reversed_range.status_code, 400)

    def _minute_enabled(self):
        """临时打开分钟同步开关，用于验证 410 之外的既有逻辑（锁/参数校验）。"""
        original = main.settings.minute_sync_enabled
        main.settings.minute_sync_enabled = True
        self.addCleanup(lambda: setattr(main.settings, "minute_sync_enabled", original))

    def test_minute_retry_returns_404_without_failed_run(self):
        # 临时库里没有任何 minute run -> 404（且不会启动后台任务）
        self._minute_enabled()
        response = self.client.post("/api/v1/sync/minute/all/retry")
        self.assertEqual(response.status_code, 404)

    def test_minute_retry_conflicts_while_running(self):
        """锁语义：状态检查在 _start_locks["minute"] 内完成，运行中必须 409 而不是再启动一个。"""
        self._minute_enabled()
        original = main.minute.state.get("status")
        main.minute.state["status"] = "running"
        try:
            response = self.client.post("/api/v1/sync/minute/all/retry")
            self.assertEqual(response.status_code, 409)
        finally:
            main.minute.state["status"] = original or "idle"

    def test_minute_all_validates_freq(self):
        self._minute_enabled()
        self.assertEqual(self.client.post("/api/v1/sync/minute/all?freq=3m").status_code, 400)

    def test_minute_sync_is_disabled_by_default(self):
        """股票分钟线已清零：默认停用，三个入口都必须 410，避免误触发重新下载 21GB。"""
        self.assertFalse(main.settings.minute_sync_enabled)
        for path in ("/api/v1/sync/minute?code=000001.SZ",
                     "/api/v1/sync/minute/all?freq=5m",
                     "/api/v1/sync/minute/all/retry"):
            with self.subTest(path=path):
                response = self.client.post(path)
                self.assertEqual(response.status_code, 410)
                self.assertIn("停用", response.json()["detail"])

    def test_minute_guard_is_the_only_blocker_when_enabled(self):
        original = main.settings.minute_sync_enabled
        main.settings.minute_sync_enabled = True
        try:
            # 重新打开后，非法 freq 走到参数校验（400）而不是 410，证明 410 来自停用开关
            self.assertEqual(self.client.post("/api/v1/sync/minute/all?freq=3m").status_code, 400)
        finally:
            main.settings.minute_sync_enabled = original

    def test_api_key_guard_covers_write_endpoints_only(self):
        original = main.settings.api_key
        main.settings.api_key = "secret-key"
        try:
            # 读接口始终开放
            self.assertEqual(self.client.get("/api/v1/records").status_code, 200)
            # 写接口缺 key -> 401
            denied = self.client.post("/api/v1/settings/data-sources/test", json={"id": "nope"})
            self.assertEqual(denied.status_code, 401)
            # 带正确 key -> 通过中间件，进入处理器（未知 id 由处理器返回 404，不触网）
            allowed = self.client.post("/api/v1/settings/data-sources/test",
                                       json={"id": "nope"}, headers={"X-API-Key": "secret-key"})
            self.assertEqual(allowed.status_code, 404)
            wrong = self.client.post("/api/v1/settings/data-sources/test",
                                     json={"id": "nope"}, headers={"X-API-Key": "wrong"})
            self.assertEqual(wrong.status_code, 401)
        finally:
            main.settings.api_key = original


if __name__ == "__main__":
    unittest.main()
