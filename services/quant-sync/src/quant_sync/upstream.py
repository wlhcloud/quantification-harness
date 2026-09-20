from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .meta import DataSource, MetaStore


class UpstreamError(Exception):
    pass


class PendingError(UpstreamError):
    pass


@dataclass
class GatewayResult:
    source: str
    rows: list[dict[str, Any]]
    raw_rows: list[dict[str, Any]]


_ENDPOINTS = {
    "stock_basic": "stock-basic",
    "daily_basic": "daily-basic",
    "fina_indicator": "fina-indicator",
    "trade_cal": "trade-cal",
    "stk_mins": "stk-mins",
    "rt_k": "rt-k",
}

# 布尔等式表达式：v==='1' || v===1
_BOOL_EQ = re.compile(r"^v\s*===\s*'1'\s*\|\|\s*v\s*===\s*1$")
# 数值缩放表达式：v*100 / value/100
_SCALE = re.compile(r"^(?:v|value)\s*([*/])\s*([0-9.]+)$")


def apply_transform(expression: str | None, value: Any) -> Any:
    """Port of the gateway's applyTransform: null -> null; ''/'v' -> passthrough;
    boolean equality -> bool; v/value * or / N -> numeric scale; unknown -> null."""
    if value is None:
        return None
    if not expression or expression.strip() == "v":
        return value
    expr = expression.strip()
    if _BOOL_EQ.match(expr):
        return bool(value == "1" or value == 1)
    match = _SCALE.match(expr)
    if match:
        try:
            factor = float(match.group(2))
            number = float(value)
            return number * factor if match.group(1) == "*" else number / factor
        except (TypeError, ValueError):
            return None
    return None


def decode(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if payload.get("code") != 0:
        raise UpstreamError(payload.get("msg") or f"upstream code {payload.get('code')}")
    data = payload.get("data") or {}
    fields = data.get("fields") or []
    items = data.get("items") or []
    return [dict(zip(fields, item)) for item in items]


def apply_dictionary(source_id: str, dataset_id: str, rows: list[dict[str, Any]], meta: MetaStore) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mappings = meta.mappings_for(dataset_id, source_id)
    if not mappings:
        return rows, rows
    standard: list[dict[str, Any]] = []
    for raw in rows:
        row: dict[str, Any] = {}
        for source_field, standard_field, expression in mappings:
            row[standard_field] = apply_transform(expression, raw.get(source_field))
        standard.append(row)
    return standard, rows


class TushareClient:
    """Multi-source Tushare channel (rds / promax / official) with fallback and dictionary normalization."""

    def __init__(self, meta: MetaStore, timeout_ms: int = 30000):
        self.meta = meta
        # 已废弃的全局覆盖：并发操作（分钟/日线/ETF 各持一把 kind 锁，彼此不互斥）会互相改值。
        # 现在请用 call(..., timeout_ms=) 显式传入；这里仅作兜底保留。
        self._run_timeout: int | None = None

    def call(self, api: str, params: dict[str, Any], route: str | None = None, dataset: str | None = None,
             timeout_ms: int | None = None) -> GatewayResult:
        if route == "promax-only":
            chain = [s for s in self.meta.sources() if s.id == "promax"]
        elif route == "official-only":
            chain = [s for s in self.meta.sources() if s.id == "official"]
        else:
            chain = self.meta.tool_route(api)
        if not chain:
            raise UpstreamError(f"no available Tushare data source for {api}")
        errors: list[Exception] = []
        pending: PendingError | None = None
        for source in chain:
            try:
                raw = self._request(source, api, params, timeout_ms)
                if dataset:
                    rows, raw_rows = apply_dictionary(source.id, dataset, raw, self.meta)
                else:
                    rows, raw_rows = raw, raw
                return GatewayResult(source=source.id, rows=rows, raw_rows=raw_rows)
            except PendingError as error:
                # 上游"数据未就绪/限流"可能只是单个通道的问题：记下后继续尝试备用通道。
                # 若所有通道都 pending，仍抛 PendingError，让调用方按"稍后整体重试"处理（语义不变）。
                pending = pending or error
                errors.append(error)
            except UpstreamError as error:
                errors.append(error)
        if pending is not None:
            raise pending
        raise UpstreamError(f"{api} failed on all sources: {errors}")

    def _request(self, source: DataSource, api: str, params: dict[str, Any],
                 timeout_ms: int | None = None) -> list[dict[str, Any]]:
        if not source.token:
            raise UpstreamError(f"{source.id} credential is unavailable")
        base = source.base_url.rstrip("/")
        if source.protocol == "official":
            body = json.dumps({"api_name": api, "token": source.token, "params": params, "fields": ""}).encode()
            request = urllib.request.Request(source.base_url.rstrip("/"), data=body, headers={"content-type": "application/json"}, method="POST")
        else:
            endpoint = _ENDPOINTS.get(api, api.replace("_", "-")) if source.id == "rds" else api
            query = urllib.parse.urlencode({k: str(v) for k, v in params.items()})
            request = urllib.request.Request(f"{base}/{endpoint}?{query}", headers={"X-API-Key": source.token})
        try:
            timeout_seconds = (timeout_ms if timeout_ms is not None
                               else (self._run_timeout or source.timeout_ms)) / 1000
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise PendingError(f"UPSTREAM_PENDING:{error.headers.get('retry-after', 30)}")
            if error.code == 202:
                try:
                    pending_payload = json.loads(error.read().decode())
                except Exception:  # noqa: BLE001
                    pending_payload = {}
                if pending_payload.get("error") == "minute_data_pending":
                    raw_retry = error.headers.get("retry-after") or 30
                    try:
                        retry = max(5, min(120, int(raw_retry)))
                    except (TypeError, ValueError):
                        retry = 30
                    raise PendingError(f"UPSTREAM_PENDING:{retry}")
            raise UpstreamError(f"{source.id} {api} HTTP {error.code}")
        except (urllib.error.URLError, TimeoutError) as error:
            if api == "stk_mins":
                raise PendingError("UPSTREAM_PENDING:10")
            raise UpstreamError(f"{source.id} {api} {getattr(error, 'reason', error)}")
        if payload.get("code") is None:
            raise UpstreamError(f"{source.id} {api}: {payload.get('error') or payload.get('msg') or 'unexpected upstream response'}")
        return decode(payload)
