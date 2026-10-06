package main

// Tests for the TUI's pure pipeline: load() over fixture files and View()
// rendering. No terminal, no network.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/charmbracelet/lipgloss"
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
	m.w = 160
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	view := m.View()
	for _, want := range []string{"Harbor FC", "Ava Stone", "◆you", "Dock United", "Strik", "GW1", "bench"} {
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

func TestMatchViewRendersClubXIs(t *testing.T) {
	dir := fixtureDir(t)
	// Give the live file a started fixture and a starter + a sub for one club.
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 60, "total_points": 8, "starts": 1}},
			"2": map[string]any{"stats": map[string]any{"minutes": 15, "total_points": 1, "starts": 0}},
			"3": map[string]any{"stats": map[string]any{"minutes": 60, "total_points": 2, "starts": 1}},
		},
		"fixtures": []map[string]any{{
			"team_h": 1, "team_a": 2, "team_h_score": 1, "team_a_score": 0,
			"started": true, "finished": false, "kickoff_time": "2026-08-22T14:00:00Z",
		}},
	})
	m := newModel(dir, t.TempDir(), 5, 501, 0)
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	if len(m.snap.Matches) != 1 {
		t.Fatalf("expected 1 live match, got %d", len(m.snap.Matches))
	}
	md := m.snap.Matches[0]
	if md.Minute != 60 {
		t.Fatalf("derived minute wrong: %d", md.Minute)
	}
	m.matchView = true
	view := m.matchBody(80)
	for _, want := range []string{"ARS 1", "0 COV", "Striker", "subs on", "Benchman"} {
		if !strings.Contains(view, want) {
			t.Fatalf("match view missing %q:\n%s", want, view)
		}
	}
}

func TestPlayedGamesListAfterLiveAndOpenLineups(t *testing.T) {
	dir := fixtureDir(t)
	write(t, filepath.Join(dir, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": []map[string]any{
			{"id": 1, "web_name": "Striker", "element_type": 4, "team": 1},
			{"id": 2, "web_name": "Benchman", "element_type": 3, "team": 1},
			{"id": 3, "web_name": "OppKeeper", "element_type": 1, "team": 2},
			{"id": 4, "web_name": "EarlyBird", "element_type": 2, "team": 3},
		},
		"teams": []map[string]any{
			{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "COV"},
			{"id": 3, "short_name": "LEE"}, {"id": 4, "short_name": "HUL"},
		},
	})
	// Fixture order in the file is finished-first; the list must still put
	// the live game on top and the completed one after it, marked FT.
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 2, "starts": 1}},
			"4": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 6, "starts": 1}},
		},
		"fixtures": []map[string]any{
			{"team_h": 3, "team_a": 4, "team_h_score": 2, "team_a_score": 0,
				"started": true, "finished": true, "minutes": 90},
			{"team_h": 1, "team_a": 2, "team_h_score": 1, "team_a_score": 0,
				"started": true, "finished": false},
		},
	})
	m := newModel(dir, t.TempDir(), 5, 501, 0)
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	if len(m.snap.Matches) != 2 {
		t.Fatalf("expected live + played matches, got %d", len(m.snap.Matches))
	}
	if m.snap.Matches[0].Finished || !m.snap.Matches[1].Finished {
		t.Fatalf("live game must list before the played one: %+v", m.snap.Matches)
	}
	// Page 1 holds the live game, page 2 the completed one.
	if list := m.liveBody(30, true); !strings.Contains(list, "ARS 1-0 COV") || strings.Contains(list, "LEE") {
		t.Fatalf("live page wrong:\n%s", list)
	}
	m.gamesPage = 1
	if list := m.liveBody(30, true); !strings.Contains(list, "LEE 2-0 HUL FT") || strings.Contains(list, "ARS") {
		t.Fatalf("played page wrong:\n%s", list)
	}
	// Opening the completed game shows its lineup with an FT score line.
	// (matchSel walks the full games list; the completed game is index 1.)
	m.matchView, m.matchSel = true, 1
	view := m.matchBody(80)
	for _, want := range []string{"LEE 2", "0 HUL", "FT", "EarlyBird"} {
		if !strings.Contains(view, want) {
			t.Fatalf("played-game lineups missing %q:\n%s", want, view)
		}
	}
}

