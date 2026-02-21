package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// ---- shared test helpers ----

// writeJSON marshals v to JSON and writes it to path, creating parent dirs.
func writeJSON(t *testing.T, path string, v any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(path), err)
	}
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

// tmpCfg creates a temp directory and a ServerConfig pointing at it.
func tmpCfg(t *testing.T) (string, ServerConfig) {
	t.Helper()
	dir := t.TempDir()
	return dir, ServerConfig{RawRoot: dir}
}

// writeBootstrap writes a minimal bootstrap-static.json with three players:
//
//	1 = Salah  (LIV, MID / element_type=3)
//	2 = Haaland (MCI, FWD / element_type=4)
//	3 = Alexander-Arnold (LIV, DEF / element_type=2)
func writeBootstrap(t *testing.T, dir string) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "bootstrap", "bootstrap-static.json"), map[string]any{
		"elements": []any{
			map[string]any{"id": 1, "web_name": "Salah", "team": 10, "element_type": 3, "status": "a", "total_points": 150},
			map[string]any{"id": 2, "web_name": "Haaland", "team": 11, "element_type": 4, "status": "a", "total_points": 180},
			map[string]any{"id": 3, "web_name": "Alexander-Arnold", "team": 10, "element_type": 2, "status": "a", "total_points": 90},
		},
		"teams": []any{
			map[string]any{"id": 10, "short_name": "LIV"},
			map[string]any{"id": 11, "short_name": "MCI"},
		},
		"fixtures": map[string]any{},
	})
}

// writeGameJSON writes game/game.json declaring the given current event.
func writeGameJSON(t *testing.T, dir string, currentEvent int) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{"current_event": currentEvent})
}

// writeLeagueDetailsFixture writes league/{leagueID}/details.json.
func writeLeagueDetailsFixture(t *testing.T, dir string, leagueID int, entries []any, matches []any) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, fmt.Sprintf("league/%d/details.json", leagueID)), map[string]any{
		"league_entries": entries,
		"matches":        matches,
	})
}

// ---- TestBuildCurrentRoster ----

func TestBuildCurrentRoster(t *testing.T) {
	twoEntries := []any{
		map[string]any{"id": 1, "entry_id": 200, "entry_name": "Alpha FC", "short_name": "AFC"},
		map[string]any{"id": 2, "entry_id": 201, "entry_name": "Beta FC", "short_name": "BFC"},
	}

	// writePicks creates entry/{entryID}/gw/{gw}.json with n picks at positions 1..n.
	writePicks := func(t *testing.T, dir string, entryID, gw, count int) {
		t.Helper()
		picks := make([]any, count)
		for i := 0; i < count; i++ {
			pos := i + 1
			picks[i] = map[string]any{
				"element":         1, // all Salah for simplicity
				"position":        pos,
				"is_captain":      pos == 1,
				"is_vice_captain": pos == 2,
				"multiplier":      1,
			}
		}
		writeJSON(t, filepath.Join(dir, fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw)), map[string]any{"picks": picks})
	}

	t.Run("SplitsStartersAndBench", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 26)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writePicks(t, dir, 200, 26, 15) // positions 1-15; 1-11=starters, 12-15=bench

		entryID := 200
		out, err := buildCurrentRoster(cfg, CurrentRosterArgs{LeagueID: 100, EntryID: &entryID})
		if err != nil {
			t.Fatal(err)
		}
		if len(out.Starters) != 11 {
			t.Errorf("starters=%d want 11", len(out.Starters))
		}
		if len(out.Bench) != 4 {
			t.Errorf("bench=%d want 4", len(out.Bench))
		}
		for _, p := range out.Bench {
			if !p.OnBench {
				t.Errorf("bench player element=%d has OnBench=false", p.Element)
			}
		}
		for _, p := range out.Starters {
			if p.OnBench {
				t.Errorf("starter player element=%d has OnBench=true", p.Element)
			}
		}
	})

	t.Run("ResolveByName", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 26)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writePicks(t, dir, 200, 26, 1)

		name := "Alpha FC"
		out, err := buildCurrentRoster(cfg, CurrentRosterArgs{LeagueID: 100, EntryName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.EntryID != 200 {
			t.Errorf("entry_id=%d want 200", out.EntryID)
		}
		if out.EntryName != "Alpha FC" {
			t.Errorf("entry_name=%q want Alpha FC", out.EntryName)
		}
	})

	t.Run("ResolveByShortName", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 26)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writePicks(t, dir, 200, 26, 1)

		name := "AFC" // short name for Alpha FC
		out, err := buildCurrentRoster(cfg, CurrentRosterArgs{LeagueID: 100, EntryName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.EntryID != 200 {
			t.Errorf("entry_id=%d want 200 (matched via short_name)", out.EntryID)
		}
	})

	t.Run("MissingLeagueID", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildCurrentRoster(cfg, CurrentRosterArgs{})
		if err == nil || err.Error() != "league_id is required" {
			t.Fatalf("expected league_id error, got %v", err)
		}
	})

	t.Run("EntryNameNotFound", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		name := "Unknown FC"
		_, err := buildCurrentRoster(cfg, CurrentRosterArgs{LeagueID: 100, EntryName: &name})
		if err == nil {
			t.Fatal("expected error for unknown entry name")
		}
	})

	t.Run("NoEntryIdentifier", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		_, err := buildCurrentRoster(cfg, CurrentRosterArgs{LeagueID: 100})
		if err == nil {
			t.Fatal("expected error when neither entry_id nor entry_name supplied")
		}
	})
}

