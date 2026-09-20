import json
import logging
import re
import secrets
import threading

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from . import __version__
from .audit import AuditStore
from .hub import SyncHub, websocket_loop
from .daily import DailySync
from .etf import EtfSync
from .finance import FinanceSync
from .meta import MetaStore, ensure_meta_schema
from .minute import MinuteSync
from .market_scheduler import MarketScheduler
from .settings import settings
from .settings_store import SettingsStore
from .tool_sync import CATALOG, ToolSync
from .upstream import TushareClient
from .util import now_iso

logger = logging.getLogger(__name__)
CODE_RE = re.compile(r"^\d{6}\.(SZ|SH|BJ)$")

# 启动即把"鉴权/密钥到底加载到没有"写进日志：key 为空时鉴权会**静默失效**，
# 光看服务起来了发现不了（历史上计划任务用内联命令启动就踩过这个坑）。
print(f"[startup] quant-sync 自检 authEnabled={bool(settings.api_key)} "
      f"authSource={settings.api_key_source or '(空)'} credentialKeyLoaded={bool(settings.credential_key)} "
      f"root={settings.project_root}", flush=True)

# 启动自举：全新环境/临时目录下 meta.db 可能还没有表（契约 DDL 幂等，对既有库无副作用）
if not ensure_meta_schema(settings.meta_path, settings.meta_contract_path):
    logger.warning("未找到 meta.db 契约 %s，跳过元数据库自举", settings.meta_contract_path)
meta = MetaStore(settings.meta_path, credential_key=settings.credential_key)
client = TushareClient(meta, settings.request_timeout_ms)
audit = AuditStore(settings.sync_path)
minute = MinuteSync(settings.minute_path, client, meta, settings.concurrency,
                    settings.one_minute_segment_days, settings.five_minute_segment_days)
daily = DailySync(settings.market_path, client, meta, audit=audit)
etf = EtfSync(settings.market_path, client, meta, audit=audit)
finance = FinanceSync(settings.finance_path, client, meta)
settings_store = SettingsStore(settings.meta_path, client, settings.credential_key)
settings_store.ensure_market_snapshot_metadata()

# ---- sync task configuration (persistent, transparent) ----
CONFIG_PATH = settings.data_dir / "sync-config.json"

# sync-config.json 允许出现的键（也是 PUT /api/v1/sync/config 会持久化的键）。
# None = 标量键；集合 = 该节允许的子键。用来发现"没人读的死配置"。
CONFIG_SCHEMA: dict[str, set[str] | None] = {
    "minute": {"concurrency", "oneMinuteSegmentDays", "fiveMinuteSegmentDays"},
    "marketScheduler": {"enabled", "intervalSeconds", "catchupEnabled"},
    "requestTimeoutMs": None,
}


def _unknown_config_keys(config: dict) -> list[str]:
    """返回不在 CONFIG_SCHEMA 里的键（不删除，只用于告警/测试断言）。"""
    unknown = [key for key in config if key not in CONFIG_SCHEMA]
    for key, allowed in CONFIG_SCHEMA.items():
        if allowed and isinstance(config.get(key), dict):
            unknown += [f"{key}.{sub}" for sub in config[key] if sub not in allowed]
    return sorted(unknown)


def _default_config() -> dict:
    return {"minute": {"concurrency": settings.concurrency,
                       "oneMinuteSegmentDays": settings.one_minute_segment_days,
                       "fiveMinuteSegmentDays": settings.five_minute_segment_days},
            "marketScheduler": {"enabled": True, "intervalSeconds": 60, "catchupEnabled": True},
            "requestTimeoutMs": settings.request_timeout_ms}


