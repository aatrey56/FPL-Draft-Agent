package main

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
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
// computeConsistencyStats — only count GWs where the player has live data
// ---------------------------------------------------------------------------

// writeLiveJSON writes a minimal gw/<gw>/live.json into rawRoot.
func writeLiveJSON(t *testing.T, rawRoot string, gw int, elements map[string]any) {
	t.Helper()
	dir := filepath.Join(rawRoot, "gw", itoa(gw))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir gw dir: %v", err)
	}
	payload := map[string]any{"elements": elements}
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal live json: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "live.json"), b, 0o644); err != nil {
		t.Fatalf("write live json: %v", err)
	}
}

func makeStats(pts int) map[string]any {
	return map[string]any{"stats": map[string]any{"total_points": pts, "minutes": 90}}
}

// TestComputeConsistencyStats_PlayerAbsentFromGW verifies that gameweeks where
// a player has no entry in the live stats file are excluded from the average
// and standard deviation, rather than being counted as 0-point appearances.
//
// Regression test for the bug where cur.count was incremented unconditionally,
// even when the player was absent from live[e.ID], artificially deflating the
// player's mean and distorting their standard deviation.
func TestComputeConsistencyStats_PlayerAbsentFromGW(t *testing.T) {
	rawRoot := t.TempDir()

	// GW1: player 100 scores 10pts; player 200 absent (e.g. injured / newly added).
	writeLiveJSON(t, rawRoot, 1, map[string]any{
		"100": makeStats(10),
	})
	// GW2: player 100 scores 8pts; player 200 scores 6pts.
	writeLiveJSON(t, rawRoot, 2, map[string]any{
		"100": makeStats(8),
		"200": makeStats(6),
	})
	// GW3: player 100 scores 12pts; player 200 scores 9pts.
	writeLiveJSON(t, rawRoot, 3, map[string]any{
		"100": makeStats(12),
		"200": makeStats(9),
	})

	elements := []elementInfo{
		{ID: 100},
		{ID: 200},
	}

	avg, stddev, err := computeConsistencyStats(rawRoot, elements, 3, 3)
	if err != nil {
		t.Fatalf("computeConsistencyStats: %v", err)
	}

	// Player 100: present in all 3 GWs → avg = (10+8+12)/3 = 10.0
	const wantAvg100 = 10.0
	if math.Abs(avg[100]-wantAvg100) > 1e-9 {
		t.Errorf("player 100 avg: want %.4f, got %.4f", wantAvg100, avg[100])
	}

	// Player 200: absent GW1, present GW2+GW3 → avg = (6+9)/2 = 7.5
	// Before the fix, avg would have been (0+6+9)/3 = 5.0.
	const wantAvg200 = 7.5
	if math.Abs(avg[200]-wantAvg200) > 1e-9 {
		t.Errorf("player 200 avg: want %.4f, got %.4f (was the GW1 absence counted as 0?)", wantAvg200, avg[200])
	}

	// Player 200 stddev: population stddev of [6, 9] = sqrt(((6-7.5)² + (9-7.5)²)/2) = 1.5
	const wantStddev200 = 1.5
	if math.Abs(stddev[200]-wantStddev200) > 1e-9 {
		t.Errorf("player 200 stddev: want %.4f, got %.4f", wantStddev200, stddev[200])
	}
}

