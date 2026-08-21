package main

// gw_report — the post-gameweek review in one call (consolidates
// matchup_breakdown + lineup_efficiency): why each matchup was won or lost,
// and who left points on the bench. The live view during matches is gw_live.

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type GwReportArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	GW       int `json:"gw,omitempty" jsonschema:"Finished gameweek to review (0 = current)"`
}

func gwReportHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, GwReportArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args GwReportArgs) (*mcp.CallToolResult, any, error) {
		if args.LeagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		out := map[string]any{"gw": gw}

		matchupPath := fmt.Sprintf("summary/matchup/%d/gw/%d.json", args.LeagueID, gw)
		if raw, err := loadSummaryFile(cfg, args.LeagueID, gw, matchupPath, nil, nil); err == nil {
			out["matchup_breakdown"] = json.RawMessage(raw)
		} else {
			out["matchup_breakdown_error"] = err.Error()
		}

		efficiencyPath := fmt.Sprintf("summary/lineup_efficiency/%d/gw/%d.json", args.LeagueID, gw)
		if raw, err := loadSummaryFile(cfg, args.LeagueID, gw, efficiencyPath, nil, nil); err == nil {
			out["lineup_efficiency"] = json.RawMessage(raw)
		} else {
			out["lineup_efficiency_error"] = err.Error()
		}

		out["note"] = "Post-GW review: matchup_breakdown = points by position per matchup; lineup_efficiency = bench points and zero-minute starters. For an in-progress GW use gw_live instead."
		return toolMarshal(out)
	}
}
