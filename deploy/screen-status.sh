#!/usr/bin/env bash
# 查看 screen 会话与三个服务的健康状态。
#   bash deploy/screen-status.sh [--logs]
set -euo pipefail

# shellcheck source=env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

declare -A SESSION=([sync]=quant-sync [engine]=quant-engine [query]=data-query)
declare -A PORT=([sync]=9101 [engine]=9102 [query]=9103)

echo "=== screen 会话 ==="
if screen -list 2>/dev/null | grep -qE '[0-9]+\.'; then
  screen -list 2>/dev/null | grep -E '[0-9]+\.'
else
  echo "  (无)"
fi

echo
echo "=== 监听端口 ==="
ss -lntp 2>/dev/null | grep -E ':(9101|9102|9103)\b' || echo "  (无)"

echo
echo "=== 健康检查 ==="
for svc in sync engine query; do
  session="${SESSION[$svc]}"; port="${PORT[$svc]}"
  info="$(curl -s --max-time 5 "http://127.0.0.1:$port/api/v1/health" || true)"
  if [[ -n "$info" ]]; then
    printf '  %-14s :%s  %s\n' "$session" "$port" "$info"
  else
    printf '  %-14s :%s  \033[1;31m无响应\033[0m\n' "$session" "$port"
  fi
done

if [[ "${1:-}" == "--logs" ]]; then
  echo
  for svc in sync engine query; do
    session="${SESSION[$svc]}"
    echo "=== tail logs/$session.log ==="
    tail -n 15 "$PROJECT_ROOT/logs/$session.log" 2>/dev/null || echo "  (无日志)"
    echo
  done
fi
