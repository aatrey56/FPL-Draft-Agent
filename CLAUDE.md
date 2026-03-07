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

Branch naming:
- `fix/<topic>`
- `feat/<topic>`
- `refactor/<topic>`
- `chore/<topic>`

Never work directly on `main`.

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

### Python (if touched)
- pytest
- ruff check .
- mypy . (if configured)

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
│   │   └── fpl-server/
│   │       ├── main.go                  # Entry point, tool registration
│   │       ├── bootstrap.go             # Loads FPL bootstrap JSON
│   │       ├── waiver.go                # Waiver scoring logic
│   │       ├── fixture_difficulty.go    # FDR calculations
│   │       ├── head_to_head.go          # H2H record tool
│   │       ├── manager_season.go        # Season stats tool
│   │       ├── player_gw_stats.go       # Per-GW player stats tool
│   │       ├── current_roster.go        # Active roster tool
│   │       ├── draft_picks.go           # Draft history tool
│   │       ├── transaction_analysis.go  # Transaction ranking tool
│   │       ├── *_test.go                # Go unit tests (no live calls)
│   │       └── config.go                # ServerConfig struct
│   └── backend/             # Python FastAPI backend (port 8000)
│       └── backend/
│           ├── main.py          # FastAPI app, /chat endpoint
│           ├── agent.py         # AI agent routing + intent detection
│           ├── mcp.py           # MCP client (calls Go server)
│           ├── reports.py       # Report generation (markdown)
│           ├── rag.py           # RAG index (file-backed)
│           ├── scheduler.py     # APScheduler for data refresh
│           ├── constants.py     # Shared constants (GW_PATTERN, POSITION_TYPE_LABELS)
│           └── config.py        # SETTINGS (env-backed)
├── data/                    # FPL raw + derived data (gitignored)
│   ├── raw/                 # API snapshots (bootstrap.json, gw/*/live.json, etc.)
│   └── derived/
│       ├── summary/         # league/standings/transactions summaries
│       └── reports/         # GW markdown reports
├── CLAUDE.md
└── README.md
```

## 12.2 Data Flow

```
FPL API
  │
  ▼
Scheduler (Python, APScheduler)
  │  fetches raw JSON → data/raw/
  │  generates summaries → data/derived/summary/
  │  generates reports → data/derived/reports/
  ▼
Go MCP Server (:8080)
  │  reads data/raw/ + derived/
  │  exposes 22 tools via MCP protocol
  ▼
Python Agent (backend/agent.py)
  │  receives user message
  │  detects intent via _INTENT_KEYWORDS
  │  calls MCP tools via MCPClient
  │  augments with RAG context
  │  calls Claude LLM
  ▼
FastAPI /chat endpoint (:8000)
  │  returns structured response
  ▼
User / Frontend
```

## 12.3 Port Assignments

| Service | Port | Notes |
|---|---|---|
| Go MCP Server | 8080 | HTTP, MCP protocol |
| Python FastAPI | 8000 | HTTP, /chat endpoint |
| Dolt SQL (Gas Town) | 3307 | Persistent agent state |

---

# 13. Component Boundaries

**What lives in Go (MCP Server):**
- All file reads from `data/raw/` and `data/derived/`
- Stateless analytics: FDR scoring, waiver ranking, H2H records, player stats
- JSON in → JSON out — no side effects, no database, no network calls
- All MCP tool handlers

**What lives in Python (Backend):**
- Intent detection and routing (`agent.py`)
- LLM calls (Claude via `llm.py`)
- RAG index construction and search (`rag.py`)
- Scheduling and data refresh (`scheduler.py`)
- HTTP API surface (`main.py`)
- Report generation (`reports.py`)

**Crossing the boundary:**
- Python → Go: HTTP POST to MCP server with tool name + JSON args
- Go → Python: Never (Go is downstream, Python is orchestrator)
- The MCP protocol is the only interface between the two

**Rules:**
- Never add network calls to Go tools (they read local files only)
- Never add file I/O to agent routing logic (agent calls tools, tools read files)
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

```bash
cd apps/backend
pytest
ruff check .
mypy backend/   # if mypy is configured
```

## Run Both Servers Locally

```bash
# Terminal 1 — Go MCP server
cd apps/mcp-server
go run ./fpl-server

# Terminal 2 — Python backend
cd apps/backend
uvicorn backend.main:app --reload --port 8000
```

## Test Chat Endpoint

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "show standings", "session_id": "test"}' | jq .
```

## Data Directories (local dev)

Set in `.env`:
```
DATA_DIR=/path/to/data
REPORTS_DIR=/path/to/data/derived/reports
MCP_URL=http://localhost:8080
```

---

# 15. Never (Autonomous Agent Rules)

These rules are non-negotiable for all agents operating in this repo:

- **Never call the live FPL API in tests.** Use fixtures in `t.TempDir()` (Go) or `tmp_path` (pytest).
- **Never hardcode league IDs, entry IDs, or element IDs** in source code. Pass them as parameters.
- **Never push directly to `main` or `dev`.** All changes must go through a PR.
- **Never merge a PR with failing CI** (tests, lint, vet).
- **Never add a `# type: ignore`** without a comment explaining why it's unavoidable.
- **Never use `fmt.Sscanf` for float parsing** in Go — use `strconv.ParseFloat`.
- **Never use insertion sort** — use `sort.Slice` (Go) or sorted() (Python).
- **Never mutate `_docs` in RAGIndex** outside of `refresh()`.
- **Never skip `--force-with-lease`** when force-pushing a rebased branch.
- **Never open more than one PR per issue.**
- **Never ship without updating this file** if architecture changes.
