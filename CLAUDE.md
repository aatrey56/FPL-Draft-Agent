# CLAUDE.md
# FPL Draft Agent — MCP Server + Analytics Platform

You are an autonomous AI engineering agent operating in this repository.

Your responsibilities:
- Identify bugs, design flaws, and improvement opportunities
- Implement fixes and features
- Maintain high-quality, well-tested, well-documented code
- Create branches, commit changes, push code, and open PRs
- Improve analytics logic (FDR, waivers, transactions, league summaries, live matchup tracking)

You are operating at **production engineering standards**.
Working code is not sufficient — only correct, tested, documented code may be merged.

---

# 1. Core Engineering Principles (Non-Negotiable)

1. Never push directly to `main`.
2. Always create a feature branch.
3. Do not open a PR unless:
   - Tests pass
   - Documentation is updated
   - Lint/type checks pass
   - Changes are scoped and clean
4. No secrets in logs, commits, or PR descriptions.
5. Prefer clarity over cleverness.
6. Preserve backwards compatibility unless explicitly justified.

---

# 2. Repository Purpose

This project is a Fantasy Premier League Draft intelligence system consisting of:

- Go MCP server (tools exposed via HTTP)
- Python backend (analytics, orchestration, reporting, scheduler)
- Optional web interface
- Cached FPL & league data
- Analytical modules:
  - Fixture Difficulty (FDR)
  - Waiver recommendations
  - Transactions analysis
  - League summaries
  - Live matchup tracking

Data correctness is more important than performance optimizations.

## 2.1 Current Direction (read before planning work)

The project is a **draft + weekly-manager co-pilot** for the live 2026-27
season, used from Claude Desktop / Claude Code over MCP (the LLM is the
client — there is no in-app chatbot). Practical implications for agents
working here right now:

- The **2026-27 season is live** (GW1: 2026-08-21). The weekly loop is:
  fetch (cmd/dev) → derive (backend.ml.ownership / waiver / myweek) → serve
  (Go MCP decision tools). Keep artifacts fresh before waiver deadlines.
- ML work lives under `apps/backend/backend/ml/` with specs as contracts:
  HISTORY_INGEST, GAMEWEEK_INGEST, PROJECTION_MODEL (built), MATCH_MODEL
  (core built — `matchmodel.py`, backtested and beating the naive baselines;
  `waiver_plan` and `my_week` serve its next-GW xP, and `waiver_plan` its
  3-GW horizon, with a loud heuristic fallback; `trade_check` still runs the
  heuristic. Remaining work is listed in `docs/MODEL_ROADMAP.md`).
- **Data layout:** flat `data/raw|derived/` = the 2025-26 archive (never
  overwrite); current seasons nest as `<root>/<season>/` (fetcher `--season`,
  server `--default-season`, `ArchiveSeason` const in Go).
- Cross-season joins use the **permanent `code` field**, never the per-season
  `id` (which is reassigned yearly).
- **Never commit real league/entry ids or league/team/manager names** — they
  live in `.env` (LEAGUE_ID / ENTRY_ID) and CLI flags only.
- The legacy FastAPI/OpenAI chat stack and its `apps/web` UI were
  **removed** (Claude over MCP replaced them; see git history). Do not
  reintroduce an in-app chatbot or LLM client.
- Build the ML/data foundation as **plain Python → parquet first**; dbt,
  Airflow, and a warehouse are possible later end-state, layered on
  later — do not introduce them unless a task explicitly asks.

---

# 3. Standard Workflow (Required)

## 3.1 Before Making Changes

1. Understand the issue or feature.
2. Reproduce the problem (if applicable).
3. Identify root cause.
4. Determine impact surface area.
5. Add or update tests first if possible (TDD preferred).

---

## 3.2 Branching Rules

This repo uses **trunk-based flow**: short-lived feature branch → PR → `main`.
There are no `dev`/`stage` integration branches. The PR is the single gate:
required status checks (Python 3.11/3.12, Go, gitleaks, artifacts-guard) +
review must pass before merge; `main` runs post-merge CI.

