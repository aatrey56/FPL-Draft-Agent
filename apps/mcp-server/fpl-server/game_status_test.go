package main

import (
	"path/filepath"
	"strconv"
	"testing"
)

// writeLiveFixtures writes gw/{gw}/live.json with the given fixtures.
func writeLiveFixtures(t *testing.T, dir string, gw int, fixtures []any) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "gw", strconv.Itoa(gw), "live.json"), map[string]any{
		"elements": map[string]any{},
		"fixtures": fixtures,
	})
}

// writeFullGameJSON writes game/game.json with all status fields.
func writeFullGameJSON(t *testing.T, dir string, current int, finished bool, next int, waiversProcessed bool, processingStatus string) {
	t.Helper()
	writeJSON(t, filepath.Join(dir, "game", "game.json"), map[string]any{
		"current_event":          current,
		"current_event_finished": finished,
		"next_event":             next,
		"waivers_processed":      waiversProcessed,
		"processing_status":      processingStatus,
	})
}

// writeBootstrapEvents writes bootstrap-static.json with events and optional fixtures.
func writeBootstrapEvents(t *testing.T, dir string, events []map[string]any, fixtures map[string]any) {
	t.Helper()
	if fixtures == nil {
		fixtures = map[string]any{}
	}
	writeJSON(t, filepath.Join(dir, "bootstrap", "bootstrap-static.json"), map[string]any{
		"events": map[string]any{
			"data": events,
		},
		"fixtures": fixtures,
		"elements": []any{},
		"teams":    []any{},
	})
}

