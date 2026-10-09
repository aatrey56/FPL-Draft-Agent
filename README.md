# FPL Draft Co-Pilot

A **draft + weekly-manager co-pilot** for Fantasy Premier League Draft.
A Go MCP server exposes 14 tools over locally-cached FPL Draft API data; a
Python ML pipeline turns seven seasons of history into projections and
weekly recommendations; **Claude (Desktop or Code) is the client** — you ask
questions in natural language, Claude calls the decision tools and layers
live web research on top.

```
FPL API → Go fetcher → data/<season>/ → Python ML (projections, waivers,
start/sit) → Go MCP server (:8080, 14 tools) → Claude Desktop / Claude Code
```

## The decision layer

| Tool | Answers |
|---|---|
| `draft_board` | Who do I draft? Tiered, VOR-ranked projections per position |
| `player_card` | Who is this player? Projection + 7-season history + live news (falls back to history for unprojected players) |
| `waiver_plan` | Who do I add/drop? Roster-aware, labeled `upgrade` / `stream` / `hold`, with gains over three horizons (next GW / next 3 GWs / rest of season) |
| `trade_check` | Is this trade good? Give vs get on match-model xP (next GW, next 3 GWs — fewer near season end, labelled via `horizon_events`) and role-adjusted rest-of-season value from `player_values.json`; falls back to projection + VOR (`value_source: "heuristic"` + reason) when that file is missing or stale (wrong GW, or a panel not through the last finished GW) |
| `my_week` | Who starts this GW? Best XI over every legal formation (`formation`), bench in auto-sub order, `if_out` (`if_out_mode: "autosub"` marks the automatic format) = what FPL auto-subs do automatically if a doubtful starter plays 0 minutes (blank-GW bench players never come on; a manual swap only when it gains >0.5 xP before lock), and attention flags (injuries, blanks, unknowns) |
| `league_pulse` | What's happening? Standings, named transactions, game clock + this week's deadlines (trades/waivers/lineup lock, in EST) |
| `drop_radar` | Who hit the wire? Ownership diffs from element-status snapshots |
| `team_env` | Shootout or stalemate? Per-team points/xG generated and conceded this season (`season` arg, default the server season; rebuilt by `make derive`), by position and venue |
| `gw_live` | How's my matchup going? Live H2H tracker: both XIs with in-play points (refresh mid-match) |

**Game day:** `make tui` opens a live terminal dashboard — your H2H matchup
(any matchup, ←/→) with per-player in-play points, manager names, and a
countdown to the next deadline (its Suggestions rail is headed `heuristic xP`
when my_week fell back from the match model). The autopilot keeps it fresh and sends macOS
notifications when a gameweek finalizes and 24h before every deadline, and a
[deadline checklist](docs/DEADLINE_AGENT.md) arrives 30–60 min before each
trades / waivers / lineup-lock deadline — only while you are awake, after a
fresh research + derive run. All of it follows each GW's own kickoff-anchored
clock, so midweek and festive schedules work automatically.

Plus 5 data-layer tools — `manager_card` (one manager: record, form,
schedule, H2H, draft picks), `current_roster`, `player_gw_stats`, `epl`
(real PL table/results), `gw_report` (post-GW review) — all season-aware:
flat `data/` roots are the 2025-26 archive, current seasons nest under
`data/{raw,derived}/<season>/`.

The Python derive steps (waiver, my_week, ownership, replay) resolve paths
through `backend/ml/paths.py` and their `--data-root` flag: a season's
projection is `derived/<season>/ml/projections.json`, falling back — for
2026-27 only — to the flat preseason `derived/ml/projections_2627.json`; any
other season without its own projection fails fast naming the expected path.
Team strengths and club moves read `prior_season(season)` from the
multi-season `derived/ml/player_seasons.parquet`. Go `draft_board` (a
preseason tool) still reads the flat 2026-27 projection directly.

## The models (honest by design)

- **Season projection** (`backend/ml/projection.py`): closed-form ridge +
  persistence candidates per position, chosen by walk-forward validation with
  the naive baseline *in the candidate zoo* — where nothing beats
  last-season-points, the model honestly *is* last-season-points. Measured
  outcome: ties the baseline for GKP/DEF/FWD, real MID edge via
  points-calibrated ICT. Deterministic, no deep learning, drivers explainable.
