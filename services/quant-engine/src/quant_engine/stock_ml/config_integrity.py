# -*- coding: utf-8 -*-
"""配置可观测性：为每次运行生成可回溯的配置指纹，并归档配置快照。

解决的问题（2026-09-20 审计发现的缺口）
------------------------------------------------
审计 25 次 walk-forward 运行后发现三处"看得到但追不回"：

1. **配置没有指纹**：`stock_walkforward_runs.config` 存了内容快照，但没有
   `config_sha256`，也没有代码版本。于是"这次跑的是哪个提交的配置"只能靠人肉
   逐 run diff JSON 反推——`publishGate.minPositiveWindowRate` 从 0.55 改成 0.50
   落在哪两次运行之间，当时无法直接回答。
2. **配置文件与运行记录没有链接**：`config/*.yaml` 是可变文件，运行记录是内容
   快照，两者之间没有可比的键，改了 yaml 再跑也看不出来。
3. **没有可还原的配置存档**：`artifacts/` 只存模型产物，配置树本身不归档，
   想在任何时候回答"当时完整的配置是什么"做不到。

本模块给出的四个值
------------------------------------------------
- ``effectiveConfigSha256`` — **实际生效配置**的规范化哈希（唯一权威指纹）。
  按 run 存储，相同哈希 == 完全相同的运行配置，这是可回溯的主键。
- ``yamlSha256`` — 当次读入的 **YAML 文件**哈希。与上一个值不同即说明
  "运行输入被代码覆盖过"，能直接暴露"改了配置没生效/没提交"。
- ``gitCommit`` — 服务**启动时**的 HEAD 提交号。
- ``gitDirty`` — 服务启动时工作区是否脏。脏即代码不可复现。

为什么 git 信息在"部署时"生成、运行时只读文件
------------------------------------------------
两个约束共同决定了这个设计：

1. **架构约定**：本仓库禁止服务进程 shell out（`test_scripts_never_shell_out`：
   "Python 服务只做 HTTP/SQLite，编排交给链/计划任务"）。因此运行时**不能**去调
   `git rev-parse`。部署脚本 `deploy/build-info.sh` 在部署/升级时把版本信息写成
   `.build-info.json`，服务只读文件。
2. **长驻进程的正确性**：引擎启动后代码即固定。若在一次运行**结束后**才去读当前
   git 状态，而期间有人 `git pull`，记录的提交号就会与实际执行的代码不符——这是
   "看起来可回溯、实际误导人"的典型陷阱。部署时写入 + 进程内缓存，保证记录的是
   真正加载进内存的那份代码。

优先级：环境变量 ``QUANT_BUILD_COMMIT`` / ``QUANT_BUILD_DIRTY`` >
``<项目根>/.build-info.json`` > 未知（None）。**未知就是未知**，不会退化成"没有配置"。

落盘位置
------------------------------------------------
- ``data/factors.db`` 表 ``config_fingerprint`` — 按哈希索引，含原始 YAML 文本，
  是"配置 → 所有使用它的运行"的查询入口。
- ``artifacts/config-snapshots/<sha256>/`` — 内容寻址的配置存档（config.json +
  meta.json + 原始 YAML 副本），哈希相同则目录已存在、不重复写。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

SNAPSHOT_DIRNAME = "config-snapshots"
BUILD_INFO_FILENAME = ".build-info.json"

# 服务启动时解析一次的构建信息（见模块 docstring 的说明）。
_GIT_INFO: dict[str, Any] | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS config_fingerprint (
  config_sha256 TEXT PRIMARY KEY,
  yaml_sha256   TEXT,
  yaml_path     TEXT,
  git_commit    TEXT,
  git_dirty     INTEGER,
  generated_at  TEXT NOT NULL,
  run_id        TEXT,
  config_json   TEXT NOT NULL,
  yaml_text     TEXT
);
CREATE INDEX IF NOT EXISTS idx_config_fingerprint_run ON config_fingerprint(run_id);
"""