// ---- TestBuildDraftPicks ----

func TestBuildDraftPicks(t *testing.T) {
	twoChoices := map[string]any{
		"choices": []any{
			map[string]any{"entry": 200, "entry_name": "Alpha FC", "element": 2, "round": 1, "pick": 2, "index": 2, "was_auto": false},
			map[string]any{"entry": 201, "entry_name": "Beta FC", "element": 1, "round": 1, "pick": 1, "index": 1, "was_auto": false},
		},
	}

	t.Run("AllPicksSortedByIndex", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeJSON(t, filepath.Join(dir, "draft/100/choices.json"), twoChoices)

		out, err := buildDraftPicks(cfg, DraftPicksArgs{LeagueID: 100})
		if err != nil {
			t.Fatal(err)
		}
		if out.TotalPicks != 2 {
			t.Errorf("total_picks=%d want 2", out.TotalPicks)
		}
		if out.Picks[0].OverallIndex != 1 {
			t.Errorf("first pick index=%d want 1 (sorted by index)", out.Picks[0].OverallIndex)
		}
		if out.Picks[1].OverallIndex != 2 {
			t.Errorf("second pick index=%d want 2", out.Picks[1].OverallIndex)
		}
	})

	t.Run("FilterByEntryName", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeJSON(t, filepath.Join(dir, "draft/100/choices.json"), twoChoices)

		name := "Alpha FC"
		out, err := buildDraftPicks(cfg, DraftPicksArgs{LeagueID: 100, EntryName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.TotalPicks != 1 {
			t.Errorf("filtered picks=%d want 1", out.TotalPicks)
		}
		if out.Picks[0].EntryID != 200 {
			t.Errorf("entry_id=%d want 200", out.Picks[0].EntryID)
		}
		if out.FilteredBy != "Alpha FC" {
			t.Errorf("filtered_by=%q want Alpha FC", out.FilteredBy)
		}
	})

	t.Run("FilterByEntryID", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeJSON(t, filepath.Join(dir, "draft/100/choices.json"), twoChoices)

		eid := 201
		out, err := buildDraftPicks(cfg, DraftPicksArgs{LeagueID: 100, EntryID: &eid})
		if err != nil {
			t.Fatal(err)
		}
		if out.TotalPicks != 1 {
			t.Errorf("filtered by ID picks=%d want 1", out.TotalPicks)
		}
		if out.Picks[0].EntryID != 201 {
			t.Errorf("entry_id=%d want 201", out.Picks[0].EntryID)
		}
	})

	t.Run("MissingLeagueID", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildDraftPicks(cfg, DraftPicksArgs{})
		if err == nil {
			t.Fatal("expected league_id error")
		}
	})

	t.Run("EntryNameNotFound", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeJSON(t, filepath.Join(dir, "draft/100/choices.json"), twoChoices)
		name := "Unknown FC"
		_, err := buildDraftPicks(cfg, DraftPicksArgs{LeagueID: 100, EntryName: &name})
		if err == nil {
			t.Fatal("expected error for unknown entry name")
		}
	})
}

