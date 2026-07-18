# PLAN — FPL Draft Co-Pilot build

Living build plan, maintained by the `architect` agent. Strategy lives in
`docs/RESHAPE_PLAN.md`; model contracts in `apps/backend/backend/ml/*_SPEC.md`.
This file is the ordered execution of those, with a testable acceptance criterion
per section. Status: `[ ]` todo, `[~]` in progress, `[x]` done.

## Phase 0 — Foundation & declutter
- [x] Commit in-flight work; remove stale worktrees; prune ephemeral branches (60 -> 5).
- [x] Scaffold build-loop: architect (Fable) + reviewer (Opus) agents, `/build-loop` command, PLAN/STATE.
- [ ] Disable Codex workflows on `stage` (branch `chore/disable-codex-ci` ready) and uninstall the Codex GitHub App; rotate keys. Acceptance: no Codex checks/comments on new PRs.

## Phase 1 — Data foundation
- [ ] Phase A ingest -> `player_seasons.parquet` (HISTORY_INGEST_SPEC). Acceptance: builds offline; the 4 spec tests pass.
- [ ] Phase A.2 per-GW panel -> `player_gameweeks.parquet` (GAMEWEEK_INGEST_SPEC). Acceptance: builds offline; the 5 spec tests pass.
- [ ] Eval harness + naive baseline over 25/26. Acceptance: one command produces a report scoring recommendation ranking vs a last-season-points baseline, per position.

## Phase 2 — Draft board (hero, target ~mid Aug)
- [ ] Season projection model (PROJECTION_MODEL_SPEC), validated vs 25/26. Acceptance: beats the last-season-points baseline (Spearman) per position on the 2024-25 backtest.
- [ ] `draft_board` + `draft_assistant` + `player_card` tools, wired to Claude Desktop. Acceptance: returns a tiered, roster-aware board for league 14204.

## Phase 3 — Weekly co-pilot (season start onward)
- [ ] Match xP model (MATCH_MODEL_SPEC). Acceptance: beats FPL `ep_next` (Spearman) on a 25/26 walk-forward, per position.
- [ ] `my_week`, `waiver_plan`, `trade_check`, `league_pulse` tools.
