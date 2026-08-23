package pulse

// pulse.go — the fetch/orchestration half. RefreshSquads is called from the
// fetcher (cmd/dev), never from MCP tools: tools stay local-file-only. Team
// sheets are immutable once published, so fixtures whose sheets are already
// stored are never refetched — steady-state cost is two list calls.

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

const DefaultBase = "https://footballapi.pulselive.com/football"

type Client struct {
	HTTP *http.Client
	Base string
}

func NewClient() *Client {
	return &Client{HTTP: &http.Client{Timeout: 15 * time.Second}, Base: DefaultBase}
}

func (c *Client) getJSON(path string, v any) error {
	req, err := http.NewRequest(http.MethodGet, c.Base+path, nil)
	if err != nil {
		return err
	}
	// The feed rejects requests without a premierleague.com origin.
	req.Header.Set("Origin", "https://www.premierleague.com")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("pulse %s: HTTP %d", path, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	return json.Unmarshal(body, v)
}

type fixtureJSON struct {
	ID     float64 `json:"id"`
	Status string  `json:"status"` // U upcoming, L live, C completed
	Teams  []struct {
		Team struct {
			ID   float64 `json:"id"`
			Club struct {
				Abbr string `json:"abbr"`
			} `json:"club"`
		} `json:"team"`
	} `json:"teams"`
	Kickoff struct {
		Millis float64 `json:"millis"` // arrives in scientific notation
	} `json:"kickoff"`
	TeamLists []*struct {
		TeamID      float64       `json:"teamId"`
		Lineup      []pulsePlayer `json:"lineup"`
		Substitutes []pulsePlayer `json:"substitutes"`
	} `json:"teamLists"`
	Events []struct {
		Type     string   `json:"type"` // G goal, B booking, others ignored
		PersonID *float64 `json:"personId"`
		AssistID *float64 `json:"assistId"`
		TeamID   *float64 `json:"teamId"`
		Desc     string   `json:"description"` // for B: Y or R
		Clock    struct {
			Secs  float64 `json:"secs"`
			Label string  `json:"label"` // "55'00", "90+3'00"
		} `json:"clock"`
	} `json:"events"`
}

type pulsePlayer struct {
	ID   float64 `json:"id"` // pulse person id, matches event personId
	Name struct {
		Display string `json:"display"`
		First   string `json:"first"`
		Last    string `json:"last"`
	} `json:"name"`
}

// Sheet is one club's resolved team sheet.
type Sheet struct {
	XI        []int    `json:"xi"`
	Bench     []int    `json:"bench"`
	Unmatched []string `json:"unmatched,omitempty"`
}

// FixtureSquads is one fixture's sheets, keyed by FPL club short name.
type FixtureSquads struct {
	PulseID    int              `json:"pulse_id"`
	Home       string           `json:"home,omitempty"`
	Away       string           `json:"away,omitempty"`
	KickoffUTC string           `json:"kickoff_utc,omitempty"`
	Sheets     map[string]Sheet `json:"sheets"`
}

// File is the on-disk gw/<n>/squads.json payload.
type File struct {
	GW       int             `json:"gw"`
	Fetched  string          `json:"fetched"`
	Fixtures []FixtureSquads `json:"fixtures"`
}

// MatchEvent is one resolved on-pitch event (goal, assist, card) with its
// exact game minute and real wall-clock time.
type MatchEvent struct {
	Element int    `json:"element"` // FPL element id
	Kind    string `json:"kind"`    // G goal, A assist, Y yellow, R red
	Minute  string `json:"minute"`  // "55'", "90+3'"
	Secs    int    `json:"secs"`    // seconds into the match, for ordering
	UTC     string `json:"utc"`     // real time the event occurred (RFC3339)
	Club    string `json:"club"`
	PulseID int    `json:"fx"`    // source fixture, for caching
	Final   bool   `json:"final"` // fixture completed — events won't change
}

// EventsFile is the on-disk gw/<n>/match_events.json payload.
type EventsFile struct {
	GW      int          `json:"gw"`
	Fetched string       `json:"fetched"`
	Events  []MatchEvent `json:"events"`
}

// complete reports whether both sides' sheets are stored with a full XI.
func (f FixtureSquads) complete() bool {
	n := 0
	for _, s := range f.Sheets {
		if len(s.XI) == 11 {
			n++
		}
	}
	return n == 2
}

// bootstrapSlice is the piece of bootstrap-static the matcher needs.
type bootstrapSlice struct {
	Elements []struct {
		ID         int    `json:"id"`
		FirstName  string `json:"first_name"`
		SecondName string `json:"second_name"`
		WebName    string `json:"web_name"`
		Team       int    `json:"team"`
	} `json:"elements"`
	Teams []struct {
		ID        int    `json:"id"`
		ShortName string `json:"short_name"`
	} `json:"teams"`
}

// RefreshSquads fetches team sheets for fixtures kicking off within the
// window around now and writes gw/<gw>/squads.json. Best-effort by design:
// callers log the error and move on — a pulse outage must never break the
// FPL refresh path.
func RefreshSquads(c *Client, st *store.JSONStore, gw int, now time.Time) error {
	raw, err := st.ReadRaw("bootstrap/bootstrap-static.json")
	if err != nil {
		return fmt.Errorf("bootstrap not on disk yet: %w", err)
	}
	var boot bootstrapSlice
	if err := json.Unmarshal(raw, &boot); err != nil {
		return err
	}
	elemsByClub := map[string][]Element{}
	clubByID := map[int]string{}
	for _, t := range boot.Teams {
		clubByID[t.ID] = t.ShortName
	}
	for _, e := range boot.Elements {
		club := clubByID[e.Team]
		elemsByClub[club] = append(elemsByClub[club], Element{e.ID, e.FirstName, e.SecondName, e.WebName})
	}

	// Sheets already stored never change — carry them over. Match events are
	// cached only once a fixture is final (completed); live fixtures always
	// refetch so the clock advances.
	rel := fmt.Sprintf("gw/%d/squads.json", gw)
	evRel := fmt.Sprintf("gw/%d/match_events.json", gw)
	stored := map[int]FixtureSquads{}
	var prev File
	if body, err := st.ReadRaw(rel); err == nil && json.Unmarshal(body, &prev) == nil {
		for _, fs := range prev.Fixtures {
			if fs.complete() {
				stored[fs.PulseID] = fs
			}
		}
	}
	storedEvents := map[int][]MatchEvent{} // pulse fixture id -> final events
	var prevEv EventsFile
	if body, err := st.ReadRaw(evRel); err == nil && json.Unmarshal(body, &prevEv) == nil {
		for _, e := range prevEv.Events {
			if e.Final {
				storedEvents[e.PulseID] = append(storedEvents[e.PulseID], e)
			}
		}
	}

	var cs struct {
		Content []struct {
			ID float64 `json:"id"`
		} `json:"content"`
	}
	if err := c.getJSON("/competitions/1/compseasons?pageSize=1", &cs); err != nil {
		return err
	}
	if len(cs.Content) == 0 {
		return fmt.Errorf("no compseason")
	}
	season := int(cs.Content[0].ID)

	var relevant []fixtureJSON
	for _, q := range []string{
		fmt.Sprintf("/fixtures?comps=1&compSeasons=%d&statuses=U,L&pageSize=20&page=0&sort=asc", season),
		fmt.Sprintf("/fixtures?comps=1&compSeasons=%d&statuses=C&pageSize=15&page=0&sort=desc", season),
	} {
		var list struct {
			Content []fixtureJSON `json:"content"`
		}
		if err := c.getJSON(q, &list); err != nil {
			return err
		}
		relevant = append(relevant, list.Content...)
	}

	out := File{GW: gw, Fetched: now.UTC().Format(time.RFC3339)}
	evOut := EventsFile{GW: gw, Fetched: now.UTC().Format(time.RFC3339)}
	lo, hi := now.Add(-12*time.Hour), now.Add(2*time.Hour)
	for _, f := range relevant {
		ko := time.UnixMilli(int64(f.Kickoff.Millis)).UTC()
		if ko.Before(lo) || ko.After(hi) || len(f.Teams) != 2 {
			continue
		}
		id := int(f.ID)
		sheetCached, sheetOK := stored[id]
		evCached, evOK := storedEvents[id]
		// Reuse only when the fixture is final and both are cached; live
		// fixtures always refetch so goals/cards keep flowing.
		if f.Status == "C" && sheetOK && evOK {
			out.Fixtures = append(out.Fixtures, sheetCached)
			evOut.Events = append(evOut.Events, evCached...)
			continue
		}
		var detail fixtureJSON
		if err := c.getJSON(fmt.Sprintf("/fixtures/%d", id), &detail); err != nil {
			if sheetOK { // keep what we had on a transient error
				out.Fixtures = append(out.Fixtures, sheetCached)
				evOut.Events = append(evOut.Events, evCached...)
			}
			continue
		}
		abbrByTeam := map[int]string{}
		for _, t := range f.Teams {
			abbrByTeam[int(t.Team.ID)] = t.Team.Club.Abbr
		}
		fs := FixtureSquads{
			PulseID: id,
			Home:    f.Teams[0].Team.Club.Abbr, Away: f.Teams[1].Team.Club.Abbr,
			KickoffUTC: ko.Format(time.RFC3339),
			Sheets:     map[string]Sheet{},
		}
		personElem := map[int]int{}    // pulse person id -> FPL element
		personClub := map[int]string{} // pulse person id -> club short name
		for _, side := range detail.TeamLists {
			if side == nil {
				continue
			}
			club := abbrByTeam[int(side.TeamID)]
			pool := elemsByClub[club]
			all := append(append([]pulsePlayer{}, side.Lineup...), side.Substitutes...)
			names := make([]Name, len(all))
			for i, p := range all {
				names[i] = Name{p.Name.Display, p.Name.First, p.Name.Last}
			}
			resolved := ResolveNames(names, pool)
			for i, p := range all {
				if resolved[i] != 0 {
					personElem[int(p.ID)] = resolved[i]
					personClub[int(p.ID)] = club
				}
			}
			sheet := Sheet{}
			var miss1, miss2 []string
			sheet.XI, miss1 = MatchNames(names[:len(side.Lineup)], pool)
			sheet.Bench, miss2 = MatchNames(names[len(side.Lineup):], pool)
			sheet.Unmatched = append(miss1, miss2...)
			fs.Sheets[club] = sheet
		}
		if len(fs.Sheets) > 0 {
			out.Fixtures = append(out.Fixtures, fs)
		}

		// Resolve match events to FPL elements with exact minute + real time.
		final := detail.Status == "C"
		add := func(personID int, kind string, secs float64, label string) {
			elem, ok := personElem[personID]
			if !ok {
				return
			}
			evOut.Events = append(evOut.Events, MatchEvent{
				Element: elem, Kind: kind, Minute: minuteLabel(label), Secs: int(secs),
				UTC:  ko.Add(time.Duration(secs) * time.Second).Format(time.RFC3339),
				Club: personClub[personID], PulseID: id, Final: final,
			})
		}
		for _, e := range detail.Events {
			switch e.Type {
			case "G":
				if e.PersonID != nil {
					add(int(*e.PersonID), "G", e.Clock.Secs, e.Clock.Label)
				}
				if e.AssistID != nil {
					add(int(*e.AssistID), "A", e.Clock.Secs, e.Clock.Label)
				}
			case "B":
				if e.PersonID != nil {
					kind := "Y"
					if e.Desc == "R" {
						kind = "R"
					}
					add(int(*e.PersonID), kind, e.Clock.Secs, e.Clock.Label)
				}
			}
		}
	}
	sort.Slice(evOut.Events, func(a, b int) bool { return evOut.Events[a].Secs < evOut.Events[b].Secs })

	body, err := json.MarshalIndent(out, "", " ")
	if err != nil {
		return err
	}
	if err := st.WriteRaw(rel, body, false); err != nil {
		return err
	}
	evBody, err := json.MarshalIndent(evOut, "", " ")
	if err != nil {
		return err
	}
	return st.WriteRaw(evRel, evBody, false)
}

// minuteLabel turns a pulse clock label ("55'00", "90+3'00") into a display
// minute ("55'", "90+3'").
func minuteLabel(label string) string {
	if i := strings.Index(label, "'"); i >= 0 {
		return label[:i] + "'"
	}
	return label
}
