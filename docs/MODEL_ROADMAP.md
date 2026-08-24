# Model roadmap — from heuristic serving to a backtested prediction engine

## Where this is

**Serving layer: built.** A 14-tool MCP server, a two-layout matchday TUI,
official team sheets and match events, projected auto-subs, a live league
table, and a predictions log. Data flows `fetch → derive → serve` near-live.

**Modelling layer: early.** A season-projection model (ridge + persistence,
honestly validated) and a per-gameweek *heuristic* placeholder
(`projection / 38 × fixture multiplier × availability`). The correlation study
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

### On `ep_next`

`MATCH_MODEL_SPEC.md` originally set FPL's own `ep_next` as the bar. It is not
reachable from this project's sources: the **draft** API returns the field but
leaves it null, and no historical archive (including the public vaastav mirror)
carries it per gameweek. Options, unresolved: amend the acceptance criterion to
the best naive baseline above, or add the public FPL API as a second read-only
source and snapshot `ep_next` weekly so a real comparison accumulates
prospectively. Until then, the baselines above are the bar.

## Phases

### Phase A — Match xP model
- **Minutes / start-probability model first**, availability-gated.
- **Component expected points**: appearance, goals, assists, clean sheet,
  goals conceded, saves, defensive contributions, bonus — each modelled on its
  own driver and summed, rather than one regression onto total points.
- **Fixture-aware**: venue-split team environment and the position × opponent
  matrix already built in `matchfeatures.team_form`.
- **Walk-forward evaluation** against the baselines above, per position, on
  the startable pool. No merge without the number.
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
