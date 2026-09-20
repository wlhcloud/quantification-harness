"""脚本布局巡检：文档/生产引用的脚本必须存在，归档目录必须有索引。

背景：`scripts/` 根目录原先混着"计划任务在调的编排脚本"和"跑过一次的研究脚本"（80 个未跟踪
文件），清理时最容易犯的错是**把还被引用的脚本挪走**——本次就差点把 V5 回测脚本
（`backtest_technical_timing_v5*.py`，被「策略回测」页面和引擎提示点名）和 V5 模拟盘
（被每日链 `import`）归档掉。这个用例把"引用→存在"变成可执行断言。

同时锁住归档目录本身：里面的文件必须在 `research_archive/README.md` 里列名（否则归档会变成
无人认领的垃圾堆），且不能被生产代码引用。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = ROOT / "scripts" / "research_archive"
README = ARCHIVE / "README.md"

# 文档里用 N/x 代表通配（start-9101/2/3.bat、示例占位路径），不是真实文件名
PLACEHOLDERS = {"data/start-910N.bat", "scripts/start-910N.bat", "scripts/research_archive/xxx.py"}
REF_PATTERN = re.compile(r"(?:scripts|data)[/\\]([A-Za-z0-9_./\\-]+\.(?:py|ps1|bat|sh))")
SCAN_EXT = {".md", ".py", ".ps1", ".bat", ".yml", ".yaml"}
SKIP_DIRS = {"data", "node_modules", ".git", "deepseek-harness", "artifacts", "dist", "logs",
             "var", ".venv", "__pycache__", ".pnpm-store", "research_archive"}


def _iter_scan_files():
    for folder in ("docs", "services", "scripts", ".github"):
        base = ROOT / folder
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCAN_EXT:
                continue
            if set(path.relative_to(ROOT).parts) & SKIP_DIRS:
                continue
            yield path


class ScriptLayoutTest(unittest.TestCase):
    def test_referenced_scripts_exist(self):
        """docs/services/scripts/.github 里写到的 scripts|x 路径必须真实存在（防归档挪错、防文档过期）。"""
        missing: dict[str, set[str]] = {}
        for path in _iter_scan_files():
            rel = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in REF_PATTERN.finditer(text):
                raw = match.group(0).replace("\\", "/")
                sub = "/".join(part for part in raw.split("/") if part)  # 去掉 scripts//x 这种重复斜杠
                target = ROOT / sub
                if target.exists() or raw in PLACEHOLDERS or sub in PLACEHOLDERS:
                    continue
                missing.setdefault(sub, set()).add(rel)
        self.assertEqual(missing, {}, f"引用了不存在的脚本：{missing}")

    def test_archive_is_indexed_in_readme(self):
        """归档目录里的每个文件都要在 README 里出现，避免归档变成无人认领的目录。"""
        self.assertTrue(README.exists(), "缺少 scripts/research_archive/README.md")
        index = README.read_text(encoding="utf-8")
        unlisted = sorted(p.name for p in ARCHIVE.glob("*")
                          if p.name != "README.md" and p.name not in index)
        self.assertEqual(unlisted, [], f"归档文件未在 README 中列名：{unlisted}")

    def test_archive_is_not_executed_by_production(self):
        """生产代码不得 import 归档模块、也不得把它当子进程跑。

        注意：注释/面向用户的提示字符串里**允许**出现归档路径（例如"该产物由
        scripts/research_archive/xxx.py 生成"），所以这里按 AST 只看 import 与
        subprocess/exec 这类真会执行的地方。
        """
        archived = {p.stem for p in ARCHIVE.glob("*.py")}
        imported, executed = [], []
        for path in (ROOT / "services").rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            except SyntaxError:
                continue
            rel = path.relative_to(ROOT).as_posix()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported += [(rel, alias.name) for alias in node.names if alias.name.split(".")[0] in archived]
                elif isinstance(node, ast.ImportFrom):
                    module = (node.module or "").split(".")[0]
                    if module in archived:
                        imported.append((rel, module))
                elif isinstance(node, ast.Call):
                    func = ast.unparse(node.func)
                    if not any(k in func for k in ("subprocess", "os.system", "os.popen", "runpy", "Popen")):
                        continue
                    for arg in list(node.args) + [kw.value for kw in node.keywords]:
                        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "research_archive" in arg.value:
                            executed.append((rel, arg.value))
        self.assertEqual(imported, [], f"生产代码 import 了归档脚本：{imported}")
        self.assertEqual(executed, [], f"生产代码试图执行归档脚本：{executed}")

    def test_services_never_shell_out(self):
        """服务进程不得 shell out（架构约定：Python 服务只做 HTTP/SQLite，编排交给链/计划任务）。"""
        offenders = []
        for path in (ROOT / "services").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*(?:import|from)\s+(?:subprocess|runpy)\b", text, flags=re.MULTILINE):
                offenders.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(offenders, [], f"服务里出现了 shell out：{offenders}")

    def test_live_scripts_stay_at_root(self):
        """被生产代码 import 的链上脚本必须留在 scripts/ 根目录（移动会让每日链直接崩）。"""
        chain = (ROOT / "scripts" / "daily_stock_chain.py").read_text(encoding="utf-8")
        imports = re.findall(r"^\s*(?:from|import)\s+(\w+)", chain, flags=re.MULTILINE)
        for module in imports:
            if (ARCHIVE / f"{module}.py").exists():
                self.fail(f"{module}.py 被每日链 import，却在归档目录里")
        for required in ("technical_timing_sim.py", "technical_timing_sim_v5.py"):
            self.assertTrue((ROOT / "scripts" / required).exists(), f"{required} 必须留在 scripts/ 根目录")


class StartupScriptSafetyTest(unittest.TestCase):
    """启动脚本与它读取的密钥文件必须"cmd 安全"。

    踩过的坑（2026-09-13，看门狗当场抓到）：`.quant.env` 里写了中文注释、且用 LF 行尾之后，
    `data\\start-910N.bat` 的 `for /f "usebackq eol=# tokens=1,* delims=="` 会把多字节注释行的
    下一行并进"注释"，于是 `QUANT_API_KEY` **被静默漏读**——服务照常起来，但写接口鉴权失效
    （health: authEnabled=false）。所以：.bat 与 .quant.env 只允许 ASCII + CRLF。
    """

    def _assert_cmd_safe(self, path: Path):
        raw = path.read_bytes()
        non_ascii = [i for i, byte in enumerate(raw) if byte > 127]
        self.assertEqual(non_ascii, [], f"{path.name} 含非 ASCII 字节（cmd 的 for /f 会读歪）：偏移 {non_ascii[:5]}")
        text = raw.decode("ascii")
        lines = text.split("\n")
        bad = [i for i, line in enumerate(lines[:-1], 1) if not line.endswith("\r")]
        self.assertEqual(bad, [], f"{path.name} 存在 LF-only 行（cmd 可能把下一行并进当前行）：行号 {bad[:5]}")

    def test_start_bat_files_are_cmd_safe(self):
        bats = sorted((ROOT / "data").glob("start-910*.bat"))
        self.assertTrue(bats, "缺少 data/start-910*.bat")
        for bat in bats:
            with self.subTest(bat=bat.name):
                self._assert_cmd_safe(bat)
                text = bat.read_text(encoding="ascii")
                self.assertIn("127.0.0.1", text, "启动脚本必须只绑回环地址")
                self.assertIn(".quant.env", text, "启动脚本必须加载 .quant.env")

    def test_quant_env_is_cmd_safe(self):
        env_file = ROOT / ".quant.env"
        if not env_file.exists():
            self.skipTest("无 .quant.env（CI/全新环境）")
        self._assert_cmd_safe(env_file)
        keys = {line.split("=", 1)[0] for line in env_file.read_text(encoding="ascii").splitlines()
                if "=" in line and not line.startswith("#")}
        self.assertIn("QUANT_API_KEY", keys, ".quant.env 必须提供权威变量 QUANT_API_KEY")


if __name__ == "__main__":
    unittest.main()
