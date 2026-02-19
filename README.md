# FPL Draft Agent

An end-to-end **Fantasy Premier League Draft** toolkit.  Ask any question about your league in natural language — the agent calls the right data tools and returns a clear, structured answer.

The stack is:
- **Go MCP server** — exposes 22 tools backed by locally-cached FPL Draft API data
- **Python backend** — chat API, scheduled reports, and a WebSocket endpoint
- **Web UI** — chat interface with tool-call visibility and one-click report generation

---

## What it does

### Chat

Open the UI and ask questions in plain English:

- *"Show my waiver recommendations for GW 28"*
- *"What's our league table?"*
- *"How has Salah done each gameweek this season?"*
- *"Head to head record between Alpha FC and Beta United?"*
- *"Show me my season stats"*
- *"Who's had the best fixture run lately?"*
- *"What did people add and drop this week across the league?"*

The agent routes simple questions directly using a keyword table, and falls back to an LLM (GPT-4.1 by default) for complex multi-step queries.

### Scheduled reports

The scheduler generates Markdown + JSON reports on a configurable cron:

| Schedule | Reports generated |
|---|---|
| Tuesday 11:00 | League summary, waiver recommendations, trades summary |
| Friday 23:00 | Waiver recommendations, starting XI, waiver FA summary |

Reports are saved to `reports/gw_<N>/` and served at `/reports` in the browser.

### MCP Tools (22 total)

| Group | Tools |
|---|---|
| League & standings | `league_summary`, `standings`, `league_entries` |
| Matchups & performance | `matchup_breakdown`, `lineup_efficiency`, `manager_schedule`, `manager_streak`, `manager_season` |
| Transactions & waivers | `transactions`, `waiver_targets`, `waiver_recommendations`, `ownership_scarcity`, `transaction_analysis` |
| Players & fixtures | `fixtures`, `fixture_difficulty`, `player_form`, `player_lookup`, `player_gw_stats` |
| Manager utilities | `manager_lookup`, `current_roster`, `draft_picks`, `head_to_head` |

---

## How to run it

### Prerequisites

- **Go 1.23+** — `go version`
- **Python 3.11+** — `python3 --version`
- An **OpenAI API key** (optional; required for LLM-powered answers)
- Your **FPL Draft league ID** (visible in the URL on draft.premierleague.com)

### 1. Clone and configure

```bash
git clone https://github.com/aatrey56/FPL-Draft-Agent.git
cd FPL-Draft-Agent
cp .env.example .env
```

Edit `.env`:

```dotenv
LEAGUE_ID=14204          # replace with your league ID
ENTRY_ID=286192          # replace with your entry (team) ID
OPENAI_API_KEY=sk-...    # required for LLM answers
FPL_MCP_API_KEY=secret   # any strong random string
```

See `.env.example` for all options with explanations.

### 2. Fetch FPL data (Go)

This pulls data from the FPL Draft API into `data/raw/` and `data/derived/`.

```bash
go run ./apps/mcp-server/cmd/dev --league 14204 --gw-max 0
```

Replace `14204` with your league ID.  This takes ~30 seconds on a fast connection.

### 3. Start the MCP server (Go)

```bash
export FPL_MCP_API_KEY="secret"
go run ./apps/mcp-server/fpl-server --addr :8080 --path /mcp
```

The server starts on port 8080 and exposes all 22 tools at `/mcp`.

### 4. Start the Python backend + UI

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r apps/backend/requirements.txt

export FPL_MCP_API_KEY="secret"
export OPENAI_API_KEY="sk-..."   # optional

PYTHONPATH=apps/backend uvicorn backend.server:app --reload --port 8000
```

Open in your browser:
- **Chat UI:** http://localhost:8000/ui
- **Reports:** http://localhost:8000/reports

> **Tip:** Set `START_GO_SERVER=false` in `.env` if you manage the Go server separately. When `START_GO_SERVER=true` (default), the Python backend starts the Go server automatically.

### Using `.env` (recommended for development)

Instead of setting environment variables manually, put everything in `.env` at the repo root:

```dotenv
LEAGUE_ID=14204
ENTRY_ID=286192
FPL_MCP_API_KEY=secret
OPENAI_API_KEY=sk-...
START_GO_SERVER=true
CACHE_REFRESH_ON_START=true
```

Then just run:

```bash
PYTHONPATH=apps/backend uvicorn backend.server:app --reload --port 8000
```

The backend loads `.env` automatically on startup.

---

## CLI

Generate reports from the command line without the UI:

```bash
# Waiver recommendations
PYTHONPATH=apps/backend python -m backend.cli --type waivers --gw 0

