package main

// Tests for trade_check and league_pulse. Fixtures in t.TempDir() — no live calls.

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestTradeCheckComparesSidesAndFlagsUnprojected(t *testing.T) {
	cfg := fixtureConfig(t) // Alpha FWD 200/vor 90, Beta FWD 150/vor 40, Gamma MID 180/vor 80

	res, _, err := tradeCheckHandler(cfg)(context.Background(), nil, TradeCheckArgs{
		Give: []string{"Beta"}, Get: []string{"Alpha"},
	})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	// Regression: with no player_values.json the old heuristic still runs, loudly.
	for _, want := range []string{`"season_gain": 50`, `"vor_gain": 50`, "accept",
		`"value_source": "heuristic"`, "player_values.json unavailable"} {
		if !strings.Contains(text, want) {
			t.Fatalf("trade_check missing %q: %s", want, text)
		}
	}

	// An unprojected player must be warned about, never valued as zero.
	res, _, err = tradeCheckHandler(cfg)(context.Background(), nil, TradeCheckArgs{
		Give: []string{"Alpha"}, Get: []string{"Mystery"},
	})
	if err != nil {
		t.Fatal(err)
	}
	text = resultText(t, res)
	if !strings.Contains(text, "no projection") {
		t.Fatalf("expected unprojected warning: %s", text)
	}

	// Ambiguous names error instead of guessing ("a" matches Alpha+Beta+Gamma).
	res, _, _ = tradeCheckHandler(cfg)(context.Background(), nil, TradeCheckArgs{
		Give: []string{"a"}, Get: []string{"Alpha"},
	})
	if res == nil || !res.IsError {
		t.Fatal("expected ambiguity error")
	}
}

// tradeNow sits between the GW6 deadline (past) and GW7's: next GW = 7.
var tradeNow = time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)

// writeTradeFixtures writes a season bootstrap whose next GW is 7 and a
// player_values.json built for gw.
func writeTradeFixtures(t *testing.T, cfg ServerConfig, gw int) {
	t.Helper()
	writeFixture(t, filepath.Join(cfg.RawRoot, "2026-27/bootstrap/bootstrap-static.json"), map[string]any{
		"events": map[string]any{"current": 6, "next": 7, "data": []map[string]any{
			{"id": 5, "finished": true, "deadline_time": "2026-09-26T10:00:00Z"},
			{"id": 6, "finished": false, "deadline_time": "2026-10-03T10:00:00Z"},
			{"id": 7, "finished": false, "deadline_time": "2026-10-17T10:00:00Z"},
		}},
	})
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "2026-27/ml/player_values.json"), map[string]any{
		"season": "2026-27", "gw": gw, "panel_max_gw": 5, "generated_at": "2026-10-07T08:00:00+00:00",
		"scorer": "model", "xp_fallback": false, "horizon_fallback": false, "horizon_events": []int{7, 8, 9},
		"players": []map[string]any{
			{"code": 1, "web_name": "Alpha", "position": "FWD", "team": "ARS", "status": "a",
				"xp_next": 5.5, "xp_source": "model", "xp_h3": 15.0, "xp_h3_source": "model",
				"ros_points": 200.0, "ros_adj": 180.0, "p_start": 0.95},
			{"code": 2, "web_name": "Beta", "position": "FWD", "team": "WOL", "status": "a",
				"xp_next": 3.25, "xp_source": "model", "xp_h3": 9.5, "xp_h3_source": "model",
				"ros_points": 150.0, "ros_adj": 140.0, "p_start": 0.9},
			{"code": 4, "web_name": "Delta", "position": "MID", "team": "HUL", "status": "a",
				"xp_next": 4.0, "xp_source": "model", "xp_h3": 12.0, "xp_h3_source": "model",
				"ros_points": nil, "ros_adj": nil, "p_start": 0.8},
		},
	})
}

