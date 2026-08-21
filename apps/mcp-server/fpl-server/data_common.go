package main

// Shared data-layer plumbing: bootstrap parsing (elementInfo), per-GW live
// stats, and the points-conceded-by-position aggregation used by
// fixture_difficulty. Extracted from the retired waiver_recommendations tool
// (superseded by waiver_plan) — several kept tools depend on these.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

type elementInfo struct {
	ID           int
	Name         string
	TeamID       int
	PositionType int
	Status       string
	TotalPoints  int

	// Fields from bootstrap-static.json used by the expanded scoring model.
	SeasonXGI           float64 // expected_goal_involvements
	SeasonXA            float64 // expected_assists
	SeasonBonus         int     // bonus
	SeasonBPS           int     // bps
	SeasonGoals         int     // goals_scored
	SeasonAssists       int     // assists
	SeasonCleanSheets   int     // clean_sheets
	SeasonSaves         int     // saves
	ChanceOfPlayingNext int     // chance_of_playing_next_round (0-100; -1 = null/unknown)
	ICTIndex            float64 // ict_index (string in JSON, parsed to float)
	SeasonMinutes       int     // minutes
}

type fixture struct {
	ID    int
	Event int
	TeamH int
	TeamA int
}

type liveStats struct {
	Minutes     int
	TotalPoints int
	XG          float64

	// Fields from gw/N/live.json used by the expanded scoring model.
	XA          float64
	Goals       int
	Assists     int
	CleanSheets int
	Bonus       int
	BPS         int
	Saves       int
}

