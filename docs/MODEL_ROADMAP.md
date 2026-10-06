# Model roadmap — from heuristic serving to a backtested prediction engine

## Where this is

**Serving layer: built.** A 14-tool MCP server, a two-layout matchday TUI,
official team sheets and match events, projected auto-subs, a live league
table, and a predictions log. Data flows `fetch → derive → serve` near-live.

**Modelling layer: two models built.** A season-projection model (ridge +
persistence, honestly validated) and, as of 2026-08-24, the **match xP model**
(`backend.ml.matchmodel`) — two-stage, availability-gated, per position, and
measured against the baselines below on held-out gameweeks. The weekly tools
still serve the *heuristic* placeholder
(`projection / 38 × fixture multiplier × availability`); wiring them onto
`matchmodel` is the next task. The correlation study
(`data/derived/reports/stat_correlations_2526.md`) and the walk-forward
baseline benchmark (`backend.ml.matcheval`) are the analytical foundation.

## The data

- ~29.7k player-gameweek rows for 2025-26, 35 columns including xG, xA, xGC,
  BPS, defensive contributions, ICT, venue and opponent — underlying signals,
  not only outcomes.
- Six prior seasons (2019-20 → 2024-25) at season aggregate, joinable on the
  permanent `code`.

## Goal

A research-grade, backtested per-gameweek prediction engine that:

1. predicts points as a **distribution** (floor / median / ceiling,
   `P(haul)`), not a point estimate;
2. **beats the best naive baseline** on walk-forward evaluation, per position,
   with the improvement measured and logged every time the model changes;
3. carries a **live, calibrated track record** proving the edge week over week;
4. is reproducible end to end: ingestion → features → models → evaluation →
   serving → monitoring.

## Baselines (measured, 2025-26, GW≥6)

`backend.ml.matcheval` scores every predictor per gameweek, per position, over
two pools. Mean Spearman on the **startable** pool (players with recent
minutes — the set a manager actually chooses between):

| predictor | GKP | DEF | MID | FWD |
|---|---|---|---|---|
| last gameweek | 0.254 | 0.197 | 0.251 | 0.224 |
| trailing 3 mean | 0.219 | 0.170 | 0.218 | 0.216 |
| trailing 5 mean | 0.203 | 0.157 | 0.192 | 0.239 |
| season-to-date mean | 0.220 | 0.208 | 0.219 | 0.198 |
| **trailing minutes** | **0.297** | **0.253** | **0.277** | **0.239** |

Two findings that shape everything downstream:

- **Trailing minutes out-ranks every points-based predictor**, in every
  position. Playing time is the product; scoring is the margin. A minutes model
  is therefore stage one of the match model, not a detail.
- **Pool choice dominates the headline.** The same predictors score 0.42–0.65
  across the full panel, because most rows are players who were never going to
  feature and predicting zero for them is free accuracy. Any reported number
  must name its pool.

## The match model against those baselines (measured, held out)

`backend.ml.matchmodel --backtest` selects the ridge strength per position on
GW6-24 and reports on GW25-38. The split is the point: tuning and reporting on
the same gameweeks would make "beats the baseline" a claim about one season's
noise. Mean Spearman, **startable** pool, 14 held-out gameweeks:

| position | model | best baseline | edge | model top-20% | baseline top-20% | model MAE |
|---|---|---|---|---|---|---|
| GKP | 0.340 | 0.300 (last GW) | +0.041 | 0.275 | 0.229 | 1.99 |
| DEF | 0.378 | 0.243 (minutes) | +0.135 | 0.371 | 0.268 | 2.18 |
| MID | 0.430 | 0.266 (minutes) | +0.164 | 0.347 | 0.292 | 1.97 |
| FWD | 0.409 | 0.247 (mean l5) | +0.163 | 0.298 | 0.258 | 2.30 |

Stage 1 is calibrated, not merely ranked: Brier 0.16-0.20 with predicted start
rate within ~3pp of observed, per position.

