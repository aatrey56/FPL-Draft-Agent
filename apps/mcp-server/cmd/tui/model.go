package main

// Data loading + bubbletea model. The render pipeline is pure: load() reads
// the local snapshots into a snapshot struct, View() formats it — so tests
// can exercise both without a terminal.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type playerRow struct {
	Slot    int
	Name    string
	Pos     string
	Team    string
	Minutes int
	Points  int
	Starter bool
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

type snapshot struct {
	GW       int
	Matchups []matchup
	Loaded   time.Time
	NextDue  string // e.g. "waivers due Thu 1:30 PM EST (in 5d16h)"
}

type model struct {
	dir      string
	league   int
	entry    int
	gwArg    int
	snap     snapshot
	selected int // index into snap.Matchups
	loadErr  string
	status   string
	fetching bool
}

func newModel(dir string, league, entry, gw int) *model {
	return &model{dir: dir, league: league, entry: entry, gwArg: gw}
}

func readJSON(path string, v any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	return json.Unmarshal(raw, v)
}

// load assembles every matchup of the GW from local snapshots.
func load(dir string, league, entry, gwArg int) (snapshot, int, error) {
	snap := snapshot{Loaded: time.Now()}
	myIndex := 0

	gw := gwArg
	if gw == 0 {
		var game struct {
			CurrentEvent int `json:"current_event"`
			NextEvent    int `json:"next_event"`
		}
		if err := readJSON(filepath.Join(dir, "game/game.json"), &game); err != nil {
			return snap, 0, err
		}
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
			} `json:"stats"`
		} `json:"elements"`
	}
	_ = readJSON(filepath.Join(dir, fmt.Sprintf("gw/%d/live.json", gw)), &live) // pre-kickoff: all zeros

	var bootstrap struct {
		Elements []struct {
			ID          int    `json:"id"`
			WebName     string `json:"web_name"`
			ElementType int    `json:"element_type"`
			Team        int    `json:"team"`
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
	teams := map[int]string{}
	for _, t := range bootstrap.Teams {
		teams[t.ID] = t.ShortName
	}
	positions := map[int]string{1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
	info := map[int]playerRow{}
	for _, e := range bootstrap.Elements {
		info[e.ID] = playerRow{Name: e.WebName, Pos: positions[e.ElementType], Team: teams[e.Team]}
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

func (m *model) reload() error {
	snap, myIndex, err := load(m.dir, m.league, m.entry, m.gwArg)
	if err != nil {
		m.loadErr = err.Error()
		return err
	}
	first := m.snap.Loaded.IsZero()
	m.snap, m.loadErr = snap, ""
	if first {
		m.selected = myIndex
	}
	if m.selected >= len(snap.Matchups) {
		m.selected = 0
	}
	return nil
}

func (m *model) Init() tea.Cmd {
	_ = m.reload()
	// Kick one live fetch immediately, then every minute (self-feeding).
	m.fetching = true
	m.status = "fetching…"
	return tea.Batch(doTick(), doFetchTick(), runFetch())
}

func (m *model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tick:
		_ = m.reload()
		return m, doTick()
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
			_ = m.reload()
		}
		return m, nil
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "left", "h":
			m.selected = (m.selected + len(m.snap.Matchups) - 1) % max(1, len(m.snap.Matchups))
		case "right", "l", "m":
			m.selected = (m.selected + 1) % max(1, len(m.snap.Matchups))
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

var (
	titleStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("15")).Background(lipgloss.Color("55")).Padding(0, 1)
	scoreStyle = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("120"))
	dimStyle   = lipgloss.NewStyle().Foreground(lipgloss.Color("245"))
	mineStyle  = lipgloss.NewStyle().Bold(true).Foreground(lipgloss.Color("222"))
	colStyle   = lipgloss.NewStyle().PaddingRight(3)
)

func renderSide(s side, mine bool) string {
	var b strings.Builder
	name := s.Name
	if mine {
		name = mineStyle.Render(name + " (you)")
	}
	b.WriteString(name + "\n")
	if s.Manager != "" {
		b.WriteString(dimStyle.Render(s.Manager) + "\n")
	}
	b.WriteString(fmt.Sprintf("%s  %s\n\n",
		scoreStyle.Render(fmt.Sprintf("%3d pts", s.Total)),
		dimStyle.Render(fmt.Sprintf("%d/11 played · bench %d", s.Played, s.Bench))))
	for _, p := range s.Players {
		if p.Slot == 12 {
			b.WriteString(dimStyle.Render("── bench ──────────────────") + "\n")
		}
		line := fmt.Sprintf("%-4s %-16s %-4s %3d' %3d", p.Pos, p.Name, p.Team, p.Minutes, p.Points)
		if !p.Starter {
			line = dimStyle.Render(line)
		} else if p.Points > 0 {
			line = lipgloss.NewStyle().Foreground(lipgloss.Color("120")).Render(line)
		}
		b.WriteString(line + "\n")
	}
	return b.String()
}

func (m *model) View() string {
	if m.loadErr != "" {
		return fmt.Sprintf("cannot load matchday data: %s\n(run `make fetch` once, then retry — q to quit)\n", m.loadErr)
	}
	if len(m.snap.Matchups) == 0 {
		return "loading…\n"
	}
	mu := m.snap.Matchups[m.selected]
	header := titleStyle.Render(fmt.Sprintf(" GW%d LIVE ", m.snap.GW)) +
		dimStyle.Render(fmt.Sprintf("  matchup %d/%d · data %s · ←/→ switch · r refresh · q quit",
			m.selected+1, len(m.snap.Matchups), m.snap.Loaded.Format("15:04:05")))
	if m.status != "" {
		header += "  " + dimStyle.Render(m.status)
	}
	if m.snap.NextDue != "" {
		header += "\n" + mineStyle.Render("⏰ "+m.snap.NextDue)
	}
	left := colStyle.Render(renderSide(mu.A, mu.A.EntryID == m.entry))
	right := renderSide(mu.B, mu.B.EntryID == m.entry)
	body := lipgloss.JoinHorizontal(lipgloss.Top, left, right)
	return header + "\n\n" + body
}
