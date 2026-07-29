# Known Issues

Tracked defects found during the reshape, not yet fixed. Newest first.

## Pre-existing

- **`render_game_status` crashes on out-of-range dates** — `apps/backend/backend/agent.py:1869`.
  A property test (`test_agent_hypothesis.py::TestRenderGameStatusProperty::test_never_crashes`)
  raises `OverflowError: date value out of range` on an extreme kickoff timestamp
  (e.g. `0001-01-01T00:00:00Z`). Fails identically on `origin/main` — predates the
  reshape. Fix: guard the date parse/format against year-underflow. Low priority
  (only real FPL timestamps occur in practice), but the test is currently
  deselected in CI runs until fixed.

- **Eval predictors limited to season-aggregate columns** — `apps/backend/backend/ml/eval.py`.
  `expected_goal_involvements` lives only in the per-GW panel, not
  `player_seasons.parquet`, so xGI-based predictors can't be scored by the season
  eval yet. Fold GW-panel features into the eval (or the season table) when the
  projection model needs them.