Selected ridge strengths: GKP 1000, DEF 300, MID 100, FWD 100. The keeper
number is the interesting one — with ~18 startable keepers a gameweek, stage 2
is shrunk almost flat and the honest keeper model is very nearly *"is he the
number one, and is the opponent poor going forward"*.

### What to distrust in these numbers

- **One season, 14 gameweeks.** GKP's +0.041 is the thinnest edge on the
  smallest pool; treat it as "no worse than the baseline" rather than an edge.
- **The feature set was designed knowing the full-season baseline table.**
  Alpha selection is held out; feature *choice* is not. The next season is the
  real test.
- **Availability is inert in the backtest.** The panel carries no historical
  injury flags, so the gate contributes nothing to these numbers and only bites
  when serving live. That makes the backtest a floor, not a ceiling.

### Cold start: what this means for August

Trailing features are keyed on `(code, season)`, so in GW2 of a new season the
model has exactly one gameweek of within-season form. Measured on the real
2026-27 GW2 artifact: xP rank correlates 0.84 with GW1 points across the whole
pool, and 0.32 among likely starters — i.e. outside the "did they play at all"
signal the model is still adding fixture and role information, but the form
half of it rests on a single match. Early-season output should be read as
*fixture + role, lightly tinted by one gameweek*, and the cross-season priors
in RESHAPE_PLAN §6 remain the fix.

### Serving fix: team form was one gameweek stale (2026-10-06)

`matchfeatures._carry_team_form` filled a gameweek with no played rows — a
blank, or the outcome-free stub rows `build_gw_xp` scores — with the *pre*-GW
team-form value of the team's last played gameweek. Serving's
`team_scored_pg`, `team_conceded_pg`, `opp_scored_pg` and `opp_conceded_pg`
therefore ignored the most recent completed gameweek, and were all NaN at GW2
(so the cold-start GW2 numbers above were measured without team form). Such
gameweeks now carry the *post*-GW value: the mean over every played gameweek
strictly before the target, exactly what a training row for that gameweek
sees. Played rows are unchanged (verified identical on the 2025-26 and 2026-27
panels).

Measured impact:
- **GW6 2026-27 xP:** mean absolute change 0.03 (median 0.01, max 0.30) over
  667 players; 18 of the top 20 and 48 of the top 50 unchanged. Small because
  at GW6 the missing gameweek is one of five in an expanding mean; the
  reviewer's rebuild saw shifts of up to 2.2 at GW2, where the old value was
  NaN.
- **2025-26 backtest:** byte-identical. Only blank-gameweek rows changed
  (409), and those never reach stage 2 (not played) or a score (0 fixtures).

### On `ep_next`

`MATCH_MODEL_SPEC.md` originally set FPL's own `ep_next` as the bar. It is not
reachable from this project's sources: the **draft** API returns the field but
leaves it null, and no historical archive (including the public vaastav mirror)
carries it per gameweek. Options, unresolved: amend the acceptance criterion to
the best naive baseline above, or add the public FPL API as a second read-only
source and snapshot `ep_next` weekly so a real comparison accumulates
prospectively. Until then, the baselines above are the bar.

## Live-season check — 2026-27 GW2-5

`uv run python -m backend.ml.matchmodel --eval-season 2026-27 --eval-gws 2-5
--panel <archive panel> <2026-27 panel> --out data/derived/2026-27/ml/` scores
each target gameweek *as it would have been served*
(`matchmodel.served_walk_forward`):

- **Training set:** every row of the seasons that started before 2026-27 (the
  2025-26 archive) plus 2026-27 rows with `gw < N` — what `build_gw_xp` fits
  on. Later seasons are never included.
