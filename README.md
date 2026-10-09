# FPL Draft Co-Pilot

A draft and weekly-manager co-pilot for Fantasy Premier League Draft. A Go
MCP server serves 14 tools over locally cached FPL Draft data; a Python
pipeline turns multi-season history into projections and per-gameweek
recommendations (waivers, lineup, trades); **Claude (Desktop or Code) is the
client** — you ask in natural language and Claude calls the tools and layers
its own research on top. There is no in-app chatbot or LLM client.

```
FPL Draft API -> Go fetcher (cmd/dev) -> data/raw/<season>/
              -> Python derive (backend.ml)  -> data/derived/<season>/ml/
              -> Go MCP server (:8080, /mcp) -> Claude Desktop / Claude Code
```

Only the fetcher touches live APIs (FPL Draft, plus the official PL team
sheets shortly before kickoff). The MCP server and the TUI read local files.

## Components

### MCP tools

Decision layer (serve the ML artifacts):

| Tool | Answers |
|---|---|
| `draft_board` | Who do I draft? Tiered, VOR-ranked preseason projections per position |
| `player_card` | Who is this player? Projection, multi-season history, live news |
| `waiver_plan` | Who do I add/drop? Roster-aware swaps labeled `upgrade` / `stream` / `hold`, with gains over next GW / next 3 GWs / rest of season |
| `my_week` | Who starts this GW? Best XI over legal formations, bench order, auto-sub `if_out`, attention flags |
| `trade_check` | Is this trade good? Give vs get on match-model xP and rest-of-season value; falls back to projection + VOR (`value_source: "heuristic"`) when `player_values.json` is missing or stale |
| `league_pulse` | Standings, named transactions, game clock and this week's deadlines |
| `drop_radar` | Who hit the wire? Ownership diffs from element-status snapshots |
| `team_env` | Per-team points/xG generated and conceded, by position and venue |
| `gw_live` | Live H2H tracker: both XIs with in-play points |

Data layer: `manager_card` (record, form, schedule, H2H, draft picks),
`current_roster`, `player_gw_stats`, `epl` (real PL table and results),
`gw_report` (post-GW matchup review and lineup efficiency).

Tools fall back loudly rather than guess: when the match model's xP file is
missing or stale, `waiver_plan` / `my_week` switch to a per-GW heuristic and
say so (`scorer`, `xp_fallback_reason`, per-player `xp_source`). Players the
model cannot value (promoted clubs, new signings, long injuries) are surfaced
for human judgment, never scored as zero.

### Derive pipeline (`apps/backend/backend/ml/`)

Plain Python to parquet/JSON, run via `make derive` (no server):

- `history.py`, `gameweeks.py`: multi-season and per-GW panel ingestion
- `projection.py`: season projection (ridge + persistence candidates chosen by
  walk-forward validation; where nothing beats last-season points, it is last-season points)
- `matchfeatures.py`, `matchmodel.py`: two-stage match xP model (who plays, then points if they do), next-GW and 3-GW horizon; `matcheval.py` and `make backtest` measure it against naive baselines
- `ownership.py`, `waiver.py`, `myweek.py`, `player_values.py`, `teamenv.py`, `trackrecord.py`: the weekly artifacts the Go tools serve
- `deadline_agent.py`: deadline checklists (below)

Specs (`*_SPEC.md` beside the code) are the contracts. Measured results and
open work: [docs/MODEL_ROADMAP.md](docs/MODEL_ROADMAP.md) and
[docs/MINUTES_FINDINGS.md](docs/MINUTES_FINDINGS.md).

### Matchday TUI

`make tui` opens a live terminal dashboard (bubbletea): your H2H matchup
(any matchup, left/right arrows) with per-player in-play points, a
suggestions rail from `my_week` / `waiver_plan`, and a countdown to the next
deadline. It only reads local snapshots; `r` triggers one fetch through
`cmd/dev`. `--once` renders a single frame and exits.

### Autopilot and deadline checklists (macOS)

`make autopilot` installs three launchd agents: the MCP server (always on),
a fetch + derive refresh every 15 minutes (`scripts/autorefresh.sh`, which
also sends macOS notifications when a GW finalizes and 24h before each
deadline), and a 5-minute deadline tick (`scripts/deadline_tick.sh`).

