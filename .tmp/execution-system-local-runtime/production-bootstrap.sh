#!/bin/bash
# 从包含 docker-compose.yml 的内层项目目录运行。
# 默认预演 v1 -> v2 接替，不再使用旧 slug 白名单修改启停状态。
# 用法：bash ../.tmp/execution-system-local-runtime/production-bootstrap.sh [--operator 用户名] [--apply]
# Windows PowerShell 等价命令和备份步骤见 docs/execution-v2/production-v2-upgrade.md。
set -euo pipefail
COMPOSE=(docker compose --env-file .env -f docker-compose.yml
         -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml)
"${COMPOSE[@]}" ps
"${COMPOSE[@]}" exec -T backend fc-match '宋体'
"${COMPOSE[@]}" exec -T execution-worker fc-match '宋体'
"${COMPOSE[@]}" exec -T backend python -m app.execution.deploy_builtin_workflows --sync-existing "$@"
