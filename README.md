# FPL Draft Agent

An end-to-end Fantasy Premier League Draft toolkit:

- **Go MCP server** with league tools and a personalized waiver recommendation tool.
- **Python backend** that calls the MCP server, runs automated reports, and serves a UI.
- **Web UI** for chat + tool visibility + one-click reports.

## Project Layout

- `fpl-server/` — Go MCP server (tools + HTTP endpoint)
- `internal/` — Go logic (summaries, points, ledger, fetcher)
- `cmd/` — CLI utilities (data fetcher, dev tools)
- `backend/` — Python API + scheduler + CLI
- `web/` — Simple UI (chat + reports)
- `data/` — Raw/derived data (local only, not committed)
- `reports/` — Generated reports (local only, not committed)

## Prereqs

- Go 1.21+
- Python 3.11+

## Setup

### 1) Fetch data (local cache)
```bash
go run ./cmd/dev --league 14204 --gw-max 0
```

### 2) Start MCP server
```bash
export FPL_MCP_API_KEY="your-strong-secret"
go run ./fpl-server --addr :8080 --path /mcp
```

### 3) Start Python backend + UI
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export FPL_MCP_API_KEY="your-strong-secret"
# optional
export OPENAI_API_KEY="your-openai-key"

uvicorn backend.server:app --reload --port 8000
```

Open:
- UI: http://localhost:8000/ui
- Reports: http://localhost:8000/reports

## Reports

Reports are saved to:
```
reports/gw_<gw>/
```

## Scheduler

```bash
python -m backend.scheduler
```

Timezone: America/New_York (configurable via `REPORTS_TZ`).

## CLI

```bash
python -m backend.cli --type waivers --gw 0
python -m backend.cli --type league_summary --gw 0
python -m backend.cli --type starting_xi --gw 0
python -m backend.cli --type trades --gw 0
```

`--gw 0` auto-resolves to current GW (waivers/starting XI use current+1).

## CI Checks

- Go: `gofmt` check, `go vet`, `go test`, and `go mod tidy` (diff check).
- Python: byte-compile, `ruff check`, and `pytest`.

Ruff check runs the Ruff linter on your Python codebase. It flags common bugs (unused imports, undefined names), style issues, and many “lint” rules (similar to flake8/isort/etc.). It can also auto-fix certain issues if you run it with `--fix`. The rules come from your `.ruff.toml`.

## Notes

- The MCP server reads from local `data/` only. Use `cmd/dev` to refresh.
- `data/` and `reports/` are intentionally ignored by git.
