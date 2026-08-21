package main

// Tests for season-aware path resolution: flat roots are the 2025-26 archive;
// current seasons nest under <root>/<season>. Integration coverage lives in
// the league_pulse / gw_live / player_card fixture tests.

import (
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
