package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type ManagerScheduleArgs struct {
	LeagueID  int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID   *int    `json:"entry_id,omitempty" jsonschema:"Entry id"`
	EntryName *string `json:"entry_name,omitempty" jsonschema:"Entry name (if entry_id not provided)"`
	First     *string `json:"first,omitempty" jsonschema:"First name (optional helper)"`
	Last      *string `json:"last,omitempty" jsonschema:"Last name (optional helper)"`
	GW        *int    `json:"gw,omitempty" jsonschema:"Gameweek to query (0 = auto)"`
	Horizon   *int    `json:"horizon,omitempty" jsonschema:"Number of future GWs to include when gw is set (default 1)"`
}

type ManagerScheduleEntry struct {
	Gameweek        int    `json:"gameweek"`
	OpponentEntryID int    `json:"opponent_entry_id"`
	OpponentName    string `json:"opponent_name"`
	ScoreFor        int    `json:"score_for"`
	ScoreAgainst    int    `json:"score_against"`
	Result          string `json:"result"`
	Started         bool   `json:"started"`
	Finished        bool   `json:"finished"`
}

type ManagerScheduleOutput struct {
	LeagueID  int                    `json:"league_id"`
	EntryID   int                    `json:"entry_id"`
	EntryName string                 `json:"entry_name"`
	Matches   []ManagerScheduleEntry `json:"matches"`
}

type leagueDetailsRaw struct {
	LeagueEntries []struct {
		ID        int    `json:"id"`
		EntryID   int    `json:"entry_id"`
		EntryName string `json:"entry_name"`
		ShortName string `json:"short_name"`
	} `json:"league_entries"`
	Matches []struct {
		Event              int  `json:"event"`
		Finished           bool `json:"finished"`
		Started            bool `json:"started"`
		LeagueEntry1       int  `json:"league_entry_1"`
		LeagueEntry1Points int  `json:"league_entry_1_points"`
		LeagueEntry2       int  `json:"league_entry_2"`
		LeagueEntry2Points int  `json:"league_entry_2_points"`
	} `json:"matches"`
}

func buildManagerSchedule(cfg ServerConfig, args ManagerScheduleArgs) (ManagerScheduleOutput, error) {
	if args.LeagueID == 0 {
		return ManagerScheduleOutput{}, fmt.Errorf("league_id is required")
	}

	path := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	raw, err := os.ReadFile(path)
	if err != nil {
		return ManagerScheduleOutput{}, err
	}

	var details leagueDetailsRaw
	if err := json.Unmarshal(raw, &details); err != nil {
		return ManagerScheduleOutput{}, err
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
			return ManagerScheduleOutput{}, fmt.Errorf("entry_id or entry_name is required")
		}
		matches := make([]int, 0)
		for _, e := range details.LeagueEntries {
			if strings.EqualFold(e.EntryName, name) || strings.EqualFold(e.ShortName, name) {
				matches = append(matches, e.EntryID)
			}
		}
		if len(matches) == 0 {
			return ManagerScheduleOutput{}, fmt.Errorf("no entry found for name: %s", name)
		}
		if len(matches) > 1 {
			return ManagerScheduleOutput{}, fmt.Errorf("ambiguous entry_name: %s", name)
		}
		entryID = matches[0]
	}

	leagueEntryID = leagueEntryByEntry[entryID]
	entryName = nameByEntry[entryID]
	if leagueEntryID == 0 {
		return ManagerScheduleOutput{}, fmt.Errorf("entry not found: %d", entryID)
	}

	minGW := 1
	maxGW := 38
	gw := 0
	if args.GW != nil {
		gw = *args.GW
	}
	if gw <= 0 {
		meta, err := loadGameMeta(cfg)
		if err == nil {
			if meta.CurrentEventFinished && meta.NextEvent > 0 {
				gw = meta.NextEvent
			} else if meta.CurrentEvent > 0 {
				gw = meta.CurrentEvent
			}
		}
	}

	if gw > 0 {
		minGW = gw
		h := 1
		if args.Horizon != nil && *args.Horizon > 0 {
			h = *args.Horizon
		}
		maxGW = gw + h - 1
	}

	matches := make([]ManagerScheduleEntry, 0)
	for _, m := range details.Matches {
		if m.Event < minGW || m.Event > maxGW {
			continue
		}
		if m.LeagueEntry1 != leagueEntryID && m.LeagueEntry2 != leagueEntryID {
			continue
		}
		var oppLeagueEntry int
		var scoreFor int
		var scoreAgainst int
		if m.LeagueEntry1 == leagueEntryID {
			oppLeagueEntry = m.LeagueEntry2
			scoreFor = m.LeagueEntry1Points
			scoreAgainst = m.LeagueEntry2Points
		} else {
			oppLeagueEntry = m.LeagueEntry1
			scoreFor = m.LeagueEntry2Points
			scoreAgainst = m.LeagueEntry1Points
		}
		oppEntryID := entryByLeague[oppLeagueEntry]
		oppName := nameByEntry[oppEntryID]
		matches = append(matches, ManagerScheduleEntry{
			Gameweek:        m.Event,
			OpponentEntryID: oppEntryID,
			OpponentName:    oppName,
			ScoreFor:        scoreFor,
			ScoreAgainst:    scoreAgainst,
			Result:          resultFromScore(scoreFor, scoreAgainst),
			Started:         m.Started,
			Finished:        m.Finished,
		})
	}

	return ManagerScheduleOutput{
		LeagueID:  args.LeagueID,
		EntryID:   entryID,
		EntryName: entryName,
		Matches:   matches,
	}, nil
}

func resultFromScore(forPts int, againstPts int) string {
	if forPts > againstPts {
		return "W"
	}
	if forPts < againstPts {
		return "L"
	}
	return "D"
}