def _deep_merge(base: dict, override: dict) -> dict:
    """嵌套字典按"键"覆盖，而不是整块替换。

    背景（配置面收敛）：`sync-config.json`（运行时可改）与环境变量（`QUANT_SYNC_*`，启动默认值）
    描述的是同一批事实（minute.concurrency / segmentDays、requestTimeoutMs、marketScheduler）。
    原先用 `defaults.update(data)` 做浅合并：只要 JSON 里出现 `{"minute": {"concurrency": 3}}`，
    环境变量给的 `oneMinuteSegmentDays` / `fiveMinuteSegmentDays` 就被整块丢掉（静默回落到
    MinuteSync 构造默认值）。深合并后"只覆盖真正改过的键"，两边不再互相暗算。
    """
    merged = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_config() -> dict:
    defaults = _default_config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("cannot load sync config %s: %s; using defaults", CONFIG_PATH, error)
        return defaults
    unknown = _unknown_config_keys(data)
    if unknown:
        # 不丢弃（保持行为不变），但要说出来：这些键没有任何代码读取
        logger.warning("sync config %s 含无人读取的键：%s", CONFIG_PATH, ", ".join(unknown))
    return _deep_merge(defaults, data)


def _save_config(config: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as error:
        logger.error("cannot persist sync config %s: %s", CONFIG_PATH, error)
        raise


def _apply_minute_config(config: dict) -> None:
    m = config.get("minute") or {}
    if m.get("concurrency"):
        minute.concurrency = max(1, min(25, int(m["concurrency"])))
    if m.get("oneMinuteSegmentDays"):
        minute.segment_days["1m"] = max(1, min(45, int(m["oneMinuteSegmentDays"])))
    if m.get("fiveMinuteSegmentDays"):
        minute.segment_days["5m"] = max(1, min(180, int(m["fiveMinuteSegmentDays"])))


sync_config = _load_config()
_apply_minute_config(sync_config)

# ---- websocket progress hub ----
hub = SyncHub()
tool_sync = ToolSync(client, settings.market_path, settings.finance_path, meta, audit,
                     on_notify=lambda msg: hub.publish(msg))
minute.on_progress = lambda: hub.publish({"type": "minute", "state": minute.state})
daily.on_progress = lambda: hub.publish({"type": "daily", "state": daily.history})
finance.on_progress = lambda: hub.publish({"type": "finance", "state": finance.bulk})

app = FastAPI(title="DSH Quant Sync Service", version=__version__)
market_scheduler = MarketScheduler(tool_sync, settings.market_path, lambda: sync_config)
if settings.scheduler_enabled:
    market_scheduler.start()


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if settings.api_key and request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(supplied, settings.api_key):
            return JSONResponse(status_code=401, content={"detail": "invalid API key"})
    return await call_next(request)


@app.websocket("/api/v1/ws")
async def ws_sync(websocket: WebSocket):
    await websocket_loop(websocket, hub, {"minute": minute.state, "daily": daily.history, "finance": finance.bulk})


@app.get("/api/v1/sync/config")
def sync_config_get():
    return {**sync_config,
            "sources": [{"id": s.id, "timeoutMs": s.timeout_ms} for s in meta.sources()],
            "minuteRun": {"concurrency": minute.concurrency, "oneMinuteSegmentDays": minute.segment_days["1m"],
                          "fiveMinuteSegmentDays": minute.segment_days["5m"]}}


@app.put("/api/v1/sync/config")
def sync_config_put(body: dict):
    if body.get("minute"):
        sync_config.setdefault("minute", {}).update(body["minute"])
        _apply_minute_config(sync_config)
    if body.get("requestTimeoutMs"):
        sync_config["requestTimeoutMs"] = max(1000, int(body["requestTimeoutMs"]))
    if body.get("marketScheduler"):
        scheduler = body["marketScheduler"]
        target = sync_config.setdefault("marketScheduler", {})
        if "enabled" in scheduler:
            target["enabled"] = bool(scheduler["enabled"])
        if "catchupEnabled" in scheduler:
            target["catchupEnabled"] = bool(scheduler["catchupEnabled"])
        if "intervalSeconds" in scheduler:
            target["intervalSeconds"] = max(30, min(300, int(scheduler["intervalSeconds"])))
    _save_config(sync_config)
    return sync_config_get()


@app.get("/api/v1/sync/tools")
def sync_tools():
    items = tool_sync.status_all()
    ms = minute.state
    for item in items:
        # Keep the newest truthful run. A later single-stock audit must not be mixed
        # with timestamps/progress from an older whole-market minute run.
        if item["toolId"] == "stk_mins" and (ms.get("startedAt") or "") >= (item.get("startedAt") or ""):
            item.update({"status": ms.get("status", "idle"), "total": ms.get("total", 0), "done": ms.get("completed", 0),
                         "runId": ms.get("runId"), "received": 0, "written": 0, "source": None, "qualityStatus": None,
                         "current": ms.get("current"), "startDate": ms.get("startDate"), "endDate": ms.get("endDate"),
                         "freq": ms.get("freq"), "startedAt": ms.get("startedAt"), "finishedAt": ms.get("finishedAt"),
                         "error": ms.get("error")})
    return {"count": len(items), "items": items}


@app.post("/api/v1/sync/tool/{tool_id}")
def sync_tool(tool_id: str, start_date: str = "", end_date: str = "", code: str = "", period: str = "",
              freq: str = "5m", limit: int = 0, concurrency: int | None = None,
              segment_days: int | None = None, request_timeout_ms: int | None = None):
    ids = {t["id"] for t in CATALOG}
    if tool_id not in ids:
        raise HTTPException(404, f"unknown tool {tool_id}; available: {','.join(sorted(ids))}")
    if tool_id == "stk_mins":
        raise HTTPException(400, "stk_mins uses the minute engine: POST /api/v1/sync/minute/all with freq")
    if tool_id in ("fina_indicator", "income", "balancesheet", "cashflow") and not code:
        raise HTTPException(400, tool_id + ": 上游仅支持按单只股票查询财务数据，请提供 code 参数")
    if tool_id in ("fina_indicator", "income", "balancesheet", "cashflow") and not CODE_RE.match(code):
        raise HTTPException(400, tool_id + ": code 必须为 000001.SZ 格式")
    params = {"start_date": start_date, "end_date": end_date, "code": code, "period": period,
              "freq": freq, "trade_date": start_date or end_date}
    try:
        tool_sync.start(tool_id, params)
    except RuntimeError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "toolId": tool_id, "status": tool_sync.states[tool_id]["status"]}