def _canonical_json(value: Any) -> str:
    """确定性序列化：排序键 + 紧凑分隔符，保证同结构必得同哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def effective_config_sha256(effective: dict[str, Any]) -> str:
    """实际生效配置的指纹。

    调用方必须先剔除指纹字段本身再传入，否则会自指。
    """
    return hashlib.sha256(_canonical_json(effective).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str | None:
    """文件内容哈希；文件读不到时返回 None（不抛错，指纹缺失不应中断训练）。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _detect_repo_root() -> Path | None:
    """定位项目根：优先环境变量，其次从模块位置向上找部署标记。

    兼容两种部署：可编辑安装（源码在仓库内）与站点包安装（需靠环境变量）。
    """
    env_root = os.environ.get("QUANT_PROJECT_ROOT") or os.environ.get("QUANT_ENGINE_PROJECT_ROOT")
    if env_root:
        return Path(env_root)
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() or (parent / BUILD_INFO_FILENAME).exists():
            return parent
    return None


def _parse_dirty(raw: Any) -> bool | None:
    """把各种写法解析成三态：True / False / None（未知）。"""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "dirty"):
        return True
    if text in ("0", "false", "no", "clean"):
        return False
    return None


def _read_build_info() -> dict[str, Any]:
    """读取构建信息：环境变量优先，其次 `.build-info.json`。

    不执行任何外部命令（架构约定：服务进程不 shell out）。
    """
    commit = os.environ.get("QUANT_BUILD_COMMIT") or None
    dirty = _parse_dirty(os.environ.get("QUANT_BUILD_DIRTY"))
    source = "env" if commit else None
    root = _detect_repo_root()
    if root is not None and not commit:
        info_path = root / BUILD_INFO_FILENAME
        try:
            data = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            commit = data.get("commit") or None
            dirty = _parse_dirty(data.get("dirty"))
            source = "build-info" if commit else None
    error = None
    if not commit:
        error = (f"缺少构建信息：未设置 QUANT_BUILD_COMMIT，且找不到可读的 {BUILD_INFO_FILENAME}"
                 "（由 deploy/build-info.sh 在部署时生成）")
    return {"commit": commit, "dirty": dirty, "source": source,
            "root": str(root) if root else None, "error": error}


def git_info(refresh: bool = False) -> dict[str, Any]:
    """部署时的代码版本信息（进程内缓存）。

    返回 ``{"commit": str|None, "dirty": bool|None, "short": str|None, "error": str|None}``。
    ``commit`` 为 None 表示无法确定代码版本；``dirty`` 为 None 表示无法判断。
    两者为 None 时不应把该次运行当作可复现。
    """
    global _GIT_INFO
    if _GIT_INFO is not None and not refresh:
        return _GIT_INFO
    info = _read_build_info()
    commit = info["commit"]
    _GIT_INFO = {
        "commit": commit,
        "dirty": info["dirty"],
        "short": commit[:7] if commit else None,
        "source": info["source"],
        "repoRoot": info["root"],
        "error": info["error"],
    }
    return _GIT_INFO


def build_fingerprint(effective: dict[str, Any], yaml_path: Path | None = None) -> dict[str, Any]:
    """汇总一次运行的配置指纹。

    ``effective`` 是实际生效的配置（不含指纹字段）。返回值可直接并入要落库的
    config JSON，也可作为独立列写入。
    """
    info = git_info()
    return {
        "effectiveConfigSha256": effective_config_sha256(effective),
        "yamlSha256": file_sha256(yaml_path) if yaml_path else None,
        "yamlPath": str(yaml_path) if yaml_path else None,
        "gitCommit": info.get("commit"),
        "gitCommitShort": info.get("short"),
        "gitDirty": info.get("dirty"),
        "gitError": info.get("error"),
    }


def ensure_schema(con: Any) -> None:
    """幂等建表。"""
    con.executescript(SCHEMA)


