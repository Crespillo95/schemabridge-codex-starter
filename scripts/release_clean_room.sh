#!/usr/bin/env bash
set -euo pipefail

SCHEMABRIDGE_RELEASE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCHEMABRIDGE_RELEASE_DATABASE_URL="postgresql://schemabridge_reader:schemabridge_reader@127.0.0.1:55433/schemabridge"
cd "$SCHEMABRIDGE_RELEASE_ROOT"

SCHEMABRIDGE_ALLOW_UNCOMMITTED=0
if [[ "${1:-}" == "--allow-uncommitted" ]]; then
  SCHEMABRIDGE_ALLOW_UNCOMMITTED=1
elif [[ -n "${1:-}" ]]; then
  printf '%s\n' 'Usage: scripts/release_clean_room.sh [--allow-uncommitted]' >&2
  exit 2
fi

for SCHEMABRIDGE_RELEASE_TOOL in docker git make curl; do
  if ! command -v "$SCHEMABRIDGE_RELEASE_TOOL" >/dev/null 2>&1; then
    printf 'Required release tool is unavailable: %s\n' "$SCHEMABRIDGE_RELEASE_TOOL" >&2
    exit 1
  fi
done

if [[ "$SCHEMABRIDGE_ALLOW_UNCOMMITTED" -eq 0 ]]; then
  git rev-parse --verify HEAD >/dev/null
  if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
    printf '%s\n' 'Strict release proof requires a clean working tree.' >&2
    exit 1
  fi
else
  printf '%s\n' 'WARNING: development audit mode; results are not release-commit evidence.'
fi

printf '%s\n' '[1/8] Clean Python environment and complete dependency install'
bash scripts/bootstrap.sh
make install
.venv/bin/python -m pip check

printf '%s\n' '[2/8] Clean synthetic PostgreSQL reset and health'
make demo-reset
make demo-health

printf '%s\n' '[3/8] Clean DataHub reset, ingest, identities, and read checks'
make datahub-reset
make datahub-health
make datahub-init-admin
make datahub-ingest
make datahub-provision-mcp
make datahub-provision-writer
make datahub-catalog-check
make datahub-mcp-check

printf '%s\n' '[4/8] Quality, integration, and acceptance suites'
make check
make test-integration
make test-acceptance
make coverage

printf '%s\n' '[5/8] Deterministic evaluation without rewriting checked-in evidence'
DATABASE_URL="$SCHEMABRIDGE_RELEASE_DATABASE_URL" .venv/bin/python -m schemabridge.entrypoints.cli.main evaluate \
  --output reports/evaluation.json \
  --markdown reports/evaluation-release.md

printf '%s\n' '[6/8] Headless Streamlit health smoke'
SCHEMABRIDGE_UI_LOG="${TMPDIR:-/tmp}/schemabridge-m16-streamlit.log"
DATABASE_URL="$SCHEMABRIDGE_RELEASE_DATABASE_URL" .venv/bin/streamlit run \
  src/schemabridge/entrypoints/streamlit/app.py \
  --server.headless true \
  --server.address 127.0.0.1 \
  --server.port 8516 >"$SCHEMABRIDGE_UI_LOG" 2>&1 &
SCHEMABRIDGE_UI_PID=$!
cleanup_ui() {
  if kill -0 "$SCHEMABRIDGE_UI_PID" 2>/dev/null; then
    kill "$SCHEMABRIDGE_UI_PID"
    wait "$SCHEMABRIDGE_UI_PID" || true
  fi
}
trap cleanup_ui EXIT
SCHEMABRIDGE_UI_READY=0
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1:8516/_stcore/health 2>/dev/null | grep -qx 'ok'; then
    SCHEMABRIDGE_UI_READY=1
    break
  fi
  sleep 1
done
if [[ "$SCHEMABRIDGE_UI_READY" -ne 1 ]]; then
  printf 'Streamlit health smoke failed; inspect %s\n' "$SCHEMABRIDGE_UI_LOG" >&2
  exit 1
fi
cleanup_ui
trap - EXIT

printf '%s\n' '[7/8] DataHub persistence after service restart'
make datahub-restart
make datahub-health
make datahub-catalog-check
make datahub-mcp-check
SCHEMABRIDGE_TEST_DATABASE_URL="$SCHEMABRIDGE_RELEASE_DATABASE_URL" .venv/bin/pytest \
  tests/integration/test_datahub_writeback.py \
  tests/integration/test_datahub_relationships.py \
  tests/integration/test_datahub_query_recipes.py
.venv/bin/schemabridge join-published --json

printf '%s\n' '[8/8] Release identity, architecture, secret, license, and link scans'
SCHEMABRIDGE_AUDIT_ARGS=(--check-external)
if [[ "$SCHEMABRIDGE_ALLOW_UNCOMMITTED" -eq 0 ]]; then
  SCHEMABRIDGE_AUDIT_ARGS+=(--require-release)
fi
.venv/bin/python scripts/release_audit.py "${SCHEMABRIDGE_AUDIT_ARGS[@]}"
.venv/bin/python scripts/validate_starter.py
bash -n scripts/bootstrap.sh scripts/release_clean_room.sh scripts/datahub.sh
git diff --check

printf '%s\n' 'M16 clean-room command completed.'
