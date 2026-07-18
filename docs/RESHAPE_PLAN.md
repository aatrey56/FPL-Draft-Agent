# FPL Draft Co-Pilot — Reshape Plan

Status: **active reshape, started 2026-07-18.** This is the master plan for
refactoring this repo from a cluttered 26-tool in-season waiver tool into a
focused **draft + weekly co-pilot** powered by two ML models. It supersedes the
scattered direction notes; the model contracts live in
`apps/backend/backend/ml/*_SPEC.md`.

Near-term deadline: **Premier League 2026-27 starts mid-to-late Aug 2026; the
league draft is ~mid Aug.** The draft is the hero milestone.

---

## 1. What we're making

A personal **FPL Draft co-pilot** the user drives through **Claude Desktop on a
Max plan** (Claude Desktop is the MCP client — this replaces the in-app OpenAI
chatbot; the LLM *is* the client, so there is no provider to pay per-token and no
OpenAI dependency). It does two jobs across the season:

- **Draft (August):** project every player's 26-27 output → a **tiered,
  roster-aware draft board** + live "who do I take next" pick suggestions.
- **Manage (season):** each gameweek → **who to start/sit, who to add/drop,
  whether a trade is worth it** — grounded in form, fixtures, availability.

Under the hood it is a data-engineering + ML pipeline with an agentic serving
layer: multi-season ingest → features → two models → served as MCP tools →
natural-language client.

## 2. Design principles

1. **Tools are shaped like decisions, not data.** The failure mode of the old
   surface was 26 endpoint-wrappers that never answered a question a manager
   asks. Every public tool answers one sentence a human would say.
2. **Minutes is king.** Availability is the single biggest FPL points driver in
   every position (verified — see §6). Models predict availability first, then
   per-game quality.
3. **xG is a stabilizer, not the model.** xG/xA earn a seat because they *repeat*
   year-over-year better than actual goals; they do not run the model. Actual
   output, minutes, and (for the season model) multi-year finishing skill matter
   more. See the model specs.
4. **Refactor in place.** Keep the 26 working Go tools as the internal data
   layer; expose ~7 decision tools on top. Do not rewrite the repo.
5. **Data correctness > performance** (unchanged from CLAUDE.md).
6. **Compute-now before external.** Ship everything derivable from data already
   on disk before integrating external feeds (predicted lineups, manager data).

## 3. Architecture spine

```
data/raw   ← 25/26 on disk (38 GWs granular + season aggregate) + vaastav prior seasons
   │
   ├─ Phase A ingest (HISTORY_INGEST_SPEC)   → player_seasons.parquet     [season aggregates, multi-season]
   ├─ Phase A.2 ingest (GAMEWEEK_INGEST_SPEC)→ player_gameweeks.parquet   [match-level panel]
   │
   ▼ features (per-90, form, opponent strength, minutes reliability, finishing skill)
   │
   ├─ Season projection model (PROJECTION_MODEL_SPEC) ─┐
   └─ Match expected-points model (MATCH_MODEL_SPEC) ──┤
   │                                                   ▼
   ▼  Decision tools (Go MCP server + Python):
      draft_board · draft_assistant · player_card · my_week · waiver_plan · trade_check · league_pulse
      (the 26 existing tools become the internal data layer behind these)
   │
   ▼
Claude Desktop (Max plan) as the MCP client     ← no OpenAI
```

Component boundaries (Go = stateless file-reading tools; Python = orchestration /
ML) are unchanged from CLAUDE.md §13. The models run in Python and write parquet
/ JSON that Go tools serve, keeping Go network-free and stateless.

## 4. Target tool surface (7 public tools)

