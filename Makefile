SEASON ?= 2026-27
GO_ROOTS := --raw-root ../../data/raw --derived-root ../../data/derived

.PHONY: serve fetch derive weekly matchday preflight

## serve: run the MCP server (Ctrl-C to stop; restart after every git pull)
serve:
	cd apps/mcp-server && go run ./fpl-server $(GO_ROOTS) --default-season $(SEASON)

## fetch: refresh all raw data for the season (game, league, live GW, picks, element-status)
fetch:
	cd apps/mcp-server && go run ./cmd/dev --season $(SEASON) $(GO_ROOTS) --refresh-now

## derive: rebuild the weekly decision artifacts (ownership -> waiver -> my_week)
derive:
	cd apps/backend && uv run python -m backend.ml.ownership && uv run python -m backend.ml.waiver && uv run python -m backend.ml.myweek

## weekly: the whole weekly loop (fetch + derive)
weekly: fetch derive

## matchday: refresh loop while games are on (Ctrl-C to stop; INTERVAL=seconds)
matchday:
	bash scripts/matchday.sh

## preflight: full local CI (Go vet/test/fmt + uv sync/ruff/pytest)
preflight:
	bash scripts/preflight.sh
