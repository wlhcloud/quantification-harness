# -*- coding: utf-8 -*-
"""股票机器学习 walkforward：横截面因子 → LightGBM Ranker滚动训练 → 14:40尾盘回测 → 最新持仓写回。

原单文件已拆分（行为不变，公开 API 保持）：
  - common.py    常量/表结构/幂等迁移/配置体检
  - factors.py   build_factors（因子快照 + forward 标签）
  - backtest.py  行情加载、ensemble 加载、单窗口回测、指定模型回测/推理
  - __init__.py  run_walkforward 主流程、run 管理、配置/审计/特征重要性（并对上述模块做再导出）

表与口径：stock_ml_factors（因子快照展开 + forward_3/5/20 标签）、stock_walkforward_runs（滚动窗口曲线与持仓）、
selection_candidates 新增 model_id='ml_walkforward'；14:40信号、当日尾盘收盘代理价成交、topN等权。
"""
from __future__ import annotations

import json
import hashlib
import math
import multiprocessing
import pickle
import sqlite3
import sys
import time
import gc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

# 再导出：保持 `from quant_engine.stock_ml import X` 的既有调用方式不变
from .aux_factors import aux_factor_freshness  # noqa: F401  (再导出，便于外部按 stock_ml.aux_factor_freshness 调用)
from .backtest import (  # noqa: F401
    EnsembleRanker, _close, _exec_price, _load_bars, _load_benchmark, _load_ensemble_or_single,
    _lookback_start_date,
    _regime_filter_map, _run_window_backtest, backtest_with_model, find_model_path_by_run_id,
    predict_with_model,
)
from .common import (  # noqa: F401
    BACKTEST_CONFIG_KEYS, FACTOR_COLUMNS, FACTOR_MIGRATIONS, INDUSTRY_FACTOR_COLUMNS,
    LABEL_COLUMNS, MIN_HISTORY, ML_MODEL_ID, MONEY_FLOW_FACTOR_COLUMNS, SCHEMA,
    UNIVERSE_CONFIG_KEYS, _assert_label_supported, _config_warnings, _ensure_factors_columns,
    _ensure_selection_columns, _ensure_walkforward_columns, _forward_labels, _now, _rolling_corr, _validate_execution_config,
    connect, load_config,
)
from .factors import build_factors  # noqa: F401


