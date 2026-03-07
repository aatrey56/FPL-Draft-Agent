#!/usr/bin/env bash
set -euo pipefail

echo "--- Go checks ---"
(
  cd apps/mcp-server
  go vet ./...
  go test ./...
  UNFMT=$(gofmt -l .)
  if [ -n "$UNFMT" ]; then echo "gofmt needed: $UNFMT"; exit 1; fi
)

# Python checks
PYTHON_BIN="${PYTHON_BIN:-}"
if [ -z "$PYTHON_BIN" ]; then
  if command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  else
    echo "python or python3 not found on PATH"
    exit 1
  fi
fi

ensure_module() {
  local module="$1"
  local pip_name="${2:-$1}"
  if ! "$PYTHON_BIN" - <<PY
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec("$module") else 1)
PY
  then
    echo "Installing missing Python tool: ${pip_name}"
    "$PYTHON_BIN" -m pip install "${pip_name}"
  fi
}

ensure_module "ruff"
ensure_module "pytest"
ensure_module "dotenv" "python-dotenv"

echo "--- Python checks ---"

find apps/backend/backend -name "*.py" -not -path "*/.venv/*" -print0 \
  | xargs -0 "$PYTHON_BIN" -m py_compile
"$PYTHON_BIN" -m ruff check apps/backend
PYTHONPATH=apps/backend "$PYTHON_BIN" -m pytest apps/backend/tests
