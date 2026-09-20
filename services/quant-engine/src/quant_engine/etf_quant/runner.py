"""ETF Quant MVP orchestration: factors -> research -> train -> backtest.

Additive module; never touches the stock pipeline or its tables.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from ..stock_ml.config_integrity import stamp_run_config
from . import backtest as bt
from . import db as etf_db
from . import factors as ef
from . import ranker
from . import research as rs


def load_config(config_path: Path) -> dict[str, Any]:
    import yaml  # noqa: PLC0415
    return yaml.safe_load(config_path.read_text(encoding="utf-8"))


def run_factors(market_path: Path, etf_db_path: Path, cfg: dict[str, Any],
                progress: Callable[[float], None] = lambda p: None,
                cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    pool_cfg = cfg.get("pool", {})
    codes = ef.eligible_pool(market_path, pool_cfg)
    if not codes:
        raise ValueError("无可交易的 ETF 池，请先同步 ETF 池")
    frame = ef.compute_factors(market_path, codes, cfg.get("benchmark", "000300.SH"), pool_cfg)
    if frame.empty:
        raise ValueError("无 ETF 日线数据可用于因子计算")
    # 取消/超时检查点：因子计算完成后先看一次，避免超时后继续写快照
    if cancelled():
        return {"cancelled": True, "pool": len(codes)}
    con = etf_db.connect(etf_db_path)
    try:
        written = etf_db.write_factor_snapshots(con, frame)
    finally:
        con.close()
    progress(100)
    return {"pool": len(codes), "rows": written, "startDate": frame["trade_date"].min(),
            "endDate": frame["trade_date"].max(), "factors": ef.FACTOR_NAMES}


def run_research(etf_db_path: Path, cfg: dict[str, Any],
                 progress: Callable[[float], None] = lambda p: None) -> dict[str, Any]:
    research_cfg = cfg.get("research", {})
    label = research_cfg.get("label", "forward_20")
    con = etf_db.connect(etf_db_path)
    try:
        frame = etf_db.load_factor_frame(con, label, int(research_cfg.get("minDays", 40)))
    finally:
        con.close()
    result = rs.run_research(frame, label, int(research_cfg.get("minDays", 40)))
    con = etf_db.connect(etf_db_path)
    try:
        from .common import now_iso  # noqa: PLC0415
        run_id = f"etf-ic-{now_iso()}"
        con.execute("INSERT OR REPLACE INTO etf_ic_research VALUES(?,?,?,?,?,?,?)",
                    (run_id, now_iso(), label, result["windowStart"], result["windowEnd"],
                     result["days"], json.dumps(result["results"], ensure_ascii=False)))
        con.commit()
    finally:
        con.close()
    result["runId"] = run_id
    return result


def run_train(etf_db_path: Path, artifact_dir: Path, cfg: dict[str, Any],
              progress: Callable[[float], None] = lambda p: None,
              cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    research_cfg = cfg.get("research", {})
    label = research_cfg.get("label", "forward_20")
    con = etf_db.connect(etf_db_path)
    try:
        frame = etf_db.load_factor_frame(con, label, int(research_cfg.get("minDays", 40)))
    finally:
        con.close()
    if not frame:
        raise ValueError("无因子快照，请先运行 factors")
    result = ranker.train_ranker(frame, label, cfg.get("model", {}), artifact_dir,
                                 progress=lambda p: progress(10 + 70 * p / 100),
                                 cancelled=cancelled)
    from .common import now_iso  # noqa: PLC0415
    run_id = f"etf-model-{now_iso()}"
    # 训练族的生效配置：原先 params 只存 cfg["model"]，把 research(labels/minDays)
    # 丢了——事后无法回答"这个模型是在哪套标签口径上训的"。这里存完整片段。
    effective_cfg = {
        "kind": "etf_train",
        "label": label,
        "research": dict(research_cfg),
        "model": dict(cfg.get("model", {})),
        "pool": dict(cfg.get("pool", {})),
    }
    con = etf_db.connect(etf_db_path)
    try:
        columns: dict[str, Any] = {}
        # params 已经是完整配置，故不再重复存一份 config_json，只补指纹。
        stamp_run_config(con, columns, effective_cfg, run_id=run_id,
                         generated_at=now_iso(), include_config_json=False)
        con.execute("""INSERT OR REPLACE INTO etf_model_runs(run_id,generated_at,label,train_start,train_end,
                     valid_start,valid_end,test_start,test_end,rank_ic,ic_mean,icir,model_path,params,status,
                     config_sha256,config_yaml_sha256,git_commit,git_dirty)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, now_iso(), label, result["trainStart"], result["trainEnd"],
                     result["validStart"], result["validEnd"], result["testStart"], result["testEnd"],
                     result["rankIc"], result["ic"], result["rankIc"], result["modelPath"],
                     json.dumps(effective_cfg, ensure_ascii=False), "complete",
                     columns["config_sha256"], columns["config_yaml_sha256"],
                     columns["git_commit"], columns["git_dirty"]))
        con.commit()
    finally:
        con.close()
    result["runId"] = run_id
    return result


