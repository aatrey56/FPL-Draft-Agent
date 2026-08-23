# Match Expected-Points Model (Spec) — the Weekly Co-Pilot's brain

Predicts a player's **FPL points for the next fixture** (expected points, `xP`).
Powers the weekly tools: `my_week` (start/sit), `waiver_plan` (add/drop),
`trade_check`. The second of the two models (see `docs/RESHAPE_PLAN.md`); the
first is the season model (`PROJECTION_MODEL_SPEC.md`). Build from this contract.

Depends on `player_gameweeks.parquet` (GAMEWEEK_INGEST_SPEC). This is where the
user's "how do I know a player will have a good game" mental model is encoded:
form, intrinsic quality, opponent strength, opponent context.

## Target

For each player and an upcoming gameweek, predict **`total_points` for that GW**
(the match-level label already in the panel). Sum across fixtures for a double
gameweek; a blank GW is `xP = 0`.

## Core structure — availability-gated two-stage

A great player who doesn't start scores ~0. Gate on availability:

```
xP = P(start) · E[points | started]  +  P(cameo) · E[points | cameo]
```

- **Stage 1 — start probability `P(start)`.** The single biggest lever and the
  hardest input. **v1 (compute-now):** heuristic from recent `started`/`minutes`
  (e.g. share of last 5 GWs started, trending) combined with the current FPL
  availability flag from bootstrap (`status`, `chance_of_playing_this_round`).
  **v2 (Tier-2 external):** replace/augment with a predicted-lineups feed
  (Fantasy Football Scout, Rotowire, odds). Design Stage 1 as a swappable
  component so the external feed drops in later without touching Stage 2.
- **Stage 2 — expected points given minutes.** Regression on the feature set
  below, per position.

## Features (Tier-1 — all derivable from data on disk)

Computed as-of the deadline (NO leakage — only GWs strictly before the target):

1. **Form** — rolling last-N (N≈4-6) per-90 `total_points`, `xGI`, `minutes`,
   `bps`; and a short/long form ratio. (The form/baseline blend weight is a key
   tunable — set by backtest, not guessed.)
2. **Player baseline & consistency** — season-to-date per-90 output, and a
   **floor/ceiling** measure (mean and variance/percentiles of GW points). A
   high-floor player is safer for start/sit; a high-ceiling one for differentials.
3. **Opponent defensive strength** — opponent per-90 `expected_goals_conceded` /
   goals conceded, **home/away split**, over a trailing window. This is the
   "playing a bad team" signal. (Reuse/clean up the existing `fixture_difficulty`
   / `strength_of_schedule` logic as the source.)
4. **Home/away** for the player's own fixture.
5. **Fixture congestion / rest** — days since last fixture, DGW flag (from
   `num_fixtures`).
6. **Position-specific weighting** (RESHAPE_PLAN §6): GK/DEF lean on own-team
   clean-sheet odds (opponent *attack* strength + own defence) and, for DEF,
   attacking involvement + `defensive_contribution`; MID/FWD lean on own attacking
   form × opponent *defensive* weakness.

## Features (Tier-2 / Tier-3 — external, deferred)

- **Tier-2:** predicted lineups (upgrades Stage 1 dramatically); live injury/
  suspension state per GW (FPL flags cover the current GW for the live model).
- **Tier-3:** opponent "key player absent," manager change / new-manager bounce,
  tactical setup. Low ROI, external, parked. Note: opponent recent *form* (last-N
  xG/results) IS Tier-1 and can proxy "opponent in a rut" without lineup data.

Do not let Tier-2/3 block Tier-1 — form + opponent strength + start-probability
capture the majority of the signal.

## Blank / double gameweeks (per CLAUDE.md FDR rules)

- A player may have 0, 1, or 2 fixtures in a GW. Compute `xP` per fixture and
  **sum**; blank = 0. Surface `num_fixtures` so tools can flag DGW upside.
- Never assume every player has exactly one fixture.

## Model choice

