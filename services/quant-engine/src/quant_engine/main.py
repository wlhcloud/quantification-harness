from contextlib import asynccontextmanager
import secrets
from pathlib import Path

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .api import create_router
from .backtest import run_monthly
from .backtest.short import run_short
from .common import load_config, load_yaml
from .data_gate import check_backtest_readiness
from .jobs import JobManager
from .models import JobType, utc_now
from .pipeline import build_features, run_inference, train_model
from .repository import JobRepository
from .settings import settings
from .etf_quant import runner as etf_runner
from .etf_quant import sim as etf_sim
from .stock import sim as stock_sim
from .stock import regime_analysis as stock_regime
from .stock_ml import build_factors as stock_ml_build_factors, run_walkforward as stock_ml_run_walkforward, load_config as stock_ml_load_config
from .stock_ml import latest_walkforward as stock_ml_latest_walkforward, latest_candidates as stock_ml_latest_candidates, list_walkforward as stock_ml_list_walkforward, publish_walkforward as stock_ml_publish_walkforward, delete_walkforward as stock_ml_delete_walkforward, update_walkforward_notes as stock_ml_update_walkforward_notes
from .stock_ml import refresh_published_candidates as stock_ml_refresh_published_candidates
from .stock_ml import get_model_config as stock_ml_get_model_config, get_feature_importance as stock_ml_get_feature_importance
from .stock_ml import backtest_with_model as stock_ml_backtest_with_model, find_model_path_by_run_id as stock_ml_find_model_path
from .stock_ml import predict_with_model as stock_ml_predict_with_model
from .stock_ml import audit_label_price_basis as stock_ml_audit_label_price_basis
from .stock_ml import git_info as stock_ml_git_info
from .stock_ml.aux_factors import (
    aux_factor_freshness,
    build_industry_factors,
    build_money_flow_factors,
)
from .technical_timing import summarize_v5_backtest

repository = JobRepository(settings.metadata_path)
repository.recover_interrupted()
manager = JobManager(repository, settings.max_workers)


def _backtest_handler(parameters, progress, cancelled):
    if parameters.get("kind") == "short" and not settings.short_backtest_enabled:
        # 双保险：准入已拦截，这里再挡一次，避免绕过 readiness 直接提交产生的无效 job
        raise ValueError("短线回测已停用（股票 5 分钟线已清除；QUANT_ENGINE_SHORT_BACKTEST_ENABLED=0）")
    if cancelled():
        return {"cancelled": True}
    progress(5)
    config = load_config(settings.model_config_path)
    if parameters.get("kind") == "short":
        result = run_short(settings.backtest_path, settings.market_path, settings.minute_path, parameters,
                           cancelled)
    else:
        defaults = load_yaml(settings.backtest_config_path)
        top_n = max(1, min(500, int(parameters.get("topN", defaults.get("topN", 30)))))
        commission = max(0.0, float(parameters.get("commissionRate", defaults.get("commissionRate", 0.00008))))
        stamp = max(0.0, float(parameters.get("stampDutyRate", defaults.get("stampDutyRate", 0.0005))))
        slippage = max(0.0, float(parameters.get("slippageRate", defaults.get("slippageRate", 0.001))))
        risk_free = max(-0.99, float(parameters.get("annualRiskFreeRate", defaults.get("annualRiskFreeRate", 0.0))))
        result = run_monthly(settings.backtest_path, settings.market_path, settings.finance_path, config,
                             top_n, commission, stamp, slippage, risk_free, progress, cancelled)
    progress(100)
    return result


def _features_handler(parameters, progress, cancelled):
    return build_features(settings.market_path, settings.feature_dir, parameters, progress, cancelled)


def _training_handler(parameters, progress, cancelled):
    return train_model(settings.feature_dir, settings.artifact_dir, parameters, progress, cancelled)


def _inference_handler(parameters, progress, cancelled):
    return run_inference(settings.feature_dir, settings.artifact_dir, parameters, progress, cancelled)


def _etf_config():
    return etf_runner.load_config(settings.etf_config_path)


def _etf_factors_handler(parameters, progress, cancelled):
    return etf_runner.run_factors(settings.market_path, settings.etf_quant_db_path, _etf_config(), progress, cancelled)


