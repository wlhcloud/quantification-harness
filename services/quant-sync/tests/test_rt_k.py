import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from quant_sync.audit import AuditStore
from quant_sync.tool_sync import ToolSync


class _Client:
    def call(self, api, params, dataset=None):
        self.request = (api, params, dataset)
        row = {
            "code": "000001.SH", "name": "上证指数", "open": 3900, "high": 3950, "low": 3890,
            "close": 3936.8, "pre_close": 3930.1, "volume": 10, "amount": 20, "num": None,
        }
        return SimpleNamespace(rows=[row], raw_rows=[{"ts_code": "000001.SH"}], source="promax")


class _Meta:
    def register_dataset(self, *args):
        self.dataset = args


class RealtimeIndexSyncTest(unittest.TestCase):
    def test_writes_calculated_snapshot_and_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client, meta = _Client(), _Meta()
            audit = AuditStore(root / "sync.db")
            sync = ToolSync(client, root / "market.db", root / "finance.db", meta, audit)
            run_id = audit.start("rt_idx_k", {}, tool_id="rt_idx_k")
            sync.states["rt_idx_k"]["runId"] = run_id

            sync._sync_realtime_index("rt_idx_k")

            db = sqlite3.connect(root / "market.db")
            row = db.execute("SELECT code,change,pct_chg,source FROM index_snapshots").fetchone()
            db.close()
            self.assertEqual(row[0], "000001.SH")
            self.assertAlmostEqual(row[1], 6.7)
            self.assertAlmostEqual(row[2], 6.7 / 3930.1 * 100)
            self.assertEqual(row[3], "promax")
            self.assertEqual(client.request[0], "rt_idx_k")
            self.assertEqual(client.request[2], "market.index_snapshot")
            self.assertEqual(meta.dataset[0], "market.index_snapshot")
            self.assertEqual(audit.logs(run_id)[0]["source"], "promax")
            audit.db.close()


class RealtimeIndexFreqValidationTest(unittest.TestCase):
    """freq 只对分钟工具有效，日线工具 rt_idx_k 不该被它卡住。

    真实事故（2026-09-20）：`_sync_realtime_index` 对 freq 做**无条件**校验，
    于是从"上游工具"卡片点 rt_idx_k 的同步按钮必然 400
    （"index minute freq must be 1MIN/..."），该入口一次都不可能成功——
    而 rt_idx_k 是日线工具，请求参数里根本不带 freq。
    """

    def setUp(self):
        # 用 mkdtemp + addCleanup 而不是 `with TemporaryDirectory()`：
        # addCleanup 是 LIFO，先注册目录、后注册 db.close，关闭就一定发生在删目录之前。
        # 若写 `with` 块，目录清理会先于连接关闭，Windows 上直接 PermissionError(WinError 32)。
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(self._rmtree)

    def _rmtree(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _sync(self) -> ToolSync:
        self.audit = AuditStore(self.root / "sync.db")
        self.addCleanup(self.audit.db.close)      # 后注册 → 先执行
        self.client = _Client()
        return ToolSync(self.client, self.root / "market.db", self.root / "finance.db",
                        _Meta(), self.audit)

    def _start_and_wait(self, sync: ToolSync, tool_id: str, params: dict | None = None,
                        timeout: float = 5.0) -> dict:
        """走真实入口 start()（它起线程跑 _run），等到终态再断言。

        直接调 _sync_realtime_index 测不到状态流转——状态的设置/清理在 _run 里。
        """
        import time
        sync.start(tool_id, params or {})
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = sync.states[tool_id]
            if state["status"] not in ("running", "starting"):
                return state
            time.sleep(0.02)
        self.fail(f"{tool_id} 未在 {timeout}s 内结束：{sync.states[tool_id]}")

    def test_daily_tool_ignores_freq_entirely(self):
        sync = self._sync()
        # 前端默认会带 freq=1m（对分钟工具是非法的）；日线工具必须无视它而不是报错
        state = self._start_and_wait(sync, "rt_idx_k", {"freq": "1m"})
        self.assertEqual(state["status"], "complete", state.get("error"))
        self.assertNotIn("freq", self.client.request[1],
                         "日线工具的请求参数里不该出现 freq")

    def test_daily_tool_needs_no_params_at_all(self):
        """从卡片直接点同步（params={}）也必须能成功。"""
        sync = self._sync()
        state = self._start_and_wait(sync, "rt_idx_k", {})
        self.assertEqual(state["status"], "complete", state.get("error"))

    def test_minute_tool_still_validates_freq(self):
        """分钟工具的校验不能被一起放松掉。"""
        sync = self._sync()
        state = self._start_and_wait(sync, "rt_idx_min", {"freq": "1m"})   # 非法
        self.assertEqual(state["status"], "error")
        self.assertIn("freq", str(state.get("error") or ""))

    def test_minute_tool_accepts_valid_freq(self):
        sync = self._sync()
        state = self._start_and_wait(sync, "rt_idx_min", {"freq": "5MIN"})
        self.assertEqual(state["status"], "complete", state.get("error"))
        self.assertEqual(self.client.request[1].get("freq"), "5MIN")


if __name__ == "__main__":
    unittest.main()
