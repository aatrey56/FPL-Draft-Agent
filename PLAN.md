# PLAN — FPL Draft Co-Pilot build

Living build plan, maintained by the `architect` agent. Strategy lives in
`docs/RESHAPE_PLAN.md`; model contracts in `apps/backend/backend/ml/*_SPEC.md`.
This file is the ordered execution of those, with a testable acceptance criterion
per section. Status: `[ ]` todo, `[~]` in progress, `[x]` done.

## Phase 0 — Foundation & declutter
- [x] Commit in-flight work; remove stale worktrees; prune ephemeral branches (60 -> 5).
- [x] Scaffold build-loop: architect (Fable) + reviewer (Opus) agents, `/build-loop` command, PLAN/STATE.
- [x] Codex workflows removed (deleted with the retired stage branch); GitHub App uninstall still manual.

## Phase 1 — Data foundation  ✅ DONE (2026-07-29)
- [x] Phase A ingest -> `player_seasons.parquet` (HISTORY_INGEST_SPEC). 5404 rows, 7 seasons; 5 tests pass.
- [x] Phase A.2 per-GW panel -> `player_gameweeks.parquet` (GAMEWEEK_INGEST_SPEC). 29747 rows, 38 GWs, 841 players (409 DGW + 409 blank rows correctly flagged); 6 tests pass.
- [x] Eval harness + naive baseline (`backend.ml.eval`). One command backtests any signal vs the last-season-points baseline, per position (Spearman + top-N). Result: baseline Spearman 0.42-0.66; ICT beats it for DEF/MID; per-90 alone loses (minutes matter). Confirms the "define accurate = beat baseline at ranking" framing.

## Phase 2 — Draft board (hero, target ~mid Aug)
- [ ] Season projection model (PROJECTION_MODEL_SPEC), validated vs 25/26. Acceptance: beats the last-season-points baseline (Spearman) per position on the 2024-25 backtest.
- [ ] `draft_board` + `draft_assistant` + `player_card` tools, wired to Claude Desktop. Acceptance: returns a tiered, roster-aware board for the configured league.

## Phase 3 — Weekly co-pilot (season start onward)
- [ ] Match xP model (MATCH_MODEL_SPEC). Acceptance: beats FPL `ep_next` (Spearman) on a 25/26 walk-forward, per position.
- [ ] `my_week`, `waiver_plan`, `trade_check`, `league_pulse` tools.
