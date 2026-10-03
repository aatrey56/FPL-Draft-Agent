package pulse

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

// fakeFx describes one fixture served by the fake pulse feed. Home is club
// ARS (team 1) and away is CHE (team 2); each fields 11 players named
// "<Club>One".."<Club>Eleven" style (see playerName).
type fakeFx struct {
	id      int
	ko      time.Time
	lineups bool // false: teamLists are null (sheets not published)
	goal    bool // one goal by the first ARS player at 10'
}

var clubs = []string{"ARS", "CHE"}

func playerName(club string, n int) string { return fmt.Sprintf("%sPlayer%c", club, 'A'+n) }

// testBootstrap returns a bootstrap-static slice with 11 uniquely named
// players per club so a full XI resolves.
func testBootstrap() string {
	var teams, elems []string
	for ti, club := range clubs {
		teams = append(teams, fmt.Sprintf(`{"id":%d,"short_name":%q}`, ti+1, club))
		for n := 0; n < 11; n++ {
			elems = append(elems, fmt.Sprintf(`{"id":%d,"first_name":"F","second_name":%q,"web_name":%q,"team":%d}`,
				ti*100+n+1, playerName(club, n), playerName(club, n), ti+1))
		}
	}
	return fmt.Sprintf(`{"teams":[%s],"elements":[%s]}`, strings.Join(teams, ","), strings.Join(elems, ","))
}

func (f fakeFx) json() string {
	lists := `"teamLists":[null,null]`
	if f.lineups {
		var sides []string
		for ti, club := range clubs {
			var xi []string
			for n := 0; n < 11; n++ {
				xi = append(xi, fmt.Sprintf(`{"id":%d,"name":{"display":%q,"first":"F","last":%q}}`,
					ti*100+n+1, playerName(club, n), playerName(club, n)))
			}
			sides = append(sides, fmt.Sprintf(`{"teamId":%d,"lineup":[%s],"substitutes":[]}`, ti+1, strings.Join(xi, ",")))
		}
		lists = `"teamLists":[` + strings.Join(sides, ",") + `]`
	}
	events := ""
	if f.goal {
		events = `{"type":"G","personId":1,"clock":{"secs":600,"label":"10'00"}}`
	}
	return fmt.Sprintf(`{"id":%d,"status":"U","kickoff":{"millis":%d},
"teams":[{"team":{"id":1,"club":{"abbr":"ARS"}}},{"team":{"id":2,"club":{"abbr":"CHE"}}}],%s,"events":[%s]}`,
		f.id, f.ko.UnixMilli(), lists, events)
}

// newFakePulse serves the given fixtures as upcoming. The returned pointer
// lets a test change what the feed serves between runs.
func newFakePulse(t *testing.T, fxs *[]fakeFx) *Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/competitions/1/compseasons"):
			fmt.Fprint(w, `{"content":[{"id":7}]}`)
		case r.URL.Path == "/fixtures":
			var list []string
			if strings.Contains(r.URL.RawQuery, "statuses=U,L") {
				for _, f := range *fxs {
					list = append(list, f.json())
				}
			}
			fmt.Fprintf(w, `{"content":[%s]}`, strings.Join(list, ","))
		case strings.HasPrefix(r.URL.Path, "/fixtures/"):
			for _, f := range *fxs {
				if r.URL.Path == fmt.Sprintf("/fixtures/%d", f.id) {
					fmt.Fprint(w, f.json())
					return
				}
			}
			http.NotFound(w, r)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return &Client{HTTP: srv.Client(), Base: srv.URL}
}

func newStore(t *testing.T) *store.JSONStore {
	t.Helper()
	st := store.NewJSONStore(t.TempDir())
	if err := st.WriteRaw("bootstrap/bootstrap-static.json", []byte(testBootstrap()), false); err != nil {
		t.Fatal(err)
	}
	return st
}

