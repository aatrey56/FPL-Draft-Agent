#!/usr/bin/env bash
set -euo pipefail

# Go checks
(
  cd apps/mcp-server
  go vet ./...
  go test ./...
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
  if ! "$PYTHON_BIN" - <<PY
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec("$module") else 1)
PY
  then
    echo "Installing missing Python tool: ${module}"
    "$PYTHON_BIN" -m pip install "${module}"
  fi
}

ensure_module "ruff"
ensure_module "pytest"

# Syntax-check all Python sources, explicitly skipping any .venv tree.
# Using find+py_compile rather than compileall so we can exclude .venv without
# compileall traversing thousands of installed-package files.
find apps/backend -name "*.py" \
  -not -path "*/.venv/*" \
  -not -path "*/__pycache__/*" \
  -print0 \
  | xargs -0 "$PYTHON_BIN" -m py_compile

# Lint — pass --exclude explicitly so ruff skips .venv even when invoked
# from outside the apps/backend directory.
"$PYTHON_BIN" -m ruff check apps/backend --exclude "**/.venv/**"

PYTHONPATH=apps/backend "$PYTHON_BIN" -m pytest apps/backend/tests