func loadBootstrapData(rawRoot string) ([]elementInfo, map[int]string, map[int][]fixture, error) {
	path := filepath.Join(rawRoot, "bootstrap", "bootstrap-static.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, nil, err
	}
	var resp struct {
		Elements []struct {
			ID                       int    `json:"id"`
			WebName                  string `json:"web_name"`
			Team                     int    `json:"team"`
			ElementType              int    `json:"element_type"`
			Status                   string `json:"status"`
			TotalPoints              int    `json:"total_points"`
			ExpectedGoalInvolvements string `json:"expected_goal_involvements"`
			ExpectedAssists          string `json:"expected_assists"`
			Bonus                    int    `json:"bonus"`
			BPS                      int    `json:"bps"`
			GoalsScored              int    `json:"goals_scored"`
			Assists                  int    `json:"assists"`
			CleanSheets              int    `json:"clean_sheets"`
			Saves                    int    `json:"saves"`
			ChanceOfPlayingNext      *int   `json:"chance_of_playing_next_round"`
			ICTIndex                 string `json:"ict_index"`
			Minutes                  int    `json:"minutes"`
		} `json:"elements"`
		Teams []struct {
			ID        int    `json:"id"`
			ShortName string `json:"short_name"`
		} `json:"teams"`
		Fixtures map[string][]struct {
			ID    int `json:"id"`
			Event int `json:"event"`
			TeamH int `json:"team_h"`
			TeamA int `json:"team_a"`
		} `json:"fixtures"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, nil, nil, err
	}

	teams := make(map[int]string, len(resp.Teams))
	for _, t := range resp.Teams {
		teams[t.ID] = t.ShortName
	}

	elements := make([]elementInfo, 0, len(resp.Elements))
	for _, e := range resp.Elements {
		chanceNext := -1
		if e.ChanceOfPlayingNext != nil {
			chanceNext = *e.ChanceOfPlayingNext
		}
		ictIndex, _ := strconv.ParseFloat(e.ICTIndex, 64)
		seasonXGI, _ := strconv.ParseFloat(e.ExpectedGoalInvolvements, 64)
		seasonXA, _ := strconv.ParseFloat(e.ExpectedAssists, 64)

		elements = append(elements, elementInfo{
			ID:                  e.ID,
			Name:                e.WebName,
			TeamID:              e.Team,
			PositionType:        e.ElementType,
			Status:              e.Status,
			TotalPoints:         e.TotalPoints,
			SeasonXGI:           seasonXGI,
			SeasonXA:            seasonXA,
			SeasonBonus:         e.Bonus,
			SeasonBPS:           e.BPS,
			SeasonGoals:         e.GoalsScored,
			SeasonAssists:       e.Assists,
			SeasonCleanSheets:   e.CleanSheets,
			SeasonSaves:         e.Saves,
			ChanceOfPlayingNext: chanceNext,
			ICTIndex:            ictIndex,
			SeasonMinutes:       e.Minutes,
		})
	}

	fixtures := make(map[int][]fixture)
	for k, list := range resp.Fixtures {
		gw, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		for _, f := range list {
			fixtures[gw] = append(fixtures[gw], fixture{
				ID:    f.ID,
				Event: gw,
				TeamH: f.TeamH,
				TeamA: f.TeamA,
			})
		}
	}
	return elements, teams, fixtures, nil
}

type liveGWData struct {
	Stats    map[int]liveStats
	Fixtures []fixture
}

func loadLiveGWData(rawRoot string, gw int) (liveGWData, error) {
	path := filepath.Join(rawRoot, "gw", strconv.Itoa(gw), "live.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return liveGWData{}, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber() // required: elements are decoded into map[string]any
	var resp struct {
		Elements map[string]struct {
			Stats map[string]any `json:"stats"`
		} `json:"elements"`
		Fixtures []struct {
			ID    int `json:"id"`
			TeamH int `json:"team_h"`
			TeamA int `json:"team_a"`
		} `json:"fixtures"`
	}
	if err := dec.Decode(&resp); err != nil {
		return liveGWData{}, err
	}

	stats := make(map[int]liveStats, len(resp.Elements))
	for k, v := range resp.Elements {
		id, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		stats[id] = liveStats{
			Minutes:     int(asNumber(v.Stats["minutes"])),
			TotalPoints: int(asNumber(v.Stats["total_points"])),
			XG:          asFloat(v.Stats["expected_goals"]),
			XA:          asFloat(v.Stats["expected_assists"]),
			Goals:       int(asNumber(v.Stats["goals_scored"])),
			Assists:     int(asNumber(v.Stats["assists"])),
			CleanSheets: int(asNumber(v.Stats["clean_sheets"])),
			Bonus:       int(asNumber(v.Stats["bonus"])),
			BPS:         int(asNumber(v.Stats["bps"])),
			Saves:       int(asNumber(v.Stats["saves"])),
		}
	}

	fixtures := make([]fixture, 0, len(resp.Fixtures))
	for _, f := range resp.Fixtures {
		fixtures = append(fixtures, fixture{
			ID:    f.ID,
			Event: gw,
			TeamH: f.TeamH,
			TeamA: f.TeamA,
		})
	}

	return liveGWData{Stats: stats, Fixtures: fixtures}, nil
}

func computePointsConcededByPosition(rawRoot string, elements []elementInfo, asOfGW int, horizon int) map[int]map[string]map[int]avgStat {
	elementTeam := make(map[int]int, len(elements))
	elementPos := make(map[int]int, len(elements))
	for _, e := range elements {
		elementTeam[e.ID] = e.TeamID
		elementPos[e.ID] = e.PositionType
	}

	start := asOfGW - horizon + 1
	if start < 1 {
		start = 1
	}
	conceded := make(map[int]map[string]map[int]avgStat)
	for gw := start; gw <= asOfGW; gw++ {
		// Single file read supplies both element stats and fixture pairings.
		gwData, err := loadLiveGWData(rawRoot, gw)
		if err != nil {
			continue
		}
		pointsByTeamPos := make(map[int]map[int]int)
		for id, stats := range gwData.Stats {
			team := elementTeam[id]
			pos := elementPos[id]
			if team == 0 || pos == 0 {
				continue
			}
			if _, ok := pointsByTeamPos[team]; !ok {
				pointsByTeamPos[team] = make(map[int]int)
			}
			pointsByTeamPos[team][pos] += stats.TotalPoints
		}

		for _, f := range gwData.Fixtures {
			home := f.TeamH
			away := f.TeamA
			homePts := pointsByTeamPos[home]
			awayPts := pointsByTeamPos[away]

			for pos, pts := range awayPts {
				addConceded(conceded, home, "HOME", pos, float64(pts))
			}
			for pos, pts := range homePts {
				addConceded(conceded, away, "AWAY", pos, float64(pts))
			}
		}
	}
	return conceded
}

type avgStat struct {
	Sum   float64
	Count int
}

func addConceded(store map[int]map[string]map[int]avgStat, teamID int, venue string, pos int, val float64) {
	if _, ok := store[teamID]; !ok {
		store[teamID] = map[string]map[int]avgStat{"HOME": {}, "AWAY": {}}
	}
	cur := store[teamID][venue][pos]
	cur.Sum += val
	cur.Count++
	store[teamID][venue][pos] = cur
}

func fixtureDifficulty(conceded map[int]map[string]map[int]avgStat, opponentID int, venue string, pos int) float64 {
	if opponentID == 0 {
		return 0
	}
	venue = strings.ToUpper(venue)
	stats := conceded[opponentID][venue][pos]
	if stats.Count > 0 {
		return stats.Sum / float64(stats.Count)
	}
	// fallback to overall (both venues)
	home := conceded[opponentID]["HOME"][pos]
	away := conceded[opponentID]["AWAY"][pos]
	totalSum := home.Sum + away.Sum
	totalCount := home.Count + away.Count
	if totalCount == 0 {
		return 0
	}
	return totalSum / float64(totalCount)
}

type FixtureContext struct {
	FixtureID     int    `json:"fixture_id,omitempty"`
	Event         int    `json:"event,omitempty"`
	TeamID        int    `json:"team_id,omitempty"`
	TeamShort     string `json:"team_short,omitempty"`
	OpponentID    int    `json:"opponent_id"`
	OpponentShort string `json:"opponent_short"`
	Venue         string `json:"venue"`
}

type AvailabilityInfo struct {
	Minutes60Last3  int `json:"minutes_60_last3"`
	Minutes60Season int `json:"minutes_60_season"`
}

func asNumber(v any) float64 {
	switch t := v.(type) {
	case json.Number:
		f, _ := t.Float64()
		return f
	case float64:
		return t
	case string:
		f, _ := strconv.ParseFloat(t, 64)
		return f
	default:
		return 0
	}
}

func asFloat(v any) float64 {
	return asNumber(v)
}
