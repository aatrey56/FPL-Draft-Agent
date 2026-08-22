# Changelog

## Unreleased (2026-27 season reshape)
- Reshaped into a draft + weekly-manager co-pilot: Claude (Desktop/Code) as the
  client over a 14-tool MCP surface (decision layer: draft_board, player_card,
  waiver_plan, my_week, trade_check, league_pulse, drop_radar, team_env, gw_live).
- Season projection model (honest walk-forward selection, baseline-in-the-zoo)
  + 2026-27 draft board; unprojected players surfaced for human judgment,
  never valued as zero.
- Season-aware data layout (flat roots = 2025-26 archive; `<root>/<season>/`).
- Live matchday: gw_live tool + terminal TUI (bubbletea) with deadline countdown.
- Deadline intelligence: per-GW trades/waivers/lineup calendar (kickoff-anchored,
  tz-correct) + macOS notifications.
- Ops: root Makefile, macOS autopilot (always-on server + 15-min refresh),
  Python packaging moved to uv (pyproject + lockfile).
- Tool surface consolidated 34 → 14; legacy OpenAI/FastAPI chat stack deprecated.

## 0.2.0
- Initial public release.
