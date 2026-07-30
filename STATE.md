# STATE — checkpoint

Updated: 2026-07-30
Branch model: **trunk-based** — feature branch -> PR -> `main` (dev/stage retired 2026-07-30; Docling-style).

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

## Done (CI/CD + branch model — 2026-07-30)
- Fixed the 7-week CI failure (OverflowError dates); CI installs ML deps; re-enabled
  the auto-disabled CI workflow; required status checks now gate `main` (strict).
- PR #142 merged: full reshape foundation on `main`; main CI GREEN.
- Branch cleanup: 44 remote branches -> main + dependabot. dev/stage retired;
  Codex workflows deleted with stage. claude-review now gates every PR into main.

## Not done — needs you (outward-facing)
- Set a real random `FPL_MCP_API_KEY` (local .env still has the placeholder).
- Uninstall the `chatgpt-codex-connector` GitHub App (GitHub settings).
- Dependabot PR triage (#125, #115 merge; #137, #138 close; #141, #135 judge; #122 request changes).
