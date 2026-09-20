#!/usr/bin/env python
"""契约一致性校验：代码里的 HTTP 路由 / job 类型 vs contracts/ 下的契约文件。

背景（本轮修复）：`contracts/` 是"契约先行"的落地点，但实际长期滞后于代码 ——
例如 `contracts/schemas/job.schema.json` 的 job 类型枚举只有最初 5 种（features/
factor_mining/training/backtest/inference），而服务已注册 14 种，且该 schema 声明
`additionalProperties: false`，按它校验真实 job 会**拒绝合法任务**。

校验规则：
  1. **契约里声明、代码里不存在** → ERROR（消费者会照着不存在的接口写代码）；
  2. **JobType 枚举不一致**（代码 vs job.schema.json）→ ERROR；
  3. **代码里有、契约里未声明** → WARNING（文档欠账）；`--strict` 时升级为 ERROR。

用法：
  python scripts/check_contracts.py            # 宽松：只有 1/2 类问题会失败
  python scripts/check_contracts.py --strict   # 严格：未声明的路由也算失败（CI 用）
  python scripts/check_contracts.py --root <dir>   # 指向另一份仓库副本（自测用）

不依赖 PyYAML：契约里的 path 用行扫描解析（这些文件都是手写的、缩进一致）。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROUTES = re.compile(r'@(?:app|router)\.(get|post|put|delete|patch)\(\s*"([^"]+)"')
# 契约文件里 paths 段下的键：两个空格缩进 + / 开头
CONTRACT_PATH = re.compile(r"^  (/[^\s:]+):\s*$")

SERVICES: dict[str, dict[str, object]] = {
    "quant-engine": {
        "sources": ["services/quant-engine/src/quant_engine/main.py",
                    "services/quant-engine/src/quant_engine/api.py"],
        "router_prefix": "/api/v1",       # api.py 里的 @router 挂在 /api/v1 下
        "contract": "contracts/openapi/quant-engine.yaml",
    },
    "quant-sync": {
        "sources": ["services/quant-sync/src/quant_sync/main.py"],
        "router_prefix": "",
        "contract": "contracts/openapi/quant-sync.yaml",
    },
    "data-query": {
        "sources": ["services/data-query-service/src/data_query/main.py"],
        "router_prefix": "",
        "contract": "contracts/openapi/data-query.yaml",
    },
}

JOB_TYPE_SOURCE = "services/quant-engine/src/quant_engine/models.py"
JOB_SCHEMA = "contracts/schemas/job.schema.json"


def code_routes(root: Path, sources: list[str], router_prefix: str) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for rel in sources:
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for method, route in ROUTES.findall(text):
            prefix = router_prefix if route.startswith("/") and not route.startswith("/api/") else ""
            routes.add((method.upper(), f"{prefix}{route}"))
    return routes


def contract_paths(contract_file: Path) -> set[str]:
    if not contract_file.exists():
        return set()
    paths: set[str] = set()
    for line in contract_file.read_text(encoding="utf-8").splitlines():
        match = CONTRACT_PATH.match(line)
        if match:
            paths.add(match.group(1))
    return paths


def code_job_types(root: Path) -> list[str]:
    """从 models.py 的 JobType 枚举里取全部值（AST，避免导入服务）。"""
    path = root / JOB_TYPE_SOURCE
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "JobType":
            values = []
            for stmt in node.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    values.append(str(stmt.value.value))
            return values
    return []


def schema_job_types(root: Path) -> list[str]:
    data = json.loads((root / JOB_SCHEMA).read_text(encoding="utf-8"))
    return list(data.get("properties", {}).get("type", {}).get("enum", []))


def contract_methods(contract_file: Path) -> set[tuple[str, str]]:
    """契约里每个 path 声明了哪些方法（缩进 4 空格的 get/post/...）。"""
    if not contract_file.exists():
        return set()
    out: set[tuple[str, str]] = set()
    current: str | None = None
    for line in contract_file.read_text(encoding="utf-8").splitlines():
        match = CONTRACT_PATH.match(line)
        if match:
            current = match.group(1)
            continue
        if current and re.match(r"^    (get|post|put|delete|patch):\s*$", line):
            out.add((line.strip().rstrip(":").upper(), current))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 contracts/ 与代码是否一致")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--strict", action="store_true", help="代码有而契约没有的路由也算失败")
    args = parser.parse_args()
    root = Path(args.root)

    errors: list[str] = []
    warnings: list[str] = []

    print("== HTTP 路由 ==")
    for service, spec in SERVICES.items():
        routes = code_routes(root, spec["sources"], str(spec["router_prefix"]))
        declared = contract_paths(root / str(spec["contract"]))
        declared_methods = contract_methods(root / str(spec["contract"]))
        code_paths = {route for _, route in routes}
        missing_in_code = sorted(declared - code_paths)
        documented_prefixes = {p for m, p in declared_methods}
        missing_in_contract = sorted(
            route for method, route in routes
            if route not in documented_prefixes  # 该路径未被契约声明
        )
        stale_methods = sorted(
            f"{method} {path}" for method, path in declared_methods
            if (method, path) not in routes
        )
        print(f"  {service}: 代码 {len(code_paths)} 个路径 / 契约 {len(declared)} 个路径")
        for path in missing_in_code:
            errors.append(f"[{service}] 契约声明了 {path}，但代码里不存在")
        for item in stale_methods:
            errors.append(f"[{service}] 契约声明的 {item} 与代码方法不匹配")
        for path in missing_in_contract:
            warnings.append(f"[{service}] 代码有 {path}，契约未声明")

    print("\n== JobType 枚举 ==")
    code_types = code_job_types(root)
    schema_types = schema_job_types(root)
    print(f"  代码 {len(code_types)} 种 / job.schema.json {len(schema_types)} 种")
    for job_type in sorted(set(code_types) - set(schema_types)):
        errors.append(f"[job.schema.json] 缺少代码已注册的 job 类型 {job_type}")
    for job_type in sorted(set(schema_types) - set(code_types)):
        errors.append(f"[job.schema.json] 声明了代码中不存在的 job 类型 {job_type}")

    if warnings:
        print(f"\n== 未声明路由（{len(warnings)} 条{ '，--strict 下视为失败' if not args.strict else ''}）==")
        for item in warnings:
            print("  WARN " + item)
    if errors:
        print(f"\n== 错误（{len(errors)} 条）==")
        for item in errors:
            print("  ERR  " + item)

    failed = bool(errors) or (args.strict and bool(warnings))
    print("\n" + ("[FAIL] 契约与代码不一致" if failed else "[OK] 契约与代码一致"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
