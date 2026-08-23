package main

// Tests for gw_live. Fixtures in t.TempDir() — no live calls.

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestGwLiveTracksBothSidesOfTheMatchup(t *testing.T) {
	cfg := fixtureConfig(t)
	season := filepath.Join(cfg.RawRoot, "2026-27")

	writeFixture(t, filepath.Join(season, "game/game.json"), map[string]any{
		"current_event": 1, "next_event": 2,
	})
	writeFixture(t, filepath.Join(season, "league/5/details.json"), map[string]any{
		"league_entries": []map[string]any{
			{"id": 71, "entry_id": 501, "entry_name": "Harbor FC"},
			{"id": 72, "entry_id": 502, "entry_name": "Dock United"},
		},
		"matches": []map[string]any{
			{"event": 1, "league_entry_1": 71, "league_entry_2": 72},
			{"event": 2, "league_entry_1": 72, "league_entry_2": 71},
		},
	})
	writeFixture(t, filepath.Join(season, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"id": 1, "web_name": "Striker", "element_type": 4, "team": 1},
			{"id": 2, "web_name": "Benchman", "element_type": 3, "team": 1},
			{"id": 3, "web_name": "OppKeeper", "element_type": 1, "team": 2},
		},
		"teams": []map[string]any{
			{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "COV"},
		},
	})
	writeFixture(t, filepath.Join(season, "entry/501/gw/1.json"), map[string]any{
		"picks": []map[string]any{
			{"element": 1, "position": 1},
			{"element": 2, "position": 12}, // benched: points must not count
		},
	})
	writeFixture(t, filepath.Join(season, "entry/502/gw/1.json"), map[string]any{
		"picks": []map[string]any{{"element": 3, "position": 1}},
	})
	writeFixture(t, filepath.Join(season, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			// total_points already includes the 3 awarded bonus: the tool
			// must pass it through, not add bonus on top of it.
			"1": map[string]any{"stats": map[string]any{
				"minutes": 45, "total_points": 8, "goals_scored": 1,
				"bonus": 3, "bps": 29}},
			"2": map[string]any{"stats": map[string]any{
				"minutes": 45, "total_points": 6, "goals_scored": 1}},
			"3": map[string]any{"stats": map[string]any{
				"minutes": 45, "total_points": 1}},
		},
	})

	res, _, err := gwLiveHandler(cfg)(context.Background(), nil, GwLiveArgs{LeagueID: 5, EntryID: 501})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{
		"Harbor FC", "Dock United", // both sides resolved from the H2H schedule
		`"live_points": 8`,  // my starter only — bench 6 not counted
		`"bench_points": 6`, // ...but visible separately
		`"live_points": 1`,  // opponent's keeper
		"Striker",
		`"bonus": 3`,  // awarded bonus is surfaced, not just its bps
		`"points": 8`, // ...and points stay as the API reported them
		`"bonus": 0`,  // match in progress, none awarded yet (opp keeper)
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("gw_live missing %q: %s", want, text)
		}
	}

	// Missing live snapshot errors with guidance, not a crash.
	res, _, _ = gwLiveHandler(cfg)(context.Background(), nil, GwLiveArgs{LeagueID: 5, EntryID: 501, GW: 9})
	if res == nil || !res.IsError {
		t.Fatal("expected clean error for missing GW snapshot")
	}
	if !strings.Contains(resultText2(t, res), "refresh") {
		t.Fatal("error should tell the user how to fetch the snapshot")
	}

	// Unknown entry errors clearly.
	res, _, _ = gwLiveHandler(cfg)(context.Background(), nil, GwLiveArgs{LeagueID: 5, EntryID: 999})
	if res == nil || !res.IsError {
		t.Fatal("expected error for entry not in league")
	}
}

// resultText2 reads text from an error result (resultText fails on IsError).
func resultText2(t *testing.T, res *mcp.CallToolResult) string {
	t.Helper()
	if res == nil || len(res.Content) == 0 {
		t.Fatal("empty result")
	}
	text, ok := res.Content[0].(*mcp.TextContent)
	if !ok {
		t.Fatalf("unexpected content type %T", res.Content[0])
	}
	return text.Text
}

func TestGwLiveProjectsAutoSubs(t *testing.T) {
	cfg := fixtureConfig(t)
	season := filepath.Join(cfg.RawRoot, "2026-27")

	writeFixture(t, filepath.Join(season, "game/game.json"), map[string]any{"current_event": 1})
	writeFixture(t, filepath.Join(season, "league/5/details.json"), map[string]any{
		"league_entries": []map[string]any{{"id": 71, "entry_id": 501, "entry_name": "Harbor FC"}},
		"matches":        []map[string]any{},
	})
	// XI: GKP + 3 DEF + 4 MID + 3 FWD. The MID in slot 5 blanked (fixture
	// finished, 0 minutes) — the DEF on the bench played and must come on.
	elements := []map[string]any{{"id": 1, "web_name": "Keeper", "element_type": 1, "team": 1}}
	picks := []map[string]any{{"element": 1, "position": 1}}
	types := []int{2, 2, 2, 3, 3, 3, 3, 4, 4, 4}
	for i, et := range types {
		elements = append(elements, map[string]any{
			"id": i + 2, "web_name": "P" + string(rune('A'+i)), "element_type": et, "team": 1,
		})
		picks = append(picks, map[string]any{"element": i + 2, "position": i + 2})
	}
	elements = append(elements,
		map[string]any{"id": 20, "web_name": "BenchDef", "element_type": 2, "team": 1},
	)
	picks = append(picks, map[string]any{"element": 20, "position": 12})
	writeFixture(t, filepath.Join(season, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": elements,
		"teams":    []map[string]any{{"id": 1, "short_name": "ARS"}},
	})
	writeFixture(t, filepath.Join(season, "entry/501/gw/1.json"), map[string]any{"picks": picks})
	stats := map[string]any{}
	for i := 1; i <= 11; i++ {
		stats[fmt.Sprintf("%d", i)] = map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}}
	}
	stats["5"] = map[string]any{"stats": map[string]any{"minutes": 0, "total_points": 0}} // MID blanked
	stats["20"] = map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 5}}
	writeFixture(t, filepath.Join(season, "gw/1/live.json"), map[string]any{
		"elements": stats,
		"fixtures": []map[string]any{{"team_h": 1, "team_a": 2, "finished": true}},
	})

	res, _, err := gwLiveHandler(cfg)(context.Background(), nil, GwLiveArgs{LeagueID: 5, EntryID: 501})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{
		`"live_points": 20`,      // named XI: 10 players on 2, one on 0
		`"effective_points": 25`, // +5 from the projected sub
		`"in": "BenchDef"`,
		`"in_points": 5`,
	} {
		if !strings.Contains(text, want) {
			t.Fatalf("gw_live auto-sub missing %q: %s", want, text)
		}
	}
}
