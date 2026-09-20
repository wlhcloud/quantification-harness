"""管理 key 收敛测试（配置面：同一事实只能有一个权威来源）。

背景：管理 key 曾经在 3 个地方各写一份（`QUANT_SYNC_API_KEY`、`QUANT_ENGINE_API_KEY`、
前端 `VITE_QUANT_API_KEY`），轮换要改多处，而且**key 为空时鉴权会静默失效**。
现在权威变量是 `QUANT_API_KEY`，服务专属变量仅作兼容回退——本用例锁住这个优先级：
写反了会出现"改了 QUANT_API_KEY 却被陈旧的旧变量覆盖"。
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from quant_engine.settings import SHARED_API_KEY_ENV, Settings


class EngineApiKeyTest(unittest.TestCase):
    def _settings(self, **env: str) -> Settings:
        clean = {name: "" for name in (SHARED_API_KEY_ENV, "QUANT_ENGINE_API_KEY")}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=False):
            for name, value in clean.items():
                if value:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            return Settings()

    def test_canonical_key_is_used(self):
        settings = self._settings(QUANT_API_KEY="canonical")
        self.assertEqual(settings.api_key, "canonical")
        self.assertEqual(settings.api_key_source, SHARED_API_KEY_ENV)

    def test_legacy_key_still_works(self):
        settings = self._settings(QUANT_ENGINE_API_KEY="legacy")
        self.assertEqual(settings.api_key, "legacy")
        self.assertEqual(settings.api_key_source, "QUANT_ENGINE_API_KEY")

    def test_canonical_wins_over_stale_legacy(self):
        settings = self._settings(QUANT_API_KEY="canonical", QUANT_ENGINE_API_KEY="stale")
        self.assertEqual(settings.api_key, "canonical")
        self.assertEqual(settings.api_key_source, SHARED_API_KEY_ENV)

    def test_empty_env_keeps_auth_disabled_and_says_so(self):
        settings = self._settings()
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.api_key_source, "")


if __name__ == "__main__":
    unittest.main()
