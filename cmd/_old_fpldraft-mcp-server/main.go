package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"time"

	"fpl-draft-mcp/internal/draftapi"
	"fpl-draft-mcp/internal/insights"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

var (
	cacheDir = flag.String("cache-dir", "data-cache", "directory for cached API responses")
)

type WeeklyPointsArgs struct {
	LeagueID int  `json:"league_id" jsonschema:"Draft league id (e.g. 14204)"`
	Refresh  bool `json:"refresh" jsonschema:"If true, bypass cache and refetch"`
}

type ScheduleDifficultyArgs struct {
	LeagueID   int  `json:"league_id" jsonschema:"Draft league id (e.g. 14204)"`
	AsOfGW     int  `json:"as_of_gw" jsonschema:"Compute schedule after this GW (0 = use current GW from /api/game)"`
	Lookahead  int  `json:"lookahead" jsonschema:"How many future opponents to consider (default 3)"`
	FormWindow int  `json:"form_window" jsonschema:"Opponent form window in GWs (default 3)"`
	Refresh    bool `json:"refresh" jsonschema:"If true, bypass cache and refetch"`
}

func main() {
	flag.Parse()

	api := draftapi.NewClient(*cacheDir)

	server := mcp.NewServer(
		&mcp.Implementation{
			Name:    "fpl-draft-mcp",
			Version: "0.1.0",
		},
		nil,
	)

	// Tool: weekly points (team x GW)
	mcp.AddTool(server, &mcp.Tool{
		Name:        "draft_weekly_points",
		Description: "Returns every team's points by gameweek, derived from league match results",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args WeeklyPointsArgs) (*mcp.CallToolResult, any, error) {
		ld, err := api.GetLeagueDetails(args.LeagueID, args.Refresh)
		if err != nil {
			return toolError(err), nil, nil
		}

		nameBy := insights.TeamNameMap(ld)
		weekly := insights.ComputeWeeklyPoints(ld)

		out := map[string]any{
			"league_id":        ld.League.ID,
			"league":           ld.League.Name,
			"team_name_by_id":  nameBy,
			"weekly_points":    weekly,
			"cache_dir":        *cacheDir,
			"generated_at_utc": time.Now().UTC().Format(time.RFC3339),
		}

		return toolJSON(out), nil, nil
	})

	// Tool: schedule difficulty
	mcp.AddTool(server, &mcp.Tool{
		Name:        "draft_schedule_difficulty",
		Description: "Ranks teams by average opponent recent scoring (form) over the next K matchups. Lower = easier.",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args ScheduleDifficultyArgs) (*mcp.CallToolResult, any, error) {
		ld, err := api.GetLeagueDetails(args.LeagueID, args.Refresh)
		if err != nil {
			return toolError(err), nil, nil
		}

		asOf := args.AsOfGW
		if asOf == 0 {
			g, err := api.GetGame(args.Refresh)
			if err != nil {
				return toolError(err), nil, nil
			}
			asOf = g.CurrentEvent
		}

		lookahead := args.Lookahead
		if lookahead <= 0 {
			lookahead = 3
		}
		window := args.FormWindow
		if window <= 0 {
			window = 3
		}

		nameBy := insights.TeamNameMap(ld)
		weekly := insights.ComputeWeeklyPoints(ld)
		rows := insights.ComputeScheduleDifficulty(ld, weekly, asOf, lookahead, window)

		// Attach names for readability
		type RowWithNames struct {
			Team       string           `json:"team"`
			Difficulty float64          `json:"difficulty"`
			Opponents  []map[string]any `json:"opponents"`
		}

		withNames := make([]RowWithNames, 0, len(rows))
		for _, r := range rows {
			oppOut := make([]map[string]any, 0, len(r.Opponents))
			for _, o := range r.Opponents {
				oppOut = append(oppOut, map[string]any{
					"gameweek":      o.Gameweek,
					"opponent_id":   o.Opponent,
					"opponent":      nameBy[o.Opponent],
					"opponent_form": o.Form,
				})
			}
			withNames = append(withNames, RowWithNames{
				Team:       nameBy[r.TeamID],
				Difficulty: r.Difficulty,
				Opponents:  oppOut,
			})
		}

		out := map[string]any{
			"league_id":   ld.League.ID,
			"league":      ld.League.Name,
			"as_of_gw":    asOf,
			"lookahead":   lookahead,
			"form_window": window,
			"explanation": "difficulty = average(opponent recent form) over next lookahead matchups; lower means easier schedule",
			"schedule":    withNames,
			"cache_dir":   *cacheDir,
		}

		return toolJSON(out), nil, nil
	})

	// Run MCP server over stdin/stdout.
	if err := server.Run(context.Background(), &mcp.StdioTransport{}); err != nil {
		log.Fatal(err)
	}
}

func toolJSON(v any) *mcp.CallToolResult {
	b, _ := json.MarshalIndent(v, "", "  ")
	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{Text: string(b)},
		},
	}
}

func toolError(err error) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{
			&mcp.TextContent{Text: fmt.Sprintf("error: %v", err)},
		},
	}
}