@app.get("/api/v1/sync/market-scheduler")
def market_scheduler_status():
    return market_scheduler.snapshot()


def _source_json(source):
    return {"id": source.id, "label": source.label, "protocol": source.protocol,
            "baseUrl": source.base_url, "token": "", "hasToken": bool(source.token), "priority": source.priority,
            "timeoutMs": source.timeout_ms}


def _background(fn):
    threading.Thread(target=fn, daemon=True).start()


# serialize task submission: reserve status synchronously to avoid double-start races
_start_locks = {"minute": threading.Lock(), "daily": threading.Lock(), "finance": threading.Lock(),
                "etf": threading.Lock(), "etf_index": threading.Lock()}


@app.get("/api/v1/health")
def health():
    try:
        sources = meta.sources()
        meta_online = True
    except Exception:  # noqa: BLE001
        sources, meta_online = [], False
    try:
        historical_segment_errors = minute.db.execute("SELECT COUNT(*) c FROM minute_sync_segments WHERE status='error'").fetchone()["c"]
        unresolved_quality_cells = minute.db.execute("SELECT COUNT(*) c FROM minute_sync_days WHERE status IN ('incomplete','source_missing')").fetchone()["c"]
        current_run_id = minute.state.get("runId")
        current_failed_items = minute.db.execute(
            "SELECT COUNT(*) c FROM minute_sync_run_items WHERE run_id=? AND status='error'", (current_run_id,)
        ).fetchone()["c"] if current_run_id else 0
    except Exception:  # noqa: BLE001
        historical_segment_errors, unresolved_quality_cells, current_failed_items = 0, 0, 0
    return {"status": "ok", "service": "quant-sync", "version": __version__,
            "databasePath": str(settings.minute_path), "failedSegments": current_failed_items,
            "currentRunId": minute.state.get("runId"), "currentRunStatus": minute.state.get("status"),
            "unresolvedQualityCells": unresolved_quality_cells,
            "historicalSegmentErrors": historical_segment_errors,
            # 编排侧断言用：未加载 .quant.env 时 api_key 为空、鉴权会静默失效；
            # authSource 说明 key 来自哪个环境变量（QUANT_API_KEY 为收敛后的权威变量）
            "authEnabled": bool(settings.api_key),
            "authSource": settings.api_key_source,
            # 前端能力探测用：false 时 /sync/minute* 一律 410，页面据此禁用分钟同步入口，
            # 而不是等用户点提交才弹错误。
            "minuteSyncEnabled": bool(settings.minute_sync_enabled),
            "meta": {"online": meta_online, "path": str(settings.meta_path), "sources": len(sources)}}