func TestTradeCheckUsesModelValues(t *testing.T) {
	cfg := fixtureConfig(t)
	writeTradeFixtures(t, cfg, 7)

	res, _, err := evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{`"value_source": "model"`, `"gw": 7`, `"panel_max_gw": 5`,
		`"xp_next": 2.25`, `"xp_h3": 5.5`, `"ros_adj": 40`, "accept: clear rest-of-season gain"} {
		if !strings.Contains(text, want) {
			t.Fatalf("model trade_check missing %q: %s", want, text)
		}
	}
	if strings.Contains(text, "vor_gain") {
		t.Fatalf("model path must not report the heuristic VOR: %s", text)
	}

	// Edge: a player without a ROS value is warned about, not counted as zero.
	res, _, _ = evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Alpha"}, Get: []string{"Delta", "Mystery"}}, tradeNow)
	text = resultText(t, res)
	for _, want := range []string{"Delta has no ros_adj value", "Mystery is not in player_values.json", `"ros_adj": -180`} {
		if !strings.Contains(text, want) {
			t.Fatalf("missing %q: %s", want, text)
		}
	}
}

func TestTradeCheckFallsBackWhenValuesStaleOrUnverifiable(t *testing.T) {
	cfg := fixtureConfig(t)
	writeTradeFixtures(t, cfg, 6) // built for GW6, bootstrap is planning GW7

	res, _, err := evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{`"value_source": "heuristic"`, "stale: built for GW6", `"vor_gain": 50`} {
		if !strings.Contains(text, want) {
			t.Fatalf("stale fallback missing %q: %s", want, text)
		}
	}

	// Before GW6's deadline the bootstrap still plans GW6: the same file is fresh.
	before := time.Date(2026, 10, 2, 12, 0, 0, 0, time.UTC)
	res, _, _ = evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, before)
	if text := resultText(t, res); !strings.Contains(text, `"value_source": "model"`) {
		t.Fatalf("expected model values pre-deadline: %s", text)
	}

	// A values file without a bootstrap cannot be checked for freshness.
	cfg2 := fixtureConfig(t)
	writeTradeFixtures(t, cfg2, 7)
	writeFixture(t, filepath.Join(cfg2.RawRoot, "2026-27/bootstrap/bootstrap-static.json"), map[string]any{
		"events": map[string]any{"data": []map[string]any{}},
	})
	res, _, _ = evaluateTrade(cfg2, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if text := resultText(t, res); !strings.Contains(text, "no upcoming gameweek") {
		t.Fatalf("expected season-over fallback reason: %s", text)
	}
}

func TestTradeCheckRejectsStalePanel(t *testing.T) {
	cfg := fixtureConfig(t)
	writeTradeFixtures(t, cfg, 7) // GW7 export built mid-GW6: panel_max_gw 5
	// GW6 has since finished: the panel must reach GW6, so the file is stale.
	writeFixture(t, filepath.Join(cfg.RawRoot, "2026-27/bootstrap/bootstrap-static.json"), map[string]any{
		"events": map[string]any{"current": 6, "next": 7, "data": []map[string]any{
			{"id": 5, "finished": true, "deadline_time": "2026-09-26T10:00:00Z"},
			{"id": 6, "finished": true, "deadline_time": "2026-10-03T10:00:00Z"},
			{"id": 7, "finished": false, "deadline_time": "2026-10-17T10:00:00Z"},
		}},
	})
	res, _, err := evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{`"value_source": "heuristic"`, "panel through GW5, but the last finished GW is 6"} {
		if !strings.Contains(text, want) {
			t.Fatalf("stale-panel fallback missing %q: %s", want, text)
		}
	}

	// Edge: an export without panel_max_gw cannot be verified and is rejected.
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "2026-27/ml/player_values.json"), map[string]any{
		"season": "2026-27", "gw": 7, "panel_max_gw": nil, "scorer": "model", "players": []map[string]any{},
	})
	res, _, _ = evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if text := resultText(t, res); !strings.Contains(text, "has no panel_max_gw") {
		t.Fatalf("expected missing-panel fallback reason: %s", text)
	}
}

func TestLastFinishedEvent(t *testing.T) {
	var cal bootstrapCalendar
	if got := lastFinishedEvent(cal); got != 0 {
		t.Fatalf("no events: got %d, want 0", got)
	}
	cal.Events.Data = []deadlineEvent{{ID: 3, Finished: true}, {ID: 1, Finished: true}, {ID: 4}}
	if got := lastFinishedEvent(cal); got != 3 {
		t.Fatalf("got %d, want 3", got)
	}
}

