package ledger

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------------------
// BuildDraftLedger
// ---------------------------------------------------------------------------

func TestBuildDraftLedger_Empty(t *testing.T) {
	ledger := BuildDraftLedger(1, nil)

	if ledger.LeagueID != 1 {
		t.Errorf("LeagueID = %d, want 1", ledger.LeagueID)
	}
	if len(ledger.Managers) != 0 {
		t.Errorf("Managers len = %d, want 0", len(ledger.Managers))
	}
	if len(ledger.Squads) != 0 {
		t.Errorf("Squads len = %d, want 0", len(ledger.Squads))
	}
	if len(ledger.Picks) != 0 {
		t.Errorf("Picks len = %d, want 0", len(ledger.Picks))
	}
}

func TestBuildDraftLedger_SortsByIndex(t *testing.T) {
	choices := []DraftChoice{
		{Entry: 1, EntryName: "Alpha", Element: 10, Round: 1, Pick: 1, Index: 3},
		{Entry: 2, EntryName: "Beta", Element: 20, Round: 1, Pick: 2, Index: 1},
		{Entry: 1, EntryName: "Alpha", Element: 30, Round: 2, Pick: 3, Index: 5},
		{Entry: 2, EntryName: "Beta", Element: 40, Round: 2, Pick: 4, Index: 2},
	}

	l := BuildDraftLedger(10, choices)

	// Picks must be in ascending Index order.
	for i := 1; i < len(l.Picks); i++ {
		if l.Picks[i].Index < l.Picks[i-1].Index {
			t.Errorf("Picks not sorted by index at position %d: %d < %d",
				i, l.Picks[i].Index, l.Picks[i-1].Index)
		}
	}
}

func TestBuildDraftLedger_ManagersDeduplicatedAndSorted(t *testing.T) {
	choices := []DraftChoice{
		{Entry: 5, EntryName: "Five", Element: 50, Index: 1},
		{Entry: 3, EntryName: "Three", Element: 30, Index: 2},
		{Entry: 5, EntryName: "Five", Element: 55, Index: 3}, // duplicate entry
	}

	l := BuildDraftLedger(1, choices)

	if len(l.Managers) != 2 {
		t.Errorf("Managers len = %d, want 2 (duplicate entry deduplicated)", len(l.Managers))
	}
	// Sorted by EntryID ascending.
	if l.Managers[0].EntryID != 3 || l.Managers[1].EntryID != 5 {
		t.Errorf("Managers not sorted: got %v %v", l.Managers[0], l.Managers[1])
	}
}

func TestBuildDraftLedger_SquadsAggregatePerEntry(t *testing.T) {
	choices := []DraftChoice{
		{Entry: 1, EntryName: "A", Element: 10, Index: 1},
		{Entry: 2, EntryName: "B", Element: 20, Index: 2},
		{Entry: 1, EntryName: "A", Element: 30, Index: 3},
	}

	l := BuildDraftLedger(1, choices)

	squadByEntry := make(map[int][]int)
	for _, s := range l.Squads {
		squadByEntry[s.EntryID] = s.PlayerIDs
	}

	if len(squadByEntry[1]) != 2 {
		t.Errorf("Entry 1 squad len = %d, want 2", len(squadByEntry[1]))
	}
	if len(squadByEntry[2]) != 1 {
		t.Errorf("Entry 2 squad len = %d, want 1", len(squadByEntry[2]))
	}
}

func TestBuildDraftLedger_SquadsSortedByEntryID(t *testing.T) {
	choices := []DraftChoice{
		{Entry: 9, EntryName: "Nine", Element: 90, Index: 1},
		{Entry: 2, EntryName: "Two", Element: 20, Index: 2},
	}

	l := BuildDraftLedger(1, choices)

	if l.Squads[0].EntryID != 2 || l.Squads[1].EntryID != 9 {
		t.Errorf("Squads not sorted by EntryID: %v", l.Squads)
	}
}

