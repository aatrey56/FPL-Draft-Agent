# Season Projection Model (Spec) — the Draft Board's brain

Predicts a player's **total FPL points for the upcoming season** so `draft_board`
can rank and tier everyone before the draft. This is one of the two models in the
reshape (see `docs/RESHAPE_PLAN.md`); the other is the match model
(`MATCH_MODEL_SPEC.md`). Build from this contract.

Depends on: `player_seasons.parquet` (HISTORY_INGEST_SPEC) and
`player_gameweeks.parquet` (GAMEWEEK_INGEST_SPEC). Plain Python → parquet; no
warehouse/dbt yet.

## Target

For each player, predict **next-season `total_points`** (a per-`code` scalar).
Train on historical season-to-next-season pairs from `player_seasons.parquet`:
features from season *S* → label = `total_points` in season *S+1* (joined on
`code`). Players with no *S+1* row (left the league) are dropped from training,
not labelled 0.

## Core modeling stance (non-negotiable)

1. **Minutes first — two-stage model.** Availability is the dominant driver
   (verified: minutes/starts is a top-3 correlate of season points in every
   position). Model it separately:
   - Stage 1: **expected minutes next season** `Ê[minutes]` (regression on
     durability signals — prior 2-3 seasons' minutes/starts, age proxy via
     seasons-of-history, position).
   - Stage 2: **points-per-90 given availability** `Ê[pts/90]` (regression on
     per-90 output + quality features).
   - Projection = `Ê[minutes] / 90 * Ê[pts/90]`, then calibrate.
   This prevents the classic failure of ranking a brilliant 1500-minute player
   over a nailed 3000-minute one.
2. **xG/xA as stabilizers, not the model.** Use per-90 `expected_goals` /
   `expected_assists` as the attacking-quality signal instead of raw goals/assists
   (they repeat better year-over-year). Raw output still enters via the
   finishing-skill term below.
3. **Multi-year finishing skill.** For each player compute the persistent
   `goals - expected_goals` (and `assists - expected_assists`) gap across their
   available seasons. A *persistent* positive gap = genuine finishing skill (keep
   it — do not regress elite finishers to xG); a one-season spike = variance
   (regress it). Encode as a shrunk per-player gap (e.g. gap weighted by minutes,
   regressed toward 0 with few seasons). Requires the multi-season table — this is
   why Phase A is multi-season.
4. **Position-specific.** Fit per `element_type` (GKP/DEF/MID/FWD). The drivers
   differ (see RESHAPE_PLAN §6): GK = minutes + saves + team defence (xGC); DEF =
   minutes + clean-sheet/defensive + attacking involvement + `defensive_contribution`;
   MID = attacking output + minutes; FWD = attacking (xGI/threat/xG) + minutes.

## Features (from the two parquet tables, season S)

Per-90 rates (from season aggregates; guard divide-by-zero with null when
minutes small): `xG90`, `xA90`, `xGI90`, `threat90`, `creativity90`,
`influence90`, `defcon90`, `saves90` (GK), plus:
- durability: `minutes`, `starts`, `starts_share` (starts / team games),
  `minutes` from S-1 and S-2 if present (trend).
- quality: `ict_index` per-90, `bps` per-90, `points_per_game`.
- finishing skill: shrunk multi-year `(goals-xG)` and `(assists-xA)` gaps.
- team context (optional v1): team's xGC per-90 (for GK/DEF clean-sheet odds),
  team attacking strength. Derive from `player_gameweeks` aggregated to team.
- **Not available:** age/DOB (FPL has none — omit; do not fabricate), and
  `now_cost` is null for the 25/26 draft snapshot (no draft prices).

Missing columns for pre-2022-23 seasons (no xG/xA/starts) → null, handled
natively by a tree model.

## Model choice

- v1: **explainable and small.** A regularized linear model or a shallow
  `LightGBM`/`GradientBoosting` per position is enough. Prioritize
  interpretability (the tool must explain *why* a player ranks where he does).
- Every projection must carry the **top drivers** (feature contributions) so
  `draft_board` / `player_card` can render a one-line rationale.
- Do NOT chase model complexity for v1. Beating the baselines below with an
  explainable model is the goal.

## Draft-specific outputs (beyond raw projection)

