package main

// gw_live — the in-play matchup tracker ("how's my matchup going?").
//
// Local files only, like every tool: the fetcher writes gw/<n>/live.json and
// per-entry picks during a refresh; run one mid-match refresh (full mode, not
// --fast) and this tool reads the snapshot:
//
//	gw_live <- game/game.json            (which GW is current)
//	        + league/<id>/details.json   (who my H2H opponent is this event)
//	        + entry/<id>/gw/<gw>.json    (both sides' picks: 1-11 start, 12-15 bench)
//	        + gw/<gw>/live.json          (per-player in-play stats)
//	        + bootstrap                  (names, positions, teams)

import (
	"context"
	"fmt"
	"path/filepath"
	"sort"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type GwLiveArgs struct {
	LeagueID int    `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID  int    `json:"entry_id" jsonschema:"My entry (team) id (required)"`
	GW       int    `json:"gw,omitempty" jsonschema:"Gameweek to track (default: the current event)"`
	Season   string `json:"season,omitempty" jsonschema:"Season (default: server default season)"`
}

type gwLiveStats struct {
	Minutes     int `json:"minutes"`
	TotalPoints int `json:"total_points"`
	GoalsScored int `json:"goals_scored"`
	Assists     int `json:"assists"`
	Bonus       int `json:"bonus"`
	Bps         int `json:"bps"`
}

func gwLiveHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, GwLiveArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args GwLiveArgs) (*mcp.CallToolResult, any, error) {
		if args.LeagueID == 0 || args.EntryID == 0 {
			return toolError(fmt.Errorf("league_id and entry_id are required")), nil, nil
		}
		rawDir := cfg.rawDir(args.Season)

		gw := args.GW
		if gw == 0 {
			var game struct {
				CurrentEvent int `json:"current_event"`
				NextEvent    int `json:"next_event"`
			}
			if err := readJSONFile(filepath.Join(rawDir, "game/game.json"), &game); err != nil {
				return toolError(err), nil, nil
			}
			gw = game.CurrentEvent
			if gw == 0 {
				gw = game.NextEvent
			}
		}

		var details struct {
			LeagueEntries []struct {
				ID        int    `json:"id"`
				EntryID   int    `json:"entry_id"`
				EntryName string `json:"entry_name"`
			} `json:"league_entries"`
			Matches []struct {
				Event        int `json:"event"`
				LeagueEntry1 int `json:"league_entry_1"`
				LeagueEntry2 int `json:"league_entry_2"`
			} `json:"matches"`
		}
		if err := readJSONFile(filepath.Join(rawDir, fmt.Sprintf("league/%d/details.json", args.LeagueID)), &details); err != nil {
			return toolError(err), nil, nil
		}
		nameByEntry := map[int]string{}
		entryByLeagueEntry := map[int]int{}
		myLeagueEntry := 0
		for _, e := range details.LeagueEntries {
			nameByEntry[e.EntryID] = e.EntryName
			entryByLeagueEntry[e.ID] = e.EntryID
			if e.EntryID == args.EntryID {
				myLeagueEntry = e.ID
			}
		}
		if myLeagueEntry == 0 {
			return toolError(fmt.Errorf("entry %d is not in league %d", args.EntryID, args.LeagueID)), nil, nil
		}
		opponentEntry := 0
		for _, m := range details.Matches {
			if m.Event != gw {
				continue
			}
			if m.LeagueEntry1 == myLeagueEntry {
				opponentEntry = entryByLeagueEntry[m.LeagueEntry2]
			}
			if m.LeagueEntry2 == myLeagueEntry {
				opponentEntry = entryByLeagueEntry[m.LeagueEntry1]
			}
		}

		var live struct {
			Elements map[string]struct {
				Stats gwLiveStats `json:"stats"`
			} `json:"elements"`
		}
		if err := readJSONFile(filepath.Join(rawDir, fmt.Sprintf("gw/%d/live.json", gw)), &live); err != nil {
			return toolError(fmt.Errorf("no live snapshot for GW %d yet — run a full (non --fast) fetch refresh during or after matches: %w", gw, err)), nil, nil
		}

		var bootstrap struct {
			Elements []struct {
				ID          int    `json:"id"`
				WebName     string `json:"web_name"`
				ElementType int    `json:"element_type"`
				Team        int    `json:"team"`
			} `json:"elements"`
			Teams []struct {
				ID        int    `json:"id"`
				ShortName string `json:"short_name"`
			} `json:"teams"`
		}
		if err := readJSONFile(filepath.Join(rawDir, "bootstrap/bootstrap-static.json"), &bootstrap); err != nil {
			return toolError(err), nil, nil
		}
		teamShort := map[int]string{}
		for _, t := range bootstrap.Teams {
			teamShort[t.ID] = t.ShortName
		}
		positions := map[int]string{1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
		elByID := map[int]struct {
			Name, Pos, Team string
		}{}
		for _, e := range bootstrap.Elements {
			elByID[e.ID] = struct{ Name, Pos, Team string }{e.WebName, positions[e.ElementType], teamShort[e.Team]}
		}

		side := func(entryID int) (map[string]any, error) {
			var snapshot struct {
				Picks []struct {
					Element  int `json:"element"`
					Position int `json:"position"`
				} `json:"picks"`
			}
			path := filepath.Join(rawDir, fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw))
			if err := readJSONFile(path, &snapshot); err != nil {
				return nil, err
			}
			liveTotal, benchTotal, played := 0, 0, 0
			players := make([]map[string]any, 0, len(snapshot.Picks))
			sort.Slice(snapshot.Picks, func(i, j int) bool {
				return snapshot.Picks[i].Position < snapshot.Picks[j].Position
			})
			for _, p := range snapshot.Picks {
				stats := live.Elements[fmt.Sprintf("%d", p.Element)].Stats
				info := elByID[p.Element]
				starter := p.Position <= 11
				if starter {
					liveTotal += stats.TotalPoints
					if stats.Minutes > 0 {
						played++
					}
				} else {
					benchTotal += stats.TotalPoints
				}
				players = append(players, map[string]any{
					"slot": p.Position, "starter": starter,
					"web_name": info.Name, "position": info.Pos, "team": info.Team,
					"minutes": stats.Minutes, "points": stats.TotalPoints,
					"goals": stats.GoalsScored, "assists": stats.Assists, "bps": stats.Bps,
				})
			}
			return map[string]any{
				"team_name": nameByEntry[entryID], "entry_id": entryID,
				"live_points": liveTotal, "bench_points": benchTotal,
				"starters_played": played, "players": players,
			}, nil
		}

		me, err := side(args.EntryID)
		if err != nil {
			return toolError(err), nil, nil
		}
		result := map[string]any{
			"gw": gw, "me": me,
			"note": "In-play points are provisional: bonus is estimated from BPS until the day's last match ends, and everything locks the morning after the GW's final match. Refresh the snapshot with a full (non --fast) fetch.",
		}
		if opponentEntry != 0 {
			if opp, err := side(opponentEntry); err == nil {
				result["opponent"] = opp
			} else {
				result["opponent_error"] = err.Error()
			}
		}
		return toolMarshal(result)
	}
}