// ---- TestBuildHeadToHead ----

func TestBuildHeadToHead(t *testing.T) {
	// leagueEntryID 1 → entryID 200 (Alpha FC), leagueEntryID 2 → entryID 201 (Beta FC).
	twoEntries := []any{
		map[string]any{"id": 1, "entry_id": 200, "entry_name": "Alpha FC", "short_name": "AFC"},
		map[string]any{"id": 2, "entry_id": 201, "entry_name": "Beta FC", "short_name": "BFC"},
	}

	t.Run("WDLAccumulation", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			// GW1: Alpha(le=1) vs Beta(le=2), Alpha wins 50-40.
			map[string]any{"event": 1, "finished": true, "league_entry_1": 1, "league_entry_1_points": 50, "league_entry_2": 2, "league_entry_2_points": 40},
			// GW2: Beta(le=2) vs Alpha(le=1), Alpha still wins 60-55 (Alpha is entry_2).
			map[string]any{"event": 2, "finished": true, "league_entry_1": 2, "league_entry_1_points": 55, "league_entry_2": 1, "league_entry_2_points": 60},
			// GW3: Draw 50-50.
			map[string]any{"event": 3, "finished": true, "league_entry_1": 1, "league_entry_1_points": 50, "league_entry_2": 2, "league_entry_2_points": 50},
		})

		idA, idB := 200, 201
		out, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryIDA: &idA, EntryIDB: &idB})
		if err != nil {
			t.Fatal(err)
		}
		if out.TeamA.Wins != 2 {
			t.Errorf("TeamA.Wins=%d want 2", out.TeamA.Wins)
		}
		if out.TeamA.Draws != 1 {
			t.Errorf("TeamA.Draws=%d want 1", out.TeamA.Draws)
		}
		if out.TeamA.Losses != 0 {
			t.Errorf("TeamA.Losses=%d want 0", out.TeamA.Losses)
		}
		if out.TeamB.Wins != 0 {
			t.Errorf("TeamB.Wins=%d want 0", out.TeamB.Wins)
		}
		if out.TeamB.Losses != 2 {
			t.Errorf("TeamB.Losses=%d want 2", out.TeamB.Losses)
		}
	})

	t.Run("ScoreAssignmentWhenAIsEntry2", func(t *testing.T) {
		// Verify scores are correctly swapped when A is league_entry_2.
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			// Alpha (le=1) is entry_2 in this match (Beta is entry_1).
			map[string]any{"event": 1, "finished": true, "league_entry_1": 2, "league_entry_1_points": 40, "league_entry_2": 1, "league_entry_2_points": 70},
		})

		idA, idB := 200, 201
		out, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryIDA: &idA, EntryIDB: &idB})
		if err != nil {
			t.Fatal(err)
		}
		if len(out.Matches) != 1 {
			t.Fatalf("matches=%d want 1", len(out.Matches))
		}
		m := out.Matches[0]
		// ScoreA = Alpha's score = 70 (was entry_2); ScoreB = Beta's score = 40.
		if m.ScoreA != 70 {
			t.Errorf("ScoreA=%d want 70", m.ScoreA)
		}
		if m.ScoreB != 40 {
			t.Errorf("ScoreB=%d want 40", m.ScoreB)
		}
		if m.ResultA != "W" {
			t.Errorf("ResultA=%q want W", m.ResultA)
		}
	})

	t.Run("ChronologicalSort", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		// Write matches intentionally out of order.
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			map[string]any{"event": 5, "finished": true, "league_entry_1": 1, "league_entry_1_points": 50, "league_entry_2": 2, "league_entry_2_points": 40},
			map[string]any{"event": 2, "finished": true, "league_entry_1": 2, "league_entry_1_points": 60, "league_entry_2": 1, "league_entry_2_points": 55},
			map[string]any{"event": 3, "finished": true, "league_entry_1": 1, "league_entry_1_points": 45, "league_entry_2": 2, "league_entry_2_points": 50},
		})

		idA, idB := 200, 201
		out, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryIDA: &idA, EntryIDB: &idB})
		if err != nil {
			t.Fatal(err)
		}
		for i := 1; i < len(out.Matches); i++ {
			if out.Matches[i].Gameweek <= out.Matches[i-1].Gameweek {
				t.Errorf("matches not in order: GW%d after GW%d", out.Matches[i].Gameweek, out.Matches[i-1].Gameweek)
			}
		}
	})

	t.Run("ResolveByName", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			map[string]any{"event": 1, "finished": true, "league_entry_1": 1, "league_entry_1_points": 60, "league_entry_2": 2, "league_entry_2_points": 50},
		})
		nameA, nameB := "Alpha FC", "Beta FC"
		out, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryNameA: &nameA, EntryNameB: &nameB})
		if err != nil {
			t.Fatal(err)
		}
		if out.TeamA.EntryName != "Alpha FC" {
			t.Errorf("TeamA.EntryName=%q want Alpha FC", out.TeamA.EntryName)
		}
	})

	t.Run("MissingLeagueID", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildHeadToHead(cfg, HeadToHeadArgs{})
		if err == nil {
			t.Fatal("expected league_id error")
		}
	})

	t.Run("EntryNameNotFound", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		name := "Unknown FC"
		_, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryNameA: &name})
		if err == nil {
			t.Fatal("expected error for unknown entry name")
		}
	})

	t.Run("UnfinishedMatchNotCounted", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			// Unfinished — should not affect W/D/L record.
			map[string]any{"event": 1, "finished": false, "league_entry_1": 1, "league_entry_1_points": 70, "league_entry_2": 2, "league_entry_2_points": 50},
		})
		idA, idB := 200, 201
		out, err := buildHeadToHead(cfg, HeadToHeadArgs{LeagueID: 100, EntryIDA: &idA, EntryIDB: &idB})
		if err != nil {
			t.Fatal(err)
		}
		if out.TeamA.Wins != 0 || out.TeamA.Draws != 0 || out.TeamA.Losses != 0 {
			t.Errorf("unfinished match should not count in record: A=%+v", out.TeamA)
		}
		if len(out.Matches) != 1 {
			t.Errorf("match should still appear in list: len=%d", len(out.Matches))
		}
	})
}

