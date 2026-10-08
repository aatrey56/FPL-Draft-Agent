package main

// Tests for the per-GW fetch plan. Fixtures in t.TempDir() and an httptest
// server standing in for the draft API — no live calls.

import (
	"bytes"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/fetch"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

var planEntries = []int{101, 102}

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
	confirmed := `{"elements":{},"fixtures":[{"id":1,"finished":true,"finished_provisional":true},{"id":2,"finished":true,"finished_provisional":true}]}`
	provisional := `{"elements":{},"fixtures":[{"id":3,"finished":true,"finished_provisional":true},{"id":4,"finished":false,"finished_provisional":true}]}`
	write(fetch.EventLivePath(1), confirmed)
	write(fetch.EventLivePath(2), provisional)
	write(fetch.EventLivePath(3), confirmed)
	for gw := 1; gw <= 3; gw++ {
		for _, id := range planEntries {
			write(fetch.EntryEventPath(id, gw), `{"entry_history":{},"picks":[],"subs":[]}`)
		}
	}
	return st
}

// fakeDraftAPI answers every GET with an empty JSON object and records the paths.
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
		fmt.Fprint(w, `{}`)
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
	client, hits := fakeDraftAPI(t, st)
	settled, err := settledGWs(st, 3)
	if err != nil {
		t.Fatal(err)
	}
	plan := planGWFetches(client, planEntries, 1, 3, refresh, refetchAll, settled)
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
	settled, err := settledGWs(seedSeason(t), 3)
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
	if settled, _ := settledGWs(st, 0); len(settled) != 0 {
		t.Fatalf("current_event 0 settled %v", settled)
	}
	// A live file listing no fixtures never settles.
	if err := st.WriteRaw(fetch.EventLivePath(1), []byte(`{"elements":{}}`), false); err != nil {
		t.Fatal(err)
	}
	if settled, _ := settledGWs(st, 3); settled[1] {
		t.Fatal("GW1 settled without any fixtures in its live file")
	}
	// No bootstrap: error (main then plans every GW as unsettled).
	if _, err := settledGWs(store.NewJSONStore(t.TempDir()), 3); err == nil {
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
