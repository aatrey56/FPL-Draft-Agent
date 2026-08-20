# STATE — checkpoint

Updated: 2026-08-20
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

## Done (Phase 2 model + new-league spine — 2026-08-20)
- PR #144 merged: projection model + 26/27 draft board (394 players).
- New 12-manager league verified against the live API (ids live in .env,
  never tracked); league_size=12 in projection config (VOR regenerated).
- Season-nested data layout (data/raw|derived/<season>/); 25/26 stays in the
  legacy flat layout, untouched. First 26/27 fetch complete.
- Fetcher: + league element-status (with timestamped snapshot archive),
  element-summary, entry public/history endpoints.
- Drop-radar (backend.ml.ownership): diffs snapshots -> ownership events;
  ranked free-agent report (departed players filtered). 7 tests.

## Done (waiver_plan v1 — 2026-08-20)
- backend.ml.waiver: roster-aware add/drop CLI. Squad eval (best legal XI by
  next-3 xP), per-GW baseline from the season projection, fixture multiplier
  from 25/26 team strengths (promoted prior), live availability gating
  (next-3 only — ROS survives injuries), and explicit short-vs-long balance:
  every rec shows next3_gain AND season_gain with upgrade/stream/hold labels.
  9 no-network tests; live-run verified against the real league.

## Next
- my_week (start/sit from the same xP core), trade_check, league_pulse;
  match xP model (MATCH_MODEL_SPEC) replaces the per-GW baseline once GWs
  accumulate; MCP tool wrappers for Claude Desktop; schedule fast-mode fetches
  around waiver deadlines.

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
