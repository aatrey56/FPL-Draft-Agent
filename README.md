# FPL Draft Co-Pilot

A **draft + weekly-manager co-pilot** for Fantasy Premier League Draft.
A Go MCP server exposes 34 tools over locally-cached FPL Draft API data; a
Python ML pipeline turns seven seasons of history into projections and
weekly recommendations; **Claude (Desktop or Code) is the client** — you ask
questions in natural language, Claude calls the decision tools and layers
live web research on top.

```
FPL API → Go fetcher → data/<season>/ → Python ML (projections, waivers,
start/sit) → Go MCP server (:8080, 34 tools) → Claude Desktop / Claude Code
```

## The decision layer

| Tool | Answers |
|---|---|
| `draft_board` | Who do I draft? Tiered, VOR-ranked projections per position |
| `player_card` | Who is this player? Projection + 7-season history + live news (falls back to history for unprojected players) |
| `waiver_plan` | Who do I add/drop? Roster-aware, labeled `upgrade` / `stream` / `hold` over two horizons |
| `my_week` | Who starts this GW? Best XI, bench, and attention flags (injuries, blanks, unknowns) |
| `trade_check` | Is this trade good? Give vs get on season value + starter scarcity (VOR) |
| `league_pulse` | What's happening? Standings, named transactions, game clock |
| `drop_radar` | Who hit the wire? Ownership diffs from element-status snapshots |
| `team_env` | Shootout or stalemate? Per-team points/xG generated and conceded, by position and venue |

Plus the 26 data-layer tools (standings, matchups, fixtures, transactions,
per-GW player stats, …) — all season-aware: flat `data/` roots are the
2025-26 archive, current seasons nest under `data/{raw,derived}/<season>/`.

## The models (honest by design)

- **Season projection** (`backend/ml/projection.py`): closed-form ridge +
  persistence candidates per position, chosen by walk-forward validation with
  the naive baseline *in the candidate zoo* — where nothing beats
  last-season-points, the model honestly *is* last-season-points. Measured
  outcome: ties the baseline for GKP/DEF/FWD, real MID edge via
  points-calibrated ICT. Deterministic, no deep learning, drivers explainable.
- **Match xP model** (`backend/ml/MATCH_MODEL_SPEC.md`): in progress — trained
  on the 29,747-row per-GW panel, must beat FPL's own `ep_next` to ship.
  Replaces the current per-GW heuristic (projection/38 × fixture multiplier ×
  availability) inside `waiver_plan` / `my_week`.
- Players the model cannot value (long injury last season, promoted, new
  signings) are **surfaced for human judgment, never scored as zero** — the
  tools refuse to guess rather than quietly recommend dropping a returning star.

## Quickstart

Prerequisites: Go 1.25+, [uv](https://docs.astral.sh/uv/) (manages Python
itself — no system Python needed: `curl -LsSf https://astral.sh/uv/install.sh | sh`).

```bash
git clone https://github.com/aatrey56/FPL-Draft-Agent.git && cd FPL-Draft-Agent
(cd apps/backend && uv sync)

# one-time config — ids from draft.premierleague.com URLs, key is any random string
printf 'LEAGUE_ID=<yours>\nENTRY_ID=<yours>\nFPL_MCP_API_KEY=%s\n' \
  "$(openssl rand -hex 16)" >> .env
```

First time only — fetch the season (step 1 below), then build the ML
artifacts (they're gitignored; the explicit season list pulls the 2025-26
history from the public vaastav mirror):

```bash
cd apps/backend
uv run python -m backend.ml.history --seasons 2019-20 2020-21 2021-22 2022-23 2023-24 2024-25 2025-26
uv run python -m backend.ml.projection --project
uv run python -m backend.ml.serve_export
```

Weekly loop (both Go binaries and the Python CLIs read `.env` automatically):

```bash
# 1. fetch: game state, league, transactions, element-status snapshot
cd apps/mcp-server && go run ./cmd/dev --season 2026-27 \
  --raw-root ../../data/raw --derived-root ../../data/derived --refresh-now

# 2. derive: ownership events, waiver plan, start/sit
cd ../backend
uv run python -m backend.ml.ownership && uv run python -m backend.ml.waiver && uv run python -m backend.ml.myweek

# 3. serve
cd ../mcp-server && go run ./fpl-server \
  --raw-root ../../data/raw --derived-root ../../data/derived --default-season 2026-27
```

Connect Claude and ask away (full guide: `docs/CLAUDE_DESKTOP.md`):

```bash
claude mcp add fpl --transport http http://localhost:8080/mcp \
  --header "X-API-Key: $(grep '^FPL_MCP_API_KEY=' .env | cut -d= -f2)"
```

> *"Run my waiver plan — which adds are streams vs season upgrades?"* ·
> *"my_week: who starts and what needs my attention?"* ·
> *"trade_check: I give X, I get Y — worth it?"*

## Project layout

```
apps/
  mcp-server/            Go module
    fpl-server/          34 MCP tool handlers + HTTP server (X-API-Key auth)
    cmd/dev/             FPL data fetcher (the only live-API component)
    internal/            fetch, store, ledger, points, summary, config
  backend/               Python package
    backend/ml/          ingestion → parquet, projection model, waiver_plan,
                         my_week, drop-radar, specs (treat *_SPEC.md as contracts)
    tests/               pytest suite (300 tests, no network)
data/                    Raw + derived FPL data (gitignored; flat = 25/26 archive)
docs/                    Setup + design docs
PLAN.md / STATE.md / ISSUES.md   Living roadmap, checkpoint, known issues
```

## CI

Required checks on every PR to `main` (strict, 1 review): Go
(vet/test/gofmt), Python 3.11 + 3.12 (ruff + pytest), gitleaks secret scan,
artifacts-guard, plus an automated Claude code review. Run locally:
`bash scripts/preflight.sh`.

Configuration reference: `.env.example` (every variable annotated). League
and entry ids live in `.env` only — never in tracked files.

## Legacy

`apps/backend`'s FastAPI chat server, OpenAI agent, RAG index, and
APScheduler (`server.py`, `agent.py`, `llm.py`, `rag.py`, `scheduler.py`)
are the pre-MCP-client stack: kept compiling and tested, deprecated, not
developed. Claude over MCP replaced them.
