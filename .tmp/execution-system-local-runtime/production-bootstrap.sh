#!/bin/bash
# 从包含 docker-compose.yml 的内层项目目录运行。
# 兼容旧运维入口：直接应用当前全部流程；正常更新已由容器启动自动完成。
# 用法：bash ../.tmp/execution-system-local-runtime/production-bootstrap.sh
# Windows 更新只需原构建命令，见 docs/execution-v2/production-v2-upgrade.md。
set -euo pipefail
COMPOSE=(docker compose --env-file .env -f docker-compose.yml
         -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml)
"${COMPOSE[@]}" exec -T backend python -m app.execution.deploy_builtin_workflows --startup "$@"