- **Match xP model** (`backend/ml/matchmodel.py`): built and measured. A
  two-stage, availability-gated model on the 29,747-row per-GW panel —
  stage one predicts *who plays*, stage two *how many points if they do*, per
  position, with the opponent in the features. It beats every naive baseline
  in all four positions on a held-out slice of gameweeks (table below).
  `waiver_plan` / `my_week` read it (`xp_gw<N>.parquet`) with
  `--scorer model` and fall back, with a WARNING and a per-player `xp_source`
  (`model` / `heuristic` / `none`), to the per-GW heuristic (projection/38 ×
  fixture multiplier × availability) for uncovered players or a missing/unreadable/stale
  file (the JSON's `scorer` then reads `heuristic`, with `xp_fallback: true`
  and an `xp_fallback_reason`). A model row for a player whose *current*
  availability is 0 (ruled out after the xP file was built) is zeroed
  (`xp_next`/`p_start` 0, and a model 3-GW `next3_xp` 0; `xp_reconciled:
  true`, and the JSON's `xp_reconciled` counts them). Every waiver recommendation carries
  `gains: {gw1, gw3, ros}` — next-GW xP, 3-GW xP and rest-of-season, each the
  add minus the drop (also as the flat `next1_gain` / `next3_gain` /
  `season_gain`). The 3-GW value is the match model's horizon
  (`xp_horizon_gw<N>.parquet`: GW N..N+2 scored with form frozen at GW N, so
  it is a schedule view; a blank adds 0, a double both fixtures, and today's
  availability is applied to all three), with `add_next3_source` saying
  `model` or `heuristic`. `--horizon {1,3,ros}` (default `3`) picks the gain
  that ranks and `rank_by` in the JSON records it; `hold` recs (no short-term
  gain, better ROS) come last except under `ros`. Without a usable horizon
  file the JSON says `horizon_fallback: true` with a reason, 3-GW values are
  heuristic and `--horizon 3` ranks on the next GW. A free agent with model
  xP but no ROS projection (promoted club) is ranked as a `stream` with
  `season_gain: null` and `season_unknown: true`.
  `--scorer model` is the default (the 2026-27 GW2-5 live check in
  `docs/MODEL_ROADMAP.md` found the model ahead of every baseline in all
  four positions); `--scorer heuristic` reproduces the pre-xP output exactly
  between gameweeks — labels and ordering are then next-3-GW based, as they
  are after a model fallback, and the `waiver_plan` tool note describes
  whichever ranking the served file used (keyed on its `scorer`). Mid-gameweek (GW N in play) both scorers now plan for
  N+1: the heuristic's fixture loads and my_week's `gw` used to start at the
  locked GW N.
- **Role signals** (`waiver_plan` / `my_week`, both scorers): a season
  projection only knows last season's role at last season's club.
  `club_moved` = the bootstrap club differs from the 2025-26 club in
  `player_seasons` (null = new to the league); `expected_minutes` = mean
  minutes over the last 5 finished GWs of the season panel (a missing row is
  0; null before GW1 finishes). For club-movers only, `ros_adj = ros_points ×
  clip(expected_minutes / 60, 0.15, 1.0)` (`ROLE_MINUTES_FULL`, `ROLE_FLOOR`
  in `waiver.py`), and `season_gain`, the drop pick and the heuristic per-GW
  baseline use `ros_adj`; `ros_points` stays in the output. A club-mover
  whose status is not `a` (injured, doubtful, suspended) keeps factor 1.0:
  the absence, not a lost role, explains his minutes. A departed squad
  player is always the drop at his position. Otherwise, under the model
  scorer, the drop is the lowest value on the ranking horizon (`xp_next` /
  `next3_xp` / `ros_adj` for `--horizon 1` / `3` / `ros`), ties by
  `ros_adj` — so an injured or benched player with model xP but no ROS
  projection can be the drop (that rec's season gain is then unknown:
  `drop_ros_unknown`, `season_unknown`). A player with neither stays in
  `unprojected_squad`, never auto-dropped. The heuristic scorer keeps its
  frozen pick: ROS-projected players only, by `ros_adj`. `drop_candidates`
  lists the top 3 per position in that order.
- **Diversified top N + `best_by_position`**: under the model scorer
  `recommendations` caps any one drop player at 3 recs (`MAX_RECS_PER_DROP`;
  recs past the cap only backfill, after the others, when the list would be
  short — rank 1 never moves). `best_by_position` lists the best 3 swaps per
  position against that position's drop pick, for both scorers, so a run of
  MID swaps cannot hide the best DEF/FWD move.
  An optional `data/derived/<season>/ml/role_overrides.json`
  (`{"overrides": [{"player": "<web_name>", "team": "<short name>",
  "p_start": 0.4, "fact": "...", "return_gw": 9, "valid_through_gw": 7,
  "as_of": "2026-10-06", "code": 123}]}` — all but `player`/`team`
  optional; `code` wins when present and may be quoted) is hand-maintained team news for the next
  GW: `xp_next = p_start × xP-if-he-starts` (cameo term dropped), `return_gw`
  still ahead forces 0 (and keeps him off the my_week XI), and an entry whose
  `return_gw` has arrived is expired and ignored. An override never lifts
  the availability gate: a player the live feed rules out (status u/i/s or a
  0% chance) keeps his 0 and the entry is listed as blocked. Every entry is
  accounted for in `overrides_applied` / `overrides_unmatched` /
  `overrides_expired` / `overrides_stale` / `overrides_blocked`.
  Staleness rule: an entry with `valid_through_gw` applies through that GW;
  an entry with neither `return_gw` nor `valid_through_gw` applies only up to
  the first GW whose deadline falls after its `as_of` (else the file's
  top-level `as_of`/`updated`, else the file mtime; a bare date means 00:00
  UTC). Later it is stale and ignored — team news written for GW2 is not
  evidence about GW6. Give long-lived facts a `valid_through_gw`.
  my_week lists the override `fact` and any departed squad player under
  `attention` (warning codes `role_override`, `departed`).
- Players the model cannot value (long injury last season, promoted, new
  signings) are **surfaced for human judgment, never scored as zero** — the
  tools refuse to guess rather than quietly recommend dropping a returning star.

## Research & evaluation

Every model claim here is a measured number, and the measurement code ships
with the repo.

**The bar to beat** (`backend.ml.matcheval`) — five naive predictors scored
walk-forward, per gameweek, per position, over 2025-26. Mean Spearman on the
*startable* pool:

| predictor | GKP | DEF | MID | FWD |
|---|---|---|---|---|
| last gameweek's points | 0.254 | 0.197 | 0.251 | 0.224 |
| trailing 3-GW mean | 0.219 | 0.170 | 0.218 | 0.216 |
| season-to-date mean | 0.220 | 0.208 | 0.219 | 0.198 |
| **trailing minutes** | **0.297** | **0.253** | **0.277** | **0.239** |

**The match model against that bar.** Ridge strength was selected on GW6-24 and
the result claimed on GW25-38, so the comparison is not the model marking its
own homework. Mean Spearman, startable pool, 14 held-out gameweeks:

| position | match model | best naive baseline | edge |
|---|---|---|---|
| GKP | **0.340** | 0.300 (last gameweek) | +0.041 |
| DEF | **0.378** | 0.243 (trailing minutes) | +0.135 |
| MID | **0.430** | 0.266 (trailing minutes) | +0.164 |
| FWD | **0.409** | 0.247 (trailing 5-GW mean) | +0.163 |

Top-of-slice precision improves in every position too (e.g. MID 0.347 vs
0.292), and stage one's `P(start)` is calibrated rather than merely ranked
(Brier 0.16-0.20, predicted start rate within ~3pp of observed). Reproduce with
`uv run python -m backend.ml.matchmodel --backtest`.

Two results that shape the modelling:

- **Minutes out-rank points.** Ranking players purely by recent minutes beats
  every points-based form measure, in every position. Playing time is the
  product; scoring is the margin — so a minutes model is stage one of the
  match model, not a detail.
- **The evaluation pool doubles the headline.** The same predictors score
  0.42–0.65 across the full panel, because most rows are players who were
  never going to feature and predicting zero for them is free accuracy. Every
  metric here names its pool; the honest one is the smaller number.

**Minutes and the cold-start problem** (`docs/MINUTES_FINDINGS.md`) — new
managers and summer transfers make last season untrustworthy, so the question
is *how* untrustworthy, and for how long:

- One gameweek of current-season evidence already outranks a full prior
  season (0.765 vs 0.611). From GW3 the optimal weight on last season is
  **zero**.
- Inside that window, club moves are the failure mode: prior start rate
  correlates 0.656 for players who stayed and **0.245** for those who moved,
  and only 40% of last season's nailed starters who changed club are still
  nailed.
- Trailing 3-gameweek mean minutes is the strongest single predictor of both
  future starts (0.791) and future minutes (0.835). A single gameweek is the
  worst predictor tested — recency wins, overreaction does not.

Supporting analysis: `backend.ml.eval` (season-level backtests) and the
per-stat correlation study over all 29,747 player-gameweeks. Roadmap and open
questions: `docs/MODEL_ROADMAP.md`.

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

Then turn on the autopilot (macOS) — after this, no routine commands at all:

```bash
make autopilot   # always-on server + data/artifact refresh every 15 min + deadline checklists (launchd)
make checklist-plan                    # next deadlines with computed research/delivery times
make checklist-preview KIND=waivers    # render the next checklist to stdout, send nothing
make update      # after a merge: pull latest code + restart the server
make stop / make start   # pause / resume without uninstalling
make tui         # game days: self-feeding live dashboard (auto-fetch every 60s)
make autopilot-off
```

`make autopilot` is safe to re-run: each agent is booted out, waited on until launchd
forgets it, then re-bootstrapped (with retries); a failing agent is reported by label and
the rest still load (nonzero exit at the end).

Manual equivalents when you want them: `make serve` / `make weekly` (fetch +
derive: ownership, then season panel && next-GW xP && 3-GW horizon (`-`-prefixed: a failure warns and waiver/my_week fall back — to the heuristic without the xP file, to the next-GW ranking without the horizon file), then the season's `team_env.json` from that panel (also `-`-prefixed), then waiver and my_week with `SCORER={heuristic,model}` (waiver also `HORIZON={1,3,ros}`, default 3), then the model's weekly track record (`track_record.csv`/`.md`: xP vs realized per finished GW, `live` or `replay`); `SEASON` defaults to 2026-27 and the flat `data/` layout is never written; `make xp GW=n` builds one specific GW's xP and horizon files) / `make matchday` (5-min refresh loop) / `make preflight` (local CI).

`make fetch` skips settled gameweeks: a past GW whose bootstrap event is
`finished` and whose cached `live.json` is a complete payload with every fixture
`finished` (bonus confirmed, not just `finished_provisional`) is not
re-downloaded, nor are its entry picks; the current GW and anything not yet
settled are always refetched, and each run logs `requests_fetched` /
`requests_skipped`. "Complete" means non-empty `elements`, each with integer
(non-null, non-string, non-fractional) `stats.total_points` and `stats.minutes`,
and — once that GW's entry picks are cached — an element for every player the
league's entries picked. The draft API has no `data_checked` flag, so that is
the settled test. `make fetch-all` (`--refetch-all`) forces the full per-GW
pull, e.g. after an FPL points correction. Independently of settlement, a cached
live file failing those checks, or an entry-event file that is malformed or not
a full squad (15 picks, each with `element` > 0, `position` 1..15, no duplicate
elements or positions), is re-downloaded on any run and logged as
`forcing refetch` with its `reason`. The one exception is an empty picks
response for a GW before the entry took part — before the league's
`start_event` (`league/<id>/details.json`) or the entry's first
`history.json` row (late joiners) — which is final and kept. If neither start
is known, empty picks are treated as corrupt and refetched.

A settled GW's entry picks are only skipped if they were downloaded *after* the
GW was finalised: once a GW is over the draft API's picks include automatic
substitutions (subs moved into positions 1–11), so a squad cached mid-GW would
make the points builder drop the substitute (e.g. 60 instead of 69). The payload
cannot reveal this (`subs` is `[]` both mid-GW and when no sub happened), so the
first run to see a GW settled writes `gw/<n>/entries_final.json`; each non-empty
entry-event file older than that marker is refetched once
(`reason="picks fetched before GW finalised"`) and is newer than it afterwards.
The empty pre-start picks above are exempt. Deleting the marker re-triggers one
refetch of that GW's picks.

Non-Mac or cron fans: schedule `scripts/autorefresh.sh` (crontab example inline).

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
    fpl-server/          14 MCP tool handlers + HTTP server (X-API-Key auth)
    cmd/dev/             FPL data fetcher (the only live-API component)
    cmd/tui/             live matchday dashboard (bubbletea)
    internal/            fetch, store, ledger, points, summary, config
  backend/               Python package
    backend/ml/          ingestion → parquet, projection model, waiver_plan,
                         my_week, drop-radar, matchfeatures (leakage-safe per-GW
                         training table), matchmodel (two-stage match xP model),
                         matcheval (walk-forward benchmark), trackrecord
                         (weekly xP-vs-realized log),
                         specs (treat *_SPEC.md as contracts)
    tests/               pytest suite (no network)
data/                    Raw + derived FPL data (gitignored; flat = 25/26 archive)
docs/                    Setup, design docs, model roadmap, measured findings
scripts/                 autorefresh (launchd/cron), autopilot install, deadline_tick, preflight, notifications
Makefile                 every operation: serve/fetch/derive/weekly/tui/autopilot/update/...
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

The pre-MCP chat stack (a FastAPI server, an OpenAI agent, a RAG index, a
scheduler, and the `apps/web` UI) was removed once Claude over MCP replaced
it; it remains in git history.
