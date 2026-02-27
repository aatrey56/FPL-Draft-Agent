package main

import (
	"context"
	"fmt"
	"sort"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// EPLStandingsArgs is the input schema for the epl_standings tool.
type EPLStandingsArgs struct{}

// EPLStandingsRow represents one team's row in the league table.
type EPLStandingsRow struct {
	Pos    int    `json:"pos"`
	Team   string `json:"team"`
	Short  string `json:"short"`
	Played int    `json:"played"`
	Won    int    `json:"won"`
	Drawn  int    `json:"drawn"`
	Lost   int    `json:"lost"`
	GF     int    `json:"gf"`
	GA     int    `json:"ga"`
	GD     int    `json:"gd"`
	Points int    `json:"points"`
}

// EPLStandingsResult is the output of the epl_standings tool.
type EPLStandingsResult struct {
	AsOfGW    int               `json:"as_of_gw"`
	Standings []EPLStandingsRow `json:"standings"`
}

// teamAccum accumulates W/D/L/GF/GA for a single team.
type teamAccum struct {
	TeamID int
	Won    int
	Drawn  int
	Lost   int
	GF     int
	GA     int
}

// buildEPLStandings computes the Premier League standings table
// by iterating over all completed fixtures from GW 1 to current.
func buildEPLStandings(cfg ServerConfig) (*EPLStandingsResult, error) {
	teams, err := loadTeams(cfg.RawRoot)
	if err != nil {
		return nil, err
	}
	meta, err := loadGameMeta(cfg)
	if err != nil {
		return nil, err
	}
	currentGW := meta.CurrentEvent
	if currentGW < 1 {
		return nil, fmt.Errorf("no gameweeks played yet")
	}

	accum := make(map[int]*teamAccum)
	for id := range teams {
		accum[id] = &teamAccum{TeamID: id}
	}

	for gw := 1; gw <= currentGW; gw++ {
		fixtures, err := loadFixtureResults(cfg.RawRoot, gw)
		if err != nil {
			// Missing GW data — skip gracefully
			continue
		}
		for _, f := range fixtures {
			if !f.Finished {
				continue
			}
			if f.TeamHS == nil || f.TeamAS == nil {
				continue
			}
			hs, as := *f.TeamHS, *f.TeamAS

			home := accum[f.TeamH]
			if home == nil {
				home = &teamAccum{TeamID: f.TeamH}
				accum[f.TeamH] = home
			}
			away := accum[f.TeamA]
			if away == nil {
				away = &teamAccum{TeamID: f.TeamA}
				accum[f.TeamA] = away
			}

			home.GF += hs
			home.GA += as
			away.GF += as
			away.GA += hs

			if hs > as {
				home.Won++
				away.Lost++
			} else if hs < as {
				away.Won++
				home.Lost++
			} else {
				home.Drawn++
				away.Drawn++
			}
		}
	}

	rows := make([]EPLStandingsRow, 0, len(accum))
	for id, a := range accum {
		t, ok := teams[id]
		if !ok {
			continue
		}
		played := a.Won + a.Drawn + a.Lost
		if played == 0 {
			continue
		}
		rows = append(rows, EPLStandingsRow{
			Team:   t.Name,
			Short:  t.ShortName,
			Played: played,
			Won:    a.Won,
			Drawn:  a.Drawn,
			Lost:   a.Lost,
			GF:     a.GF,
			GA:     a.GA,
			GD:     a.GF - a.GA,
			Points: a.Won*3 + a.Drawn,
		})
	}

	// EPL sort: Points DESC → GD DESC → GF DESC → Team name ASC
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Points != rows[j].Points {
			return rows[i].Points > rows[j].Points
		}
		if rows[i].GD != rows[j].GD {
			return rows[i].GD > rows[j].GD
		}
		if rows[i].GF != rows[j].GF {
			return rows[i].GF > rows[j].GF
		}
		return rows[i].Team < rows[j].Team
	})

	// Assign positions (identical records share the same position)
	for i := range rows {
		if i == 0 {
			rows[i].Pos = 1
		} else if rows[i].Points == rows[i-1].Points &&
			rows[i].GD == rows[i-1].GD &&
			rows[i].GF == rows[i-1].GF {
			rows[i].Pos = rows[i-1].Pos
		} else {
			rows[i].Pos = i + 1
		}
	}

	return &EPLStandingsResult{AsOfGW: currentGW, Standings: rows}, nil
}

// eplStandingsHandler is the MCP tool handler for epl_standings.
func eplStandingsHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, EPLStandingsArgs) (*mcp.CallToolResult, any, error) {
	return func(ctx context.Context, req *mcp.CallToolRequest, args EPLStandingsArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildEPLStandings(cfg)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolMarshal(out)
	}
}
