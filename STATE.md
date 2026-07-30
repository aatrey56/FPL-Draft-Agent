# STATE — checkpoint

Updated: 2026-07-29
Branch: `feat/history-ingestion` (holds the foundation commits until you reorganize on push)

## Done
- Committed in-flight ML ingestion (Phase A) + reshape plan and model specs.
- Removed stale on-disk worktree copies; pruned ephemeral branches (60 -> 5:
  main, dev, stage, feat/history-ingestion, chore/disable-codex-ci).
- Scaffolded the build-loop: `architect` (Fable) + `reviewer` (Opus) agents,
  `/build-loop` command, PLAN.md, STATE.md.

## Done (Phase 1 — 2026-07-29)
- Phase A ingest verified -> player_seasons.parquet (5404 rows, 7 seasons).
- Phase A.2 per-GW panel -> player_gameweeks.parquet (29747 rows, 38 GWs).
- Eval harness (backend.ml.eval): baseline Spearman 0.42-0.66; ICT beats it for
  DEF/MID; per-90 alone loses. 15 ML tests; full suite 259 pass (1 pre-existing
  bug deselected, logged in ISSUES.md).

## Next
- Phase 2: season projection model (PROJECTION_MODEL_SPEC) that beats the eval
  baseline per position -> draft_board / draft_assistant / player_card tools.

## Not done — needs you (outward-facing, not done locally)
- Push branches / open PRs (nothing has been pushed).
- Rotate `OPENAI_API_KEY`; set a real random `FPL_MCP_API_KEY`.
- Uninstall the `chatgpt-codex-connector` GitHub App (GitHub settings).
- Merge/close open PRs (#125, #115 safe; #137, #138 close; #122 request changes).