def _etf_research_handler(parameters, progress, cancelled):
    return etf_runner.run_research(settings.etf_quant_db_path, _etf_config(), progress)


def _etf_train_handler(parameters, progress, cancelled):
    return etf_runner.run_train(settings.etf_quant_db_path, settings.etf_artifact_dir, _etf_config(), progress, cancelled)


def _etf_backtest_handler(parameters, progress, cancelled):
    return etf_runner.run_backtest(settings.etf_quant_db_path, settings.market_path, _etf_config(), progress, parameters)


def _etf_walkforward_handler(parameters, progress, cancelled):
    return etf_runner.run_walkforward(settings.etf_quant_db_path, settings.market_path,
                                      settings.etf_artifact_dir, _etf_config(), progress, cancelled, parameters)


def _stock_ml_config():
    return stock_ml_load_config(settings.stock_ml_config_path)


def _stock_ml_factors_handler(parameters, progress, cancelled):
    return stock_ml_build_factors(settings.market_path, settings.factors_path, settings.finance_path,
                                  parameters, progress, cancelled)


def _stock_walkforward_handler(parameters, progress, cancelled):
    # config_path 只用于配置可观测性：把 YAML 文件哈希与实际生效配置哈希一起入库，
    # 两者不同即说明 YAML 被代码/参数覆盖（详见 stock_ml.config_integrity）。
    return stock_ml_run_walkforward(settings.factors_path, settings.market_path,
                                    settings.stock_ml_artifact_dir, _stock_ml_config(),
                                    progress, cancelled, parameters,
                                    config_path=settings.stock_ml_config_path)


manager.register(JobType.BACKTEST, _backtest_handler)
manager.register(JobType.FEATURES, _features_handler)
manager.register(JobType.TRAINING, _training_handler)
manager.register(JobType.INFERENCE, _inference_handler)
manager.register(JobType.ETF_FACTORS, _etf_factors_handler)
manager.register(JobType.ETF_RESEARCH, _etf_research_handler)
manager.register(JobType.ETF_TRAIN, _etf_train_handler)
manager.register(JobType.ETF_BACKTEST, _etf_backtest_handler)
manager.register(JobType.ETF_WALKFORWARD, _etf_walkforward_handler)
manager.register(JobType.STOCK_WALKFORWARD, _stock_walkforward_handler)
manager.register(JobType.STOCK_ML_FACTORS, _stock_ml_factors_handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.feature_dir.mkdir(parents=True, exist_ok=True)
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    yield
    manager.executor.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="DSH Quant Engine", version=__version__, lifespan=lifespan)

# 启动自检（写进服务日志）：key 为空时鉴权会**静默失效**，光看服务起来了发现不了。
print(f"[startup] quant-engine 自检 authEnabled={bool(settings.api_key)} "
      f"authSource={settings.api_key_source or '(空)'} root={settings.project_root}", flush=True)


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if settings.api_key and request.method != "GET":
        supplied = request.headers.get("x-api-key", "")
        if not secrets.compare_digest(supplied, settings.api_key):
            return JSONResponse(status_code=401, content={"detail": "invalid API key"})
    return await call_next(request)


def _weighted_model_ids() -> list[str]:
    """factor-models.yaml 里**带权重**的模型（= legacy 因子模型）。

    legacy 因子选股已于 2026-09-18 退役，配置里只剩 `ml_walkforward`（weights 为空）。
    月度回测 `run_monthly` 是按 weights 加权打分的，遇到空权重会 `used / total_weight`
    除零崩溃，所以这里先把"没有可回测模型"这个事实转成准入门禁。
    """
    try:
        config = load_config(settings.model_config_path)
    except Exception:  # noqa: BLE001
        return []
    return [model_id for model_id, model in (config.get("models") or {}).items() if model.get("weights")]