- **Features:** the panel's GW-N rows are stripped to outcome-free stubs and
  featurised through `stub_frame`, the same function `build_gw_xp` uses, so
  the scored features are the served ones by construction (pinned by a parity
  test against `upcoming_fixture_rows` + `build_gw_xp`). Outcomes are attached
  only after scoring. This differs from `--backtest`, which trains within one
  season on panel rows.
- Alphas are the shipped `SELECTED_ALPHAS` (nothing tuned on these gameweeks);
  every metric averages over the same gameweeks (`gws`). It writes
  `derived/2026-27/ml/match_model_eval_gw2-5.csv`.

Mean per-gameweek Spearman, **startable** pool, 4 gameweeks (re-run 2026-10-06
on the serving path, after the team-form carry fix):

| position | model | best baseline | edge | model top-20% | baseline top-20% | model MAE |
|---|---|---|---|---|---|---|
| GKP | 0.391 | 0.223 (mean l5) | +0.168 | 0.312 | 0.250 | 2.26 |
| DEF | 0.342 | 0.226 (minutes) | +0.117 | 0.405 | 0.298 | 2.43 |
| MID | 0.345 | 0.291 (minutes) | +0.054 | 0.296 | 0.333 | 2.05 |
| FWD | 0.412 | 0.356 (minutes) | +0.056 | 0.462 | 0.488 | 2.69 |

The numbers match the first (pre-parity) run to three decimals: on finished
gameweeks the stub features equal the panel rows' own features once team form
carries the latest completed gameweek, so the earlier run's *numbers* stood —
it was serving, not the eval, that had been stale. The full pool agrees (model
ahead in all four positions, +0.03 to +0.12). Stage 1 holds up live: Brier
0.07 / 0.13 / 0.13 / 0.12 (GKP/DEF/MID/FWD), predicted start rate within 4pp
of actual.

**Verdict: model wins 4/4** (rule: startable-pool Spearman ≥ best baseline in
≥ 3 of 4 positions and within 0.02 on the fourth). Per the S1 → S2 decision
rule, the weekly tools should default to the model scorer (`--scorer model`)
once they are wired onto it; until then they still serve the heuristic.

Caveats: GW2 form is one gameweek; four gameweeks is a small sample (the
baselines differ by less than the week-to-week spread), and top-20% precision
is behind the baseline for MID/FWD even where Spearman is ahead. Availability
is inert here (no historical flags), as in the backtest. GKP `minutes_only` is
constant at GW2 (every startable keeper has one 90-minute game), so all its
metrics average 3 gameweeks, not 4; it is not the best GKP baseline.

**Known limitation — double and blank gameweeks.** Serving (`score_fixtures`)
scores each fixture of a double against its own opponent and sums. The panel
stores a double as one row with `num_fixtures == 2` and *no* opponent or venue
(an ingest gap: all 409 DGW rows of 2025-26 — GW26/33/36 — lack them), so the
eval scores such a row once with imputed opponent features × 2, and a blank
row (`num_fixtures == 0`) as 0 where serving has no row. Pinned by a test. It
does not touch this check: 2026-27 GW2-5 has **0** double and 0 blank rows.
The same gap reaches 8 single-fixture rows of 2026-27 GW2-5
(`num_fixtures == 1`, no `opponent_team`): the eval imputes their opponent
features where serving would know the opponent. Fixing both needs
per-fixture rows in `GAMEWEEK_INGEST`.

## Live track record — scored weekly

The live check above is a one-off. The standing answer is
`backend.ml.trackrecord` (#178: the model's own output is the accountability
log), run by `make derive` after the xP step:

```
uv run python -m backend.ml.trackrecord --season 2026-27 [--data-root ../../data] [--gws 3-5] [--rescore]
```

For every finished gameweek of the season it scores `model_xp` and the five
baselines per position, on the startable and full pools, plus Stage-1 Brier,
and upserts `derived/<season>/ml/track_record.csv` (one row per
`gw, pool, position, predictor, source`; 4 GWs × 2 pools × 4 positions × 6
predictors = 192 rows at GW5) and rewrites `track_record.md`.

