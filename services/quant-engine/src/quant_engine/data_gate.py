"""Authoritative data-admission checks for backtest jobs.

The UI can display these checks, but this module deliberately lives in the
engine so an API caller cannot bypass a failed precondition by skipping UI.
"""
from __future__ import annotations

import sqlite3
import re
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _check(check_id: str, label: str, ok: bool, detail: str, *, warning: bool = False) -> dict[str, str]:
    return {"id": check_id, "label": label, "status": "pass" if ok else ("warning" if warning else "fail"), "detail": detail}


def _scalar(db: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()) -> Any:
    return db.execute(query, params).fetchone()[0]


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    return bool(_scalar(db, "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)))


def check_backtest_readiness(kind: str, market_path: Path, finance_path: Path, minute_path: Path,
                             start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    """Return a serialisable decision and its individual, user-visible checks."""
    kind = "short" if kind == "short" else "monthly"
    checks: list[dict[str, str]] = []
    required_minute_dates: list[str] = []
    required_pit_dates: list[str] = []
    short_range_valid = bool(start_date and end_date and re.fullmatch(r"\d{8}", start_date)
                             and re.fullmatch(r"\d{8}", end_date) and start_date <= end_date)
    if kind == "short":
        checks.append(_check("backtest-range", "回测日期范围", short_range_valid,
                             f"本次检查范围：{start_date} — {end_date}。" if short_range_valid else "短周期回测必须提供有效的 YYYYMMDD 开始和结束日期。"))
    try:
        # A synchronisation writer must not turn the admission endpoint into a
        # hanging UI request. A busy database is itself an unsafe input.
        with closing(sqlite3.connect(market_path, timeout=2)) as market:
            if kind == "short" and short_range_valid:
                calendar = [row[0] for row in market.execute(
                    "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date>=? ORDER BY trade_date", (start_date,)
                ).fetchall()] if _table_exists(market, "daily_bars") else []
                signal_dates = [date for date in calendar if date <= end_date]
                daily_days = len(signal_dates)
                daily_minimum = 3
                calendar_index = {date: index for index, date in enumerate(calendar)}
                minute_required: set[str] = set()
                pit_required: set[str] = set(signal_dates)
                for signal_date in signal_dates:
                    index = calendar_index[signal_date]
                    if index + 1 < len(calendar):
                        pit_required.add(calendar[index + 1])
                        minute_required.add(calendar[index + 1])
                    if index + 2 < len(calendar):
                        minute_required.add(calendar[index + 2])
                required_minute_dates = sorted(minute_required)
                required_pit_dates = sorted(pit_required)
            else:
                daily_days = _scalar(market, "SELECT COUNT(DISTINCT trade_date) FROM daily_bars") if _table_exists(market, "daily_bars") else 0
                daily_minimum = 120
            checks.append(_check("daily-history", "日线行情历史", daily_days >= daily_minimum, f"检查范围内有 {daily_days} 个交易日；准入下限为 {daily_minimum} 个交易日。"))

            has_status = _table_exists(market, "security_status_history")
            if has_status and kind == "short" and short_range_valid:
                pit_start = required_pit_dates[0] if required_pit_dates else start_date
                pit_end = required_pit_dates[-1] if required_pit_dates else end_date
                status_rows = _scalar(market, "SELECT COUNT(*) FROM security_status_history WHERE trade_date BETWEEN ? AND ?", (pit_start, pit_end))
                status_dates = {row[0] for row in market.execute(
                    "SELECT DISTINCT trade_date FROM security_status_history WHERE trade_date BETWEEN ? AND ?", (pit_start, pit_end)
                ).fetchall()}
            else:
                status_rows = _scalar(market, "SELECT COUNT(*) FROM security_status_history") if has_status else 0
                status_dates = set()
            checks.append(_check("pit-status", "PIT 证券状态", status_rows > 0, f"检查范围内 PIT 状态记录 {status_rows:,} 条。" if status_rows else "检查范围缺少历史证券状态，无法避免状态穿越。"))
            if kind == "short" and short_range_valid:
                missing_pit_dates = [date for date in required_pit_dates if date not in status_dates]
                checks.append(_check("pit-date-coverage", "PIT 日期覆盖",
                                     not missing_pit_dates and bool(required_pit_dates),
                                     f"所需 {len(required_pit_dates)} 个信号/入场日均有 PIT 状态。" if not missing_pit_dates and required_pit_dates
                                     else f"缺少 {len(missing_pit_dates)} 个 PIT 日期：{'、'.join(missing_pit_dates[:5])}{'…' if len(missing_pit_dates) > 5 else ''}"))

            if kind == "monthly":
                has_adjustment = _table_exists(market, "adjustment_factors")
                adjustment_rows = _scalar(market, "SELECT COUNT(*) FROM adjustment_factors") if has_adjustment else 0
                checks.append(_check("adjustment-factors", "复权因子", adjustment_rows > 0, f"复权因子 {adjustment_rows:,} 条；月度回测据此计算连续价格。" if adjustment_rows else "缺少复权因子，无法构建连续价格序列。"))

        if kind == "monthly":
            with closing(sqlite3.connect(finance_path, timeout=2)) as finance:
                has_finance = _table_exists(finance, "financial_indicator")
                finance_rows = _scalar(finance, "SELECT COUNT(*) FROM financial_indicator") if has_finance else 0
                checks.append(_check("pit-financials", "PIT 财务指标", finance_rows > 0, f"财务指标 {finance_rows:,} 条，按公告日取数。" if finance_rows else "缺少财务指标，因子截面不可重建。"))

        if kind == "short":
            with closing(sqlite3.connect(minute_path, timeout=2)) as minute:
                has_bars = _table_exists(minute, "minute_bars")
                has_rows = bool(minute.execute("SELECT 1 FROM minute_bars WHERE freq='5m' LIMIT 1").fetchone()) if has_bars else False
                has_ledger = _table_exists(minute, "minute_sync_days")
                ledger_start = required_minute_dates[0] if required_minute_dates else start_date
                ledger_end = required_minute_dates[-1] if required_minute_dates else end_date
                ledger_filter = " AND trade_date BETWEEN ? AND ?" if short_range_valid else ""
                ledger_params: tuple[Any, ...] = (ledger_start, ledger_end) if short_range_valid else ()
                completed_days = _scalar(minute, "SELECT COUNT(*) FROM minute_sync_days WHERE freq='5m' AND status='complete'" + ledger_filter, ledger_params) if has_ledger else 0
                checks.append(_check("minute-5m", "5 分钟行情", has_rows and completed_days >= 20, f"治理台账中有 {completed_days:,} 条完整代码×交易日记录；短周期回测仅使用 5m 数据。" if has_rows else "没有 5 分钟行情数据。"))
                tracked_dates = {row[0] for row in minute.execute(
                    "SELECT DISTINCT trade_date FROM minute_sync_days WHERE freq='5m' AND status IN ('complete','incomplete','source_missing')" + ledger_filter,
                    ledger_params,
                ).fetchall()} if has_ledger else set()
                missing_minute_dates = [date for date in required_minute_dates if date not in tracked_dates]
                checks.append(_check("minute-date-coverage", "5 分钟交易日覆盖",
                                     not missing_minute_dates and bool(required_minute_dates),
                                     f"策略所需 {len(required_minute_dates)} 个 T+1/T+2 交易日均有分钟台账。" if not missing_minute_dates and required_minute_dates
                                     else f"整日缺失 {len(missing_minute_dates)} 天：{'、'.join(missing_minute_dates[:5])}{'…' if len(missing_minute_dates) > 5 else ''}"))
                gaps = _scalar(minute, "SELECT COUNT(*) FROM minute_sync_days WHERE freq='5m' AND status IN ('incomplete','source_missing')" + ledger_filter, ledger_params) if has_ledger else 0
                total_quality_cells = completed_days + gaps
                completeness = 100 * completed_days / total_quality_cells if total_quality_cells else 0
                quality_detail = f"治理台账完整度 {completeness:.2f}%（{gaps:,} 条历史缺口记录）；回测准入阈值为 99.9%。" if gaps else "治理台账未发现 5 分钟缺口记录。"
                if completeness >= 99.9:
                    checks.append(_check("minute-5m-quality", "5 分钟数据完整性", gaps == 0, quality_detail, warning=gaps > 0))
                else:
                    checks.append(_check("minute-5m-quality", "5 分钟数据完整性", False, quality_detail))
    except (sqlite3.Error, OSError) as error:
        checks.append(_check("data-store", "数据存储可读", False, f"无法读取本地研究数据：{error}"))

    failures = [item["label"] for item in checks if item["status"] == "fail"]
    return {
        "kind": kind,
        "range": {"startDate": start_date, "endDate": end_date} if kind == "short" else None,
        "allowed": not failures,
        "checkedAt": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "blockedReasons": failures,
        "forcePolicy": "带风险运行必须提交不少于 10 字的风险原因；决定、原因与检查快照将写入任务审计记录。",
    }
