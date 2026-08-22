# Connecting Claude Desktop (or Claude Code) to the FPL co-pilot

The Go MCP server serves 14 tools over Streamable HTTP at `/mcp`, including the
decision layer: `draft_board`, `player_card`, `waiver_plan`, `my_week`,
`trade_check`, `league_pulse`, `drop_radar`, `team_env`, `gw_live`. Claude
(on a Max plan) is the client — there is no in-app LLM.

During matches: run a full (non `--fast`) fetch refresh, then ask
*"gw_live — how's my matchup going?"* for both XIs with in-play points.

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

Recommended (macOS): install the autopilot once — always-on server plus a
fetch+derive refresh every 15 minutes and deadline notifications:

```bash
make autopilot        # undo: make autopilot-off · pause/resume: make stop / make start
make update           # after every merged PR: pull + restart the server
```

Manual alternative: `make serve` in a terminal (Ctrl-C to stop; restart after
every `git pull` — `go run` compiles at launch, so a running server never
picks up new code).

Path convention: flat roots are the 2025-26 archive; every tool resolves
season-nested paths from `--default-season` unless a call passes `season`.
To browse last season, run a second instance with `--default-season 2025-26`.

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
make weekly     # fetch + ownership + waiver + my_week
```

During matches, keep `gw_live` fresh in a second terminal:

```bash
make matchday   # near-live: live points every 60s + full refresh every 10 min; Ctrl-C after the games
```

Automate weekly with cron: see the crontab example in scripts/weekly.sh.

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

## 5. The FPL week, on autopilot

Every gameweek's clock derives from ITS OWN first kickoff (lock = first
kickoff − 90 min; waivers = lock − 24h; trades = lock − 48h) — so the times
below are GW1's shape, not a fixed schedule. Midweek and festive gameweeks
shift everything, and the system follows automatically: league_pulse,
the TUI countdown, and the notifications all read the per-event timestamps
the API publishes, never an assumed weekday/hour.

| When (GW1 example)  | What                                   | Automated?                          |
|---------------------|----------------------------------------|-------------------------------------|
| Wed ~1:30 PM EST    | trades due                             | reminder notification (24h)         |
| Thu ~1:30 PM EST    | waivers due -> free agency opens       | notifications (24h + 3h); plan kept fresh |
| Fri ~1:30 PM EST    | lineup lock (90 min before kickoff)    | notifications (24h + 2h)            |
| Fri-Mon             | matches                                | autopilot refresh; gw_live + TUI    |
| Morning after last match ~4:00 AM EST | scores final ("lockdown") | next tick fetches finals + notifies "GW final" |

The only human steps left: approve waiver claims and set the lineup on
draft.premierleague.com, and ask Claude what to do about either.
