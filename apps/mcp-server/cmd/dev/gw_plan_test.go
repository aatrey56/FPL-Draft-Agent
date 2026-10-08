package main

// Tests for the per-GW fetch plan. Fixtures in t.TempDir() and an httptest
// server standing in for the draft API — no live calls.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/fetch"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/points"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

var planEntries = []int{101, 102}

// liveElementsFor builds a live elements body holding elements 1..n with the
// stats loadLiveStatsForPoints reads.
func liveElementsFor(n int) string {
	parts := make([]string, 0, n)
	for id := 1; id <= n; id++ {
		parts = append(parts, fmt.Sprintf(`"%d":{"stats":{"minutes":90,"total_points":6}}`, id))
	}
	return "{" + strings.Join(parts, ",") + "}"
}

// liveElements covers every player in validEntryEvent.
var liveElements = liveElementsFor(draftSquadSize)

// squadPicks is a picks array of n distinct players (elements and positions 1..n).
func squadPicks(n int) string {
	parts := make([]string, 0, n)
	for i := 1; i <= n; i++ {
		parts = append(parts, fmt.Sprintf(`{"element":%d,"position":%d}`, i, i))
	}
	return "[" + strings.Join(parts, ",") + "]"
}

// validEntryEvent is a full 15-pick draft squad.
var validEntryEvent = `{"entry_history":{},"picks":` + squadPicks(draftSquadSize) + `,"subs":[]}`

// seedSeason writes a three-GW season with current_event 3:
//
//	GW1 finished, every fixture bonus-confirmed       -> settled
//	GW2 finished, one fixture only finished_provisional -> not settled
//	GW3 finished and confirmed, but the current GW    -> not settled
//
// plus every live and entry-event file, so nothing is missing from disk.
func seedSeason(t *testing.T) *store.JSONStore {
	t.Helper()
	st := store.NewJSONStore(t.TempDir())
	write := func(rel, body string) {
		t.Helper()
		if err := st.WriteRaw(rel, []byte(body), false); err != nil {
			t.Fatal(err)
		}
	}
	write("bootstrap/bootstrap-static.json", `{"events":{"current":3,"next":4,"data":[
		{"id":1,"finished":true},{"id":2,"finished":true},{"id":3,"finished":true},{"id":4,"finished":false}]}}`)
	confirmed := `{"elements":` + liveElements + `,"fixtures":[{"id":1,"finished":true,"finished_provisional":true},{"id":2,"finished":true,"finished_provisional":true}]}`
	provisional := `{"elements":` + liveElements + `,"fixtures":[{"id":3,"finished":true,"finished_provisional":true},{"id":4,"finished":false,"finished_provisional":true}]}`
	write(fetch.EventLivePath(1), confirmed)
	write(fetch.EventLivePath(2), provisional)
	write(fetch.EventLivePath(3), confirmed)
	for gw := 1; gw <= 3; gw++ {
		for _, id := range planEntries {
			write(fetch.EntryEventPath(id, gw), validEntryEvent)
		}
	}
	// GW1 was already seen settled an hour ago, before the entry files above
	// were (notionally) fetched, so its picks count as post-finalisation.
	markFinalisedAt(t, st, 1, time.Now().Add(-time.Hour))
	return st
}

// markFinalisedAt writes gw's finalisation marker with the given mtime.
func markFinalisedAt(t *testing.T, st *store.JSONStore, gw int, at time.Time) {
	t.Helper()
	if err := st.WriteRaw(entriesFinalPath(gw), []byte(`{}`), false); err != nil {
		t.Fatal(err)
	}
	if err := os.Chtimes(st.Path(entriesFinalPath(gw)), at, at); err != nil {
		t.Fatal(err)
	}
}

// ageFile backdates a cached raw file's mtime.
func ageFile(t *testing.T, st *store.JSONStore, rel string, age time.Duration) {
	t.Helper()
	at := time.Now().Add(-age)
	if err := os.Chtimes(st.Path(rel), at, at); err != nil {
		t.Fatal(err)
	}
}

// fakeDraftAPI answers every GET with a valid payload for its endpoint and
// records the paths.
func fakeDraftAPI(t *testing.T, st *store.JSONStore) (*fetch.Client, func() []string) {
	t.Helper()
	var (
		mu   sync.Mutex
		hits []string
	)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		hits = append(hits, r.URL.Path)
		mu.Unlock()
		if strings.HasSuffix(r.URL.Path, "/live") {
			fmt.Fprint(w, `{"elements":`+liveElements+`,"fixtures":[{"id":1,"finished":true}]}`)
			return
		}
		fmt.Fprint(w, validEntryEvent)
	}))
	t.Cleanup(srv.Close)
	client := fetch.NewClient(st)
	client.HTTP = srv.Client()
	client.BaseURL = srv.URL
	client.Sleep = 0
	return client, func() []string {
		mu.Lock()
		defer mu.Unlock()
		out := append([]string(nil), hits...)
		sort.Strings(out)
		return out
	}
}