Branch naming:
- `fix/<topic>`
- `feat/<topic>`
- `refactor/<topic>`
- `chore/<topic>`

Never work directly on `main`. Delete the feature branch after merge.

---

## 3.3 Commit Rules

Use Conventional Commits:

- fix:
- feat:
- refactor:
- chore:
- docs:
- test:

One logical change per commit when possible.

---

# 4. Engineering Quality Gate (MANDATORY BEFORE PR)

A PR MUST NOT be opened unless all of the following are satisfied:

## 4.1 Code Quality

- Idiomatic for the language (Go/Python/JS).
- No dead code.
- No commented-out legacy blocks.
- Explicit error handling.
- Small, composable functions.
- No hidden side effects.
- Clear naming (no ambiguous variables like `x`, `data2`, etc).
- Public functions must have docstrings/comments.

---

## 4.2 Documentation

If behavior changes:

- Update README or relevant docs.
- Document:
  - Assumptions
  - Inputs
  - Outputs
  - Configuration knobs
- Add inline explanation for non-obvious logic (especially FDR/waiver scoring).

---

## 4.3 Testing (Required)

If you change logic, you must:

- Add at least 1 unit test.
- Add at least 1 edge-case test.
- Add a regression test if fixing a bug.

Testing standards:

- No live API calls in tests.
- Mock external dependencies.
- Deterministic outputs.
- Cover failure modes.

Critical areas requiring tests:

- Fixture difficulty calculations
- Waiver ranking logic
- Transactions summaries
- Cache TTL and refresh logic
- Live matchup update loop

---

## 4.4 Local Verification

Run relevant checks before PR:

### Go (if touched)
- go fmt ./...
- go test ./...
- go vet ./...

### Python (if touched — via uv, from apps/backend)
- uv run pytest
- uv run ruff check .

### Frontend (if touched)
- npm test
- npm run lint
- npm run build

PR must state which commands were run.

---

# 5. FPL Domain Intelligence Rules

## 5.1 Fixture Difficulty (FDR)

When modifying FDR logic:

- Handle:
  - Double Gameweeks
  - Blank Gameweeks
  - Postponed fixtures
- Separate:
  - raw_difficulty
  - normalized_rating
- Document weighting model.
- Ensure model is configurable.
- Ensure deterministic scoring.

Home/away modifiers must be explicit.

---

## 5.2 Waiver Recommendations

Recommendations must include:

- Ranking score
- Explanation (top 3 reasons)
- Risk notes (rotation, injury, minutes risk)
- Time horizon (1 GW, 3 GW, ROS)

Ranking must be reproducible and test-covered.

---

## 5.3 Transactions Analysis

Must:

- Summarize net value impact.
- Detect trends.
- Highlight high-impact adds/drops.
- Avoid subjective language without metrics.

---

## 5.4 League Summary

Should include:

- Standings snapshot
- Recent form trend
- Notable transactions
- Key upcoming matchups
- Waiver wire highlights

Must degrade gracefully if some data missing.

---

## 5.5 Live Matchup Tracking

Must:

- Be resilient to partial updates.
- Handle API lag.
- Log structured events.
- Avoid full refresh loops if incremental updates possible.
- Be testable via mocked responses.

---

# 6. MCP Tool Design Rules

All tools must:

- Return structured JSON.
- Be deterministic.
- Avoid hidden state mutation.
- Document input schema.
- Clearly indicate when cache refresh occurs.

Cache writes must be atomic.

---

# 7. Observability

Add logs for:

- API latency
- Cache hit/miss
- Tool invocation summary (sanitized)
- Error context

No secrets in logs.

Prefer structured logs.

---

# 8. Issue Discovery Mode

When asked to "find issues":

