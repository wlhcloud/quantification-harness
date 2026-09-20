"""TushareClient 多源回退与超时传参测试。

修复背景：
  1. 原实现遇到 PendingError（429/202 上游"未就绪/限流"）立即 re-raise，**不尝试备用通道**；
     现改为：记下 pending 继续尝试备用源，若所有通道都 pending 仍抛 PendingError（调用方语义不变）。
  2. 上游超时原先只能通过 TushareClient 的全局 `_run_timeout` 覆盖，不同 kind 的同步
     （分钟/日线/ETF 各持一把锁、彼此不互斥）并发时会互相改值；现支持 call(..., timeout_ms=) 显式传入。
"""
from __future__ import annotations

import unittest

from quant_sync.meta import DataSource
from quant_sync.upstream import GatewayResult, PendingError, TushareClient, UpstreamError


def _source(sid: str, priority: int = 1) -> DataSource:
    return DataSource(id=sid, protocol="x-api-key", base_url=f"https://{sid}.example",
                      token="t", timeout_ms=15000, label=sid, priority=priority)


class FakeMeta:
    def __init__(self, sources: list[DataSource]):
        self._sources = sources
        self.registered: list[tuple] = []

    def sources(self) -> list[DataSource]:
        return list(self._sources)

    def tool_route(self, api: str) -> list[DataSource]:
        return list(self._sources)


class ClientFallbackTest(unittest.TestCase):
    def _client(self, behaviour: dict[str, object]) -> tuple[TushareClient, list[dict]]:
        """behaviour: source_id -> rows | Exception"""
        calls: list[dict] = []
        client = TushareClient(FakeMeta([_source("primary"), _source("backup", 2)]))

        def fake_request(source, api, params, timeout_ms=None):
            calls.append({"source": source.id, "api": api, "timeout_ms": timeout_ms})
            outcome = behaviour[source.id]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        client._request = fake_request  # type: ignore[method-assign]
        return client, calls

    def test_falls_back_to_backup_on_pending(self):
        client, calls = self._client({"primary": PendingError("UPSTREAM_PENDING:30"),
                                      "backup": [{"a": 1}]})
        result = client.call("daily", {"trade_date": "20260911"})
        self.assertEqual(result.source, "backup")
        self.assertEqual(result.rows, [{"a": 1}])
        self.assertEqual([c["source"] for c in calls], ["primary", "backup"])

    def test_all_sources_pending_still_raises_pending(self):
        client, _ = self._client({"primary": PendingError("UPSTREAM_PENDING:30"),
                                  "backup": PendingError("UPSTREAM_PENDING:30")})
        with self.assertRaises(PendingError):
            client.call("stk_mins", {})

    def test_falls_back_on_upstream_error(self):
        client, _ = self._client({"primary": UpstreamError("HTTP 500"), "backup": [{"a": 2}]})
        self.assertEqual(client.call("daily", {}).source, "backup")

    def test_all_sources_failed_raises_upstream_error(self):
        client, _ = self._client({"primary": UpstreamError("HTTP 500"),
                                  "backup": UpstreamError("HTTP 503")})
        with self.assertRaises(UpstreamError):
            client.call("daily", {})

    def test_explicit_timeout_is_forwarded(self):
        client, calls = self._client({"primary": [{"a": 1}], "backup": []})
        client.call("daily", {}, timeout_ms=1234)
        self.assertEqual(calls[0]["timeout_ms"], 1234)

    def test_timeout_defaults_to_none_when_not_given(self):
        client, calls = self._client({"primary": [{"a": 1}], "backup": []})
        client.call("daily", {})
        self.assertIsNone(calls[0]["timeout_ms"])


class RequestTimeoutResolutionTest(unittest.TestCase):
    """_request 内部的超时解析顺序：显式参数 > 全局覆盖 > 数据源默认。"""

    def _capture_timeout(self, **kwargs) -> float:
        captured: dict[str, float] = {}
        client = TushareClient(FakeMeta([_source("primary")]))

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self) -> bytes:
                return b'{"code": 0, "data": {"fields": ["a"], "items": [[1]]}}'

        def fake_urlopen(request, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse()

        import quant_sync.upstream as upstream_module
        original = upstream_module.urllib.request.urlopen
        upstream_module.urllib.request.urlopen = fake_urlopen
        try:
            client._run_timeout = kwargs.pop("global_timeout", None)
            client._request(_source("primary"), "daily", {}, **kwargs)
        finally:
            upstream_module.urllib.request.urlopen = original
        return captured["timeout"]

    def test_source_default(self):
        self.assertAlmostEqual(self._capture_timeout(), 15.0)

    def test_global_override_wins_over_source_default(self):
        self.assertAlmostEqual(self._capture_timeout(global_timeout=60000), 60.0)

    def test_explicit_parameter_wins_over_global(self):
        self.assertAlmostEqual(self._capture_timeout(global_timeout=60000, timeout_ms=2000), 2.0)


if __name__ == "__main__":
    unittest.main()