| Tool | Job it answers | Status |
|---|---|---|
| `draft_board` | "Rank everyone for my draft, in tiers, by projected 26-27 points." | NEW (season model) |
| `draft_assistant` | "Given who's gone and my picks so far, who do I take next?" | NEW (roster-aware) |
| `player_card` | "Tell me everything about player X (form, xG, fixtures, availability, set-pieces)." | merge |
| `my_week` | "What do I do this gameweek — matchup, lineup, start/sit flags?" | merge |
| `waiver_plan` | "Who should I add, and who do I drop, and why?" | rework |
| `trade_check` | "Is this proposed trade worth it?" | NEW (issue #112) |
| `league_pulse` | "How's my league — standings, rivals, notable moves, key matchups?" | merge |

## 5. Tool audit — the 26 → the surface

The 26 existing tools are **kept as the internal data layer**; none are deleted
(refactor in place). They stop being top-level and become libraries behind the 7.

| Existing tool | Disposition |
|---|---|
| `waiver_recommendations` | **rework** → core of `waiver_plan` (roster-aware; add+drop) |
| `waiver_targets` | internal → feeds `waiver_plan` |
| `player_form`, `player_gw_stats`, `player_lookup` | internal → feed `player_card` |
| `current_roster`, `lineup_efficiency`, `game_status` | internal → feed `my_week` |
| `standings`, `league_summary`, `matchup_breakdown`, `transactions`, `transaction_analysis`, `strength_of_schedule` | internal → feed `league_pulse` |
| `fixtures`, `fixture_difficulty` | internal → feed opponent-strength in `draft_board`/`my_week`/`waiver_plan` |
| `manager_lookup`, `manager_schedule`, `manager_streak`, `manager_season`, `head_to_head`, `draft_picks`, `league_entries` | internal helpers → `league_pulse` / `my_week` / `draft_assistant` |
| `ownership_scarcity` | internal → draft/waiver scarcity context |
| `epl_fixtures`, `epl_standings` | internal → team/opponent context |

### The #99–#113 issues are signals, not tools

The 15 "new tool" issues do **not** become 15 tools. They are **features/columns**
that feed the 7 decision tools:

| Issue(s) | Folds into |
|---|---|
| #99 injury_report, #102 suspension_risk | availability gate in `my_week`, `waiver_plan`, `player_card` |
| #101 overperformance, #106 points_breakdown | projection model + `player_card` |
| #100 set_piece_takers, #103 bonus_magnets, #104 ict_profile, #105 defensive_profile | `player_card` + model features |
| #110 live_gw_tracker | in-play mode of `my_week` |
| #111 player_compare | compare mode of `player_card` |
| #112 trade_evaluator | `trade_check` |
| #107 waiver_priority_tracker, #108 draft_value_analysis, #109 lineup_management | features of `league_pulse` / `draft_assistant` |
| #113 report_retrieval | drop — Claude Desktop reads report files directly |

## 6. The two models

Two models = the two products' brains. Full contracts in the ML dir:

- **Season projection model** → `draft_board`. Predicts next-season total points.
  Spec: `apps/backend/backend/ml/PROJECTION_MODEL_SPEC.md`.
- **Match expected-points (xP) model** → weekly tools. Predicts a player's points
  for the next fixture. Spec: `apps/backend/backend/ml/MATCH_MODEL_SPEC.md`.

Both consume the two ingested tables (`player_seasons.parquet`,
`player_gameweeks.parquet`).

**Verified 25/26 correlation findings (drivers of season points, per position)** —
these justify the position-specific weighting in both models. After discarding
mechanical point-components (bps/bonus/goals/assists/clean_sheets, which *are*
points), the underlying drivers are:

- **GKP:** minutes/starts, saves, team defensive strength (xGC). No attacking.
- **DEF:** minutes + clean-sheet/defensive signals **and** attacking involvement
  (xGI, threat, xG); `defensive_contribution` (new CBIT points) is real. Hybrid.
- **MID:** attacking output (xGI, creativity, xA, threat, xG) + minutes; clean
  sheets still count.
- **FWD:** almost purely attacking (xGI, threat, xG, goals) + minutes.
- **Universal:** minutes/starts is a top-3 correlate in every position.

## 7. Phased plan

| Phase | When | Deliverable | Done = |
|---|---|---|---|
| **0 — Foundation & declutter** | Jul 18–24 | prune stale branches + `.claude/worktrees/*` copies; fix docs drift (22→26 tools); disable Codex + rotate keys + uninstall Codex bot; drop OpenAI web-chat path (client = Claude Desktop); gitignore `reports/`; land Phase A ingest PR; add `verify_data.py` | repo clean; `player_seasons.parquet` builds; data-coverage check green |
| **1 — Data foundation** | Jul 21–27 | add per-GW panel `player_gameweeks.parquet` (GAMEWEEK_INGEST_SPEC) from the 38 `live.json` | both parquet tables build offline + tested |
| **2 — Draft board (hero)** | Jul 28 – Aug 14 | season features → explainable projection model validated vs 25/26 → `draft_board` + `draft_assistant` + `player_card` wired into Claude Desktop → mock-draft dry run | trustworthy "who do I draft next" on draft night |
| **— DRAFT NIGHT —** | ~mid Aug | use it live | |
| **3 — Weekly co-pilot** | season start → ongoing | match xP model (Tier-1 features) → `my_week`, `waiver_plan`, `trade_check`, `league_pulse`; then external lineups/injuries (Tier-2), manager context (Tier-3) | each GW: start/sit + add/drop + trade calls |

## 8. Week-0 cleanup checklist

- [ ] Prune merged-squash-residue branches and all `worktree-agent-*` branches.
- [ ] Delete stale `.claude/worktrees/agent-*/` full-repo copies.
- [ ] Fix README tool count (22 → 26) and remove dual-identity drift in docs.
- [ ] Disable Codex workflows on `stage` (branch `chore/disable-codex-ci` — done, awaiting push); uninstall the `chatgpt-codex-connector` GitHub App (manual, repo settings).
- [ ] Rotate the local `OPENAI_API_KEY`; set a real random `FPL_MCP_API_KEY`.
- [ ] Confirm `reports/` and `data/` are gitignored; stop committing generated GW reports.
- [ ] Land the `ml/` Phase A work as a clean PR rebased on `origin/main`, excluding `__pycache__`.
- [ ] Lock down FastAPI (CORS + auth) *if* the web backend is kept; otherwise mark it optional.

## 9. Non-goals / deferred

- **dbt / Airflow / warehouse (DuckDB→Snowflake):** intended end-state, layered
  later. Build plain Python → parquet first. Do not introduce unless a task asks.
- **In-app OpenAI web chatbot:** dropped; Claude Desktop is the client. The
  FastAPI/web UI is optional and no longer the primary surface.
- **Predicted lineups, live injuries:** Tier-2 external integration (Phase 3).
- **Manager / sacking / tactical context:** Tier-3, low ROI, external. Parked.
- **Shot-level xG / PSxG / defender tracking:** not in FPL data; Understat
  integration is a later phase, not needed for v1.

## 10. Open decisions

- Season model: pure season-aggregate (simpler) vs pulling per-GW signal
  (stronger). Recommendation: use the per-GW panel — it is cheap once ingested.
- Weekly model: how much recent form should outweigh season-long baseline
  (the form/quality blend weight). To be tuned via backtest, not guessed.
- Start-probability source for v1: heuristic from recent starts + FPL
  availability flags, before any external lineup feed.