// runPlan settles, plans and executes GW1..3 the way main does.
func runPlan(t *testing.T, st *store.JSONStore, refresh, refetchAll bool) (gwFetchPlan, []string) {
	t.Helper()
	return runPlanWith(t, st, refresh, refetchAll, participation{})
}

// runPlanWith is runPlan with explicit league participation context.
func runPlanWith(t *testing.T, st *store.JSONStore, refresh, refetchAll bool, part participation) (gwFetchPlan, []string) {
	t.Helper()
	client, hits := fakeDraftAPI(t, st)
	settled, err := settledGWs(st, 3, planEntries)
	if err != nil {
		t.Fatal(err)
	}
	plan := planGWFetches(client, planEntries, 1, 3, refresh, refetchAll, settled, part)
	if err := runFetchTasks(plan.Tasks, 2); err != nil {
		t.Fatal(err)
	}
	return plan, hits()
}

func gwPaths(gws ...int) []string {
	var out []string
	for _, gw := range gws {
		out = append(out, fmt.Sprintf("/event/%d/live", gw))
		for _, id := range planEntries {
			out = append(out, fmt.Sprintf("/entry/%d/event/%d", id, gw))
		}
	}
	sort.Strings(out)
	return out
}

func TestSettledGWsRequiresFinishedConfirmedAndPast(t *testing.T) {
	settled, err := settledGWs(seedSeason(t), 3, planEntries)
	if err != nil {
		t.Fatal(err)
	}
	if len(settled) != 1 || !settled[1] {
		t.Fatalf("settled = %v, want only GW1", settled)
	}
}

// Regression: --refresh-now used to re-download every GW since GW1.
func TestPlanSkipsSettledGWAndRefetchesProvisionalAndCurrent(t *testing.T) {
	plan, hits := runPlan(t, seedSeason(t), true, false)
	if want := gwPaths(2, 3); strings.Join(hits, ",") != strings.Join(want, ",") {
		t.Fatalf("requests = %v, want %v (GW1 settled, GW2 provisional, GW3 current)", hits, want)
	}
	if plan.Fetched != 6 || plan.Skipped != 3 {
		t.Fatalf("fetched/skipped = %d/%d, want 6/3", plan.Fetched, plan.Skipped)
	}
	if len(plan.SettledGWs) != 1 || plan.SettledGWs[0] != 1 {
		t.Fatalf("SettledGWs = %v, want [1]", plan.SettledGWs)
	}
}

func TestPlanRefetchAllForcesSettledGWs(t *testing.T) {
	plan, hits := runPlan(t, seedSeason(t), true, true)
	if want := gwPaths(1, 2, 3); strings.Join(hits, ",") != strings.Join(want, ",") {
		t.Fatalf("requests = %v, want every GW %v", hits, want)
	}
	if plan.Fetched != 9 || plan.Skipped != 0 {
		t.Fatalf("fetched/skipped = %d/%d, want 9/0", plan.Fetched, plan.Skipped)
	}
}

func TestPlanFetchesMissingFileOfSettledGW(t *testing.T) {
	st := seedSeason(t)
	if err := os.Remove(st.Path(fetch.EntryEventPath(102, 1))); err != nil {
		t.Fatal(err)
	}
	_, hits := runPlan(t, st, true, false)
	want := append(gwPaths(2, 3), "/entry/102/event/1")
	sort.Strings(want)
	if strings.Join(hits, ",") != strings.Join(want, ",") {
		t.Fatalf("requests = %v, want %v", hits, want)
	}
}

func TestPlanWithoutRefreshServesCache(t *testing.T) {
	plan, hits := runPlan(t, seedSeason(t), false, false)
	if len(hits) != 0 || plan.Skipped != 9 {
		t.Fatalf("unforced run with every file cached hit the API: %v (skipped %d)", hits, plan.Skipped)
	}
}

