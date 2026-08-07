#!/usr/bin/env bash
set -euo pipefail

SCHEMABRIDGE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCHEMABRIDGE_ROOT"

# shellcheck disable=SC1091
source infra/datahub/versions.env

SCHEMABRIDGE_MCP_ENV="$SCHEMABRIDGE_ROOT/.local/datahub/mcp.env"
if [[ ! -f "$SCHEMABRIDGE_MCP_ENV" ]]; then
  printf '%s\n' 'Missing .local/datahub/mcp.env; provision the read-only identity first.' >&2
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$SCHEMABRIDGE_MCP_ENV"
set +a

export TOOLS_IS_MUTATION_ENABLED=false
export SAVE_DOCUMENT_TOOL_ENABLED=false
export DATAHUB_MCP_DOCUMENT_TOOLS_DISABLED=true
export DATAHUB_TELEMETRY_ENABLED=false
export FASTMCP_LOG_LEVEL=WARNING
export LOGURU_LEVEL=WARNING
export UV_CACHE_DIR="$SCHEMABRIDGE_ROOT/.local/uv-cache"
export UV_TOOL_DIR="$SCHEMABRIDGE_ROOT/.local/uv-tools"
export UV_PYTHON_INSTALL_DIR="$SCHEMABRIDGE_ROOT/.local/uv-python"

exec "$SCHEMABRIDGE_ROOT/.venv/bin/uv" tool run \
  --python 3.11 \
  --from "mcp-server-datahub==$DATAHUB_MCP_VERSION" \
  mcp-server-datahub --transport stdio
