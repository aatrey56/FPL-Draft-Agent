package main

// Tests for the deadline calendar (buildDeadlines). Fixtures only.

import (
	"path/filepath"
	"strings"
	"testing"
)

func TestBuildDeadlinesCalendar(t *testing.T) {
	root := t.TempDir()
	writeFixture(t, filepath.Join(root, "bootstrap/bootstrap-static.json"), map[string]any{
		"events": map[string]any{
			"current": 1, "next": 2,
			"data": []map[string]any{
				{"id": 1, "name": "Gameweek 1", "finished": false,
					"deadline_time": "2026-08-21T17:30:00Z",
					"waivers_time":  "2026-08-20T17:30:00Z",
					"trades_time":   "2026-08-19T17:30:00Z"},
				{"id": 2, "name": "Gameweek 2", "finished": false,
					"deadline_time": "2026-08-28T17:30:00Z",
					"waivers_time":  "2026-08-27T17:30:00Z",
					"trades_time":   "2026-08-26T17:30:00Z"},
			},
		},
		"fixtures": map[string]any{
			"1": []map[string]any{
				{"kickoff_time": "2026-08-21T19:00:00Z"},
				{"kickoff_time": "2026-08-23T15:30:00Z"},
			},
		},
	})

	out, err := buildDeadlines(root)
	if err != nil {
		t.Fatal(err)
	}
	current, ok := out["current"].(map[string]any)
	if !ok || current["gw"].(int) != 1 {
		t.Fatalf("current GW wrong: %+v", out["current"])
	}
	// Eastern rendering: 17:30 UTC in August = 1:30 PM EST-label.
	lock := current["lineup_lock"].(map[string]string)
	if !strings.Contains(lock["est"], "1:30 PM") || !strings.HasSuffix(lock["est"], "EST") {
		t.Fatalf("eastern time wrong: %q", lock["est"])
	}
	// Points-final estimate: last kickoff Aug 23 15:30 UTC -> Aug 24 08:00 UTC.
	final := current["points_final_estimate"].(map[string]string)
	if !strings.HasPrefix(final["utc"], "2026-08-24T08:00") {
		t.Fatalf("points_final_estimate wrong: %q", final["utc"])
	}
	// Next GW present with waiver deadline; no fixtures -> no kickoff fields.
	next := out["next"].(map[string]any)
	if next["gw"].(int) != 2 {
		t.Fatalf("next GW wrong: %+v", next)
	}
	if _, has := next["first_kickoff"]; has {
		t.Fatal("next GW should omit kickoff fields without fixture data")
	}
}
