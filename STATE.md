# STATE — checkpoint

Updated: 2026-08-21 (GW1 kickoff day)
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

## Done (serving layer — 2026-08-20)
- Go decision tools live on the MCP server (30 total): draft_board,
  player_card (projection + multi-season history + live news — the reversion
  safeguard), waiver_plan, drop_radar. --default-season flag; 5 Go tests.
- serve_export writes player_history.json (2108 players); jsonutil.dumps_strict
  fixes NaN-poisoned artifacts (Go strict JSON) — both writers routed through it.
- Verified end-to-end over real MCP (initialize -> tools/call).
- docs/CLAUDE_DESKTOP.md: connector setup + the weekly artifact-refresh loop.

## Done (week-1 co-pilot — 2026-08-21)
- Season-aware legacy tools: all 26 data-layer tools resolve
  `data/{raw,derived}/<season>/` via `--default-season` (flat roots = 25/26
  archive, `ArchiveSeason` const). 2 new Go tests.
- Maddison safety: waiver_plan never auto-drops unprojected squad players
  (surfaced in `unprojected_squad` instead); player_card falls back to
  history + live news; trade_check warns. Root cause + open projection-side
  fix tracked in ISSUES.md.
- my_week v1 (backend.ml.myweek + MCP tool): next-GW best XI, bench,
  attention flags (injury/blank/unprojected). 4 + 1 tests.
- trade_check + league_pulse MCP tools (33 total). 2 test funcs.
- Config via repo .env: LEAGUE_ID/ENTRY_ID/FPL_MCP_API_KEY picked up by both
  Go binaries (internal/config, no new dep) and the ML CLIs (python-dotenv).
- Dependabot cleared (4 PRs merged post-verification: go-sdk 1.6.1,
  fastapi 0.141.1, uvicorn 0.42, python-dotenv 1.2.2). Only PR #122 open.

## Next
- Match xP model (MATCH_MODEL_SPEC) — **hold until Mon 2026-08-24 ~17:00 EST**
  (GW1 finished), then train on the 25/26 GW panel + early 26/27 data; must
  beat FPL's ep_next; replaces the per-GW heuristic in waiver_plan/my_week.
- Team attack/defense strength ratings from the GW panel (match context:
  expected game environment) as match-model features; odds-based calibration
  optional later.
- Breakout radar (the Bruno case): rolling underlying-rate spikes vs baseline
  + context flags (manager change, minutes jump, cup-free schedule).
- Schedule fast-mode fetches around waiver deadlines.

## Done (CI/CD + branch model — 2026-07-30)
- Fixed the 7-week CI failure (OverflowError dates); CI installs ML deps; re-enabled
  the auto-disabled CI workflow; required status checks now gate `main` (strict).
- PR #142 merged: full reshape foundation on `main`; main CI GREEN.
- Branch cleanup: 44 remote branches -> main + dependabot. dev/stage retired;
  Codex workflows deleted with stage. claude-review now gates every PR into main.

## Not done — needs you (outward-facing)
- Fill `.env`: LEAGUE_ID, ENTRY_ID, and a random `FPL_MCP_API_KEY`.
- Uninstall the `chatgpt-codex-connector` GitHub App (GitHub settings).
- Decide PR #122 (env-drift CI guardrail): request SHA-pinning changes or close.
- Connect Claude Desktop/Code per docs/CLAUDE_DESKTOP.md.