- **`live`** — `xp_gw{N}.parquet` exists and predates the GW-N deadline: the
  forecast actually served. Derive only writes the *next* GW, so a past file is
  a pre-deadline forecast. A file built after the deadline (by its
  `generated_at` parquet stamp, else its mtime; e.g. a manual `make xp GW=n`),
  or one whose `gw` column is not N, is ignored with a warning; live rows
  already in the CSV are kept even if the file later disappears.
- **`replay`** — otherwise: rebuilt as it would have been served, through
  `matchmodel.served_walk_forward` (the live-season check's path: earlier
  seasons + earlier GWs of this one, features from outcome-free stubs via
  `stub_frame`; today's code, no availability gate). Not live evidence, and
  the markdown labels every replay row as such.
- GW1 is skipped (no in-season history, so no trailing predictor can rank it).
  Each predictor is scored on the players the model covered that week. A re-run
  with unchanged inputs leaves the CSV byte-identical. No `ep_next` column: the
  draft API leaves it null.
- Gameweeks already in the CSV are not scored again (a replay re-fit takes
  ~70s and the autopilot derives every 15 minutes) unless `--rescore` is
  passed, they are named in `--gws`, or a recorded `replay` week has since
  gained a usable live file.

First record (2026-10-06, GW2 live, GW3-5 replay; re-run on the serving path the
same day — every replayed score unchanged), startable pool, mean Spearman:
GKP 0.289 vs 0.223 (mean l5), DEF 0.345 vs 0.226 (minutes), MID 0.305 vs 0.291
(minutes), FWD 0.406 vs 0.356 (minutes). The one live week is the warning: the
served GW2 forecast scored GKP −0.021 and MID 0.188, where the GW2 replay
scores 0.388 and 0.346. Replay flatters the model — the edge claimed in the
live-season check has to be re-earned by live rows from GW6 on.

## Phases

### Phase A — Match xP model  *(core built 2026-08-24)*
- ~~**Minutes / start-probability model first**, availability-gated.~~ Built:
  `matchmodel.StartModel`, two ridge heads (`started`, `played`) plus the live
  bootstrap availability factor. Deliberately swappable for a predicted-lineups
  feed without touching stage 2.
- ~~**Fixture-aware**~~ Built: opponent goals for/against and points-allowed-to-
  this-position are stage-2 features; fitting per position is what makes the
  weighting position-specific.
- ~~**Walk-forward evaluation** against the baselines above~~ Built, with the
  alpha-selection window held out. Numbers above.
- **Still to do:**
  - **Component expected points**: appearance, goals, assists, clean sheet,
    goals conceded, saves, defensive contributions, bonus — each modelled on
    its own driver and summed. Today stage 2 is a single regression onto
    points, so `drivers` names features rather than *point sources*, and the
    spec's position × opponent buy matrix is implicit in the coefficients
    rather than explicit in the output.
  - **Venue-split team environment** (`lambda_for`/`lambda_against` per venue)
    and the `fixture_targets` derived view.
  - **Wire `trade_check` onto it** — `waiver_plan` and `my_week` now consume
    the per-GW xP (see "Weekly tools on match xP" below); `trade_check` still
    uses the heuristic.
  - **Horizons**: xP is produced for one gameweek; the 3-GW and ROS horizons
    in CLAUDE.md §5.2 are not built.
  - Club-move flag and `expected_minutes` surfaced (fixes the case where
    persistence carries a departed starter's role into a new season).

### Phase B — Underlying-metrics and breakout detection
- Trend detection on xG / xA / xGI / minutes: flag players whose underlying
  output is rising before the points follow.
- **Luck adjustment**: separate skill from variance (finishing G−xG, BPS
  variance). The correlation study shows finishing luck does not repeat
  year over year — turn that into explicit regression-to-mean signals.
- Feeds `fixture_targets` and `waiver_plan` with a stated reason per pick.

### Phase C — Distributions and portfolio optimisation
- Quantile or simulation output: floor, ceiling, `P(haul ≥ 10)`. Start/sit and
  differential decisions care about ceilings, not means.
- Lineup and draft optimisation under formation and roster constraints, with a
  risk setting (chase ceiling vs protect floor) and 12-team scarcity.

### Phase D — Track record and writeup
- ~~Accumulate backtested and live predictions.~~ Built: `trackrecord.py`
  (see "Live track record" above); live rows accrue from GW6.
- Calibration plots, edge-vs-baseline charts, and a written account of what
  worked, what did not, and why.

## Waiver replay harness

`backend.ml.replay` rebuilds each 2026-27 waiver deadline (GW2-5 by default)
from the ownership snapshots and per-GW live files as they stood at
`waivers_time`, scores `waiver_plan`'s rank-1 add/drop against realized raw
points, and compares it with `no_change`, `std_points`, `form3` and my actual
moves (`me`). Run `uv run python -m backend.ml.replay --season 2026-27
--gws 2-5` from `apps/backend` (`--data-root` points at another checkout's
`data/`); it writes `derived/2026-27/ml/waiver_replay_<scorer>.json`
(`--scorer {heuristic,model}`). Availability is
neutralised (only the current status/news snapshot exists), so n=4 deadlines
proves the harness runs, not the model. The as-of contract and caveats are in
the module docstring.

## Weekly tools on match xP

`waiver_plan` and `my_week` take `--scorer {heuristic,model}`; the default is
`model` because the 2026-27 GW2-5 live check (startable pool, Spearman model
vs best baseline: GKP 0.391 vs 0.223, DEF 0.342 vs 0.226, MID 0.345 vs 0.291,
FWD 0.412 vs 0.356) met the win rule in 4 of 4 positions. The Makefile
`SCORER` variable and `replay` CLI default the same way. `model` joins
`xp_gw<N>.parquet` on the permanent `code`; players it does not cover (blank
GW) get the heuristic value, tagged `xp_source` = `heuristic` (`none` when
there is no projection either). `xp_gw<N>.parquet` is stale unless its `gw`
is N and its `panel_max_gw` (the last finished GW in the training panel,
stamped by `build_gw_xp`) is N-1; `make derive` chains the panel rebuild and
the xP build with `&&`, so a failed panel step never yields an xP file
trained on an old panel. Mid-gameweek the panel stops at N-2, so the model
file is rejected until the gameweek finishes. A missing or stale file falls back to the
heuristic for everyone with a WARNING, and the JSON says so: `scorer` is the
scorer actually used (`heuristic` after a fallback), beside
`scorer_requested`, `xp_fallback` and `xp_fallback_reason`. Waiver recs rank
by `next1_gain`; a free agent with no ROS projection but a model `xp_next`
(promoted clubs) is ranked too, with `season_gain` 0 (at most a `stream`);
`next3_xp` is still heuristic (horizons are a later section) and the drop
pick is unchanged. Known scale mismatch: model xP averages higher than the
`ros/38` heuristic fallback, so a heuristic-valued drop flatters model-valued
adds until the horizons work removes most fallbacks.

`replay --scorer model` rebuilds the match xP per deadline N from
`archive panel UNION season panel[gw < N]` with the same neutral availability
as the heuristic (a guard raises if any season gw >= N is present). Replay
`waiver_plan` rank-1 `gw_gain` totals over GW2-5 (n=4, a reported number, not
a gate): heuristic **-15.0**, model **+9.0**; `std_points` +7.0, `form3` +7.0
and `me` +2.0 are identical between scorers. Outputs go to
`waiver_replay_<scorer>.json`.

## Working rules

- Plain Python → parquet first (per `CLAUDE.md`); no warehouse until a task
  demands one.
- Every model change reports a walk-forward number against the baselines.
- Every reported metric names its evaluation pool.