- v1: explainable per-position regression (`LightGBM`/linear). Must expose the
  drivers so `my_week` can say *why* ("nailed starter, soft home fixture vs a
  leaky defence, in form").
- **Baseline to beat: FPL's own `ep_next`** (already in bootstrap). Report the
  improvement over it — if the model can't beat `ep_next`, diagnose before adding
  complexity.

## Outputs

- Per player, `xP` for the upcoming GW (and over a short horizon for planning),
  with: start-probability, floor/ceiling band, top drivers, DGW/blank flag,
  availability flag. Written to `data/derived/ml/xp_gw{gw}.parquet` (git-ignored),
  consumed by the weekly tools.
- `waiver_plan` uses `xP` as **marginal value over my worst startable at that
  position**; `my_week` uses it for start/sit ordering; `trade_check` sums `xP`
  over a horizon for each side.

## Validation (walk-forward — required)

- **Walk-forward over 25/26:** for t = N..37, train on GWs 1..t, predict GW t+1,
  compare to actual. Strictly no future data (deadline-time features only).
- Metrics, per position: Spearman rank correlation (start/sit cares about order),
  MAE on GW points, top-N precision (did the predicted top starters actually
  return), and **calibration** of `P(start)`.
- **Beat `ep_next`** on the same GWs — assert improvement.
- Backtest Stage 1 separately: does `P(start)` predict actual `started`?

## Acceptance criteria

- Builds `xp_gw{gw}.parquet` offline from `player_gameweeks.parquet` (+ current
  bootstrap for availability flags).
- Prints the walk-forward report (per-position + vs `ep_next` baseline).
- Tests (pytest, no network, small fixtures):
  1. Availability gate: a flagged-out player (`chance_of_playing == 0`) gets
     `P(start) ≈ 0` and low `xP` regardless of form.
  2. No leakage: features for GW t+1 use only rows with `gw ≤ t`.
  3. DGW: a player with `num_fixtures == 2` gets `xP` summed over both fixtures.
  4. Beats `ep_next` (Spearman) on a held-out slice of 25/26 GWs, per position.
- Per CLAUDE.md: feature branch, conventional commits, preflight passes,
  deterministic (seed), document assumptions/inputs/outputs.

## Relationship to the season model

The season model predicts a **stable annual total** (draft); this model predicts
a **volatile weekly number** (management). They share the feature foundation
(per-90 rates, opponent strength, minutes) but differ in horizon, target, and
which signals dominate (season = durability + repeatable quality; match = form +
opponent + who-starts). Keep them as separate modules under `ml/`.

## Fixture-aware market targeting (the waiver product contract)

The model's xP is not the end product — the end product is *"who should I pick
up because of who they play."* The user's framing (2026-08-23): target
transactions at players facing bad teams / easy grounds, position-aware —
a defender against a toothless attack (clean-sheet buy), an attacking mid or
forward against a leaky defence (goal-involvement buy). Judge every component
of FPL points, not just goals.

### Two-sided team environment, per venue

For each fixture derive Poisson-style team expectations from `team_env`
(re-estimated on 26/27 data as it accrues, with the club-change discount):

- `lambda_for` / `lambda_against` per team **split by venue** — "relegation
  fodder away" and "solid at home" are different opponents.
- `P(CS)`, expected goals conceded, and expected shots faced (save volume).

### Position × opponent interaction matrix (what to buy, against whom)

| Buy…            | When the opponent…                | Point source captured |
|-----------------|-----------------------------------|-----------------------|
| GKP             | attacks poorly (CS odds)          | CS + low GC risk      |
| GKP (budget)    | concedes many *shots* (weak team) | save points volume    |
| DEF             | attacks poorly                    | CS, GC, bonus         |
| DEF (attacking) | + defends set pieces badly        | goal/assist upside    |
| MID (attacking) | defends poorly (high xGA)         | goals 5 / assists 3   |
| MID (defensive) | possession-heavy vs their block   | DC threshold points   |
| FWD             | defends poorly, high line         | goals 4               |

xP decomposes per component (appearance, goals, assists, CS, GC, saves, DC,
bonus proxy) so tools can explain *which* component the fixture inflates.

### Horizons and outputs

- Score the FA pool + current roster over **1 GW / next 3 GWs / ROS** (CLAUDE.md
  §5.2 horizons). A pickup for this week's fixture is a different product than
  a run-of-fixtures hold — surface both.
- `waiver_plan` consumes fixture-adjusted xP and stays roster-aware (marginal
  value over my worst startable at that position, league_size=12 scarcity).
- New derived view `fixture_targets`: per position, top FA-pool players ranked
  by fixture-adjusted xP over the chosen horizon, with the component driver
  ("home vs SUN, CS 48%") — this is the "make the most of the market" scan.
- my_week/trade_check reuse the same per-fixture components so all advice
  agrees on why.

### Guards

- Early season: shrink team rates toward priors, weight underlying xG/xGA over
  results; promoted sides get a promoted-team prior, not zeros.
- Never hardcode user holds (Madjo, Kroupi Jr) — advice layer only.