func TestSettledGWsEdgeCases(t *testing.T) {
	st := seedSeason(t)
	// Preseason / unknown current event: nothing is past, nothing is settled.
	if settled, _ := settledGWs(st, 0, planEntries); len(settled) != 0 {
		t.Fatalf("current_event 0 settled %v", settled)
	}
	// A live file listing no fixtures never settles.
	if err := st.WriteRaw(fetch.EventLivePath(1), []byte(`{"elements":`+liveElements+`}`), false); err != nil {
		t.Fatal(err)
	}
	if settled, _ := settledGWs(st, 3, planEntries); settled[1] {
		t.Fatal("GW1 settled without any fixtures in its live file")
	}
	// No bootstrap: error (main then plans every GW as unsettled).
	if _, err := settledGWs(store.NewJSONStore(t.TempDir()), 3, planEntries); err == nil {
		t.Fatal("missing bootstrap should error")
	}
}

func TestLogGWFetchPlanIsStructured(t *testing.T) {
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	logGWFetchPlan(gwFetchPlan{Fetched: 6, Skipped: 3, SettledGWs: []int{1}}, 1, 3, false)
	out := buf.String()
	for _, want := range []string{"gw fetch plan", "requests_fetched=6", "requests_skipped=3", "settled_gws=1", "refetch_all=false"} {
		if !strings.Contains(out, want) {
			t.Fatalf("log %q missing %q", out, want)
		}
	}
}

// Regression: a live payload with finished fixtures but no player points used
// to settle the GW, so it was never refetched and BuildResult wrote zero totals.
func TestSettledGWsRejectsPartialLivePayload(t *testing.T) {
	fixtures := `"fixtures":[{"id":1,"finished":true}]`
	cases := map[string]string{
		"empty elements":        `{"elements":{},` + fixtures + `}`,
		"missing elements":      `{` + fixtures + `}`,
		"element without stats": `{"elements":{"1":{}},` + fixtures + `}`,
		"missing total_points":  `{"elements":{"1":{"stats":{"minutes":90}}},` + fixtures + `}`,
		"missing minutes":       `{"elements":{"1":{"stats":{"total_points":2}}},` + fixtures + `}`,
		"malformed JSON":        `{"elements":`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			st := seedSeason(t)
			if err := st.WriteRaw(fetch.EventLivePath(1), []byte(body), false); err != nil {
				t.Fatal(err)
			}
			settled, err := settledGWs(st, 3, planEntries)
			if err != nil {
				t.Fatal(err)
			}
			if settled[1] {
				t.Fatalf("GW1 settled on partial live payload %s", body)
			}
		})
	}
}

// A partial live payload is re-downloaded even on a run that is not a forced
// refresh, and the download replaces the cached file.
func TestPlanRefetchesPartialLivePayload(t *testing.T) {
	partial := `{"elements":{},"fixtures":[{"id":1,"finished":true}]}`
	seed := func() *store.JSONStore {
		st := seedSeason(t)
		if err := st.WriteRaw(fetch.EventLivePath(1), []byte(partial), false); err != nil {
			t.Fatal(err)
		}
		return st
	}

	// Unforced: only the partial live file is fetched.
	st := seed()
	_, hits := runPlan(t, st, false, false)
	if len(hits) != 1 || hits[0] != "/event/1/live" {
		t.Fatalf("unforced requests = %v, want only /event/1/live", hits)
	}
	raw, err := st.ReadRaw(fetch.EventLivePath(1))
	if err != nil || validateLivePayload(raw, nil) != nil {
		t.Fatalf("cached GW1 live not replaced by a valid payload: %s (%v)", raw, err)
	}
	if _, hits := runPlan(t, st, false, false); len(hits) != 0 {
		t.Fatalf("repaired file refetched again: %v", hits)
	}

	// Forced refresh: GW1 is no longer settled, so it is refetched with GW2-3.
	_, hits = runPlan(t, seed(), true, false)
	if want := gwPaths(1, 2, 3); strings.Join(hits, ",") != strings.Join(want, ",") {
		t.Fatalf("refresh requests = %v, want %v", hits, want)
	}
}

