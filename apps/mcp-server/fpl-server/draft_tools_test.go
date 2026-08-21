package main

// Tests for the decision-layer tools. Fixtures in t.TempDir() per repo rules —
// no live calls, no dependence on real data/ contents.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func writeFixture(t *testing.T, path string, v any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, raw, 0o644); err != nil {
		t.Fatal(err)
	}
}

func fixtureConfig(t *testing.T) ServerConfig {
	t.Helper()
	root := t.TempDir()
	cfg := ServerConfig{
		RawRoot:       filepath.Join(root, "raw"),
		DerivedRoot:   filepath.Join(root, "derived"),
		DefaultSeason: "2026-27",
	}
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "ml/projections_2627.json"), []map[string]any{
		{"code": 1, "web_name": "Alpha", "position": "FWD", "projected_points": 200.0,
			"tier": 1, "vor": 90.0, "confidence": "high", "risk_flags": "", "position_rank": 1,
			"drivers": "pts90+"},
		{"code": 2, "web_name": "Beta", "position": "FWD", "projected_points": 150.0,
			"tier": 2, "vor": 40.0, "confidence": "high", "risk_flags": "", "position_rank": 2,
			"drivers": "xg90+"},
		{"code": 3, "web_name": "Gamma", "position": "MID", "projected_points": 180.0,
			"tier": 1, "vor": 80.0, "confidence": "medium", "risk_flags": "low_minutes",
			"position_rank": 1, "drivers": "ict_index+"},
	})
	return cfg
}

func resultText(t *testing.T, res *mcp.CallToolResult) string {
	t.Helper()
	if res == nil || len(res.Content) == 0 {
		t.Fatal("empty tool result")
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("unexpected content type %T", res.Content[0])
	}
	if res.IsError {
		t.Fatalf("tool returned error: %s", text.Text)
	}
	return text.Text
}

func TestDraftBoardFiltersPositionAndTop(t *testing.T) {
	cfg := fixtureConfig(t)
	res, _, err := draftBoardHandler(cfg)(context.Background(), nil, DraftBoardArgs{Position: "fwd", Top: 1})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "Alpha") || strings.Contains(text, "Beta") {
		t.Fatalf("expected only FWD rank 1 (Alpha), got: %s", text)
	}
	if strings.Contains(text, "Gamma") {
		t.Fatal("position filter leaked a MID row")
	}
}

func TestPlayerCardMergesHistoryAndFlagsAmbiguity(t *testing.T) {
	cfg := fixtureConfig(t)
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "ml/player_history.json"), map[string]any{
		"1": []map[string]any{
			{"season": "2024-25", "team_name": "Arsenal", "total_points": 344.0},
			{"season": "2025-26", "team_name": "Arsenal", "total_points": 123.0},
		},
	})
	writeFixture(t, filepath.Join(cfg.RawRoot, "2026-27/bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"code": 1, "status": "d", "news": "Knock", "chance_of_playing_next_round": 75},
		},
	})

	res, _, err := playerCardHandler(cfg)(context.Background(), nil, PlayerCardArgs{Name: "alph"})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{"344", "123", "Knock", "season_history"} {
		if !strings.Contains(text, want) {
			t.Fatalf("card missing %q: %s", want, text)
		}
	}

	// Ambiguous substring must error, not guess.
	res, _, _ = playerCardHandler(cfg)(context.Background(), nil, PlayerCardArgs{Name: "a"})
	if res == nil || !res.IsError {
		t.Fatal("expected ambiguity error for name 'a'")
	}
}

func TestPlayerCardFallsBackToHistoryForUnprojectedPlayer(t *testing.T) {
	cfg := fixtureConfig(t)
	// Newman: in the season bootstrap with real history, but no projection
	// (the Maddison case — under the minutes floor last season).
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "ml/player_history.json"), map[string]any{
		"9": []map[string]any{
			{"season": "2024-25", "team_name": "Spurs", "total_points": 133.0},
			{"season": "2025-26", "team_name": "Spurs", "total_points": 3.0},
		},
	})
	writeFixture(t, filepath.Join(cfg.RawRoot, "2026-27/bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"code": 9, "web_name": "Newman", "status": "a", "news": ""},
		},
	})

	res, _, err := playerCardHandler(cfg)(context.Background(), nil, PlayerCardArgs{Name: "newman"})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{"133", "No projection", "season_history", "Newman"} {
		if !strings.Contains(text, want) {
			t.Fatalf("fallback card missing %q: %s", want, text)
		}
	}

	// A name in neither projections nor bootstrap still errors cleanly.
	res, _, _ = playerCardHandler(cfg)(context.Background(), nil, PlayerCardArgs{Name: "nosuch"})
	if res == nil || !res.IsError {
		t.Fatal("expected error for unknown player")
	}
}

func TestWaiverPlanServesSeasonArtifact(t *testing.T) {
	cfg := fixtureConfig(t)
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "2026-27/ml/waiver_plan.json"), map[string]any{
		"xi_next3_xp": 49.3,
		"recommendations": []map[string]any{
			{"add": "Dubravka", "drop": "BackupGK", "label": "upgrade",
				"next3_gain": 7.5, "season_gain": 96.0},
		},
	})
	res, _, err := waiverPlanHandler(cfg)(context.Background(), nil, WaiverPlanArgs{})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "Dubravka") || !strings.Contains(text, "49.3") {
		t.Fatalf("waiver plan not served: %s", text)
	}
}

func TestDropRadarReturnsMostRecentEvents(t *testing.T) {
	cfg := fixtureConfig(t)
	events := []map[string]any{
		{"ts": "t1", "kind": "add", "web_name": "Old"},
		{"ts": "t2", "kind": "drop", "web_name": "Recent"},
	}
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "2026-27/ml/ownership_events.json"), events)
	res, _, err := dropRadarHandler(cfg)(context.Background(), nil, DropRadarArgs{Limit: 1})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "Recent") || strings.Contains(text, "Old") {
		t.Fatalf("expected only the most recent event: %s", text)
	}
}

func TestToolsErrorCleanlyWhenArtifactsMissing(t *testing.T) {
	cfg := ServerConfig{RawRoot: t.TempDir(), DerivedRoot: t.TempDir(), DefaultSeason: "2026-27"}
	res, _, _ := draftBoardHandler(cfg)(context.Background(), nil, DraftBoardArgs{})
	if res == nil || !res.IsError {
		t.Fatal("draft_board should error when projections are missing")
	}
	res, _, _ = waiverPlanHandler(cfg)(context.Background(), nil, WaiverPlanArgs{})
	if res == nil || !res.IsError {
		t.Fatal("waiver_plan should error when the artifact is missing")
	}
}
