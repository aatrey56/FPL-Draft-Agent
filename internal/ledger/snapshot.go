package ledger

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

type EntryEventRaw struct {
	EntryHistory json.RawMessage `json:"entry_history"`
	Picks        []EntryPick     `json:"picks"`
	Subs         []EntrySub      `json:"subs"`
}

type EntryPick struct {
	Element       int  `json:"element"`
	IsCaptain     bool `json:"is_captain"`
	IsViceCaptain bool `json:"is_vice_captain"`
	Multiplier    int  `json:"multiplier"`
	Position      int  `json:"position"`
}

type EntrySub struct {
	ElementIn  int `json:"element_in"`
	ElementOut int `json:"element_out"`
	Event      int `json:"event"`
}

type EntrySnapshot struct {
	LeagueID       int             `json:"league_id"`
	EntryID        int             `json:"entry_id"`
	Gameweek       int             `json:"gameweek"`
	GeneratedAtUTC string          `json:"generated_at_utc"`
	EntryHistory   json.RawMessage `json:"entry_history"`
	Picks          []EntryPick     `json:"picks"`
	Subs           []EntrySub      `json:"subs"`
}

func BuildEntrySnapshot(leagueID int, entryID int, gw int, raw EntryEventRaw) *EntrySnapshot {
	return &EntrySnapshot{
		LeagueID:       leagueID,
		EntryID:        entryID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		EntryHistory:   raw.EntryHistory,
		Picks:          raw.Picks,
		Subs:           raw.Subs,
	}
}

func WriteEntrySnapshot(path string, snapshot *EntrySnapshot) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}