def _monthly_backtest_block() -> dict | None:
    """月度（legacy 因子）回测的硬阻断原因；None 表示可继续走通用数据准入。

    区分两种"没有可回测模型"的原因，避免把配置读不到误报成功能已退役。
    """
    try:
        config = load_config(settings.model_config_path)
    except Exception as error:  # noqa: BLE001
        return {"id": "monthly-config-unreadable", "label": "因子模型配置", "status": "fail",
                "detail": f"无法读取因子模型配置 {settings.model_config_path}：{error}"}
    if [m for m, body in (config.get("models") or {}).items() if body.get("weights")]:
        return None
    return {"id": "monthly-backtest-retired", "label": "月度因子回测可用性", "status": "fail",
            "detail": "legacy 因子选股已退役（config/factor-models.yaml 只剩 ml_walkforward，"
                      "没有任何带权重的因子模型），因此没有可回测的模型。"}


def _backtest_readiness(kind: str, start: str | None, end: str | None) -> dict:
    """回测数据准入：短线与 legacy 月度回测在停用后直接判不通过。"""
    if kind == "short" and not settings.short_backtest_enabled:
        return {
            "kind": kind,
            "range": {"startDate": start, "endDate": end},
            "allowed": False,
            "checkedAt": utc_now().isoformat(),
            "checks": [{"id": "short-backtest-disabled", "label": "短线回测可用性", "status": "fail",
                        "detail": "短线回测已停用（QUANT_ENGINE_SHORT_BACKTEST_ENABLED=0）："
                                  "其依赖的股票 5 分钟线已按使用方要求清零。"}],
            "blockedReasons": ["短线回测已停用（股票分钟线已清除）"],
            "hardBlocked": True,  # 不可通过 forceDataGate 绕过（见 api.create_job）
            "forcePolicy": "该限制不可通过 forceDataGate 绕过",
        }
    if kind == "monthly":
        block = _monthly_backtest_block()
        if block:
            return {
                "kind": kind,
                "range": None,
                "allowed": False,
                "checkedAt": utc_now().isoformat(),
                "checks": [block],
                "blockedReasons": [block["detail"]],
                "hardBlocked": True,
                "forcePolicy": "该限制不可通过 forceDataGate 绕过",
            }
    return check_backtest_readiness(kind, settings.market_path, settings.finance_path,
                                    settings.minute_path, start, end)


app.include_router(
    create_router(manager, _backtest_readiness),
    prefix="/api/v1",
)


@app.get("/api/v1/etf-quant/research/latest")
def etf_research_latest() -> dict:
    return etf_runner.latest_research(settings.etf_quant_db_path)


@app.get("/api/v1/etf-quant/backtest/latest")
def etf_backtest_latest() -> dict:
    return etf_runner.latest_backtest(settings.etf_quant_db_path)


@app.get("/api/v1/etf-quant/train/latest")
def etf_train_latest() -> dict:
    return etf_runner.latest_train(settings.etf_quant_db_path)


@app.get("/api/v1/etf-quant/walkforward/latest")
def etf_walkforward_latest() -> dict:
    return etf_runner.latest_walkforward(settings.etf_quant_db_path)


# ---------------------------------------------------------------- 股票 ML 模型（walkforward + 选股候选）

@app.get("/api/v1/stock-ml/walkforward/latest")
def stock_ml_walkforward_latest() -> dict:
    return stock_ml_latest_walkforward(settings.factors_path)


@app.get("/api/v1/stock-ml/walkforward/list")
def stock_ml_walkforward_list(limit: int = Query(50, ge=1, le=200)) -> dict:
    return stock_ml_list_walkforward(settings.factors_path, limit=limit)


# ---- 配置可观测性：回答"每次运行到底用了什么配置、哪个代码版本" ----

def _config_integrity_con():
    from .stock_ml.common import connect
    return connect(settings.factors_path)


@app.get("/api/v1/stock-ml/config/fingerprints")
def stock_ml_config_fingerprints(limit: int = Query(50, ge=1, le=500)) -> dict:
    """列出运行过的配置指纹（按时间倒序）。

    相同 configSha256 == 完全相同的生效配置。指纹机制上线前的旧记录不在本表内。
    """
    from .stock_ml.config_integrity import list_fingerprints
    con = _config_integrity_con()
    try:
        items = list_fingerprints(con, limit=limit)
    finally:
        con.close()
    return {"count": len(items), "items": items, "git": stock_ml_git_info()}


