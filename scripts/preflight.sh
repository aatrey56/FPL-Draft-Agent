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

echo "--- Python checks (uv) ---"
if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found — install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
fi
(
  cd apps/backend
  uv sync --locked
  uv run python -m compileall -q backend
  uv run ruff check .
  uv run pytest tests
)
