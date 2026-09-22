"""Cross-sectional LightGBM ranker with walk-forward time splits (M2, additive).

Train on (features -> forward_20) with per-trade-date query groups; validate on
a trailing window for early stopping; report test-window RankIC. The test
window is never used for fitting or early stopping (no future leakage).
"""
from __future__ import annotations

import math
import statistics
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .factors import FACTOR_NAMES
from .research import _spearman


def _label_horizon(label: str) -> int:
    """从标签名解析持有期（forward_20 -> 20）；非 forward_N 命名返回 0。"""
    head, _, tail = str(label).rpartition("_")
    return int(tail) if head == "forward" and tail.isdigit() else 0


def _apply_embargo(rows: list[dict[str, Any]], cutoff_date: str, all_dates: list[str],
                   embargo_days: int) -> list[dict[str, Any]]:
    """剔除标签窗口会伸进下一段（valid/test）的最后 embargo_days 个交易日。

    forward_N 标签在 t 日用到 t+N 的价格，若 t 紧邻下一段起点，训练样本就带入了
    验证/测试期的信息（purge/embargo，López de Prado）。embargo_days=0 时行为不变。
    """
    if embargo_days <= 0:
        return rows
    prior = [d for d in all_dates if d < cutoff_date]
    blocked = set(prior[-embargo_days:])
    return [r for r in rows if r["trade_date"] not in blocked]


def _split(frame: list[dict[str, Any]], validation_days: int, test_days: int):
    dates = sorted({r["trade_date"] for r in frame})
    if len(dates) < validation_days + test_days + 20:
        raise ValueError("训练窗口不足：需要至少 validation+test+20 个交易日")
    valid_start = dates[-(validation_days + test_days)]
    test_start = dates[-test_days]
    train = [r for r in frame if r["trade_date"] < valid_start]
    valid = [r for r in frame if valid_start <= r["trade_date"] < test_start]
    test = [r for r in frame if r["trade_date"] >= test_start]
    return train, valid, test, dates[0], valid_start, test_start, dates[-1]


def _to_matrices(rows: list[dict[str, Any]], label: str, features: list[str] | None = None):
    cols = list(features or FACTOR_NAMES)
    x = np.array([[r.get(f) for f in cols] for r in rows], dtype=float)
    y = np.array([r[label] for r in rows], dtype=float)
    dates = [r["trade_date"] for r in rows]
    return x, y, dates


def _groups(rows: list[dict[str, Any]]) -> np.ndarray:
    counts: list[int] = []
    current_date = None
    current = 0
    for r in rows:
        if r["trade_date"] != current_date:
            if current:
                counts.append(current)
            current_date = r["trade_date"]
            current = 1
        else:
            current += 1
    if current:
        counts.append(current)
    return np.asarray(counts, dtype=np.int32)


def _date_rank_ic(model, rows: list[dict[str, Any]], label: str, features: list[str] | None = None) -> tuple[float | None, int]:
    cols = list(features or FACTOR_NAMES)
    by_date: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_date.setdefault(r["trade_date"], []).append(r)
    ics: list[float] = []
    for date in sorted(by_date):
        batch = by_date[date]
        x = np.array([[r.get(f) for f in cols] for r in batch], dtype=float)
        pred = model.predict(x)
        ys = [r[label] for r in batch]
        ic = _spearman(pred.tolist(), ys)
        if ic is not None:
            ics.append(ic)
    if not ics:
        return None, len(by_date)
    return sum(ics) / len(ics), len(by_date)


def _make_ranker(lgb: Any, model_cfg: dict[str, Any], gains: dict[str, Any],
                 device: str, n_estimators: int | None = None) -> Any:
    deterministic = bool(model_cfg.get("deterministic", False)) and device == "cpu"
    return lgb.LGBMRanker(
        objective="lambdarank", metric=str(model_cfg.get("evalMetric", "ndcg")),
        n_estimators=int(n_estimators or model_cfg.get("nEstimators", 300)),
        learning_rate=float(model_cfg.get("learningRate", 0.03)),
        num_leaves=int(model_cfg.get("numLeaves", 31)),
        min_data_in_leaf=int(model_cfg.get("minDataInLeaf", 20)),
        subsample=float(model_cfg.get("subsample", 1.0)),
        colsample_bytree=float(model_cfg.get("colsampleBytree", 0.8)),
        random_state=int(model_cfg.get("randomState", 42)),
        data_random_seed=int(model_cfg.get("dataRandomSeed", model_cfg.get("randomState", 42))),
        feature_fraction_seed=int(model_cfg.get("featureFractionSeed", model_cfg.get("randomState", 42))),
        bagging_seed=int(model_cfg.get("baggingSeed", model_cfg.get("randomState", 42))),
        extra_trees=bool(model_cfg.get("extraTrees", False)),
        n_jobs=1 if device != "cpu" else int(model_cfg.get("cpuNJobs", -1)),
        device_type=device, deterministic=deterministic,
        force_col_wise=bool(model_cfg.get("forceColWise", deterministic)),
        verbosity=-1, label_gain=gains["gains"])


