#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

select_python() {
  local candidate
  local candidates=(python3.13 python3.12 python3.11 python3 python)

  if [[ -n "${SCHEMABRIDGE_PYTHON:-}" ]]; then
    candidates=("$SCHEMABRIDGE_PYTHON")
  fi

  for candidate in "${candidates[@]}"; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys; raise SystemExit(0 if (3, 11) <= sys.version_info[:2] < (3, 14) else 1)' 2>/dev/null; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  printf '%s\n' 'Python >=3.11,<3.14 was not found. Set SCHEMABRIDGE_PYTHON to a supported interpreter.' >&2
  return 1
}

SCHEMABRIDGE_PYTHON_BIN="$(select_python)"
"$SCHEMABRIDGE_PYTHON_BIN" -V
make bootstrap PYTHON="$SCHEMABRIDGE_PYTHON_BIN"

printf '\nBootstrap complete. Next: run make check.\n'
