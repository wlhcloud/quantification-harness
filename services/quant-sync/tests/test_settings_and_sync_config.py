"""配置面收敛测试（quant-sync 侧）：管理 key 优先级 + sync-config.json 的合并/死键。

背景见 `docs/configuration.md`：
1. 管理 key 权威变量是 `QUANT_API_KEY`，服务专属旧名仅作回退；
2. `data/sync-config.json`（运行时可改）与环境变量默认值是"同一批事实"的两个来源，
   必须深层合并——浅合并会让 JSON 里的部分覆盖把同节其它键整块丢掉。

隔离方式与 `test_http_api.py` 一致：导入 `quant_sync.main` 之前把 project_root 指到临时目录、
关掉调度线程（否则交易时段会真的发起上游同步）。导入的库连接会持有文件句柄，
所以临时目录用 `ignore_cleanup_errors=True`（Windows 上不这样做 tearDown 会报 PermissionError）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]

_TMP = tempfile.TemporaryDirectory(prefix="qs-cfg-", ignore_cleanup_errors=True)
os.environ["QUANT_SYNC_PROJECT_ROOT"] = _TMP.name
os.environ["QUANT_SYNC_SCHEDULER_ENABLED"] = "0"
_CONTRACT_DIR = Path(_TMP.name) / "contracts" / "schemas"
_CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "contracts" / "schemas" / "meta.sql", _CONTRACT_DIR / "meta.sql")

from quant_sync import main  # noqa: E402
from quant_sync.settings import SHARED_API_KEY_ENV, Settings  # noqa: E402


class SyncApiKeyTest(unittest.TestCase):
    def _settings(self, **env: str) -> Settings:
        clean = {SHARED_API_KEY_ENV: "", "QUANT_SYNC_API_KEY": ""}
        clean.update(env)
        with mock.patch.dict(os.environ, clean, clear=False):
            for name, value in clean.items():
                if value:
                    os.environ[name] = value
                else:
                    os.environ.pop(name, None)
            return Settings()

    def test_canonical_key_wins(self):
        settings = self._settings(QUANT_API_KEY="canonical", QUANT_SYNC_API_KEY="stale")
        self.assertEqual(settings.api_key, "canonical")
        self.assertEqual(settings.api_key_source, SHARED_API_KEY_ENV)

    def test_legacy_fallback(self):
        settings = self._settings(QUANT_SYNC_API_KEY="legacy")
        self.assertEqual(settings.api_key, "legacy")
        self.assertEqual(settings.api_key_source, "QUANT_SYNC_API_KEY")

    def test_empty_env_disables_auth_explicitly(self):
        settings = self._settings()
        self.assertEqual(settings.api_key, "")
        self.assertEqual(settings.api_key_source, "")


class SyncConfigMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_deep_merge_keeps_siblings(self):
        merged = main._deep_merge(
            {"minute": {"concurrency": 10, "oneMinuteSegmentDays": 30}, "requestTimeoutMs": 30000},
            {"minute": {"concurrency": 3}},
        )
        self.assertEqual(merged["minute"], {"concurrency": 3, "oneMinuteSegmentDays": 30})
        self.assertEqual(merged["requestTimeoutMs"], 30000)

    def test_deep_merge_scalar_and_new_keys(self):
        self.assertEqual(main._deep_merge({"a": {"b": 1}}, {"a": 2, "c": {"d": 3}}), {"a": 2, "c": {"d": 3}})

    def test_load_config_merges_partial_json_over_env_defaults(self):
        config_path = self.root / "partial.json"
        config_path.write_text(json.dumps({"minute": {"concurrency": 7}}), encoding="utf-8")
        with mock.patch.object(main, "CONFIG_PATH", config_path):
            loaded = main._load_config()
        self.assertEqual(loaded["minute"]["concurrency"], 7)
        # 环境变量给的段天数不能被"部分覆盖"整块吃掉（浅合并的旧行为会丢）
        self.assertEqual(loaded["minute"]["oneMinuteSegmentDays"], main.settings.one_minute_segment_days)
        self.assertEqual(loaded["minute"]["fiveMinuteSegmentDays"], main.settings.five_minute_segment_days)
        self.assertIn("marketScheduler", loaded)

    def test_broken_json_falls_back_to_defaults(self):
        config_path = self.root / "broken.json"
        config_path.write_text("{not json", encoding="utf-8")
        with mock.patch.object(main, "CONFIG_PATH", config_path):
            self.assertEqual(main._load_config(), main._default_config())

    def test_unknown_keys_are_reported_not_dropped(self):
        unknown = main._unknown_config_keys({"minute": {"concurrency": 1, "typo": 2},
                                             "marketScheduler": {"enabled": True, "wat": 1},
                                             "requestTimeoutMs": 1000, "extra": {}})
        self.assertEqual(unknown, ["extra", "marketScheduler.wat", "minute.typo"])
        self.assertEqual(main._unknown_config_keys(main._default_config()), [])

    def test_repo_sync_config_has_no_unknown_keys(self):
        """仓库里的真实 sync-config.json 不能有死键（有的话说明配置面又漂了）。"""
        live = ROOT / "data" / "sync-config.json"
        if not live.exists():
            self.skipTest("无 data/sync-config.json（CI/全新环境）")
        data = json.loads(live.read_text(encoding="utf-8"))
        self.assertEqual(main._unknown_config_keys(data), [])


if __name__ == "__main__":
    unittest.main()
