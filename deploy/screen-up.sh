#!/usr/bin/env bash
# 用 screen 后台拉起 Python 服务（不使用 systemd）。
#
#   bash deploy/screen-up.sh            # 全部三个
#   bash deploy/screen-up.sh engine     # 只起 quant-engine
#
# screen 会话名与端口一致：quant-sync / quant-engine / data-query
# 日志写到 logs/<会话名>.log（screen 的窗口输出直接重定向，不用 screen -L 以免依赖编译选项）
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

[[ -x "$PYTHON_BIN" ]] || { echo "缺少 conda 环境: $PYTHON_BIN（先跑 deploy/install.sh）" >&2; exit 3; }
mkdir -p "$PROJECT_ROOT/logs"

# 启动前刷新构建信息：服务进程按架构约定不允许 shell out，运行记录里的"代码版本"
# 只能来自这里写下的 .build-info.json。放在启动**之前**，才能保证记录的就是
# 本批即将加载进内存的代码。
if [[ -x "$PROJECT_ROOT/deploy/build-info.sh" ]]; then
  "$PROJECT_ROOT/deploy/build-info.sh" || echo "build-info.sh 失败（代码版本将记为未知）" >&2
else
  echo "提示: 缺少 deploy/build-info.sh，运行记录中的代码版本会记为未知" >&2
fi

start_one() {
  local svc="$1" session="${SESSION[$1]}" log="$PROJECT_ROOT/logs/${SESSION[$1]}.log"
  if screen -list 2>/dev/null | grep -qE "[0-9]+\.${session}[[:space:]]"; then
    printf '  %-14s 已在运行（要重启先 screen-down.sh）\n' "$session"
    return 0
  fi
  {
    printf '\n===== %s start %s =====\n' "$session" "$(date '+%F %T')"
  } >> "$log"
  # run-service.sh 里是 exec，所以 screen 窗口的直接子进程就是 uvicorn。
  screen -dmS "$session" bash -c "exec '$PROJECT_ROOT/deploy/run-service.sh' '$svc' >> '$log' 2>&1"
  printf '  %-14s 已启动 :%s  (log: %s)\n' "$session" "${PORT[$svc]}" "$log"
}

echo "启动 screen 会话 (项目根: $PROJECT_ROOT)"
for svc in "${TARGETS[@]}"; do start_one "$svc"; done

echo
sleep 4
for svc in "${TARGETS[@]}"; do
  session="${SESSION[$svc]}"; port="${PORT[$svc]}"
  body="$(curl -s --max-time 5 "http://127.0.0.1:$port/api/v1/health" || true)"
  if [[ -n "$body" ]]; then
    printf '  %-14s :%s  %s\n' "$session" "$port" "$body"
  else
    printf '  %-14s :%s  \033[1;31m无响应\033[0m → tail -n 40 %s/logs/%s.log\n' \
      "$session" "$port" "$PROJECT_ROOT" "$session"
  fi
done