func TestPlanRefetchesInvalidEntryEventOfSettledGW(t *testing.T) {
	bodies := map[string]string{
		"malformed JSON": `{"picks":[`,
		"empty picks":    `{"entry_history":{},"picks":[],"subs":[]}`,
		"missing picks":  `{"entry_history":{}}`,
		"short squad":    `{"picks":[{"element":1}]}`,
	}
	for name, body := range bodies {
		t.Run(name, func(t *testing.T) {
			st := seedSeason(t)
			if err := st.WriteRaw(fetch.EntryEventPath(102, 1), []byte(body), false); err != nil {
				t.Fatal(err)
			}
			// Settled GW1, forced refresh: only the invalid entry-event is downloaded.
			_, hits := runPlan(t, st, true, false)
			want := append(gwPaths(2, 3), "/entry/102/event/1")
			sort.Strings(want)
			if strings.Join(hits, ",") != strings.Join(want, ",") {
				t.Fatalf("requests = %v, want %v", hits, want)
			}
			// Same with no refresh at all: the invalid file alone is fetched. The
			// refresh above confirmed GW2's live payload and re-downloaded its
			// picks afterwards, so GW2 counts as already finalised.
			markFinalisedAt(t, st, 2, time.Now().Add(-time.Hour))
			if err := st.WriteRaw(fetch.EntryEventPath(102, 1), []byte(body), false); err != nil {
				t.Fatal(err)
			}
			plan, hits := runPlan(t, st, false, false)
			if len(hits) != 1 || hits[0] != "/entry/102/event/1" || plan.Fetched != 1 || plan.Skipped != 8 {
				t.Fatalf("unforced run: hits=%v fetched/skipped=%d/%d", hits, plan.Fetched, plan.Skipped)
			}
		})
	}
}

func TestPlanLogsReasonForForcedRefetch(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	st := seedSeason(t)
	if err := st.WriteRaw(fetch.EntryEventPath(101, 1), []byte(`{"picks":[]}`), false); err != nil {
		t.Fatal(err)
	}
	if err := st.WriteRaw(fetch.EventLivePath(1), []byte(`{"elements":{}}`), false); err != nil {
		t.Fatal(err)
	}
	runPlan(t, st, false, false)
	out := buf.String()
	for _, want := range []string{"forcing refetch", "entry_event entry=101 gw=1", "picks has 0 entries, want 15", "event_live gw=1", "no elements"} {
		if !strings.Contains(out, want) {
			t.Fatalf("log %q missing %q", out, want)
		}
	}
}

func TestValidateEntryEventAcceptsFullSquad(t *testing.T) {
	if err := validateEntryEvent([]byte(validEntryEvent), false); err != nil {
		t.Fatalf("full squad rejected: %v", err)
	}
}

const gw1Fixtures = `"fixtures":[{"id":1,"finished":true}]`

func writeRaw(t *testing.T, st *store.JSONStore, rel, body string) {
	t.Helper()
	if err := st.WriteRaw(rel, []byte(body), false); err != nil {
		t.Fatal(err)
	}
}

// A live payload holding only some players used to settle the GW and give the
// missing roster players zero points.
func TestLiveCoverageOfPickedPlayers(t *testing.T) {
	subset := `{"elements":` + liveElementsFor(draftSquadSize-1) + `,` + gw1Fixtures + `}`
	full := `{"elements":` + liveElementsFor(draftSquadSize+5) + `,` + gw1Fixtures + `}`

	t.Run("subset missing a picked player is not settled and is refetched", func(t *testing.T) {
		st := seedSeason(t)
		writeRaw(t, st, fetch.EventLivePath(1), subset)
		if settled, _ := settledGWs(st, 3, planEntries); settled[1] {
			t.Fatal("GW1 settled on a live payload missing picked player 15")
		}
		_, hits := runPlan(t, st, false, false)
		if len(hits) != 1 || hits[0] != "/event/1/live" {
			t.Fatalf("unforced requests = %v, want only /event/1/live", hits)
		}
		raw, _ := st.ReadRaw(fetch.EventLivePath(1))
		if err := validateLivePayload(raw, requiredElements(st, planEntries, 1)); err != nil {
			t.Fatalf("refetched payload still incomplete: %v", err)
		}
	})

	t.Run("full coverage settles", func(t *testing.T) {
		st := seedSeason(t)
		writeRaw(t, st, fetch.EventLivePath(1), full)
		if settled, _ := settledGWs(st, 3, planEntries); !settled[1] {
			t.Fatal("GW1 not settled despite covering every picked player")
		}
		if _, hits := runPlan(t, st, false, false); len(hits) != 0 {
			t.Fatalf("complete payload refetched: %v", hits)
		}
	})

	t.Run("no entry-event files falls back to the payload-only check", func(t *testing.T) {
		st := seedSeason(t)
		writeRaw(t, st, fetch.EventLivePath(1), subset)
		if settled, _ := settledGWs(st, 3, []int{999}); !settled[1] {
			t.Fatal("GW1 with no cached picks should fall back to the non-empty check")
		}
	})

	t.Run("refetch reason names the missing players", func(t *testing.T) {
		var buf bytes.Buffer
		prev := slog.Default()
		slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
		t.Cleanup(func() { slog.SetDefault(prev) })
		st := seedSeason(t)
		writeRaw(t, st, fetch.EventLivePath(1), subset)
		runPlan(t, st, false, false)
		if out := buf.String(); !strings.Contains(out, "1 picked players missing from live payload (first: 15)") {
			t.Fatalf("log %q missing coverage reason", out)
		}
	})
}