func TestMatchupProjectsAutoSubs(t *testing.T) {
	dir := fixtureDir(t)
	// Full legal XI (1 GKP, 3 DEF, 4 MID, 3 FWD) so the auto-sub engine can
	// validate formations. The MID in slot 5 blanked in a finished fixture;
	// the bench DEF played and must be projected in.
	elements := []map[string]any{{"id": 1, "web_name": "Keeper", "element_type": 1, "team": 1}}
	picks := []map[string]any{{"element": 1, "position": 1}}
	stats := map[string]any{}
	types := []int{2, 2, 2, 3, 3, 3, 3, 4, 4, 4}
	for i, et := range types {
		id := i + 2
		elements = append(elements, map[string]any{
			"id": id, "web_name": fmt.Sprintf("Player%d", id), "element_type": et, "team": 1,
		})
		picks = append(picks, map[string]any{"element": id, "position": id})
		stats[fmt.Sprintf("%d", id)] = map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}}
	}
	stats["1"] = map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}}
	stats["5"] = map[string]any{"stats": map[string]any{"minutes": 0, "total_points": 0}}
	elements = append(elements, map[string]any{"id": 20, "web_name": "BenchDef", "element_type": 2, "team": 1})
	picks = append(picks, map[string]any{"element": 20, "position": 12})
	stats["20"] = map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 5}}
	write(t, filepath.Join(dir, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": elements,
		"teams":    []map[string]any{{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "COV"}},
	})
	write(t, filepath.Join(dir, "entry/501/gw/1.json"), map[string]any{"picks": picks})
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": stats,
		"fixtures": []map[string]any{{"team_h": 1, "team_a": 2, "finished": true, "started": true}},
	})

	snap, myIndex, err := load(dir, t.TempDir(), 5, 501, 0)
	if err != nil {
		t.Fatal(err)
	}
	mine := snap.Matchups[myIndex].A
	if mine.EntryID != 501 {
		mine = snap.Matchups[myIndex].B
	}
	if mine.Total != 20 || mine.Effective != 25 {
		t.Fatalf("total %d effective %d, want 20/25", mine.Total, mine.Effective)
	}
	if mine.Played != 11 {
		t.Fatalf("a confirmed DNP counts as resolved: played %d, want 11", mine.Played)
	}
	for _, p := range mine.Players {
		if p.Name == "BenchDef" && p.Glyph != "⇄" {
			t.Fatalf("projected sub should carry ⇄, got %q", p.Glyph)
		}
	}
}

