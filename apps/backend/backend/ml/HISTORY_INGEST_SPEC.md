# Phase A — Historical Multi-Season Ingestion (Spec)

Build the canonical, cross-season player table that all next-season modeling
(Phases B/C) is built on. This document is the contract: an implementer (human or
agent) should be able to build the ingester from this alone. Every fact below was
verified empirically on 2026-06-05 — do not re-assume, just implement.

## Goal

Produce ONE tidy table: **one row per `(code, season)`**, written to
`data/derived/ml/player_seasons.parquet` (git-ignored). This is the season-level
aggregate per player, the spine for predicting a player's *next-season* output.

Out of scope here: per-90 rates, trends, team-strength, the prediction target.
Those belong to Phase B (feature engineering). Phase A ingests RAW season
aggregates + identity only. Keep the layers separate.

## Data sources

1. **Historical seasons 2019-20 … 2024-25** — vaastav/Fantasy-Premier-League.
   Per-season player aggregates at:
   `https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data/<season>/players_raw.csv`
   Team id→name map (per season, ids are NOT stable across seasons):
   `.../data/<season>/teams.csv`
2. **2025-26** — use the project's OWN local data (more complete, exact schema):
   `data/raw/bootstrap/bootstrap-static.json` → `elements[]`. Do NOT pull 25-26
   from vaastav; we backfilled the full 38-GW season locally.

Available vaastav seasons (verified): 2016-17 through 2025-26. Ingest 2019-20
onward by default (configurable start season).

## The join key — VERIFIED, do not deviate

Join players across seasons by the **permanent `code`** field, NEVER the
per-season `id`. Proof (Haaland): `code=223094` in both vaastav 2024-25 and local
2025-26, while `id` was 351 vs 430. `id` is reassigned every season; `code` is the
player's permanent identifier. A required test must assert a known player maps to
ONE code across ≥2 seasons.

## Column availability across seasons — VERIFIED

| Column | 2019-20 | 2020-21 | 2021-22 | 2022-23 → 2025-26 |
|---|---|---|---|---|
| `code`, `id`, `element_type`, `total_points`, `minutes`, `now_cost`, `team` | ✅ | ✅ | ✅ | ✅ |
| `goals_scored`, `assists`, `clean_sheets`, `bonus`, `bps`, `ict_index` | ✅ | ✅ | ✅ | ✅ |
| `expected_goals`, `expected_assists`, `starts` | ❌ | ❌ | ❌ | ✅ |

Rule: when a column is absent for a season, write **null** (not 0). LightGBM
handles nulls natively downstream; 0 would corrupt per-90 math in Phase B.

## NOT available from these sources (do not fabricate)

- **Age / birth date** — FPL data has none. The earlier draft plan mentioned
  "age if available"; it is NOT. Omit it. (Could be sourced from Understat /
  Transfermarkt later; out of scope for Phase A.)
- Anything requiring per-GW granularity (use vaastav `gws/merged_gw.csv` in a
  later phase if needed; not required here).

## Canonical schema — `player_seasons.parquet`

One row per `(code, season)`:

| Column | Type | Source | Notes |
|---|---|---|---|
| `code` | int | both | permanent player id — primary join key |
| `season` | str | derived | e.g. "2024-25" |
| `season_id` | int | `id` | per-season id (keep for debugging/joins within a season) |
| `web_name` | str | both | |
| `element_type` | int | both | 1=GK 2=DEF 3=MID 4=FWD |
| `team_id` | int | `team` | per-season team id |
| `team_name` | str | teams.csv / local teams | nullable if unmapped |
| `minutes` | int | both | |
| `starts` | int? | both | null pre-2022-23 |
| `total_points` | int | both | season total |
| `goals_scored` | int | both | |
| `assists` | int | both | |
| `clean_sheets` | int | both | |
| `goals_conceded` | int | both | |
| `saves` | int | both | GK |
| `bonus` | int | both | |
| `bps` | int | both | |
| `ict_index` | float | both | |
| `influence` | float | both | |
| `creativity` | float | both | |
| `threat` | float | both | |
| `expected_goals` | float? | both | null pre-2022-23 |
| `expected_assists` | float? | both | null pre-2022-23 |
| `now_cost` | int | both | price in tenths (e.g. 145 = £14.5m). NOTE: the 2025-26 *draft* bootstrap has no price field, so now_cost is **null for 2025-26 only** (Draft FPL has no transfer prices). Backfillable from vaastav 2025-26 players_raw later if a price feature is wanted. |

Keep raw aggregates only. Do not pre-divide by minutes here.

## Source field mapping

- **vaastav CSV → canonical**: column names already match the canonical names
  above (vaastav mirrors the FPL bootstrap schema). Cast numeric strings to
  int/float; empty string → null. Map `team` (season id) → `team_name` via that
  season's `teams.csv` (`id`,`name`).
- **local 2025-26 JSON → canonical**: `bootstrap-static.json` →
  `elements[]` has the same field names. Map `team` → name via the same file's
  `teams[]` (`id`,`name`,`short_name`).

## Edge cases to handle

- A player appears in N seasons → N rows, same `code`. Correct, not a dup.
- `(code, season)` must be UNIQUE — assert it.
- Players who left the league simply have no row in later seasons. Fine.
- Numeric fields stored as strings in CSV/JSON — coerce defensively; never crash
  on a bad cell, coerce-or-null and continue.
- Network: fetch vaastav over HTTPS; cache downloaded CSVs under a temp/scratch
  dir so re-runs don't re-download. No live FPL API calls.

## Output & acceptance criteria

- Writes `data/derived/ml/player_seasons.parquet` (git-ignored — confirm the
  `.gitignore` already covers `data/`).
- Prints a summary table: rows per season, distinct players, % xG-null per season.
- Tests (pytest, no network — use small CSV/JSON fixtures in `tmp_path`):
  1. Known player (e.g. Haaland code 223094) maps to ONE code across ≥2 seasons.
  2. No duplicate `(code, season)` pairs.
  3. xG is null (not 0) for a pre-2022-23 fixture row.
  4. Numeric coercion: a fixture with an empty/garbage cell yields null, no crash.
- Per repo CLAUDE.md: feature branch, conventional commits, `scripts/preflight.sh`
  must pass before any PR. Document assumptions/inputs/outputs.
