package main

import (
	"bytes"
	"fmt"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/pulse"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

// squad builds 11 uniquely named players for a club so a full XI resolves.
func squad(club string, teamID int) (elems, lineup string) {
	var e, l []string
	for n := 0; n < 11; n++ {
		name := fmt.Sprintf("%sPlayer%c", club, 'A'+n)
		id := teamID*100 + n
		e = append(e, fmt.Sprintf(`{"id":%d,"first_name":"F","second_name":%q,"web_name":%q,"team":%d}`, id, name, name, teamID))
		l = append(l, fmt.Sprintf(`{"id":%d,"name":{"display":%q,"first":"F","last":%q}}`, id, name, name))
	}
	return strings.Join(e, ","), strings.Join(l, ",")
}

func bootstrapJSON() string {
	arsE, _ := squad("ARS", 1)
	cheE, _ := squad("CHE", 2)
	return `{"teams":[{"id":1,"short_name":"ARS"},{"id":2,"short_name":"CHE"}],"elements":[` + arsE + "," + cheE + `]}`
}

// fakePulse serves a single upcoming fixture (id 99) kicking off at ko, with
// real or null team sheets depending on lineups.
func fakePulse(t *testing.T, ko time.Time, lineups bool) *pulse.Client {
	t.Helper()
	lists := `"teamLists":[null,null]`
	if lineups {
		_, arsL := squad("ARS", 1)
		_, cheL := squad("CHE", 2)
		lists = fmt.Sprintf(`"teamLists":[{"teamId":1,"lineup":[%s],"substitutes":[]},{"teamId":2,"lineup":[%s],"substitutes":[]}]`, arsL, cheL)
	}
	fixture := fmt.Sprintf(`{"id":99,"status":"U","kickoff":{"millis":%d},
"teams":[{"team":{"id":1,"club":{"abbr":"ARS"}}},{"team":{"id":2,"club":{"abbr":"CHE"}}}],%s,"events":[]}`,
		ko.UnixMilli(), lists)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasPrefix(r.URL.Path, "/competitions/1/compseasons"):
			fmt.Fprint(w, `{"content":[{"id":7}]}`)
		case r.URL.Path == "/fixtures":
			if strings.Contains(r.URL.RawQuery, "statuses=U,L") {
				fmt.Fprintf(w, `{"content":[%s]}`, fixture)
				return
			}
			fmt.Fprint(w, `{"content":[]}`)
		case r.URL.Path == "/fixtures/99":
			fmt.Fprint(w, fixture)
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	return &pulse.Client{HTTP: srv.Client(), Base: srv.URL}
}

// runRefresh runs refreshSquads against a fresh store (optionally seeded with
// an existing GW6 squads.json) and returns the captured log output.
func runRefresh(t *testing.T, c *pulse.Client, existing string, now time.Time) (string, *store.JSONStore) {
	t.Helper()
	st := store.NewJSONStore(t.TempDir())
	if err := st.WriteRaw("bootstrap/bootstrap-static.json", []byte(bootstrapJSON()), false); err != nil {
		t.Fatal(err)
	}
	if existing != "" {
		if err := st.WriteRaw("gw/6/squads.json", []byte(existing), false); err != nil {
			t.Fatal(err)
		}
	}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	refreshSquads(c, st, 6, now)
	return buf.String(), st
}

func TestRefreshSquadsLogsSuccessWithCounts(t *testing.T) {
	now := time.Now()
	out, _ := runRefresh(t, fakePulse(t, now.Add(time.Hour), true), "", now)
	t.Log(strings.TrimSpace(out))
	if !strings.Contains(out, "team sheets refreshed: GW 6 (1 fixtures in window, 1 with lineups)") {
		t.Fatalf("missing success-with-counts log: %q", out)
	}
}

func TestRefreshSquadsNoFixturesInWindow(t *testing.T) {
	now := time.Now()
	const existing = `{"gw":6,"fixtures":[{"pulse_id":5}]}`
	out, st := runRefresh(t, fakePulse(t, now.Add(72*time.Hour), true), existing, now)
	t.Log(strings.TrimSpace(out))
	if strings.Contains(out, "team sheets refreshed") {
		t.Fatalf("empty window must not log success: %q", out)
	}
	if !strings.Contains(out, "no fixtures in team-sheet window") {
		t.Fatalf("missing no-fixtures info log: %q", out)
	}
	got, err := os.ReadFile(filepath.Join(st.Root, "gw/6/squads.json"))
	if err != nil || string(got) != existing {
		t.Fatalf("existing squads.json clobbered: %q (err %v)", got, err)
	}
}

func TestRefreshSquadsAllNullLineupsWarns(t *testing.T) {
	now := time.Now()
	out, _ := runRefresh(t, fakePulse(t, now.Add(time.Hour), false), "", now)
	t.Log(strings.TrimSpace(out))
	if strings.Contains(out, "team sheets refreshed") {
		t.Fatalf("all-null lineups must not log success: %q", out)
	}
	if !strings.Contains(out, "WARN team sheets: 1 fixtures in window but no lineups published") {
		t.Fatalf("missing warn log: %q", out)
	}
}
