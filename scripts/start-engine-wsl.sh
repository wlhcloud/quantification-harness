#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${QUANT_WSL_VENV:-$HOME/.venvs/quant-engine}"

if [[ -f "$PROJECT_ROOT/.quant.env" ]]; then
  set -a
  # Windows生成的环境文件可能为CRLF，去掉行尾\r后再加载。
  source <(sed 's/\r$//' "$PROJECT_ROOT/.quant.env")
  set +a
fi

export QUANT_ENGINE_PROJECT_ROOT="$PROJECT_ROOT"
export PATH="/usr/local/cuda/bin:$VENV/bin:$PATH"
export LD_LIBRARY_PATH="/usr/local/cuda/lib64:/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH:-}"

exec "$VENV/bin/python" -m uvicorn quant_engine.main:app \
  --app-dir "$PROJECT_ROOT/services/quant-engine/src" \
  --host "${QUANT_ENGINE_HOST:-127.0.0.1}" \
  --port "${QUANT_ENGINE_PORT:-9102}"
