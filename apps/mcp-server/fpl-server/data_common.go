package main

// Shared data-layer plumbing: bootstrap parsing (elementInfo), per-GW live
// stats, and the points-conceded-by-position aggregation used by
// fixture_difficulty. Extracted from the retired waiver_recommendations tool
// (superseded by waiver_plan) — several kept tools depend on these.

import (
	"encoding/json"
	"fmt"
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

type GameMeta struct {
	CurrentEvent         int  `json:"current_event"`
	CurrentEventFinished bool `json:"current_event_finished"`
	NextEvent            int  `json:"next_event"`
}

func loadGameMeta(cfg ServerConfig) (GameMeta, error) {
	path := fmt.Sprintf("%s/game/game.json", strings.TrimRight(cfg.rawDir(""), "/"))
	raw, err := os.ReadFile(path)
	if err != nil {
		return GameMeta{}, err
	}
	var meta GameMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return GameMeta{}, err
	}
	return meta, nil
}
