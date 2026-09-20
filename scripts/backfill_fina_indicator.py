# -*- coding: utf-8 -*-
"""补拉 financial_indicator 历史报告期（全市场逐只，start_date=20200101）。

背景：financial_indicator 表此前只从 20240101 起覆盖（约 10 期），而
income/balance/cashflow 已有 2020 起数据；因子重建的 roe/netprofitYoy/
debtToAssets 来自 financial_indicator，导致 2022-2023 历史回测期财务因子
缺失（空转期根因）。本脚本按 tushare 限制（仅支持逐只查询）串行补拉，
INSERT OR REPLACE 幂等、可断点续跑（已覆盖到 2020 的股票自动跳过）。

用法：D:\\ProdApp\\Python\\Python313\\python.exe scripts\\backfill_fina_indicator.py
建议在系统空闲时后台运行（约 5553 只 × ~0.6s ≈ 1 小时）。
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SYNC = "http://127.0.0.1:9101/api/v1"
MARKET_DB = Path(__file__).resolve().parents[1] / "data" / "market.db"
FIN_DB = Path(__file__).resolve().parents[1] / "data" / "finance.db"
START = "20200101"   # 覆盖 2020 起全部报告期（2022 回测期可用 2021 财报）
MIN_INTERVAL_S = 0.45  # tushare 频率保护


def _codes() -> list[str]:
    con = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True, timeout=10)
    try:
        row = con.execute("PRAGMA table_info(security_master)").fetchall()
        cols = [c[1] for c in row]
        col = "code" if "code" in cols else ("ts_code" if "ts_code" in cols else cols[0])
        rows = [r[0] for r in con.execute(
            f"SELECT {col} FROM security_master WHERE {col} LIKE '%.S%' ORDER BY {col}")]
        return rows
    finally:
        con.close()


def _already_done() -> set[str]:
    con = sqlite3.connect(f"file:{FIN_DB}?mode=ro", uri=True, timeout=10)
    try:
        rows = con.execute("SELECT DISTINCT code FROM financial_indicator").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _covered_early(codes: set[str]) -> set[str]:
    """已覆盖到 2020 报告期的股票（min end_date <= 20201231 视为已补完）。"""
    if not codes:
        return set()
    con = sqlite3.connect(f"file:{FIN_DB}?mode=ro", uri=True, timeout=10)
    try:
        q = ",".join("?" * len(codes))
        rows = con.execute(
            f"SELECT code, MIN(end_date) FROM financial_indicator WHERE code IN ({q}) "
            "GROUP BY code", list(codes)).fetchall()
        return {r[0] for r in rows if r[1] and r[1] <= "20201231"}
    finally:
        con.close()


def _sync_one(code: str) -> tuple[bool, str]:
    url = f"{SYNC}/sync/tool/fina_indicator?code={code}&start_date={START}"
    try:
        req = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            json.loads(resp.read().decode("utf-8"))
        return True, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:160]
        return False, f"HTTP{exc.code}: {body}"
    except urllib.error.URLError as exc:
        return False, str(exc)


def main() -> int:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] backfill fina_indicator start={START}", flush=True)
    codes = _codes()
    print(f"[INFO] 股票总数 {len(codes)}", flush=True)
    done = _already_done()
    early = _covered_early(done)
    todo = [c for c in codes if c not in early]
    print(f"[INFO] 已有 {len(done)} 只、其中覆盖到2020的有 {len(early)} 只；本次待补 {len(todo)} 只", flush=True)

    ok = fail = 0
    t0 = time.time()
    for i, code in enumerate(todo, 1):
        for attempt in range(3):
            ok_flag, err = _sync_one(code)
            if ok_flag:
                break
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
        if ok_flag:
            ok += 1
        else:
            fail += 1
            print(f"[FAIL] {code}: {err}", flush=True)
        if i % 200 == 0 or i == len(todo):
            el = time.time() - t0
            eta = el / i * (len(todo) - i) / 60 if i else 0
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {i}/{len(todo)} ok={ok} fail={fail} "
                  f"elapsed={el/60:.1f}min eta={eta:.0f}min", flush=True)
        time.sleep(MIN_INTERVAL_S)
    print(f"[DONE] ok={ok} fail={fail} (失败清单见上方 FAIL 行，可重跑续补)", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