func TestValidateLivePayloadStatValues(t *testing.T) {
	stats := func(minutes, total string) string {
		return `{"elements":{"1":{"stats":{"minutes":` + minutes + `,"total_points":` + total + `}}}}`
	}
	invalid := map[string]string{
		"null minutes":         stats(`null`, `6`),
		"null total_points":    stats(`90`, `null`),
		"string minutes":       stats(`"90"`, `6`),
		"string total_points":  stats(`90`, `"6"`),
		"object total_points":  stats(`90`, `{"v":6}`),
		"array minutes":        stats(`[90]`, `6`),
		"fractional points":    stats(`90`, `6.5`),
		"fractional minutes":   stats(`89.5`, `6`),
		"boolean total_points": stats(`90`, `true`),
	}
	for name, body := range invalid {
		t.Run(name, func(t *testing.T) {
			if err := validateLivePayload([]byte(body), nil); err == nil {
				t.Fatalf("accepted %s", body)
			}
		})
	}
	if err := validateLivePayload([]byte(stats(`0`, `-2`)), nil); err != nil {
		t.Fatalf("zero minutes / negative points are legitimate: %v", err)
	}
}

func TestValidateEntryEventPicks(t *testing.T) {
	picks := func(p string) string { return `{"picks":` + p + `}` }
	repeat := func(item string) string {
		return "[" + strings.TrimSuffix(strings.Repeat(item+",", draftSquadSize), ",") + "]"
	}
	// Valid squad with one pick replaced.
	withPick := func(i int, item string) string {
		parts := make([]string, draftSquadSize)
		for n := range parts {
			parts[n] = fmt.Sprintf(`{"element":%d,"position":%d}`, n+1, n+1)
		}
		parts[i] = item
		return "[" + strings.Join(parts, ",") + "]"
	}
	invalid := map[string]string{
		"15 nulls":               picks(repeat(`null`)),
		"15 empty objects":       picks(repeat(`{}`)),
		"wrongly typed picks":    picks(repeat(`"x"`)),
		"string element":         picks(withPick(0, `{"element":"1","position":1}`)),
		"zero element":           picks(withPick(3, `{"element":0,"position":4}`)),
		"negative element":       picks(withPick(3, `{"element":-5,"position":4}`)),
		"duplicate element":      picks(withPick(1, `{"element":1,"position":2}`)),
		"duplicate position":     picks(withPick(1, `{"element":99,"position":1}`)),
		"position zero":          picks(withPick(0, `{"element":1,"position":0}`)),
		"position out of range":  picks(withPick(14, `{"element":15,"position":16}`)),
		"missing position":       picks(withPick(2, `{"element":3}`)),
		"too many picks":         picks(squadPicks(draftSquadSize + 1)),
		"picks not an array":     picks(`{}`),
		"picks null":             picks(`null`),
		"malformed JSON":         `{"picks":[`,
		"empty picks, no waiver": picks(`[]`),
	}
	for name, body := range invalid {
		t.Run(name, func(t *testing.T) {
			if err := validateEntryEvent([]byte(body), false); err == nil {
				t.Fatalf("accepted %s", body)
			}
		})
	}
	if err := validateEntryEvent([]byte(picks(squadPicks(draftSquadSize))), false); err != nil {
		t.Fatalf("valid squad rejected: %v", err)
	}
	// allowEmpty relaxes only the no-picks case, not malformed or partial squads.
	for _, body := range []string{picks(`[]`), picks(`null`), `{"entry_history":{}}`} {
		if err := validateEntryEvent([]byte(body), true); err != nil {
			t.Fatalf("allowEmpty rejected %s: %v", body, err)
		}
	}
	for _, body := range []string{picks(repeat(`null`)), picks(squadPicks(3)), `{"picks":[`} {
		if err := validateEntryEvent([]byte(body), true); err == nil {
			t.Fatalf("allowEmpty accepted %s", body)
		}
	}
}

const emptyPicks = `{"entry_history":null,"picks":[],"subs":[]}`

