#!/usr/bin/env bash
# 单入口服务启动器：run-service.sh <sync|engine|query>
# exec 到 conda 环境里的 python，保证 systemd 能正确接管进程与信号。
set -euo pipefail

SERVICE="${1:-}"
if [[ -z "$SERVICE" ]]; then
  echo "usage: run-service.sh <sync|engine|query>" >&2
  exit 2
fi

# shellcheck source=env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

case "$SERVICE" in
  sync)
    APP="quant_sync.main:app"
    APP_DIR="$PROJECT_ROOT/services/quant-sync/src"
    HOST="${QUANT_SYNC_HOST:-0.0.0.0}"
    PORT="${QUANT_SYNC_PORT:-9101}"
    ;;
  engine)
    APP="quant_engine.main:app"
    APP_DIR="$PROJECT_ROOT/services/quant-engine/src"
    HOST="${QUANT_ENGINE_HOST:-0.0.0.0}"
    PORT="${QUANT_ENGINE_PORT:-9102}"
    ;;
  query)
    APP="data_query.main:app"
    APP_DIR="$PROJECT_ROOT/services/data-query-service/src"
    HOST="${DATA_QUERY_HOST:-0.0.0.0}"
    PORT="${DATA_QUERY_PORT:-9103}"
    ;;
  *)
    echo "unknown service: $SERVICE (expected sync|engine|query)" >&2
    exit 2
    ;;
esac

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "python not found: $PYTHON_BIN" >&2
  echo "先跑 deploy/install.sh 创建 conda 环境 $CONDA_ENV" >&2
  exit 3
fi

# WorkingDirectory 也设了，这里再 cd 一次，保证脱离 systemd 手工执行时相对路径一致。
cd "$PROJECT_ROOT"

exec "$PYTHON_BIN" -m uvicorn "$APP" \
  --app-dir "$APP_DIR" \
  --host "$HOST" \
  --port "$PORT" \
  --no-access-log