@app.get("/api/v1/sources")
def sources():
    try:
        items = [_source_json(s) for s in meta.sources()]
    except Exception as error:  # noqa: BLE001
        raise HTTPException(503, str(error)) from error
    return {"count": len(items), "items": items}


@app.get("/api/v1/routes")
def routes():
    try:
        items = [{"toolId": api, "primarySourceId": ids[0], "fallbackSourceId": ids[1]}
                 for api, ids in meta._load()[1].items()]
    except Exception as error:  # noqa: BLE001
        raise HTTPException(503, str(error)) from error
    return {"count": len(items), "items": sorted(items, key=lambda x: x["toolId"])}


# ---- daily ----
@app.post("/api/v1/sync/daily")
def sync_daily(trade_date: str):
    run_id = audit.start("daily.single", {"trade_date": trade_date}, tool_id="daily")
    try:
        counts = daily.sync(trade_date)
        bars = daily.db.execute("SELECT COUNT(*) FROM daily_bars WHERE trade_date=?", (trade_date,)).fetchone()[0]
        adjustments = daily.db.execute("SELECT COUNT(*) FROM adjustment_factors WHERE trade_date=?", (trade_date,)).fetchone()[0]
        quality = "passed" if bars and adjustments >= bars * .95 else "gaps"
        audit.update(run_id, status="complete" if quality == "passed" else "complete_with_gaps", total=1,
                     completed=1 if quality == "passed" else 0, failed=0 if quality == "passed" else 1,
                     received=bars + adjustments, written=bars + adjustments, source="tushare", qualityStatus=quality,
                     qualityDetails={"bars": bars, "adjustments": adjustments}, finishedAt=now_iso())
        audit.log(run_id, level="warning" if quality != "passed" else "info", trade_date=trade_date,
                  request_api="daily.bundle", request_params={"trade_date": trade_date},
                  status="gap" if quality != "passed" else "passed", received=bars + adjustments,
                  written=bars + adjustments, source="tushare",
                  error=None if quality == "passed" else f"复权因子覆盖不足: {adjustments}/{bars}",
                  details={"bars": bars, "adjustments": adjustments})
        return {"status": "ok", "tradeDate": trade_date, "counts": counts, "runId": run_id, "quality": quality}
    except Exception as error:  # noqa: BLE001
        audit.update(run_id, status="error", failed=1, error=str(error), qualityStatus="failed",
                     finishedAt=now_iso())
        raise HTTPException(502, str(error)) from error


@app.post("/api/v1/sync/daily/history", status_code=202)
def sync_daily_history(start_date: str, end_date: str, days: int = 260,
                       concurrency: int | None = None, request_timeout_ms: int | None = None):
    if len(start_date) != 8 or len(end_date) != 8 or start_date > end_date or days < 1:
        raise HTTPException(400, "start_date/end_date must be YYYYMMDD, start<=end and days>=1")
    with _start_locks["daily"]:
        if daily.history["status"] in ("running", "starting"):
            raise HTTPException(409, daily.history)
        daily.history["status"] = "starting"

    def run():
        try:
            daily.timeout_ms = max(1000, request_timeout_ms) if request_timeout_ms else None
            daily.sync_history(start_date, end_date, days, concurrency=concurrency or 4)
        finally:
            daily.timeout_ms = None

    _background(run)
    return daily.history


@app.get("/api/v1/sync/daily/history/status")
def sync_daily_history_status():
    return daily.history