// Entries in a league that started after GW1 have empty picks for earlier GWs;
// those immutable responses must not be refetched on every run.
func TestPlanDoesNotRefetchEmptyPicksBeforeLeagueStart(t *testing.T) {
	st := seedSeason(t)
	for _, id := range planEntries {
		writeRaw(t, st, fetch.EntryEventPath(id, 1), emptyPicks)
		writeRaw(t, st, fetch.EntryEventPath(id, 2), emptyPicks)
	}
	writeRaw(t, st, "league/7/details.json", `{"league":{"id":7,"start_event":3},"league_entries":[]}`)
	part := loadParticipation(st, 7, planEntries)

	plan, hits := runPlanWith(t, st, false, false, part)
	if len(hits) != 0 || plan.Skipped != 9 {
		t.Fatalf("pre-start empty picks refetched: hits=%v skipped=%d", hits, plan.Skipped)
	}

	// A forced refresh still skips settled GW1's empty picks.
	_, hits = runPlanWith(t, st, true, false, part)
	for _, h := range hits {
		if strings.HasSuffix(h, "/event/1") {
			t.Fatalf("settled pre-start GW1 entry refetched on refresh: %v", hits)
		}
	}
}

func TestPlanRefetchesEmptyPicksInParticipatingGW(t *testing.T) {
	st := seedSeason(t)
	writeRaw(t, st, fetch.EntryEventPath(101, 3), emptyPicks)
	writeRaw(t, st, "league/7/details.json", `{"league":{"id":7,"start_event":2}}`)
	part := loadParticipation(st, 7, planEntries)

	_, hits := runPlanWith(t, st, false, false, part)
	if len(hits) != 1 || hits[0] != "/entry/101/event/3" {
		t.Fatalf("requests = %v, want only the empty participating-GW picks", hits)
	}
}

// Without league context an empty picks response cannot be shown to be
// legitimate, so it is refetched (the pre-existing strict behaviour).
func TestPlanRefetchesEmptyPicksWhenStartUnknown(t *testing.T) {
	st := seedSeason(t)
	writeRaw(t, st, fetch.EntryEventPath(101, 1), emptyPicks)
	part := loadParticipation(st, 7, planEntries) // no details.json / history.json on disk
	if part.firstGW(101) != 0 {
		t.Fatalf("firstGW = %d, want unknown (0)", part.firstGW(101))
	}
	_, hits := runPlanWith(t, st, false, false, part)
	if len(hits) != 1 || hits[0] != "/entry/101/event/1" {
		t.Fatalf("requests = %v, want only /entry/101/event/1", hits)
	}
}

// A manager who joined mid-season: their own history starts at GW2 even though
// the league started at GW1, so only their GW1 empty picks are exempt.
func TestPlanLateJoinerEmptyPicksUseEntryHistory(t *testing.T) {
	st := seedSeason(t)
	writeRaw(t, st, fetch.EntryEventPath(101, 1), emptyPicks)
	writeRaw(t, st, fetch.EntryEventPath(102, 1), emptyPicks)
	writeRaw(t, st, "league/7/details.json", `{"league":{"id":7,"start_event":1}}`)
	writeRaw(t, st, "entry/102/history.json", `{"entry":102,"history":[{"event":2},{"event":3}]}`)
	part := loadParticipation(st, 7, planEntries)
	if part.firstGW(101) != 1 || part.firstGW(102) != 2 {
		t.Fatalf("firstGW = %d/%d, want 1/2", part.firstGW(101), part.firstGW(102))
	}
	_, hits := runPlanWith(t, st, false, false, part)
	if len(hits) != 1 || hits[0] != "/entry/101/event/1" {
		t.Fatalf("requests = %v, want only the league-start entry's empty GW1 picks", hits)
	}
}

func TestLoadParticipationEdgeCases(t *testing.T) {
	st := store.NewJSONStore(t.TempDir())
	writeRaw(t, st, "league/7/details.json", `{"league":`)
	writeRaw(t, st, "entry/101/history.json", `{"history":[{"event":5},{"event":4},{"event":0}]}`)
	writeRaw(t, st, "entry/102/history.json", `{"history":[]}`)
	part := loadParticipation(st, 7, planEntries)
	if part.leagueStart != 0 || part.firstGW(101) != 4 || part.firstGW(102) != 0 {
		t.Fatalf("participation = %+v (101 -> %d, 102 -> %d)", part, part.firstGW(101), part.firstGW(102))
	}
	if part.beforeStart(102, 1) || !part.beforeStart(101, 3) || part.beforeStart(101, 4) {
		t.Fatal("beforeStart boundary wrong")
	}
}

