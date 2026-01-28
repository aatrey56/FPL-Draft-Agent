package ledger

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"time"

	"fpl-draft-mcp/internal/model"
)

type DraftChoicesResponse struct {
	Choices []DraftChoice `json:"choices"`
}

type DraftChoice struct {
	Entry      int    `json:"entry"`
	EntryName  string `json:"entry_name"`
	Element    int    `json:"element"`
	Round      int    `json:"round"`
	Pick       int    `json:"pick"`
	Index      int    `json:"index"`
	ChoiceTime string `json:"choice_time"`
	WasAuto    bool   `json:"was_auto"`
	League     int    `json:"league"`
}

func BuildDraftLedger(leagueID int, choices []DraftChoice) *model.DraftLedger {
	sort.Slice(choices, func(i, j int) bool {
		return choices[i].Index < choices[j].Index
	})

	managerNameBy := make(map[int]string)
	squadBy := make(map[int][]int)
	picks := make([]model.DraftPick, 0, len(choices))

	for _, c := range choices {
		managerNameBy[c.Entry] = c.EntryName
		squadBy[c.Entry] = append(squadBy[c.Entry], c.Element)

		picks = append(picks, model.DraftPick{
			EntryID:    c.Entry,
			EntryName:  c.EntryName,
			Element:    c.Element,
			Round:      c.Round,
			Pick:       c.Pick,
			Index:      c.Index,
			ChoiceTime: c.ChoiceTime,
			WasAuto:    c.WasAuto,
		})
	}

	managers := make([]model.Manager, 0, len(managerNameBy))
	for entryID, name := range managerNameBy {
		managers = append(managers, model.Manager{
			EntryID: entryID,
			Name:    name,
		})
	}
	sort.Slice(managers, func(i, j int) bool {
		return managers[i].EntryID < managers[j].EntryID
	})

	squads := make([]model.Squad, 0, len(squadBy))
	for entryID, players := range squadBy {
		squads = append(squads, model.Squad{
			EntryID:   entryID,
			PlayerIDs: players,
		})
	}
	sort.Slice(squads, func(i, j int) bool {
		return squads[i].EntryID < squads[j].EntryID
	})

	return &model.DraftLedger{
		LeagueID:       leagueID,
		Event:          0,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Managers:       managers,
		Squads:         squads,
		Picks:          picks,
	}
}

func WriteDraftLedger(path string, ledger *model.DraftLedger) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(ledger, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}