// readFixtureIDs returns the pulse ids stored in gw/6/squads.json, plus
// whether each has a full set of sheets.
func readFixtureIDs(t *testing.T, st *store.JSONStore) map[int]bool {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(st.Root, "gw/6/squads.json"))
	if err != nil {
		t.Fatal(err)
	}
	var f File
	if err := json.Unmarshal(body, &f); err != nil {
		t.Fatal(err)
	}
	ids := map[int]bool{}
	for _, fs := range f.Fixtures {
		ids[fs.PulseID] = fs.complete()
	}
	return ids
}

func TestRefreshSquadsCountsLineups(t *testing.T) {
	now := time.Now()
	fxs := []fakeFx{{id: 1, ko: now.Add(time.Hour), lineups: true}}
	res, err := RefreshSquads(newFakePulse(t, &fxs), newStore(t), 6, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 1 || res.WithLineups != 1 {
		t.Fatalf("got %+v, want 1 in window / 1 with lineups", res)
	}
}

func TestRefreshSquadsNullLineupsCountedInWindowOnly(t *testing.T) {
	now := time.Now()
	fxs := []fakeFx{{id: 1, ko: now.Add(time.Hour), lineups: false}}
	res, err := RefreshSquads(newFakePulse(t, &fxs), newStore(t), 6, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 1 || res.WithLineups != 0 {
		t.Fatalf("got %+v, want 1 in window / 0 with lineups", res)
	}
}

func TestRefreshSquadsNoFixturesInWindowKeepsExistingFile(t *testing.T) {
	now := time.Now()
	st := newStore(t)
	const existing = `{"gw":6,"fixtures":[{"pulse_id":5}]}`
	if err := st.WriteRaw("gw/6/squads.json", []byte(existing), false); err != nil {
		t.Fatal(err)
	}
	// Kickoff three days out: outside the now-12h..now+2h window.
	fxs := []fakeFx{{id: 1, ko: now.Add(72 * time.Hour), lineups: true}}
	res, err := RefreshSquads(newFakePulse(t, &fxs), st, 6, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 0 || res.WithLineups != 0 {
		t.Fatalf("got %+v, want zeros", res)
	}
	got, err := os.ReadFile(filepath.Join(st.Root, "gw/6/squads.json"))
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != existing {
		t.Fatalf("squads.json was overwritten: %s", got)
	}
}

// Regression: an in-window fixture whose sheet is not published yet must not
// wipe the sheets already stored for earlier fixtures.
func TestRefreshSquadsNullLineupsPreserveEarlierFixtures(t *testing.T) {
	now := time.Now()
	st := newStore(t)
	fxs := []fakeFx{{id: 1, ko: now.Add(-time.Hour), lineups: true}}
	c := newFakePulse(t, &fxs)
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}

	// Later: fixture 1 has left the feed's upcoming list; fixture 2 is in the
	// window with null lineups (e.g. Monday 20:00 match, run at 18:30).
	fxs = []fakeFx{{id: 2, ko: now.Add(time.Hour), lineups: false}}
	res, err := RefreshSquads(c, st, 6, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 1 || res.WithLineups != 0 {
		t.Fatalf("got %+v, want 1 in window / 0 with lineups", res)
	}
	ids := readFixtureIDs(t, st)
	if complete, ok := ids[1]; !ok || !complete {
		t.Fatalf("earlier fixture 1 lost or incomplete: %v", ids)
	}
}

// Regression: a transient null team list for a fixture we already hold sheets
// for must keep the cached sheet.
func TestRefreshSquadsTransientNullKeepsCachedSheet(t *testing.T) {
	now := time.Now()
	st := newStore(t)
	fxs := []fakeFx{{id: 1, ko: now.Add(time.Hour), lineups: true}}
	c := newFakePulse(t, &fxs)
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}

	fxs[0].lineups = false
	res, err := RefreshSquads(c, st, 6, now)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 1 || res.WithLineups != 1 {
		t.Fatalf("got %+v, want cached sheet to count (1/1)", res)
	}
	if complete, ok := readFixtureIDs(t, st)[1]; !ok || !complete {
		t.Fatal("cached sheet for fixture 1 was dropped")
	}
}

// Carry-over across runs: run 1 captures fixture A, run 2 only has fixture B
// in the window; the file must hold both.
func TestRefreshSquadsCarriesOverAcrossRuns(t *testing.T) {
	now := time.Now()
	st := newStore(t)
	fxs := []fakeFx{{id: 1, ko: now.Add(time.Hour), lineups: true}}
	c := newFakePulse(t, &fxs)
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}

	later := now.Add(24 * time.Hour) // fixture 1 is now outside the window
	fxs = []fakeFx{
		{id: 1, ko: now.Add(time.Hour), lineups: true},
		{id: 2, ko: later.Add(time.Hour), lineups: true},
	}
	res, err := RefreshSquads(c, st, 6, later)
	if err != nil {
		t.Fatal(err)
	}
	if res.InWindow != 1 || res.WithLineups != 1 {
		t.Fatalf("got %+v, want 1/1 (only fixture 2 in window)", res)
	}
	ids := readFixtureIDs(t, st)
	if len(ids) != 2 || !ids[1] || !ids[2] {
		t.Fatalf("want both fixtures complete, got %v", ids)
	}
}

func readEventCount(t *testing.T, st *store.JSONStore) int {
	t.Helper()
	body, err := os.ReadFile(filepath.Join(st.Root, "gw/6/match_events.json"))
	if err != nil {
		t.Fatal(err)
	}
	var f EventsFile
	if err := json.Unmarshal(body, &f); err != nil {
		t.Fatal(err)
	}
	return len(f.Events)
}

// A goal overturned by VAR: the fresh fetch has sheets and zero events, so the
// stored goal must go. A fetch without sheets must not touch events.
func TestRefreshSquadsFreshEmptyEventsReplaceStored(t *testing.T) {
	now := time.Now()
	st := newStore(t)
	fxs := []fakeFx{{id: 1, ko: now.Add(-time.Hour), lineups: true, goal: true}}
	c := newFakePulse(t, &fxs)
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}
	if n := readEventCount(t, st); n != 1 {
		t.Fatalf("want 1 stored goal, got %d", n)
	}

	fxs[0].lineups, fxs[0].goal = false, false // fetch without sheets: keep
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}
	if n := readEventCount(t, st); n != 1 {
		t.Fatalf("sheetless fetch must keep stored events, got %d", n)
	}

	fxs[0].lineups = true // sheets present, goal overturned
	if _, err := RefreshSquads(c, st, 6, now); err != nil {
		t.Fatal(err)
	}
	if n := readEventCount(t, st); n != 0 {
		t.Fatalf("overturned goal must be removed, got %d events", n)
	}
}

// A corrupt existing file is an error and is left byte-identical.
func TestRefreshSquadsCorruptExistingFileIsNotOverwritten(t *testing.T) {
	for _, rel := range []string{"gw/6/squads.json", "gw/6/match_events.json"} {
		t.Run(rel, func(t *testing.T) {
			now := time.Now()
			st := newStore(t)
			const corrupt = `{"gw":6,"fixtures":[{"pulse_id":`
			if err := st.WriteRaw(rel, []byte(corrupt), false); err != nil {
				t.Fatal(err)
			}
			fxs := []fakeFx{{id: 1, ko: now.Add(time.Hour), lineups: true}}
			if _, err := RefreshSquads(newFakePulse(t, &fxs), st, 6, now); err == nil {
				t.Fatal("expected an error for corrupt existing file")
			}
			got, err := os.ReadFile(filepath.Join(st.Root, rel))
			if err != nil || string(got) != corrupt {
				t.Fatalf("corrupt file modified: %q (err %v)", got, err)
			}
		})
	}
}
