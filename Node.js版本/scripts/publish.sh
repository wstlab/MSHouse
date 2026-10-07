#!/usr/bin/env bash
#
# 一键发布到 GitHub：wstlab/MSHouse
#
# 用法：
#   bash scripts/publish.sh [仓库全名] [分支]
#
# 例：
#   bash scripts/publish.sh                        # 默认 wstlab/MSHouse / main
#   bash scripts/publish.sh wstlab/MSHouse main
#
# Token 来源（按优先级）：
#   1. 环境变量 GITHUB_TOKEN / GH_TOKEN（外部注入，例如 OAuth Device Flow）
#   2. CodeBuddy 连接器脚本 get_token.sh
#
# 全程以环境变量引用 Token，不打印、不写入仓库；
# 推送完成后会立即把 remote 中的凭据抹掉，避免 Token 残留在 .git/config。

set -euo pipefail

REPO="${1:-wstlab/MSHouse}"
BRANCH="${2:-main}"

TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

if [ -z "$TOKEN" ]; then
  SKILL_DIR="${HOME}/.codebuddy/skills/github-connector"
  TOKEN_SH="${SKILL_DIR}/scripts/get_token.sh"
  if [ ! -f "$TOKEN_SH" ]; then
    echo "✗ 未找到连接器脚本：$TOKEN_SH" >&2
    exit 1
  fi
  echo "→ 从连接器获取 GitHub Token ..."
  # shellcheck disable=SC1090
  source "$TOKEN_SH" github
  TOKEN="${GITHUB_TOKEN:-}"
fi

if [ -z "$TOKEN" ]; then
  echo "✗ 未能获取 Token（连接器鉴权未通过）" >&2
  exit 1
fi

echo "→ 校验身份 ..."
LOGIN=$(curl -s -H "Authorization: Bearer ${TOKEN}" https://api.github.com/user |
  grep -m1 '"login"' | sed 's/.*"login"[[:space:]]*:[[:space:]]*"//; s/".*//')
echo "  已登录为：${LOGIN:-<未知>}"

cd "$(dirname "$0")/.."

# 推送结束后（无论成功失败）抹掉 remote 里的 Token
cleanup() { git remote set-url origin "https://github.com/${REPO}.git" 2>/dev/null || true; }
trap cleanup EXIT

echo "→ 设置远端：https://github.com/${REPO}.git"
git remote remove origin 2>/dev/null || true
git remote add origin "https://oauth2:${TOKEN}@github.com/${REPO}.git"

echo "→ 推送 ${BRANCH} ..."
git push -u origin "${BRANCH}"

echo
echo "✓ 已发布：https://github.com/${REPO}"
