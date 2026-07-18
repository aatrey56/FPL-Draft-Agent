# STATE — checkpoint

Updated: 2026-07-18
Branch: `feat/history-ingestion` (holds the foundation commits until you reorganize on push)

## Done
- Committed in-flight ML ingestion (Phase A) + reshape plan and model specs.
- Removed stale on-disk worktree copies; pruned ephemeral branches (60 -> 5:
  main, dev, stage, feat/history-ingestion, chore/disable-codex-ci).
- Scaffolded the build-loop: `architect` (Fable) + `reviewer` (Opus) agents,
  `/build-loop` command, PLAN.md, STATE.md.

## Next
- Phase 1: finish Phase A ingest to parquet + tests; build the per-GW panel;
  stand up the eval harness + naive baseline against the completed 25/26 season
  (define "accurate" as beating that baseline at ranking).

## Not done — needs you (outward-facing, not done locally)
- Push branches / open PRs (nothing has been pushed).
- Rotate `OPENAI_API_KEY`; set a real random `FPL_MCP_API_KEY`.
- Uninstall the `chatgpt-codex-connector` GitHub App (GitHub settings).
- Merge/close open PRs (#125, #115 safe; #137, #138 close; #122 request changes).
