"""每日链的编排保护测试：单实例锁 + 运行台账。

背景（编排收敛）：链脚本被 Windows 计划任务每 30 分钟拉起一次，也可能被手工/豆包任务
同时调用；原实现**没有任何重入保护**（0 处锁/标记），只靠任务设置的 IgnoreNew 兜住。
现在脚本自带锁（含残留锁接管）并把每次运行写进 JSONL 台账，便于核对是否漏跑/叠加。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

import daily_stock_chain as chain  # noqa: E402


class ChainLockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self._orig_lock, self._orig_ledger = chain.LOCK_PATH, chain.RUN_LEDGER
        chain.LOCK_PATH = self.root / ".daily-chain.lock"
        chain.RUN_LEDGER = self.root / "runs.jsonl"
        self.addCleanup(self._restore)

    def _restore(self):
        chain.LOCK_PATH, chain.RUN_LEDGER = self._orig_lock, self._orig_ledger

    def test_second_acquire_is_rejected_while_held(self):
        self.assertTrue(chain._acquire_chain_lock())
        self.assertFalse(chain._acquire_chain_lock(), "持锁期间应拒绝第二次进入")
        payload = json.loads(chain.LOCK_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload["pid"], __import__("os").getpid())
        chain._release_chain_lock()
        self.assertTrue(chain._acquire_chain_lock(), "释放后应能重新获取")
        chain._release_chain_lock()

    def test_stale_lock_is_taken_over(self):
        chain.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        stale = (datetime.now() - timedelta(hours=7)).isoformat(timespec="seconds")
        chain.LOCK_PATH.write_text(json.dumps({"pid": 12345, "startedAt": stale}), encoding="utf-8")
        self.assertTrue(chain._acquire_chain_lock(), "残留锁（>6h）应被接管")
        payload = json.loads(chain.LOCK_PATH.read_text(encoding="utf-8"))
        self.assertNotEqual(payload["pid"], 12345)
        chain._release_chain_lock()

    def test_corrupt_lock_is_taken_over(self):
        chain.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        chain.LOCK_PATH.write_text("not-json", encoding="utf-8")
        self.assertTrue(chain._acquire_chain_lock())
        chain._release_chain_lock()

    def test_run_ledger_records_each_run(self):
        started = datetime.now() - timedelta(seconds=5)
        chain._append_run_ledger(started, 0)
        chain._append_run_ledger(started, 1)
        lines = [json.loads(line) for line in chain.RUN_LEDGER.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["exitCode"], 0)
        self.assertEqual(lines[1]["exitCode"], 1)
        self.assertGreaterEqual(lines[0]["durationSec"], 4)

    def test_cancellation_and_freshness_helpers_are_importable(self):
        # 链里新增的辅助因子判定可被独立调用（无需联网）
        self.assertTrue(callable(chain._aux_factor_stale))
        self.assertTrue(callable(chain._advance_stock_sim))

    def test_api_key_prefers_canonical_variable(self):
        """key 收敛：链与两个服务读同一个权威变量 QUANT_API_KEY，旧名仅作回退。"""
        import os
        from unittest import mock

        names = ("QUANT_API_KEY", "QUANT_ENGINE_API_KEY", "QUANT_SYNC_API_KEY")
        with mock.patch.dict(os.environ, {n: "" for n in names}, clear=False):
            for name in names:
                os.environ.pop(name, None)
            self.assertEqual(chain._api_key(), "", "三个变量都为空时应返回空串（不带鉴权头）")
            os.environ["QUANT_ENGINE_API_KEY"] = "legacy"
            self.assertEqual(chain._api_key(), "legacy")
            os.environ["QUANT_SYNC_API_KEY"] = "legacy2"
            self.assertEqual(chain._api_key(), "legacy", "引擎旧名优先于同步旧名（历史上两者同值）")
            os.environ["QUANT_API_KEY"] = "canonical"
            self.assertEqual(chain._api_key(), "canonical", "权威变量必须压过陈旧旧名")


if __name__ == "__main__":
    unittest.main()
