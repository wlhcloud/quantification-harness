#!/usr/bin/env python
"""为真实数据补齐缺失索引（默认只报告，不改动数据库）。

背景（只读实测）：
  market.db  daily_bars 主键为 (code, trade_date)，因此
      SELECT SUM(pct_chg>0) FROM daily_bars WHERE trade_date=?   ->  SCAN daily_bars（全表扫描 3.5GB）
  minute.db  minute_bars 主键为 (code, trade_time, freq)，按 code + 时间区间 + freq 查询时
      freq 只能做残余过滤（1m/5m 混读），且 22GB / 1.29 亿行。

用法：
  python scripts/ensure_indexes.py                  # 只报告缺失与现状（不动库）
  python scripts/ensure_indexes.py --apply          # 真正建索引
  python scripts/ensure_indexes.py --apply --db market

注意：
  - 建索引会写库并持写锁（daily_bars 3.5GB 预计数十秒，minute_bars 22GB 预计数分钟），
    请在收盘后、quant-sync/quant-engine 空闲时执行；执行前建议备份。
  - 全部使用 CREATE INDEX IF NOT EXISTS，可重复执行。
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (库文件, 表, 索引名, 索引列, 说明)
WANTED: list[tuple[str, str, str, str, str]] = [
    ("market.db", "daily_bars", "idx_daily_bars_trade_date", "trade_date",
     "data-query 市场宽度 SUM(pct_chg>0) WHERE trade_date=? 目前全表扫描"),
    ("market.db", "etf_daily_bars", "idx_etf_daily_bars_trade_date", "trade_date",
     "ETF 按交易日聚合/回补判断"),
    ("market.db", "index_daily_bars", "idx_index_daily_bars_trade_date", "trade_date",
     "指数按交易日聚合（walkforward 基准）"),
    ("minute.db", "minute_bars", "idx_minute_bars_code_freq_time", "code, freq, trade_time",
     "分钟查询把 freq 从残余过滤变为索引前缀，避免 1m/5m 混读"),
]


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def _describe(con: sqlite3.Connection, table: str) -> tuple[bool, set[str]]:
    exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    if not exists:
        return False, set()
    return True, {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))}


def _estimate_rows(con: sqlite3.Connection, table: str) -> str:
    """行数估算：大表 COUNT(*) 可能跑几分钟，改用 sqlite_stat1 / rowid 上限。

    minute_bars 1.29 亿行时 COUNT(*) 需要全索引扫描，dry-run 不该被它拖住。
    """
    try:
        stat = con.execute("SELECT stat FROM sqlite_stat1 WHERE tbl=? LIMIT 1", (table,)).fetchone()
        if stat and stat[0]:
            return f"~{int(str(stat[0]).split()[0]):,} 行(stat1)"
    except sqlite3.Error:
        pass
    try:
        max_rowid = con.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()[0]
        if max_rowid is not None:
            return f"≤{int(max_rowid):,} 行(rowid 上限)"
    except sqlite3.Error:
        pass
    return "行数未知"


def main() -> int:
    parser = argparse.ArgumentParser(description="补齐量化库缺失索引（默认 dry-run）")
    parser.add_argument("--apply", action="store_true", help="真正执行 CREATE INDEX（默认只报告）")
    parser.add_argument("--db", choices=["market", "minute"], help="只处理指定库")
    args = parser.parse_args()

    todo = [w for w in WANTED if not args.db or w[0] == f"{args.db}.db"]
    missing = 0
    for db_name, table, index_name, columns, why in todo:
        path = ROOT / "data" / db_name
        if not path.exists():
            print(f"[SKIP] 缺少库文件 {path}", flush=True)
            continue
        con = _connect(path)
        try:
            present, indexes = _describe(con, table)
            if not present:
                print(f"[SKIP] {db_name}:{table} 不存在", flush=True)
                continue
            if index_name in indexes:
                print(f"[OK]   {db_name}:{table} 已有 {index_name}", flush=True)
                continue
            # 同名但不同列的旧索引也要能识别
            same_cols = any(
                columns.replace(" ", "") in
                ((con.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0] or "")
                 .replace(" ", "").lower())
                for name in indexes)
            if same_cols:
                print(f"[OK]   {db_name}:{table} 已有等价索引", flush=True)
                continue
            missing += 1
            print(f"[MISS] {db_name}:{table} 缺 {index_name}({columns})  {_estimate_rows(con, table)}",
                  flush=True)
            print(f"       原因：{why}", flush=True)
            if not args.apply:
                continue
            print("       正在建索引（写锁，请勿中断）...", flush=True)
            started = time.time()
            con.execute(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table}({columns})")
            con.commit()
            print(f"       完成，用时 {time.time() - started:.1f}s", flush=True)
        finally:
            con.close()

    if missing and not args.apply:
        print(f"\n共 {missing} 个缺失索引。确认后执行：python scripts/ensure_indexes.py --apply", flush=True)
    elif args.apply:
        print("\n建索引完成。可用 EXPLAIN QUERY PLAN 复查查询计划（应不再出现 SCAN）。", flush=True)
    else:
        print("\n无需补索引。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
