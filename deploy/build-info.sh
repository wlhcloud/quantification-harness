#!/usr/bin/env bash
# 部署时固化"这批代码是哪个版本"，供运行时读取（服务进程不允许 shell out）。
#
# 为什么需要它
#   stock_ml 的运行记录要回答"这次跑的是哪个提交"。服务进程按架构约定不能调 git
#   （见 test_script_layout.py::test_services_never_shell_out），所以版本信息必须在
#   部署/升级时写下来。运行时只读文件，不做任何外部调用。
#
# 产物：<项目根>/.build-info.json
#   {"commit": "...", "short": "...", "dirty": true|false|null, "generatedAt": "...", "source": "git|env"}
#
# 用法：
#   bash deploy/build-info.sh            # 自动探测
#   QUANT_BUILD_COMMIT=<sha> QUANT_BUILD_DIRTY=0 bash deploy/build-info.sh   # 无 git 时手工指定
#
# 已在 install.sh 与 screen-up.sh 中调用；升级代码后重跑 screen-up.sh 即会刷新。
set -euo pipefail

DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="${QUANT_PROJECT_ROOT:-$(cd "$DEPLOY_DIR/.." && pwd)}"
OUT="$PROJECT_ROOT/.build-info.json"

# 显式传入优先（例如从 tarball 部署、现场没有 git 元数据）。
COMMIT="${QUANT_BUILD_COMMIT:-}"
DIRTY_RAW="${QUANT_BUILD_DIRTY:-}"
SOURCE="env"

if [[ -z "$COMMIT" ]] && [[ -d "$PROJECT_ROOT/.git" ]] && command -v git >/dev/null 2>&1; then
  COMMIT="$(git -C "$PROJECT_ROOT" rev-parse HEAD 2>/dev/null || true)"
  if [[ -z "$DIRTY_RAW" ]]; then
    if [[ -n "$(git -C "$PROJECT_ROOT" status --porcelain 2>/dev/null)" ]]; then
      DIRTY_RAW="1"
    else
      DIRTY_RAW="0"
    fi
  fi
  SOURCE="git"
fi

# 统一成 JSON 的三态：true / false / null（null = 未知，不能当 clean）。
case "${DIRTY_RAW,,}" in
  1|true|yes|dirty) DIRTY_JSON="true" ;;
  0|false|no|clean) DIRTY_JSON="false" ;;
  *)                DIRTY_JSON="null" ;;
esac

if [[ -z "$COMMIT" ]]; then
  COMMIT_JSON="null"
  echo "build-info: 无法确定提交号（没有 git 元数据，也未传 QUANT_BUILD_COMMIT）" >&2
else
  COMMIT_JSON="\"$COMMIT\""
fi
SHORT="$([[ -n "$COMMIT" ]] && printf '%s' "${COMMIT:0:7}" || printf 'null')"
SHORT_JSON="$([[ -n "$COMMIT" ]] && printf '"%s"' "$SHORT" || printf 'null')"

cat > "$OUT" <<EOF
{
  "commit": $COMMIT_JSON,
  "short": $SHORT_JSON,
  "dirty": $DIRTY_JSON,
  "generatedAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "source": "$SOURCE"
}
EOF

echo "build-info: commit=${SHORT:-unknown} dirty=${DIRTY_JSON} -> $OUT"