# League summary
PYTHONPATH=apps/backend python -m backend.cli --type league_summary --gw 0

# Starting XI
PYTHONPATH=apps/backend python -m backend.cli --type starting_xi --gw 0

# Trades/transactions summary
PYTHONPATH=apps/backend python -m backend.cli --type trades --gw 0
```

`--gw 0` auto-resolves to the current gameweek (waiver/starting XI use GW+1).

---

## Scheduler (automated reports)

```bash
PYTHONPATH=apps/backend python -m backend.scheduler
```

Generates reports on a Tuesday/Friday cron.  Timezone defaults to `America/New_York`; override with `REPORTS_TZ=Europe/London` in `.env`.

---

## Project layout

```
apps/
  mcp-server/           Go MCP server
    fpl-server/         Tool handlers + HTTP server
    cmd/dev/            FPL data fetcher
  backend/              Python API, agent, scheduler, reports
    backend/            Package source
    tests/              pytest test suite (51 tests)
  web/                  Web UI (chat + reports browser)
data/                   Raw/derived FPL data (git-ignored)
reports/                Generated GW reports (git-ignored)
scripts/
  preflight.sh          CI validation (go vet, go test, ruff, pytest)
.env.example            Annotated environment variable reference
```

---

## CI checks

| Check | What runs |
|---|---|
| Go | `go vet ./...`, `go test ./...`, `gofmt` diff check |
| Python | `py_compile` on all source files, `ruff check`, `pytest` |

Run locally:

```bash
bash scripts/preflight.sh
```

---

## Configuration reference

Copy `.env.example` to `.env` — every variable has a comment explaining what it controls and how to find its value.  Key variables:

| Variable | Default | Description |
|---|---|---|
| `LEAGUE_ID` | `14204` | Your FPL Draft league ID |
| `ENTRY_ID` | `286192` | Your team (entry) ID |
| `FPL_MCP_API_KEY` | *(none)* | Shared secret for the MCP server |
| `OPENAI_API_KEY` | *(none)* | OpenAI key for LLM-powered answers |
| `OPENAI_MODEL` | `gpt-4.1` | OpenAI model to use |
| `START_GO_SERVER` | `true` | Auto-start Go server from Python backend |
| `CACHE_REFRESH_ON_START` | `true` | Refresh FPL data on backend startup |
| `REPORTS_TZ` | `America/New_York` | Scheduler timezone |

---

## Adding screenshots or a demo GIF

To show the chat UI and reports in action, follow these steps:

1. **Take screenshots** of:
   - The chat UI at `http://localhost:8000/ui` with a sample question and answer
   - The reports browser at `http://localhost:8000/reports` showing generated reports
   - A sample generated report (e.g. `reports/gw_28/waiver_recommendations.md`)

2. **Record a demo GIF** using a tool like [Kap](https://getkap.co/) (macOS), [ScreenToGif](https://www.screentogif.com/) (Windows), or [peek](https://github.com/phw/peek) (Linux):
   - Show: opening the UI → typing a question → seeing the answer + tool trace → clicking a report

3. **Add to the repo:**
   ```
   docs/
     screenshots/
       chat-ui.png
       reports-browser.png
     demo.gif
   ```

4. **Embed in this README** below this section:

   ```markdown
   ## Demo

   ![Chat UI showing a waiver question with tool trace](docs/screenshots/chat-ui.png)

   ![Demo GIF](docs/demo.gif)
   ```

> Tip: keep GIFs under 10 MB; use [ezgif.com](https://ezgif.com/optimize) to optimise if needed.  For video, consider linking to a YouTube/Loom recording instead.