func TestPlanningEventMirrorsNextGameweek(t *testing.T) {
	now := time.Date(2026, 10, 7, 12, 0, 0, 0, time.UTC)
	ev := func(id int, finished bool, deadline string) deadlineEvent {
		return deadlineEvent{ID: id, Finished: finished, DeadlineTime: deadline}
	}
	cases := []struct {
		name          string
		current, next int
		data          []deadlineEvent
		want          int
	}{
		{"current not started wins", 6, 7, []deadlineEvent{
			ev(6, false, "2026-10-10T10:00:00Z"), ev(7, false, "2026-10-17T10:00:00Z")}, 6},
		{"events.next when current started", 6, 7, []deadlineEvent{
			ev(6, false, "2026-10-03T10:00:00Z"), ev(7, false, "2026-10-17T10:00:00Z")}, 7},
		// Regression: the calendar fallback took the first array member.
		{"fallback is min id, not array order", 0, 0, []deadlineEvent{
			ev(9, false, "2026-10-31T10:00:00Z"), ev(5, true, "2026-09-26T10:00:00Z"),
			ev(8, false, "2026-10-24T10:00:00Z"), ev(6, false, "2026-10-03T10:00:00Z"),
			ev(7, false, "2026-10-17T10:00:00Z")}, 7},
		{"locked or unparseable events never qualify", 0, 0, []deadlineEvent{
			ev(6, false, "2026-10-03T10:00:00Z"), ev(7, false, "not-a-date")}, 0},
	}
	for _, tc := range cases {
		var cal bootstrapCalendar
		cal.Events.Current, cal.Events.Next, cal.Events.Data = tc.current, tc.next, tc.data
		if got := planningEvent(cal, now); got != tc.want {
			t.Errorf("%s: got %d, want %d", tc.name, got, tc.want)
		}
	}
}

func TestTradeCheckLabelsShortHorizon(t *testing.T) {
	cfg := fixtureConfig(t)
	writeTradeFixtures(t, cfg, 7)
	// A full 3-event horizon keeps the nominal label and adds no note.
	res, _, _ := evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	text := resultText(t, res)
	if !strings.Contains(text, "xp_h3 = next 3 GWs") || strings.Contains(text, "horizon_note") {
		t.Fatalf("full horizon mislabelled: %s", text)
	}

	// Season end: the export's 3-GW value sums only GW7-8.
	path := filepath.Join(cfg.DerivedRoot, "2026-27/ml/player_values.json")
	var doc map[string]any
	if err := readJSONFile(path, &doc); err != nil {
		t.Fatal(err)
	}
	doc["horizon_events"] = []int{7, 8}
	writeFixture(t, path, doc)
	res, _, err := evaluateTrade(cfg, TradeCheckArgs{Give: []string{"Beta"}, Get: []string{"Alpha"}}, tradeNow)
	if err != nil {
		t.Fatal(err)
	}
	text = resultText(t, res)
	for _, want := range []string{`"horizon_events": [`, "xp_h3 = next 2 GWs", `"horizon_note"`, "covers only 2 event(s)"} {
		if !strings.Contains(text, want) {
			t.Fatalf("short horizon missing %q: %s", want, text)
		}
	}
	if strings.Contains(text, "next 3 GWs") {
		t.Fatalf("short horizon still labelled 3 GWs: %s", text)
	}
}

func TestHorizonLabel(t *testing.T) {
	cases := map[string][]int{"next 3 GWs": nil, "next GW": {38}, "next 2 GWs": {37, 38}}
	for want, events := range cases {
		if got := horizonLabel(events); got != want {
			t.Errorf("horizonLabel(%v) = %q, want %q", events, got, want)
		}
	}
	if horizonNote([]int{6, 7, 8}) != "" || horizonNote(nil) != "" {
		t.Error("full or unreported horizon must not carry a note")
	}
}

func TestModelVerdictBands(t *testing.T) {
	cases := map[[2]float64]string{
		{12, 0}:   "accept",
		{12, -5}:  "lean accept",
		{-12, 3}:  "short-term rental",
		{-12, -1}: "reject",
		{5, 8}:    "lean accept: similar",
		{5, -3}:   "lean reject",
		{5, 1}:    "close call",
	}
	for in, want := range cases {
		if got := modelVerdict(in[0], in[1], "next 3 GWs"); !strings.HasPrefix(got, want) {
			t.Fatalf("modelVerdict(%v) = %q, want prefix %q", in, got, want)
		}
	}
}