The deadline agent delivers a compact checklist about 45 minutes
(`CHECKLIST_LEAD_MIN`, allowed 30-60) before each trades / waivers /
lineup-lock deadline, only inside your awake window (default 07:00-02:00
`USER_TZ`, with a 01:40 late slot for early-morning deadlines), after a fresh
derive. It follows each GW's own kickoff-anchored clock. Details:
[docs/DEADLINE_AGENT.md](docs/DEADLINE_AGENT.md). Non-Mac users can schedule
the two scripts from cron (examples in the script headers).

## Quickstart

Prerequisites: Go 1.26+ (per `apps/mcp-server/go.mod`) and
[uv](https://docs.astral.sh/uv/), which manages Python (3.11+) itself.

```bash
git clone https://github.com/aatrey56/FPL-Draft-Agent.git && cd FPL-Draft-Agent
(cd apps/backend && uv sync)

# .env at the repo root; ids are in draft.premierleague.com URLs,
# the key is any random string you invent
printf 'LEAGUE_ID=<LEAGUE_ID>\nENTRY_ID=<ENTRY_ID>\nFPL_MCP_API_KEY=%s\n' \
  "$(openssl rand -hex 16)" >> .env
```

First-time build (artifacts are gitignored; the season list pulls history from
the public vaastav mirror):

```bash
make fetch                    # raw data for SEASON (default 2026-27)
cd apps/backend
uv run python -m backend.ml.history --seasons 2019-20 2020-21 2021-22 2022-23 2023-24 2024-25 2025-26
uv run python -m backend.ml.projection --project
uv run python -m backend.ml.serve_export
cd .. && make derive          # weekly artifacts
make serve                    # MCP server on :8080
```

Connect Claude (full guide: [docs/CLAUDE_DESKTOP.md](docs/CLAUDE_DESKTOP.md)):

```bash
claude mcp add fpl --transport http http://localhost:8080/mcp \
  --header "X-API-Key: $(grep '^FPL_MCP_API_KEY=' .env | cut -d= -f2)"
```

Then ask, for example: "Run my waiver plan, which adds are streams vs season
upgrades?", "my_week: who starts and what needs my attention?", "trade_check:
I give X, I get Y, worth it?"

For hands-off operation on macOS, run `make autopilot` once instead of the
manual fetch/derive/serve steps.

## Make targets

| Target | Does |
|---|---|
| `serve` | Run the MCP server (restart after every `git pull`) |
| `fetch` | Refresh raw data for `SEASON`; settled gameweeks already on disk are skipped |
| `fetch-all` | Same, but re-download every GW (e.g. after an FPL points correction) |
| `livefetch` | In-play refresh of the current GW's live points only |
| `derive` | Rebuild weekly artifacts: ownership, panel + xP + 3-GW horizon, `team_env`, waiver, my_week, `player_values`, track record |
| `weekly` | `fetch` then `derive` |
| `xp GW=n` | Build the xP and horizon files for one specific GW |
| `matchday` | Game-day loop: live points every 60s, full refresh every 10 min |
| `tui` | Live matchday dashboard |
| `backtest` | Walk-forward evaluation of the match model vs naive baselines |
| `preflight` | Full local CI (Go vet/test/gofmt, uv sync, ruff, pytest) |
| `autopilot` / `autopilot-off` | Install / remove the launchd agents |
| `stop` / `start` | Pause / resume the agents without uninstalling |
| `update` | `git pull --ff-only` and restart the autopilot server |
| `restart-server` | Reload the autopilot server after code changes |
| `checklist-plan` | Next deadlines with computed research and delivery times |
| `checklist-preview KIND=waivers` | Render the next checklist to stdout, send nothing (`KIND` = `trades`, `waivers`, `lineup`) |

Variables: `SEASON` (default `2026-27`), `SCORER` (`model` default, or
`heuristic`), `HORIZON` (`3` default, or `1`, `ros`; the gain `waiver_plan`
ranks on), `DATA_DIR` (data root as seen from `apps/backend`).

A failed xP/horizon step in `derive` only warns; waiver and my_week fall back
to the heuristic (or the next-GW ranking) and record why. The fetch
settlement rules (which GWs are skipped, when picks are re-downloaded) are
documented in the comments above the `fetch` and `derive` targets in the
[Makefile](Makefile) and in `apps/mcp-server/cmd/dev`.

## Data layout

```
data/                     gitignored
  raw/                    flat = 2025-26 archive (never overwritten)
    <season>/             2026-27 onward, written by the fetcher (--season)
  derived/
    ml/                   flat: multi-season history, 2025-26 panel, preseason projections
    <season>/ml/          weekly artifacts: waiver_plan, my_week, player_values,
                          team_env, xp_gw<N>, xp_horizon_gw<N>, track_record, ...
```

Servers and ML CLIs take `--raw-root` / `--derived-root` / `--data-root`
(defaults `data/raw`, `data/derived`) and `--default-season`. Cross-season
joins use the permanent player `code`, never the per-season `id`.

## Configuration

Set in the repo `.env` (see [.env.example](.env.example), every variable
annotated). Real environment variables win.

- `LEAGUE_ID`, `ENTRY_ID`, `FPL_MCP_API_KEY`: required. Ids live in `.env`
  only, never in tracked files.
- Deadline agent (all optional): `USER_TZ`, `AWAKE_START`, `AWAKE_END`,
  `CHECKLIST_LEAD_MIN`, `MIN_ACTION_MIN`, `RESEARCH_LEAD_MIN`, `NTFY_TOPIC`,
  `NTFY_SERVER`.

Two hand-maintained, gitignored files under `data/derived/<season>/ml/` steer
`waiver_plan` and `my_week`; re-run `make derive` after editing either:

- `squad_prefs.json`, the never-drop list: players you are keeping are never
  a drop pick or drop candidate.
  `{"never_drop": [{"player": "<web_name>", "team": "<SHORT>", "until_gw": 12, "note": "..."}]}`
  (`code` may replace `player`/`team`; `until_gw` and `note` are optional.)
  Every entry is accounted for in `never_drop_applied` / `_unmatched` /
  `_expired` / `_invalid`.
- `role_overrides.json`, team news for the next GW:
  `{"overrides": [{"player": "<web_name>", "team": "<SHORT>", "p_start": 0.4, "fact": "...", "return_gw": 9, "valid_through_gw": 7, "as_of": "<date>"}]}`.
  Entries go stale or expire by GW and never lift an availability gate; each
  is accounted for in `overrides_applied` / `_unmatched` / `_expired` /
  `_stale` / `_blocked`. Research current news before each deadline.

Field semantics and edge cases are documented in `apps/backend/backend/ml/waiver.py`.

## Development

```bash
# Go (apps/mcp-server)
go fmt ./... && go vet ./... && go test ./...

# Python (apps/backend)
uv run pytest && uv run ruff check .

bash scripts/preflight.sh     # all of the above (make preflight)
```

No test calls a live API. PRs to `main` need passing CI (Go, Python 3.11 and
3.12, gitleaks, artifacts-guard) and review; see
[CONTRIBUTING.md](CONTRIBUTING.md) and [CLAUDE.md](CLAUDE.md) for the
engineering rules and architecture.

## Docs

- [docs/CLAUDE_DESKTOP.md](docs/CLAUDE_DESKTOP.md): connecting Claude Desktop / Code
- [docs/DEADLINE_AGENT.md](docs/DEADLINE_AGENT.md): deadline checklist agent
- [docs/MODEL_ROADMAP.md](docs/MODEL_ROADMAP.md): model status and remaining work
- [docs/MINUTES_FINDINGS.md](docs/MINUTES_FINDINGS.md): minutes / cold-start analysis
- [PLAN.md](PLAN.md), [STATE.md](STATE.md), [ISSUES.md](ISSUES.md), [CHANGELOG.md](CHANGELOG.md): roadmap, checkpoint, known issues, release notes

The pre-MCP chat stack (FastAPI server, OpenAI agent, RAG index, `apps/web`)
was removed; it remains in git history.
