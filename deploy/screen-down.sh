#!/usr/bin/env bash
# 停止 screen 托管的 Python 服务（先 SIGTERM 让正在跑的 job 收尾，再退出 screen）。
#
#   bash deploy/screen-down.sh            # 全部
#   bash deploy/screen-down.sh engine     # 只停 quant-engine
set -euo pipefail

# shellcheck source=env.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/env.sh"

declare -A SESSION=([sync]=quant-sync [engine]=quant-engine [query]=data-query)
declare -A PORT=([sync]=9101 [engine]=9102 [query]=9103)

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=(sync engine query)
fi

for svc in "${TARGETS[@]}"; do
  if [[ -z "${SESSION[$svc]:-}" ]]; then
    echo "未知服务: $svc（可选 sync|engine|query）" >&2
    exit 2
  fi
done

stop_one() {
  local svc="$1" session="${SESSION[$1]}" port="${PORT[$1]}"
  if ! screen -list 2>/dev/null | grep -qE "[0-9]+\.${session}[[:space:]]"; then
    printf '  %-14s 未在运行\n' "$session"
    return 0
  fi
  # 按端口找进程发 SIGTERM（uvicorn 会优雅退出；engine 的长 job 有机会收尾）
  fuser -k -TERM -n tcp "$port" >/dev/null 2>&1 || true
  for _ in $(seq 1 30); do
    ss -lnt 2>/dev/null | grep -qE ":${port}[[:space:]]" || break
    sleep 0.5
  done
  screen -S "$session" -X quit >/dev/null 2>&1 || true
  if ss -lnt 2>/dev/null | grep -qE ":${port}[[:space:]]"; then
    printf '  %-14s \033[1;33m端口 :%s 仍被占用\033[0m（可 fuser -k -KILL -n tcp %s）\n' "$session" "$port" "$port"
  else
    printf '  %-14s 已停止\n' "$session"
  fi
}

echo "停止 screen 会话"
for svc in "${TARGETS[@]}"; do stop_one "$svc"; done
