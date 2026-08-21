package main

// Tests for trade_check and league_pulse. Fixtures in t.TempDir() — no live calls.

import (
	"context"
	"path/filepath"
	"strings"
	"testing"
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
	for _, want := range []string{`"season_gain": 50`, `"vor_gain": 50`, "accept"} {
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
