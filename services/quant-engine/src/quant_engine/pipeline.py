"""Leakage-controlled daily feature, training and inference pipeline."""
from __future__ import annotations

import json
import math
import sqlite3
from collections import deque
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .common import now_iso

FEATURE_COLUMNS = ["momentum5", "momentum20", "momentum60", "volatility20", "turnover_rate", "pe_ttm", "pb"]


def _compound(values: list[float]) -> float:
    result = 1.0
    for value in values:
        result *= 1 + value
    return result - 1


def build_features(market_path: Path, feature_dir: Path, parameters: dict[str, Any],
                   progress: Callable[[float], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
    output = feature_dir / "daily-v1.parquet"
    feature_dir.mkdir(parents=True, exist_ok=True)
    start = str(parameters.get("startDate", ""))
    end = str(parameters.get("endDate", ""))
    db = sqlite3.connect(f"file:{market_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    sql = """SELECT b.code,b.trade_date,b.open,b.close,b.pct_chg,b.volume,
                    d.turnover_rate,d.pe_ttm,d.pb
             FROM daily_bars b LEFT JOIN daily_basic d
               ON d.code=b.code AND d.trade_date=b.trade_date"""
    conditions, args = [], []
    if start:
        conditions.append("b.trade_date>=?")
        args.append(start)
    if end:
        conditions.append("b.trade_date<=?")
        args.append(end)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY b.code,b.trade_date"
    try:
        rows = db.execute(sql, args).fetchall()
    finally:
        # 原实现把 close 放在 execute 之后：SQL 报错（如表不存在）时连接泄漏，
        # 在 Windows 上还会一直占用 market.db 的文件句柄
        db.close()
    if not rows:
        raise ValueError("no daily bars available for feature generation")

    result: list[dict[str, Any]] = []
    by_code: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_code.setdefault(row["code"], []).append(row)
    for code_number, (code, series) in enumerate(by_code.items()):
        if cancelled():
            raise RuntimeError("feature generation cancelled")
        returns: deque[float] = deque(maxlen=60)
        for index, row in enumerate(series):
            daily_return = float(row["pct_chg"] or 0) / 100
            returns.append(daily_return)
            if len(returns) < 60 or index + 1 >= len(series):
                continue
            next_bar = series[index + 1]
            next_open, next_close = next_bar["open"], next_bar["close"]
            label = next_close / next_open - 1 if next_open not in (None, 0) and next_close is not None else None
            values = list(returns)
            mean20 = sum(values[-20:]) / 20
            variance20 = sum((x - mean20) ** 2 for x in values[-20:]) / 19
            item = {
                "code": code, "trade_date": row["trade_date"],
                "momentum5": _compound(values[-5:]), "momentum20": _compound(values[-20:]),
                "momentum60": _compound(values), "volatility20": math.sqrt(max(0, variance20) * 250),
                "turnover_rate": row["turnover_rate"], "pe_ttm": row["pe_ttm"], "pb": row["pb"],
                "label_t1_open_close": label,
            }
            if label is not None and all(item[name] is not None and math.isfinite(float(item[name])) for name in FEATURE_COLUMNS):
                result.append(item)
        progress(5 + 80 * (code_number + 1) / max(1, len(by_code)))
    if not result:
        raise ValueError("no complete feature rows were generated")
    temp = output.with_suffix(".tmp.parquet")
    pq.write_table(pa.Table.from_pylist(result), temp, compression="zstd")
    temp.replace(output)
    progress(100)
    return {"featureSet": "daily-v1", "path": str(output), "rows": len(result),
            "startDate": min(item["trade_date"] for item in result),
            "endDate": max(item["trade_date"] for item in result),
            "features": FEATURE_COLUMNS}


def train_model(feature_dir: Path, artifact_dir: Path, parameters: dict[str, Any],
                progress: Callable[[float], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except ImportError as error:
        raise RuntimeError("LightGBM is not installed; install quant-engine project dependencies") from error
    source = feature_dir / "daily-v1.parquet"
    if not source.exists():
        raise FileNotFoundError("daily-v1 features are missing; run a features job first")
    table = pq.read_table(source, columns=["trade_date", *FEATURE_COLUMNS, "label_t1_open_close"])
    data = table.to_pydict()
    dates = np.asarray(data["trade_date"])
    unique_dates = np.unique(dates)
    if len(unique_dates) < 60:
        raise ValueError("at least 60 trading dates are required for walk-forward training")
    validation_days = max(20, int(parameters.get("validationDays", 40)))
    test_days = max(20, int(parameters.get("testDays", 40)))
    if validation_days + test_days >= len(unique_dates):
        raise ValueError("validation/test windows leave no training observations")
    validation_start = unique_dates[-(validation_days + test_days)]
    test_start = unique_dates[-test_days]
    x = np.column_stack([np.asarray(data[name], dtype=float) for name in FEATURE_COLUMNS])
    y = np.asarray(data["label_t1_open_close"], dtype=float)
    train_mask, validation_mask, test_mask = dates < validation_start, (dates >= validation_start) & (dates < test_start), dates >= test_start
    if cancelled():
        raise RuntimeError("training cancelled")
    model = lgb.LGBMRegressor(n_estimators=int(parameters.get("nEstimators", 300)), learning_rate=0.03,
                             num_leaves=31, subsample=0.8, colsample_bytree=0.8, random_state=42,
                             n_jobs=1, verbosity=-1)
    model.fit(x[train_mask], y[train_mask], eval_X=x[validation_mask], eval_y=y[validation_mask],
              callbacks=[lgb.early_stopping(30, verbose=False)])
    progress(85)
    predictions = model.predict(x[test_mask])
    actual = y[test_mask]
    mse = float(np.mean((predictions - actual) ** 2))
    correlation = float(np.corrcoef(predictions, actual)[0, 1]) if len(actual) > 1 else 0.0
    artifact_dir.mkdir(parents=True, exist_ok=True)
    model_id = str(parameters.get("modelId", "lightgbm-t1"))
    model_path = artifact_dir / f"{model_id}.txt"
    model.booster_.save_model(str(model_path))
    metadata = {"modelId": model_id, "trainedAt": now_iso(), "features": FEATURE_COLUMNS,
                "label": "t1_open_close_return", "trainRows": int(train_mask.sum()),
                "validationRows": int(validation_mask.sum()), "testRows": int(test_mask.sum()),
                "testMse": mse, "testRankCorrelation": correlation,
                "validationStart": str(validation_start), "testStart": str(test_start)}
    model_path.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(100)
    return {**metadata, "path": str(model_path)}


def run_inference(feature_dir: Path, artifact_dir: Path, parameters: dict[str, Any],
                  progress: Callable[[float], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except ImportError as error:
        raise RuntimeError("LightGBM is not installed; install quant-engine project dependencies") from error
    model_id = str(parameters.get("modelId", "lightgbm-t1"))
    model_path = artifact_dir / f"{model_id}.txt"
    source = feature_dir / "daily-v1.parquet"
    if not model_path.exists() or not source.exists():
        raise FileNotFoundError("trained model or daily-v1 features are missing")
    table = pq.read_table(source, columns=["code", "trade_date", *FEATURE_COLUMNS])
    latest = max(table.column("trade_date").to_pylist())
    frame = table.filter(pc.equal(table["trade_date"], latest))
    values = frame.to_pydict()
    x = np.column_stack([np.asarray(values[name], dtype=float) for name in FEATURE_COLUMNS])
    if cancelled():
        raise RuntimeError("inference cancelled")
    scores = lgb.Booster(model_file=str(model_path)).predict(x)
    ranked = sorted(({"code": code, "score": float(score)} for code, score in zip(values["code"], scores)),
                    key=lambda item: -item["score"])
    top_n = max(1, min(500, int(parameters.get("topN", 50))))
    output = artifact_dir / f"{model_id}-selection-{latest}.json"
    payload = {"modelId": model_id, "tradeDate": latest, "generatedAt": now_iso(), "items": ranked[:top_n]}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress(100)
    return {**payload, "path": str(output)}
