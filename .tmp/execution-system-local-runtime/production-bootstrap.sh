#!/bin/bash
# 生产初始化/验收脚本（仅开放 5 个工作流）。在仓库根 textile-device-monitor/ 下运行：
#   bash ../.tmp/execution-system-local-runtime/production-bootstrap.sh
#
# 前置：compose 已用生产 .env 启动，postgres/backend/execution-worker/frontend healthy。
# 本脚本不含也不读取任何凭据；SECRET 类配置在 .env 中由管理员维护。
set -euo pipefail

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.execution.yml
         -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml)

echo '==> 1/4 服务健康状态'
"${COMPOSE[@]}" ps

echo '==> 2/4 收敛工作流开放范围（白名单，幂等）'
# 仅开放：regenerated-fiber-count-method / regenerated-fiber-area-method /
#         electron-microscopy-gbt36422 / electron-cross-section-gbt36422 /
#         paper-fiber-gbt4688-2020-qualitative
# 其余（含后续代码新增播种的工作流）一律禁用，避免未验收流程意外暴露。
"${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" <<'SQL'
UPDATE execution_workflows
   SET is_enabled = (slug IN (
     'regenerated-fiber-count-method',
     'regenerated-fiber-area-method',
     'electron-microscopy-gbt36422',
     'electron-cross-section-gbt36422',
     'paper-fiber-gbt4688-2020-qualitative'
   ));
SELECT slug, name, is_enabled FROM execution_workflows ORDER BY slug;
SQL

echo '==> 3/4 容器内宋体检查（微观形貌/横截面原始记录必须）'
"${COMPOSE[@]}" exec -T backend fc-match '宋体'
"${COMPOSE[@]}" exec -T execution-worker fc-match '宋体'
echo '期望输出含 simsun.ttc: "SimSun" "Regular"'

echo '==> 4/4 执行系统存储根与项目匹配规则'
"${COMPOSE[@]}" exec -T postgres psql -U "${POSTGRES_USER:?}" -d "${POSTGRES_DB:?}" \
  -c "SELECT root_id, path, is_available FROM execution_storage_roots ORDER BY root_id;" \
  -c "SELECT rule_key, revision, updated_at FROM execution_project_rules ORDER BY rule_key;"

cat <<'NEXT'

剩余人工步骤（含机密，不在本脚本内）：
  1. 浏览器打开 http://<主机>/execution ，用 EXECUTION_BOOTSTRAP_ADMIN_USERNAME
     首次引导的管理员登录（引导后从 .env 移除该两项并重启 backend/worker）。
  2. 为每位检验员创建执行系统账号与角色。
  3. 在“凭据”页为写旧系统的账号登记 legacy_inspection 凭据（需要
     EXECUTION_CREDENTIAL_KEY 固定不变，否则已存凭据不可读）。
  4. Windows Bridge 主机：安装 textile-execution-bridge-setup-<version>.exe，
     填 config\BridgeConfig.psd1 与 config\bridge.env，跑
     Test-BridgeInstallation.ps1，再 Register-BridgeScheduledTasks.ps1。
  5. 用只读编号（如 26W006701）验证任务快照刷新命中项目名称与 GB/T 4688-2020。
NEXT
