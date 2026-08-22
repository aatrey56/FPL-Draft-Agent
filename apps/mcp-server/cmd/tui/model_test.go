package main

// Tests for the TUI's pure pipeline: load() over fixture files and View()
// rendering. No terminal, no network.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func write(t *testing.T, path string, v any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(v)
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
}

func fixtureDir(t *testing.T) string {
	dir := t.TempDir()
	write(t, filepath.Join(dir, "game/game.json"), map[string]any{"current_event": 1})
	write(t, filepath.Join(dir, "league/5/details.json"), map[string]any{
		"league_entries": []map[string]any{
			{"id": 71, "entry_id": 501, "entry_name": "Harbor FC", "player_first_name": "Ava", "player_last_name": "Stone"},
			{"id": 72, "entry_id": 502, "entry_name": "Dock United"},
			{"id": 73, "entry_id": 503, "entry_name": "Pier Rovers"},
			{"id": 74, "entry_id": 504, "entry_name": "Quay Town"},
		},
		"matches": []map[string]any{
			{"event": 1, "league_entry_1": 71, "league_entry_2": 72},
			{"event": 1, "league_entry_1": 73, "league_entry_2": 74},
		},
	})
	write(t, filepath.Join(dir, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"id": 1, "web_name": "Striker", "element_type": 4, "team": 1},
			{"id": 2, "web_name": "Benchman", "element_type": 3, "team": 1},
			{"id": 3, "web_name": "OppKeeper", "element_type": 1, "team": 2},
		},
		"teams": []map[string]any{{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "COV"}},
	})
	write(t, filepath.Join(dir, "entry/501/gw/1.json"), map[string]any{
		"picks": []map[string]any{{"element": 1, "position": 1}, {"element": 2, "position": 12}},
	})
	write(t, filepath.Join(dir, "entry/502/gw/1.json"), map[string]any{
		"picks": []map[string]any{{"element": 3, "position": 1}},
	})
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 8}},
			"2": map[string]any{"stats": map[string]any{"minutes": 45, "total_points": 6}},
			"3": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}},
		},
	})
	return dir
}

func TestLoadBuildsMatchupsAndFindsMine(t *testing.T) {
	snap, myIndex, err := load(fixtureDir(t), t.TempDir(), 5, 502, 0)
	if err != nil {
		t.Fatal(err)
	}
	if snap.GW != 1 || len(snap.Matchups) != 2 {
		t.Fatalf("GW %d, %d matchups", snap.GW, len(snap.Matchups))
	}
	if myIndex != 0 {
		t.Fatalf("entry 502 should be in matchup 0, got %d", myIndex)
	}
	a := snap.Matchups[0].A
	if a.Total != 8 || a.Bench != 6 || a.Played != 1 {
		t.Fatalf("starter/bench split wrong: total=%d bench=%d played=%d", a.Total, a.Bench, a.Played)
	}
}

func TestViewRendersBothSides(t *testing.T) {
	m := newModel(fixtureDir(t), t.TempDir(), 5, 501, 0)
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	view := m.View()
	for _, want := range []string{"Harbor FC", "Ava Stone", "◆you", "Dock United", "Striker", "GW1", "bench"} {
		if !strings.Contains(view, want) {
			t.Fatalf("view missing %q:\n%s", want, view)
		}
	}
}

func TestLoadErrorsWithoutData(t *testing.T) {
	if _, _, err := load(t.TempDir(), t.TempDir(), 5, 501, 0); err == nil {
		t.Fatal("expected error on empty dir")
	}
}
