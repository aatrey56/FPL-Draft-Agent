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

### On `ep_next`

`MATCH_MODEL_SPEC.md` originally set FPL's own `ep_next` as the bar. It is not
reachable from this project's sources: the **draft** API returns the field but
leaves it null, and no historical archive (including the public vaastav mirror)
carries it per gameweek. Options, unresolved: amend the acceptance criterion to
the best naive baseline above, or add the public FPL API as a second read-only
source and snapshot `ep_next` weekly so a real comparison accumulates
prospectively. Until then, the baselines above are the bar.

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
  - **Wire the weekly tools onto it** — `waiver_plan`, `my_week` and
    `trade_check` still consume the per-GW heuristic.
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
- Accumulate backtested and live predictions.
- Calibration plots, edge-vs-baseline charts, and a written account of what
  worked, what did not, and why.

## Working rules

- Plain Python → parquet first (per `CLAUDE.md`); no warehouse until a task
  demands one.
- Every model change reports a walk-forward number against the baselines.
- Every reported metric names its evaluation pool.
