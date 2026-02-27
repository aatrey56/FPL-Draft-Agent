package main

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// EPLFixturesArgs is the input schema for the epl_fixtures tool.
type EPLFixturesArgs struct {
	GW int `json:"gw" jsonschema:"Gameweek number (0 = current)"`
}

// EPLFixture is a single Premier League fixture result.
type EPLFixture struct {
	Home      string `json:"home"`
	HomeShort string `json:"home_short"`
	Away      string `json:"away"`
	AwayShort string `json:"away_short"`
	HomeScore *int   `json:"home_score"`
	AwayScore *int   `json:"away_score"`
	Finished  bool   `json:"finished"`
	Started   bool   `json:"started"`
}

// EPLFixturesResult is the output of the epl_fixtures tool.
type EPLFixturesResult struct {
	Gameweek int          `json:"gameweek"`
	Fixtures []EPLFixture `json:"fixtures"`
}

// buildEPLFixtures constructs the fixture results for a single gameweek.
func buildEPLFixtures(cfg ServerConfig, gw int) (*EPLFixturesResult, error) {
	teams, err := loadTeams(cfg.RawRoot)
	if err != nil {
		return nil, err
	}
	resolvedGW, err := resolveGW(cfg, gw)
	if err != nil {
		return nil, err
	}
	rawFixtures, err := loadFixtureResults(cfg.RawRoot, resolvedGW)
	if err != nil {
		return nil, fmt.Errorf("gw %d fixtures: %w", resolvedGW, err)
	}

	fixtures := make([]EPLFixture, 0, len(rawFixtures))
	for _, f := range rawFixtures {
		home, ok := teams[f.TeamH]
		if !ok {
			home = Team{Name: fmt.Sprintf("Team %d", f.TeamH), ShortName: "???"}
		}
		away, ok := teams[f.TeamA]
		if !ok {
			away = Team{Name: fmt.Sprintf("Team %d", f.TeamA), ShortName: "???"}
		}
		fixtures = append(fixtures, EPLFixture{
			Home:      home.Name,
			HomeShort: home.ShortName,
			Away:      away.Name,
			AwayShort: away.ShortName,
			HomeScore: f.TeamHS,
			AwayScore: f.TeamAS,
			Finished:  f.Finished,
			Started:   f.Started,
		})
	}
	return &EPLFixturesResult{Gameweek: resolvedGW, Fixtures: fixtures}, nil
}

// eplFixturesHandler is the MCP tool handler for epl_fixtures.
func eplFixturesHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, EPLFixturesArgs) (*mcp.CallToolResult, any, error) {
	return func(ctx context.Context, req *mcp.CallToolRequest, args EPLFixturesArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildEPLFixtures(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolMarshal(out)
	}
}
