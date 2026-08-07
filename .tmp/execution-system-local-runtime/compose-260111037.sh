#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h:h}"
SECRETS="$ROOT/.tmp/execution-system-secrets/inspection-systems.env"
LEGACY_PASSWORD="$(
  awk -F= '$1 == "LEGACY_FIBRECHECK_PASSWORD" { print substr($0, index($0, "=") + 1); exit }' "$SECRETS"
)"
if [[ -z "$LEGACY_PASSWORD" ]]; then
  print -u2 'Local legacy credential is unavailable'
  exit 2
fi
export EXECUTION_BRIDGE_TOKEN="$(
  printf %s "execution-bridge-local-test|260111037|$LEGACY_PASSWORD" \
    | shasum -a 256 \
    | awk '{print $1}'
)"
unset LEGACY_PASSWORD

exec docker compose \
  --env-file "$ROOT/textile-device-monitor/.env" \
  --env-file "$ROOT/.tmp/execution-system-secrets/inspection-systems.env" \
  --env-file "$ROOT/.tmp/execution-system-local-runtime/local.env" \
  --env-file "$ROOT/.tmp/execution-system-local-runtime/real-write-260111037.env" \
  -f "$ROOT/textile-device-monitor/docker-compose.yml" \
  -f "$ROOT/.tmp/execution-system-local-runtime/docker-compose.local.yml" \
  -f "$ROOT/.tmp/execution-system-local-runtime/docker-compose.company-lan.yml" \
  "$@"
