package points

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
)

// makeSnap builds a minimal EntrySnapshot from (element, position) pairs.
// FPL Draft has no captain mechanic — every player scores raw points.
func makeSnap(picks ...struct{ elem, pos int }) *ledger.EntrySnapshot {
	ps := make([]ledger.EntryPick, 0, len(picks))
	for _, p := range picks {
		ps = append(ps, ledger.EntryPick{Element: p.elem, Position: p.pos})
	}
	return &ledger.EntrySnapshot{Picks: ps}
}

func TestBuildResult_BasicPoints(t *testing.T) {
	snap := makeSnap(
		struct{ elem, pos int }{10, 1},
		struct{ elem, pos int }{20, 2},
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 6},
		20: {Minutes: 90, TotalPoints: 4},
	}

	r := BuildResult(1, 2, 3, snap, live)

	if r.TotalPoints != 10 {
		t.Errorf("TotalPoints = %d, want 10", r.TotalPoints)
	}
	if len(r.Players) != 2 {
		t.Errorf("Players len = %d, want 2", len(r.Players))
	}
}

func TestBuildResult_RawPointsOnly(t *testing.T) {
	// FPL Draft has no captain bonus — every player scores exactly what they scored.
	snap := makeSnap(
		struct{ elem, pos int }{10, 1},
		struct{ elem, pos int }{20, 2},
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 6},
		20: {Minutes: 90, TotalPoints: 3},
	}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 9 {
		t.Errorf("TotalPoints = %d, want 9 (no captain doubling in draft)", r.TotalPoints)
	}
	for _, p := range r.Players {
		if p.Element == 10 && p.Points != 6 {
			t.Errorf("player 10 Points = %d, want 6 (raw, undoubled)", p.Points)
		}
	}
}

func TestBuildResult_BenchPlayersExcluded(t *testing.T) {
	// Positions 12–15 are bench and must not contribute to total.
	snap := makeSnap(
		struct{ elem, pos int }{10, 1},
		struct{ elem, pos int }{99, 12}, // bench — excluded
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 6},
		99: {Minutes: 90, TotalPoints: 8}, // bench player scored big — must not count
	}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 6 {
		t.Errorf("TotalPoints = %d, want 6 (bench excluded)", r.TotalPoints)
	}
	if len(r.Players) != 1 {
		t.Errorf("Players len = %d, want 1 (only starters)", len(r.Players))
	}
}

func TestBuildResult_MissingLiveStats(t *testing.T) {
	// If no live stats exist for a player, treat as 0 points — no panic.
	snap := makeSnap(
		struct{ elem, pos int }{10, 1},
		struct{ elem, pos int }{20, 2},
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 5},
		// 20 absent — defaults to 0
	}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 5 {
		t.Errorf("TotalPoints = %d, want 5 (missing stats = 0)", r.TotalPoints)
	}
}

func TestBuildResult_EmptyPicks(t *testing.T) {
	snap := &ledger.EntrySnapshot{Picks: []ledger.EntryPick{}}
	r := BuildResult(1, 1, 1, snap, map[int]LiveStats{})

	if r.TotalPoints != 0 {
		t.Errorf("TotalPoints = %d, want 0 for empty picks", r.TotalPoints)
	}
	if len(r.Players) != 0 {
		t.Errorf("Players len = %d, want 0", len(r.Players))
	}
}

func TestBuildResult_ZeroPointsPlayer(t *testing.T) {
	// A starter who scored 0 should contribute 0, not be skipped.
	snap := makeSnap(
		struct{ elem, pos int }{10, 1},
	)
	live := map[int]LiveStats{
		10: {Minutes: 0, TotalPoints: 0},
	}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 0 {
		t.Errorf("TotalPoints = %d, want 0 (player scored 0)", r.TotalPoints)
	}
}

func TestBuildResult_FieldsPopulated(t *testing.T) {
	snap := makeSnap(struct{ elem, pos int }{10, 1})
	live := map[int]LiveStats{10: {Minutes: 45, TotalPoints: 2}}

	r := BuildResult(42, 99, 7, snap, live)

	if r.LeagueID != 42 {
		t.Errorf("LeagueID = %d, want 42", r.LeagueID)
	}
	if r.EntryID != 99 {
		t.Errorf("EntryID = %d, want 99", r.EntryID)
	}
	if r.Gameweek != 7 {
		t.Errorf("Gameweek = %d, want 7", r.Gameweek)
	}
	if r.GeneratedAtUTC == "" {
		t.Error("GeneratedAtUTC should not be empty")
	}
}

func TestBuildResult_Position11IsStarter(t *testing.T) {
	// Position == 11 is still a starter and must be included.
	snap := makeSnap(struct{ elem, pos int }{11, 11})
	live := map[int]LiveStats{11: {Minutes: 90, TotalPoints: 3}}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 3 {
		t.Errorf("TotalPoints = %d, want 3 (position 11 is a starter)", r.TotalPoints)
	}
}

func TestWriteResult(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sub", "result.json")

	snap := makeSnap(struct{ elem, pos int }{10, 1})
	live := map[int]LiveStats{10: {TotalPoints: 5}}
	r := BuildResult(1, 1, 1, snap, live)

	if err := WriteResult(path, r); err != nil {
		t.Fatalf("WriteResult error: %v", err)
	}

	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("ReadFile error: %v", err)
	}
	content := string(b)
	if !strings.Contains(content, `"total_points"`) {
		t.Error("output JSON missing total_points key")
	}
	if strings.Contains(content, `"multiplier"`) {
		t.Error("output JSON must not contain multiplier — no captain mechanic in draft")
	}
}