func TestDoubleGameweekDefersAutoSub(t *testing.T) {
	dir := fixtureDir(t)
	// Same squad as the auto-sub test, but the blanked MID's club has a
	// second, unfinished fixture — no substitution may be projected yet.
	elements := []map[string]any{{"id": 1, "web_name": "Keeper", "element_type": 1, "team": 1}}
	picks := []map[string]any{{"element": 1, "position": 1}}
	stats := map[string]any{}
	types := []int{2, 2, 2, 3, 3, 3, 3, 4, 4, 4}
	for i, et := range types {
		id := i + 2
		elements = append(elements, map[string]any{
			"id": id, "web_name": fmt.Sprintf("Player%d", id), "element_type": et, "team": 1,
		})
		picks = append(picks, map[string]any{"element": id, "position": id})
		stats[fmt.Sprintf("%d", id)] = map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}}
	}
	stats["1"] = map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 2}}
	stats["5"] = map[string]any{"stats": map[string]any{"minutes": 0, "total_points": 0}}
	elements = append(elements, map[string]any{"id": 20, "web_name": "BenchDef", "element_type": 2, "team": 1})
	picks = append(picks, map[string]any{"element": 20, "position": 12})
	stats["20"] = map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 5}}
	write(t, filepath.Join(dir, "bootstrap/bootstrap-static.json"), map[string]any{
		"elements": elements,
		"teams":    []map[string]any{{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "COV"}},
	})
	write(t, filepath.Join(dir, "entry/501/gw/1.json"), map[string]any{"picks": picks})
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": stats,
		"fixtures": []map[string]any{
			{"team_h": 1, "team_a": 2, "finished": true, "started": true},
			{"team_h": 2, "team_a": 1, "finished": false, "started": false},
		},
	})

	snap, myIndex, err := load(dir, t.TempDir(), 5, 501, 0)
	if err != nil {
		t.Fatal(err)
	}
	mine := snap.Matchups[myIndex].A
	if mine.EntryID != 501 {
		mine = snap.Matchups[myIndex].B
	}
	if mine.Effective != mine.Total {
		t.Fatalf("DGW with an unfinished fixture must not project a sub: total %d effective %d", mine.Total, mine.Effective)
	}
}

func TestSubbedOffGetsEarlyCheck(t *testing.T) {
	dir := fixtureDir(t)
	// Live match, clock at 60' (max player minutes). Striker started and froze
	// at 30' -> subbed off, day done, early ✓. Benchman came ON at 45' (no
	// starts flag, 15 minutes) -> still playing, green ●.
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 6, "starts": 1}},
			"2": map[string]any{"stats": map[string]any{"minutes": 15, "total_points": 1, "starts": 0}},
			"3": map[string]any{"stats": map[string]any{"minutes": 60, "total_points": 2, "starts": 1}},
		},
		"fixtures": []map[string]any{{
			"team_h": 1, "team_a": 2, "started": true, "finished": false,
		}},
	})
	write(t, filepath.Join(dir, "entry/501/gw/1.json"), map[string]any{
		"picks": []map[string]any{{"element": 1, "position": 1}, {"element": 2, "position": 2}},
	})
	snap, myIndex, err := load(dir, t.TempDir(), 5, 501, 0)
	if err != nil {
		t.Fatal(err)
	}
	mine := snap.Matchups[myIndex].A
	if mine.EntryID != 501 {
		mine = snap.Matchups[myIndex].B
	}
	glyphs := map[int]string{}
	for _, p := range mine.Players {
		glyphs[p.ID] = p.Glyph
	}
	if glyphs[1] != "✓" {
		t.Fatalf("subbed-off starter should bank the ✓ mid-match, got %q", glyphs[1])
	}
	if glyphs[2] != "●" {
		t.Fatalf("a sub who came on is still playing, got %q", glyphs[2])
	}
}

