# Connecting Claude Desktop (or Claude Code) to the FPL co-pilot

The Go MCP server serves 34 tools over Streamable HTTP at `/mcp`, including the
decision layer: `draft_board`, `player_card`, `waiver_plan`, `my_week`,
`trade_check`, `league_pulse`, `drop_radar`, `team_env`. Claude (on a Max
plan) is the client — there is no in-app LLM.

## 0. One-time setup: `.env` at the repo root

```bash
# from the repo root — generates a random API key and stores your ids
printf 'LEAGUE_ID=%s\nENTRY_ID=%s\nFPL_MCP_API_KEY=%s\n' \
  <your-league-id> <your-entry-id> "$(openssl rand -hex 16)" >> .env
```

`FPL_MCP_API_KEY` is not issued by anyone — it is a password you invent so
only clients that know it can call your server. Both Go binaries and the
Python CLIs read `.env` automatically (a real exported env var always wins).
`.env` is gitignored; ids never go in tracked files.

## 1. Start the server

```bash
cd apps/mcp-server
go run ./fpl-server --raw-root ../../data/raw --derived-root ../../data/derived \
  --default-season 2026-27
```

Path convention: flat roots are the 2025-26 archive; every tool resolves
season-nested paths (`data/raw/<season>/`, …) from `--default-season` unless a
call passes `season` explicitly. To browse last season, run a second instance
with `--default-season 2025-26`.

## 2. Connect a client

**Claude Code (simplest):**

```bash
claude mcp add fpl --transport http http://localhost:8080/mcp \
  --header "X-API-Key: $(grep '^FPL_MCP_API_KEY=' .env | cut -d= -f2)"
```

**Claude Desktop** (custom connector → local servers need a stdio bridge):

```json
{
  "mcpServers": {
    "fpl": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8080/mcp",
               "--header", "X-API-Key:<value from .env>"]
    }
  }
}
```

## 3. Keep the artifacts fresh (the weekly loop)

The decision tools serve files the pipeline writes. Before waivers each week
(ids come from `.env` — nothing to type):

```bash
# fetch: game state, league, transactions + element-status snapshot
cd apps/mcp-server && go run ./cmd/dev --season 2026-27 \
  --raw-root ../../data/raw --derived-root ../../data/derived --refresh-now

# derive: ownership events, waiver plan, start/sit
cd ../backend
uv run python -m backend.ml.ownership
uv run python -m backend.ml.waiver
uv run python -m backend.ml.myweek
```

After any ingest refresh also run `uv run python -m backend.ml.serve_export`
(player history for player_card).

## 3b. Fresh machine? Bootstrap the ML artifacts once

The derived artifacts (projections, player history) are gitignored. On a new
clone, after `uv sync` and one fetch (step 3 above), rebuild them — the
explicit season list pulls 2025-26 from the public vaastav mirror, needed on
machines without the local 25/26 archive:

```bash
cd apps/backend
uv run python -m backend.ml.history --seasons 2019-20 2020-21 2021-22 2022-23 2023-24 2024-25 2025-26
uv run python -m backend.ml.projection --project
uv run python -m backend.ml.serve_export
```

(`team_env` additionally needs the 25/26 per-GW archive and won't regenerate
on a fresh machine yet — its tool errors cleanly until then.)

## 4. Ask real questions

- *"Run my waiver plan — which adds are streams vs season upgrades? Anything
  in unprojected_squad I should judge myself?"*
- *"my_week — who starts, and what needs my attention before the deadline?"*
- *"Player card for Maddison — no projection? Show me his history and search
  the web for his current form before I decide."*
- *"trade_check: I give X and Y, I get Z — worth it?"*
- *"league_pulse for my league — who's hot on the wire?"* (pair with
  `drop_radar`)

The quantitative floor comes from the tools; have Claude layer live news,
lineups, and manager context on top with web search at decision time.
