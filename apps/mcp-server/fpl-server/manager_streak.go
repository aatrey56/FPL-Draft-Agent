package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type ManagerStreakArgs struct {
	LeagueID  int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID   *int    `json:"entry_id,omitempty" jsonschema:"Entry id"`
	EntryName *string `json:"entry_name,omitempty" jsonschema:"Entry name (if entry_id not provided)"`
	First     *string `json:"first,omitempty" jsonschema:"First name (optional helper)"`
	Last      *string `json:"last,omitempty" jsonschema:"Last name (optional helper)"`
	StartGW   *int    `json:"start_gw,omitempty" jsonschema:"Start gameweek (default 1)"`
	EndGW     *int    `json:"end_gw,omitempty" jsonschema:"End gameweek (default latest finished)"`
}

type ManagerStreakOutput struct {
	LeagueID         int    `json:"league_id"`
	EntryID          int    `json:"entry_id"`
	EntryName        string `json:"entry_name"`
	StartGW          int    `json:"start_gw"`
	EndGW            int    `json:"end_gw"`
	StartWinStreak   int    `json:"start_win_streak"`
	CurrentWinStreak int    `json:"current_win_streak"`
	MaxWinStreak     int    `json:"max_win_streak"`
}

func buildManagerStreak(cfg ServerConfig, args ManagerStreakArgs) (ManagerStreakOutput, error) {
	if args.LeagueID == 0 {
		return ManagerStreakOutput{}, fmt.Errorf("league_id is required")
	}
	path := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	raw, err := os.ReadFile(path)
	if err != nil {
		return ManagerStreakOutput{}, err
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(raw, &details); err != nil {
		return ManagerStreakOutput{}, err
	}

	entryID := 0
	if args.EntryID != nil {
		entryID = *args.EntryID
	}
	entryName := ""
	leagueEntryID := 0

	entryByLeague := make(map[int]int)
	nameByEntry := make(map[int]string)
	leagueEntryByEntry := make(map[int]int)
	for _, e := range details.LeagueEntries {
		entryByLeague[e.ID] = e.EntryID
		nameByEntry[e.EntryID] = e.EntryName
		leagueEntryByEntry[e.EntryID] = e.ID
	}

	if entryID == 0 {
		name := ""
		if args.EntryName != nil {
			name = strings.TrimSpace(*args.EntryName)
		} else {
			first := ""
			last := ""
			if args.First != nil {
				first = strings.TrimSpace(*args.First)
			}
			if args.Last != nil {
				last = strings.TrimSpace(*args.Last)
			}
			name = strings.TrimSpace(strings.Join([]string{first, last}, " "))
		}
		if name == "" {
			return ManagerStreakOutput{}, fmt.Errorf("entry_id or entry_name is required")
		}
		matches := make([]int, 0)
		for _, e := range details.LeagueEntries {
			if strings.EqualFold(e.EntryName, name) || strings.EqualFold(e.ShortName, name) {
				matches = append(matches, e.EntryID)
			}
		}
		if len(matches) == 0 {
			return ManagerStreakOutput{}, fmt.Errorf("no entry found for name: %s", name)
		}
		if len(matches) > 1 {
			return ManagerStreakOutput{}, fmt.Errorf("ambiguous entry_name: %s", name)
		}
		entryID = matches[0]
	}

	leagueEntryID = leagueEntryByEntry[entryID]
	entryName = nameByEntry[entryID]
	if leagueEntryID == 0 {
		return ManagerStreakOutput{}, fmt.Errorf("entry not found: %d", entryID)
	}

	startGW := 1
	if args.StartGW != nil && *args.StartGW > 0 {
		startGW = *args.StartGW
	}

	finishedMax := 0
	maxEvent := 0
	for _, m := range details.Matches {
		if m.Event > maxEvent {
			maxEvent = m.Event
		}
		if m.Finished && m.Event > finishedMax {
			finishedMax = m.Event
		}
	}
	endGW := finishedMax
	if endGW == 0 {
		endGW = maxEvent
	}
	if args.EndGW != nil && *args.EndGW > 0 {
		endGW = *args.EndGW
	}
	if endGW < startGW {
		endGW = startGW
	}

	resultByGW := make(map[int]string)
	finishedByGW := make(map[int]bool)
	for _, m := range details.Matches {
		if m.Event < startGW || m.Event > endGW {
			continue
		}
		if m.LeagueEntry1 != leagueEntryID && m.LeagueEntry2 != leagueEntryID {
			continue
		}
		var scoreFor int
		var scoreAgainst int
		if m.LeagueEntry1 == leagueEntryID {
			scoreFor = m.LeagueEntry1Points
			scoreAgainst = m.LeagueEntry2Points
		} else {
			scoreFor = m.LeagueEntry2Points
			scoreAgainst = m.LeagueEntry1Points
		}
		resultByGW[m.Event] = resultFromScore(scoreFor, scoreAgainst)
		finishedByGW[m.Event] = m.Finished
	}

	startStreak := 0
	for gw := startGW; gw <= endGW; gw++ {
		if !finishedByGW[gw] {
			break
		}
		if resultByGW[gw] == "W" {
			startStreak++
		} else {
			break
		}
	}

	currentStreak := 0
	for gw := endGW; gw >= startGW; gw-- {
		if !finishedByGW[gw] {
			continue
		}
		if resultByGW[gw] == "W" {
			currentStreak++
		} else {
			break
		}
	}

	maxStreak := 0
	run := 0
	for gw := startGW; gw <= endGW; gw++ {
		if !finishedByGW[gw] {
			continue
		}
		if resultByGW[gw] == "W" {
			run++
			if run > maxStreak {
				maxStreak = run
			}
		} else {
			run = 0
		}
	}

	return ManagerStreakOutput{
		LeagueID:         args.LeagueID,
		EntryID:          entryID,
		EntryName:        entryName,
		StartGW:          startGW,
		EndGW:            endGW,
		StartWinStreak:   startStreak,
		CurrentWinStreak: currentStreak,
		MaxWinStreak:     maxStreak,
	}, nil
}