func TestResolveProjectionExactNameBeatsSubstringAmbiguity(t *testing.T) {
	rows := []projectionRow{
		{WebName: "Wood", ProjectedPoints: 120},
		{WebName: "Hinshelwood", ProjectedPoints: 90},
	}
	match, err := resolveProjection(rows, "wood")
	if err != nil {
		t.Fatalf("exact match must not be ambiguous: %v", err)
	}
	if match.WebName != "Wood" {
		t.Fatalf("wrong player: %s", match.WebName)
	}
	if _, err := resolveProjection(rows, "ood"); err == nil {
		t.Fatal("pure substring collisions must still be ambiguous")
	}
	if match, _ := resolveProjection(rows, "hinshel"); match == nil || match.WebName != "Hinshelwood" {
		t.Fatal("unique substring must still resolve")
	}
}

func TestTeamEnvServesAndFiltersTeams(t *testing.T) {
	cfg := fixtureConfig(t)
	writeFixture(t, filepath.Join(cfg.DerivedRoot, "ml/team_env.json"), map[string]any{
		"season": "2025-26",
		"teams": map[string]any{
			"Arsenal": map[string]any{"attack": map[string]any{"xg_pg": 1.76}},
			"Wolves":  map[string]any{"attack": map[string]any{"xg_pg": 1.1}},
		},
	})
	res, _, err := teamEnvHandler(cfg)(context.Background(), nil, TeamEnvArgs{Team: "arse"})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	if !strings.Contains(text, "Arsenal") || strings.Contains(text, "Wolves") {
		t.Fatalf("filter failed: %s", text)
	}
	res, _, _ = teamEnvHandler(cfg)(context.Background(), nil, TeamEnvArgs{Team: "nope"})
	if res == nil || !res.IsError {
		t.Fatal("expected error for unknown team")
	}
}

func TestLeaguePulseComposesStandingsTransactionsAndGame(t *testing.T) {
	cfg := fixtureConfig(t)
	season := filepath.Join(cfg.RawRoot, "2026-27")
	writeFixture(t, filepath.Join(season, "league/5/details.json"), map[string]any{
		"league_entries": []map[string]any{
			{"id": 71, "entry_id": 501, "entry_name": "Harbor FC"},
			{"id": 72, "entry_id": 502, "entry_name": "Dock United"},
		},
		"standings": []map[string]any{
			{"league_entry": 71, "rank": 1, "total": 3, "points_for": 61,
				"points_against": 40, "matches_won": 1},
			{"league_entry": 72, "rank": 2, "total": 0, "points_for": 40,
				"points_against": 61, "matches_lost": 1},
		},
	})
	writeFixture(t, filepath.Join(season, "league/5/transactions.json"), map[string]any{
		"transactions": []map[string]any{
			{"added": "2026-08-19T10:00:00Z", "element_in": 1, "element_out": 2,
				"entry": 501, "event": 1, "kind": "w", "result": "a"},
			{"added": "2026-08-20T10:00:00Z", "element_in": 2, "element_out": 1,
				"entry": 502, "event": 1, "kind": "f", "result": "d"},
		},
	})
	writeFixture(t, filepath.Join(season, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"id": 1, "web_name": "InMan"}, {"id": 2, "web_name": "OutMan"},
		},
	})
	writeFixture(t, filepath.Join(season, "game/game.json"), map[string]any{
		"current_event": 1, "next_event": 2, "waivers_processed": false,
	})

	res, _, err := leaguePulseHandler(cfg)(context.Background(), nil, LeaguePulseArgs{LeagueID: 5, Transactions: 1})
	if err != nil {
		t.Fatal(err)
	}
	text := resultText(t, res)
	for _, want := range []string{"Harbor FC", "1-0-0", "InMan", "next_event"} {
		if !strings.Contains(text, want) {
			t.Fatalf("league_pulse missing %q: %s", want, text)
		}
	}
	// Transactions limited to the most recent (2026-08-20 entry, a denied FA move).
	if !strings.Contains(text, `"result": "d"`) || strings.Contains(text, `"result": "a"`) {
		t.Fatalf("expected only the most recent transaction: %s", text)
	}

	res, _, _ = leaguePulseHandler(cfg)(context.Background(), nil, LeaguePulseArgs{})
	if res == nil || !res.IsError {
		t.Fatal("league_pulse should require league_id")
	}
}
