package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// DraftPicksArgs are the input arguments for the draft_picks tool.
type DraftPicksArgs struct {
	LeagueID  int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID   *int    `json:"entry_id,omitempty" jsonschema:"Filter by entry id (0 = all teams)"`
	EntryName *string `json:"entry_name,omitempty" jsonschema:"Filter by entry name (if entry_id not provided)"`
}

// DraftPickInfo describes a single draft pick.
type DraftPickInfo struct {
	Round        int    `json:"round"`
	Pick         int    `json:"pick"`
	OverallIndex int    `json:"overall_index"`
	EntryID      int    `json:"entry_id"`
	EntryName    string `json:"entry_name"`
	Element      int    `json:"element"`
	PlayerName   string `json:"player_name"`
	Team         string `json:"team"`
	PositionType int    `json:"position_type"`
	WasAuto      bool   `json:"was_auto"`
}

// DraftPicksOutput is the output of the draft_picks tool.
type DraftPicksOutput struct {
	LeagueID   int             `json:"league_id"`
	TotalPicks int             `json:"total_picks"`
	FilteredBy string          `json:"filtered_by,omitempty"`
	Picks      []DraftPickInfo `json:"picks"`
}

func buildDraftPicks(cfg ServerConfig, args DraftPicksArgs) (DraftPicksOutput, error) {
	if args.LeagueID == 0 {
		return DraftPicksOutput{}, fmt.Errorf("league_id is required")
	}

	// Load draft choices.
	choicesPath := filepath.Join(cfg.RawRoot, fmt.Sprintf("draft/%d/choices.json", args.LeagueID))
	choicesRaw, err := os.ReadFile(choicesPath)
	if err != nil {
		return DraftPicksOutput{}, fmt.Errorf("draft choices not found for league %d: %w", args.LeagueID, err)
	}
	var resp struct {
		Choices []struct {
			Entry      int    `json:"entry"`
			EntryName  string `json:"entry_name"`
			Element    int    `json:"element"`
			Round      int    `json:"round"`
			Pick       int    `json:"pick"`
			Index      int    `json:"index"`
			ChoiceTime string `json:"choice_time"`
			WasAuto    bool   `json:"was_auto"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(choicesRaw, &resp); err != nil {
		return DraftPicksOutput{}, err
	}

	// Resolve optional entry filter.
	filterEntryID := 0
	if args.EntryID != nil {
		filterEntryID = *args.EntryID
	}
	filterLabel := ""
	if filterEntryID == 0 && args.EntryName != nil {
		name := strings.TrimSpace(*args.EntryName)
		if name != "" {
			// Look up entry id from the choices themselves.
			norm := strings.ToLower(name)
			for _, c := range resp.Choices {
				if strings.ToLower(c.EntryName) == norm {
					filterEntryID = c.Entry
					filterLabel = c.EntryName
					break
				}
			}
			if filterEntryID == 0 {
				return DraftPicksOutput{}, fmt.Errorf("no entry found for name: %s", name)
			}
		}
	}
	if filterEntryID != 0 && filterLabel == "" {
		for _, c := range resp.Choices {
			if c.Entry == filterEntryID {
				filterLabel = c.EntryName
				break
			}
		}
	}

	// Build player metadata map from bootstrap.
	elements, teamShort, _, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return DraftPicksOutput{}, err
	}
	playerByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		playerByID[e.ID] = e
	}

	// Sort choices by overall draft index.
	sort.Slice(resp.Choices, func(i, j int) bool {
		return resp.Choices[i].Index < resp.Choices[j].Index
	})

	picks := make([]DraftPickInfo, 0, len(resp.Choices))
	for _, c := range resp.Choices {
		if filterEntryID != 0 && c.Entry != filterEntryID {
			continue
		}
		meta := playerByID[c.Element]
		picks = append(picks, DraftPickInfo{
			Round:        c.Round,
			Pick:         c.Pick,
			OverallIndex: c.Index,
			EntryID:      c.Entry,
			EntryName:    c.EntryName,
			Element:      c.Element,
			PlayerName:   meta.Name,
			Team:         teamShort[meta.TeamID],
			PositionType: meta.PositionType,
			WasAuto:      c.WasAuto,
		})
	}

	return DraftPicksOutput{
		LeagueID:   args.LeagueID,
		TotalPicks: len(picks),
		FilteredBy: filterLabel,
		Picks:      picks,
	}, nil
}
