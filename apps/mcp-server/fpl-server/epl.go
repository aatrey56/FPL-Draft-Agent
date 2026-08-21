package main

// epl — the real Premier League in one call (consolidates epl_standings +
// epl_fixtures): league table, fixture results for a GW, or both.

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type EPLArgs struct {
	View string `json:"view,omitempty" jsonschema:"standings | fixtures | both (default both)"`
	GW   int    `json:"gw,omitempty" jsonschema:"Gameweek for fixture results (default: current)"`
}

func eplHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, EPLArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args EPLArgs) (*mcp.CallToolResult, any, error) {
		view := args.View
		if view == "" {
			view = "both"
		}
		out := map[string]any{}

		if view == "standings" || view == "both" {
			if standings, err := buildEPLStandings(cfg); err == nil {
				out["standings"] = standings
			} else {
				out["standings_error"] = err.Error()
			}
		}
		if view == "fixtures" || view == "both" {
			gw, err := resolveGW(cfg, args.GW)
			if err != nil {
				out["fixtures_error"] = err.Error()
			} else if fixtures, err := buildEPLFixtures(cfg, gw); err == nil {
				out["fixtures"] = fixtures
			} else {
				out["fixtures_error"] = err.Error()
			}
		}
		if len(out) == 0 {
			return toolError(fmt.Errorf("unknown view %q (use standings, fixtures, or both)", args.View)), nil, nil
		}
		return toolMarshal(out)
	}
}