def record_fingerprint(
    con: Any,
    fingerprint: dict[str, Any],
    effective: dict[str, Any],
    *,
    run_id: str | None = None,
    yaml_text: str | None = None,
    generated_at: str | None = None,
) -> None:
    """把指纹与完整生效配置写入 ``config_fingerprint``（按哈希去重）。

    同一个哈希再次运行时只补全 run_id/时间，不覆盖已有的原始 YAML 文本——
    第一份证据比后写的更可信。
    """
    ensure_schema(con)
    sha = fingerprint["effectiveConfigSha256"]
    con.execute(
        "INSERT OR IGNORE INTO config_fingerprint("
        "config_sha256,yaml_sha256,yaml_path,git_commit,git_dirty,generated_at,run_id,"
        "config_json,yaml_text) VALUES(?,?,?,?,?,?,?,?,?)",
        (sha, fingerprint.get("yamlSha256"), fingerprint.get("yamlPath"),
         fingerprint.get("gitCommit"), None if fingerprint.get("gitDirty") is None
         else int(bool(fingerprint["gitDirty"])),
         generated_at or "", run_id, _canonical_json(effective), yaml_text))
    con.execute(
        "UPDATE config_fingerprint SET run_id=COALESCE(?,run_id), generated_at=? "
        "WHERE config_sha256=?",
        (run_id, generated_at or "", sha))
    con.commit()


def write_snapshot(
    artifact_dir: Path,
    fingerprint: dict[str, Any],
    effective: dict[str, Any],
    *,
    run_id: str | None = None,
    yaml_text: str | None = None,
) -> Path:
    """内容寻址归档：``<artifact_dir>/config-snapshots/<sha256>/``。

    哈希相同则不重写（快照天然幂等）。返回快照目录。
    """
    sha = fingerprint["effectiveConfigSha256"]
    target = Path(artifact_dir) / SNAPSHOT_DIRNAME / sha
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text(
        json.dumps(effective, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    meta = dict(fingerprint)
    meta["runId"] = run_id
    (target / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    if yaml_text:
        (target / "config.yaml").write_text(yaml_text, encoding="utf-8")
    return target


def list_fingerprints(con: Any, limit: int = 50) -> list[dict[str, Any]]:
    """按配置哈希列出运行过的配置（可观测性入口）。"""
    ensure_schema(con)
    rows = con.execute(
        "SELECT config_sha256,yaml_sha256,git_commit,git_dirty,generated_at,run_id "
        "FROM config_fingerprint ORDER BY generated_at DESC, config_sha256 LIMIT ?",
        (int(limit),)).fetchall()
    out = []
    for row in rows:
        if hasattr(row, "keys"):
            out.append({k: row[k] for k in row.keys()})
        else:
            out.append({"config_sha256": row[0], "yaml_sha256": row[1], "git_commit": row[2],
                        "git_dirty": row[3], "generated_at": row[4], "run_id": row[5]})
    return out


def get_fingerprint(con: Any, config_sha256: str) -> dict[str, Any] | None:
    """取回某个配置的完整存档（可回溯：配置 + 原始 YAML + 代码版本）。"""
    ensure_schema(con)
    row = con.execute(
        "SELECT * FROM config_fingerprint WHERE config_sha256=?", (config_sha256,)).fetchone()
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else None
    data = {k: row[k] for k in keys} if keys else dict(zip(
        ["config_sha256", "yaml_sha256", "yaml_path", "git_commit", "git_dirty",
         "generated_at", "run_id", "config_json", "yaml_text"], row))
    try:
        data["config"] = json.loads(data.get("config_json") or "{}")
    except (TypeError, ValueError):
        data["config"] = {}
    return data


if __name__ == "__main__":  # pragma: no cover - 手工排查用
    info = git_info()
    print(json.dumps(info, ensure_ascii=False, indent=2))
    print("effective_config_sha256 自检:", effective_config_sha256({"a": 1, "b": [2, 3]}))
    sys.exit(0)
