# FPL Draft Agent

An end-to-end Fantasy Premier League Draft toolkit with a Go MCP server, a Python backend, and a lightweight web UI.

**Features**
- Go MCP server backed by local cached FPL data and derived summaries.
- Python backend for chat, reports, and scheduling.
- Web UI with chat, tool-call visibility, and one-click reports.
- Report generation with Markdown + JSON outputs.
- RAG-style memory over recent reports for “what changed” questions.

**MCP Tools (Grouped)**
- League & standings: `league_summary`, `standings`, `league_entries`
- Matchups & performance: `matchup_breakdown`, `lineup_efficiency`, `manager_schedule`, `manager_streak`
- Transactions & waivers: `transactions`, `waiver_targets`, `waiver_recommendations`, `ownership_scarcity`
- Players & fixtures: `fixtures`, `fixture_difficulty`, `player_form`, `player_lookup`
- Manager utilities: `manager_lookup`

**Report Types**
- Waiver recommendations (personalized, weighted by fixtures/form/points/xG).
- League summary (weekly recap with matchups and best starters).
- Transactions summary (waivers, free agents, trades).
- Starting XI recommendations (fixture difficulty + form + xGI + minutes).

**Project Layout**
- `apps/mcp-server/` — Go MCP server (tools + HTTP endpoint)
- `apps/backend/` — Python API + scheduler + CLI
- `apps/web/` — Web UI (chat + reports)
- `data/` — Raw/derived data (local only, not committed)
- `reports/` — Generated reports (local only, not committed)

**How It Works**
1. Fetch & cache (Go)
   - Command: `go run ./apps/mcp-server/cmd/dev --league <id> --gw-max 0`
   - Outputs: `data/raw/` and `data/derived/`
2. Serve MCP tools (Go)
   - Command: `go run ./apps/mcp-server/fpl-server --addr :8080 --path /mcp`
   - Reads: `data/raw/` and `data/derived/`
   - Exposes: tool endpoints over HTTP at `/mcp`
3. Backend orchestration (Python)
   - Runs HTTP + WebSocket server
   - Calls MCP tools to answer chat + generate reports
   - Writes reports to `reports/gw_<gw>/...` and serves them at `/reports`
4. UI (Web)
   - Calls Python backend for chat + report runs
   - Shows tool-call trace + links to generated reports (relative to backend origin)

**Prereqs**
- Go 1.23+
- Python 3.11+

**Quickstart**
1. Fetch data (local cache)

```bash
go run ./apps/mcp-server/cmd/dev --league 14204 --gw-max 0
```

2. Start MCP server

```bash
export FPL_MCP_API_KEY="your-strong-secret"
go run ./apps/mcp-server/fpl-server --addr :8080 --path /mcp
```

3. Start Python backend + UI

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r apps/backend/requirements.txt

export FPL_MCP_API_KEY="your-strong-secret"
# optional
export OPENAI_API_KEY="your-openai-key"

PYTHONPATH=apps/backend uvicorn backend.server:app --reload --port 8000
```

Open:
- UI: http://localhost:8000/ui
- Reports: http://localhost:8000/reports

**Reports**
Reports are saved to:
```
reports/gw_<gw>/
```
The backend serves the `reports/` directory at `/reports`, and the UI links to `/reports/<path>`.

**Scheduler**
```bash
PYTHONPATH=apps/backend python -m backend.scheduler
```

Timezone: America/New_York (configurable via `REPORTS_TZ`).

**CLI**
```bash
PYTHONPATH=apps/backend python -m backend.cli --type waivers --gw 0
PYTHONPATH=apps/backend python -m backend.cli --type league_summary --gw 0
PYTHONPATH=apps/backend python -m backend.cli --type starting_xi --gw 0
PYTHONPATH=apps/backend python -m backend.cli --type trades --gw 0
```

`--gw 0` auto-resolves to current GW (waivers/starting XI use current+1).

**Config**
- Copy `.env.example` to `.env` and fill in API keys.
- Set `REPO_ROOT` if you run commands outside the repo root.
- `REPORTS_DIR` and `REPORTS_TZ` control report output path and timezone.

**Notes**
- The MCP server reads from local `data/` only. Use the dev fetcher to refresh.
- Cache contract: the Python backend mostly uses MCP tools for league data, but it also reads local cache files directly for bootstrap fixtures/xGI and points lookups.
- `data/` and `reports/` are intentionally ignored by git.
