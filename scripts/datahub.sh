#!/usr/bin/env bash
set -euo pipefail

SCHEMABRIDGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCHEMABRIDGE_ROOT"

# shellcheck disable=SC1091
source infra/datahub/versions.env

SCHEMABRIDGE_UV="$SCHEMABRIDGE_ROOT/.venv/bin/uv"
SCHEMABRIDGE_DATAHUB_DIR="$SCHEMABRIDGE_ROOT/.local/datahub"
SCHEMABRIDGE_DATAHUB_COMPOSE="$SCHEMABRIDGE_DATAHUB_DIR/docker-compose.quickstart.yml"
SCHEMABRIDGE_DATAHUB_QUICKSTART_ENV="$SCHEMABRIDGE_DATAHUB_DIR/quickstart.env"
export UV_CACHE_DIR="$SCHEMABRIDGE_ROOT/.local/uv-cache"
export UV_TOOL_DIR="$SCHEMABRIDGE_ROOT/.local/uv-tools"
export UV_PYTHON_INSTALL_DIR="$SCHEMABRIDGE_ROOT/.local/uv-python"

require_tooling() {
  if [[ ! -x "$SCHEMABRIDGE_UV" ]]; then
    printf '%s\n' 'Missing .venv/bin/uv; install the project datahub extra first.' >&2
    exit 1
  fi
}

datahub_cli() {
  "$SCHEMABRIDGE_UV" tool run \
    --python 3.11 \
    --from "acryl-datahub[postgres]==$DATAHUB_CLI_VERSION" \
    datahub "$@"
}

prepare() {
  .venv/bin/python scripts/ensure_datahub_secrets.py "$SCHEMABRIDGE_DATAHUB_QUICKSTART_ENV"
  set -a
  # shellcheck disable=SC1090
  source "$SCHEMABRIDGE_DATAHUB_QUICKSTART_ENV"
  set +a
  export DATAHUB_VERSION="$DATAHUB_CORE_VERSION"
  export UI_INGESTION_DEFAULT_CLI_VERSION="$DATAHUB_CLI_VERSION"
  .venv/bin/python scripts/prepare_datahub_quickstart.py \
    --url "$DATAHUB_QUICKSTART_COMPOSE_URL" \
    --sha256 "$DATAHUB_QUICKSTART_COMPOSE_SHA256" \
    --output "$SCHEMABRIDGE_DATAHUB_COMPOSE"
}

start() {
  prepare
  datahub_cli docker quickstart \
    --version "$DATAHUB_CORE_VERSION" \
    --quickstart-compose-file "$SCHEMABRIDGE_DATAHUB_COMPOSE" \
    --dump-logs-on-failure
}

health() {
  curl -fsS http://127.0.0.1:8080/health >/dev/null
  curl -fsS http://127.0.0.1:9002/ >/dev/null
  SCHEMABRIDGE_GMS_CONTAINER="$(
    docker ps \
      --filter label=com.docker.compose.project=datahub \
      --filter label=com.docker.compose.service=datahub-gms-quickstart \
      --filter label=io.datahubproject.datahub.component=gms \
      --format '{{.ID}}'
  )"
  if [[ -z "$SCHEMABRIDGE_GMS_CONTAINER" ]]; then
    printf '%s\n' 'DataHub GMS container was not found.' >&2
    exit 1
  fi
  if [[ "$SCHEMABRIDGE_GMS_CONTAINER" == *$'\n'* ]]; then
    printf '%s\n' 'Multiple DataHub GMS containers matched the release stack.' >&2
    exit 1
  fi
  docker inspect "$SCHEMABRIDGE_GMS_CONTAINER" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' |
    grep -qx 'LOGICAL_MODELS_ENABLED=true'
  docker inspect "$SCHEMABRIDGE_GMS_CONTAINER" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' |
    grep -qx 'METADATA_SERVICE_AUTH_ENABLED=true'
  printf '%s\n' 'DataHub GMS, UI, logical models, and metadata authentication are healthy.'
}

init_admin() {
  # Fresh pinned GMS authorization policy caches refresh every 120 seconds.
  # Poll just beyond that interval while the official CLI remains fail-closed.
  local SCHEMABRIDGE_DATAHUB_INIT_ATTEMPTS=31
  local SCHEMABRIDGE_DATAHUB_INIT_DELAY_SECONDS=5
  local SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT
  for ((
    SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT = 1;
    SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT <= SCHEMABRIDGE_DATAHUB_INIT_ATTEMPTS;
    SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT++
  )); do
    if datahub_cli init \
      --host http://127.0.0.1:8080 \
      --username datahub \
      --password datahub \
      --token-duration ONE_MONTH \
      --force; then
      return 0
    fi
    if [[ "$SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT" -eq "$SCHEMABRIDGE_DATAHUB_INIT_ATTEMPTS" ]]; then
      break
    fi
    printf 'DataHub admin initialization is not ready; retrying (%s/%s).\n' \
      "$SCHEMABRIDGE_DATAHUB_INIT_ATTEMPT" \
      "$SCHEMABRIDGE_DATAHUB_INIT_ATTEMPTS" >&2
    sleep "$SCHEMABRIDGE_DATAHUB_INIT_DELAY_SECONDS"
  done
  printf '%s\n' 'DataHub admin initialization did not become ready.' >&2
  return 1
}

ingest() {
  export POSTGRES_READER_PASSWORD="${POSTGRES_READER_PASSWORD:-schemabridge_reader}"
  export DATAHUB_GMS_URL="${DATAHUB_GMS_URL:-http://127.0.0.1:8080}"
  DATAHUB_GMS_TOKEN="$({ .venv/bin/python -c '
from pathlib import Path

import yaml

config = yaml.safe_load((Path.home() / ".datahubenv").read_text())
token = config.get("gms", {}).get("token")
if not isinstance(token, str) or not token:
    raise SystemExit("DataHub admin token is missing; run make datahub-init-admin.")
print(token, end="")
'; })"
  export DATAHUB_GMS_TOKEN
  datahub_cli ingest -c infra/datahub/ingestion/postgres.yml 2>&1 |
    .venv/bin/python scripts/sanitize_datahub_log.py
}

provision_mcp() {
  .venv/bin/python scripts/provision_datahub_mcp.py
}

provision_writer() {
  .venv/bin/python scripts/provision_datahub_writer.py
}

stop() {
  prepare
  datahub_cli docker quickstart \
    --version "$DATAHUB_CORE_VERSION" \
    --quickstart-compose-file "$SCHEMABRIDGE_DATAHUB_COMPOSE" \
    --stop
}

reset() {
  prepare
  docker compose \
    --profile quickstart \
    -f "$SCHEMABRIDGE_DATAHUB_COMPOSE" \
    -p datahub \
    down -v --remove-orphans
  start
}

restart() {
  stop
  start
}

usage() {
  printf '%s\n' 'Usage: scripts/datahub.sh {version|prepare|start|health|init-admin|ingest|provision-mcp|provision-writer|restart|reset|stop}'
}

require_tooling
case "${1:-}" in
  version) datahub_cli version ;;
  prepare) prepare ;;
  start) start ;;
  health) health ;;
  init-admin) init_admin ;;
  ingest) ingest ;;
  provision-mcp) provision_mcp ;;
  provision-writer) provision_writer ;;
  restart) restart ;;
  reset) reset ;;
  stop) stop ;;
  *) usage; exit 2 ;;
esac