// ---- TestBuildManagerSeason ----

func TestBuildManagerSeason(t *testing.T) {
	twoEntries := []any{
		map[string]any{"id": 1, "entry_id": 200, "entry_name": "Alpha FC", "short_name": "AFC"},
		map[string]any{"id": 2, "entry_id": 201, "entry_name": "Beta FC", "short_name": "BFC"},
	}

	t.Run("RecordHighLowAvg", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			// GW1: Alpha(le=1) wins 80-60.
			map[string]any{"event": 1, "finished": true, "league_entry_1": 1, "league_entry_1_points": 80, "league_entry_2": 2, "league_entry_2_points": 60},
			// GW2: Alpha(le=2) loses 70-45.
			map[string]any{"event": 2, "finished": true, "league_entry_1": 2, "league_entry_1_points": 70, "league_entry_2": 1, "league_entry_2_points": 45},
			// GW3: Draw 60-60.
			map[string]any{"event": 3, "finished": true, "league_entry_1": 1, "league_entry_1_points": 60, "league_entry_2": 2, "league_entry_2_points": 60},
		})

		entryID := 200
		out, err := buildManagerSeason(cfg, ManagerSeasonArgs{LeagueID: 100, EntryID: &entryID})
		if err != nil {
			t.Fatal(err)
		}
		if out.Record.Wins != 1 {
			t.Errorf("wins=%d want 1", out.Record.Wins)
		}
		if out.Record.Losses != 1 {
			t.Errorf("losses=%d want 1", out.Record.Losses)
		}
		if out.Record.Draws != 1 {
			t.Errorf("draws=%d want 1", out.Record.Draws)
		}
		if out.HighestGW != 1 || out.HighestPts != 80 {
			t.Errorf("highest GW%d=%d, want GW1=80", out.HighestGW, out.HighestPts)
		}
		if out.LowestGW != 2 || out.LowestPts != 45 {
			t.Errorf("lowest GW%d=%d, want GW2=45", out.LowestGW, out.LowestPts)
		}
		wantAvg := (80.0 + 45.0 + 60.0) / 3.0
		if out.AvgScore != wantAvg {
			t.Errorf("avg=%f want %f", out.AvgScore, wantAvg)
		}
	})

	t.Run("ChronologicalSort", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		// Matches stored out of order.
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			map[string]any{"event": 5, "finished": true, "league_entry_1": 1, "league_entry_1_points": 60, "league_entry_2": 2, "league_entry_2_points": 50},
			map[string]any{"event": 2, "finished": true, "league_entry_1": 1, "league_entry_1_points": 55, "league_entry_2": 2, "league_entry_2_points": 60},
		})
		entryID := 200
		out, err := buildManagerSeason(cfg, ManagerSeasonArgs{LeagueID: 100, EntryID: &entryID})
		if err != nil {
			t.Fatal(err)
		}
		for i := 1; i < len(out.Gameweeks); i++ {
			if out.Gameweeks[i].Gameweek <= out.Gameweeks[i-1].Gameweek {
				t.Errorf("gameweeks not sorted: GW%d after GW%d", out.Gameweeks[i].Gameweek, out.Gameweeks[i-1].Gameweek)
			}
		}
	})

	t.Run("ResolveByName", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, []any{
			map[string]any{"event": 1, "finished": true, "league_entry_1": 1, "league_entry_1_points": 70, "league_entry_2": 2, "league_entry_2_points": 50},
		})
		name := "Alpha FC"
		out, err := buildManagerSeason(cfg, ManagerSeasonArgs{LeagueID: 100, EntryName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.EntryID != 200 {
			t.Errorf("entry_id=%d want 200", out.EntryID)
		}
	})

	t.Run("NoFinishedMatches", func(t *testing.T) {
		// All high/low/avg fields should default to zero when no finished matches exist.
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		entryID := 200
		out, err := buildManagerSeason(cfg, ManagerSeasonArgs{LeagueID: 100, EntryID: &entryID})
		if err != nil {
			t.Fatal(err)
		}
		if out.HighestPts != 0 || out.LowestPts != 0 || out.AvgScore != 0 {
			t.Errorf("expected zeros for empty season, got high=%d low=%d avg=%f", out.HighestPts, out.LowestPts, out.AvgScore)
		}
	})

	t.Run("MissingLeagueID", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildManagerSeason(cfg, ManagerSeasonArgs{})
		if err == nil {
			t.Fatal("expected league_id error")
		}
	})

	t.Run("MissingEntryIdentifier", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		_, err := buildManagerSeason(cfg, ManagerSeasonArgs{LeagueID: 100})
		if err == nil {
			t.Fatal("expected error when neither entry_id nor entry_name supplied")
		}
	})
}

