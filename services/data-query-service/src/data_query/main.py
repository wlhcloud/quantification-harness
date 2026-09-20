import json
import sqlite3

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .repository import ReadOnlyRepository
from .settings import settings

def _model_config() -> dict:
    try:
        return json.loads(settings.model_config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"filters": {}, "factors": {}, "models": {}}


app = FastAPI(title="DSH Data Query Service", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:3082", "http://localhost:3082"], allow_methods=["GET"], allow_headers=["*"])
repository = ReadOnlyRepository(settings.project_root / "data", settings.busy_timeout_ms)

@app.get("/api/v1/health")
def health() -> dict[str, object]:
    return {"status": "ok", "service": "data-query-service", "version": "0.1.0", "read_only": True}

@app.get("/api/v1/datasets")
def datasets() -> list[dict[str, object]]:
    try: return repository.datasets()
    except (sqlite3.Error, FileNotFoundError) as error: raise HTTPException(503, str(error)) from error

@app.get("/api/v1/datasets/minute/{freq}/quality")
def minute_quality(freq: str) -> dict[str, object]:
    if freq not in {"1m", "5m"}: raise HTTPException(400, "freq must be 1m or 5m")
    return repository.minute_quality(freq)

@app.get("/api/v1/datasets/minute/{freq}/governance")
def minute_governance(freq: str, dates: int = Query(8, ge=3, le=20), codes: int = Query(4, ge=1, le=20)) -> dict[str, object]:
    if freq not in {"1m", "5m"}: raise HTTPException(400, "freq must be 1m or 5m")
    return repository.minute_governance(freq, dates, codes)

@app.get("/api/v1/minute-bars")
def minute_bars(code: str, start: str, end: str, freq: str = "5m", limit: int = Query(500, ge=1, le=settings.max_rows), offset: int = Query(0, ge=0)) -> list[dict[str, object]]:
    if freq not in {"1m", "5m"}: raise HTTPException(400, "freq must be 1m or 5m")
    if not code or not start or not end: raise HTTPException(400, "code, start and end are required")
    return repository.minute_bars(code.upper(), freq, start, end, limit, offset)

@app.get("/api/v1/securities")
def securities(q: str = "", limit: int = Query(20, ge=1, le=100)) -> list[dict[str, object]]:
    return repository.securities(q, limit)

@app.get("/api/v1/etf/universe")
def etf_universe(q: str = "", eligible: bool = False,
                 limit: int = Query(50, ge=1, le=500)) -> list[dict[str, object]]:
    return repository.etf_universe(q, eligible, limit)

@app.get("/api/v1/etf/bars")
def etf_bars(ts_code: str, start: str = "", end: str = "",
             limit: int = Query(200, ge=1, le=settings.max_rows),
             offset: int = Query(0, ge=0)) -> list[dict[str, object]]:
    return repository.etf_bars(ts_code.upper(), start, end, limit, offset)

@app.get("/api/v1/index/bars")
def index_bars(ts_code: str, start: str = "", end: str = "",
               limit: int = Query(200, ge=1, le=settings.max_rows),
               offset: int = Query(0, ge=0)) -> list[dict[str, object]]:
    return repository.index_bars(ts_code.upper(), start, end, limit, offset)

@app.get("/api/v1/market/overview")
def market_overview(code: str = Query("000001.SH", pattern=r"^\d{6}\.(SH|SZ)$"),
                    freq: str = Query("1m", pattern=r"^(1m|5m|15m|30m|60m)$")) -> dict[str, object]:
    try:
        return repository.market_overview(_model_config(), code, freq)
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/meta/datasets")
def meta_datasets() -> dict[str, object]:
    try:
        items = repository.meta_datasets()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error
    return {"count": len(items), "items": items}

@app.get("/api/v1/meta/data-sources")
def meta_data_sources() -> dict[str, object]:
    try:
        items = repository.meta_data_sources()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error
    return {"count": len(items), "items": items}

@app.get("/api/v1/meta/tool-routes")
def meta_tool_routes() -> dict[str, object]:
    try:
        items = repository.meta_tool_routes()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error
    return {"count": len(items), "items": items}

@app.get("/api/v1/meta/dictionary")
def meta_dictionary() -> dict[str, object]:
    try:
        return repository.meta_dictionary()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/selection/models")
def selection_models() -> dict[str, object]:
    """可选选股模型清单（供前端下拉）。

    legacy 因子选股已于 2026-09-18 退役，`config/factor-models.yaml` 只剩 `ml_walkforward`，
    所以这里现在只返回它；下面的动态追加逻辑保留，并对"配置里已登记"的情况去重
    （原实现会追加出重复的 ml_walkforward 项）。
    """
    config = _model_config()
    items = [{"id": k, "label": v["label"], "minScore": v["minScore"], "minCoverage": v["minCoverage"],
              "maxCandidates": v["maxCandidates"], "weights": v["weights"]} for k, v in config["models"].items()]
    known = {item["id"] for item in items}
    # 机器学习 walkforward 信号（动态：有信号才出现在下拉）
    if "ml_walkforward" not in known and repository.has_selection_candidates("ml_walkforward"):
        items.append({"id": "ml_walkforward", "label": "机器学习", "minScore": 0, "minCoverage": 1.0,
                      "maxCandidates": 100, "weights": {}})
    return {"items": items}

@app.get("/api/v1/selection")
def selection(model: str = "ml_walkforward", limit: int = Query(50, ge=1, le=500)) -> dict[str, object]:
    try:
        return repository.selection(_model_config(), model, limit)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/backtest/latest")
def backtest_latest() -> dict[str, object]:
    try:
        return repository.backtest_latest()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/backtest/periods")
def backtest_periods(runId: str = "") -> dict[str, object]:
    try:
        return repository.backtest_periods(runId or None)
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/backtest/short/latest")
def backtest_short_latest() -> dict[str, object]:
    try:
        return repository.short_latest()
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/backtest/short/trades")
def backtest_short_trades(run_id: str = "", limit: int = Query(100, ge=1, le=500)) -> dict[str, object]:
    try:
        return repository.short_trades(run_id or None, limit)
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error

@app.get("/api/v1/stock/technical")
def stock_technical(codes: str = Query("", description="逗号分隔的股票代码列表，如 000001.SZ,600000.SH")) -> dict[str, object]:
    """批量获取股票技术指标：MA5/MA10/MA20、金叉/死叉、趋势、V5买卖信号"""
    if not codes:
        raise HTTPException(400, "codes is required (comma-separated stock codes)")
    code_list = [c.strip() for c in codes.split(",") if c.strip()]
    if len(code_list) > 100:
        raise HTTPException(400, "too many codes (max 100)")
    try:
        return repository.stock_technical(code_list)
    except (sqlite3.Error, FileNotFoundError) as error:
        raise HTTPException(503, str(error)) from error
