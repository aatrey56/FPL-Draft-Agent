package points

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
)

// makeSnap builds a minimal EntrySnapshot from a slice of (element, position, multiplier) triples.
func makeSnap(picks ...struct{ elem, pos, mult int }) *ledger.EntrySnapshot {
	ps := make([]ledger.EntryPick, 0, len(picks))
	for _, p := range picks {
		ps = append(ps, ledger.EntryPick{
			Element:    p.elem,
			Position:   p.pos,
			Multiplier: p.mult,
		})
	}
	return &ledger.EntrySnapshot{Picks: ps}
}

func TestBuildResult_BasicPoints(t *testing.T) {
	snap := makeSnap(
		struct{ elem, pos, mult int }{10, 1, 1},
		struct{ elem, pos, mult int }{20, 2, 1},
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

func TestBuildResult_CaptainDoubles(t *testing.T) {
	// Captain has multiplier 2 — his points should be doubled.
	snap := makeSnap(
		struct{ elem, pos, mult int }{10, 1, 2}, // captain
		struct{ elem, pos, mult int }{20, 2, 1},
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 6},
		20: {Minutes: 90, TotalPoints: 3},
	}

	r := BuildResult(1, 1, 1, snap, live)

	// Captain: 6*2=12, other: 3*1=3, total=15
	if r.TotalPoints != 15 {
		t.Errorf("TotalPoints = %d, want 15 (captain doubled)", r.TotalPoints)
	}
	// Verify the captain's individual entry
	for _, p := range r.Players {
		if p.Element == 10 && p.Total != 12 {
			t.Errorf("Captain Total = %d, want 12", p.Total)
		}
	}
}

func TestBuildResult_BenchPlayersExcluded(t *testing.T) {
	// Positions 12–15 are bench and must not contribute to total.
	snap := makeSnap(
		struct{ elem, pos, mult int }{10, 1, 1},
		struct{ elem, pos, mult int }{99, 12, 1}, // bench — excluded
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
	// If no live stats exist for a player, treat as 0 points.
	snap := makeSnap(
		struct{ elem, pos, mult int }{10, 1, 1},
		struct{ elem, pos, mult int }{20, 2, 1},
	)
	live := map[int]LiveStats{
		10: {Minutes: 90, TotalPoints: 5},
		// 20 is absent — should default to 0
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

func TestBuildResult_CaptainZeroPoints(t *testing.T) {
	// Captain scores 0 — doubled 0 is still 0.
	snap := makeSnap(
		struct{ elem, pos, mult int }{10, 1, 2}, // captain
	)
	live := map[int]LiveStats{
		10: {Minutes: 0, TotalPoints: 0},
	}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 0 {
		t.Errorf("TotalPoints = %d, want 0 (captain 0 * 2 = 0)", r.TotalPoints)
	}
}

func TestBuildResult_FieldsPopulated(t *testing.T) {
	snap := makeSnap(struct{ elem, pos, mult int }{10, 1, 1})
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

func TestBuildResult_AllPositions11Included(t *testing.T) {
	// Position == 11 is still a starter and must be included.
	snap := makeSnap(struct{ elem, pos, mult int }{11, 11, 1})
	live := map[int]LiveStats{11: {Minutes: 90, TotalPoints: 3}}

	r := BuildResult(1, 1, 1, snap, live)

	if r.TotalPoints != 3 {
		t.Errorf("TotalPoints = %d, want 3 (position 11 is a starter)", r.TotalPoints)
	}
}

func TestWriteResult(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sub", "result.json")

	snap := makeSnap(struct{ elem, pos, mult int }{10, 1, 1})
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
	if !strings.HasSuffix(strings.TrimRight(content, "\n"), "}") {
		t.Error("output JSON should end with }")
	}
}