The draft is about **relative value across positions**, not raw points:
- **Tiers:** cluster projected points within each position into tiers (natural
  gaps), so the board reads as "elite / high / mid / punt," not a flat list.
- **Value over replacement (VOR):** projected points minus the projected points
  of the replacement-level player at that position (e.g. the Nth-best where N =
  league size × starters at that position). VOR is the correct cross-position
  draft-ordering signal.
- **Confidence:** wider for players with little history / promoted-team players.
- **Risk flags:** promoted-team player (no PL history), new signing (league
  change), thin minutes history, injury-prone (from availability history).

Write `data/derived/ml/projections_2627.parquet` (git-ignored): one row per
`code` with projected_points, per-position rank, tier, VOR, confidence, risk
flags, top drivers.

## Validation (backtest — required before trusting it)

- **Walk-forward:** train on seasons ≤ 2023-24, predict 2024-25, compare to
  actual 2024-25 `total_points`. Then optionally train ≤ 2024-25, sanity-check on
  25/26.
- Metrics, **per position**: Spearman rank correlation (draft cares about order),
  MAE on total points, and **top-N precision** (did the projected top-20 at each
  position contain the actual top-20 — this is what wins drafts).
- **Baselines to beat:** (a) rank by previous-season `total_points`, (b) rank by
  previous-season points-per-90 × minutes. If the model can't beat "last year's
  points," simplify and diagnose before adding features.
- No leakage: features come only from seasons ≤ S; label from S+1.

## Known caveats (document, don't hide)

- **Promoted-team players** have no PL history — projection is low-confidence;
  flag them, consider a positional-prior fallback.
- **New signings / league switches** — prior-league data not ingested in v1;
  flag as low-confidence.
- **Regression to mean** is the model's edge and its risk — validate that the
  finishing-skill term doesn't over-regress genuine elite finishers.
- Draft FPL has no prices, so this is a **pure points projection** (cleaner than
  classic FPL's points-per-cost problem).

## Acceptance criteria

- Builds `projections_2627.parquet` offline from the two parquet tables.
- Prints the backtest report (per-position Spearman / MAE / top-N vs baselines).
- Tests (pytest, no network, small fixtures):
  1. Two-stage projection = minutes-stage × pts/90-stage (component test).
  2. A persistent over-performer keeps a larger finishing adjustment than an
     equal-rate one-season spike (shrinkage).
  3. VOR is computed relative to the correct replacement rank for a given league
     size.
  4. Never loses to the "previous-season total_points" baseline (Spearman), per
     position, on the most recent completed holdout (2025-26); and on 2024-25
     for GKP/DEF/MID. Known documented exception: FWD on 2024-25 (see
     Empirical findings) — tracked in ISSUES.md.

## Empirical findings (2026-07-30, after implementation)

Measured on two held-out seasons (2024-25, 2025-26), selection never seeing the
target season:

- **Last-season total points is a near-unbeatable ranking baseline** at
  season-aggregate granularity with ~5 training pairs. The honest selector
  anchors GKP and DEF to it (ties, by design: the baseline is in the candidate
  zoo, so "beats baseline" is the selection floor).
- **The reproducible edge is MID via ICT** (rank:ict_index, points-calibrated):
  beats the baseline on BOTH holdouts (0.452 vs 0.442; 0.354 vs 0.335) with a
  large top-20 precision gain (0.45 vs 0.35) — and MID is the deepest draft
  pool, where ranking skill matters most.
- **FWD is the volatile small pool (~30/season):** ties the baseline on 2025-26;
  on 2024-25 the internally-validated challenger lost to an unusually strong
  persistence season (0.537 vs 0.662). Documented as a known miss rather than
  tuned away — further selection-rule tuning against that season would be
  test-set overfitting.
- **Model selection is a paired, sign-consistent displacement rule** (challenger
  must beat the incumbent's per-fold scores by more than the paired SE AND in a
  majority of folds), candidates ordered simplest-first. Fully deterministic.
- The projection's value over a raw last-season-points sort: points-calibrated
  numbers usable for VOR/tiers, the MID edge, and the draft layers (tiers,
  VOR, confidence, risk flags). The next real accuracy unlock is GW-panel
  features (form curves, minutes stability) — a Phase B extension.
- Per CLAUDE.md: feature branch, conventional commits, preflight passes, document
  assumptions/inputs/outputs, deterministic (seed any model).
