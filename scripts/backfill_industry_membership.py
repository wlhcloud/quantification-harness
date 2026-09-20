"""Backfill point-in-time SW2021 L1 industry membership into market.db.

Uses Tushare ``index_classify`` and ``index_member_all``.  Both active and
removed membership rows are retained; consumers must join with
``valid_from <= trade_date <= valid_to`` (open-ended when valid_to is NULL).
"""
from __future__ import annotations

import sqlite3
import sys
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
    classes = client.call(
        "index_classify", {"level": "L1", "src": "SW2021"},
        route="official-only", timeout_ms=60_000,
    ).rows
    if not classes:
        raise RuntimeError("Tushare index_classify returned no SW2021 L1 industries")

    records: dict[tuple[str, str, str, str | None], tuple] = {}
    for number, item in enumerate(classes, 1):
        l1_code = item["index_code"]
        for is_new in ("Y", "N"):
            rows = client.call(
                "index_member_all", {"l1_code": l1_code, "is_new": is_new},
                route="official-only", timeout_ms=60_000,
            ).rows
            for row in rows:
                code = row.get("ts_code")
                valid_from = row.get("in_date")
                if not code or not valid_from:
                    continue
                valid_to = row.get("out_date") or None
                key = (code, row.get("l1_code") or l1_code, valid_from, valid_to)
                records[key] = (
                    code, row.get("name"), row.get("l1_code") or l1_code,
                    row.get("l1_name") or item.get("industry_name"), valid_from,
                    valid_to, row.get("is_new") or is_new, "tushare.index_member_all",
                )
        print(f"[{number}/{len(classes)}] {l1_code} rows={len(records)}", flush=True)

    if not records:
        raise RuntimeError("Tushare index_member_all returned no membership rows")
    con = sqlite3.connect(settings.market_path, timeout=60)
    try:
        con.execute("DROP TABLE IF EXISTS industry_membership_history_staging")
        con.execute("""
            CREATE TABLE industry_membership_history_staging(
              code TEXT NOT NULL,name TEXT,l1_code TEXT NOT NULL,industry TEXT NOT NULL,
              valid_from TEXT NOT NULL,valid_to TEXT,is_current TEXT NOT NULL,source TEXT NOT NULL,
              PRIMARY KEY(code,l1_code,valid_from)
            )
        """)
        con.executemany(
            "INSERT OR REPLACE INTO industry_membership_history_staging VALUES(?,?,?,?,?,?,?,?)",
            records.values(),
        )
        con.commit()
        con.execute("BEGIN IMMEDIATE")
        con.execute("DROP TABLE IF EXISTS industry_membership_history")
        con.execute("ALTER TABLE industry_membership_history_staging RENAME TO industry_membership_history")
        con.execute(
            "CREATE INDEX idx_industry_membership_code_dates "
            "ON industry_membership_history(code,valid_from,valid_to)"
        )
        con.commit()
    finally:
        con.close()
    print(f"complete rows={len(records)} industries={len(classes)}", flush=True)


if __name__ == "__main__":
    main()