func TestBuildGameStatus(t *testing.T) {
	// Shared events for most tests: GW27 finished, GW28 not finished.
	twoEvents := []map[string]any{
		{"id": 27, "finished": true, "deadline_time": "2026-02-20T18:30:00Z", "waivers_time": "2026-02-19T18:30:00Z", "trades_time": "2026-02-18T18:30:00Z"},
		{"id": 28, "finished": false, "deadline_time": "2026-02-27T18:30:00Z", "waivers_time": "2026-02-26T18:30:00Z", "trades_time": "2026-02-25T18:30:00Z"},
	}

	t.Run("FinishedGW", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 27, true, 28, true, "n")
		writeBootstrapEvents(t, dir, twoEvents, map[string]any{
			"28": []any{
				map[string]any{"id": 280, "event": 28, "kickoff_time": "2026-02-27T20:00:00Z", "started": false, "finished": false},
			},
		})
		// GW27 live.json with all fixtures finished.
		writeLiveFixtures(t, dir, 27, []any{
			map[string]any{"id": 261, "event": 27, "team_h": 1, "team_a": 2, "started": true, "finished": true},
			map[string]any{"id": 262, "event": 27, "team_h": 3, "team_a": 4, "started": true, "finished": true},
		})

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if out.PointsStatus != "final" {
			t.Errorf("points_status=%q want final", out.PointsStatus)
		}
		if out.CurrentGWFinished != true {
			t.Error("current_gw_finished should be true")
		}
		if out.NextDeadline != "2026-02-27T18:30:00Z" {
			t.Errorf("next_deadline=%q want 2026-02-27T18:30:00Z", out.NextDeadline)
		}
		if out.NextGWFirstKickoff != "2026-02-27T20:00:00Z" {
			t.Errorf("next_gw_first_kickoff=%q want 2026-02-27T20:00:00Z", out.NextGWFirstKickoff)
		}
	})

	t.Run("LiveGW", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 28, false, 29, false, "n")
		writeBootstrapEvents(t, dir, []map[string]any{
			{"id": 27, "finished": true, "deadline_time": "2026-02-20T18:30:00Z", "waivers_time": "2026-02-19T18:30:00Z", "trades_time": "2026-02-18T18:30:00Z"},
			{"id": 28, "finished": false, "deadline_time": "2026-02-27T18:30:00Z", "waivers_time": "2026-02-26T18:30:00Z", "trades_time": "2026-02-25T18:30:00Z"},
			{"id": 29, "finished": false, "deadline_time": "2026-03-07T18:30:00Z", "waivers_time": "2026-03-06T18:30:00Z", "trades_time": "2026-03-05T18:30:00Z"},
		}, map[string]any{
			"29": []any{
				map[string]any{"id": 290, "event": 29, "kickoff_time": "2026-03-07T20:00:00Z", "started": false, "finished": false},
			},
		})
		// GW28 live.json: 3 started, 1 finished out of 5 total.
		writeLiveFixtures(t, dir, 28, []any{
			map[string]any{"id": 280, "event": 28, "team_h": 1, "team_a": 2, "started": true, "finished": true},
			map[string]any{"id": 281, "event": 28, "team_h": 3, "team_a": 4, "started": true, "finished": false},
			map[string]any{"id": 282, "event": 28, "team_h": 5, "team_a": 6, "started": true, "finished": false},
			map[string]any{"id": 283, "event": 28, "team_h": 7, "team_a": 8, "started": false, "finished": false},
			map[string]any{"id": 284, "event": 28, "team_h": 9, "team_a": 10, "started": false, "finished": false},
		})

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if out.PointsStatus != "live" {
			t.Errorf("points_status=%q want live", out.PointsStatus)
		}
		if out.CurrentGWFixtures.Total != 5 {
			t.Errorf("total=%d want 5", out.CurrentGWFixtures.Total)
		}
		if out.CurrentGWFixtures.Started != 3 {
			t.Errorf("started=%d want 3", out.CurrentGWFixtures.Started)
		}
		if out.CurrentGWFixtures.Finished != 1 {
			t.Errorf("finished=%d want 1", out.CurrentGWFixtures.Finished)
		}
	})

	t.Run("PendingGW", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 28, false, 29, true, "n")
		writeBootstrapEvents(t, dir, []map[string]any{
			{"id": 28, "finished": false, "deadline_time": "2026-02-27T18:30:00Z", "waivers_time": "2026-02-26T18:30:00Z", "trades_time": "2026-02-25T18:30:00Z"},
		}, map[string]any{
			"28": []any{
				map[string]any{"id": 280, "event": 28, "kickoff_time": "2026-02-27T20:00:00Z", "started": false, "finished": false},
				map[string]any{"id": 281, "event": 28, "kickoff_time": "2026-02-28T15:00:00Z", "started": false, "finished": false},
			},
		})

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if out.PointsStatus != "pending" {
			t.Errorf("points_status=%q want pending", out.PointsStatus)
		}
		// Bootstrap fixtures available pre-kickoff.
		if out.CurrentGWFixtures.Total != 2 {
			t.Errorf("total=%d want 2", out.CurrentGWFixtures.Total)
		}
		if out.CurrentGWFixtures.Started != 0 {
			t.Errorf("started=%d want 0", out.CurrentGWFixtures.Started)
		}
	})

	t.Run("NextKickoffPicksEarliest", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 27, true, 28, true, "n")
		writeBootstrapEvents(t, dir, twoEvents, map[string]any{
			"28": []any{
				map[string]any{"id": 282, "event": 28, "kickoff_time": "2026-02-28T15:00:00Z", "started": false, "finished": false},
				map[string]any{"id": 280, "event": 28, "kickoff_time": "2026-02-27T20:00:00Z", "started": false, "finished": false},
				map[string]any{"id": 281, "event": 28, "kickoff_time": "2026-02-28T12:30:00Z", "started": false, "finished": false},
			},
		})
		writeLiveFixtures(t, dir, 27, []any{
			map[string]any{"id": 261, "event": 27, "team_h": 1, "team_a": 2, "started": true, "finished": true},
		})

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if out.NextGWFirstKickoff != "2026-02-27T20:00:00Z" {
			t.Errorf("next_gw_first_kickoff=%q want 2026-02-27T20:00:00Z (earliest of 3)", out.NextGWFirstKickoff)
		}
	})

	t.Run("MissingGameJSON", func(t *testing.T) {
		_, cfg := tmpCfg(t)
		_, err := buildGameStatus(cfg)
		if err == nil {
			t.Fatal("expected error for missing game.json")
		}
	})

	t.Run("NoFixturesForGW", func(t *testing.T) {
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 28, false, 29, false, "n")
		// Bootstrap has events but no fixtures for GW28 (bootstrap dropped it).
		// Also no live.json for GW28.
		writeBootstrapEvents(t, dir, []map[string]any{
			{"id": 28, "finished": false, "deadline_time": "2026-02-27T18:30:00Z", "waivers_time": "2026-02-26T18:30:00Z", "trades_time": "2026-02-25T18:30:00Z"},
		}, nil)

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		// Graceful zero-value fixture progress.
		if out.CurrentGWFixtures.Total != 0 {
			t.Errorf("total=%d want 0 (no fixtures available)", out.CurrentGWFixtures.Total)
		}
		if out.PointsStatus != "pending" {
			t.Errorf("points_status=%q want pending", out.PointsStatus)
		}
	})

	t.Run("BootstrapDroppedCurrentGWFallsBackToLiveJSON", func(t *testing.T) {
		// Bootstrap has dropped GW28 fixtures (happens once GW starts),
		// but live.json has the fixture data. Verify we read from live.json.
		dir, cfg := tmpCfg(t)
		writeFullGameJSON(t, dir, 28, false, 29, false, "n")
		writeBootstrapEvents(t, dir, []map[string]any{
			{"id": 28, "finished": false, "deadline_time": "2026-02-27T18:30:00Z", "waivers_time": "2026-02-26T18:30:00Z", "trades_time": "2026-02-25T18:30:00Z"},
		}, map[string]any{
			// No "28" key — bootstrap has dropped it.
			"29": []any{
				map[string]any{"id": 290, "event": 29, "kickoff_time": "2026-03-07T20:00:00Z", "started": false, "finished": false},
			},
		})
		// But live.json has the real data.
		writeLiveFixtures(t, dir, 28, []any{
			map[string]any{"id": 280, "event": 28, "team_h": 1, "team_a": 2, "started": true, "finished": true},
			map[string]any{"id": 281, "event": 28, "team_h": 3, "team_a": 4, "started": true, "finished": false},
		})

		out, err := buildGameStatus(cfg)
		if err != nil {
			t.Fatal(err)
		}
		if out.CurrentGWFixtures.Total != 2 {
			t.Errorf("total=%d want 2 (from live.json fallback)", out.CurrentGWFixtures.Total)
		}
		if out.CurrentGWFixtures.Started != 2 {
			t.Errorf("started=%d want 2", out.CurrentGWFixtures.Started)
		}
		if out.CurrentGWFixtures.Finished != 1 {
			t.Errorf("finished=%d want 1", out.CurrentGWFixtures.Finished)
		}
		if out.PointsStatus != "live" {
			t.Errorf("points_status=%q want live", out.PointsStatus)
		}
	})
}