# ---- etf (additive; stock pipeline untouched) ----
@app.get("/api/v1/sync/etf/status")
def sync_etf_status():
    return {"universe": etf.universe, "daily": etf.daily, "index": etf.index,
            "counts": {"universe": etf._count("etf_metadata"),
                       "eligible": etf.db.execute("SELECT COUNT(*) c FROM etf_metadata WHERE eligible=1").fetchone()["c"],
                       "etfBars": etf._count("etf_daily_bars"),
                       "indexBars": etf._count("index_daily_bars")}}


@app.post("/api/v1/sync/etf/universe")
def sync_etf_universe():
    return etf.sync_universe()


@app.post("/api/v1/sync/etf/daily")
def sync_etf_daily(trade_date: str):
    if len(trade_date) != 8:
        raise HTTPException(400, "trade_date must be YYYYMMDD")
    try:
        return etf.sync_daily(trade_date)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, str(error)) from error


@app.post("/api/v1/sync/etf/history", status_code=202)
def sync_etf_history(start_date: str, end_date: str, days: int = 260,
                     concurrency: int | None = None, request_timeout_ms: int | None = None):
    if len(start_date) != 8 or len(end_date) != 8 or start_date > end_date or days < 1:
        raise HTTPException(400, "start_date/end_date must be YYYYMMDD, start<=end and days>=1")
    with _start_locks["etf"]:
        if etf.daily["status"] in ("running", "starting"):
            raise HTTPException(409, etf.daily)
        etf.daily["status"] = "starting"

    def run():
        # 超时按调用传参：ETF 的 daily/index 两个入口共用同一个 etf 对象，写成对象属性会被并发覆盖
        etf.sync_daily_history(start_date, end_date, days, concurrency=concurrency or 4,
                               timeout_ms=max(1000, request_timeout_ms) if request_timeout_ms else None)

    _background(run)
    return etf.daily


@app.get("/api/v1/sync/etf/history/status")
def sync_etf_history_status():
    return etf.daily


@app.post("/api/v1/sync/etf/index-daily")
def sync_etf_index_daily(trade_date: str):
    if len(trade_date) != 8:
        raise HTTPException(400, "trade_date must be YYYYMMDD")
    try:
        return etf.sync_index_daily(trade_date)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(502, str(error)) from error


@app.post("/api/v1/sync/etf/index-history", status_code=202)
def sync_etf_index_history(start_date: str, end_date: str, days: int = 260,
                           concurrency: int | None = None, request_timeout_ms: int | None = None):
    if len(start_date) != 8 or len(end_date) != 8 or start_date > end_date or days < 1:
        raise HTTPException(400, "start_date/end_date must be YYYYMMDD, start<=end and days>=1")
    with _start_locks["etf_index"]:
        if etf.index["status"] in ("running", "starting"):
            raise HTTPException(409, etf.index)
        etf.index["status"] = "starting"

    def run():
        etf.sync_index_history(start_date, end_date, days, concurrency=concurrency or 4,
                               timeout_ms=max(1000, request_timeout_ms) if request_timeout_ms else None)

    _background(run)
    return etf.index


@app.get("/api/v1/sync/etf/index-history/status")
def sync_etf_index_history_status():
    return etf.index


# ---- finance ----
@app.post("/api/v1/sync/finance")
def sync_finance(code: str, start_date: str):
    run_id = audit.start("finance.single", {"code": code, "start_date": start_date}, tool_id="fina_indicator")
    try:
        result = finance.sync(code, start_date)
        sources = ",".join(sorted(set(result.get("sources", {}).values())))
        audit.update(run_id, status="complete", total=1, completed=1, source=sources,
                     qualityStatus="passed", qualityDetails={"catalogCounts": result.get("counts")}, finishedAt=now_iso())
        audit.log(run_id, code=code, request_api="finance.bundle", request_params={"code": code, "start_date": start_date},
                  status="passed", source=sources, details={"catalogCounts": result.get("counts")})
        return {"status": "ok", "code": code, "result": result, "runId": run_id}
    except Exception as error:  # noqa: BLE001
        audit.update(run_id, status="error", failed=1, error=str(error), qualityStatus="failed",
                     finishedAt=now_iso())
        raise HTTPException(502, str(error)) from error


