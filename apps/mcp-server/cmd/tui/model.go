package main

// Data loading + bubbletea model. The pipeline stays pure — load() reads
// local snapshots into a snapshot struct, view.go formats it — and the parse
// happens inside a tea.Cmd (its own goroutine), never on the event loop.
// An mtime gate makes an unchanged tick cost a handful of stats, not a parse.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
)

type playerRow struct {
	Slot    int
	Name    string
	Pos     string
	Team    string
	TeamID  int
	Avail   string // bootstrap status: a/d/i/s/u
	Minutes int
	Points  int
	Starter bool
	Glyph   string // ● live, ✓ played, – DNP, ○ not yet, ⚠ doubt, · bench
}

type side struct {
	Name    string
	Manager string
	EntryID int
	Total   int
	Bench   int
	Played  int
	Players []playerRow
}

type matchup struct{ A, B side }

type standingRow struct {
	Name   string
	Record string
	Total  int
	Mine   bool
}

type railItem struct {
	Glyph string // ⚠ availability, ◇ blank, ? unprojected, ↑ wire
	Name  string
	Team  string
	Note  string
}

type fixtureRow struct {
	Home, Away string
	HS, AS     int
	Started    bool
	Finished   bool
	Minutes    int
	Kickoff    time.Time
}

type txRow struct {
	TeamName string
	In, Out  string
	Accepted bool
}

type clubPlayer struct {
	Name    string
	Pos     string
	Minutes int
	Points  int
	Start   bool
}

type matchDetail struct {
	Home, Away         string
	HS, AS, Minute     int
	HomeForm, AwayForm string
	HomeXI, AwayXI     []clubPlayer
	HomeSubs, AwaySubs []clubPlayer
}

type snapshot struct {
	GW           int
	Matchups     []matchup
	Loaded       time.Time
	NextDue      string
	Standings    []standingRow
	Fixtures     []fixtureRow
	Transactions []txRow
	NeedsYou     []railItem
	Matches      []matchDetail // one per in-play fixture, liveSel-aligned
}

type model struct {
	dir       string // season raw dir
	derived   string // season derived dir
	league    int
	entry     int
	gwArg     int
	snap      snapshot
	selected  int
	focus     int // 0 = matchup, 1 = live games, 2 = rail
	liveSel   int
	matchView bool
	w, h      int
	loadErr   string
	status    string
	fetching  bool
	loading   bool
	stamp     string // mtime fingerprint of the inputs at last load
}

func newModel(dir, derived string, league, entry, gw int) *model {
	return &model{dir: dir, derived: derived, league: league, entry: entry, gwArg: gw, w: 130, h: 40}
}