@app.get("/api/v1/stock-ml/config/fingerprint")
def stock_ml_config_fingerprint(sha256: str = Query(..., min_length=8)) -> dict:
    """按配置哈希取回完整存档：生效配置 + 原始 YAML 文本 + 代码版本。"""
    from .stock_ml.config_integrity import get_fingerprint
    con = _config_integrity_con()
    try:
        found = get_fingerprint(con, sha256)
    finally:
        con.close()
    if found is None:
        return {"ok": False, "error": f"未找到配置指纹: {sha256}"}
    return {"ok": True, **found}


@app.post("/api/v1/stock-ml/walkforward/publish")
def stock_ml_walkforward_publish(run_id: str = Query(...)) -> dict:
    """发布指定walkforward版本，设为当前正式使用的模型"""
    return stock_ml_publish_walkforward(settings.factors_path, run_id)


@app.delete("/api/v1/stock-ml/walkforward/delete")
def stock_ml_walkforward_delete(run_id: str = Query(...)) -> dict:
    """删除指定walkforward版本（已发布的版本不允许删除）"""
    return stock_ml_delete_walkforward(settings.factors_path, run_id)


@app.put("/api/v1/stock-ml/walkforward/notes")
async def stock_ml_walkforward_notes(request: Request) -> dict:
    """更新指定walkforward版本的备注/介绍"""
    body = await request.json()
    run_id = body.get("runId") or body.get("run_id")
    notes = body.get("notes", "")
    if not run_id:
        return {"ok": False, "error": "缺少runId参数"}
    return stock_ml_update_walkforward_notes(settings.factors_path, run_id, notes)


@app.get("/api/v1/stock-ml/candidates")
def stock_ml_candidates(limit: int = Query(50, ge=1, le=200)) -> dict:
    return stock_ml_latest_candidates(settings.factors_path, limit=limit)


@app.get("/api/v1/stock-ml/label-audit")
def stock_ml_label_audit(label: str = Query("forward_5"),
                         code_glob: str = Query("*", description="代码 GLOB 过滤，如 0000* 取子集"),
                         threshold: float = Query(0.005, gt=0, le=1)) -> dict:
    """口径审计（只读）：量化「未复权 close 标签」与「复权价标签」的差异，不修改任何数据。"""
    return stock_ml_audit_label_price_basis(settings.factors_path, settings.market_path,
                                           label, code_glob, threshold)


@app.get("/api/v1/stock/technical-timing/backtest/latest")
def technical_timing_backtest_latest(variant: str = Query("long", pattern="^(long|short)$")) -> dict:
    """V5 技术面择时回测：直接读 artifacts 下的回测 CSV 现场计算（可复算，非写死数字）。"""
    return summarize_v5_backtest(settings.stock_ml_artifact_dir, variant)


@app.get("/api/v1/stock-ml/config")
def stock_ml_config() -> dict:
    return stock_ml_get_model_config(settings.stock_ml_config_path)


@app.get("/api/v1/stock-ml/feature-importance")
def stock_ml_feature_importance(top_n: int = Query(50, ge=1, le=100)) -> dict:
    return stock_ml_get_feature_importance(settings.stock_ml_artifact_dir / "stock-wf-model.txt", top_n=top_n)


def _resolve_stock_ml_model(body: dict) -> Path:
    """run_id / model_path / 缺省最新模型 → 模型文件路径（HTTP 端点与 job handler 共用）。"""
    model_path_str = body.get("modelPath") or body.get("model_path")
    if model_path_str:
        return Path(str(model_path_str))
    run_id = body.get("runId") or body.get("run_id")
    if run_id:
        found = stock_ml_find_model_path(str(run_id), settings.stock_ml_artifact_dir, settings.factors_path)
        if not found:
            raise FileNotFoundError(f"未找到 run_id={run_id} 对应的模型文件")
        return Path(found)
    return settings.stock_ml_artifact_dir / "stock-wf-model.txt"


def _run_stock_ml_backtest(body: dict, progress=None) -> dict:
    model_path = _resolve_stock_ml_model(body)
    top_n = body.get("topN") or body.get("top_n")
    cfg = stock_ml_load_config(settings.stock_ml_config_path)
    if top_n:
        cfg["topN"] = int(top_n)
    kwargs = {"progress": progress} if progress else {}
    return stock_ml_backtest_with_model(
        settings.factors_path, settings.market_path, model_path, cfg,
        start_date=body.get("startDate") or body.get("start_date"),
        end_date=body.get("endDate") or body.get("end_date"), **kwargs)