// ---- TestParseFloat ----

func TestParseFloat(t *testing.T) {
	tests := []struct {
		input string
		want  float64
	}{
		{"0.85", 0.85},
		{"1.23", 1.23},
		{"10", 10.0},
		{"0", 0.0},
		{"", 0.0},
		{"invalid", 0.0},
		{"abc1.5", 0.0}, // non-numeric prefix → parse error
	}
	for _, tc := range tests {
		got := parseFloat(tc.input)
		if got != tc.want {
			t.Errorf("parseFloat(%q)=%f want %f", tc.input, got, tc.want)
		}
	}
}

// ---- TestBuildPlayerGWStats ----

func TestBuildPlayerGWStats(t *testing.T) {
	// liveEntry builds the per-element live stats object.
	liveEntry := func(pts int, xg, xa string) map[string]any {
		return map[string]any{
			"stats": map[string]any{
				"minutes": 90, "total_points": pts,
				"goals_scored": 0, "assists": 0, "clean_sheets": 0,
				"bps": 20, "expected_goals": xg, "expected_assists": xa,
			},
		}
	}

	t.Run("ExactNameMatch", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 3)
		for gw := 1; gw <= 3; gw++ {
			writeJSON(t, filepath.Join(dir, fmt.Sprintf("gw/%d/live.json", gw)), map[string]any{
				"elements": map[string]any{"1": liveEntry(gw*4, "0.5", "0.3")},
			})
		}
		name := "Salah"
		out, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{PlayerName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.ElementID != 1 {
			t.Errorf("element_id=%d want 1", out.ElementID)
		}
		// GW1=4, GW2=8, GW3=12 → total=24
		if out.TotalPoints != 24 {
			t.Errorf("total_points=%d want 24", out.TotalPoints)
		}
		wantAvg := 24.0 / 3.0
		if out.AvgPoints != wantAvg {
			t.Errorf("avg_points=%f want %f", out.AvgPoints, wantAvg)
		}
		if len(out.Gameweeks) != 3 {
			t.Errorf("gameweek entries=%d want 3", len(out.Gameweeks))
		}
	})

	t.Run("PartialNameMatch", func(t *testing.T) {
		// "Arnold" is a substring of "Alexander-Arnold" (element 3).
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 1)
		writeJSON(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
			"elements": map[string]any{"3": liveEntry(6, "0.1", "0.6")},
		})
		name := "Arnold"
		out, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{PlayerName: &name})
		if err != nil {
			t.Fatal(err)
		}
		if out.ElementID != 3 {
			t.Errorf("element_id=%d want 3 (Alexander-Arnold partial match)", out.ElementID)
		}
	})

	t.Run("GWRangeFiltering", func(t *testing.T) {
		// Write GW 1-5 data, but only request GW 2-3.
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		for gw := 1; gw <= 5; gw++ {
			writeJSON(t, filepath.Join(dir, fmt.Sprintf("gw/%d/live.json", gw)), map[string]any{
				"elements": map[string]any{"1": liveEntry(gw*3, "0.0", "0.0")},
			})
		}
		startGW, endGW := 2, 3
		id := 1
		out, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{ElementID: &id, StartGW: &startGW, EndGW: &endGW})
		if err != nil {
			t.Fatal(err)
		}
		// GW2=6, GW3=9 → total=15
		if out.TotalPoints != 15 {
			t.Errorf("total_points=%d want 15 (GW2+GW3 only)", out.TotalPoints)
		}
		if len(out.Gameweeks) != 2 {
			t.Errorf("gameweek count=%d want 2", len(out.Gameweeks))
		}
		if out.StartGW != 2 || out.EndGW != 3 {
			t.Errorf("gw range start=%d end=%d want 2-3", out.StartGW, out.EndGW)
		}
	})

	t.Run("XGParsedFromString", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeGameJSON(t, dir, 1)
		writeJSON(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
			"elements": map[string]any{"1": liveEntry(10, "0.75", "0.50")},
		})
		id := 1
		gw := 1
		out, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{ElementID: &id, StartGW: &gw, EndGW: &gw})
		if err != nil {
			t.Fatal(err)
		}
		if out.Gameweeks[0].XG != 0.75 {
			t.Errorf("xg=%f want 0.75", out.Gameweeks[0].XG)
		}
		if out.Gameweeks[0].XA != 0.50 {
			t.Errorf("xa=%f want 0.50", out.Gameweeks[0].XA)
		}
	})

	t.Run("MissingIdentifier", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		_, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{})
		if err == nil {
			t.Fatal("expected error when neither element_id nor player_name provided")
		}
	})

	t.Run("PlayerNotFound", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		name := "Ronaldo"
		_, err := buildPlayerGWStats(cfg, PlayerGWStatsArgs{PlayerName: &name})
		if err == nil {
			t.Fatal("expected error for unknown player name")
		}
	})
}

