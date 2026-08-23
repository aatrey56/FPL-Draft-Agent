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

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/autosub"
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
	// Bonus is awarded live by the FPL API during matches and is already
	// included in TotalPoints — never add it on top. Bps is the running
	// tally it is derived from, and can still move until the whistle.
	Bonus int `json:"bonus"`
	Bps   int `json:"bps"`
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
			Fixtures []struct {
				TeamH        int  `json:"team_h"`
				TeamA        int  `json:"team_a"`
				Started      bool `json:"started"`
				Finished     bool `json:"finished"`
				FinishedProv bool `json:"finished_provisional"`
			} `json:"fixtures"`
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
		type elInfo struct {
			Name, Pos, Team string
			PosCode, TeamID int
		}
		elByID := map[int]elInfo{}
		for _, e := range bootstrap.Elements {
			elByID[e.ID] = elInfo{e.WebName, positions[e.ElementType], teamShort[e.Team], e.ElementType, e.Team}
		}
		// A club with no unfinished fixture this GW counts as done — that is
		// when a 0-minute starter becomes a confirmed non-player. In a double
		// gameweek every one of the club's fixtures must be finished.
		fixtureDone := map[int]bool{}
		for _, f := range live.Fixtures {
			fDone := f.Finished || f.FinishedProv
			for _, team := range []int{f.TeamH, f.TeamA} {
				merged := fDone
				if cur, seen := fixtureDone[team]; seen {
					merged = merged && cur
				}
				fixtureDone[team] = merged
			}
		}
		teamDone := func(teamID int) bool {
			done, known := fixtureDone[teamID]
			return !known || done
		}
		teamStarted := map[int]bool{}
		for _, f := range live.Fixtures {
			if f.Started {
				teamStarted[f.TeamH] = true
				teamStarted[f.TeamA] = true
			}
		}

		// Official team sheets (best effort): xi/bench per element, and which
		// clubs have released one. Absent file = no sheet knowledge.
		var squads struct {
			Fixtures []struct {
				Sheets map[string]struct {
					XI    []int `json:"xi"`
					Bench []int `json:"bench"`
				} `json:"sheets"`
			} `json:"fixtures"`
		}
		sheetRole := map[int]string{}
		clubSheet := map[string]bool{}
		if err := readJSONFile(filepath.Join(rawDir, fmt.Sprintf("gw/%d/squads.json", gw)), &squads); err == nil {
			for _, fx := range squads.Fixtures {
				for club, sheet := range fx.Sheets {
					if len(sheet.XI) == 0 {
						continue
					}
					clubSheet[club] = true
					for _, id := range sheet.XI {
						sheetRole[id] = "xi"
					}
					for _, id := range sheet.Bench {
						sheetRole[id] = "bench"
					}
				}
			}
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
			squad := make([]autosub.Player, 0, len(snapshot.Picks))
			nameBySlot := map[int]string{}
			pointsBySlot := map[int]int{}
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
				role := sheetRole[p.Element]
				sheetOut := clubSheet[info.Team] && role == ""
				status := role
				if sheetOut {
					status = "out" // squad released without him — ruled out
				}
				squad = append(squad, autosub.Player{
					Slot: p.Position, Pos: info.PosCode, Minutes: stats.Minutes,
					FixtureDone: teamDone(info.TeamID) || (teamStarted[info.TeamID] && sheetOut),
				})
				nameBySlot[p.Position] = info.Name
				pointsBySlot[p.Position] = stats.TotalPoints
				players = append(players, map[string]any{
					"slot": p.Position, "starter": starter,
					"web_name": info.Name, "position": info.Pos, "team": info.Team,
					"minutes": stats.Minutes, "points": stats.TotalPoints,
					"goals": stats.GoalsScored, "assists": stats.Assists,
					"bonus": stats.Bonus, "bps": stats.Bps,
					"squad_status": status,
				})
			}
			// Projected end-of-GW auto-subs (draft rules): confirmed 0-minute
			// starters replaced from the bench, keeper-for-keeper, formation
			// kept legal. Effective points = live_points with those applied.
			effective := liveTotal
			subs := []map[string]any{}
			if len(live.Fixtures) > 0 {
				for _, sw := range autosub.Project(squad) {
					effective += pointsBySlot[sw.In]
					subs = append(subs, map[string]any{
						"out": nameBySlot[sw.Out], "in": nameBySlot[sw.In],
						"in_points": pointsBySlot[sw.In],
					})
				}
			}
			return map[string]any{
				"team_name": nameByEntry[entryID], "entry_id": entryID,
				"live_points": liveTotal, "effective_points": effective,
				"projected_auto_subs": subs, "bench_points": benchTotal,
				"starters_played": played, "players": players,
			}, nil
		}

		me, err := side(args.EntryID)
		if err != nil {
			return toolError(err), nil, nil
		}
		result := map[string]any{
			"gw": gw, "me": me,
			"note": "Points come straight from the FPL live endpoint and already include any bonus awarded so far — bonus is now published during matches, not after. Bonus can still move while a match is in progress (bps shows the running tally); scores lock the morning after the GW's final match. squad_status (from official team sheets, ~1h pre-kickoff): 'xi' named starter, 'bench' in squad, 'out' left out entirely, empty = no sheet yet. effective_points = live_points plus projected_auto_subs: draft auto-substitutions (0-minute starters whose fixture finished, replaced in bench order, keeper-for-keeper, formation kept legal) that will apply when the GW completes. Refresh the snapshot with a full (non --fast) fetch.",
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
