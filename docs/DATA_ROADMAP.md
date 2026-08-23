# Data roadmap — from co-pilot to prediction engine

## Where this is (2026-08-23)

**Serving layer: done.** 14-tool MCP server, two-layout matchday TUI, official
team sheets + match events (pulse), projected auto-subs, live PL table,
predictions log + keeper cron. The *plumbing* is finished — data flows
fetch → derive → serve → Claude/TUI, near-live.

**Modeling layer: barely started.** One season-projection heuristic + a
per-GW `ep_next`-style placeholder. The correlation study
(`data/derived/reports/stat_correlations_2526.md`) is the only real analysis.

## The goldmine (what the data actually is)

- ~29.7k player-gameweek rows, 2025-26, 35 columns **with xG/xA/xGC, BPS,
  defensive contributions, ICT, venue, opponent** — the underlying signals,
  not just outcomes.
- 6 prior seasons (2019-20 → 2024-25) at season aggregate, joinable on the
  permanent `code`.
- This is enough to build genuinely predictive models, not heuristics.

## Aspiration (set high)

A **research-grade, backtested FPL prediction engine** that:
1. predicts per-GW points as a **distribution** (floor / median / ceiling,
   P(haul)), not a point estimate;
2. **beats the official `ep_next` baseline** on walk-forward evaluation, per
   position, with the improvement measured and logged;
3. carries a **live, calibrated track record** (the predictions log) proving
   the edge week over week;
4. is a **portfolio centerpiece** demonstrating end-to-end ML engineering:
   ingestion → features → multiple models → rigorous evaluation → serving
   (already built) → monitoring. Writeup + calibration plots = hireable proof.

Stretch: generalize the methodology into a reusable sports-prediction
framework and publish the results-vs-market writeup.

## Phased plan

### Phase A — Match xP model (STARTS Mon 2026-08-24 5pm EST, cron e59d8b75)
The foundation. Per `MATCH_MODEL_SPEC.md`:
- **Minutes / start-probability model first** (correlation study: minutes is
  the #1 predictor for every position). Availability-gated.
- **Component xP**: appearance, goals, assists, CS, GC, saves, DC, bonus —
  each modeled on its real driver, summed. Fixture-aware (venue-split team
  env, position × opponent matrix — the `fixture_targets` contract).
- **Backtest**: walk-forward over 25/26, per-position Spearman + MAE +
  top-N precision + P(start) calibration. **Assert it beats `ep_next`.**
- Club-move flag + expected_minutes surfaced (fixes the Pope/Isak blindness).

### Phase B — Underlying-metrics & breakout engine (the buy-low edge)
- Trend detection on xG/xA/xGI/minutes: flag players whose **underlying is
  rising before points follow** — the market's blind spot.
- **Luck adjustment**: separate skill from variance (finishing G−xG, BPS
  variance). The correlation study already showed finishing luck doesn't
  repeat — operationalize it into "regression-to-mean" buy/sell signals.
- Feeds `fixture_targets` and `waiver_plan` with *why-now* reasons.

### Phase C — Probabilistic + portfolio optimization
- Turn point xP into **distributions** (quantile or simulation): floor,
  ceiling, P(haul ≥ 10). Start/sit and captaincy care about ceilings.
- **Lineup + draft optimization** under formation/roster constraints with a
  risk knob (chase ceiling vs protect floor). 12-team scarcity aware.

### Phase D — Track record & writeup (the portfolio payoff)
- Accumulate backtested + live predictions (predictions log already running).
- Calibration plots, edge-vs-baseline charts, a clean README/blog narrative.
- This is what turns the repo from "a project" into "proof I can ship ML."

## Working notes

- Do the heavy analysis in a **fresh Claude session** — memory carries the
  context (MEMORY.md + project_fpl26.md + this file), and a fresh session
  avoids paying for a long history on every turn.
- Keep models as plain Python → parquet first (per CLAUDE.md); no warehouse
  until a task demands it.
- Every model change gets a walk-forward number vs `ep_next`. No vibes.
