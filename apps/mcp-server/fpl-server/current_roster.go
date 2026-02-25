package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// CurrentRosterArgs are the input arguments for the current_roster tool.
type CurrentRosterArgs struct {
	LeagueID  int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID   *int    `json:"entry_id,omitempty" jsonschema:"Entry id"`
	EntryName *string `json:"entry_name,omitempty" jsonschema:"Entry name (if entry_id not provided)"`
	GW        *int    `json:"gw,omitempty" jsonschema:"Gameweek (0 = current)"`
}

// RosterPlayerInfo describes a single player on a manager's roster.
// FPL Draft has no captain mechanic, so is_captain/is_vice_captain are omitted.
type RosterPlayerInfo struct {
	Element      int    `json:"element"`
	Name         string `json:"name"`
	Team         string `json:"team"`
	PositionType int    `json:"position_type"`
	PositionSlot int    `json:"position_slot"`
	OnBench      bool   `json:"on_bench"`
}

// CurrentRosterOutput is the output of the current_roster tool.
type CurrentRosterOutput struct {
	LeagueID  int                `json:"league_id"`
	EntryID   int                `json:"entry_id"`
	EntryName string             `json:"entry_name"`
	Gameweek  int                `json:"gameweek"`
	Starters  []RosterPlayerInfo `json:"starters"`
	Bench     []RosterPlayerInfo `json:"bench"`
}

func buildCurrentRoster(cfg ServerConfig, args CurrentRosterArgs) (CurrentRosterOutput, error) {
	if args.LeagueID == 0 {
		return CurrentRosterOutput{}, fmt.Errorf("league_id is required")
	}

	// Resolve gameweek.
	gw := 0
	if args.GW != nil {
		gw = *args.GW
	}
	resolvedGW, err := resolveGW(cfg, gw)
	if err != nil {
		return CurrentRosterOutput{}, err
	}

	// Load league details to resolve entry name → entry id.
	detailsPath := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	detailsRaw, err := os.ReadFile(detailsPath)
	if err != nil {
		return CurrentRosterOutput{}, fmt.Errorf("league details not found: %w", err)
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(detailsRaw, &details); err != nil {
		return CurrentRosterOutput{}, err
	}

	nameByEntry := make(map[int]string)
	for _, e := range details.LeagueEntries {
		nameByEntry[e.EntryID] = e.EntryName
	}

	entryID := 0
	if args.EntryID != nil {
		entryID = *args.EntryID
	}
	if entryID == 0 {
		name := ""
		if args.EntryName != nil {
			name = strings.TrimSpace(*args.EntryName)
		}
		if name == "" {
			return CurrentRosterOutput{}, fmt.Errorf("entry_id or entry_name is required")
		}
		for _, e := range details.LeagueEntries {
			if strings.EqualFold(e.EntryName, name) || strings.EqualFold(e.ShortName, name) {
				entryID = e.EntryID
				break
			}
		}
		if entryID == 0 {
			return CurrentRosterOutput{}, fmt.Errorf("no entry found for name: %s", name)
		}
	}

	entryName := nameByEntry[entryID]
	if entryName == "" {
		return CurrentRosterOutput{}, fmt.Errorf("entry not found: %d", entryID)
	}

	// Load the entry snapshot for this gameweek.
	snapPath := filepath.Join(cfg.RawRoot, fmt.Sprintf("entry/%d/gw/%d.json", entryID, resolvedGW))
	snapRaw, err := os.ReadFile(snapPath)
	if err != nil {
		return CurrentRosterOutput{}, fmt.Errorf("roster snapshot not available for entry %d GW%d: %w", entryID, resolvedGW, err)
	}
	var snap struct {
		Picks []struct {
			Element  int `json:"element"`
			Position int `json:"position"`
		} `json:"picks"`
	}
	if err := json.Unmarshal(snapRaw, &snap); err != nil {
		return CurrentRosterOutput{}, err
	}

	// Build player metadata map from bootstrap.
	elements, teamShort, _, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return CurrentRosterOutput{}, err
	}
	playerByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		playerByID[e.ID] = e
	}

	starters := make([]RosterPlayerInfo, 0, 11)
	bench := make([]RosterPlayerInfo, 0, 4)
	for _, p := range snap.Picks {
		// Guard: skip picks referencing an element absent from the bootstrap
		// (e.g. data freshness gap, mid-season player addition).  A zero-value
		// struct would produce blank Name/Team and PositionType 0, silently
		// corrupting the roster output.
		meta, ok := playerByID[p.Element]
		if !ok {
			continue
		}
		info := RosterPlayerInfo{
			Element:      p.Element,
			Name:         meta.Name,
			Team:         teamShort[meta.TeamID],
			PositionType: meta.PositionType,
			PositionSlot: p.Position,
			OnBench:      p.Position > 11,
		}
		if p.Position <= 11 {
			starters = append(starters, info)
		} else {
			bench = append(bench, info)
		}
	}

	return CurrentRosterOutput{
		LeagueID:  args.LeagueID,
		EntryID:   entryID,
		EntryName: entryName,
		Gameweek:  resolvedGW,
		Starters:  starters,
		Bench:     bench,
	}, nil
}