// ---- TestBuildTxRanking ----

func TestBuildTxRanking(t *testing.T) {
	playerByID := map[int]elementInfo{
		1: {ID: 1, Name: "Salah", TeamID: 10, PositionType: 3},
		2: {ID: 2, Name: "Haaland", TeamID: 11, PositionType: 4},
		3: {ID: 3, Name: "TAA", TeamID: 10, PositionType: 2},
	}
	teamShort := map[int]string{10: "LIV", 11: "MCI"}

	t.Run("SortedByCountDesc", func(t *testing.T) {
		counts := map[int]int{1: 3, 2: 5, 3: 1}
		out := buildTxRanking(counts, playerByID, teamShort, 10)
		if out[0].Element != 2 {
			t.Errorf("first=%d want 2 (count=5)", out[0].Element)
		}
		if out[1].Element != 1 {
			t.Errorf("second=%d want 1 (count=3)", out[1].Element)
		}
		if out[2].Element != 3 {
			t.Errorf("third=%d want 3 (count=1)", out[2].Element)
		}
	})

	t.Run("LimitRespected", func(t *testing.T) {
		counts := map[int]int{1: 3, 2: 5, 3: 1}
		out := buildTxRanking(counts, playerByID, teamShort, 2)
		if len(out) != 2 {
			t.Errorf("len=%d want 2", len(out))
		}
	})

	t.Run("TieBreakByLowerID", func(t *testing.T) {
		counts := map[int]int{1: 3, 2: 3}
		out := buildTxRanking(counts, playerByID, teamShort, 10)
		// On equal count, lower element ID should come first.
		if out[0].Element != 1 {
			t.Errorf("first=%d want 1 (lower id on tie)", out[0].Element)
		}
	})

	t.Run("EmptyCounts", func(t *testing.T) {
		out := buildTxRanking(map[int]int{}, playerByID, teamShort, 10)
		if len(out) != 0 {
			t.Errorf("len=%d want 0 for empty counts", len(out))
		}
	})
}

