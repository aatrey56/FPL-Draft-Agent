package main

import (
	"testing"
)

// ---------------------------------------------------------------------------
// buildFixtureIndex — double gameweek (DGW) support
// ---------------------------------------------------------------------------

// TestBuildFixtureIndex_NormalGW verifies that each team gets exactly one
// fixture context in a normal (single-fixture) gameweek.
func TestBuildFixtureIndex_NormalGW(t *testing.T) {
	fixtures := []fixture{
		{ID: 1, Event: 25, TeamH: 10, TeamA: 20},
		{ID: 2, Event: 25, TeamH: 30, TeamA: 40},
	}
	teamShort := map[int]string{10: "ARS", 20: "CHE", 30: "LIV", 40: "MCI"}

	idx := buildFixtureIndex(fixtures, teamShort)

	for _, teamID := range []int{10, 20, 30, 40} {
		fxList, ok := idx[teamID]
		if !ok {
			t.Errorf("team %d missing from fixture index", teamID)
			continue
		}
		if len(fxList) != 1 {
			t.Errorf("team %d: expected 1 fixture, got %d", teamID, len(fxList))
		}
	}

	// Verify home / away assignment.
	if idx[10][0].Venue != "HOME" {
		t.Errorf("team 10 should be HOME, got %s", idx[10][0].Venue)
	}
	if idx[20][0].Venue != "AWAY" {
		t.Errorf("team 20 should be AWAY, got %s", idx[20][0].Venue)
	}
	if idx[10][0].OpponentID != 20 {
		t.Errorf("team 10 opponent: want 20, got %d", idx[10][0].OpponentID)
	}
}

// TestBuildFixtureIndex_DoubleGW verifies that when a team plays twice in the
// same gameweek, both fixtures are retained (not overwritten).
// Prior to the fix, buildFixtureIndex stored only the LAST fixture for a team,
// silently discarding the first one.
func TestBuildFixtureIndex_DoubleGW(t *testing.T) {
	// Arsenal (ID=10) plays twice in GW25: home vs Chelsea (20) and away at Liverpool (30).
	fixtures := []fixture{
		{ID: 1, Event: 25, TeamH: 10, TeamA: 20}, // ARS home vs CHE
		{ID: 2, Event: 25, TeamH: 30, TeamA: 10}, // LIV home; ARS away
	}
	teamShort := map[int]string{10: "ARS", 20: "CHE", 30: "LIV"}

	idx := buildFixtureIndex(fixtures, teamShort)

	arsFixtures, ok := idx[10]
	if !ok {
		t.Fatal("team 10 (ARS) missing from fixture index")
	}
	if len(arsFixtures) != 2 {
		t.Fatalf("DGW team should have 2 fixtures, got %d", len(arsFixtures))
	}

	// Verify both venues are represented.
	venues := make(map[string]bool)
	for _, fx := range arsFixtures {
		venues[fx.Venue] = true
	}
	if !venues["HOME"] || !venues["AWAY"] {
		t.Errorf("DGW fixtures should contain both HOME and AWAY; got %v", venues)
	}

	// Chelsea (only one fixture) should still have exactly 1.
	if len(idx[20]) != 1 {
		t.Errorf("CHE should have 1 fixture in this GW, got %d", len(idx[20]))
	}
}

// TestBuildFixtureIndex_EmptyFixtures verifies no panic on empty input.
func TestBuildFixtureIndex_EmptyFixtures(t *testing.T) {
	idx := buildFixtureIndex([]fixture{}, map[int]string{})
	if len(idx) != 0 {
		t.Errorf("empty fixtures should produce empty index, got %d entries", len(idx))
	}
}

// ---------------------------------------------------------------------------
// resolveRosterGW
// ---------------------------------------------------------------------------

func TestResolveRosterGW(t *testing.T) {
	tests := []struct {
		name   string
		asOf   int
		target int
		want   int
	}{
		{
			name:   "UseTargetMinusOneWhenAhead",
			asOf:   25,
			target: 27,
			want:   26,
		},
		{
			name:   "KeepAsOfWhenAlreadyCurrent",
			asOf:   26,
			target: 27,
			want:   26,
		},
		{
			name:   "KeepAsOfForEarlyTarget",
			asOf:   1,
			target: 1,
			want:   1,
		},
		{
			name:   "ClampToOne",
			asOf:   0,
			target: 1,
			want:   1,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveRosterGW(tc.asOf, tc.target)
			if got != tc.want {
				t.Fatalf("resolveRosterGW(%d, %d)=%d want %d", tc.asOf, tc.target, got, tc.want)
			}
		})
	}
}
