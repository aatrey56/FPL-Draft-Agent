# Known Issues

Tracked defects found during the reshape, not yet fixed. Newest first.

## Pre-existing

- **Eval predictors limited to season-aggregate columns** — `apps/backend/backend/ml/eval.py`.
  `expected_goal_involvements` lives only in the per-GW panel, not
  `player_seasons.parquet`, so xGI-based predictors can't be scored by the season
  eval yet. Fold GW-panel features into the eval (or the season table) when the
  projection model needs them.

## Model limitations (tracked)

- **FWD projection loses to the naive baseline on the 2024-25 holdout**
  (Spearman 0.537 vs 0.662; ties baseline on 2025-26). Small pool (~30-35
  forwards/season) makes internal validation noisy. Do not tune the selection
  rule against 2024-25 further (test-set overfitting); the expected fix is
  GW-panel features (form curves, minutes stability) in a Phase B extension
  of `backend/ml/projection.py`.

- **Single-season persistence has no multi-year reversion** — e.g. M.Salah:
  344 pts in 2024-25, injury-hit 123 in 2025-26, so the board ranks him MID
  #16 for 2026-27. Multi-year weighted history (or GW-panel form context)
  would moderate this; meanwhile `player_card` should always show multi-season
  history next to the projection so a human can catch buy-low cases.

- **Players under `PRIOR_MINUTES_FLOOR` (500 min) in 2025-26 have no
  projection at all** — the Maddison case: 34 injury minutes in 25/26, six
  strong prior seasons, absent from the board. Serving-side safety shipped
  2026-08-21 (waiver_plan never auto-drops unprojected squad players and lists
  them in `unprojected_squad`; player_card falls back to history + live news;
  trade_check warns instead of valuing them at zero). The projection-side fix —
  project from the last healthy season with a decay + `injury_return` risk
  flag — is open; in practice the match model's early-minutes signal will
  resolve these players within a few GWs.