func TestBuildDraftLedger_PicksPreserveFields(t *testing.T) {
	choices := []DraftChoice{
		{Entry: 1, EntryName: "A", Element: 10, Round: 2, Pick: 7, Index: 4,
			ChoiceTime: "2024-08-01T10:00:00Z", WasAuto: true, League: 99},
	}

	l := BuildDraftLedger(99, choices)

	if len(l.Picks) != 1 {
		t.Fatalf("Picks len = %d, want 1", len(l.Picks))
	}
	p := l.Picks[0]
	if p.EntryID != 1 || p.Element != 10 || p.Round != 2 ||
		p.Pick != 7 || p.Index != 4 || p.ChoiceTime != "2024-08-01T10:00:00Z" || !p.WasAuto {
		t.Errorf("Pick fields not preserved: %+v", p)
	}
}

func TestBuildDraftLedger_GeneratedAtUTCIsRFC3339(t *testing.T) {
	l := BuildDraftLedger(1, nil)
	if _, err := time.Parse(time.RFC3339, l.GeneratedAtUTC); err != nil {
		t.Errorf("GeneratedAtUTC %q is not RFC3339: %v", l.GeneratedAtUTC, err)
	}
}

func TestWriteDraftLedger(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sub", "ledger.json")

	choices := []DraftChoice{
		{Entry: 1, EntryName: "A", Element: 10, Index: 1},
	}
	l := BuildDraftLedger(1, choices)

	if err := WriteDraftLedger(path, l); err != nil {
		t.Fatalf("WriteDraftLedger error: %v", err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile error: %v", err)
	}
	if !strings.Contains(string(b), `"league_id"`) {
		t.Error("output missing league_id key")
	}
}

// ---------------------------------------------------------------------------
// BuildEntrySnapshot
// ---------------------------------------------------------------------------

func TestBuildEntrySnapshot_FieldsPreserved(t *testing.T) {
	// FPL Draft has no captain mechanic — EntryPick only carries Element and Position.
	raw := EntryEventRaw{
		EntryHistory: json.RawMessage(`{"total_points":120}`),
		Picks: []EntryPick{
			{Element: 5, Position: 1},
			{Element: 9, Position: 3},
		},
		Subs: []EntrySub{
			{ElementIn: 7, ElementOut: 5, Event: 3},
		},
	}

	snap := BuildEntrySnapshot(10, 20, 3, raw)

	if snap.LeagueID != 10 || snap.EntryID != 20 || snap.Gameweek != 3 {
		t.Errorf("IDs not propagated: league=%d entry=%d gw=%d", snap.LeagueID, snap.EntryID, snap.Gameweek)
	}
	if len(snap.Picks) != 2 {
		t.Errorf("Picks len = %d, want 2", len(snap.Picks))
	}
	if len(snap.Subs) != 1 {
		t.Errorf("Subs len = %d, want 1", len(snap.Subs))
	}
	if string(snap.EntryHistory) != `{"total_points":120}` {
		t.Errorf("EntryHistory not preserved: %s", snap.EntryHistory)
	}
}

func TestBuildEntrySnapshot_GeneratedAtUTCIsRFC3339(t *testing.T) {
	snap := BuildEntrySnapshot(1, 1, 1, EntryEventRaw{})
	if _, err := time.Parse(time.RFC3339, snap.GeneratedAtUTC); err != nil {
		t.Errorf("GeneratedAtUTC %q is not RFC3339: %v", snap.GeneratedAtUTC, err)
	}
}

func TestBuildEntrySnapshot_EmptyPicksAndSubs(t *testing.T) {
	snap := BuildEntrySnapshot(1, 1, 1, EntryEventRaw{})
	if snap.Picks != nil {
		t.Errorf("Picks should be nil for empty raw, got %v", snap.Picks)
	}
	if snap.Subs != nil {
		t.Errorf("Subs should be nil for empty raw, got %v", snap.Subs)
	}
}

func TestWriteEntrySnapshot(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "nested", "snapshot.json")

	raw := EntryEventRaw{
		Picks: []EntryPick{{Element: 1, Position: 1}},
	}
	snap := BuildEntrySnapshot(1, 2, 3, raw)

	if err := WriteEntrySnapshot(path, snap); err != nil {
		t.Fatalf("WriteEntrySnapshot error: %v", err)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile error: %v", err)
	}
	if !strings.Contains(string(b), `"entry_id"`) {
		t.Error("output missing entry_id key")
	}
}
