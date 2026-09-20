"""Backfill daily point-in-time ST membership from an available Tushare gateway."""
from __future__ import annotations

import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "quant-sync" / "src"))

from quant_sync.meta import MetaStore  # noqa: E402
from quant_sync.settings import Settings  # noqa: E402
from quant_sync.upstream import TushareClient  # noqa: E402


def main() -> None:
    settings = Settings()
    meta = MetaStore(settings.meta_path, credential_key=settings.credential_key)
    client = TushareClient(meta, timeout_ms=60_000)
    con = sqlite3.connect(settings.market_path)
    dates = [r[0] for r in con.execute("SELECT DISTINCT trade_date FROM daily_bars ORDER BY trade_date")]
    con.close()

    def fetch(day: str) -> tuple[str, list[dict], str]:
        errors = []
        for attempt in range(1, 6):
            for source_id in ("promax", "rds"):
                try:
                    rows = client._request(meta.source(source_id), "stock_st", {"trade_date": day}, 60_000)
                    return day, rows, source_id
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{source_id}:{error}")
            time.sleep(min(30, attempt * 5))
        raise RuntimeError(f"{day}: {'; '.join(errors)}")

    records = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch, day): day for day in dates}
        for number, future in enumerate(as_completed(futures), 1):
            day, rows, source = future.result()
            for row in rows:
                code = row.get("ts_code") or row.get("code")
                if code:
                    records.append((day, code, row.get("name"), row.get("type"),
                                    row.get("type_name"), source))
            if number % 100 == 0 or number == len(dates):
                print(f"{number}/{len(dates)} dates, {len(records)} rows", flush=True)

    con = sqlite3.connect(settings.market_path, timeout=60)
    try:
        con.execute("DROP TABLE IF EXISTS stock_st_history_staging")
        con.execute("CREATE TABLE stock_st_history_staging("
                    "trade_date TEXT NOT NULL,code TEXT NOT NULL,name TEXT,st_type TEXT,type_name TEXT,"
                    "source TEXT NOT NULL,PRIMARY KEY(trade_date,code))")
        con.executemany("INSERT OR REPLACE INTO stock_st_history_staging VALUES(?,?,?,?,?,?)", records)
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        con.execute("DROP TABLE IF EXISTS stock_st_history")
        con.execute("ALTER TABLE stock_st_history_staging RENAME TO stock_st_history")
        con.execute("CREATE INDEX idx_stock_st_code_date ON stock_st_history(code,trade_date)")
        con.commit()
    finally:
        con.close()
    print(f"complete dates={len(dates)} rows={len(records)}", flush=True)


if __name__ == "__main__":
    main()
