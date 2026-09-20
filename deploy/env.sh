#!/usr/bin/env bash
# 公共环境装载：解析项目根、加载 .quant.env、固定三个服务的 project_root。
# 被 run-service.sh 与 install.sh source，不要直接执行。
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${QUANT_PROJECT_ROOT:-$(cd "$DEPLOY_DIR/.." && pwd)}"
export PROJECT_ROOT

# conda：环境名与安装前缀都可覆盖，默认沿用本机实测值。
CONDA_ENV="${QUANT_CONDA_ENV:-quant}"
CONDA_PREFIX_DIR="${QUANT_CONDA_PREFIX:-/root/miniconda3/envs/$CONDA_ENV}"
PYTHON_BIN="$CONDA_PREFIX_DIR/bin/python"
export CONDA_ENV CONDA_PREFIX_DIR PYTHON_BIN

# .quant.env 由 Windows 侧生成（ASCII + CRLF，Windows 的 .bat 解析要求）。
# 这里用 sed 去掉行尾 \r 后再 source，保证既能被 Linux 读取，也不破坏仓库侧的不变量。
if [[ -f "$PROJECT_ROOT/.quant.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source <(sed 's/\r$//' "$PROJECT_ROOT/.quant.env")
  set +a
fi

# settings.py 里 project_root 默认由 __file__ 的 parents[4] 推导；可编辑安装下
# 推导结果就是本目录，这里再显式钉死一次，避免安装方式变化把数据目录指到别处。
export QUANT_SYNC_PROJECT_ROOT="$PROJECT_ROOT"
export QUANT_ENGINE_PROJECT_ROOT="$PROJECT_ROOT"
export DATA_QUERY_PROJECT_ROOT="$PROJECT_ROOT"

# 默认监听 0.0.0.0，让本机前端（Vite dev server）能直连服务器端口。
export QUANT_SYNC_HOST="${QUANT_SYNC_HOST:-0.0.0.0}"
export QUANT_ENGINE_HOST="${QUANT_ENGINE_HOST:-0.0.0.0}"
export DATA_QUERY_HOST="${DATA_QUERY_HOST:-0.0.0.0}"

export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1