def _run_stock_ml_predict(body: dict) -> dict:
    model_path = _resolve_stock_ml_model(body)
    cfg = stock_ml_load_config(settings.stock_ml_config_path)
    return stock_ml_predict_with_model(
        settings.factors_path, model_path, cfg,
        top_n=int(body.get("topN") or body.get("top_n") or 10),
        trade_date=body.get("tradeDate") or body.get("trade_date"))


def _stock_ml_backtest_handler(parameters, progress, cancelled):
    return _run_stock_ml_backtest(parameters or {}, progress)


def _stock_ml_predict_handler(parameters, progress, cancelled):
    progress(10)
    result = _run_stock_ml_predict(parameters or {})
    progress(100)
    return result


# 注册需在 handler 定义之后（模块级执行顺序）
manager.register(JobType.STOCK_ML_BACKTEST, _stock_ml_backtest_handler)
manager.register(JobType.STOCK_ML_PREDICT, _stock_ml_predict_handler)


def _industry_factors_handler(parameters, progress, cancelled):
    """行业轮动因子：原先只有 scripts/ 下的脚本能产出（服务外、会静默过期）。"""
    return build_industry_factors(
        settings.market_path, settings.factors_path,
        start_date=str((parameters or {}).get("startDate") or "20210101"),
        progress=progress, cancelled=cancelled)


def _money_flow_factors_handler(parameters, progress, cancelled):
    """资金流向因子：同上。"""
    return build_money_flow_factors(
        settings.market_path, settings.factors_path,
        start_date=str((parameters or {}).get("startDate") or "20210101"),
        progress=progress, cancelled=cancelled)


manager.register(JobType.STOCK_INDUSTRY_FACTORS, _industry_factors_handler)
manager.register(JobType.STOCK_MONEY_FLOW_FACTORS, _money_flow_factors_handler)


@app.get("/api/v1/stock-ml/data-freshness")
def stock_ml_data_freshness() -> dict:
    """辅助因子表新鲜度：行业/资金流因子 vs 最新因子日（落后会在 walkforward 结果里告警）。"""
    return aux_factor_freshness(settings.factors_path, None)


