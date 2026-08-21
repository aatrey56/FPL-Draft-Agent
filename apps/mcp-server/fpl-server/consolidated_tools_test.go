package main

// Tests for the phase-2 consolidated tools (manager_card, epl, gw_report).
// Deep logic is covered by the underlying builder tests; these verify the
// composition contract: required args, section assembly, and graceful
// degradation (a missing section yields <name>_error, never a hard failure).

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
)

func TestManagerCardComposesSectionsAndDegrades(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir(), DerivedRoot: t.TempDir(), DefaultSeason: "2026-27"}

	// league details exists, but no finished-GW data: sections must degrade.
	writeFixture(t, filepath.Join(cfg.RawRoot, "2026-27/league/5/details.json"), map[string]any{
		"league_entries": []map[string]any{
			{"id": 71, "entry_id": 501, "entry_name": "Harbor FC"},
		},
		"standings": []map[string]any{},
		"matches":   []map[string]any{},
	})
	entry := 501
	res, _, err := managerCardHandler(cfg)(context.Background(), nil, ManagerCardArgs{LeagueID: 5, EntryID: &entry})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{"season", "streak", "schedule"} {
		if !strings.Contains(text, want) {
			t.Fatalf("card missing section (or its _error) for %q: %s", want, text)
		}
	}
	if strings.Contains(text, "head_to_head") {
		t.Fatal("h2h section must only appear when a vs opponent is given")
	}

	// Missing identity args error hard.
	res, _, _ = managerCardHandler(cfg)(context.Background(), nil, ManagerCardArgs{LeagueID: 5})
	if res == nil || !res.IsError {
		t.Fatal("expected error without entry_id/entry_name")
	}
}

func TestEplViewsAndValidation(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir(), DerivedRoot: t.TempDir()}

	res, _, err := eplHandler(cfg)(context.Background(), nil, EPLArgs{View: "standings"})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "standings") {
		t.Fatalf("expected standings section or error: %s", text)
	}
	if strings.Contains(text, "fixtures") {
		t.Fatal("fixtures section must not appear for view=standings")
	}

	res, _, _ = eplHandler(cfg)(context.Background(), nil, EPLArgs{View: "nonsense"})
	if res == nil || !res.IsError {
		t.Fatal("expected error for unknown view")
	}
}

func TestGwReportRequiresLeagueAndDegrades(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir(), DerivedRoot: t.TempDir(), ComputeMissing: false}

	res, _, _ := gwReportHandler(cfg)(context.Background(), nil, GwReportArgs{GW: 1})
	if res == nil || !res.IsError {
		t.Fatal("expected error without league_id")
	}

	res, _, err := gwReportHandler(cfg)(context.Background(), nil, GwReportArgs{LeagueID: 5, GW: 1})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "matchup_breakdown_error") || !strings.Contains(text, "lineup_efficiency_error") {
		t.Fatalf("expected graceful section errors when summaries are missing: %s", text)
	}
}