func readJSON(path string, v any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

// inputStamp fingerprints the files whose changes matter mid-match.
func inputStamp(dir string, league, gw int) string {
	var b strings.Builder
	for _, p := range []string{
		filepath.Join(dir, fmt.Sprintf("gw/%d/live.json", gw)),
		filepath.Join(dir, "game/game.json"),
		filepath.Join(dir, fmt.Sprintf("league/%d/details.json", league)),
	} {
		if st, err := os.Stat(p); err == nil {
			fmt.Fprintf(&b, "%s:%d;", p, st.ModTime().UnixNano())
		}
	}
	return b.String()
}

// load assembles the whole screen state from local snapshots.
func load(dir, derived string, league, entry, gwArg int) (snapshot, int, error) {
	snap := snapshot{Loaded: time.Now()}
	myIndex := 0

	gw := gwArg
	var game struct {
		CurrentEvent int `json:"current_event"`
		NextEvent    int `json:"next_event"`
	}
	if err := readJSON(filepath.Join(dir, "game/game.json"), &game); err != nil {
		return snap, 0, err
	}
	if gw == 0 {
		if gw = game.CurrentEvent; gw == 0 {
			gw = game.NextEvent
		}
	}
	snap.GW = gw

	var details struct {
		LeagueEntries []struct {
			ID        int    `json:"id"`
			EntryID   int    `json:"entry_id"`
			EntryName string `json:"entry_name"`
			FirstName string `json:"player_first_name"`
			LastName  string `json:"player_last_name"`
		} `json:"league_entries"`
		Matches []struct {
			Event        int `json:"event"`
			LeagueEntry1 int `json:"league_entry_1"`
			LeagueEntry2 int `json:"league_entry_2"`
		} `json:"matches"`
		Standings []struct {
			LeagueEntry  int `json:"league_entry"`
			Total        int `json:"total"`
			PointsFor    int `json:"points_for"`
			MatchesWon   int `json:"matches_won"`
			MatchesDrawn int `json:"matches_drawn"`
			MatchesLost  int `json:"matches_lost"`
		} `json:"standings"`
	}
	if err := readJSON(filepath.Join(dir, fmt.Sprintf("league/%d/details.json", league)), &details); err != nil {
		return snap, 0, err
	}
	entryByLE := map[int]int{}
	nameByEntry := map[int]string{}
	managerByEntry := map[int]string{}
	for _, e := range details.LeagueEntries {
		entryByLE[e.ID] = e.EntryID
		nameByEntry[e.EntryID] = e.EntryName
		managerByEntry[e.EntryID] = strings.TrimSpace(e.FirstName + " " + e.LastName)
	}

	var live struct {
		Elements map[string]struct {
			Stats struct {
				Minutes     int `json:"minutes"`
				TotalPoints int `json:"total_points"`
				Starts      int `json:"starts"`
			} `json:"stats"`
		} `json:"elements"`
		Fixtures []struct {
			TeamH        int    `json:"team_h"`
			TeamA        int    `json:"team_a"`
			TeamHScore   *int   `json:"team_h_score"`
			TeamAScore   *int   `json:"team_a_score"`
			Started      bool   `json:"started"`
			Finished     bool   `json:"finished"`
			FinishedProv bool   `json:"finished_provisional"`
			Minutes      int    `json:"minutes"`
			KickoffTime  string `json:"kickoff_time"`
		} `json:"fixtures"`
	}
	_ = readJSON(filepath.Join(dir, fmt.Sprintf("gw/%d/live.json", gw)), &live) // pre-kickoff: zeros

	type fxState struct{ started, finished bool }
	fixtureByTeam := map[int]fxState{}
	for _, f := range live.Fixtures {
		done := f.Finished || f.FinishedProv
		fixtureByTeam[f.TeamH] = fxState{f.Started, done}
		fixtureByTeam[f.TeamA] = fxState{f.Started, done}
	}

	var bootstrap struct {
		Elements []struct {
			ID          int    `json:"id"`
			WebName     string `json:"web_name"`
			ElementType int    `json:"element_type"`
			Team        int    `json:"team"`
			Status      string `json:"status"`
		} `json:"elements"`
		Teams []struct {
			ID        int    `json:"id"`
			ShortName string `json:"short_name"`
		} `json:"teams"`
		Events struct {
			Data []struct {
				ID           int    `json:"id"`
				DeadlineTime string `json:"deadline_time"`
				WaiversTime  string `json:"waivers_time"`
				TradesTime   string `json:"trades_time"`
			} `json:"data"`
		} `json:"events"`
	}
	if err := readJSON(filepath.Join(dir, "bootstrap/bootstrap-static.json"), &bootstrap); err != nil {
		return snap, 0, err
	}
	teamShort := map[int]string{}
	for _, t := range bootstrap.Teams {
		teamShort[t.ID] = t.ShortName
	}
	for _, f := range live.Fixtures {
		row := fixtureRow{
			Home: teamShort[f.TeamH], Away: teamShort[f.TeamA],
			Started: f.Started, Finished: f.Finished || f.FinishedProv, Minutes: f.Minutes,
		}
		if f.TeamHScore != nil {
			row.HS = *f.TeamHScore
		}
		if f.TeamAScore != nil {
			row.AS = *f.TeamAScore
		}
		if t, err := time.Parse(time.RFC3339, f.KickoffTime); err == nil {
			row.Kickoff = t
		}
		snap.Fixtures = append(snap.Fixtures, row)
	}
	sort.Slice(snap.Fixtures, func(i, j int) bool { return snap.Fixtures[i].Kickoff.Before(snap.Fixtures[j].Kickoff) })
	positions := map[int]string{1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
	info := map[int]playerRow{}
	for _, e := range bootstrap.Elements {
		info[e.ID] = playerRow{Name: e.WebName, Pos: positions[e.ElementType],
			Team: teamShort[e.Team], TeamID: e.Team, Avail: e.Status}
	}

	// Per-club appearances for the match view, and a derived game clock (the
	// fixture "minutes" field is unreliable in-play; use max player minutes).
	appearances := map[int][]clubPlayer{} // club team id -> appeared players
	maxMin := map[int]int{}
	for _, e := range bootstrap.Elements {
		st := live.Elements[fmt.Sprintf("%d", e.ID)].Stats
		if st.Minutes == 0 && st.Starts == 0 {
			continue
		}
		appearances[e.Team] = append(appearances[e.Team], clubPlayer{
			Name: e.WebName, Pos: positions[e.ElementType],
			Minutes: st.Minutes, Points: st.TotalPoints, Start: st.Starts > 0,
		})
		if st.Minutes > maxMin[e.Team] {
			maxMin[e.Team] = st.Minutes
		}
	}
	posOrder := map[string]int{"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
	for i := range snap.Fixtures {
		f := &snap.Fixtures[i]
		hID, aID := 0, 0
		for id, short := range teamShort {
			if short == f.Home {
				hID = id
			}
			if short == f.Away {
				aID = id
			}
		}
		if m := max(maxMin[hID], maxMin[aID]); f.Started && !f.Finished && m > f.Minutes {
			f.Minutes = m
		}
		if !f.Started || f.Finished {
			continue
		}
		md := matchDetail{Home: f.Home, Away: f.Away, HS: f.HS, AS: f.AS, Minute: f.Minutes}
		split := func(teamID int) (xi, subs []clubPlayer, form string) {
			players := appearances[teamID]
			sort.Slice(players, func(a, b int) bool {
				if players[a].Start != players[b].Start {
					return players[a].Start
				}
				if posOrder[players[a].Pos] != posOrder[players[b].Pos] {
					return posOrder[players[a].Pos] < posOrder[players[b].Pos]
				}
				return players[a].Minutes > players[b].Minutes
			})
			counts := map[string]int{}
			for _, p := range players {
				if p.Start {
					xi = append(xi, p)
					counts[p.Pos]++
				} else {
					subs = append(subs, p)
				}
			}
			form = fmt.Sprintf("%d-%d-%d", counts["DEF"], counts["MID"], counts["FWD"])
			return
		}
		md.HomeXI, md.HomeSubs, md.HomeForm = split(hID)
		md.AwayXI, md.AwaySubs, md.AwayForm = split(aID)
		snap.Matches = append(snap.Matches, md)
	}

	glyph := func(p playerRow) string {
		if !p.Starter {
			return "·"
		}
		fx, known := fixtureByTeam[p.TeamID]
		switch {
		case known && fx.finished && p.Minutes > 0:
			return "✓"
		case known && fx.finished:
			return "–"
		case known && fx.started && p.Minutes > 0:
			return "●"
		case p.Avail == "d" || p.Avail == "i" || p.Avail == "s":
			return "⚠"
		default:
			return "○"
		}
	}

	buildSide := func(entryID int) side {
		s := side{Name: nameByEntry[entryID], Manager: managerByEntry[entryID], EntryID: entryID}
		var picks struct {
			Picks []struct {
				Element  int `json:"element"`
				Position int `json:"position"`
			} `json:"picks"`
		}
		if err := readJSON(filepath.Join(dir, fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw)), &picks); err != nil {
			return s
		}
		sort.Slice(picks.Picks, func(i, j int) bool { return picks.Picks[i].Position < picks.Picks[j].Position })
		for _, p := range picks.Picks {
			row := info[p.Element]
			row.Slot = p.Position
			row.Starter = p.Position <= 11
			st := live.Elements[fmt.Sprintf("%d", p.Element)].Stats
			row.Minutes, row.Points = st.Minutes, st.TotalPoints
			row.Glyph = glyph(row)
			if row.Starter {
				s.Total += row.Points
				if row.Minutes > 0 {
					s.Played++
				}
			} else {
				s.Bench += row.Points
			}
			s.Players = append(s.Players, row)
		}
		return s
	}

	for _, m := range details.Matches {
		if m.Event != gw {
			continue
		}
		a, b := entryByLE[m.LeagueEntry1], entryByLE[m.LeagueEntry2]
		if a == 0 || b == 0 {
			continue
		}
		if a == entry || b == entry {
			myIndex = len(snap.Matchups)
		}
		snap.Matchups = append(snap.Matchups, matchup{A: buildSide(a), B: buildSide(b)})
	}
	if len(snap.Matchups) == 0 {
		return snap, 0, fmt.Errorf("no matchups found for GW %d", gw)
	}

	// League standings for the rail (all zeros pre-lockdown — still orienting).
	rows := details.Standings
	sort.Slice(rows, func(i, j int) bool {
		if rows[i].Total != rows[j].Total {
			return rows[i].Total > rows[j].Total
		}
		return rows[i].PointsFor > rows[j].PointsFor
	})
	for _, s := range rows {
		eid := entryByLE[s.LeagueEntry]
		snap.Standings = append(snap.Standings, standingRow{
			Name:   nameByEntry[eid],
			Record: fmt.Sprintf("%d-%d-%d", s.MatchesWon, s.MatchesDrawn, s.MatchesLost),
			Total:  s.PointsFor,
			Mine:   eid == entry,
		})
	}

	// Recent transactions for the rail (best effort).
	var txFile struct {
		Transactions []struct {
			Added      string `json:"added"`
			ElementIn  int    `json:"element_in"`
			ElementOut int    `json:"element_out"`
			Entry      int    `json:"entry"`
			Result     string `json:"result"`
		} `json:"transactions"`
	}
	if err := readJSON(filepath.Join(dir, fmt.Sprintf("league/%d/transactions.json", league)), &txFile); err == nil {
		txs := txFile.Transactions
		sort.Slice(txs, func(i, j int) bool { return txs[i].Added > txs[j].Added })
		for i, t := range txs {
			if i >= 7 {
				break
			}
			snap.Transactions = append(snap.Transactions, txRow{
				TeamName: nameByEntry[t.Entry],
				In:       info[t.ElementIn].Name,
				Out:      info[t.ElementOut].Name,
				Accepted: t.Result == "a",
			})
		}
	}

	// Needs-you suggestions: my_week attention + top waiver targets (best effort).
	var week struct {
		Attention []struct {
			WebName  string   `json:"web_name"`
			Warnings []string `json:"warnings"`
		} `json:"attention"`
	}
	if err := readJSON(filepath.Join(derived, "ml/my_week.json"), &week); err == nil {
		for _, a := range week.Attention {
			for _, w := range a.Warnings {
				g, note := "⚠", w
				switch {
				case strings.Contains(w, "no projection"):
					g, note = "?", "unprojected"
				case strings.Contains(w, "blank"):
					g, note = "◇", "blank GW"
				case strings.Contains(w, "availability"):
					note = strings.TrimPrefix(w, "availability ")
				}
				snap.NeedsYou = append(snap.NeedsYou, railItem{Glyph: g, Name: a.WebName, Note: note})
				break
			}
		}
	}
	var plan struct {
		Recommendations []struct {
			Add        string  `json:"add"`
			Label      string  `json:"label"`
			SeasonGain float64 `json:"season_gain"`
		} `json:"recommendations"`
	}
	if err := readJSON(filepath.Join(derived, "ml/waiver_plan.json"), &plan); err == nil {
		for i, r := range plan.Recommendations {
			if i >= 3 {
				break
			}
			snap.NeedsYou = append(snap.NeedsYou, railItem{
				Glyph: "↑", Name: r.Add, Note: fmt.Sprintf("wire · %s +%.0f", r.Label, r.SeasonGain)})
		}
	}

	snap.NextDue = nextDeadline(bootstrapEventsForCountdown(bootstrap.Events.Data), time.Now())
	return snap, myIndex, nil
}

type countdownEvent struct {
	Label string
	At    time.Time
}

func bootstrapEventsForCountdown(events []struct {
	ID           int    `json:"id"`
	DeadlineTime string `json:"deadline_time"`
	WaiversTime  string `json:"waivers_time"`
	TradesTime   string `json:"trades_time"`
}) []countdownEvent {
	out := []countdownEvent{}
	for _, e := range events {
		for _, pair := range []struct{ label, iso string }{
			{fmt.Sprintf("GW%d trades due", e.ID), e.TradesTime},
			{fmt.Sprintf("GW%d waivers due", e.ID), e.WaiversTime},
			{fmt.Sprintf("GW%d lineup lock", e.ID), e.DeadlineTime},
		} {
			if t, err := time.Parse(time.RFC3339, pair.iso); err == nil {
				out = append(out, countdownEvent{Label: pair.label, At: t})
			}
		}
	}
	return out
}

var eastern = func() *time.Location {
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		return time.UTC
	}
	return loc
}()

// nextDeadline picks the soonest future deadline and formats a countdown.
func nextDeadline(events []countdownEvent, now time.Time) string {
	var best *countdownEvent
	for i := range events {
		if events[i].At.After(now) && (best == nil || events[i].At.Before(best.At)) {
			best = &events[i]
		}
	}
	if best == nil {
		return ""
	}
	d := best.At.Sub(now).Round(time.Minute)
	var in string
	if h := int(d.Hours()); h >= 24 {
		in = fmt.Sprintf("in %dd%dh", h/24, h%24)
	} else if h >= 1 {
		in = fmt.Sprintf("in %dh%02dm", h, int(d.Minutes())%60)
	} else {
		in = fmt.Sprintf("in %dm", int(d.Minutes()))
	}
	return fmt.Sprintf("%s %s EST (%s)", best.Label, best.At.In(eastern).Format("Mon 3:04 PM"), in)
}

// ---------------------------------------------------------------------------
// Async load plumbing
// ---------------------------------------------------------------------------

type snapMsg struct {
	snap    snapshot
	myIndex int
	stamp   string
	skipped bool
	err     error
}

func loadCmd(dir, derived string, league, entry, gw int, prevStamp string) tea.Cmd {
	return func() tea.Msg {
		stamp := inputStamp(dir, league, gwOrProbe(dir, gw))
		if stamp != "" && stamp == prevStamp {
			return snapMsg{skipped: true, stamp: stamp}
		}
		s, idx, err := load(dir, derived, league, entry, gw)
		return snapMsg{snap: s, myIndex: idx, stamp: stamp, err: err}
	}
}

// gwOrProbe resolves gw 0 to the current event cheaply for the mtime stamp.
func gwOrProbe(dir string, gw int) int {
	if gw != 0 {
		return gw
	}
	var game struct {
		CurrentEvent int `json:"current_event"`
		NextEvent    int `json:"next_event"`
	}
	_ = readJSON(filepath.Join(dir, "game/game.json"), &game)
	if game.CurrentEvent != 0 {
		return game.CurrentEvent
	}
	return game.NextEvent
}

// reload is the synchronous path used by --once and tests.
func (m *model) reload() error {
	snap, myIndex, err := load(m.dir, m.derived, m.league, m.entry, m.gwArg)
	if err != nil {
		m.loadErr = err.Error()
		return err
	}
	m.applySnap(snap, myIndex)
	return nil
}

func (m *model) applySnap(snap snapshot, myIndex int) {
	first := m.snap.Loaded.IsZero()
	m.snap, m.loadErr = snap, ""
	if first {
		m.selected = myIndex
	}
	if m.selected >= len(snap.Matchups) {
		m.selected = 0
	}
}

func (m *model) Init() tea.Cmd {
	// Kick one live fetch immediately, then every minute (self-feeding);
	// the first data load runs async like every other.
	m.fetching = true
	m.loading = true
	m.status = "fetching…"
	return tea.Batch(doTick(), doFetchTick(), runFetch(),
		loadCmd(m.dir, m.derived, m.league, m.entry, m.gwArg, ""))
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.w, m.h = msg.Width, msg.Height
		return m, nil
	case tick:
		if m.loading {
			return m, doTick()
		}
		m.loading = true
		return m, tea.Batch(doTick(),
			loadCmd(m.dir, m.derived, m.league, m.entry, m.gwArg, m.stamp))
	case snapMsg:
		m.loading = false
		m.stamp = msg.stamp
		if msg.skipped {
			return m, nil
		}
		if msg.err != nil {
			m.loadErr = msg.err.Error()
			return m, nil
		}
		m.applySnap(msg.snap, msg.myIndex)
		return m, nil
	case fetchTick:
		if m.fetching {
			return m, doFetchTick()
		}
		m.fetching = true
		m.status = "auto-refreshing…"
		return m, tea.Batch(doFetchTick(), runFetch())
	case refreshDone:
		m.fetching = false
		if msg.err != nil {
			m.status = "fetch failed: " + msg.err.Error()
		} else {
			m.status = "fetched " + time.Now().Format("15:04:05")
		}
		return m, nil
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "enter":
			if m.focus == 1 && liveCount(m.snap) > 0 {
				m.matchView = !m.matchView
			}
		case "esc":
			m.matchView = false
		case "tab":
			m.focus = (m.focus + 1) % 3
		case "left", "h":
			if m.focus == 1 {
				m.liveSel = (m.liveSel + max(1, liveCount(m.snap)) - 1) % max(1, liveCount(m.snap))
			} else {
				m.selected = (m.selected + len(m.snap.Matchups) - 1) % max(1, len(m.snap.Matchups))
			}
		case "right", "l", "m":
			if m.focus == 1 {
				m.liveSel = (m.liveSel + 1) % max(1, liveCount(m.snap))
			} else {
				m.selected = (m.selected + 1) % max(1, len(m.snap.Matchups))
			}
		case "r":
			if !m.fetching {
				m.fetching = true
				m.status = "fetching…"
				return m, runFetch()
			}
		}
	}
	return m, nil
}

// liveCount is the number of in-play fixtures.
func liveCount(s snapshot) int {
	n := 0
	for _, f := range s.Fixtures {
		if f.Started && !f.Finished {
			n++
		}
	}
	return n
}
