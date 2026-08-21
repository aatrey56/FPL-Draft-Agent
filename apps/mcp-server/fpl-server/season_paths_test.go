package main

// Tests for season-aware path resolution across the legacy (data-layer) tools.
// The flat roots are the 2025-26 archive; current seasons nest under
// <root>/<season>. Fixtures only — no live calls.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestRawDirResolution(t *testing.T) {
	cfg := ServerConfig{RawRoot: "/r", DerivedRoot: "/d", DefaultSeason: "2026-27"}

	if got := cfg.rawDir(""); got != filepath.Join("/r", "2026-27") {
		t.Fatalf("default season should nest: %s", got)
	}
	if got := cfg.rawDir("2027-28"); got != filepath.Join("/r", "2027-28") {
		t.Fatalf("override should win: %s", got)
	}
	if got := cfg.rawDir(ArchiveSeason); got != "/r" {
		t.Fatalf("archive season must stay flat: %s", got)
	}
	if got := cfg.derivedDir(""); got != filepath.Join("/d", "2026-27") {
		t.Fatalf("derived default season should nest: %s", got)
	}

	// No default season configured (legacy tests, archive-only servers).
	flat := ServerConfig{RawRoot: "/r", DerivedRoot: "/d"}
	if got := flat.rawDir(""); got != "/r" {
		t.Fatalf("no season must stay flat: %s", got)
	}
	if got := flat.derivedDir(""); got != "/d" {
		t.Fatalf("no season must stay flat for derived: %s", got)
	}
}

func TestLegacyToolReadsSeasonNestedData(t *testing.T) {
	root := t.TempDir()
	cfg := ServerConfig{
		RawRoot:       filepath.Join(root, "raw"),
		DerivedRoot:   filepath.Join(root, "derived"),
		DefaultSeason: "2026-27",
	}
	details := map[string]any{
		"league_entries": []map[string]any{
			{"id": 7, "entry_id": 111, "entry_name": "Team A", "short_name": "TA"},
		},
	}
	path := filepath.Join(cfg.RawRoot, "2026-27/league/5/details.json")
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(details)
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}

	out, err := buildLeagueEntries(cfg, 5)
	if err != nil {
		t.Fatalf("legacy tool did not resolve season-nested path: %v", err)
	}
	if len(out.Teams) != 1 || out.Teams[0].EntryID != 111 {
		t.Fatalf("unexpected output: %+v", out)
	}

	// The same server must NOT see the archive league at the nested path.
	if _, err := buildLeagueEntries(cfg, 99); err == nil {
		t.Fatal("expected error for missing league in nested season dir")
	}
}