# ---------------------------------------------------------------- 主流程

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并覆盖字典：嵌套段（如 backtest.technicalTiming）按叶子键覆盖，
    而不是整段替换。否则 job 参数只改 technicalTiming 里的一个开关，会把同段的
    其他键（goldenCross/requireAboveMa20 等）一起丢掉，导致实验静默失效。"""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _sweep_stale_window_tmp(artifact_dir: Path, preserve_seconds: float = 3600.0) -> int:
    """清理上次运行残留的窗口临时目录（隔离 CUDA 的 2.5GB train-frame.pkl）。

    job 被 kill 或进程被 OOM 杀掉时 finally 不执行，临时帧会永久残留。
    只清理超过 preserve_seconds 的目录，避免误删正在运行的其它 job 的文件。
    """
    import shutil
    import time as _time
    tmp_root = Path(artifact_dir) / "tmp"
    if not tmp_root.is_dir():
        return 0
    removed = 0
    now = _time.time()
    for child in tmp_root.iterdir():
        if not child.is_dir() or not child.name.startswith("stock-wf-"):
            continue
        try:
            if now - child.stat().st_mtime < preserve_seconds:
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    if removed:
        print(f"[INFO] 已清理 {removed} 个残留窗口临时目录（{tmp_root}）", flush=True)
    return removed


def _release_native_memory() -> None:
    """Best-effort release for NumPy/LightGBM allocations between windows.

    CPython releases the objects, but glibc may retain their arenas in a
    long-running uvicorn worker.  malloc_trim is Linux-only and deliberately
    optional so the CPU/Windows fallback keeps working.
    """
    gc.collect()
    if sys.platform.startswith("linux"):
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except (AttributeError, OSError):
            pass


def _publish_gate_failures(metrics: dict[str, Any], evaluation_scope: str,
                           gate_cfg: dict[str, Any]) -> list[str]:
    """Return explicit reasons that a research run cannot become production."""
    failures: list[str] = []
    if evaluation_scope != "full_walkforward":
        failures.append("仅完整full_walkforward评估允许发布")
    min_windows = int(gate_cfg.get("minWindows", 20))
    min_sharpe = float(gate_cfg.get("minSharpe", 0.5))
    min_excess = float(gate_cfg.get("minExcessReturn", 0.0))
    max_drawdown = abs(float(gate_cfg.get("maxDrawdown", 0.30)))
    if int(metrics.get("windows", 0)) < min_windows:
        failures.append(f"窗口数低于{min_windows}")
    if float(metrics.get("sharpe", float("-inf"))) < min_sharpe:
        failures.append(f"Sharpe低于{min_sharpe:g}")
    if float(metrics.get("excessReturn", float("-inf"))) <= min_excess:
        failures.append(f"超额收益不高于{min_excess:g}")
    if float(metrics.get("maxDrawdown", float("-inf"))) < -max_drawdown:
        failures.append(f"最大回撤超过{max_drawdown:.0%}")
    if "minNetAnnualReturn" in gate_cfg:
        minimum = float(gate_cfg["minNetAnnualReturn"])
        if float(metrics.get("annualReturn", float("-inf"))) < minimum:
            failures.append(f"净年化收益低于{minimum:.0%}")
    if "maxAnnualTurnover" in gate_cfg:
        maximum = float(gate_cfg["maxAnnualTurnover"])
        if float(metrics.get("annualTurnover", float("inf"))) > maximum:
            failures.append(f"年化换手超过{maximum:g}倍")
    if "minPositiveWindowRate" in gate_cfg:
        minimum = float(gate_cfg["minPositiveWindowRate"])
        if float(metrics.get("positiveWindowRate", float("-inf"))) < minimum:
            failures.append(f"盈利窗口比例低于{minimum:.0%}")
    return failures


def _merge_publish_blockers(existing: list[str], additional: list[str]) -> list[str]:
    """Accumulate independent publication checks without losing earlier failures."""
    return list(dict.fromkeys([*existing, *additional]))


def _train_ranker_isolated(frame_file: Path, label: str, model_cfg: dict[str, Any],
                           output_dir: Path, cancelled: Callable[[], bool]) -> dict[str, Any]:
    """Train one CUDA model in a disposable process to contain native leaks."""
    from ._ranker_worker import run_worker

    request_file = output_dir / "worker-request.pkl"
    result_file = output_dir / "worker-result.json"
    with request_file.open("wb") as fh:
        pickle.dump({"frameFile": str(frame_file), "label": label,
                     "modelConfig": model_cfg, "outputDir": str(output_dir)},
                    fh, protocol=pickle.HIGHEST_PROTOCOL)
    proc = multiprocessing.get_context("spawn").Process(
        target=run_worker, args=(str(request_file), str(result_file)), daemon=False)
    proc.start()
    while proc.is_alive():
        if cancelled():
            proc.terminate()
            proc.join(timeout=10)
            if proc.is_alive():
                proc.kill()
                proc.join()
            raise RuntimeError("training cancelled")
        proc.join(timeout=0.5)
    if proc.exitcode != 0 or not result_file.exists():
        raise RuntimeError(f"isolated ranker failed (exitcode={proc.exitcode})")
    envelope = json.loads(result_file.read_text(encoding="utf-8"))
    if not envelope.get("ok"):
        raise RuntimeError(f"isolated ranker failed: {envelope.get('error')}\n{envelope.get('traceback', '')}")
    return dict(envelope["result"])

def run_walkforward(factors_path: Path, market_path: Path, artifact_dir: Path, cfg: dict[str, Any],
                    progress: Callable[[float], None] = lambda p: None,
                    cancelled: Callable[[], bool] = lambda: False,
                    parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    import tempfile
    import lightgbm as lgb

    from ..etf_quant import ranker as etf_ranker

    label = str(cfg.get("label", "forward_5"))
    wf_cfg = dict(cfg.get("walkforward") or {})
    if parameters:
        if "walkforward" in parameters:
            wf_cfg.update(parameters["walkforward"])
        else:
            wf_cfg.update(parameters)
    model_cfg = dict(cfg.get("model", {}))
    model_cfg.update(wf_cfg.get("model") or {})
    # purge/embargo：写在 walkforward 段或 model 段都接受；未配置时 train_ranker 默认 0（数值不变）
    if wf_cfg.get("embargoDays") is not None:
        model_cfg["embargoDays"] = int(wf_cfg["embargoDays"])
    elif cfg.get("embargoDays") is not None:
        model_cfg["embargoDays"] = int(cfg["embargoDays"])
    # 关键修复：yaml 的 featuresOverride 在顶层，而 train_ranker 从 model_cfg 读取；
    # 若 model_cfg 未显式指定特征列表，则回退注入顶层 featuresOverride，避免误用 ETF 默认 18 特征
    if not model_cfg.get("featuresOverride") and cfg.get("featuresOverride"):
        model_cfg["featuresOverride"] = list(cfg["featuresOverride"])
    bt_cfg = dict(cfg.get("backtest", {}))
    # 深合并：backtest 段里有 technicalTiming 这样的嵌套字典。原实现用 update() 做浅合并，
    # job 参数传 {"backtest": {"technicalTiming": {"deathCrossRequireBelowMa20": true}}} 会把
    # 整个 technicalTiming 段替换掉，goldenCross 等键随之丢失（实验静默失效）。
    bt_cfg = _deep_merge(bt_cfg, wf_cfg.get("backtest") or {})
    if "rebalanceDays" in wf_cfg:
        bt_cfg["rebalanceDays"] = int(wf_cfg["rebalanceDays"])
    _validate_execution_config(bt_cfg)
    dry_run = bool(wf_cfg.get("dryRun", False))
    config_warnings = _config_warnings(cfg)
    publish_gate_cfg = dict(cfg.get("publishGate") or {})
    for warning in config_warnings:
        print(f"[WARN] stock-ml 配置：{warning}", flush=True)
    min_train = int(wf_cfg.get("minTrain", wf_cfg.get("minTrainDays", 200)))
    valid_days = int(wf_cfg.get("validDays", 40))
    test_days = int(wf_cfg.get("testDays", 40))
    step_days = int(wf_cfg.get("stepDays", 40))
    load_history_days = max(0, int(wf_cfg.get("loadHistoryDays", 0) or 0))
    max_windows = int(wf_cfg.get("maxWindows", 0) or 0)

    con = connect(factors_path)
    try:
        con.executescript(SCHEMA)
        _ensure_factors_columns(con)
        # 标签契约校验：配置的 label 必须是本服务产出的列。原实现只在 SQL 里拼 label，
        # 一旦配置的列不由本服务维护（如历史上脚本写入的 forward_3），就会静默用陈旧标签训练。
        if label not in LABEL_COLUMNS:
            raise ValueError(
                f"config/stock-ml.yaml 的 label={label!r} 不是本服务产出的标签列；可选 {LABEL_COLUMNS}")
        label_cols = {r[1] for r in con.execute("PRAGMA table_info(stock_ml_factors)")}
        if label not in label_cols:
            raise ValueError(f"stock_ml_factors 缺少标签列 {label}；请先重建特征（stock_ml_factors job）")
        # 检查扩展因子表是否存在（零破坏：不存在则跳过 JOIN，只用原因子）
        has_industry = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_industry_factors'"
        ).fetchone() is not None
        has_money_flow = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_money_flow_factors'"
        ).fetchone() is not None
        history_cutoff: str | None = None
        if load_history_days > 0:
            cutoff_row = con.execute(
                "SELECT trade_date FROM (SELECT DISTINCT trade_date FROM stock_ml_factors "
                "ORDER BY trade_date DESC LIMIT ?) ORDER BY trade_date LIMIT 1",
                (load_history_days,)).fetchone()
            history_cutoff = cutoff_row[0] if cutoff_row else None
        history_filter = " AND a.trade_date>=?" if history_cutoff else ""
        query_params: tuple[Any, ...] = (history_cutoff,) if history_cutoff else ()
        base_cols = ",".join([f"a.{c}" for c in FACTOR_COLUMNS])
        extra_cols = []
        joins = []
        if has_industry:
            extra_cols.extend([f"b.{c}" for c in INDUSTRY_FACTOR_COLUMNS])
            joins.append("LEFT JOIN stock_industry_factors b ON a.trade_date=b.trade_date AND a.code=b.code")
        if has_money_flow:
            mf_alias = "c" if has_industry else "b"
            extra_cols.extend([f"{mf_alias}.{c}" for c in MONEY_FLOW_FACTOR_COLUMNS])
            joins.append(f"LEFT JOIN stock_money_flow_factors {mf_alias} ON a.trade_date={mf_alias}.trade_date AND a.code={mf_alias}.code")
        if extra_cols:
            sql = (
                "SELECT a.trade_date, a.code, " + base_cols + "," +
                ",".join(extra_cols) + ",a." + label +
                " FROM stock_ml_factors a " + " ".join(joins) +
                " WHERE a." + label + " IS NOT NULL" + history_filter + " ORDER BY a.trade_date, a.code"
            )
        else:
            sql = (
                "SELECT " + ",".join(["trade_date", "code", *FACTOR_COLUMNS, label]) +
                " FROM stock_ml_factors a WHERE a." + label + " IS NOT NULL" + history_filter +
                " ORDER BY a.trade_date, a.code"
            )
        con.set_progress_handler(lambda: 1 if cancelled() else 0, 100_000)
        try:
            rows = con.execute(sql, query_params).fetchall()
        except sqlite3.OperationalError as error:
            if cancelled() and "interrupt" in str(error).lower():
                raise RuntimeError("stock walkforward cancelled during factor loading") from error
            raise
        finally:
            con.set_progress_handler(None, 0)
    finally:
        con.close()
    frame = [dict(r) for r in rows]
    if not frame:
        raise ValueError("无因子/标签数据，请先运行 stock_ml_factors")
    dates = sorted({r["trade_date"] for r in frame})
    first_test_index = min_train + valid_days
    if max_windows > 0:
        first_test_index = max(first_test_index, len(dates) - max_windows * step_days - test_days)
    first_bar_date = dates[min(first_test_index, len(dates) - 1)]
    bar_start_date = (_lookback_start_date(market_path, first_bar_date, 25)
                      if (bt_cfg.get("technicalTiming") or {}).get("enabled", False)
                      else first_bar_date)
    bars = _load_bars(market_path, bar_start_date, dates[-1])
    bench = _load_benchmark(market_path)
    regime_map = _regime_filter_map(bench) if bt_cfg.get("regimeFilter", False) else None

    run_id = f"stock-wf-{_now().replace(':', '').replace('-', '').replace('.', '')}"
    # 代码版本自证：曾出现"以为改了代码、实际跑的是旧进程"的情形，结果看起来像
    # "改动无效"。把判定标记同时写进 job 结果与 run-metadata，任何人看 run 都能确认。
    engine_version = {"pendingSellRetry": True, "cudaInProcessSwitch": True}
    daily_rows: list[dict[str, Any]] = []
    window_stats: list[dict[str, Any]] = []
    total_transaction_cost = 0.0
    total_commission_cost = 0.0
    total_stamp_cost = 0.0
    total_slippage_cost = 0.0
    total_turnover = 0.0
    total_stop_turnover = 0.0
    total_rebalance_turnover = 0.0
    total_trades = 0
    total_stop_trades = 0
    total_corporate_action_adjustments = 0
    all_tree_counts: list[int] = []
    trade_records: list[dict[str, Any]] = []
    effective_devices: set[str] = set()
    latest_model_path: str | None = None
    k = min_train + valid_days
    n_windows = max(1, (len(dates) - min_train - valid_days) // step_days)
    evaluation_scope = "recent_refresh" if max_windows > 0 else "full_walkforward"
    sample_rows = int(wf_cfg.get("sampleRows", 0) or 0)
    # 多种子集成：model.randomStates 列表优先；否则用单个 model.randomState（向后兼容）
    ensemble_seeds = model_cfg.get("randomStates")
    if ensemble_seeds is None:
        ensemble_seeds = [int(model_cfg.get("randomState", 42))]
    ensemble_seeds = [int(s) for s in ensemble_seeds]
    n_ensemble = len(ensemble_seeds)
    ensemble_method = str(model_cfg.get("ensembleMethod") or cfg.get("ensembleMethod") or "mean_zscore")
    if ensemble_method not in ("mean_zscore", "median_zscore"):
        ensemble_method = "mean_zscore"
    if max_windows > 0 and max_windows < n_windows:
        # 只跑最近 max_windows 个窗口（每日链增量：控制耗时）
        k = max(min_train + valid_days, len(dates) - max_windows * step_days - test_days)
        n_windows = max_windows
    window_index = 0
    # 残留临时帧清扫：隔离 CUDA 路径每个窗口落一个约 2.5GB 的 train-frame.pkl，
    # 正常路径由 finally 里的 rmtree 清掉；但 job 被 kill / 进程被 OOM 杀掉时
    # finally 不执行，文件会永久残留（实测机器上积了 2.6GB×N）。
    # 这里在开跑前清掉上一次运行留下的窗口目录，避免磁盘被慢慢吃满。
    _sweep_stale_window_tmp(artifact_dir, preserve_seconds=3600)
    while k + test_days <= len(dates):
        test_start, test_end = dates[k], dates[k + test_days - 1]
        train_frame = [r for r in frame if r["trade_date"] < test_start]
        if sample_rows and len(train_frame) > sample_rows:
            # 按日期等比例抽样（保持 query group 结构），控制训练内存峰值
            import random
            rng = random.Random(42)
            by_date_t: dict[str, list[dict[str, Any]]] = {}
            for r in train_frame:
                by_date_t.setdefault(r["trade_date"], []).append(r)
            per_day = max(10, sample_rows // max(1, len(by_date_t)))
            sampled: list[dict[str, Any]] = []
            for _d, rs in by_date_t.items():
                sampled.extend(rs if len(rs) <= per_day else rng.sample(rs, per_day))
            train_frame = sampled
        if len(train_frame) < valid_days + test_days + 20:
            break
        window_index += 1
        import shutil
        td = artifact_dir / "tmp" / f"stock-wf-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}-w{window_index}"
        td.mkdir(parents=True, exist_ok=True)
        try:
            # 多种子集成：每个窗口训练 n_ensemble 个不同 randomState 的模型
            window_boosters: list[Any] = []
            window_res: dict[str, Any] | None = None
            window_ics: list[float] = []
            use_isolated_cuda = (sys.platform.startswith("linux") and
                                 str(model_cfg.get("deviceType", "cpu")).lower() in {"cuda", "gpu"} and
                                 not bool(model_cfg.get("cudaInProcess", False)))
            frame_file = Path(td) / "train-frame.pkl"
            if use_isolated_cuda:
                with frame_file.open("wb") as fh:
                    pickle.dump(train_frame, fh, protocol=pickle.HIGHEST_PROTOCOL)
            for _ei, _seed in enumerate(ensemble_seeds):
                if cancelled():
                    raise RuntimeError("training cancelled")
                seed_cfg = dict(model_cfg)
                seed_cfg["randomState"] = _seed
                seed_dir = Path(td) / f"seed{_seed}"
                seed_dir.mkdir(parents=True, exist_ok=True)
                if use_isolated_cuda:
                    res = _train_ranker_isolated(frame_file, label, seed_cfg, seed_dir, cancelled)
                else:
                    # 进程内训练：CPU 一直走这里；CUDA 在 cudaInProcess=true 时也走这里。
                    # 进程内的 CUDA 训练才是真正用 GPU 算（实测 150 棵 / 41 万行 4.3s vs CPU 104s），
                    # 隔离路径的开销（2.4GB pickle 往返 + spawn + 帧转换）反而成了瓶颈。
                    res = etf_ranker.train_ranker(train_frame, label, seed_cfg, seed_dir,
                                                  cancelled=cancelled)
                effective_devices.add(str(res.get("effectiveDevice", "cpu")))
                b = lgb.Booster(model_file=res["modelPath"])
                window_boosters.append(b)
                if window_res is None:
                    window_res = res  # 主模型（第一个种子）的元信息
                if res.get("rankIc") is not None:
                    window_ics.append(float(res["rankIc"]))
                progress(10 + 80 * (window_index - 1 + 0.7 * (_ei + 1) / max(1, n_ensemble)) /
                         max(1, n_windows))
            model = EnsembleRanker(window_boosters, ensemble_seeds, method=ensemble_method) if n_ensemble > 1 else window_boosters[0]
            res = window_res  # type: ignore[assignment]
            # 模型规模自证：早停判据抖动时，模型可能只长出个位数棵树（"残废浅模型"），
            # 而历史指标里没有任何一项会报警。这里把树数写进窗口统计并做显式告警。
            window_tree_counts = [int(b.num_trees()) for b in window_boosters]
            all_tree_counts.extend(window_tree_counts)
            window_mean_trees = sum(window_tree_counts) / len(window_tree_counts)
            latest_model_path = str(artifact_dir / "stock-wf-model.txt")
            latest_model_path_dir = Path(latest_model_path).parent
            latest_model_path_dir.mkdir(parents=True, exist_ok=True)
            import shutil
            if n_ensemble > 1:
                # 保存全部种子模型 + 集成清单
                saved = model.save_models(latest_model_path_dir, "stock-wf-model")  # type: ignore[union-attr]
                manifest = {
                    "seeds": ensemble_seeds,
                    "modelFiles": saved,
                    "ensemble": ensemble_method,
                    "generatedAt": _now(),
                }
                (latest_model_path_dir / "stock-wf-model_ensemble_seeds.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                shutil.copy(res["modelPath"], latest_model_path)  # type: ignore[union-attr]
                # 单模型时清理可能残留的旧集成清单，避免误加载
                stale_manifest = latest_model_path_dir / "stock-wf-model_ensemble_seeds.json"
                if stale_manifest.exists():
                    stale_manifest.unlink()
        finally:
            shutil.rmtree(td, ignore_errors=True)
        test_days_list = [d for d in dates if test_start <= d <= test_end]
        test_frame = [r for r in frame if test_start <= r["trade_date"] <= test_end]
        window_daily = _run_window_backtest(
            test_frame, model, test_days_list, bars, bt_cfg,
            progress=lambda p: progress(10 + 80 * (window_index - 1 + 0.7 + 0.3 * p / 100) /
                                        max(1, n_windows)),
            start_progress=0, end_progress=100, regime_map=regime_map)
        if window_daily:
            total_transaction_cost += float(window_daily[-1].get("cumulativeCost", 0.0))
            total_commission_cost += float(window_daily[-1].get("cumulativeCommission", 0.0))
            total_stamp_cost += float(window_daily[-1].get("cumulativeStampDuty", 0.0))
            total_slippage_cost += float(window_daily[-1].get("cumulativeSlippage", 0.0))
            total_turnover += float(window_daily[-1].get("cumulativeTurnover", 0.0))
            total_stop_turnover += float(window_daily[-1].get("cumulativeStopTurnover", 0.0))
            total_rebalance_turnover += float(window_daily[-1].get("cumulativeRebalanceTurnover", 0.0))
            total_trades += int(window_daily[-1].get("tradeCount", 0))
            total_stop_trades += int(window_daily[-1].get("stopTradeCount", 0))
            total_corporate_action_adjustments += int(window_daily[-1].get("corporateActionAdjustments", 0))
            for day_row in window_daily:
                trade_records.extend(day_row.get("trades") or [])
        prev = daily_rows[-1]["nav"] if daily_rows else 1.0
        prev_no_cost = daily_rows[-1]["noCostNav"] if daily_rows else 1.0
        # 每个窗口都以 1.0 初始资金独立回测；直接乘以上一窗口终值，保留窗口首日
        # 的建仓滑点/佣金。旧实现强制首日 NAV 等于 prev，会抹掉每个窗口首期盈亏。
        seq = [d["nav"] * prev for d in window_daily]
        no_cost_seq = [d.get("noCostNav", d["nav"]) * prev_no_cost for d in window_daily]
        for d, v, no_cost_v in zip(window_daily, seq, no_cost_seq):
            # 落盘每日资金口径，便于事后核验仓位/资金利用率，无需重放逐笔成交。
            daily_rows.append({"tradeDate": d["tradeDate"], "nav": v,
                               "noCostNav": no_cost_v,
                               "windowStart": test_start,
                               "cash": round(float(d.get("cash", 0.0)), 2),
                               "marketValue": round(float(d.get("marketValue", 0.0)), 2),
                               "holdings": int(d.get("holdings", 0)),
                               "dayTurnover": round(float(d.get("turnover", 0.0)), 6),
                               "dayCost": round(float(d.get("cost", 0.0)), 2),
                               # 挂账卖出观测：曾漏映射导致每日总是 0，看起来像"从未触发"。
                               "pendingSellCount": int(d.get("pendingSellCount", 0)),
                               "deferredSellCount": int(d.get("deferredSellCount", 0)),
                               "pendingSellAtWindowEnd": int(d.get("pendingSellAtWindowEnd", 0))})
        # 每个窗口都从 NAV=1 独立开始。使用 last/first 会把首日建仓
        # 产生的佣金、滑点和当日损益从窗口收益中抹掉。
        w_ret = float(window_daily[-1]["nav"] - 1.0) if window_daily else 0.0
        window_stats.append({"testStart": test_start, "testEnd": test_end,
                             "windowReturn": round(w_ret, 4),
                             "transactionCostRate": round(
                                 float(window_daily[-1].get("cumulativeCost", 0.0)) /
                                 float(bt_cfg.get("initialCapital", 1_000_000)), 6),
                             "turnover": round(float(window_daily[-1].get("cumulativeTurnover", 0.0)), 6),
                             "rankIc": round(float(np.mean(window_ics)), 4) if window_ics else res["rankIc"],
                             "features": len(res["features"]), "ensembleSeeds": n_ensemble,
                             "treeCounts": window_tree_counts,
                             "meanTrees": round(window_mean_trees, 1),
                             "effectiveDevice": res.get("effectiveDevice", "cpu")})
        # Booster/CUDA allocations and large temporary lists must not accumulate
        # across the 24 rolling windows in the long-running API process.
        for booster in window_boosters:
            try:
                booster.free_dataset()
            except (AttributeError, TypeError):
                pass
        del model, window_boosters, window_res, res, train_frame, test_frame, window_daily, seq, no_cost_seq
        _release_native_memory()
        k += step_days
    progress(92)
    if not daily_rows:
        raise ValueError("滚动窗口不足，无法执行 walk-forward")
    navs = np.asarray([d["nav"] for d in daily_rows], dtype=float)
    no_cost_navs = np.asarray([d["noCostNav"] for d in daily_rows], dtype=float)
    span_start, span_end = daily_rows[0]["tradeDate"], daily_rows[-1]["tradeDate"]
    bench_slice = [bench[d] for d in sorted(bench) if span_start <= d <= span_end]
    bench_total = (bench_slice[-1] / bench_slice[0] - 1) if len(bench_slice) > 1 else 0.0
    total = float(navs[-1] - 1)
    no_cost_total = float(no_cost_navs[-1] - 1)
    rets = np.diff(np.concatenate(([1.0], navs))) / np.concatenate(([1.0], navs))[:-1]
    annual = (1 + total) ** (252.0 / len(navs)) - 1
    vol = float(np.std(rets, ddof=1) * math.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = annual / vol if vol > 0 else 0.0
    peak, mdd = -np.inf, 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    ic_vals = [w["rankIc"] for w in window_stats if w["rankIc"] is not None]
    positive_window_rate = (
        sum(float(w["windowReturn"]) > 0 for w in window_stats) / len(window_stats)
        if window_stats else 0.0)
    # 模型规模记录（中性提示，不做因果判断）。
    # 注意：早期版本这里写成"平均树数低于上限 20% = 模型欠训练、早停该放宽"的告警，
    # 该判断已被跨窗口扫描证伪 —— 实测 avgRankIC 随树数上限单调递减
    # （上限 10→0.0418、25→0.0362、50→0.0316、100→0.0283、200→0.0172），
    # 即本任务的浅模型泛化更好。故这里只如实报告规模，不再暗示"树多=好"。
    n_estimators_cap = int(model_cfg.get("nEstimators", 0) or 0)
    mean_trees = round(sum(all_tree_counts) / len(all_tree_counts), 1) if all_tree_counts else 0.0
    tree_notes: list[str] = []
    if n_estimators_cap and all_tree_counts:
        tree_notes.append(
            f"平均树数 {mean_trees} / 上限 {n_estimators_cap}（{mean_trees / n_estimators_cap * 100:.1f}%）；"
            "早停会按各种子验证最优点截断，故实际树数通常远低于上限，属预期行为")
    for note in tree_notes:
        print(f"[INFO] stock-ml 模型规模：{note}", flush=True)
    metrics = {"windows": len(window_stats), "totalReturn": round(float(total), 4),
               "benchmarkReturn": round(float(bench_total), 4), "excessReturn": round(float(total - bench_total), 4),
               "annualReturn": round(float(annual), 4), "sharpe": round(float(sharpe), 3),
               "maxDrawdown": round(float(mdd), 4),
               "noCostTotalReturn": round(no_cost_total, 4),
               "costReturnDrag": round(no_cost_total - total, 4),
               "avgRankIc": round(float(np.mean(ic_vals)), 4) if ic_vals else None,
               "positiveWindowRate": round(positive_window_rate, 4),
               "transactionCost": round(total_transaction_cost, 2),
               "transactionCostRate": round(total_transaction_cost / 1_000_000.0, 4),
               "annualTransactionCostRate": round(
                   total_transaction_cost / float(bt_cfg.get("initialCapital", 1_000_000)) *
                   252.0 / len(daily_rows), 4),
               "commissionCost": round(total_commission_cost, 2),
               "stampDutyCost": round(total_stamp_cost, 2),
               "slippageCost": round(total_slippage_cost, 2),
               "annualTurnover": round(total_turnover * 252.0 / len(daily_rows), 4),
               "stopTurnover": round(total_stop_turnover, 2),
               "rebalanceTurnover": round(total_rebalance_turnover, 2),
               "tradeCount": total_trades, "stopTradeCount": total_stop_trades,
               "meanTrees": mean_trees,
               "minTrees": min(all_tree_counts) if all_tree_counts else 0,
               "maxTrees": max(all_tree_counts) if all_tree_counts else 0,
               "nEstimatorsCap": n_estimators_cap,
               "treeNotes": tree_notes,
               "corporateActionAdjustments": total_corporate_action_adjustments}

    # 保存分窗口明细（收益集中度分析用）
    try:
        (artifact_dir / "stock-wf-window-stats.json").write_text(
            json.dumps({"runId": run_id, "ensembleSeeds": ensemble_seeds,
                        "windows": window_stats}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    try:
        trades_path = artifact_dir / "stock-wf-trades.jsonl"
        trades_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in trade_records),
            encoding="utf-8")
        (artifact_dir / "stock-wf-daily-nav.json").write_text(
            json.dumps({"runId": run_id, "daily": daily_rows}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    # 最新持仓：最新模型 → 最新特征日（不依赖标签），写回 selection_candidates
    holdings: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []
    publish_blockers: list[str] = _publish_gate_failures(metrics, evaluation_scope, publish_gate_cfg)
    publish_top_n = max(int(bt_cfg.get("topN", 5)),
                        int(wf_cfg.get("publishTopN", cfg.get("publishTopN", 20))))
    hold_date = span_end
    if latest_model_path:
        con2 = connect(factors_path)
        try:
            latest_feat = con2.execute(
                "SELECT MAX(trade_date) FROM stock_ml_factors WHERE momentum20 IS NOT NULL").fetchone()[0]
            if latest_feat:
                hold_date = latest_feat
        finally:
            con2.close()
        # 实盘预测不能在训练时有辅助特征、信号日却整列缺失。过期时允许完成
        # 研究回测和保存模型，但禁止覆盖正式候选池。
        aux_freshness = aux_factor_freshness(
            factors_path, hold_date, required_features=list(model_cfg.get("featuresOverride") or []))
        publish_blockers = _merge_publish_blockers(
            publish_blockers, list(aux_freshness["warnings"]))
        model = _load_ensemble_or_single(Path(latest_model_path))
        feat_cols = list(model.feature_name())
        con3 = connect(factors_path)
        try:
            # 动态判断特征列所属表，JOIN 扩展因子表（零破坏：不修改原表结构）
            base_set = set(FACTOR_COLUMNS)
            ind_set = set(INDUSTRY_FACTOR_COLUMNS)
            mf_set = set(MONEY_FLOW_FACTOR_COLUMNS)
            has_ind = any(f in ind_set for f in feat_cols)
            has_mf = any(f in mf_set for f in feat_cols)
            mf_alias = "c" if has_ind else "b"
            select_parts = ["a.trade_date", "a.code"]
            for f in feat_cols:
                if f in ind_set:
                    select_parts.append(f"b.{f} AS {f}")
                elif f in mf_set:
                    select_parts.append(f"{mf_alias}.{f} AS {f}")
                else:
                    select_parts.append(f"a.{f}")
            joins = []
            if has_ind:
                joins.append("LEFT JOIN stock_industry_factors b ON a.trade_date=b.trade_date AND a.code=b.code")
            if has_mf:
                joins.append(f"LEFT JOIN stock_money_flow_factors {mf_alias} ON a.trade_date={mf_alias}.trade_date AND a.code={mf_alias}.code")
            sql = "SELECT " + ",".join(select_parts) + " FROM stock_ml_factors a " + " ".join(joins) + " WHERE a.trade_date=? ORDER BY a.code"
            last_day = [dict(r) for r in con3.execute(sql, (hold_date,))]
        finally:
            con3.close()
        if last_day:
            # 股票池过滤：排除北交所/科创板 + 最小市值 + 趋势过滤（与回测同口径）
            exclude_prefixes = bt_cfg.get("excludeCodePrefix", ["920", "688"])
            min_mktcap = float(bt_cfg.get("minMktCap", 200_000))  # total_mv单位万元
            require_trend = bool(bt_cfg.get("requireMomentum20Positive", False))
            min_industry_rank = float(bt_cfg.get("minIndustryRank20", 0.0))
            filtered_last = [r for r in last_day
                             if not any(r["code"].startswith(p) for p in exclude_prefixes)
                             and (r.get("mktCap") is not None and r["mktCap"] >= min_mktcap)
                             and (not require_trend or (r.get("momentum20") is not None and r["momentum20"] > 0))
                             and (min_industry_rank <= 0 or (r.get("industry_rank20") is not None and r["industry_rank20"] >= min_industry_rank))]
            # 过滤后为空就发布空仓，不允许风险规则静默失效。
            scores = (np.asarray(model.predict(np.asarray(
                [[r.get(f) for f in feat_cols] for r in filtered_last], dtype=float)), dtype=float)
                if filtered_last else np.asarray([], dtype=float))
            top_n = int(bt_cfg.get("topN", 5))
            # 持仓条数 vs 发布条数：
            #   holdings   = 实际持仓（回测/模拟盘口径，TopN）
            #   published  = 写回 selection_candidates 的候选池。T+1 选股页与 V5 择时盘都按"候选池"取用
            #                （V5 要 Top20），原先两者都只写 top_n=5，导致"Top20 候选池"名不副实。
            publish_top_n = max(top_n, publish_top_n)
            order = np.argsort(-scores)
            for idx in order[:top_n]:
                holdings.append({"code": filtered_last[idx]["code"], "score": round(float(scores[idx]), 4),
                                 "weight": round(1.0 / top_n, 4)})
            published = _published_candidates(
                [{"code": filtered_last[idx]["code"], "score": round(float(scores[idx]), 4)} for idx in order],
                top_n, publish_top_n)
            # 写回 selection_candidates（幂等：先删同模型同日；dryRun 不写）
            if not dry_run and not publish_blockers:
                con4 = connect(factors_path)
                try:
                    _ensure_selection_columns(con4)
                    con4.execute("DELETE FROM selection_candidates WHERE model_id=? AND trade_date=?", (ML_MODEL_ID, hold_date))
                    for item in published:
                        con4.execute(
                            "INSERT INTO selection_candidates (trade_date,model_id,rank,code,score,reasons_json,computed_at,source_run_id,result_version) "
                            "VALUES (?,?,?,?,?,?,?,?,?)",
                            (hold_date, ML_MODEL_ID, item["rank"], item["code"], round(item["score"] * 100, 2),
                             json.dumps([{"factor": "ml", "label": "机器学习预测" + ("" if item["holding"] else "（候选）"),
                                          "score": item["score"],
                                          "contribution": item["weight"] if item["holding"] else 0.0}],
                                        ensure_ascii=False), _now(), run_id, 2))
                    con4.commit()
                finally:
                    con4.close()

    # 辅助因子新鲜度断言：行业/资金流因子曾经落后信号日 2 天而无人察觉（特征静默缺失）。
    # 这里只告警不阻塞（模型对 NaN 有处理），但会写进结果与 job 结果，任何人看 run 都能发现。
    aux_freshness = aux_factor_freshness(
        factors_path, hold_date, required_features=list(model_cfg.get("featuresOverride") or []))
    for warning in aux_freshness["warnings"]:
        print(f"[WARN] 辅助因子新鲜度：{warning}", flush=True)
    config_json = json.dumps({"label": label, "topN": bt_cfg.get("topN"), "model": model_cfg,
                              "ensembleSeeds": ensemble_seeds, "ensembleSize": n_ensemble,
                              "ensembleMethod": ensemble_method if n_ensemble > 1 else "single",
                              "minTrain": min_train, "validDays": valid_days,
                              "testDays": test_days, "stepDays": step_days,
                              "loadHistoryDays": load_history_days,
                              "evaluationScope": evaluation_scope,
                              "publishGate": publish_gate_cfg,
                              "publishTopN": publish_top_n, "embargoDays": wf_cfg.get("embargoDays", 0),
                              "rebalanceDays": bt_cfg.get("rebalanceDays", 1),
                              "executionMode": bt_cfg.get("executionMode", "same_day_close"),
                              "snapshotTime": bt_cfg.get("snapshotTime", "14:40"),
                              "snapshotSource": bt_cfg.get("snapshotSource", "close_proxy"),
                              "auxFactorLatest": aux_freshness["tables"],
                              "auxFactorWarnings": aux_freshness["warnings"],
                              "regimeFilter": bool(bt_cfg.get("regimeFilter", False)),
                              # 保留完整有效回测参数，确保每个 run 可复现。上面的顶层
                              # 字段为兼容旧前端保留，新消费方应优先读取 backtest。
                              "backtest": bt_cfg,
                              "effectiveDevices": sorted(effective_devices)}, ensure_ascii=False)
    holdings_json = json.dumps({"tradeDate": hold_date, "holdings": holdings}, ensure_ascii=False)
    # 每个run固化自己的模型和集成清单，历史版本不再依赖会被下次训练覆盖的latest文件。
    run_model_path: Path | None = None
    model_sha256: str | None = None
    if latest_model_path:
        import shutil
        latest_dir = Path(latest_model_path).parent
        run_dir = artifact_dir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for source in latest_dir.glob("stock-wf-model*.txt"):
            target = run_dir / source.name
            shutil.copy2(source, target)
            copied.append(target)
        source_manifest = latest_dir / "stock-wf-model_ensemble_seeds.json"
        if source_manifest.exists():
            manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
            manifest["modelFiles"] = [str(run_dir / Path(p).name) for p in manifest.get("modelFiles", [])]
            (run_dir / source_manifest.name).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        primary = run_dir / "stock-wf-model.txt"
        run_model_path = primary if primary.exists() else (copied[0] if copied else None)
        if run_model_path:
            digest = hashlib.sha256()
            for item in sorted(run_dir.glob("stock-wf-model*"), key=lambda p: p.name):
                digest.update(item.name.encode("utf-8")); digest.update(item.read_bytes())
            model_sha256 = digest.hexdigest()
        # 归因产物必须与模型一样按 run_id 固化，否则下一次运行会覆盖
        # 根目录中的 latest 文件，历史结果将无法精确复盘。
        for artifact_name in (
            "stock-wf-window-stats.json", "stock-wf-trades.jsonl", "stock-wf-daily-nav.json"
        ):
            source = artifact_dir / artifact_name
            if source.exists():
                shutil.copy2(source, run_dir / artifact_name)
        (run_dir / "run-metadata.json").write_text(json.dumps({
            "runId": run_id, "generatedAt": _now(), "config": json.loads(config_json),
            "engineVersion": engine_version,
            "metrics": metrics, "modelSha256": model_sha256,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    if not dry_run:
        con5 = connect(factors_path)
        try:
            _ensure_walkforward_columns(con5)
            signal_published = bool(published) and not publish_blockers
            if signal_published:
                con5.execute("UPDATE stock_walkforward_runs SET is_published=0")
            con5.execute(
                "INSERT OR REPLACE INTO stock_walkforward_runs(run_id,generated_at,label,start_date,end_date,windows,config,metrics,holdings,status,result_version,model_path,model_sha256,is_published) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, _now(), label, span_start, span_end, len(window_stats),
                 config_json, json.dumps(metrics, ensure_ascii=False), holdings_json, "complete", 2,
                 str(run_model_path) if run_model_path else None, model_sha256,
                 1 if signal_published else 0))
            con5.commit()
        finally:
            con5.close()
    progress(100)
    return {"runId": run_id, "span": f"{span_start}..{span_end}", "metrics": metrics,
            "holdings": {"tradeDate": hold_date, "count": len(holdings)},
            "publishedCandidates": len(published) if not dry_run and not publish_blockers else 0,
            "publishTopN": publish_top_n,
            "evaluationScope": evaluation_scope,
            "publishBlocked": bool(publish_blockers), "publishBlockers": publish_blockers,
            "auxFactorLatest": aux_freshness["tables"], "auxFactorWarnings": aux_freshness["warnings"],
            "signalModel": ML_MODEL_ID, "dryRun": dry_run, "configWarnings": config_warnings,
            "treeNotes": tree_notes,
            "engineVersion": engine_version,
            # 生效参数回显：A/B 实验必须能一眼确认开关真的生效（曾因浅合并导致实验静默失效）。
            "effectiveBacktest": {"topN": bt_cfg.get("topN"),
                                  "maxPositionWeight": bt_cfg.get("maxPositionWeight"),
                                  "rebalanceDays": bt_cfg.get("rebalanceDays"),
                                  "initialStopLoss": bt_cfg.get("initialStopLoss"),
                                  "trailingDrawdown": bt_cfg.get("trailingDrawdown"),
                                  "technicalTiming": bt_cfg.get("technicalTiming")}}


def latest_walkforward(factors_path: Path) -> dict[str, Any]:
    con = connect(factors_path)
    try:
        _ensure_walkforward_columns(con)
        # 优先返回已发布的版本，没有则返回最新版本
        row = con.execute(
            "SELECT run_id,generated_at,label,metrics,holdings,is_published,result_version,model_path,model_sha256 "
            "FROM stock_walkforward_runs ORDER BY result_version DESC,is_published DESC,generated_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if not row:
        return {"run": None}
    return {"run": {"runId": row["run_id"], "generatedAt": row["generated_at"], "label": row["label"],
                    "metrics": json.loads(row["metrics"]), "holdings": json.loads(row["holdings"]),
                    "isPublished": bool(row["is_published"]), "resultVersion": row["result_version"],
                    "legacy": int(row["result_version"]) < 2, "modelPath": row["model_path"],
                    "modelSha256": row["model_sha256"]}}


def list_walkforward(factors_path: Path, limit: int = 50) -> dict[str, Any]:
    """返回所有历史walkforward训练记录（含config和metrics），按时间倒序。用于训练版本对比页面。"""
    con = connect(factors_path)
    try:
        _ensure_walkforward_columns(con)
        rows = con.execute(
            "SELECT run_id,generated_at,label,start_date,end_date,windows,config,metrics,status,is_published,notes,"
            "result_version,model_path,model_sha256 "
            "FROM stock_walkforward_runs ORDER BY generated_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        con.close()
    items = []
    for row in rows:
        cfg = {}
        try:
            cfg = json.loads(row["config"]) if row["config"] else {}
        except Exception:
            pass
        metrics = {}
        try:
            metrics = json.loads(row["metrics"]) if row["metrics"] else {}
        except Exception:
            pass
        # 从config提取关键模型参数，方便前端展示。新run保留完整
        # backtest 子对象，旧run仍从顶层字段兼容读取。
        model_cfg = cfg.get("model") or {}
        saved_bt_cfg = cfg.get("backtest") or {}
        seeds = model_cfg.get("randomStates") or cfg.get("ensembleSeeds") or []
        n_seeds = len(seeds) if seeds else (cfg.get("ensembleSize") or 1)
        ensemble_method = model_cfg.get("ensembleMethod") or cfg.get("ensembleMethod") or ("single" if n_seeds <= 1 else "mean_zscore")
        features = model_cfg.get("featuresOverride") or cfg.get("features") or []
        items.append({
            "runId": row["run_id"],
            "generatedAt": row["generated_at"],
            "label": row["label"],
            "startDate": row["start_date"],
            "endDate": row["end_date"],
            "windows": row["windows"],
            "status": row["status"],
            "isPublished": bool(row["is_published"]) if "is_published" in row.keys() else False,
            "notes": row["notes"] if "notes" in row.keys() else "",
            "resultVersion": row["result_version"],
            "legacy": int(row["result_version"]) < 2,
            "modelPath": row["model_path"],
            "modelSha256": row["model_sha256"],
            "model": {
                "nSeeds": n_seeds,
                "seeds": seeds,
                "ensembleMethod": ensemble_method,
                "nEstimators": model_cfg.get("nEstimators"),
                "learningRate": model_cfg.get("learningRate"),
                "numLeaves": model_cfg.get("numLeaves"),
                "subsample": model_cfg.get("subsample"),
                "colsampleBytree": model_cfg.get("colsampleBytree"),
                "randomState": model_cfg.get("randomState"),
            },
            "walkforward": {
                "minTrain": cfg.get("minTrain") or cfg.get("minTrainDays"),
                "stepDays": cfg.get("stepDays"),
                "testDays": cfg.get("testDays") or model_cfg.get("testDays"),
                "evaluationScope": cfg.get("evaluationScope", "legacy_unspecified"),
            },
            "backtest": {
                "topN": saved_bt_cfg.get("topN", cfg.get("topN")),
                "rebalanceDays": saved_bt_cfg.get("rebalanceDays", cfg.get("rebalanceDays")),
                "holdingBufferRank": saved_bt_cfg.get("holdingBufferRank"),
                "initialStopLoss": saved_bt_cfg.get("initialStopLoss"),
                "trailingDrawdown": saved_bt_cfg.get("trailingDrawdown"),
                "commissionRate": saved_bt_cfg.get("commissionRate"),
                "stampDutyRate": saved_bt_cfg.get("stampDutyRate"),
                "slippageRate": saved_bt_cfg.get("slippageRate"),
            },
            "featureCount": cfg.get("featureCount") or (len(features) if features else None),
            "features": features,
            "metrics": metrics,
        })
    return {"count": len(items), "items": items}


def publish_walkforward(factors_path: Path, run_id: str) -> dict[str, Any]:
    """发布指定walkforward版本：将该run_id设为is_published=1，其他设为0。
    发布后的版本会被latest_walkforward优先返回，作为当前正式使用的模型。"""
    con = connect(factors_path)
    try:
        _ensure_walkforward_columns(con)
        # 检查run_id是否存在
        row = con.execute("SELECT run_id,label,result_version,holdings,config,metrics FROM stock_walkforward_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"run_id不存在: {run_id}"}
        if int(row["result_version"]) < 2:
            return {"ok": False, "error": "旧执行口径结果已标记为legacy，不能重新发布；请先按新口径重跑"}
        holding_date = (json.loads(row["holdings"] or "{}").get("tradeDate"))
        run_config = json.loads(row["config"] or "{}")
        gate_failures = _publish_gate_failures(
            json.loads(row["metrics"] or "{}"), run_config.get("evaluationScope", "legacy_unspecified"),
            dict(run_config.get("publishGate") or {}))
        if gate_failures:
            return {"ok": False, "error": "发布门槛未通过", "failures": gate_failures}
        required_features = list((run_config.get("model") or {}).get("featuresOverride") or [])
        freshness = aux_factor_freshness(
            factors_path, holding_date, required_features=required_features)
        if freshness["warnings"]:
            return {"ok": False, "error": "辅助因子过期，禁止发布", "warnings": freshness["warnings"]}
        candidate_count = con.execute(
            "SELECT COUNT(*) FROM selection_candidates WHERE model_id=? AND source_run_id=? AND result_version>=2",
            (ML_MODEL_ID, run_id)).fetchone()[0]
        if not candidate_count:
            return {"ok": False, "error": "该版本没有同源候选股，禁止只切换指标而不切换信号"}
        # 先将所有版本设为未发布
        con.execute("UPDATE stock_walkforward_runs SET is_published=0")
        # 将指定版本设为已发布
        con.execute("UPDATE stock_walkforward_runs SET is_published=1 WHERE run_id=?", (run_id,))
        con.commit()
        return {"ok": True, "runId": run_id, "label": row["label"], "message": f"版本已发布: {row['label']}"}
    finally:
        con.close()


def delete_walkforward(factors_path: Path, run_id: str) -> dict[str, Any]:
    """删除指定walkforward版本（已发布的版本不允许删除，需先发布其他版本）。"""
    con = connect(factors_path)
    try:
        _ensure_walkforward_columns(con)
        # 检查run_id是否存在
        row = con.execute("SELECT run_id, label, is_published FROM stock_walkforward_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"run_id不存在: {run_id}"}
        # 已发布的版本不允许删除
        if row["is_published"]:
            return {"ok": False, "error": "已发布的版本不能删除，请先发布其他版本"}
        # 删除记录
        con.execute("DELETE FROM stock_walkforward_runs WHERE run_id=?", (run_id,))
        con.commit()
        return {"ok": True, "runId": run_id, "label": row["label"], "message": f"版本已删除: {row['label']}"}
    finally:
        con.close()


def update_walkforward_notes(factors_path: Path, run_id: str, notes: str) -> dict[str, Any]:
    """更新指定walkforward版本的备注/介绍。"""
    con = connect(factors_path)
    try:
        _ensure_walkforward_columns(con)
        # 检查run_id是否存在
        row = con.execute("SELECT run_id, label FROM stock_walkforward_runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"run_id不存在: {run_id}"}
        # 更新备注
        con.execute("UPDATE stock_walkforward_runs SET notes=? WHERE run_id=?", (notes or "", run_id))
        con.commit()
        return {"ok": True, "runId": run_id, "label": row["label"], "notes": notes, "message": "备注已更新"}
    finally:
        con.close()


def latest_candidates(factors_path: Path, model_id: str = ML_MODEL_ID, limit: int = 50) -> dict[str, Any]:
    """查询ML模型最新选股候选（从selection_candidates表）"""
    con = connect(factors_path)
    try:
        _ensure_selection_columns(con)
        # 先找最新日期
        date_row = con.execute(
            "SELECT MAX(c.trade_date) d FROM selection_candidates c "
            "JOIN stock_walkforward_runs r ON r.run_id=c.source_run_id "
            "WHERE c.model_id=? AND c.result_version>=2 AND r.is_published=1", (model_id,)
        ).fetchone()
        if not date_row or not date_row["d"]:
            return {"modelId": model_id, "tradeDate": None, "count": 0, "items": []}
        trade_date = date_row["d"]
        rows = con.execute(
            "SELECT c.trade_date, c.model_id, c.rank, c.code, c.score, c.reasons_json, c.computed_at "
            "FROM selection_candidates c JOIN stock_walkforward_runs r ON r.run_id=c.source_run_id "
            "WHERE c.model_id=? AND c.trade_date=? AND c.result_version>=2 AND r.is_published=1 "
            "ORDER BY c.rank LIMIT ?",
            (model_id, trade_date, limit)
        ).fetchall()
    finally:
        con.close()
    items = []
    for r in rows:
        reasons = []
        try:
            reasons = json.loads(r["reasons_json"]) if r["reasons_json"] else []
        except Exception:
            pass
        items.append({
            "tradeDate": r["trade_date"],
            "rank": r["rank"],
            "code": r["code"],
            "score": r["score"],
            "reasons": reasons,
            "computedAt": r["computed_at"],
        })
    return {"modelId": model_id, "tradeDate": trade_date, "count": len(items), "items": items}


def get_model_config(config_path: Path) -> dict[str, Any]:
    """返回模型配置（特征列表、训练参数、随机种子、walkforward配置等）"""
    cfg = load_config(config_path)
    features = cfg.get("featuresOverride") or []
    model_cfg = cfg.get("model") or {}
    wf_cfg = cfg.get("walkforward") or {}
    bt_cfg = cfg.get("backtest") or {}
    return {
        "label": cfg.get("label"),
        "features": features,
        "featureCount": len(features),
        "model": {
            # deviceType / cudaInProcess 必须回显：它们是同步时最容易被覆盖、
            # 且后果最严重的两个键（显存与内存路径完全不同），此前 API 返回 None，
            # 只能靠 grep yaml 核对，出过不一致。
            "deviceType": model_cfg.get("deviceType"),
            "cudaInProcess": bool(model_cfg.get("cudaInProcess", False)),
            "cpuFallback": model_cfg.get("cpuFallback"),
            "nEstimators": model_cfg.get("nEstimators"),
            "learningRate": model_cfg.get("learningRate"),
            "numLeaves": model_cfg.get("numLeaves"),
            "minDataInLeaf": model_cfg.get("minDataInLeaf"),
            "subsample": model_cfg.get("subsample"),
            "colsampleBytree": model_cfg.get("colsampleBytree"),
            "randomState": model_cfg.get("randomState"),
            "randomStates": model_cfg.get("randomStates"),
            "ensembleMethod": model_cfg.get("ensembleMethod") or (model_cfg.get("randomStates") and len(model_cfg["randomStates"]) > 1 and "mean_zscore" or "single"),
            "earlyStopping": model_cfg.get("earlyStopping"),
            "validationDays": model_cfg.get("validationDays"),
            "testDays": model_cfg.get("testDays"),
            "labelGrades": model_cfg.get("labelGrades"),
        },
        "walkforward": {
            "minTrainDays": wf_cfg.get("minTrain", wf_cfg.get("minTrainDays", 200)),
            "stepDays": wf_cfg.get("stepDays"),
            "testDays": wf_cfg.get("testDays"),
            "embargoDays": wf_cfg.get("embargoDays"),
            # 写入 selection_candidates 的候选池大小（实际持仓仍是 backtest.topN）
            "publishTopN": wf_cfg.get("publishTopN", cfg.get("publishTopN", 20)),
        },
        "backtest": {
            "topN": bt_cfg.get("topN"),
            "rebalanceDays": bt_cfg.get("rebalanceDays"),
            "holdingBufferRank": bt_cfg.get("holdingBufferRank"),
            "maxPositionWeight": bt_cfg.get("maxPositionWeight"),
            "initialStopLoss": bt_cfg.get("initialStopLoss"),
            "trailingDrawdown": bt_cfg.get("trailingDrawdown"),
            "executionMode": bt_cfg.get("executionMode", "same_day_close"),
            "snapshotTime": bt_cfg.get("snapshotTime", "14:40"),
            "snapshotSource": bt_cfg.get("snapshotSource", "close_proxy"),
            "technicalTiming": bt_cfg.get("technicalTiming") or {"enabled": False},
            # 只回报实际生效的费率（原实现回报的是代码不读取的 transactionCostBps/slippageBps，
            # 让「成本 15bp」这类未生效的配置看起来像生效了）
            "commissionRate": float(bt_cfg.get("commissionRate", 0.00008)),
            "stampDutyRate": float(bt_cfg.get("stampDutyRate", 0.0005)),
            "slippageRate": float(bt_cfg.get("slippageRate", 0.004)),
            "commissionBps": round(10000 * float(bt_cfg.get("commissionRate", 0.00008)), 2),
            "stampDutyBps": round(10000 * float(bt_cfg.get("stampDutyRate", 0.0005)), 2),
            "slippageBps": round(10000 * float(bt_cfg.get("slippageRate", 0.004)), 2),
        },
        # 股票池过滤实际发生在代码内（build_factors 与回测函数），这里回报有效值
        "universe": {
            "source": "代码内固定：excludeCodePrefix=%s、minMktCap=%.0f 万元" % (
                bt_cfg.get("excludeCodePrefix", ["920", "688"]), float(bt_cfg.get("minMktCap", 200_000))),
            "minMktCap": float(bt_cfg.get("minMktCap", 200_000)),
            "excludeCodePrefix": bt_cfg.get("excludeCodePrefix", ["920", "688"]),
        },
        "publishGate": cfg.get("publishGate") or {},
        "configWarnings": _config_warnings(cfg),
    }


def audit_label_price_basis(factors_path: Path, market_path: Path, label: str = "forward_5",
                            code_glob: str = "*", threshold: float = 0.005) -> dict[str, Any]:
    """口径审计：对比「未复权 close 标签」与「复权价标签」的差异（只读，不改任何数据）。

    校验 stock_ml_factors 的 forward_* 是否与 close*adj_factor 的复权收益一致。
    """
    label = str(label)
    if label not in LABEL_COLUMNS:
        raise ValueError(f"label 必须是服务产出的标签列之一：{LABEL_COLUMNS}")
    horizon = int(label.rsplit("_", 1)[1])
    # 注意：ATTACH 里的 file: URI 只有在连接本身 uri=True 时才被当作 URI 解析，
    # 否则 SQLite 会把整串当字面路径并报 "unable to open database"（Windows 上尤其明显）。
    con = sqlite3.connect(str(factors_path), uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("ATTACH DATABASE ? AS market", (Path(market_path).resolve().as_uri() + "?mode=ro",))
        # label/horizon 已被 LABEL_COLUMNS 白名单约束，code_glob 与 threshold 走参数绑定
        sql = f"""
        WITH prices AS (
          SELECT b.code,b.trade_date,b.close * COALESCE(
            (SELECT a.adj_factor FROM market.adjustment_factors a
             WHERE a.code=b.code AND a.trade_date<=b.trade_date ORDER BY a.trade_date DESC LIMIT 1), 1.0) adj_close
          FROM market.daily_bars b WHERE b.code GLOB ?
        ), w AS (
          SELECT code,trade_date,adj_close,
                 LEAD(adj_close, {horizon}) OVER (PARTITION BY code ORDER BY trade_date) AS adj_future
          FROM prices
        )
        SELECT COUNT(*) AS compared,
               SUM(ABS(f.{label} - (w.adj_future / w.adj_close - 1)) > ?) AS over_threshold,
               ROUND(AVG(ABS(f.{label} - (w.adj_future / w.adj_close - 1))), 6) AS mean_abs_diff,
               ROUND(MAX(ABS(f.{label} - (w.adj_future / w.adj_close - 1))), 6) AS max_abs_diff
        FROM stock_ml_factors f JOIN w ON w.code=f.code AND w.trade_date=f.trade_date
        WHERE f.{label} IS NOT NULL AND w.adj_future IS NOT NULL AND w.adj_close > 0
        """
        row = con.execute(sql, (code_glob, threshold)).fetchone()
    finally:
        con.close()
    compared = int(row["compared"] or 0)
    over = int(row["over_threshold"] or 0)
    max_diff = float(row["max_abs_diff"] or 0.0)
    return {
        "label": label, "horizonDays": horizon, "codeGlob": code_glob, "threshold": threshold,
        "comparedRows": compared, "rowsOverThreshold": over,
        "shareOverThreshold": round(over / compared, 6) if compared else None,
        "meanAbsDiff": float(row["mean_abs_diff"] or 0.0), "maxAbsDiff": max_diff,
        # 极端值通常指向复权因子异常或长时间停牌后的数据问题，需要人工看一眼
        "abnormalMaxDiff": bool(max_diff > 0.5),
        "note": "正式标签口径为 close*adj_factor；本审计用于验证重建结果且不修改数据。",
    }


def _published_candidates(ranked: list[dict[str, Any]], top_n: int, publish_top_n: int) -> list[dict[str, Any]]:
    """把"按分数降序"的候选截成发布列表：前 top_n 条标记为实际持仓，其余仅作候选。

    selection_candidates 是候选池（T+1 选股页、V5 择时盘按 Top20 取用），holdings 才是实际持仓。
    """
    limit = min(max(top_n, publish_top_n), len(ranked))
    out: list[dict[str, Any]] = []
    for rank, row in enumerate(ranked[:limit], start=1):
        out.append({"rank": rank, "code": row["code"], "score": row["score"],
                    "holding": rank <= top_n, "weight": round(1.0 / top_n, 4)})
    return out


def get_feature_importance(model_path: Path, top_n: int = 50) -> dict[str, Any]:
    """从LightGBM模型文件提取特征重要性（gain和split两种口径）；多种子集成时取各模型均值。"""
    try:
        import lightgbm as lgb
    except ImportError:
        return {"error": "lightgbm not installed", "items": []}
    if not model_path.exists():
        return {"error": f"model file not found: {model_path}", "items": []}
    try:
        model = _load_ensemble_or_single(Path(model_path))
    except Exception as e:
        return {"error": f"failed to load model: {e}", "items": []}
    is_ensemble = isinstance(model, EnsembleRanker)
    names = model.feature_name()
    if is_ensemble:
        # 集成：gain 取各模型归一化后的均值（EnsembleRanker 已归一化，和为1），split 取均值
        gain_norm = model.feature_importance("gain")  # 归一化均值，和≈1
        gain = gain_norm * 100.0  # 转成百分比口径
        split_stack = np.stack([
            np.asarray(b.feature_importance(importance_type="split"), dtype=float)
            for b in model.boosters], axis=0)
        split = np.mean(split_stack, axis=0)
        total_gain = 100.0
    else:
        gain = model.feature_importance(importance_type="gain")
        split = model.feature_importance(importance_type="split")
        total_gain = float(sum(gain)) if gain.sum() > 0 else 1.0
    total_split = float(sum(split)) if sum(split) > 0 else 1.0
    items = []
    for i, name in enumerate(names):
        items.append({
            "feature": name,
            "gain": float(gain[i]),
            "gainPct": round(float(gain[i]) / total_gain * 100, 4),
            "split": float(split[i]),
            "splitPct": round(float(split[i]) / total_split * 100, 4),
        })
    items.sort(key=lambda x: x["gain"], reverse=True)
    return {
        "featureCount": len(names),
        "modelFile": str(model_path),
        "ensemble": is_ensemble,
        "ensembleSize": model.n_models if is_ensemble else 1,
        "seeds": model.seeds if is_ensemble else None,
        "items": items[:top_n],
    }
