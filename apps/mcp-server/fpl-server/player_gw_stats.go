package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// PlayerGWStatsArgs are the input arguments for the player_gw_stats tool.
type PlayerGWStatsArgs struct {
	ElementID  *int    `json:"element_id,omitempty" jsonschema:"Player element id"`
	PlayerName *string `json:"player_name,omitempty" jsonschema:"Player name (if element_id not provided)"`
	StartGW    *int    `json:"start_gw,omitempty" jsonschema:"First gameweek to include (0 = 1)"`
	EndGW      *int    `json:"end_gw,omitempty" jsonschema:"Last gameweek to include (0 = current)"`
}

// PlayerGWEntry holds a player's stats for one gameweek.
type PlayerGWEntry struct {
	Gameweek    int     `json:"gameweek"`
	Minutes     int     `json:"minutes"`
	Points      int     `json:"points"`
	GoalsScored int     `json:"goals_scored"`
	Assists     int     `json:"assists"`
	CleanSheets int     `json:"clean_sheets"`
	BPS         int     `json:"bps"`
	XG          float64 `json:"expected_goals"`
	XA          float64 `json:"expected_assists"`
}

// PlayerGWStatsOutput is the output of the player_gw_stats tool.
type PlayerGWStatsOutput struct {
	ElementID    int             `json:"element_id"`
	PlayerName   string          `json:"player_name"`
	Team         string          `json:"team"`
	PositionType int             `json:"position_type"`
	StartGW      int             `json:"start_gw"`
	EndGW        int             `json:"end_gw"`
	TotalPoints  int             `json:"total_points"`
	AvgPoints    float64         `json:"avg_points"`
	TotalMinutes int             `json:"total_minutes"`
	Gameweeks    []PlayerGWEntry `json:"gameweeks"`
}

func buildPlayerGWStats(cfg ServerConfig, args PlayerGWStatsArgs) (PlayerGWStatsOutput, error) {
	// Resolve element ID (by id or by name search).
	elements, teamShort, _, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return PlayerGWStatsOutput{}, err
	}
	playerByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		playerByID[e.ID] = e
	}

	elementID := 0
	if args.ElementID != nil {
		elementID = *args.ElementID
	}
	if elementID == 0 {
		if args.PlayerName == nil || strings.TrimSpace(*args.PlayerName) == "" {
			return PlayerGWStatsOutput{}, fmt.Errorf("element_id or player_name is required")
		}
		needle := strings.ToLower(strings.TrimSpace(*args.PlayerName))
		// First try exact web_name match, then partial.
		for _, e := range elements {
			if strings.ToLower(e.Name) == needle {
				elementID = e.ID
				break
			}
		}
		if elementID == 0 {
			for _, e := range elements {
				if strings.Contains(strings.ToLower(e.Name), needle) {
					elementID = e.ID
					break
				}
			}
		}
		if elementID == 0 {
			return PlayerGWStatsOutput{}, fmt.Errorf("player not found: %s", *args.PlayerName)
		}
	}

	meta, ok := playerByID[elementID]
	if !ok {
		return PlayerGWStatsOutput{}, fmt.Errorf("element not found: %d", elementID)
	}

	// Resolve GW range.
	startGW := 1
	if args.StartGW != nil && *args.StartGW > 0 {
		startGW = *args.StartGW
	}
	endGW := 0
	if args.EndGW != nil && *args.EndGW > 0 {
		endGW = *args.EndGW
	}
	if endGW == 0 {
		resolved, err := resolveGW(cfg, 0)
		if err != nil {
			return PlayerGWStatsOutput{}, err
		}
		endGW = resolved
	}
	if endGW < startGW {
		endGW = startGW
	}

	// Iterate GW live files.
	gwEntries := make([]PlayerGWEntry, 0, endGW-startGW+1)
	totalPts := 0
	totalMins := 0
	gwCount := 0

	for gw := startGW; gw <= endGW; gw++ {
		livePath := filepath.Join(cfg.RawRoot, fmt.Sprintf("gw/%d/live.json", gw))
		liveRaw, err := os.ReadFile(livePath)
		if err != nil {
			// GW data not yet fetched — skip silently.
			continue
		}

		var liveResp struct {
			Elements map[string]struct {
				Stats struct {
					Minutes     int     `json:"minutes"`
					TotalPoints int     `json:"total_points"`
					GoalsScored int     `json:"goals_scored"`
					Assists     int     `json:"assists"`
					CleanSheets int     `json:"clean_sheets"`
					BPS         int     `json:"bps"`
					XG          string  `json:"expected_goals"`
					XA          string  `json:"expected_assists"`
				} `json:"stats"`
			} `json:"elements"`
		}
		if err := json.Unmarshal(liveRaw, &liveResp); err != nil {
			continue
		}

		key := fmt.Sprintf("%d", elementID)
		data, found := liveResp.Elements[key]
		if !found {
			continue
		}

		s := data.Stats
		xg := parseFloat(s.XG)
		xa := parseFloat(s.XA)

		entry := PlayerGWEntry{
			Gameweek:    gw,
			Minutes:     s.Minutes,
			Points:      s.TotalPoints,
			GoalsScored: s.GoalsScored,
			Assists:     s.Assists,
			CleanSheets: s.CleanSheets,
			BPS:         s.BPS,
			XG:          xg,
			XA:          xa,
		}
		gwEntries = append(gwEntries, entry)
		totalPts += s.TotalPoints
		totalMins += s.Minutes
		gwCount++
	}

	avg := 0.0
	if gwCount > 0 {
		avg = float64(totalPts) / float64(gwCount)
	}

	return PlayerGWStatsOutput{
		ElementID:    elementID,
		PlayerName:   meta.Name,
		Team:         teamShort[meta.TeamID],
		PositionType: meta.PositionType,
		StartGW:      startGW,
		EndGW:        endGW,
		TotalPoints:  totalPts,
		AvgPoints:    avg,
		TotalMinutes: totalMins,
		Gameweeks:    gwEntries,
	}, nil
}

// parseFloat parses a string float, returning 0.0 on error.
func parseFloat(s string) float64 {
	if s == "" {
		return 0
	}
	var f float64
	fmt.Sscanf(s, "%f", &f)
	return f
}
