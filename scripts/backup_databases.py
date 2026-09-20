"""SQLite 在线快照备份：`VACUUM INTO` + 完整性校验 + 轮转保留。

背景：`data/` 下的库合计约 7.6 GB（market 3.5G / factors 2.9G / finance 1.2G），
都是**不可再生**的研究资产（分钟线那次清零就没有备份、不可恢复）。原先仓库里没有任何
备份脚本，只有一句"无备份，不可恢复"的说明。

这里用 SQLite 的 `VACUUM INTO`：服务正在写（WAL）时也能拿到事务一致的快照，
不需要停服，也不需要 `cp`（`cp` 在 WAL 下可能拿到半截状态）。每个快照写完后立刻
`PRAGMA integrity_check` + 表数量核对，避免"备份看起来有、其实打不开"。

用法：
    python scripts/backup_databases.py                    # 备份 data/ 下所有 .db，保留最近 2 份
    python scripts/backup_databases.py --keep 3
    python scripts/backup_databases.py --databases market.db factors.db
    python scripts/backup_databases.py --dry-run
    python scripts/backup_databases.py --dest E:\\quant-backup   # 换盘（推荐：不要和源库同盘）

排期（可选，需要时手工注册一次；默认不注册，占盘取决于 --keep）：
    schtasks /Create /TN quant-db-backup /SC DAILY /ST 20:30 ^
      /TR "D:\\ProdApp\\Python\\Python313\\python.exe D:\\ProdProject\\AI\\quantification-harness\\scripts\\backup_databases.py --keep 2"
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DEFAULT_DEST = ROOT / "data" / "backups"


def list_databases(data_dir: Path) -> list[Path]:
    return sorted((p for p in data_dir.glob("*.db") if p.is_file()), key=lambda p: -p.stat().st_size)


def backup_one(source: Path, target: Path) -> dict[str, object]:
    """VACUUM INTO 快照 + 校验。返回 {source,target,bytes,tables,ok,error}。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    started = time.time()
    try:
        # 只读连接 + VACUUM INTO：源库在被别的进程写也能拿到一致快照
        src = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        try:
            src.execute("VACUUM INTO ?", (str(target),))
        finally:
            src.close()
        check = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            tables = check.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchone()[0]
        finally:
            check.close()
        ok = integrity == "ok"
        return {"source": source.name, "target": target.name, "bytes": target.stat().st_size,
                "tables": tables, "ok": ok, "seconds": round(time.time() - started, 1),
                "error": "" if ok else f"integrity_check={integrity}"}
    except (sqlite3.Error, OSError) as exc:
        return {"source": source.name, "target": target.name, "bytes": 0, "tables": 0,
                "ok": False, "seconds": round(time.time() - started, 1), "error": str(exc)}


def rotate(dest: Path, keep: int, dry_run: bool = False) -> list[str]:
    """保留最近 keep 个快照目录（按名字里的时间戳排序），其余删除。返回被删除的目录名。"""
    snaps = sorted((p for p in dest.iterdir() if p.is_dir() and not p.name.startswith(".")), key=lambda p: p.name)
    removed: list[str] = []
    for old in snaps[:-keep] if keep > 0 else []:
        removed.append(old.name)
        if not dry_run:
            shutil.rmtree(old, ignore_errors=True)
    return removed


def free_gb(path: Path) -> float:
    try:
        return round(shutil.disk_usage(path).free / 1024 ** 3, 1)
    except OSError:
        return -1.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite 在线快照备份（VACUUM INTO + 校验 + 轮转）")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--keep", type=int, default=2, help="保留最近几份快照（默认 2）")
    parser.add_argument("--databases", nargs="*", help="只备份这些文件名（默认 data/ 下全部 .db）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    args = parser.parse_args(argv)

    if args.databases:
        sources = [args.data_dir / name for name in args.databases]
        missing = [str(p) for p in sources if not p.exists()]
        if missing:
            print(f"[ERROR] 库不存在：{missing}", file=sys.stderr)
            return 2
    else:
        sources = list_databases(args.data_dir)
    if not sources:
        print(f"[ERROR] {args.data_dir} 下没有 .db", file=sys.stderr)
        return 2

    total_gb = round(sum(p.stat().st_size for p in sources) / 1024 ** 3, 2)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target_dir = args.dest / stamp
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 备份 {len(sources)} 个库（{total_gb} GB）"
          f" → {target_dir}（保留 {args.keep} 份，目标盘剩余 {free_gb(args.dest.parent if args.dest.parent.exists() else ROOT)} GB）")
    if args.dry_run:
        for src in sources:
            print(f"    [dry-run] {src.name} ({round(src.stat().st_size / 1024 ** 3, 2)} GB)")
        for name in rotate(args.dest, args.keep, dry_run=True) if args.dest.exists() else []:
            print(f"    [dry-run] 将删除旧快照 {name}")
        return 0

    results = []
    for src in sources:
        result = backup_one(src, target_dir / src.name)
        results.append(result)
        flag = "OK " if result["ok"] else "FAIL"
        print(f"    [{flag}] {result['source']:<18} {round(int(result['bytes']) / 1024 ** 3, 2):>6} GB "
              f"tables={result['tables']:<4} {result['seconds']}s {result['error']}")

    failed = [r for r in results if not r["ok"]]
    if failed:
        # 有失败就保留这一轮（便于排查），但仍照常轮转更旧的
        print(f"[ERROR] {len(failed)} 个库备份失败，快照目录保留：{target_dir}", file=sys.stderr)
    for name in rotate(args.dest, args.keep):
        print(f"    已删除旧快照 {name}")
    print(f"[DONE] 快照 {stamp}：{len(results) - len(failed)}/{len(results)} 成功，"
          f"合计 {round(sum(int(r['bytes']) for r in results) / 1024 ** 3, 2)} GB")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
