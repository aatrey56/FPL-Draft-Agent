package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type LeagueEntriesArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
}

type LeagueEntryInfo struct {
	EntryID       int    `json:"entry_id"`
	EntryName     string `json:"entry_name"`
	ShortName     string `json:"short_name"`
	LeagueEntryID int    `json:"league_entry_id"`
}

type LeagueEntriesOutput struct {
	LeagueID int               `json:"league_id"`
	Teams    []LeagueEntryInfo `json:"teams"`
}

func buildLeagueEntries(cfg ServerConfig, leagueID int) (LeagueEntriesOutput, error) {
	if leagueID == 0 {
		return LeagueEntriesOutput{}, fmt.Errorf("league_id is required")
	}
	path := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", leagueID))
	raw, err := os.ReadFile(path)
	if err != nil {
		return LeagueEntriesOutput{}, err
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(raw, &details); err != nil {
		return LeagueEntriesOutput{}, err
	}
	teams := make([]LeagueEntryInfo, 0, len(details.LeagueEntries))
	for _, e := range details.LeagueEntries {
		teams = append(teams, LeagueEntryInfo{
			EntryID:       e.EntryID,
			EntryName:     e.EntryName,
			ShortName:     e.ShortName,
			LeagueEntryID: e.ID,
		})
	}
	return LeagueEntriesOutput{LeagueID: leagueID, Teams: teams}, nil
}
