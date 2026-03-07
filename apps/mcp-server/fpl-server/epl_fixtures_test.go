package main

import (
	"path/filepath"
	"strconv"
	"testing"
)

// writeEPLBootstrap writes a bootstrap with 4 PL teams for EPL tests.
func writeEPLBootstrap(t *testing.T, dir string) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "bootstrap", "bootstrap-static.json"), map[string]any{
		"elements": []any{},
		"teams": []any{
			map[string]any{"id": 1, "name": "Arsenal", "short_name": "ARS"},
			map[string]any{"id": 2, "name": "Chelsea", "short_name": "CHE"},
			map[string]any{"id": 3, "name": "Liverpool", "short_name": "LIV"},
			map[string]any{"id": 4, "name": "Man City", "short_name": "MCI"},
		},
		"fixtures": map[string]any{},
	})
}

// writeLiveFixtures writes gw/{gw}/live.json with given fixtures.
func writeLiveFixtures(t *testing.T, dir string, gw int, fixtures []any) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "gw", strconv.Itoa(gw), "live.json"), map[string]any{
		"elements": map[string]any{},
		"fixtures": fixtures,
	})
}

func TestBuildEPLFixtures_SingleGW(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeGameJSON(t, dir, 1)

	hs, as := 2, 1
	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": hs, "team_a_score": as,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 1, "team_h": 3, "team_a": 4,
			"team_h_score": 0, "team_a_score": 0,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLFixtures(cfg, 0)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if result.Gameweek != 1 {
		t.Errorf("expected gw 1, got %d", result.Gameweek)
	}
	if len(result.Fixtures) != 2 {
		t.Fatalf("expected 2 fixtures, got %d", len(result.Fixtures))
	}

	f := result.Fixtures[0]
	if f.HomeShort != "ARS" {
		t.Errorf("home short: want ARS, got %s", f.HomeShort)
	}
	if f.AwayShort != "CHE" {
		t.Errorf("away short: want CHE, got %s", f.AwayShort)
	}
	if f.HomeScore == nil || *f.HomeScore != 2 {
		t.Errorf("home score: want 2, got %v", f.HomeScore)
	}
	if f.AwayScore == nil || *f.AwayScore != 1 {
		t.Errorf("away score: want 1, got %v", f.AwayScore)
	}
	if !f.Finished {
		t.Error("expected finished=true")
	}
}

func TestBuildEPLFixtures_InProgress(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeGameJSON(t, dir, 1)

	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": 1, "team_a_score": 0,
			"finished": false, "started": true,
		},
	})

	result, err := buildEPLFixtures(cfg, 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	f := result.Fixtures[0]
	if f.Finished {
		t.Error("expected finished=false for in-progress match")
	}
	if !f.Started {
		t.Error("expected started=true for in-progress match")
	}
}

func TestBuildEPLFixtures_NotStarted(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeGameJSON(t, dir, 1)

	writeLiveFixtures(t, dir, 1, []any{
		map[string]any{
			"id": 1, "event": 1, "team_h": 1, "team_a": 2,
			"team_h_score": nil, "team_a_score": nil,
			"finished": false, "started": false,
		},
	})

	result, err := buildEPLFixtures(cfg, 1)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	f := result.Fixtures[0]
	if f.HomeScore != nil {
		t.Errorf("expected nil home score, got %v", f.HomeScore)
	}
	if f.AwayScore != nil {
		t.Errorf("expected nil away score, got %v", f.AwayScore)
	}
}

func TestBuildEPLFixtures_MissingGW(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeGameJSON(t, dir, 5)

	// No live.json for GW 5
	_, err := buildEPLFixtures(cfg, 5)
	if err == nil {
		t.Fatal("expected error for missing GW, got nil")
	}
}

func TestBuildEPLFixtures_DGW(t *testing.T) {
	dir, cfg := tmpCfg(t)
	writeEPLBootstrap(t, dir)
	writeGameJSON(t, dir, 10)

	// DGW: Arsenal plays twice
	writeLiveFixtures(t, dir, 10, []any{
		map[string]any{
			"id": 1, "event": 10, "team_h": 1, "team_a": 2,
			"team_h_score": 3, "team_a_score": 0,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 2, "event": 10, "team_h": 3, "team_a": 4,
			"team_h_score": 1, "team_a_score": 1,
			"finished": true, "started": true,
		},
		map[string]any{
			"id": 3, "event": 10, "team_h": 1, "team_a": 4,
			"team_h_score": 2, "team_a_score": 1,
			"finished": true, "started": true,
		},
	})

	result, err := buildEPLFixtures(cfg, 10)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if len(result.Fixtures) != 3 {
		t.Errorf("expected 3 fixtures (DGW), got %d", len(result.Fixtures))
	}
}