@app.post("/api/v1/sync/finance/market", status_code=202)
def sync_finance_market(quarters: int = 2):
    with _start_locks["finance"]:
        if finance.bulk["status"] in ("running", "starting"):
            raise HTTPException(409, finance.bulk)
        finance.bulk["status"] = "starting"
    quarters = max(1, min(8, quarters))
    _background(lambda: finance.sync_incremental(quarters))
    return finance.bulk


@app.get("/api/v1/sync/finance/market/status")
def sync_finance_market_status():
    return finance.bulk


# ---- minute（股票分钟线已停用：见 settings.minute_sync_enabled）----
_MINUTE_DISABLED_DETAIL = (
    "股票分钟线同步已停用（QUANT_SYNC_MINUTE_ENABLED=0）：本地 minute_bars 已清零，"
    "重新同步会产生约 21GB / 1.3 亿行数据。确需恢复请设 QUANT_SYNC_MINUTE_ENABLED=1 后重启服务。"
)


def _require_minute_enabled() -> None:
    if not settings.minute_sync_enabled:
        raise HTTPException(410, _MINUTE_DISABLED_DETAIL)


@app.post("/api/v1/sync/minute")
def sync_minute(code: str, freq: str = "5m", trade_date: str = "", start_date: str = "", end_date: str = ""):
    _require_minute_enabled()
    if freq not in ("1m", "5m"):
        raise HTTPException(400, "freq must be 1m or 5m")
    try:
        start, end = minute.resolve_dates(start_date or trade_date, end_date)
        run_id = audit.start("minute.single", {"code": code, "start_date": start, "end_date": end, "freq": freq}, tool_id="stk_mins")
        result = minute.sync_single(code, start, end, freq)
        skipped = int(result.get("skippedSegments") or 0)
        audit.update(run_id, status="complete", total=1, completed=1, skipped=skipped,
                     received=int(result.get("received") or 0), written=int(result.get("accepted") or 0),
                     source=result.get("source"), qualityStatus="cached" if skipped else "passed",
                     qualityDetails=result, finishedAt=now_iso())
        audit.log(run_id, code=code, request_api="stk_mins", request_params={"code": code, "start_date": start,
                  "end_date": end, "freq": freq}, status="cached" if skipped else "passed",
                  received=int(result.get("received") or 0), written=int(result.get("accepted") or 0),
                  source=result.get("source"), details={"skippedSegments": skipped})
        return {"status": "ok", "result": result, "runId": run_id}
    except Exception as error:  # noqa: BLE001
        if 'run_id' in locals():
            audit.update(run_id, status="error", failed=1, error=str(error), qualityStatus="failed",
                         finishedAt=now_iso())
        raise HTTPException(502, str(error)) from error


@app.post("/api/v1/sync/minute/all", status_code=202)
def sync_minute_all(freq: str = "5m", start_date: str = "", end_date: str = "", limit: int = 0,
                    concurrency: int | None = None, segment_days: int | None = None,
                    request_timeout_ms: int | None = None):
    _require_minute_enabled()
    if freq not in ("1m", "5m"):
        raise HTTPException(400, "freq must be 1m or 5m")
    try:
        start, end = minute.resolve_dates(start_date, end_date)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(400, str(error)) from error
    limit = max(0, min(10000, limit))
    saved = (minute.concurrency, minute.segment_days["1m"], minute.segment_days["5m"])
    if concurrency:
        minute.concurrency = max(1, min(25, int(concurrency)))
    if segment_days:
        cap = 180 if freq == "5m" else 45
        minute.segment_days[freq] = max(1, min(cap, int(segment_days)))
    with _start_locks["minute"]:
        if minute.state["status"] in ("running", "starting"):
            minute.concurrency, minute.segment_days["1m"], minute.segment_days["5m"] = saved
            raise HTTPException(409, minute.state)
        minute.state["status"] = "starting"

    def run():
        try:
            minute.timeout_ms = max(1000, request_timeout_ms) if request_timeout_ms else None
            minute.sync_all(start, end, freq, limit)
        finally:
            minute.concurrency, minute.segment_days["1m"], minute.segment_days["5m"] = saved
            minute.timeout_ms = None

    _background(run)
    return {**minute.state, "startDate": start, "endDate": end, "freq": freq}


