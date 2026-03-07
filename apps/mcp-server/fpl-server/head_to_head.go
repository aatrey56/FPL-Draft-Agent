package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// HeadToHeadArgs are the input arguments for the head_to_head tool.
type HeadToHeadArgs struct {
	LeagueID   int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryIDA   *int    `json:"entry_id_a,omitempty" jsonschema:"First team entry id"`
	EntryNameA *string `json:"entry_name_a,omitempty" jsonschema:"First team name (if entry_id_a not provided)"`
	EntryIDB   *int    `json:"entry_id_b,omitempty" jsonschema:"Second team entry id"`
	EntryNameB *string `json:"entry_name_b,omitempty" jsonschema:"Second team name (if entry_id_b not provided)"`
}

// H2HMatch describes a single match between the two teams.
type H2HMatch struct {
	Gameweek int    `json:"gameweek"`
	ScoreA   int    `json:"score_a"`
	ScoreB   int    `json:"score_b"`
	ResultA  string `json:"result_a"`
	Finished bool   `json:"finished"`
}

// H2HTeamRecord holds one team's record in this H2H matchup.
type H2HTeamRecord struct {
	EntryID   int    `json:"entry_id"`
	EntryName string `json:"entry_name"`
	Wins      int    `json:"wins"`
	Draws     int    `json:"draws"`
	Losses    int    `json:"losses"`
}

// HeadToHeadOutput is the output of the head_to_head tool.
type HeadToHeadOutput struct {
	LeagueID int           `json:"league_id"`
	TeamA    H2HTeamRecord `json:"team_a"`
	TeamB    H2HTeamRecord `json:"team_b"`
	Matches  []H2HMatch    `json:"matches"`
}

func buildHeadToHead(cfg ServerConfig, args HeadToHeadArgs) (HeadToHeadOutput, error) {
	if args.LeagueID == 0 {
		return HeadToHeadOutput{}, fmt.Errorf("league_id is required")
	}

	path := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	raw, err := os.ReadFile(path)
	if err != nil {
		return HeadToHeadOutput{}, err
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(raw, &details); err != nil {
		return HeadToHeadOutput{}, err
	}

	entryByLeague := make(map[int]int)
	nameByEntry := make(map[int]string)
	leagueEntryByEntry := make(map[int]int)
	for _, e := range details.LeagueEntries {
		entryByLeague[e.ID] = e.EntryID
		nameByEntry[e.EntryID] = e.EntryName
		leagueEntryByEntry[e.EntryID] = e.ID
	}

	resolveEntry := func(id *int, name *string, label string) (int, error) {
		if id != nil && *id != 0 {
			return *id, nil
		}
		if name == nil || strings.TrimSpace(*name) == "" {
			return 0, fmt.Errorf("%s: entry_id or entry_name is required", label)
		}
		n := strings.TrimSpace(*name)
		for _, e := range details.LeagueEntries {
			if strings.EqualFold(e.EntryName, n) || strings.EqualFold(e.ShortName, n) {
				return e.EntryID, nil
			}
		}
		return 0, fmt.Errorf("%s: no entry found for name: %s", label, n)
	}

	entryIDA, err := resolveEntry(args.EntryIDA, args.EntryNameA, "team_a")
	if err != nil {
		return HeadToHeadOutput{}, err
	}
	entryIDB, err := resolveEntry(args.EntryIDB, args.EntryNameB, "team_b")
	if err != nil {
		return HeadToHeadOutput{}, err
	}

	leagueEntryIDA := leagueEntryByEntry[entryIDA]
	leagueEntryIDB := leagueEntryByEntry[entryIDB]
	if leagueEntryIDA == 0 {
		return HeadToHeadOutput{}, fmt.Errorf("team_a not found: %d", entryIDA)
	}
	if leagueEntryIDB == 0 {
		return HeadToHeadOutput{}, fmt.Errorf("team_b not found: %d", entryIDB)
	}

	recordA := H2HTeamRecord{EntryID: entryIDA, EntryName: nameByEntry[entryIDA]}
	recordB := H2HTeamRecord{EntryID: entryIDB, EntryName: nameByEntry[entryIDB]}
	matches := make([]H2HMatch, 0)

	for _, m := range details.Matches {
		// Match must involve both teams.
		involvesA := m.LeagueEntry1 == leagueEntryIDA || m.LeagueEntry2 == leagueEntryIDA
		involvesB := m.LeagueEntry1 == leagueEntryIDB || m.LeagueEntry2 == leagueEntryIDB
		if !involvesA || !involvesB {
			continue
		}

		var scoreA, scoreB int
		if m.LeagueEntry1 == leagueEntryIDA {
			scoreA = m.LeagueEntry1Points
			scoreB = m.LeagueEntry2Points
		} else {
			scoreA = m.LeagueEntry2Points
			scoreB = m.LeagueEntry1Points
		}

		resultA := resultFromScore(scoreA, scoreB)

		if !m.Finished {
			continue
		}
		h2h := H2HMatch{
			Gameweek: m.Event,
			ScoreA:   scoreA,
			ScoreB:   scoreB,
			ResultA:  resultA,
			Finished: m.Finished,
		}
		matches = append(matches, h2h)

		switch resultA {
		case "W":
			recordA.Wins++
			recordB.Losses++
		case "L":
			recordA.Losses++
			recordB.Wins++
		case "D":
			recordA.Draws++
			recordB.Draws++
		}
	}

	// Sort matches chronologically.
	sort.Slice(matches, func(i, j int) bool {
		return matches[i].Gameweek < matches[j].Gameweek
	})

	return HeadToHeadOutput{
		LeagueID: args.LeagueID,
		TeamA:    recordA,
		TeamB:    recordB,
		Matches:  matches,
	}, nil
}