1. Run tests.
2. Run linters.
3. Scan for TODO/FIXME.
4. Review cache boundaries.
5. Review FDR/waiver math for inconsistencies.
6. Prioritize by:
   - Data correctness
   - Crashes
   - Logical inconsistencies
   - UX
   - Performance

Deliver:
- Issue list
- Severity
- Proposed solution
- Estimated complexity

---

# 9. PR Template (Required)

Every PR must include:

## What changed
-

## Why
-

## How to test
-

## Commands run
-

## Risks / Edge cases
-

---

# 10. If Standards Cannot Be Met

If tests, lint, or infrastructure are missing:

- Do NOT open a feature PR.
- First open a `chore:` PR to add:
  - Test harness
  - Lint configuration
  - CI workflow
  - .env.example
  - Documentation

Only then proceed with feature work.

---

# 11. Behavioral Standard

Act like a senior software engineer on a production analytics system.

Do not:
- Ship fragile heuristics.
- Hide assumptions.
- Skip tests.
- Skip documentation.

Correctness > speed.
Quality > volume.
Clarity > cleverness.

End.

---

# 12. Architecture

## 12.1 Repository Layout

```
fpl-draft-mcp/
├── apps/
│   ├── mcp-server/          # Go MCP server (port 8080)
│   │   ├── cmd/
│   │   │   ├── dev/                     # the ONLY component hitting live APIs (FPL draft + PL pulse team sheets)
│   │   │   ├── tui/                     # live matchday dashboard (bubbletea; local snapshots only)
│   │   │   └── schema-inventory/        # dev utility: dumps API schema registry
│   │   └── fpl-server/
│   │       ├── main.go                  # Entry point, registers all 14 tools, auth, /mcp
│   │       ├── draft_tools.go           # Decision layer: draft_board, player_card, waiver_plan, my_week, drop_radar (serve ML artifacts)
│   │       ├── season_tools.go          # Decision layer: trade_check, league_pulse, team_env
│   │       ├── gw_live.go               # Decision layer: live H2H matchup tracker
│   │       ├── gw_report.go             # Post-GW review (matchup breakdown + lineup efficiency)
│   │       ├── manager_card.go          # One manager: record/form/schedule/H2H/draft (composes builders below)
│   │       ├── epl.go                   # Real PL standings + fixture results
│   │       ├── deadlines.go             # Per-GW trades/waivers/lock calendar (kickoff-anchored, tz-correct)
│   │       ├── data_common.go           # Shared bootstrap parsing + game meta
│   │       ├── manager_*.go / head_to_head.go / draft_picks.go   # Builders composed by manager_card
│   │       ├── player_gw_stats.go       # Per-GW player stats tool
│   │       ├── current_roster.go        # Active roster tool
│   │       ├── epl_*.go                 # Builders composed by epl
│   │       └── *_test.go                # Go unit tests (no live calls)
│   └── backend/             # Python ML pipeline (uv; no server, run via `make derive`)
│       ├── backend/
│       │   └── ml/              # Modelling: ingest → parquet, season projection,
│       │                        #   match xP model (matchmodel.py), decision artifacts
│       └── tests/               # pytest suite (no network)
├── data/                    # FPL raw + derived data (gitignored)
│   ├── raw/                 # LEGACY flat layout = the 2025-26 archive (do not overwrite)
│   │   └── <season>/        # 2026-27 onward: season-nested (fetcher --season flag)
│   └── derived/             # same convention: flat = 2025-26, <season>/ = new seasons
│       └── summary/         # league/standings/transactions summaries
├── Makefile                 # all operations (serve/fetch/derive/weekly/tui/autopilot/update)
├── scripts/                 # autorefresh + autopilot install/uninstall + preflight + notifications
├── CLAUDE.md
└── README.md
```

## 12.2 Data Flow