// agedGW1Picks makes GW1's entry files predate its finalisation marker (the
// marker is written now by the first run that sees GW1 settled).
func agedGW1Picks(t *testing.T, st *store.JSONStore) {
	t.Helper()
	if err := os.Remove(st.Path(entriesFinalPath(1))); err != nil {
		t.Fatal(err)
	}
	for _, id := range planEntries {
		ageFile(t, st, fetch.EntryEventPath(id, 1), 2*time.Hour)
	}
}

func hitsForGW(hits []string, gw int) []string {
	var out []string
	for _, h := range hits {
		if strings.HasSuffix(h, fmt.Sprintf("/event/%d", gw)) {
			out = append(out, h)
		}
	}
	return out
}

func TestPlanRefetchesPrefinalisationPicksOnceThenSkips(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	st := seedSeason(t)
	agedGW1Picks(t, st)

	// Settled GW1, no forced refresh: its picks predate finalisation, so both
	// are refetched — and nothing else of GW1 is.
	plan, hits := runPlan(t, st, false, false)
	want := []string{"/entry/101/event/1", "/entry/102/event/1"}
	if got := hitsForGW(hits, 1); strings.Join(got, ",") != strings.Join(want, ",") || len(hits) != 2 {
		t.Fatalf("first run requests = %v, want %v", hits, want)
	}
	if plan.Fetched != 2 {
		t.Fatalf("fetched = %d, want 2", plan.Fetched)
	}
	for _, want := range []string{"forcing refetch", "entry_event entry=101 gw=1", "picks fetched before GW finalised"} {
		if !strings.Contains(buf.String(), want) {
			t.Fatalf("log %q missing %q", buf.String(), want)
		}
	}

	// The marker was written once; the refetched files are newer, so a second
	// run (even with a forced refresh) skips GW1 entirely.
	marker, err := os.Stat(st.Path(entriesFinalPath(1)))
	if err != nil {
		t.Fatalf("marker missing: %v", err)
	}
	_, hits = runPlan(t, st, true, false)
	if got := hitsForGW(hits, 1); len(got) != 0 {
		t.Fatalf("GW1 refetched a second time: %v", got)
	}
	again, err := os.Stat(st.Path(entriesFinalPath(1)))
	if err != nil || !again.ModTime().Equal(marker.ModTime()) {
		t.Fatalf("marker rewritten: %v vs %v (err %v)", marker.ModTime(), again.ModTime(), err)
	}
}

func TestPlanSkipsPostFinalisationPicks(t *testing.T) {
	st := seedSeason(t) // marker an hour old, picks written afterwards
	_, hits := runPlan(t, st, true, false)
	if got := hitsForGW(hits, 1); len(got) != 0 {
		t.Fatalf("post-finalisation GW1 picks refetched: %v", got)
	}
}

func TestPlanRefetchAllUnchangedByFinalisationMarker(t *testing.T) {
	st := seedSeason(t)
	_, hits := runPlan(t, st, true, true)
	if strings.Join(hits, ",") != strings.Join(gwPaths(1, 2, 3), ",") {
		t.Fatalf("--refetch-all requests = %v, want %v", hits, gwPaths(1, 2, 3))
	}
	// Without a marker the full pull still happens once, and records it.
	agedGW1Picks(t, st)
	_, hits = runPlan(t, st, true, true)
	if strings.Join(hits, ",") != strings.Join(gwPaths(1, 2, 3), ",") {
		t.Fatalf("--refetch-all without marker requests = %v", hits)
	}
	if !st.Exists(entriesFinalPath(1)) {
		t.Fatal("refetch-all did not record GW1 finalisation")
	}
}

func TestPlanExemptsEmptyPrestartPicksFromFinalisationCheck(t *testing.T) {
	st := seedSeason(t)
	for _, id := range planEntries {
		writeRaw(t, st, fetch.EntryEventPath(id, 1), emptyPicks)
	}
	writeRaw(t, st, "league/7/details.json", `{"league":{"id":7,"start_event":2}}`)
	part := loadParticipation(st, 7, planEntries)
	agedGW1Picks(t, st)

	_, hits := runPlanWith(t, st, true, false, part)
	if got := hitsForGW(hits, 1); len(got) != 0 {
		t.Fatalf("pre-start empty picks refetched for finalisation: %v", got)
	}
	// GW1 is still reported settled.
	settled, err := settledGWs(st, 3, planEntries)
	if err != nil || !settled[1] {
		t.Fatalf("GW1 settled = %v, err %v", settled[1], err)
	}
}

