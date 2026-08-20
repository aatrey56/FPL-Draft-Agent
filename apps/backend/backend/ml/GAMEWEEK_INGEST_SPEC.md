# Phase A.2 — Per-Gameweek Panel Ingestion (Spec)

Companion to `HISTORY_INGEST_SPEC.md`. That spec produces season *aggregates*;
this one produces the **match-level panel** — the granular table the match model
needs and the season model uses for form/finishing-skill features. This document
is the contract: build the ingester from this alone.

## Goal

Produce ONE tidy table: **one row per `(code, season, gw)`**, written to
`data/derived/ml/player_gameweeks.parquet` (git-ignored). This is the per-player,
per-gameweek record — the spine for form curves, minutes stability, opponent
context, and the match-level expected-points target.

Out of scope: rolling/lag features, opponent-strength aggregates, the prediction
target. Those belong to feature engineering (see the model specs). Phase A.2
ingests RAW per-GW facts + identity + fixture context only. Keep layers separate.

## Data sources

1. **2025-26 (priority, local, complete)** — the project's own data:
   `data/raw/gw/{gw}/live.json` for gw = 1..38. Verified present: all 38 GWs,
   each with `elements` (dict keyed by per-season element `id`) → `{stats,
   explain}`, plus a `fixtures` array. `stats` includes minutes, total_points,
   goals_scored, assists, clean_sheets, saves, goals_conceded, bonus, bps,
   expected_goals, expected_assists, expected_goal_involvements,
   expected_goals_conceded, defensive_contribution, tackles, recoveries,
   clearances_blocks_interceptions, threat, creativity, influence, ict_index,
   starts, yellow_cards, red_cards, own_goals, penalties_missed/saved.
2. **Prior seasons (optional, later)** — vaastav `gws/merged_gw.csv` per season.
   Lower priority: 25/26 alone is enough to train and validate the match model.
   Only ingest prior-season per-GW if a task asks.

Do NOT call the live FPL API. Read local files only.

## Identity — the id→code mapping (critical)

`live.json` keys players by the **per-season `id`**, which is reassigned yearly.
The panel MUST store the permanent **`code`** (same join key as
`HISTORY_INGEST_SPEC.md`). Build an `id → code` map from that season's bootstrap:
`data/raw/bootstrap/bootstrap-static.json` → `elements[]` gives both `id` and
`code` (and `element_type`, `team`). A required test must assert a known player's
GW row carries the correct `code`.

## Opponent & home/away

`live.json` gives per-player stats but not directly "who did they play." Derive:

1. Player's team = `bootstrap.elements[id].team` (per-season team id).
2. That GW's fixtures = `live.json["fixtures"]` (each has `team_h`, `team_a`,
   `team_h_score`, `team_a_score`, `finished`, and an `id`).
3. Match the player's team to the fixture where it appears as home or away →
   `opponent_team` (the other side) and `was_home` (bool).
4. Map team ids → names via `bootstrap.teams[]` (`id`, `name`, `short_name`).

**Double gameweeks (DGW):** `event/{gw}/live` reports a player's stats **already
summed across both fixtures** for the GW — it is not per-fixture. The per-fixture
split lives in the element's `explain` array (one entry per fixture). For v1,
keep ONE row per `(code, season, gw)` with summed stats and record
`num_fixtures` (len of `explain`); if `num_fixtures == 2`, set `opponent_team` /
`was_home` to null and rely on `num_fixtures` (a later phase can split via
`explain` if per-fixture opponent context is wanted). GW31 and GW34 of 25/26 are
low-fixture (blank) GWs — expected, not missing data.

**Blank gameweeks:** a player whose team has no fixture that GW either has no
element entry or an all-zero `stats` with `minutes == 0`. Represent as a row with
`played = (minutes > 0)` and `num_fixtures = 0`; do not fabricate an opponent.

## Canonical schema — `player_gameweeks.parquet`

One row per `(code, season, gw)`:

| Column | Type | Notes |
|---|---|---|
| `code` | int | permanent player id — primary join key |
| `season` | str | e.g. "2025-26" |
| `gw` | int | 1..38 |
| `season_element_id` | int | per-season `id` (debug/join within season) |
| `element_type` | int | 1=GKP 2=DEF 3=MID 4=FWD |
| `team_id` | int | player's per-season team id |
| `opponent_team` | int? | null on DGW/blank |
| `opponent_name` | str? | null if unmapped/DGW |
| `was_home` | bool? | null on DGW/blank |
| `num_fixtures` | int | 0 (blank), 1 (normal), 2 (DGW) |
| `played` | bool | minutes > 0 |
| `started` | bool | from `starts` stat (== 1) |
| `minutes` | int | |
| `total_points` | int | the match-level target |
| `goals_scored`, `assists`, `clean_sheets`, `goals_conceded`, `saves` | int | |
| `bonus`, `bps` | int | |
| `expected_goals`, `expected_assists`, `expected_goal_involvements`, `expected_goals_conceded` | float | |
| `defensive_contribution`, `tackles`, `recoveries`, `clearances_blocks_interceptions` | int | new-system defensive signals |
| `threat`, `creativity`, `influence`, `ict_index` | float | |
| `yellow_cards`, `red_cards` | int | availability/suspension signal |

Store RAW per-GW facts only. Do not compute per-90 or rolling features here.

## Edge cases

- `(code, season, gw)` must be UNIQUE — assert it.
- Coerce numeric-as-string / missing cells defensively → null (floats) or 0
  (counts) as appropriate; never crash on a bad cell.
- A mid-season new element appears only from its first GW onward — fine.
- Keep the panel long (one row per GW), not wide — downstream feature code pivots.

## Output & acceptance criteria

- Writes `data/derived/ml/player_gameweeks.parquet` (git-ignored).
- Prints a summary: rows total, distinct players, GWs covered, % rows with
  `played`, count of DGW rows and blank-GW rows.
- Tests (pytest, no network — small JSON fixtures in `tmp_path`):
  1. A known player's GW1 25/26 `total_points` matches the raw `live.json` value.
  2. `id → code` mapping is correct for a known player (uses a fixture bootstrap).
  3. No duplicate `(code, season, gw)`.
  4. Opponent + `was_home` correctly derived for a single-fixture GW; null for a
     synthesized DGW row (`num_fixtures == 2`).
  5. A blank-GW player yields `played == False`, `num_fixtures == 0`, no opponent.
- Per CLAUDE.md: feature branch, conventional commits, `scripts/preflight.sh`
  passes before PR. Document assumptions/inputs/outputs.