@app.post("/api/v1/sync/minute/all/retry", status_code=202)
def sync_minute_all_retry(run_id: str = ""):
    _require_minute_enabled()
    # 与 /minute/all 共用同一把 kind 锁：原实现"检查状态→置 starting"在锁外，
    # 与新建任务或另一个 retry 并发时会双启动同一批分钟同步。
    with _start_locks["minute"]:
        if minute.state["status"] in ("running", "starting"):
            raise HTTPException(409, minute.state)
        if run_id:
            row = minute.db.execute("SELECT run_id rid,start_date sd,end_date ed,freq FROM minute_sync_runs WHERE run_id=?", (run_id,)).fetchone()
        else:
            row = minute.db.execute("SELECT run_id rid,start_date sd,end_date ed,freq FROM minute_sync_runs WHERE failed>0 ORDER BY started_at DESC LIMIT 1").fetchone()
        if not row:
            raise HTTPException(404, "no failed minute sync run found")
        failed_items = minute.db.execute("SELECT COUNT(*) c FROM minute_sync_run_items WHERE run_id=? AND status='error'", (row["rid"],)).fetchone()["c"]
        if failed_items == 0:
            raise HTTPException(400, "该任务没有可重试的失败项")
        minute.state["status"] = "starting"
    _background(lambda: minute.sync_all(row["sd"], row["ed"], row["freq"], 0, row["rid"]))
    return {**minute.state, "retryOf": row["rid"]}


@app.get("/api/v1/sync/minute/all/status")
def sync_minute_all_status():
    return minute.state


@app.get("/api/v1/sync/minute/all/failures")
def sync_minute_all_failures(run_id: str = "", limit: int = 100):
    return minute.failures(run_id or None, max(1, min(1000, limit)))


@app.get("/api/v1/sync/minute/all/details")
def sync_minute_all_details(run_id: str = "", limit: int = 100):
    return minute.details(run_id or None, max(1, min(500, limit)))


# ---- records ----
@app.get("/api/v1/records")
def records(limit: int = 100):
    capped = max(1, min(500, limit))
    items = audit.list(capped) + minute.runs(capped)
    items.sort(key=lambda item: item.get("startedAt") or "", reverse=True)
    items = items[:capped]
    return {"count": len(items), "items": items}


