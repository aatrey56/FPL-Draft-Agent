package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ManagerSeasonArgs are the input arguments for the manager_season tool.
type ManagerSeasonArgs struct {
	LeagueID  int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID   *int    `json:"entry_id,omitempty" jsonschema:"Entry id"`
	EntryName *string `json:"entry_name,omitempty" jsonschema:"Entry name (if entry_id not provided)"`
}

// SeasonGameweek holds results for a single gameweek in a manager's season.
type SeasonGameweek struct {
	Gameweek      int    `json:"gameweek"`
	Score         int    `json:"score"`
	OpponentID    int    `json:"opponent_entry_id"`
	OpponentName  string `json:"opponent_name"`
	OpponentScore int    `json:"opponent_score"`
	Result        string `json:"result"`
	Finished      bool   `json:"finished"`
}

// SeasonRecord holds season-level W/D/L.
type SeasonRecord struct {
	Wins   int `json:"wins"`
	Draws  int `json:"draws"`
	Losses int `json:"losses"`
}

// ManagerSeasonOutput is the output of the manager_season tool.
type ManagerSeasonOutput struct {
	LeagueID    int              `json:"league_id"`
	EntryID     int              `json:"entry_id"`
	EntryName   string           `json:"entry_name"`
	Record      SeasonRecord     `json:"record"`
	TotalPoints int              `json:"total_points"`
	HighestGW   int              `json:"highest_scoring_gw"`
	HighestPts  int              `json:"highest_score"`
	LowestGW    int              `json:"lowest_scoring_gw"`
	LowestPts   int              `json:"lowest_score"`
	AvgScore    float64          `json:"avg_score"`
	Gameweeks   []SeasonGameweek `json:"gameweeks"`
}

func buildManagerSeason(cfg ServerConfig, args ManagerSeasonArgs) (ManagerSeasonOutput, error) {
	if args.LeagueID == 0 {
		return ManagerSeasonOutput{}, fmt.Errorf("league_id is required")
	}

	path := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	raw, err := os.ReadFile(path)
	if err != nil {
		return ManagerSeasonOutput{}, err
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(raw, &details); err != nil {
		return ManagerSeasonOutput{}, err
	}

	entryByLeague := make(map[int]int)
	nameByEntry := make(map[int]string)
	leagueEntryByEntry := make(map[int]int)
	for _, e := range details.LeagueEntries {
		entryByLeague[e.ID] = e.EntryID
		nameByEntry[e.EntryID] = e.EntryName
		leagueEntryByEntry[e.EntryID] = e.ID
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
			return ManagerSeasonOutput{}, fmt.Errorf("entry_id or entry_name is required")
		}
		for _, e := range details.LeagueEntries {
			if strings.EqualFold(e.EntryName, name) || strings.EqualFold(e.ShortName, name) {
				entryID = e.EntryID
				break
			}
		}
		if entryID == 0 {
			return ManagerSeasonOutput{}, fmt.Errorf("no entry found for name: %s", name)
		}
	}

	leagueEntryID := leagueEntryByEntry[entryID]
	entryName := nameByEntry[entryID]
	if leagueEntryID == 0 {
		return ManagerSeasonOutput{}, fmt.Errorf("entry not found: %d", entryID)
	}

	// Walk all matches for this entry.
	gameweeks := make([]SeasonGameweek, 0)
	record := SeasonRecord{}
	totalPts := 0
	highestGW, highestPts := 0, -1
	// math.MaxInt32 is a reliable sentinel for "not yet set"; 1<<30 would silently
	// break if a manager ever scored more than ~1 billion points.
	lowestGW, lowestPts := 0, math.MaxInt32
	finishedCount := 0

	for _, m := range details.Matches {
		if m.LeagueEntry1 != leagueEntryID && m.LeagueEntry2 != leagueEntryID {
			continue
		}

		var score, oppScore int
		var oppLeagueEntry int
		if m.LeagueEntry1 == leagueEntryID {
			score = m.LeagueEntry1Points
			oppScore = m.LeagueEntry2Points
			oppLeagueEntry = m.LeagueEntry2
		} else {
			score = m.LeagueEntry2Points
			oppScore = m.LeagueEntry1Points
			oppLeagueEntry = m.LeagueEntry1
		}

		oppEntryID := entryByLeague[oppLeagueEntry]
		oppName := nameByEntry[oppEntryID]
		result := resultFromScore(score, oppScore)

		gw := SeasonGameweek{
			Gameweek:      m.Event,
			Score:         score,
			OpponentID:    oppEntryID,
			OpponentName:  oppName,
			OpponentScore: oppScore,
			Result:        result,
			Finished:      m.Finished,
		}
		gameweeks = append(gameweeks, gw)

		if m.Finished {
			totalPts += score
			finishedCount++
			switch result {
			case "W":
				record.Wins++
			case "D":
				record.Draws++
			case "L":
				record.Losses++
			}
			if score > highestPts {
				highestPts = score
				highestGW = m.Event
			}
			if score < lowestPts {
				lowestPts = score
				lowestGW = m.Event
			}
		}
	}

	// Sort gameweeks chronologically.
	sort.Slice(gameweeks, func(i, j int) bool {
		return gameweeks[i].Gameweek < gameweeks[j].Gameweek
	})

	avg := 0.0
	if finishedCount > 0 {
		avg = float64(totalPts) / float64(finishedCount)
	}
	if highestPts == -1 {
		highestPts = 0
	}
	if lowestPts == math.MaxInt32 {
		lowestPts = 0
	}

	return ManagerSeasonOutput{
		LeagueID:    args.LeagueID,
		EntryID:     entryID,
		EntryName:   entryName,
		Record:      record,
		TotalPoints: totalPts,
		HighestGW:   highestGW,
		HighestPts:  highestPts,
		LowestGW:    lowestGW,
		LowestPts:   lowestPts,
		AvgScore:    avg,
		Gameweeks:   gameweeks,
	}, nil
}