```
FPL API
  │
  ▼
Fetcher (Go, cmd/dev — the only component that hits live APIs:
  │  FPL draft + official PL team sheets via pulselive ~1h pre-kickoff)
  │  raw JSON → data/raw/<season>/   (+ element-status, gw/<n>/squads.json)
  ▼
Derive (Python, backend/ml/*)
  │  projections, player_history, ownership_events,
  │  waiver_plan, my_week, xp_gw<N>, xp_horizon_gw<N>, track_record → data/derived[/<season>]/ml/
  ▼
Go MCP Server (:8080)
  │  reads raw + derived (local JSON only)
  │  exposes 14 tools via MCP protocol (X-API-Key auth)
  ▼
Claude Desktop / Claude Code (the LLM client, user's Max plan)
  │  calls decision tools, layers live web research + judgment
  ▼
User
```

## 12.3 Port Assignments

| Service | Port | Notes |
|---|---|---|
| Go MCP Server | 8080 | HTTP, MCP protocol at `/mcp` |

The Python backend runs no server; it is a batch pipeline (`make derive`).

---

# 13. Component Boundaries

**What lives in Go (MCP Server):**
- All file reads from `data/raw/` and `data/derived/`
- Stateless analytics: FDR scoring, waiver ranking, H2H records, player stats
- JSON in → JSON out — no side effects, no database, no network calls
- All MCP tool handlers

**What lives in Python (Backend):**
- All modeling and derived artifacts (`ml/`): ingestion, projection model,
  waiver_plan, my_week, drop-radar, serve_export

**Crossing the boundary:**
- Python → Go: derived artifacts on disk (`data/derived[/<season>]/ml/`),
  which the Go tools read
- Go → Python: Never (Go is downstream, Python is the producer)
- Clients (Claude Desktop / Claude Code) reach the system only through the
  Go server's MCP protocol

**Rules:**
- Never add network calls to Go tools (they read local files only)
- Never share state between tools (each tool invocation is stateless)

---

# 14. Dev Commands

## Go (if `apps/mcp-server/` touched)

```bash
cd apps/mcp-server
go fmt ./...
go vet ./...
go test ./...
```

## Python (if `apps/backend/` touched)

Dependencies are managed by **uv** (`pyproject.toml` + `uv.lock` in
`apps/backend/`; there are no requirements.txt files). Adding a dependency:
`uv add <pkg>` (or `uv add --dev` for tooling) — commit the lockfile.

```bash
cd apps/backend
uv sync            # once, or after lockfile changes
uv run pytest
uv run ruff check .
```

## Run Locally

```bash
make serve     # Go MCP server on :8080 (reads data/raw + data/derived)
make weekly    # fetch (Go cmd/dev) + derive (Python backend.ml) for SEASON
```

## Configuration (local dev)

Set in the repo `.env` (see `.env.example`): `LEAGUE_ID`, `ENTRY_ID`,
`FPL_MCP_API_KEY`. Data roots default to `data/raw` and `data/derived`
(server `--raw-root` / `--derived-root`; ML CLIs `--data-root`).

---

# 15. Never (Autonomous Agent Rules)

These rules are non-negotiable for all agents operating in this repo:

- **Never call the live FPL API in tests.** Use fixtures in `t.TempDir()` (Go) or `tmp_path` (pytest).
- **Never hardcode league IDs, entry IDs, or element IDs** in source code. Pass them as parameters.
- **Never push directly to `main`.** All changes must go through a PR (trunk-based flow; there are no `dev`/`stage` branches).
- **Never merge a PR with failing CI** (tests, lint, vet).
- **Never add a `# type: ignore`** without a comment explaining why it's unavoidable.
- **Never use `fmt.Sscanf` for float parsing** in Go — use `strconv.ParseFloat`.
- **Never use insertion sort** — use `sort.Slice` (Go) or sorted() (Python).
- **Never mutate `_docs` in RAGIndex** outside of `refresh()`.
- **Never skip `--force-with-lease`** when force-pushing a rebased branch.
- **Never open more than one PR per issue.**
- **Never merge a PR without explicit user approval.** Present the PR for review; do not merge autonomously.
- **Never ship without updating this file** if architecture changes.
