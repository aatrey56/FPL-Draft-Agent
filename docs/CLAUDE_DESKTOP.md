# Connecting Claude Desktop (or Claude Code) to the FPL co-pilot

The Go MCP server serves 30 tools over Streamable HTTP at `/mcp`, including the
decision layer: `draft_board`, `player_card`, `waiver_plan`, `drop_radar`.
Claude (on a Max plan) is the client — there is no in-app LLM.

## 1. Start the server

```bash
cd apps/mcp-server
FPL_MCP_API_KEY=<your-random-secret> go run ./fpl-server \
  --raw-root ../../data/raw --derived-root ../../data/derived \
  --default-season 2026-27
```

Path convention: the flat roots are the 2025-26 archive; the decision tools
read season-nested paths (`data/raw/<season>/`, `data/derived/<season>/`) using
`--default-season` unless a call passes `season` explicitly.

## 2. Connect a client

**Claude Code (simplest):**

```bash
claude mcp add fpl --transport http http://localhost:8080/mcp \
  --header "X-API-Key: <your-random-secret>"
```

**Claude Desktop** (custom connector → local servers need a stdio bridge):

```json
{
  "mcpServers": {
    "fpl": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "http://localhost:8080/mcp",
               "--header", "X-API-Key:<your-random-secret>"]
    }
  }
}
```

## 3. Keep the artifacts fresh (the weekly loop)

The decision tools serve files the pipeline writes. Before waivers each week:

```bash
# fetch: game state, league, transactions + element-status snapshot
cd apps/mcp-server && go run ./cmd/dev --league <LEAGUE_ID> --season 2026-27 \
  --raw-root ../../data/raw --derived-root ../../data/derived --fast --refresh-now

# derive: ownership events + waiver plan (+ projections/history after ingests)
cd ../backend
python -m backend.ml.ownership --league <LEAGUE_ID>
python -m backend.ml.waiver    --league <LEAGUE_ID> --entry <ENTRY_ID>
```

Then ask Claude things like *"run my waiver plan — who do I add and drop, and
which of those are streams vs season upgrades?"* or *"player card for
Maddison — should I worry?"*.
