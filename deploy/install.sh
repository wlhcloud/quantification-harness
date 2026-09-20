#!/usr/bin/env bash
# 一键部署/更新脚本（在服务器上以 root 执行）。
#
#   bash /opt/ai_project/quantification-harness/deploy/install.sh
#
# 幂等：可反复执行。它做四件事：
#   1) 确保 conda 环境存在（默认 quant / Python 3.12）
#   2) 安装三个 Python 服务的依赖（可编辑安装，project_root 因此指向本仓库）
#   3) 用 screen 拉起三个服务（不装 systemd）
#   4) 健康检查
set -euo pipefail

# shellcheck source=env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

CONDA_BIN="${QUANT_CONDA_BIN:-/root/miniconda3/bin/conda}"
# 国内网络下 pypi 直连通常也可用；用 TUNA 镜像加速，置空即回退官方源。
PIP_INDEX="${QUANT_PIP_INDEX-https://pypi.tuna.tsinghua.edu.cn/simple}"
CONDA_CHANNEL="${QUANT_CONDA_CHANNEL:-https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "部署根目录: $PROJECT_ROOT"
[[ -f "$PROJECT_ROOT/.quant.env" ]] || { echo "缺少 $PROJECT_ROOT/.quant.env（服务鉴权/上游 token 都从这里读）" >&2; exit 1; }
command -v screen >/dev/null || { echo "缺少 screen：apt-get install -y screen" >&2; exit 1; }

# ---------------------------------------------------------------- 0. 构建信息
# 把"本批代码是哪个提交"固化下来。服务进程不允许 shell out，运行时只能读这个文件；
# 它是运行记录里 git_commit 的来源（见 stock_ml/config_integrity.py）。
if [[ -x "$PROJECT_ROOT/deploy/build-info.sh" ]]; then
  say "固化构建信息"
  "$PROJECT_ROOT/deploy/build-info.sh"
fi

# ---------------------------------------------------------------- 1. conda 环境
if [[ ! -x "$PYTHON_BIN" ]]; then
  say "创建 conda 环境 $CONDA_ENV (python 3.12)"
  "$CONDA_BIN" create -n "$CONDA_ENV" python=3.12 -y \
    --override-channels -c "$CONDA_CHANNEL"
else
  say "conda 环境已存在: $CONDA_PREFIX_DIR ($("$PYTHON_BIN" --version))"
fi

PIP=("$PYTHON_BIN" -m pip)
PIP_FLAGS=(--disable-pip-version-check)
[[ -n "$PIP_INDEX" ]] && PIP_FLAGS+=(-i "$PIP_INDEX")

say "升级 pip / wheel / setuptools"
"${PIP[@]}" install "${PIP_FLAGS[@]}" -q --upgrade pip setuptools wheel

# ---------------------------------------------------------------- 2. 依赖
# 三个服务的依赖全部声明在各自 pyproject.toml 里，可编辑安装即完成依赖安装；
# 不再额外维护一份 requirements.txt，避免"两份清单漂移"。
say "安装 quant-sync"
"${PIP[@]}" install "${PIP_FLAGS[@]}" -q -e "$PROJECT_ROOT/services/quant-sync"

say "安装 data-query-service"
"${PIP[@]}" install "${PIP_FLAGS[@]}" -q -e "$PROJECT_ROOT/services/data-query-service"

say "安装 quant-engine（lightgbm / sklearn / pandas / pyarrow，耗时最长）"
"${PIP[@]}" install "${PIP_FLAGS[@]}" -q -e "$PROJECT_ROOT/services/quant-engine"

say "安装测试/诊断附加依赖（pytest / httpx / ruff）"
"${PIP[@]}" install "${PIP_FLAGS[@]}" -q pytest httpx ruff

# ---------------------------------------------------------------- 3. 导入自检
say "导入自检（含 project_root 解析）"
"$PYTHON_BIN" - <<'PY'
import importlib, sys
from pathlib import Path
ok = True
for name in ("quant_sync", "quant_engine", "data_query"):
    try:
        importlib.import_module(name)
        print(f"  [ok] {name}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  [FAIL] {name}: {exc!r}")

from quant_engine.settings import settings as es
from quant_sync.settings import settings as ss
from data_query.settings import settings as ds
for label, st in (("quant_engine", es), ("quant_sync", ss), ("data_query", ds)):
    print(f"  {label}.project_root = {st.project_root}")
    if not Path(st.project_root).is_dir():
        ok = False
        print(f"  [FAIL] {label} project_root 不存在")

print("  market.db  =", es.market_path, es.market_path.exists())
print("  factors.db =", es.factors_path, es.factors_path.exists())
print("  meta.db    =", ss.meta_path, ss.meta_path.exists())
print("  authKey    =", "已加载" if es.api_key else "!! 未加载（.quant.env 没读到 QUANT_API_KEY）")
sys.exit(0 if ok else 1)
PY

# ---------------------------------------------------------------- 4. LightGBM 自检
say "LightGBM 自检"
"$PYTHON_BIN" - <<'PY'
import lightgbm, numpy, pandas, sklearn, pyarrow
print(f"  lightgbm {lightgbm.__version__}  numpy {numpy.__version__}  pandas {pandas.__version__}")
print(f"  scikit-learn {sklearn.__version__}  pyarrow {pyarrow.__version__}")
print(f"  lightgbm.__file__ = {lightgbm.__file__}")
PY

# ---------------------------------------------------------------- 5. screen 启动
say "重启 screen 托管服务"
chmod +x "$PROJECT_ROOT"/deploy/*.sh
bash "$PROJECT_ROOT/deploy/screen-down.sh"
sleep 2
bash "$PROJECT_ROOT/deploy/screen-up.sh"

# ---------------------------------------------------------------- 6. 健康检查
say "健康检查"
fail=0
for spec in "quant-sync:9101" "quant-engine:9102" "data-query:9103"; do
  session="${spec%%:*}"; port="${spec##*:}"
  body="$(curl -s --max-time 5 "http://127.0.0.1:$port/api/v1/health" || true)"
  if [[ -n "$body" ]]; then
    printf '  %-14s :%s  %s\n' "$session" "$port" "$body"
  else
    fail=1
    printf '  %-14s :%s  \033[1;31m无响应\033[0m（tail -n 60 %s/logs/%s.log）\n' \
      "$session" "$port" "$PROJECT_ROOT" "$session"
  fi
done

if [[ $fail -ne 0 ]]; then
  echo -e "\n\033[1;31m有服务未通过健康检查\033[0m" >&2
  exit 1
fi
echo -e "\n\033[1;32m部署完成\033[0m（screen 托管；重启机器后需重新执行 deploy/screen-up.sh）"
