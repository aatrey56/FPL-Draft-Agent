package main

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// writeLeagueDetails writes a minimal league details.json under
// rawRoot/league/<leagueID>/details.json.
func writeLeagueDetails(t *testing.T, rawRoot string, leagueID int, data any) {
	t.Helper()
	dir := filepath.Join(rawRoot, "league", itoa(leagueID))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	b, err := json.Marshal(data)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "details.json"), b, 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

func itoa(n int) string {
	return intToString(n)
}

func intToString(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	b := make([]byte, 0, 10)
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	if neg {
		b = append([]byte{'-'}, b...)
	}
	return string(b)
}

// minimalDetails returns a leagueDetailsRaw-compatible struct with two teams
// and four finished matches so each test has real data to work with.
func minimalDetails() map[string]any {
	return map[string]any{
		"league_entries": []map[string]any{
			{"id": 1, "entry_id": 101, "entry_name": "Alpha FC", "short_name": "ALP"},
			{"id": 2, "entry_id": 102, "entry_name": "Beta United", "short_name": "BET"},
		},
		"matches": []map[string]any{
			// GW1 — Alpha wins
			{"event": 1, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 70,
				"league_entry_2": 2, "league_entry_2_points": 55},
			// GW2 — Beta wins
			{"event": 2, "finished": true, "started": true,
				"league_entry_1": 2, "league_entry_1_points": 80,
				"league_entry_2": 1, "league_entry_2_points": 60},
			// GW3 — draw
			{"event": 3, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 65,
				"league_entry_2": 2, "league_entry_2_points": 65},
			// GW4 — Alpha wins
			{"event": 4, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 90,
				"league_entry_2": 2, "league_entry_2_points": 50},
		},
	}
}

// ---------------------------------------------------------------------------
// resultFromScore
// ---------------------------------------------------------------------------