@app.get("/api/v1/records/{run_id}/logs")
def record_logs(run_id: str, limit: int = 500):
    capped = max(1, min(2000, limit))
    items = audit.logs(run_id, capped)
    run = audit.get(run_id)
    if run and run["kind"] == "daily.history":
        gaps = (run.get("qualityDetails") or {}).get("gapDates", [])
        for date in gaps:
            row = daily.db.execute("SELECT * FROM research_sync_quality WHERE trade_date=?", (date,)).fetchone()
            if row and not any(item.get("tradeDate") == date and item.get("requestApi") == "quality-check" for item in items):
                items.append({"id": f"quality-{run_id}-{date}", "timestamp": row["updated_at"], "level": "warning",
                              "code": None, "tradeDate": date, "requestApi": "quality-check",
                              "requestParams": {"trade_date": date}, "status": "gap", "attempt": 1,
                              "received": row["bars_count"] + row["basic_count"],
                              "written": row["status_count"] + row["adjustment_count"], "source": "local-validation",
                              "error": row["error"], "details": {"bars": row["bars_count"], "basic": row["basic_count"],
                              "status": row["status_count"], "adjustments": row["adjustment_count"]}})
    if run_id.startswith("minute-") and not run_id.startswith("minute-single-"):
        run_row = minute.db.execute("SELECT start_date,end_date,freq FROM minute_sync_runs WHERE run_id=?", (run_id,)).fetchone()
        if run_row:
            segments = minute.db.execute(
                "SELECT s.* FROM minute_sync_segments s JOIN minute_sync_run_items i ON i.code=s.code "
                "WHERE i.run_id=? AND s.freq=? AND s.end_date>=? AND s.start_date<=? AND (s.status='error' OR i.status='error') "
                "ORDER BY CASE s.status WHEN 'error' THEN 0 ELSE 1 END,s.updated_at DESC LIMIT ?",
                (run_id, run_row["freq"], run_row["start_date"], run_row["end_date"], capped)).fetchall()
            for row in segments:
                items.append({"id": f"minute-segment-{run_id}-{row['code']}-{row['start_date']}-{row['end_date']}",
                              "timestamp": row["updated_at"], "level": "error" if row["status"] == "error" else "info",
                              "code": row["code"], "tradeDate": None, "requestApi": "stk_mins",
                              "requestParams": {"ts_code": row["code"], "start_date": row["start_date"],
                                                "end_date": row["end_date"], "freq": row["freq"]},
                              "status": row["status"], "attempt": row["attempts"], "received": row["received"],
                              "written": row["accepted"], "source": row["source"], "error": row["last_error"],
                              "details": {"segment": True}})
        rows = minute.db.execute("SELECT code,status,attempts,error,updated_at FROM minute_sync_run_items WHERE run_id=? "
                                 "ORDER BY CASE status WHEN 'error' THEN 0 WHEN 'running' THEN 1 ELSE 2 END,code LIMIT ?",
                                 (run_id, capped)).fetchall()
        for row in rows:
            items.append({"id": f"minute-item-{run_id}-{row['code']}", "timestamp": row["updated_at"],
                          "level": "error" if row["status"] == "error" else "info", "code": row["code"],
                          "tradeDate": None, "requestApi": "stk_mins", "requestParams": {}, "status": row["status"],
                          "attempt": row["attempts"], "received": 0, "written": 0, "source": None,
                          "error": row["error"], "details": None})
    items.sort(key=lambda item: item.get("timestamp") or "", reverse=True)
    items.sort(key=lambda item: {"error": 0, "warning": 1, "info": 2}.get(item.get("level"), 3))
    return {"runId": run_id, "count": len(items[:capped]), "items": items[:capped]}


# ---- settings (meta.db CRUD) ----
@app.put("/api/v1/settings/data-sources")
def settings_sources_put(body: dict):
    items = settings_store.replace_sources(body.get("items", []))
    return {"ok": True, "items": items}


@app.post("/api/v1/settings/data-sources/item")
def settings_source_post(body: dict):
    return {"ok": True, "item": settings_store.upsert_source(body)}


@app.delete("/api/v1/settings/data-sources/item")
def settings_source_delete(id: str):
    settings_store.delete_source(id)
    return {"ok": True}


@app.post("/api/v1/settings/data-sources/test")
def settings_source_test(body: dict):
    try:
        return settings_store.test_source(body.get("id", ""))
    except ValueError as error:
        # 原实现让 ValueError 冒到 FastAPI → 500；未知 id 应当是 404
        raise HTTPException(404, str(error)) from error


@app.put("/api/v1/settings/tool-routes")
def settings_routes_put(body: dict):
    items = settings_store.replace_routes(body.get("items", []))
    return {"ok": True, "items": items}


@app.put("/api/v1/settings/tool-routes/item")
def settings_route_put(body: dict):
    return {"ok": True, "item": settings_store.upsert_route(body)}


@app.post("/api/v1/settings/dictionary/item")
def settings_dict_post(kind: str, body: dict):
    settings_store.save_dictionary(kind, body)
    return {"ok": True}


@app.delete("/api/v1/settings/dictionary/item")
def settings_dict_delete(kind: str, request: Request):
    key = dict(request.query_params)
    key.pop("kind", None)
    settings_store.delete_dictionary(kind, key)
    return {"ok": True}


@app.post("/api/v1/settings/base-dictionary/item")
def settings_base_post(kind: str, body: dict):
    settings_store.save_base(kind, body)
    return {"ok": True}


@app.delete("/api/v1/settings/base-dictionary/item")
def settings_base_delete(kind: str, request: Request):
    key = dict(request.query_params)
    key.pop("kind", None)
    settings_store.delete_base(kind, key)
    return {"ok": True}