func TestEventsFromMatchFile(t *testing.T) {
	dir := fixtureDir(t)
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 60, "total_points": 6, "starts": 1}},
			"3": map[string]any{"stats": map[string]any{"minutes": 60, "total_points": 2, "starts": 1}},
		},
		"fixtures": []map[string]any{{"team_h": 1, "team_a": 2, "started": true, "finished": false}},
	})
	now := time.Now().UTC()
	// Two events today: Striker (mine, element 1, FWD) scores at 55'; OppKeeper
	// booked later at 78'. Chronological order + real minutes must survive.
	write(t, filepath.Join(dir, "gw/1/match_events.json"), map[string]any{
		"gw": 1,
		"events": []map[string]any{
			{"element": 3, "kind": "Y", "minute": "78'", "club": "COV",
				"utc": now.Add(1 * time.Minute).Format(time.RFC3339)},
			{"element": 1, "kind": "G", "minute": "55'", "club": "ARS",
				"utc": now.Format(time.RFC3339)},
		},
	})
	m := newModel(dir, t.TempDir(), 5, 501, 0)
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	if len(m.events) != 2 {
		t.Fatalf("want 2 events, got %d: %+v", len(m.events), m.events)
	}
	// Ordered by occurrence: the 55' goal first, the 78' card second.
	g := m.events[0]
	if g.Kind != "G" || g.Name != "Striker" || g.Min != "55'" || !g.Mine || g.Delta != 4 {
		t.Fatalf("goal row wrong: %+v", g)
	}
	if m.events[1].Kind != "Y" || m.events[1].Min != "78'" {
		t.Fatalf("card row wrong: %+v", m.events[1])
	}
	if g.Wall.IsZero() {
		t.Fatal("event should carry the real occurrence time")
	}
	m.evSel = 0
	d := m.eventDetailBody(48)
	for _, want := range []string{"Goal", "Striker", "55'", "+4", "YOUR player"} {
		if !strings.Contains(d, want) {
			t.Fatalf("event detail missing %q:\n%s", want, d)
		}
	}
	// A stale event from another day is filtered out.
	write(t, filepath.Join(dir, "gw/1/match_events.json"), map[string]any{
		"gw": 1,
		"events": []map[string]any{
			{"element": 1, "kind": "G", "minute": "10'", "club": "ARS",
				"utc": now.AddDate(0, 0, -2).Format(time.RFC3339)},
		},
	})
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	if len(m.events) != 0 {
		t.Fatalf("yesterday's events must not show today: %+v", m.events)
	}
}

func TestBonusRaceBuilds(t *testing.T) {
	dir := fixtureDir(t)
	write(t, filepath.Join(dir, "gw/1/live.json"), map[string]any{
		"elements": map[string]any{
			"1": map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 8, "bps": 40, "starts": 1}},
			"3": map[string]any{"stats": map[string]any{"minutes": 30, "total_points": 2, "bps": 12, "starts": 1}},
		},
		"fixtures": []map[string]any{{
			"team_h": 1, "team_a": 2, "started": true, "finished": false,
			"team_h_score": 1, "team_a_score": 0,
		}},
	})
	m := newModel(dir, t.TempDir(), 5, 501, 0)
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	if len(m.snap.BonusRace) != 1 || len(m.snap.BonusRace[0].Rows) != 2 {
		t.Fatalf("bonus race wrong: %+v", m.snap.BonusRace)
	}
	if m.snap.BonusRace[0].Rows[0].Name != "Striker" || m.snap.BonusRace[0].Rows[0].Award != 3 {
		t.Fatalf("leader should hold (3): %+v", m.snap.BonusRace[0].Rows[0])
	}
	if body := m.bonusBody(40); !strings.Contains(body, "ARS 1-0 COV") || !strings.Contains(body, "40") {
		t.Fatalf("bonus page wrong:\n%s", body)
	}
}

func TestAttentionItemMatchesStableCodes(t *testing.T) {
	cases := []struct {
		name            string
		warnings, codes []string
		glyph, note     string
	}{
		// current my_week text, with codes
		{"coded no value", []string{"no value — judge manually (player_card)"}, []string{"no_value"}, "?", "unprojected"},
		{"coded blank", []string{"blank gameweek: no fixture"}, []string{"blank_gw"}, "◇", "blank GW"},
		{"coded availability", []string{"availability [d] — Knock"}, []string{"availability"}, "⚠", "[d] — Knock"},
		{"coded heuristic", []string{"no model xP — heuristic"}, []string{"heuristic_xp"}, "⚠", "heuristic xP"},
		// the code wins even if the prose is reworded again
		{"code beats prose", []string{"needs a human look"}, []string{"no_value"}, "?", "unprojected"},
		// files written before warning_codes existed
		{"legacy no projection", []string{"no projection — judge manually"}, nil, "?", "unprojected"},
		{"legacy no value", []string{"no value — judge manually (player_card)"}, nil, "?", "unprojected"},
		{"legacy other", []string{"something new"}, nil, "⚠", "something new"},
	}
	for _, c := range cases {
		item, ok := attentionItem("P", c.warnings, c.codes)
		if !ok || item.Glyph != c.glyph || item.Note != c.note {
			t.Errorf("%s: got (%q, %q, %v), want (%q, %q)", c.name, item.Glyph, item.Note, ok, c.glyph, c.note)
		}
	}
	if _, ok := attentionItem("P", nil, nil); ok {
		t.Error("no warnings must yield no rail item")
	}
}

