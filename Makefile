SEASON ?= 2026-27
SCORER ?= model
HORIZON ?= 3
RAW_SEASON := ../../data/raw/$(SEASON)
DERIVED_SEASON := ../../data/derived/$(SEASON)
GO_ROOTS := --raw-root ../../data/raw --derived-root ../../data/derived

.PHONY: serve fetch derive weekly matchday preflight backtest xp

## serve: run the MCP server (Ctrl-C to stop; restart after every git pull)
serve:
	cd apps/mcp-server && go run ./fpl-server $(GO_ROOTS) --default-season $(SEASON)

## fetch: refresh all raw data for the season (game, league, live GW, picks, element-status)
fetch:
	cd apps/mcp-server && go run ./cmd/dev --season $(SEASON) $(GO_ROOTS) --refresh-now

## derive: rebuild the weekly artifacts for SEASON (ownership -> panel && next-GW xP && 3-GW horizon -> team_env -> waiver -> my_week -> player_values -> track record)
## player_values.json (every player's xp_next / xp_h3 / ros_adj) feeds trade_check; it always asks for the model and falls back like waiver.
## waiver/my_week consume xp_gw<N>.parquet (SCORER=model) and fall back to the heuristic with a
## WARNING when it is missing or stale, so the panel && xP step is `-`-prefixed: a failure there
## (e.g. a missing archive panel, or no next GW after GW38) warns, and the decision artifacts and
## the track record still build (GW38 must still be scored).
## Panel and xP are chained with && on one recipe line, so xP never trains on an unrefreshed
## panel; the xP file records the panel's last finished GW (panel_max_gw) and waiver/my_week
## reject it unless that equals the bootstrap's last finished GW (N-1 between gameweeks,
## N-2 mid-gameweek while N-1 is still in play).
## The 3-GW horizon (xp_horizon_gw<N>.parquet, GW N..N+2 with features frozen at N) is built
## right after xp_gw<N> on the same line, so it fails together with it and never blocks
## waiver/my_week: without a fresh horizon file waiver ranks on the next GW and says so
## (horizon_fallback), without xp_gw<N> it falls back to the heuristic as above.
## team_env.json (per-team match environment, served by the team_env tool) is rebuilt from the
## season panel; it is `-`-prefixed so a missing panel warns instead of blocking the decision artifacts
## (the tool then errors for SEASON rather than serving the 2025-26 archive).
## SCORER={heuristic,model} picks the next-GW xP source for waiver/my_week.
## HORIZON={1,3,ros} picks the gain waiver_plan ranks on under SCORER=model (default 3).
## Reads/writes season-nested paths only; the flat data/ layout is the 2025-26 archive.
derive:
	cd apps/backend && uv run python -m backend.ml.ownership --season $(SEASON)
	-cd apps/backend && uv run python -m backend.ml.gameweeks --season $(SEASON) \
		--out $(DERIVED_SEASON)/ml/player_gameweeks.parquet \
		--gw-root $(RAW_SEASON)/gw --bootstrap $(RAW_SEASON)/bootstrap/bootstrap-static.json \
		&& uv run python -m backend.ml.matchmodel --gw next --season $(SEASON) \
		--panel ../../data/derived/ml/player_gameweeks.parquet $(DERIVED_SEASON)/ml/player_gameweeks.parquet \
		--bootstrap $(RAW_SEASON)/bootstrap/bootstrap-static.json \
		&& uv run python -m backend.ml.matchmodel --gw next --horizon --season $(SEASON) \
		--panel ../../data/derived/ml/player_gameweeks.parquet $(DERIVED_SEASON)/ml/player_gameweeks.parquet \
		--bootstrap $(RAW_SEASON)/bootstrap/bootstrap-static.json
	-cd apps/backend && uv run python -m backend.ml.teamenv --season $(SEASON) --data-root ../../data
	cd apps/backend && uv run python -m backend.ml.waiver --season $(SEASON) --scorer $(SCORER) --horizon $(HORIZON)
	cd apps/backend && uv run python -m backend.ml.myweek --season $(SEASON) --scorer $(SCORER)
	cd apps/backend && uv run python -m backend.ml.player_values --season $(SEASON)
	cd apps/backend && uv run python -m backend.ml.trackrecord --season $(SEASON) --data-root ../../data

## weekly: the whole weekly loop (fetch + derive)
weekly: fetch derive

## backtest: walk-forward evaluation of the match xP model vs the naive baselines
backtest:
	cd apps/backend && uv run python -m backend.ml.matcheval && uv run python -m backend.ml.matchmodel --backtest

## xp: build xp_gw$(GW).parquet and xp_horizon_gw$(GW).parquet (GW..GW+2) for a specific gameweek
## (SEASON defaults to 2026-27; derive does the next GW)
xp:
	@test -n "$(GW)" || (echo "usage: make xp GW=2 [SEASON=2026-27]"; exit 1)
	@test -n "$(SEASON)" || (echo "usage: make xp GW=2 [SEASON=2026-27]"; exit 1)
	cd apps/backend && uv run python -m backend.ml.matchmodel \
		--panel ../../data/derived/ml/player_gameweeks.parquet \
		        ../../data/derived/$(SEASON)/ml/player_gameweeks.parquet \
		--gw $(GW) --season $(SEASON) \
		&& uv run python -m backend.ml.matchmodel \
		--panel ../../data/derived/ml/player_gameweeks.parquet \
		        ../../data/derived/$(SEASON)/ml/player_gameweeks.parquet \
		--gw $(GW) --season $(SEASON) --horizon

## livefetch: minimal in-play refresh — current GW live points only (~2s)
livefetch:
	cd apps/mcp-server && go run ./cmd/dev --season $(SEASON) $(GO_ROOTS) --live-only --refresh-now

## matchday: near-live loop for game days — live points every 60s, full refresh every 10 min
matchday:
	bash scripts/matchday.sh

## preflight: full local CI (Go vet/test/fmt + uv sync/ruff/pytest)
preflight:
	bash scripts/preflight.sh

## autopilot: install the macOS background agents (always-on server + 15-min refresh)
autopilot:
	bash scripts/install-autopilot.sh

## autopilot-off: remove the background agents
autopilot-off:
	bash scripts/uninstall-autopilot.sh

## restart-server: reload the autopilot server (after code changes)
restart-server:
	launchctl kickstart -k gui/$$(id -u)/com.fplcopilot.server

## update: pull latest code and restart the autopilot server
update:
	git pull --ff-only
	-launchctl kickstart -k gui/$$(id -u)/com.fplcopilot.server

## stop / start: pause or resume the autopilot agents without uninstalling
stop:
	-launchctl bootout gui/$$(id -u)/com.fplcopilot.server
	-launchctl bootout gui/$$(id -u)/com.fplcopilot.refresh
start:
	launchctl bootstrap gui/$$(id -u) $$HOME/Library/LaunchAgents/com.fplcopilot.server.plist
	launchctl bootstrap gui/$$(id -u) $$HOME/Library/LaunchAgents/com.fplcopilot.refresh.plist

## tui: live matchup dashboard in the terminal (game days; ←/→ switch matchup, r refresh, q quit)
tui:
	cd apps/mcp-server && go run ./cmd/tui --raw-root ../../data/raw --derived-root ../../data/derived
