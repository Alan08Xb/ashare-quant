#!/usr/bin/env bash
# =====================================================================
# NAS 东财拉取任务：每个交易日 10:00 由 NAS Hermes cron 调用。
#   1) git pull 最新代码
#   2) python -m ashare_quant pull  → 东财抓取 + 落库 data/dailydb/ + data/static/ + data/history(本地)
#   3) 拉取成功 → 提交推送 data/dailydb + data/static
#   4) 无论成败 → workflow_dispatch 触发 GitHub Actions 分析（失败时分析侧自动降级新浪+baostock）
# 依赖：NAS 上已装 python3 + 本项目 venv + git；有 workflow 权限的 GH_TOKEN。
# 用法：REPO_DIR=<路径> GH_TOKEN=<token> bash scripts/nas_pull.sh
# =====================================================================
set -uo pipefail

REPO_DIR="${REPO_DIR:-/vol1/docker/ashare-quant}"
VENV_PY="${VENV_PY:-$REPO_DIR/.venv/bin/python}"
REPO="${REPO_OWNER:-Alan08Xb}/ashare-quant"
BRANCH="${BRANCH:-main}"
GH_TOKEN="${GH_TOKEN:-}"        # 需要 workflow 权限的 PAT（Settings→Developer settings→PAT，勾选 workflow）

echo "[nas_pull] $(date '+%F %T %Z') start"
cd "$REPO_DIR" || { echo "仓库目录不存在: $REPO_DIR"; exit 1; }

# 1) 同步代码
git pull --rebase --autostash >/dev/null 2>&1 || true

# 2) 东财拉取落库（429/5xx 内部按 PULL_RETRY_INTERVAL×PULL_RETRY_TIMES 重试）
echo "[nas_pull] 运行东财拉取任务..."
"$VENV_PY" -m ashare_quant pull
PULL_RC=$?

# 3) 拉取成功 → 提交推送仓库（data/history 已 gitignored，只在本地积累回测，不提交）
if [ "$PULL_RC" -eq 0 ]; then
  git config user.name "nas-pull[bot]" >/dev/null 2>&1
  git config user.email "nas@localhost" >/dev/null 2>&1
  git add data/dailydb data/static 2>/dev/null || true
  if ! git diff --cached --quiet; then
    git commit -m "东财数据仓库 $(date +%F)" >/dev/null 2>&1 && git push || echo "[nas_pull] push 失败(稍后手动)"
  fi
fi

# 4) 触发分析任务（无论拉取成败都触发，失败时 Actions 走降级）
API="https://api.github.com/repos/$REPO/actions/workflows/daily.yml/dispatches"
if [ -n "$GH_TOKEN" ]; then
  curl -fsS -X POST \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "Content-Type: application/json" \
    "$API" -d "{\"ref\":\"$BRANCH\"}" \
    && echo "[nas_pull] 已触发分析任务 dispatch" \
    || echo "[nas_pull] dispatch 失败"
else
  echo "[nas_pull] 未设置 GH_TOKEN，跳过 dispatch（可改用已登录的 gh: gh workflow run daily-a-share-quant）"
fi

echo "[nas_pull] done, pull_rc=$PULL_RC"
exit "$PULL_RC"   # 0=库已更新；2=拉取失败(分析侧已降级)