// ---- TestBuildTransactionAnalysis ----

func TestBuildTransactionAnalysis(t *testing.T) {
	twoEntries := []any{
		map[string]any{"id": 1, "entry_id": 200, "entry_name": "Alpha FC", "short_name": "AFC"},
		map[string]any{"id": 2, "entry_id": 201, "entry_name": "Beta FC", "short_name": "BFC"},
	}

	t.Run("FiltersUnapprovedTransactions", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writeJSON(t, filepath.Join(dir, "league/100/transactions.json"), map[string]any{
			"transactions": []any{
				// Approved waiver → included.
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "w", "result": "a"},
				// Not approved (result="d") → excluded.
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "w", "result": "d"},
				// Wrong GW → excluded.
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 25, "kind": "w", "result": "a"},
				// Unknown kind → excluded.
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "x", "result": "a"},
			},
		})
		out, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{LeagueID: 100, GW: 26})
		if err != nil {
			t.Fatal(err)
		}
		if out.TotalTransactions != 1 {
			t.Errorf("total=%d want 1 (only approved waiver in correct GW)", out.TotalTransactions)
		}
	})

	t.Run("FreeAgentTransactionIncluded", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writeJSON(t, filepath.Join(dir, "league/100/transactions.json"), map[string]any{
			"transactions": []any{
				// kind="f" (free agent) should be included.
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "f", "result": "a"},
			},
		})
		out, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{LeagueID: 100, GW: 26})
		if err != nil {
			t.Fatal(err)
		}
		if out.TotalTransactions != 1 {
			t.Errorf("total=%d want 1 (free agent should be included)", out.TotalTransactions)
		}
	})

	t.Run("PositionBreakdown", func(t *testing.T) {
		// Add Salah (MID=3), drop Haaland (FWD=4).
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writeJSON(t, filepath.Join(dir, "league/100/transactions.json"), map[string]any{
			"transactions": []any{
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "f", "result": "a"},
			},
		})
		out, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{LeagueID: 100, GW: 26})
		if err != nil {
			t.Fatal(err)
		}
		if out.PositionBreakdown["MID"].Added != 1 {
			t.Errorf("MID.Added=%d want 1", out.PositionBreakdown["MID"].Added)
		}
		if out.PositionBreakdown["FWD"].Dropped != 1 {
			t.Errorf("FWD.Dropped=%d want 1", out.PositionBreakdown["FWD"].Dropped)
		}
		if out.PositionBreakdown["DEF"].Added != 0 {
			t.Errorf("DEF.Added=%d want 0", out.PositionBreakdown["DEF"].Added)
		}
	})

	t.Run("TopAddedRanking", func(t *testing.T) {
		// Salah added by both teams; Haaland added once.
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writeJSON(t, filepath.Join(dir, "league/100/transactions.json"), map[string]any{
			"transactions": []any{
				map[string]any{"entry": 200, "element_in": 1, "element_out": 3, "event": 26, "kind": "w", "result": "a"},
				map[string]any{"entry": 201, "element_in": 1, "element_out": 3, "event": 26, "kind": "f", "result": "a"},
				map[string]any{"entry": 200, "element_in": 2, "element_out": 3, "event": 26, "kind": "w", "result": "a"},
			},
		})
		out, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{LeagueID: 100, GW: 26})
		if err != nil {
			t.Fatal(err)
		}
		if len(out.TopAdded) == 0 {
			t.Fatal("top_added is empty")
		}
		if out.TopAdded[0].Element != 1 {
			t.Errorf("top added element=%d want 1 (Salah, count=2)", out.TopAdded[0].Element)
		}
		if out.TopAdded[0].Count != 2 {
			t.Errorf("top added count=%d want 2", out.TopAdded[0].Count)
		}
	})

	t.Run("ManagerActivityGroupedByEntry", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeBootstrap(t, dir)
		writeLeagueDetailsFixture(t, dir, 100, twoEntries, nil)
		writeJSON(t, filepath.Join(dir, "league/100/transactions.json"), map[string]any{
			"transactions": []any{
				map[string]any{"entry": 200, "element_in": 1, "element_out": 2, "event": 26, "kind": "w", "result": "a"},
				map[string]any{"entry": 201, "element_in": 2, "element_out": 3, "event": 26, "kind": "f", "result": "a"},
			},
		})
		out, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{LeagueID: 100, GW: 26})
		if err != nil {
			t.Fatal(err)
		}
		if len(out.ManagerActivity) != 2 {
			t.Errorf("manager_activity len=%d want 2", len(out.ManagerActivity))
		}
	})

	t.Run("MissingLeagueID", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildTransactionAnalysis(cfg, TransactionAnalysisArgs{})
		if err == nil {
			t.Fatal("expected league_id error")
		}
	})
}