// loadTotal scores entryID's cached picks for gw against its cached live file
// the way the points builder does.
func loadTotal(t *testing.T, st *store.JSONStore, entryID, gw int) int {
	t.Helper()
	rawPicks, err := st.ReadRaw(fetch.EntryEventPath(entryID, gw))
	if err != nil {
		t.Fatal(err)
	}
	var ev ledger.EntryEventRaw
	if err := json.Unmarshal(rawPicks, &ev); err != nil {
		t.Fatal(err)
	}
	rawLive, err := st.ReadRaw(fetch.EventLivePath(gw))
	if err != nil {
		t.Fatal(err)
	}
	var shaped struct {
		Elements map[string]struct {
			Stats points.LiveStats `json:"stats"`
		} `json:"elements"`
	}
	if err := json.Unmarshal(rawLive, &shaped); err != nil {
		t.Fatal(err)
	}
	byElement := map[int]points.LiveStats{}
	for id, el := range shaped.Elements {
		n, err := strconv.Atoi(id)
		if err != nil {
			t.Fatal(err)
		}
		byElement[n] = el.Stats
	}
	snap := ledger.BuildEntrySnapshot(7, entryID, gw, ev)
	return points.BuildResult(7, entryID, gw, snap, byElement).TotalPoints
}

// Regression: picks cached mid-GW lack the automatic substitution, so the XI
// scored 60; after settlement the refetched final picks move the bench player
// into the XI and the entry scores 69.
func TestPlanRefetchedFinalPicksIncludeAutoSub(t *testing.T) {
	st := store.NewJSONStore(t.TempDir())
	writeRaw(t, st, "bootstrap/bootstrap-static.json", `{"events":{"current":2,"next":3,"data":[{"id":1,"finished":true},{"id":2,"finished":false}]}}`)
	elements := make([]string, 0, draftSquadSize)
	for id := 1; id <= draftSquadSize; id++ {
		pts, mins := 6, 90
		switch id {
		case 1: // starter who never played
			pts, mins = 0, 0
		case 12: // first outfield bench player who came on
			pts = 9
		}
		elements = append(elements, fmt.Sprintf(`"%d":{"stats":{"minutes":%d,"total_points":%d}}`, id, mins, pts))
	}
	liveBody := `{"elements":{` + strings.Join(elements, ",") + `},"fixtures":[{"id":1,"finished":true}]}`
	writeRaw(t, st, fetch.EventLivePath(1), liveBody)

	midGW := `{"entry_history":{},"picks":` + squadPicks(draftSquadSize) + `,"subs":[]}`
	finalPicks := make([]string, 0, draftSquadSize)
	for pos := 1; pos <= draftSquadSize; pos++ {
		el := pos
		switch pos { // the auto-sub swaps player 1 (pos 1) with player 12 (pos 12)
		case 1:
			el = 12
		case 12:
			el = 1
		}
		finalPicks = append(finalPicks, fmt.Sprintf(`{"element":%d,"position":%d}`, el, pos))
	}
	finalBody := `{"entry_history":{},"picks":[` + strings.Join(finalPicks, ",") + `],"subs":[{"element_in":12,"element_out":1,"event":1}]}`

	writeRaw(t, st, fetch.EntryEventPath(101, 1), midGW)
	ageFile(t, st, fetch.EntryEventPath(101, 1), 2*time.Hour)
	if got := loadTotal(t, st, 101, 1); got != 60 {
		t.Fatalf("mid-GW total = %d, want 60", got)
	}

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/live") {
			fmt.Fprint(w, liveBody)
			return
		}
		fmt.Fprint(w, finalBody)
	}))
	t.Cleanup(srv.Close)
	client := fetch.NewClient(st)
	client.HTTP, client.BaseURL, client.Sleep = srv.Client(), srv.URL, 0

	for run := 1; run <= 2; run++ {
		settled, err := settledGWs(st, 2, []int{101})
		if err != nil || !settled[1] {
			t.Fatalf("run %d: GW1 settled=%v err=%v", run, settled[1], err)
		}
		plan := planGWFetches(client, []int{101}, 1, 1, false, false, settled, participation{})
		wantFetched := map[int]int{1: 1, 2: 0}[run]
		if plan.Fetched != wantFetched {
			t.Fatalf("run %d: fetched %d, want %d", run, plan.Fetched, wantFetched)
		}
		if err := runFetchTasks(plan.Tasks, 1); err != nil {
			t.Fatal(err)
		}
	}
	if got := loadTotal(t, st, 101, 1); got != 69 {
		t.Fatalf("final total = %d, want 69 (substitute included)", got)
	}
}
