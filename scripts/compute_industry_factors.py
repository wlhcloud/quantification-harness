#!/usr/bin/env python
"""行业轮动因子（stock_industry_factors）—— 薄封装。

计算逻辑已迁入 `quant_engine.stock_ml.aux_factors.build_industry_factors`（服务内 job 化，
每日链会调用 `stock_industry_factors` job）。本脚本仅作为命令行入口保留。

用法：python scripts/compute_industry_factors.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "quant-engine" / "src"))

from quant_engine.stock_ml.aux_factors import build_industry_factors  # noqa: E402


def main() -> int:
    print("=" * 60)
    print("行业轮动因子计算（实现见 quant_engine.stock_ml.aux_factors）")
    print("=" * 60)
    summary = build_industry_factors(ROOT / "data" / "market.db", ROOT / "data" / "factors.db")
    print(f"\n完成：{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
