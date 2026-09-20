"""One-shot migration: copy v1 Node core.db metadata into v2 Python-owned meta.db.

DDL is the single source of truth (contracts/schemas/meta.sql). plugin_runs is dropped.
Idempotent by default; pass --force to recreate.
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DDL = ROOT / "contracts" / "schemas" / "meta.sql"
SOURCE = ROOT / "data" / "core.db"
TARGET = ROOT / "data" / "meta.db"

TABLES = [
    "data_source_configs",
    "tool_source_routes",
    "datasets",
    "sync_runs",
    "dictionary_datasets",
    "dictionary_fields",
    "dictionary_mappings",
    "dictionary_types",
    "dictionary_items",
]


def load_ddl() -> list[str]:
    statements: list[str] = []
    for raw in DDL.read_text(encoding="utf-8").split(";"):
        stmt = " ".join(line for line in raw.splitlines() if not line.strip().startswith("--")).strip()
        if stmt:
            statements.append(stmt)
    return statements


def migrate(force: bool) -> None:
    if TARGET.exists() and not force:
        with sqlite3.connect(TARGET) as db:
            n = db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0]
        if n:
            print(f"meta.db already exists with {n} datasets; use --force to recreate")
            return
        TARGET.unlink()

    if not SOURCE.exists():
        raise SystemExit(f"source not found: {SOURCE}")

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(TARGET) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        for stmt in load_ddl():
            db.execute(stmt)
        db.commit()

        db.execute(f"ATTACH DATABASE ? AS src", (str(SOURCE),))
        for table in TABLES:
            before = db.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
            db.execute(f"INSERT INTO main.{table} SELECT * FROM src.{table}")
            after = db.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
            print(f"{table}: {after} rows copied")
            assert before == 0, f"target {table} was not empty before copy"
        db.commit()
        db.execute("DETACH DATABASE src")

    # verification pass
    with sqlite3.connect(TARGET) as db:
        for table in TABLES:
            n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"verify {table}: {n}")
    print(f"migration complete -> {TARGET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    migrate(parser.parse_args().force)