func TestResultFromScore(t *testing.T) {
	tests := []struct {
		name    string
		forPts  int
		against int
		want    string
	}{
		{"Win", 80, 70, "W"},
		{"Loss", 60, 75, "L"},
		{"Draw", 65, 65, "D"},
		{"ZeroZero", 0, 0, "D"},
		{"WinByOne", 71, 70, "W"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resultFromScore(tc.forPts, tc.against)
			if got != tc.want {
				t.Errorf("resultFromScore(%d, %d) = %q; want %q", tc.forPts, tc.against, got, tc.want)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// positionLabel
// ---------------------------------------------------------------------------

func TestPositionLabel(t *testing.T) {
	tests := []struct {
		pos  int
		want string
	}{
		{1, "GK"},
		{2, "DEF"},
		{3, "MID"},
		{4, "FWD"},
		{0, "UNK"},
		{99, "UNK"},
	}
	for _, tc := range tests {
		got := positionLabel(tc.pos)
		if got != tc.want {
			t.Errorf("positionLabel(%d) = %q; want %q", tc.pos, got, tc.want)
		}
	}
}

// ---------------------------------------------------------------------------
// horizonWeights
// ---------------------------------------------------------------------------

func TestHorizonWeights(t *testing.T) {
	tests := []struct {
		name    string
		horizon int
		wSeason float64
		wRecent float64
	}{
		{"ShortHorizon_3", 3, 0.55, 0.45},
		{"ShortHorizon_9", 9, 0.55, 0.45},
		{"MediumHorizon_10", 10, 0.50, 0.50},
		{"MediumHorizon_15", 15, 0.50, 0.50},
		{"LongHorizon_20", 20, 0.40, 0.60},
		{"LongHorizon_38", 38, 0.40, 0.60},
	}
	const eps = 1e-9
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			s, r := horizonWeights(tc.horizon)
			if math.Abs(s-tc.wSeason) > eps || math.Abs(r-tc.wRecent) > eps {
				t.Errorf("horizonWeights(%d) = (%.2f, %.2f); want (%.2f, %.2f)",
					tc.horizon, s, r, tc.wSeason, tc.wRecent)
			}
		})
	}
}

func TestHorizonWeightsSumToOne(t *testing.T) {
	for _, h := range []int{1, 5, 10, 20, 38} {
		s, r := horizonWeights(h)
		sum := s + r
		if math.Abs(sum-1.0) > 1e-9 {
			t.Errorf("horizonWeights(%d) sums to %.4f; want 1.0", h, sum)
		}
	}
}

// ---------------------------------------------------------------------------
// buildHeadToHead
// ---------------------------------------------------------------------------

func TestBuildHeadToHead_ValidMatchup(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 999, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	entryIDA := 101
	entryIDB := 102
	args := HeadToHeadArgs{
		LeagueID: 999,
		EntryIDA: &entryIDA,
		EntryIDB: &entryIDB,
	}

	out, err := buildHeadToHead(cfg, args)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if out.TeamA.EntryID != 101 {
		t.Errorf("TeamA.EntryID = %d; want 101", out.TeamA.EntryID)
	}
	if out.TeamB.EntryID != 102 {
		t.Errorf("TeamB.EntryID = %d; want 102", out.TeamB.EntryID)
	}
	// 4 matches total — all between the two teams
	if len(out.Matches) != 4 {
		t.Errorf("len(Matches) = %d; want 4", len(out.Matches))
	}
	// Alpha: GW1 W, GW2 L, GW3 D, GW4 W → 2W 1D 1L
	if out.TeamA.Wins != 2 {
		t.Errorf("TeamA.Wins = %d; want 2", out.TeamA.Wins)
	}
	if out.TeamA.Draws != 1 {
		t.Errorf("TeamA.Draws = %d; want 1", out.TeamA.Draws)
	}
	if out.TeamA.Losses != 1 {
		t.Errorf("TeamA.Losses = %d; want 1", out.TeamA.Losses)
	}
}

func TestBuildHeadToHead_ByName(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 999, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	nameA := "Alpha FC"
	nameB := "Beta United"
	args := HeadToHeadArgs{
		LeagueID:   999,
		EntryNameA: &nameA,
		EntryNameB: &nameB,
	}

	out, err := buildHeadToHead(cfg, args)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out.TeamA.EntryName != "Alpha FC" {
		t.Errorf("TeamA.EntryName = %q; want %q", out.TeamA.EntryName, "Alpha FC")
	}
}

func TestBuildHeadToHead_MatchesChronological(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 999, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	entryIDA := 101
	entryIDB := 102
	out, err := buildHeadToHead(cfg, HeadToHeadArgs{
		LeagueID: 999,
		EntryIDA: &entryIDA,
		EntryIDB: &entryIDB,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// Verify chronological ordering
	for i := 1; i < len(out.Matches); i++ {
		if out.Matches[i].Gameweek < out.Matches[i-1].Gameweek {
			t.Errorf("matches not sorted: GW%d before GW%d", out.Matches[i].Gameweek, out.Matches[i-1].Gameweek)
		}
	}
}

func TestBuildHeadToHead_MissingLeagueID(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir()}
	_, err := buildHeadToHead(cfg, HeadToHeadArgs{})
	if err == nil {
		t.Fatal("expected error for missing league_id")
	}
}

func TestBuildHeadToHead_UnknownEntry(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 999, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	unknown := 9999
	_, err := buildHeadToHead(cfg, HeadToHeadArgs{
		LeagueID: 999,
		EntryIDA: &unknown,
	})
	if err == nil {
		t.Fatal("expected error for unknown entry_id_a")
	}
}

// ---------------------------------------------------------------------------
// buildManagerSeason
// ---------------------------------------------------------------------------

func TestBuildManagerSeason_BasicRecord(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 888, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	entryID := 101
	out, err := buildManagerSeason(cfg, ManagerSeasonArgs{
		LeagueID: 888,
		EntryID:  &entryID,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Alpha FC: GW1 W(70), GW2 L(60), GW3 D(65), GW4 W(90) → W=2 D=1 L=1
	if out.Record.Wins != 2 {
		t.Errorf("Record.Wins = %d; want 2", out.Record.Wins)
	}
	if out.Record.Losses != 1 {
		t.Errorf("Record.Losses = %d; want 1", out.Record.Losses)
	}
	if out.Record.Draws != 1 {
		t.Errorf("Record.Draws = %d; want 1", out.Record.Draws)
	}
	if out.TotalPoints != 70+60+65+90 {
		t.Errorf("TotalPoints = %d; want %d", out.TotalPoints, 70+60+65+90)
	}
}

func TestBuildManagerSeason_HighestLowestGW(t *testing.T) {
	tmp := t.TempDir()
	writeLeagueDetails(t, tmp, 888, minimalDetails())

	cfg := ServerConfig{RawRoot: tmp}
	entryID := 101
	out, err := buildManagerSeason(cfg, ManagerSeasonArgs{
		LeagueID: 888,
		EntryID:  &entryID,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out.HighestPts != 90 {
		t.Errorf("HighestPts = %d; want 90", out.HighestPts)
	}
	if out.HighestGW != 4 {
		t.Errorf("HighestGW = %d; want 4", out.HighestGW)
	}
	if out.LowestPts != 60 {
		t.Errorf("LowestPts = %d; want 60", out.LowestPts)
	}
	if out.LowestGW != 2 {
		t.Errorf("LowestGW = %d; want 2", out.LowestGW)
	}
}

func TestBuildManagerSeason_MissingLeagueID(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir()}
	_, err := buildManagerSeason(cfg, ManagerSeasonArgs{})
	if err == nil {
		t.Fatal("expected error for missing league_id")
	}
}

// ---------------------------------------------------------------------------
// buildManagerStreak
// ---------------------------------------------------------------------------

func TestBuildManagerStreak_BasicStreak(t *testing.T) {
	tmp := t.TempDir()
	// Two wins then a loss — max streak should be 2.
	details := map[string]any{
		"league_entries": []map[string]any{
			{"id": 1, "entry_id": 201, "entry_name": "Gamma City", "short_name": "GAM"},
			{"id": 2, "entry_id": 202, "entry_name": "Delta Town", "short_name": "DEL"},
		},
		"matches": []map[string]any{
			{"event": 1, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 80,
				"league_entry_2": 2, "league_entry_2_points": 50},
			{"event": 2, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 75,
				"league_entry_2": 2, "league_entry_2_points": 60},
			{"event": 3, "finished": true, "started": true,
				"league_entry_1": 2, "league_entry_1_points": 90,
				"league_entry_2": 1, "league_entry_2_points": 55},
		},
	}
	writeLeagueDetails(t, tmp, 777, details)

	cfg := ServerConfig{RawRoot: tmp}
	entryID := 201
	out, err := buildManagerStreak(cfg, ManagerStreakArgs{
		LeagueID: 777,
		EntryID:  &entryID,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out.MaxWinStreak != 2 {
		t.Errorf("MaxWinStreak = %d; want 2", out.MaxWinStreak)
	}
	// Current streak should be 0 (last match was a loss)
	if out.CurrentWinStreak != 0 {
		t.Errorf("CurrentWinStreak = %d; want 0", out.CurrentWinStreak)
	}
}

func TestBuildManagerStreak_AllWins(t *testing.T) {
	tmp := t.TempDir()
	details := map[string]any{
		"league_entries": []map[string]any{
			{"id": 1, "entry_id": 301, "entry_name": "Epsilon SC", "short_name": "EPS"},
			{"id": 2, "entry_id": 302, "entry_name": "Zeta FC", "short_name": "ZET"},
		},
		"matches": []map[string]any{
			{"event": 1, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 80,
				"league_entry_2": 2, "league_entry_2_points": 50},
			{"event": 2, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 90,
				"league_entry_2": 2, "league_entry_2_points": 60},
			{"event": 3, "finished": true, "started": true,
				"league_entry_1": 1, "league_entry_1_points": 70,
				"league_entry_2": 2, "league_entry_2_points": 40},
		},
	}
	writeLeagueDetails(t, tmp, 666, details)

	cfg := ServerConfig{RawRoot: tmp}
	entryID := 301
	out, err := buildManagerStreak(cfg, ManagerStreakArgs{
		LeagueID: 666,
		EntryID:  &entryID,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if out.MaxWinStreak != 3 {
		t.Errorf("MaxWinStreak = %d; want 3", out.MaxWinStreak)
	}
	if out.CurrentWinStreak != 3 {
		t.Errorf("CurrentWinStreak = %d; want 3", out.CurrentWinStreak)
	}
}

func TestBuildManagerStreak_MissingLeagueID(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir()}
	_, err := buildManagerStreak(cfg, ManagerStreakArgs{})
	if err == nil {
		t.Fatal("expected error for missing league_id")
	}
}
