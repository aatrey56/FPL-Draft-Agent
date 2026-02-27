package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
)

// Team holds bootstrap team metadata used by EPL tools.
type Team struct {
	ID        int    `json:"id"`
	Name      string `json:"name"`
	ShortName string `json:"short_name"`
}

// rawFixture is the subset of fields from a live.json fixture entry
// needed by the EPL tools.  Score pointers are nil when a match has
// not started.
type rawFixture struct {
	ID     int  `json:"id"`
	Event  int  `json:"event"`
	TeamH  int  `json:"team_h"`
	TeamA  int  `json:"team_a"`
	TeamHS *int `json:"team_h_score"`
	TeamAS *int `json:"team_a_score"`

	Finished bool `json:"finished"`
	Started  bool `json:"started"`
}

// gwLiveFixturesOnly is a lightweight partial parse of gw/{gw}/live.json
// that decodes only the fixtures array (skipping the 600+ element stats).
type gwLiveFixturesOnly struct {
	Fixtures []rawFixture `json:"fixtures"`
}

// loadTeams reads bootstrap-static.json and returns a team-ID → Team map.
func loadTeams(dataDir string) (map[int]Team, error) {
	path := filepath.Join(dataDir, "bootstrap", "bootstrap-static.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("bootstrap-static.json: %w", err)
	}
	var resp struct {
		Teams []Team `json:"teams"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("parse bootstrap teams: %w", err)
	}
	out := make(map[int]Team, len(resp.Teams))
	for _, t := range resp.Teams {
		out[t.ID] = t
	}
	return out, nil
}

// loadFixtureResults loads just the fixtures array from gw/{gw}/live.json.
func loadFixtureResults(dataDir string, gw int) ([]rawFixture, error) {
	path := filepath.Join(dataDir, "gw", strconv.Itoa(gw), "live.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var data gwLiveFixturesOnly
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, fmt.Errorf("parse gw/%d/live.json fixtures: %w", gw, err)
	}
	return data.Fixtures, nil
}
