#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load repo .env if present (dotenv-style, no eval of commands)
load_env_file() {
  local env_file="$1"
  while IFS= read -r line || [ -n "$line" ]; do
    # skip blanks and comments
    case "$line" in
      "" | \#*) continue ;;
    esac
    # strip optional "export "
    if [[ "$line" == export\ * ]]; then
      line="${line#export }"
    fi
    # only accept KEY=VALUE
    if [[ "$line" != *"="* ]]; then
      continue
    fi
    local key="${line%%=*}"
    local val="${line#*=}"
    # strip surrounding quotes if present
    if [[ "$val" == \"*\" && "$val" == *\" ]]; then
      val="${val:1:-1}"
    elif [[ "$val" == \'*\' && "$val" == *\' ]]; then
      val="${val:1:-1}"
    fi
    export "$key=$val"
  done < "$env_file"
}

if [ -f "$ROOT/.env" ]; then
  load_env_file "$ROOT/.env"
fi

: "${FPL_MCP_API_KEY:?FPL_MCP_API_KEY is required}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"

VENV_DIR="${VENV_DIR:-}"
if [ -z "$VENV_DIR" ]; then
  if [ -d "$ROOT/apps/backend/.venv" ]; then
    VENV_DIR="$ROOT/apps/backend/.venv"
  elif [ -d "$ROOT/.venv" ]; then
    VENV_DIR="$ROOT/.venv"
  fi
fi

if [ -n "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/activate" ]; then
  # shellcheck disable=SC1090
  source "$VENV_DIR/bin/activate"
else
  echo "Venv not found. Create one at $ROOT/apps/backend/.venv or $ROOT/.venv, or set VENV_DIR."
  exit 1
fi

LEAGUE_ID="${LEAGUE_ID:-14204}"

echo "[dev] Refreshing cache..."
go -C "$ROOT/apps/mcp-server" run ./cmd/dev --league "$LEAGUE_ID" --gw-max 0

echo "[dev] Starting MCP server..."
go -C "$ROOT/apps/mcp-server" run ./fpl-server --addr :8080 --path /mcp &
GO_PID=$!

# Wait for MCP health (best effort)
if command -v curl >/dev/null 2>&1; then
  for _ in $(seq 1 20); do
    if curl -fsS http://localhost:8080/health >/dev/null 2>&1; then
      break
    fi
    sleep 0.5
  done
fi

export START_GO_SERVER=false
export CACHE_REFRESH_ON_START=false
export CACHE_REFRESH_DAILY=false
export PYTHONPATH="$ROOT/apps/backend"

echo "[dev] Starting backend UI..."
(
  cd "$ROOT/apps/backend"
  uvicorn backend.server:app --reload --port 8000
) &
UVICORN_PID=$!

cleanup() {
  echo "[dev] Stopping..."
  kill "$GO_PID" "$UVICORN_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

wait
