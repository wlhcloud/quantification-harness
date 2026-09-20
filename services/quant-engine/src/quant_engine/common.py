from __future__ import annotations

import json
import math
import yaml
from datetime import datetime, timezone
from pathlib import Path


def now_iso() -> str:
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def finite(value) -> float | None:
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def ratio(now, before) -> float | None:
    if now is not None and before is not None and before != 0:
        return now / before - 1
    return None


def round6(value: float, digits: int = 6) -> float:
    return round(value * 10 ** digits) / 10 ** digits


def compact_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 and value.isdigit() else value


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    if not cfg.get("filters") or not cfg.get("factors") or not cfg.get("models") or not cfg["models"]:
        raise ValueError("factor model config is incomplete")
    for model_id, model in cfg["models"].items():
        weights = model.get("weights", {})
        if not weights:
            # ML模型（如ml_walkforward）不需要加权因子，跳过权重检查
            continue
        total = sum(weights.values())
        if total <= 0:
            raise ValueError(f"model {model_id} has no positive weights")
        for factor in weights:
            if factor not in cfg["factors"]:
                raise ValueError(f"model {model_id} references unknown factor {factor}")
    return cfg


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"configuration must be an object: {path}")
    return data
