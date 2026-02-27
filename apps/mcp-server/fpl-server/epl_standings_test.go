package main

import (
	"path/filepath"
	"testing"
)

func TestBuildEPLStandings_MultiGW(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)

	// game.json says current event is 2
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          2,
		"current_event_finished": true,
	})

	// GW1: ARS 2-1 CHE, LIV 3-0 MCI
	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 2, "team_a_score": 1,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 1, "team_h": 3, "team_a": 4,
			"team_h_score": 3, "team_a_score": 0,
			"finished": true, "started": true,
		},
	})

	// GW2: CHE 1-1 LIV, MCI 2-0 ARS
	writeLiveFixtures(t, dir, 2, []any{
		map[string]any{
			"id": 3, "event": 2, "team_h": 2, "team_a": 3,
			"team_h_score": 1, "team_a_score": 1,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 4, "event": 2, "team_h": 4, "team_a": 1,
			"team_h_score": 2, "team_a_score": 0,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLStandings(cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if result.AsOfGW != 2 {
		t.Errorf("as_of_gw: want 2, got %d", result.AsOfGW)
	}
	if len(result.Standings) != 4 {
		t.Fatalf("expected 4 teams, got %d", len(result.Standings))
	}

	// Expected standings after 2 GWs:
	// LIV: W1 D1 L0  GF4 GA1 GD+3 Pts4
	// ARS: W1 D0 L1  GF2 GA3 GD-1 Pts3
	// MCI: W1 D0 L1  GF2 GA3 GD-1 Pts3
	// CHE: W0 D1 L1  GF2 GA3 GD-1 Pts1

	top := result.Standings[0]
	if top.Short != "LIV" {
		t.Errorf("1st place: want LIV, got %s", top.Short)
	}
	if top.Points != 4 {
		t.Errorf("LIV points: want 4, got %d", top.Points)
	}
	if top.Won != 1 || top.Drawn != 1 || top.Lost != 0 {
		t.Errorf("LIV W/D/L: want 1/1/0, got %d/%d/%d", top.Won, top.Drawn, top.Lost)
	}
	if top.GF != 4 || top.GA != 1 {
		t.Errorf("LIV GF/GA: want 4/1, got %d/%d", top.GF, top.GA)
	}

	bottom := result.Standings[3]
	if bottom.Short != "CHE" {
		t.Errorf("4th place: want CHE, got %s", bottom.Short)
	}
	if bottom.Points != 1 {
		t.Errorf("CHE points: want 1, got %d", bottom.Points)
	}
}

func TestBuildEPLStandings_Tiebreakers(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          1,
		"current_event_finished": true,
	})

	// GW1: ARS 1-0 CHE, LIV 1-0 MCI (identical records for ARS & LIV)
	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 1, "team_a_score": 0,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 1, "team_h": 3, "team_a": 4,
			"team_h_score": 1, "team_a_score": 0,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLStandings(cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// ARS and LIV both have 3pts, GD+1, GF1. Tiebreaker: team name ASC.
	// Arsenal < Liverpool alphabetically.
	if result.Standings[0].Short != "ARS" {
		t.Errorf("tiebreaker: want ARS first, got %s", result.Standings[0].Short)
	}
	if result.Standings[1].Short != "LIV" {
		t.Errorf("tiebreaker: want LIV second, got %s", result.Standings[1].Short)
	}

	// Same-score teams should share position
	if result.Standings[0].Pos != 1 || result.Standings[1].Pos != 1 {
		t.Errorf("tied teams should share pos 1, got %d and %d",
			result.Standings[0].Pos, result.Standings[1].Pos)
	}
}

func TestBuildEPLStandings_MissingGWSkipped(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          3,
		"current_event_finished": true,
	})

	// Only GW1 has data; GW2 and GW3 missing.
	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 1, "team_a_score": 0,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLStandings(cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Only 2 teams should appear (the ones that played in GW1)
	if len(result.Standings) != 2 {
		t.Errorf("expected 2 teams with data, got %d", len(result.Standings))
	}
}

func TestBuildEPLStandings_UnfinishedMatchesExcluded(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          1,
		"current_event_finished": false,
	})

	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 2, "team_a_score": 0,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 1, "team_h": 3, "team_a": 4,
			"team_h_score": 0, "team_a_score": 0,
			"finished": false, "started": true,
		},
	})

	result, err := buildEPLStandings(cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Only 2 teams from the finished match should appear
	if len(result.Standings) != 2 {
		t.Errorf("expected 2 teams (unfinished excluded), got %d", len(result.Standings))
	}
	if result.Standings[0].Short != "ARS" {
		t.Errorf("winner should be ARS, got %s", result.Standings[0].Short)
	}
}

func TestBuildEPLStandings_DGW(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          1,
		"current_event_finished": true,
	})

	// Arsenal plays twice in DGW (>10 fixtures total handled naturally)
	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 3, "team_a_score": 0,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 1, "team_h": 3, "team_a": 4,
			"team_h_score": 1, "team_a_score": 1,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 3, "event": 1, "team_h": 1, "team_a": 4,
			"team_h_score": 2, "team_a_score": 1,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLStandings(cfg)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// ARS: W2, GF5, GA1, Pts6 (played 2)
	var arsRow *EPLStandingsRow
	for i := range result.Standings {
		if result.Standings[i].Short == "ARS" {
			arsRow = &result.Standings[i]
			break
		}
	}
	if arsRow == nil {
		t.Fatal("ARS not found in standings")
	}
	if arsRow.Played != 2 {
		t.Errorf("ARS played: want 2, got %d", arsRow.Played)
	}
	if arsRow.Won != 2 {
		t.Errorf("ARS won: want 2, got %d", arsRow.Won)
	}
	if arsRow.GF != 5 {
		t.Errorf("ARS GF: want 5, got %d", arsRow.GF)
	}
	if arsRow.Points != 6 {
		t.Errorf("ARS points: want 6, got %d", arsRow.Points)
	}
}