// TestComputeConsistencyStats_AllPresent verifies the happy path: when all
// players appear in every GW, the stats are calculated correctly.
func TestComputeConsistencyStats_AllPresent(t *testing.T) {
	rawRoot := t.TempDir()

	writeLiveJSON(t, rawRoot, 5, map[string]any{
		"10": makeStats(6),
		"20": makeStats(12),
	})
	writeLiveJSON(t, rawRoot, 6, map[string]any{
		"10": makeStats(10),
		"20": makeStats(4),
	})

	elements := []elementInfo{{ID: 10}, {ID: 20}}
	avg, _, err := computeConsistencyStats(rawRoot, elements, 6, 2)
	if err != nil {
		t.Fatalf("computeConsistencyStats: %v", err)
	}

	if math.Abs(avg[10]-8.0) > 1e-9 {
		t.Errorf("player 10 avg: want 8.0, got %f", avg[10])
	}
	if math.Abs(avg[20]-8.0) > 1e-9 {
		t.Errorf("player 20 avg: want 8.0, got %f", avg[20])
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

// ---------------------------------------------------------------------------
// computePointsConcededByPosition — reads fixtures from gw/N/live.json
// ---------------------------------------------------------------------------

// TestComputePointsConcededByPosition_UsesLiveFixtures is a regression test
// verifying that fixture pairings are sourced from gw/N/live.json rather than
// bootstrap-static.json. Bootstrap only contains upcoming fixtures; historical
// GW fixture data must come from each gameweek's live file.
func TestComputePointsConcededByPosition_UsesLiveFixtures(t *testing.T) {
	dir := t.TempDir()

	// Two teams: team 1 (home) vs team 2 (away).
	// Element 10 plays for team 1 (FWD, pos 4) and scores 10 pts in GW1.
	// Element 20 plays for team 2 (DEF, pos 2) and scores 6 pts in GW1.
	liveJSON := map[string]any{
		"fixtures": []any{
			map[string]any{"id": 1, "team_h": 1, "team_a": 2},
		},
		"elements": map[string]any{
			"10": map[string]any{"stats": map[string]any{
				"minutes": 90, "total_points": 10,
				"expected_goals": 0.5, "expected_assists": 0.1,
			}},
			"20": map[string]any{"stats": map[string]any{
				"minutes": 90, "total_points": 6,
				"expected_goals": 0.0, "expected_assists": 0.0,
			}},
		},
	}
	raw, _ := json.MarshalIndent(liveJSON, "", "  ")
	gwDir := filepath.Join(dir, "gw", "1")
	if err := os.MkdirAll(gwDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gwDir, "live.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}

	elements := []elementInfo{
		{ID: 10, TeamID: 1, PositionType: 4}, // FWD for team 1
		{ID: 20, TeamID: 2, PositionType: 2}, // DEF for team 2
	}

	// asOfGW=1, horizon=1 — should process exactly GW1.
	conceded := computePointsConcededByPosition(dir, elements, 1, 1)

	// Team 2 (away) conceded 10 pts from team 1's FWD (pos 4).
	awayFWD := conceded[2]["AWAY"][4]
	if awayFWD.Count != 1 || awayFWD.Sum != 10 {
		t.Errorf("team 2 AWAY FWD conceded: sum=%.0f count=%d, want sum=10 count=1", awayFWD.Sum, awayFWD.Count)
	}

	// Team 1 (home) conceded 6 pts from team 2's DEF (pos 2).
	homeDEF := conceded[1]["HOME"][2]
	if homeDEF.Count != 1 || homeDEF.Sum != 6 {
		t.Errorf("team 1 HOME DEF conceded: sum=%.0f count=%d, want sum=6 count=1", homeDEF.Sum, homeDEF.Count)
	}
}

// TestLoadFixturesFromLive verifies that fixtures embedded in a live.json file
// are correctly parsed into the fixture struct.
func TestLoadFixturesFromLive(t *testing.T) {
	dir := t.TempDir()

	liveJSON := map[string]any{
		"fixtures": []any{
			map[string]any{"id": 42, "team_h": 5, "team_a": 7},
			map[string]any{"id": 43, "team_h": 8, "team_a": 3},
		},
		"elements": map[string]any{},
	}
	raw, _ := json.MarshalIndent(liveJSON, "", "  ")
	gwDir := filepath.Join(dir, "gw", "3")
	if err := os.MkdirAll(gwDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(gwDir, "live.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}

	fixtures, err := loadFixturesFromLive(dir, 3)
	if err != nil {
		t.Fatal(err)
	}
	if len(fixtures) != 2 {
		t.Fatalf("got %d fixtures want 2", len(fixtures))
	}
	if fixtures[0].ID != 42 || fixtures[0].TeamH != 5 || fixtures[0].TeamA != 7 {
		t.Errorf("fixture[0]=%+v want {ID:42 TeamH:5 TeamA:7}", fixtures[0])
	}
	if fixtures[1].ID != 43 || fixtures[1].TeamH != 8 || fixtures[1].TeamA != 3 {
		t.Errorf("fixture[1]=%+v want {ID:43 TeamH:8 TeamA:3}", fixtures[1])
	}
	// Event should be set to the GW argument.
	for _, f := range fixtures {
		if f.Event != 3 {
			t.Errorf("fixture event=%d want 3", f.Event)
		}
	}
}

// ---------------------------------------------------------------------------
// loadBootstrapData — new expanded fields
// ---------------------------------------------------------------------------

// writeBootstrapJSON writes a minimal bootstrap-static.json into rawRoot.
func writeBootstrapJSON(t *testing.T, rawRoot string, payload map[string]any) {
	t.Helper()
	dir := filepath.Join(rawRoot, "bootstrap")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir bootstrap dir: %v", err)
	}
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal bootstrap json: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "bootstrap-static.json"), b, 0o644); err != nil {
		t.Fatalf("write bootstrap json: %v", err)
	}
}

func TestLoadBootstrapData_ExpandedFields(t *testing.T) {
	rawRoot := t.TempDir()
	writeBootstrapJSON(t, rawRoot, map[string]any{
		"elements": []any{
			map[string]any{
				"id":                           10,
				"web_name":                     "Salah",
				"team":                         1,
				"element_type":                 3,
				"status":                       "a",
				"total_points":                 180,
				"expected_goal_involvements":   "12.5",
				"expected_assists":             "4.3",
				"bonus":                        22,
				"bps":                          550,
				"goals_scored":                 15,
				"assists":                      8,
				"clean_sheets":                 3,
				"saves":                        0,
				"chance_of_playing_next_round": 75,
				"ict_index":                    "320.4",
				"minutes":                      2500,
			},
		},
		"teams":    []any{map[string]any{"id": 1, "short_name": "LIV"}},
		"fixtures": map[string]any{},
	})

	elements, _, _, err := loadBootstrapData(rawRoot)
	if err != nil {
		t.Fatalf("loadBootstrapData: %v", err)
	}
	if len(elements) != 1 {
		t.Fatalf("expected 1 element, got %d", len(elements))
	}

	e := elements[0]
	if e.ID != 10 {
		t.Errorf("ID: want 10, got %d", e.ID)
	}
	if math.Abs(e.SeasonXGI-12.5) > 1e-9 {
		t.Errorf("SeasonXGI: want 12.5, got %f", e.SeasonXGI)
	}
	if math.Abs(e.SeasonXA-4.3) > 1e-9 {
		t.Errorf("SeasonXA: want 4.3, got %f", e.SeasonXA)
	}
	if e.SeasonBonus != 22 {
		t.Errorf("SeasonBonus: want 22, got %d", e.SeasonBonus)
	}
	if e.SeasonBPS != 550 {
		t.Errorf("SeasonBPS: want 550, got %d", e.SeasonBPS)
	}
	if e.SeasonGoals != 15 {
		t.Errorf("SeasonGoals: want 15, got %d", e.SeasonGoals)
	}
	if e.SeasonAssists != 8 {
		t.Errorf("SeasonAssists: want 8, got %d", e.SeasonAssists)
	}
	if e.SeasonCleanSheets != 3 {
		t.Errorf("SeasonCleanSheets: want 3, got %d", e.SeasonCleanSheets)
	}
	if e.SeasonSaves != 0 {
		t.Errorf("SeasonSaves: want 0, got %d", e.SeasonSaves)
	}
	if e.ChanceOfPlayingNext != 75 {
		t.Errorf("ChanceOfPlayingNext: want 75, got %d", e.ChanceOfPlayingNext)
	}
	if math.Abs(e.ICTIndex-320.4) > 1e-9 {
		t.Errorf("ICTIndex: want 320.4, got %f", e.ICTIndex)
	}
	if e.SeasonMinutes != 2500 {
		t.Errorf("SeasonMinutes: want 2500, got %d", e.SeasonMinutes)
	}
}

// TestLoadBootstrapData_NullChanceOfPlaying verifies that a JSON null for
// chance_of_playing_next_round maps to -1 in the elementInfo struct.
func TestLoadBootstrapData_NullChanceOfPlaying(t *testing.T) {
	rawRoot := t.TempDir()
	writeBootstrapJSON(t, rawRoot, map[string]any{
		"elements": []any{
			map[string]any{
				"id":                           20,
				"web_name":                     "Haaland",
				"team":                         2,
				"element_type":                 4,
				"status":                       "a",
				"total_points":                 150,
				"expected_goal_involvements":   "0.0",
				"expected_assists":             "0.0",
				"bonus":                        0,
				"bps":                          0,
				"goals_scored":                 0,
				"assists":                      0,
				"clean_sheets":                 0,
				"saves":                        0,
				"chance_of_playing_next_round": nil,
				"ict_index":                    "0.0",
				"minutes":                      0,
			},
		},
		"teams":    []any{map[string]any{"id": 2, "short_name": "MCI"}},
		"fixtures": map[string]any{},
	})

	elements, _, _, err := loadBootstrapData(rawRoot)
	if err != nil {
		t.Fatalf("loadBootstrapData: %v", err)
	}
	if elements[0].ChanceOfPlayingNext != -1 {
		t.Errorf("null chance_of_playing_next_round: want -1, got %d", elements[0].ChanceOfPlayingNext)
	}
}

// ---------------------------------------------------------------------------
// loadLiveStats — new expanded fields
// ---------------------------------------------------------------------------

func TestLoadLiveStats_ExpandedFields(t *testing.T) {
	rawRoot := t.TempDir()

	writeLiveJSON(t, rawRoot, 5, map[string]any{
		// Element with non-zero values for every field.
		"42": map[string]any{"stats": map[string]any{
			"minutes":          90,
			"total_points":     12,
			"expected_goals":   1.2,
			"expected_assists": 0.8,
			"goals_scored":     2,
			"assists":          1,
			"clean_sheets":     1,
			"bonus":            3,
			"bps":              45,
			"saves":            0,
		}},
		// Element with all zeros — verifies the zero path.
		"99": map[string]any{"stats": map[string]any{
			"minutes":          0,
			"total_points":     0,
			"expected_goals":   0.0,
			"expected_assists": 0.0,
			"goals_scored":     0,
			"assists":          0,
			"clean_sheets":     0,
			"bonus":            0,
			"bps":              0,
			"saves":            0,
		}},
	})

	live, err := loadLiveStats(rawRoot, 5)
	if err != nil {
		t.Fatalf("loadLiveStats: %v", err)
	}

	// --- element 42: non-zero values ---
	s := live[42]
	if s.Minutes != 90 {
		t.Errorf("Minutes: want 90, got %d", s.Minutes)
	}
	if s.TotalPoints != 12 {
		t.Errorf("TotalPoints: want 12, got %d", s.TotalPoints)
	}
	if math.Abs(s.XG-1.2) > 1e-9 {
		t.Errorf("XG: want 1.2, got %f", s.XG)
	}
	if math.Abs(s.XA-0.8) > 1e-9 {
		t.Errorf("XA: want 0.8, got %f", s.XA)
	}
	if s.Goals != 2 {
		t.Errorf("Goals: want 2, got %d", s.Goals)
	}
	if s.Assists != 1 {
		t.Errorf("Assists: want 1, got %d", s.Assists)
	}
	if s.CleanSheets != 1 {
		t.Errorf("CleanSheets: want 1, got %d", s.CleanSheets)
	}
	if s.Bonus != 3 {
		t.Errorf("Bonus: want 3, got %d", s.Bonus)
	}
	if s.BPS != 45 {
		t.Errorf("BPS: want 45, got %d", s.BPS)
	}
	if s.Saves != 0 {
		t.Errorf("Saves: want 0, got %d", s.Saves)
	}

	// --- element 99: all zeros ---
	z := live[99]
	if z.XA != 0 || z.Goals != 0 || z.Assists != 0 || z.CleanSheets != 0 || z.Bonus != 0 || z.BPS != 0 || z.Saves != 0 {
		t.Errorf("element 99 should be all zeros, got %+v", z)
	}
}

// Suppress unused import if math was already imported.
var _ = math.Pi