def train_ranker(frame: list[dict[str, Any]], label: str, model_cfg: dict[str, Any],
                 artifact_dir: Path, progress: Callable[[float], None] = lambda p: None,
                 cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    try:
        import lightgbm as lgb
    except ImportError as error:
        raise RuntimeError("LightGBM is not installed") from error

    artifact_dir.mkdir(parents=True, exist_ok=True)
    validation_days = int(model_cfg.get("validationDays", 40))
    test_days = int(model_cfg.get("testDays", 40))
    train, valid, test, train_start, valid_start, test_start, end = _split(frame, validation_days, test_days)
    # M8 experiments (additive, default off): relative cross-sectional label
    if model_cfg.get("labelRelative"):
        by_date: dict[str, list[int]] = {}
        for i, r in enumerate(frame):
            by_date.setdefault(r["trade_date"], []).append(i)
        medians = {}
        for date, idxs in by_date.items():
            vals = [frame[i][label] for i in idxs if frame[i][label] is not None]
            if vals:
                medians[date] = statistics.median(vals)
        frame = [dict(r, **{label: (r[label] - medians.get(r["trade_date"], 0.0))
                            if r[label] is not None else None}) for r in frame]
        train = [r for r in frame if r["trade_date"] < valid_start]
        valid = [r for r in frame if valid_start <= r["trade_date"] < test_start]
        test = [r for r in frame if r["trade_date"] >= test_start]
    # purge/embargo：默认 0（保持既有数值），配置 walkforward.embargoDays 后生效
    embargo_days = max(0, int(model_cfg.get("embargoDays", 0)))
    all_dates = sorted({r["trade_date"] for r in frame})
    train = _apply_embargo(train, valid_start, all_dates, embargo_days)
    valid = _apply_embargo(valid, test_start, all_dates, embargo_days)
    # embargo 过大时会把验证集吃空 → 明确报错，而不是让 numpy 抛 "must be 2 dimensional"
    if embargo_days > 0 and (not train or not valid):
        raise ValueError(
            f"embargoDays={embargo_days} 过大：剔除后 train={len(train)} 行 / valid={len(valid)} 行；"
            f"请调小 embargoDays 或增大 validationDays/testDays")
    horizon = _label_horizon(label)
    overlap_days = max(0, horizon - embargo_days) if horizon else 0
    features = list(model_cfg.get("featuresOverride") or FACTOR_NAMES)
    x_train, y_train, _ = _to_matrices(train, label, features)
    x_valid, y_valid, _ = _to_matrices(valid, label, features)
    x_test, y_test, _ = _to_matrices(test, label, features)
    group_train, group_valid = _groups(train), _groups(valid)

    # lambdarank needs integer labels: bucket forward returns into 10 quantile
    # grades; label_gain maps each grade to its representative return.
    n_grades = int(model_cfg.get("labelGrades", 10))
    y_train_int, gains = _bucketize(y_train, n_grades)
    y_valid_int = _apply_buckets(y_valid, gains["edges"])
    progress(20)

    requested_device = str(model_cfg.get("deviceType", "cpu")).lower()
    if requested_device not in {"cpu", "cuda", "gpu"}:
        raise ValueError(f"不支持的 model.deviceType={requested_device!r}，可选 cpu/cuda/gpu")
    def make_model(device: str) -> Any:
        return _make_ranker(lgb, model_cfg, gains, device)

    model = make_model(requested_device)
    if cancelled():
        raise RuntimeError("training cancelled")
    fit_kwargs = dict(group=group_train, feature_name=features,
                      eval_set=[(x_valid, y_valid_int)], eval_group=[group_valid],
                      callbacks=[lgb.early_stopping(int(model_cfg.get("earlyStopping", 30)), verbose=False)])
    effective_device = requested_device
    fallback_reason = None
    try:
        model.fit(x_train, y_train_int, **fit_kwargs)
    except lgb.basic.LightGBMError as exc:
        if requested_device == "cpu" or not bool(model_cfg.get("cpuFallback", True)):
            raise
        fallback_reason = str(exc)
        effective_device = "cpu"
        model = make_model("cpu")
        model.fit(x_train, y_train_int, **fit_kwargs)
    progress(70)

    rank_ic, days = _date_rank_ic(model, test, label, features)
    # also compute plain IC on test rows
    pred_all = model.predict(x_test)
    ic = _pearson(pred_all.tolist(), [r[label] for r in test])

    artifact = artifact_dir / "etf-ranker-model.txt"
    model.booster_.save_model(str(artifact))
    progress(90)

    return {"label": label, "trainStart": train_start, "trainEnd": valid_start,
            "validStart": valid_start, "validEnd": test_start, "testStart": test_start, "testEnd": end,
            "rankIc": round(rank_ic, 4) if rank_ic is not None else None,
            "ic": round(ic, 4), "testDays": days,
            "trainRows": len(train), "validRows": len(valid), "testRows": len(test),
            # 标签期与验证/测试窗口的剩余重叠天数（embargoDays >= horizon 时为 0）
            "embargoDays": embargo_days, "labelHorizonDays": horizon, "labelOverlapDays": overlap_days,
            "modelPath": str(artifact), "features": features,
            "requestedDevice": requested_device, "effectiveDevice": effective_device,
            "deviceFallbackReason": fallback_reason}


def train_ranker_final(frame: list[dict[str, Any]], label: str, model_cfg: dict[str, Any],
                       artifact_dir: Path, n_estimators: int,
                       progress: Callable[[float], None] = lambda p: None,
                       cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Fit the production ranker on every currently labelled row.

    Walk-forward validation chooses model size and proves out-of-sample quality.  Once
    those gates pass, the deployable model must not keep the evaluation split's stale
    training cutoff; it is refit here without a validation/test holdout.
    """
    try:
        import lightgbm as lgb
    except ImportError as error:
        raise RuntimeError("LightGBM is not installed") from error
    if not frame:
        raise ValueError("最终重训没有可用标签数据")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    rows = frame
    if model_cfg.get("labelRelative"):
        by_date: dict[str, list[float]] = {}
        for row in rows:
            if row.get(label) is not None:
                by_date.setdefault(row["trade_date"], []).append(float(row[label]))
        medians = {date: statistics.median(values) for date, values in by_date.items()}
        rows = [dict(row, **{label: float(row[label]) - medians[row["trade_date"]]})
                for row in rows]
    features = list(model_cfg.get("featuresOverride") or FACTOR_NAMES)
    x_train, y_train, _ = _to_matrices(rows, label, features)
    group_train = _groups(rows)
    y_train_int, gains = _bucketize(y_train, int(model_cfg.get("labelGrades", 10)))
    requested_device = str(model_cfg.get("deviceType", "cpu")).lower()
    if requested_device not in {"cpu", "cuda", "gpu"}:
        raise ValueError(f"不支持的 model.deviceType={requested_device!r}，可选 cpu/cuda/gpu")
    effective_device = requested_device
    fallback_reason = None
    model = _make_ranker(lgb, model_cfg, gains, requested_device, n_estimators)
    if cancelled():
        raise RuntimeError("training cancelled")
    try:
        model.fit(x_train, y_train_int, group=group_train, feature_name=features)
    except lgb.basic.LightGBMError as exc:
        if requested_device == "cpu" or not bool(model_cfg.get("cpuFallback", True)):
            raise
        fallback_reason = str(exc)
        effective_device = "cpu"
        model = _make_ranker(lgb, model_cfg, gains, "cpu", n_estimators)
        model.fit(x_train, y_train_int, group=group_train, feature_name=features)
    progress(90)
    artifact = artifact_dir / "etf-ranker-model.txt"
    model.booster_.save_model(str(artifact))
    dates = sorted({row["trade_date"] for row in rows})
    return {"label": label, "trainStart": dates[0], "trainEnd": dates[-1],
            "trainRows": len(rows), "trees": int(model.booster_.num_trees()),
            "modelPath": str(artifact), "features": features,
            "requestedDevice": requested_device, "effectiveDevice": effective_device,
            "deviceFallbackReason": fallback_reason}


def _bucketize(y: np.ndarray, n_grades: int) -> tuple[np.ndarray, dict[str, Any]]:
    """Quantile-bucket continuous labels into 0..n_grades-1 integers.

    Returns (int labels, {"edges": quantile edges, "gains": per-grade gain}).
    """
    finite = y[np.isfinite(y)]
    if len(finite) < n_grades:
        raise ValueError("训练样本不足，无法分档")
    edges = np.percentile(finite, np.linspace(0, 100, n_grades + 1))
    edges[-1] += 1e-9  # include the max
    labels = np.digitize(y, edges[1:-1])
    gains: list[float] = []
    for g in range(n_grades):
        sel = finite[(np.digitize(finite, edges[1:-1]) == g)]
        gains.append(float(np.mean(sel)) if len(sel) else 0.0)
    return labels.astype(np.int32), {"edges": edges, "gains": gains}


def _apply_buckets(y: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.digitize(y, edges[1:-1]).astype(np.int32)


def _pearson(a: list[float], b: list[float]) -> float:
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None
             and math.isfinite(float(x)) and math.isfinite(float(y))]
    if len(pairs) < 5:
        return 0.0
    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    var_x = sum((v - mean_x) ** 2 for v in xs)
    var_y = sum((v - mean_y) ** 2 for v in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)