def run_backtest(etf_db_path: Path, market_path: Path, cfg: dict[str, Any],
                 progress: Callable[[float], None] = lambda p: None,
                 parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    from .common import now_iso  # noqa: PLC0415
    research_cfg = cfg.get("research", {})
    label = research_cfg.get("label", "forward_20")
    model_cfg = cfg.get("model", {})
    bt_cfg = dict(cfg.get("backtest", {}))
    overrides = (parameters or {}).get("backtest")
    if isinstance(overrides, dict):
        bt_cfg.update(overrides)
    con = etf_db.connect(etf_db_path)
    try:
        frame = etf_db.load_factor_frame(con, label, int(research_cfg.get("minDays", 40)))
    finally:
        con.close()
    if not frame:
        raise ValueError("无因子快照，请先运行 factors")
    score_mode = str(bt_cfg.get("scoreMode", "model"))
    model = None
    con2 = etf_db.connect(etf_db_path)
    try:
        run_row = con2.execute(
            "SELECT model_path,test_start,test_end FROM etf_model_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
    finally:
        con2.close()
    if not score_mode.startswith("factor:"):
        if not run_row or not run_row["model_path"]:
            raise ValueError("无已训练模型，请先运行 train")
        import lightgbm as lgb  # noqa: PLC0415
        model = lgb.Booster(model_file=run_row["model_path"])
    # both model and factor variants use the same out-of-sample test window
    start_date = run_row["test_start"] if run_row else None
    end_date = run_row["test_end"] if run_row else None

    codes = sorted({r["ts_code"] for r in frame})
    bars = ef._load_bars(market_path, codes)
    bench = ef._load_benchmark(market_path, cfg.get("benchmark", "000300.SH"))
    result = bt.run_backtest(frame, model, bars, bench, bt_cfg,
                             start_date=start_date, end_date=end_date,
                             progress=progress)
    run_id = f"etf-backtest-{now_iso()}"
    con = etf_db.connect(etf_db_path)
    try:
        con.execute("""INSERT OR REPLACE INTO etf_backtest_runs(run_id,generated_at,label,start_date,end_date,
                     top_n,rebalance_days,cost_bps,total_return,benchmark_return,excess_return,annual_return,
                     annual_vol,sharpe,max_drawdown,turnover_rate,trades,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, now_iso(), label, result["startDate"], result["endDate"], bt_cfg.get("topN"),
                     bt_cfg.get("rebalanceDays"), bt_cfg.get("costBps"), result["totalReturn"],
                     result["benchmarkReturn"], result["excessReturn"], result["annualReturn"],
                     result["annualVol"], result["sharpe"], result["maxDrawdown"],
                     result["turnoverRate"], result["trades"], "complete"))
        con.execute("BEGIN")
        for d in result["daily"]:
            con.execute("INSERT OR REPLACE INTO etf_backtest_daily VALUES(?,?,?,?)",
                        (run_id, d["tradeDate"], d["nav"], d["benchmark"]))
        for t in result["tradesDetail"]:
            con.execute("INSERT OR REPLACE INTO etf_backtest_trades VALUES(?,?,?,?,?,?,?,?,?)",
                        (run_id, t.get("rebalanceDate"), t.get("tsCode"), t.get("weight"),
                         t.get("entryPrice"), t.get("exitDate"), t.get("exitPrice"),
                         t.get("ret"), t.get("cost")))
        con.commit()
    finally:
        con.close()
    result.pop("daily", None)
    result.pop("tradesDetail", None)
    result["runId"] = run_id
    return result


def latest_research(etf_db_path: Path) -> dict[str, Any]:
    con = etf_db.connect(etf_db_path)
    try:
        row = con.execute("SELECT * FROM etf_ic_research ORDER BY generated_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "none"}
        out = dict(row)
        out["results"] = json.loads(out["results"])
        return out
    finally:
        con.close()


def latest_train(etf_db_path: Path, limit: int = 20) -> dict[str, Any]:
    con = etf_db.connect(etf_db_path)
    try:
        row = con.execute(
            "SELECT run_id runId, generated_at generatedAt, label, train_start trainStart, train_end trainEnd, "
            "valid_start validStart, valid_end validEnd, test_start testStart, test_end testEnd, "
            "rank_ic rankIc, ic_mean icMean, icir, model_path modelPath, params, status "
            "FROM etf_model_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "none", "history": []}
        out = dict(row)
        out["params"] = json.loads(out["params"]) if out["params"] else {}
        out["history"] = [dict(r) for r in con.execute(
            "SELECT run_id runId, generated_at generatedAt, label, train_start trainStart, train_end trainEnd, "
            "valid_start validStart, valid_end validEnd, test_start testStart, test_end testEnd, "
            "rank_ic rankIc, ic_mean icMean, icir, status FROM etf_model_runs ORDER BY generated_at DESC LIMIT ?",
            (limit,))]
        return out
    finally:
        con.close()


def latest_backtest(etf_db_path: Path) -> dict[str, Any]:
    con = etf_db.connect(etf_db_path)
    try:
        row = con.execute("SELECT * FROM etf_backtest_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "none"}
        out = dict(row)
        out["daily"] = [dict(r) for r in con.execute(
            "SELECT trade_date tradeDate, nav, benchmark FROM etf_backtest_daily WHERE run_id=? ORDER BY trade_date",
            (row["run_id"],))]
        out["trades"] = [dict(r) for r in con.execute(
            "SELECT rebalance_date rebalanceDate, ts_code tsCode, weight, entry_price entryPrice, "
            "exit_date exitDate, exit_price exitPrice, ret, cost FROM etf_backtest_trades WHERE run_id=?",
            (row["run_id"],))]
        return out
    finally:
        con.close()


def run_walkforward(etf_db_path: Path, market_path: Path, artifact_dir: Path, cfg: dict[str, Any],
                    progress: Callable[[float], None] = lambda p: None,
                    cancelled: Callable[[], bool] = lambda: False,
                    parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Rolling walk-forward: retrain + out-of-sample backtest every N days, stitch
    equity curves, then emit current holdings from the latest window model.
    Additive job; reuses train_ranker / run_backtest building blocks."""
    import tempfile  # noqa: PLC0415
    from pathlib import Path as _P  # noqa: PLC0415

    import lightgbm as lgb  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    from .common import now_iso  # noqa: PLC0415

    research_cfg = cfg.get("research", {})
    label = research_cfg.get("label", "forward_20")
    wf_cfg = dict(cfg.get("walkforward") or {})          # 配置文件里的 walkforward 段（此前被忽略）
    wf_cfg.update((parameters or {}).get("walkforward") or {})  # job 参数优先
    model_cfg = dict(cfg.get("model", {}))
    model_cfg.update(wf_cfg.get("model") or {})
    # purge/embargo：未配置时默认 0（保持既有数值）
    if wf_cfg.get("embargoDays") is not None:
        model_cfg["embargoDays"] = int(wf_cfg["embargoDays"])
    bt_cfg = dict(cfg.get("backtest", {}))
    overrides = (parameters or {}).get("backtest")
    if isinstance(overrides, dict):
        bt_cfg.update(overrides)
    bt_cfg.update(wf_cfg.get("backtest") or {})
    min_train = int(wf_cfg.get("minTrain", 200))
    valid_days = int(wf_cfg.get("validDays", 40))
    test_days = int(wf_cfg.get("testDays", 40))
    step_days = int(wf_cfg.get("stepDays", 40))

    con = etf_db.connect(etf_db_path)
    try:
        frame = etf_db.load_factor_frame(con, label, int(research_cfg.get("minDays", 40)))
    finally:
        con.close()
    if not frame:
        raise ValueError("无因子快照，请先运行 factors")
    codes = sorted({r["ts_code"] for r in frame})
    bars = ef._load_bars(market_path, codes)
    bench = ef._load_benchmark(market_path, cfg.get("benchmark", "000300.SH"))
    dates = sorted({r["trade_date"] for r in frame})

    run_id = f"etf-wf-{now_iso()}"
    daily_rows: list[dict] = []
    window_stats: list[dict] = []
    last_model_path: str | None = None
    k = min_train + valid_days
    n_windows = max(1, (len(dates) - min_train - valid_days) // step_days)
    while k + test_days <= len(dates):
        # 取消/超时检查点：滚动重训每个窗口前都要检查，否则长跑期间无法中断
        if cancelled():
            return {"cancelled": True, "windows": len(window_stats), "runId": run_id}
        test_start, test_end = dates[k], dates[k + test_days - 1]
        train_frame = [r for r in frame if r["trade_date"] < test_start]
        if len(train_frame) < valid_days + test_days + 20:
            break
        with tempfile.TemporaryDirectory(prefix="etf-wf-") as td:
            res = ranker.train_ranker(train_frame, label, model_cfg, _P(td))
            model = lgb.Booster(model_file=res["modelPath"])
            # persist the latest window model for holdings inference (temp dirs are deleted)
            import shutil as _shutil  # noqa: PLC0415
            latest_model = artifact_dir / "etf-wf-model.txt"
            latest_model.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copy(res["modelPath"], latest_model)
        last_model_path = str(latest_model)
        bt_cfg["topN"] = int(bt_cfg.get("topN", 5))
        r = bt.run_backtest(frame, model, bars, bench, bt_cfg,
                            start_date=test_start, end_date=test_end)
        prev = daily_rows[-1]["nav"] if daily_rows else None
        navs = [d["nav"] for d in r["daily"]]
        if prev is None:
            seq = navs
        else:
            f = prev / navs[0]
            seq = [v * f for v in navs]
        for d, v in zip(r["daily"], seq):
            daily_rows.append({"tradeDate": d["tradeDate"], "nav": round(float(v), 6),
                               "benchmark": d["benchmark"]})
        window_stats.append({"testStart": test_start, "testEnd": test_end,
                             "rankIc": res["rankIc"], "features": len(res["features"]),
                             "modelPath": res["modelPath"]})
        progress(20 + 70 * (k + step_days) / max(1, len(dates)))
        k += step_days

    if not daily_rows:
        raise ValueError("滚动窗口不足，无法执行 walk-forward")
    navs = np.asarray([d["nav"] for d in daily_rows], dtype=float)
    span_start, span_end = daily_rows[0]["tradeDate"], daily_rows[-1]["tradeDate"]
    bench_slice = bench.reindex([d for d in bench.index if span_start <= d <= span_end]).ffill()
    bench_total = float(bench_slice.iloc[-1] / bench_slice.iloc[0] - 1)
    total = float(navs[-1] / navs[0] - 1)
    rets = navs[1:] / navs[:-1] - 1
    annual = (1 + total) ** (252.0 / len(navs)) - 1
    vol = float(np.std(rets, ddof=1) * np.sqrt(252)) if len(rets) > 1 else 0.0
    sharpe = annual / vol if vol > 0 else 0.0
    peak = -np.inf
    mdd = 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    ic_vals = [w["rankIc"] for w in window_stats if w["rankIc"] is not None]
    metrics = {"windows": len(window_stats), "totalReturn": round(total, 4),
               "benchmarkReturn": round(bench_total, 4), "excessReturn": round(total - bench_total, 4),
               "annualReturn": round(annual, 4), "sharpe": round(sharpe, 3),
               "maxDrawdown": round(float(mdd), 4),
               "avgRankIc": round(float(np.mean(ic_vals)), 4) if ic_vals else None}

    # current holdings from the latest window model on the LATEST factor date.
    # 信号日取因子快照最新特征日（不依赖 forward_20 标签），使模拟盘能 T+1 跟随最新行情；
    # 回测曲线/统计仍基于有标签区间（daily_rows）。
    holdings: list[dict] = []
    hold_date = max(d["tradeDate"] for d in daily_rows)
    if last_model_path:
        con2 = etf_db.connect(etf_db_path)
        try:
            latest_feat = con2.execute(
                "SELECT MAX(trade_date) FROM etf_factor_snapshots WHERE mom20 IS NOT NULL").fetchone()[0]
            if latest_feat:
                hold_date = latest_feat
        finally:
            con2.close()
    # 熊市判定与回测（backtest.py）一致：基准收盘 < MA(maWindow) 才空仓，而非仅看配置
    regime = bt_cfg.get("regimeFilter") or {}
    bear = False
    if bool(regime.get("enabled", False)) and float(regime.get("bearWeight", 0.5)) <= 0.01:
        ma_win = int(regime.get("maWindow", 20))
        sig_days = [d for d in bench.index if d <= hold_date]
        if len(sig_days) >= ma_win:
            ma = float(bench.loc[sig_days[-ma_win:]].mean())
            cur = float(bench.loc[sig_days[-1]])
            bear = cur < ma
    if last_model_path:
        model = lgb.Booster(model_file=last_model_path)
        feat_cols = list(model.feature_name()) if hasattr(model, "feature_name") else ef.FACTOR_NAMES
        last_day = [r for r in frame if r["trade_date"] == hold_date]
        if not last_day:
            # frame 仅含带标签行；从全量快照取最新特征日（可能晚于回测区间终点）
            con2 = etf_db.connect(etf_db_path)
            try:
                cols = ",".join(feat_cols)
                last_day = [dict(r) for r in con2.execute(
                    "SELECT ts_code,trade_date," + cols + " FROM etf_factor_snapshots "
                    "WHERE trade_date=? ORDER BY ts_code", (hold_date,))]
            finally:
                con2.close()
        x = np.nan_to_num(np.asarray([[r.get(f) for f in feat_cols] for r in last_day], dtype=float))
        scores = np.asarray(model.predict(x), dtype=float)
        top_n = int(bt_cfg.get("topN", 5))
        order = np.argsort(-scores)
        for idx in order[:top_n]:
            holdings.append({"tsCode": last_day[idx]["ts_code"], "score": round(float(scores[idx]), 4),
                             "weight": round(1.0 / top_n, 4)})
    if bear:
        holdings = []

    effective_cfg = {"kind": "etf_walkforward", "topN": bt_cfg.get("topN"), "regimeFilter": regime,
                     "model": {k2: v2 for k2, v2 in model_cfg.items() if k2 != "featuresOverride" or v2},
                     "minTrain": min_train, "validDays": valid_days,
                     "testDays": test_days, "stepDays": step_days}
    config_json = json.dumps(effective_cfg, ensure_ascii=False)
    metrics_json = json.dumps(metrics, ensure_ascii=False)
    holdings_json = json.dumps({"tradeDate": hold_date, "bearRegime": bear, "holdings": holdings},
                               ensure_ascii=False)
    con = etf_db.connect(etf_db_path)
    try:
        # config 列已有完整配置，故只补指纹（include_config_json=False）避免存两遍。
        columns: dict[str, Any] = {}
        stamp_run_config(con, columns, effective_cfg, run_id=run_id, generated_at=now_iso(),
                         include_config_json=False)
        con.execute("""INSERT OR REPLACE INTO etf_walkforward_runs(run_id,generated_at,label,start_date,end_date,
                     windows,config,metrics,holdings,status,
                     config_sha256,config_yaml_sha256,git_commit,git_dirty)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, now_iso(), label, span_start, span_end, len(window_stats),
                     config_json, metrics_json, holdings_json, "complete",
                     columns["config_sha256"], columns["config_yaml_sha256"],
                     columns["git_commit"], columns["git_dirty"]))
        con.execute("BEGIN")
        for d in daily_rows:
            con.execute("INSERT OR REPLACE INTO etf_walkforward_daily VALUES(?,?,?,?)",
                        (run_id, d["tradeDate"], d["nav"], d["benchmark"]))
        con.commit()
    finally:
        con.close()
    progress(100)
    return {"runId": run_id, "label": label, "span": f"{span_start}..{span_end}",
            "metrics": metrics, "windows": window_stats,
            "holdings": {"tradeDate": hold_date, "bearRegime": bear, "holdings": holdings}}


def latest_walkforward(etf_db_path: Path) -> dict[str, Any]:
    con = etf_db.connect(etf_db_path)
    try:
        row = con.execute("SELECT * FROM etf_walkforward_runs ORDER BY generated_at DESC LIMIT 1").fetchone()
        if not row:
            return {"status": "none"}
        out = dict(row)
        out["config"] = json.loads(out["config"])
        out["metrics"] = json.loads(out["metrics"])
        out["holdings"] = json.loads(out["holdings"])
        out["daily"] = [dict(r) for r in con.execute(
            "SELECT trade_date tradeDate, nav, benchmark FROM etf_walkforward_daily WHERE run_id=? ORDER BY trade_date",
            (row["run_id"],))]
        return out
    finally:
        con.close()