func TestWireRecWithUnknownSeasonGain(t *testing.T) {
	derived := t.TempDir()
	write(t, filepath.Join(derived, "ml/waiver_plan.json"), map[string]any{
		"recommendations": []map[string]any{
			{"add": "Promoted", "add_team": "HUL", "drop": "Weak", "label": "stream",
				"season_gain": nil, "season_unknown": true, "add_ros": nil, "drop_ros": 40.0},
			{"add": "Upgrade", "add_team": "ARS", "drop": "Weak", "label": "upgrade",
				"season_gain": 12.4, "add_ros": 52.4, "drop_ros": 40.0},
		},
	})
	m := newModel(fixtureDir(t), derived, 5, 501, 0)
	m.w = 160
	if err := m.reload(); err != nil {
		t.Fatal(err)
	}
	var notes []string
	for _, r := range m.snap.NeedsYou {
		notes = append(notes, r.Note)
	}
	if got := strings.Join(notes, "|"); got != "wire · stream · ROS ?|wire · upgrade +12" {
		t.Fatalf("notes = %q", got)
	}
	m.sugSel = 0
	body := m.sugDetailBody(120)
	if !strings.Contains(body, "No rest-of-season projection for Promoted") || strings.Contains(body, "+0 is that gap") {
		t.Fatalf("unknown-season detail:\n%s", body)
	}
	m.sugSel = 1
	if body := m.sugDetailBody(120); !strings.Contains(body, "the +12 is that gap") {
		t.Fatalf("known-season detail:\n%s", body)
	}
}

// sharedWarningCodes reads the my_week warning-code contract that the Python
// suite (apps/backend/tests/test_myweek.py) pins too.
func sharedWarningCodes(t *testing.T) []string {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "testdata", "my_week_warning_codes.json"))
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		WarningCodes []string `json:"warning_codes"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	return doc.WarningCodes
}

func TestWarningCodesMatchSharedFixture(t *testing.T) {
	got := []string{warnNoValue, warnHeuristicXP, warnBlankGW, warnAvailability}
	if want := sharedWarningCodes(t); strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("TUI warning codes %v, shared fixture %v", got, want)
	}
}

func TestSuggestionsHeaderMarksXPFallback(t *testing.T) {
	for _, fallback := range []bool{true, false} {
		derived := t.TempDir()
		write(t, filepath.Join(derived, "ml/my_week.json"), map[string]any{
			"xp_fallback": fallback,
			"attention": []map[string]any{{"web_name": "Striker",
				"warnings": []string{"blank gameweek: no fixture"}, "warning_codes": []string{"blank_gw"}}},
		})
		m := newModel(fixtureDir(t), derived, 5, 501, 0)
		m.w = 160
		if err := m.reload(); err != nil {
			t.Fatal(err)
		}
		if m.snap.XPFallback != fallback {
			t.Fatalf("XPFallback = %v, want %v", m.snap.XPFallback, fallback)
		}
		body := m.railBody(40, false)
		if got := strings.Contains(body, "Suggestions heuristic xP"); got != fallback {
			t.Fatalf("fallback=%v: marker shown=%v:\n%s", fallback, got, body)
		}
		for _, line := range strings.Split(body, "\n") {
			if lipgloss.Width(line) > 40 {
				t.Fatalf("rail line wider than the panel (%d): %q", lipgloss.Width(line), line)
			}
		}
	}
}