@app.post("/api/v1/stock-ml/backtest")
async def stock_ml_backtest(request: Request) -> dict:
    """用指定模型（run_id或model_path）对指定时间段进行回测（同步；长任务请用 /jobs）"""
    body = await request.json()
    try:
        # 重活（加载全量因子+行情）必须离开事件循环：async 端点里直接调用会阻塞整个 ASGI 循环
        return await run_in_threadpool(_run_stock_ml_backtest, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/stock-ml/predict")
async def stock_ml_predict(request: Request) -> dict:
    """用指定模型（run_id或model_path）进行只读选股预览。"""
    body = await request.json()
    try:
        return await run_in_threadpool(_run_stock_ml_predict, body)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/v1/stock-ml/candidates/refresh")
async def stock_ml_candidates_refresh(request: Request) -> dict:
    """使用当前 published 模型刷新正式候选；不训练、不切换模型版本。"""
    body = await request.json()
    try:
        return await run_in_threadpool(
            stock_ml_refresh_published_candidates,
            settings.factors_path, settings.stock_ml_config_path,
            body.get("tradeDate") or body.get("trade_date"))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------- ETF 模拟盘（纯增量台账）

@app.post("/api/v1/etf-quant/sim/start")
def etf_sim_start(initial_capital: float = Query(1_000_000), start_date: str | None = Query(None)) -> dict:
    # snapshot_root=artifacts：与 stock_ml/股票模拟盘共用同一个内容寻址配置档案库。
    return etf_sim.start_sim(settings.etf_quant_db_path, settings.market_path, initial_capital,
                             start_date, snapshot_root=settings.artifact_dir)


@app.post("/api/v1/etf-quant/sim/advance")
def etf_sim_advance(run_id: str = Query(...)) -> dict:
    return etf_sim.advance_sim(settings.etf_quant_db_path, settings.market_path, run_id)


@app.get("/api/v1/etf-quant/sim/status")
def etf_sim_status(run_id: str | None = None) -> dict:
    return etf_sim.sim_status(settings.etf_quant_db_path, run_id)


@app.get("/api/v1/etf-quant/sim/history")
def etf_sim_history(run_id: str | None = None, limit: int = 500) -> dict:
    return etf_sim.sim_history(settings.etf_quant_db_path, run_id, limit)


# ---------------------------------------------------------------- 股票 14:40 信号、当日尾盘成交模拟盘（纯增量台账）

@app.get("/api/v1/stock/sim/runs")
def stock_sim_runs(include_archived: bool = False) -> dict:
    return stock_sim.list_runs(settings.factors_path, include_archived=include_archived)


def _known_sim_models() -> list[str]:
    """选股模型字典（config/factor-models.yaml 的 models 段）——模拟盘只接受这里登记过的 model_id。

    背景：前端曾提供 `ml_technical_timing*` 两个后端根本不生产的模型，而 start_sim 不校验
    model_id，于是能建成净值恒 1.0 的"死账本"。校验放在入口，错误在创建时刻就暴露。
    """
    try:
        return sorted((load_config(settings.model_config_path).get("models") or {}).keys())
    except Exception:  # noqa: BLE001
        return []


@app.post("/api/v1/stock/sim/start")
def stock_sim_start(model: str = Query("ml_walkforward"), top_n: int = Query(30, ge=1, le=500),
                    initial_capital: float = Query(1_000_000),
                    regime_filter: bool = Query(False)) -> dict:
    known = _known_sim_models()
    if known and model not in known:
        raise HTTPException(status_code=400,
                            detail=f"未知的选股模型：{model}；可用：{', '.join(known)}")
    try:
        return _stock_sim_start(model, top_n, initial_capital, regime_filter)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def _stock_sim_start(model: str, top_n: int, initial_capital: float, regime_filter: bool) -> dict:
    if model == "ml_walkforward":
        cfg = _stock_ml_config()
        bt = dict(cfg.get("backtest") or {})
        top_n = int(bt.get("topN", top_n))
        return stock_sim.start_sim(settings.factors_path, model, top_n, initial_capital,
                                   commission_rate=float(bt.get("commissionRate", 0.00008)),
                                   stamp_duty_rate=float(bt.get("stampDutyRate", 0.0005)),
                                   slippage_rate=float(bt.get("slippageRate", 0.004)),
                                   regime_filter=regime_filter,
                                   rebalance_days=int(bt.get("rebalanceDays", 3)),
                                   holding_buffer_rank=int(bt.get("holdingBufferRank", 10)),
                                   initial_stop_loss=float(bt.get("initialStopLoss", 0.05)),
                                   trailing_drawdown=float(bt.get("trailingDrawdown", 0.08)),
                                   snapshot_root=settings.artifact_dir,
                                   # ML 模拟盘的参数全部来自 stock-ml.yaml，因此必须把那份
                                   # YAML 一起冻结：否则只记下解析出的几个数字，无法回答
                                   # "当时那个 topN/止损/择时开关是哪份配置给的"。
                                   source_config=cfg,
                                   source_config_path=settings.stock_ml_config_path)
    return stock_sim.start_sim(settings.factors_path, model, top_n, initial_capital,
                               regime_filter=regime_filter,
                               snapshot_root=settings.artifact_dir)


@app.post("/api/v1/stock/sim/advance")
def stock_sim_advance(run_id: str = Query(...)) -> dict:
    return stock_sim.advance_sim(settings.factors_path, settings.market_path, run_id)


@app.get("/api/v1/stock/sim/status")
def stock_sim_status(run_id: str | None = None) -> dict:
    return stock_sim.sim_status(settings.factors_path, run_id)


@app.get("/api/v1/stock/sim/history")
def stock_sim_history(run_id: str | None = None, limit: int = 500) -> dict:
    return stock_sim.sim_history(settings.factors_path, run_id, limit)


@app.get("/api/v1/stock/regime-analysis")
def stock_regime_analysis(run_id: str | None = None) -> dict:
    """只读离线诊断：亏损归因 + 沪深300 MA20 熊市空仓择时对照（不改原回测）。"""
    return stock_regime.analyze(settings.backtest_path, settings.market_path, run_id)
