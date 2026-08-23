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

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/autosub"
)

type playerRow struct {
	Slot       int
	ID         int
	MatchStart bool // started the club's match (live starts flag)
	Name       string
	Pos        string
	Team       string
	TeamID     int
	Avail      string // bootstrap status: a/d/i/s/u
	Minutes    int
	Points     int
	Bonus      int // confirmed bonus (already inside Points)
	Prov       int // provisional bonus from live BPS (not yet in Points)
	Starter    bool
	SubIn      bool   // projected auto-sub: this bench player comes on
	Glyph      string // ● on pitch, ◉ could appear, ✓ played, ✗ DNP, ○ not yet, ⚠ doubt, ⇄ auto-sub in, · bench
}

type side struct {
	Name      string
	Manager   string
	EntryID   int
	Total     int
	Effective int // Total plus projected auto-subs (equal when none fire)
	Proj      int // projected final: Effective + expected points of players yet to play
	Bench     int
	Played    int
	Players   []playerRow
}

type matchup struct{ A, B side }

type standingRow struct {
	Name    string
	Record  string
	Total   int
	Mine    bool
	EntryID int
}

type railItem struct {
	Glyph string // ⚠ availability, ◇ blank, ? unprojected, ↑ wire
	Name  string
	Team  string
	Note  string
	// Wire recommendations carry the full trade math for the detail view.
	Drop       string
	SeasonGain float64
	Next3Gain  float64
	AddROS     float64
	DropROS    float64
	AddNext3   float64
	Confidence string
	News       string
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
	Kind     string // w = waiver, f = free agent
	Accepted bool
}

type managerTx struct {
	Name string
	Pick int // this week's waiver pick number
	Txs  []txRow
}

type clubPlayer struct {
	Name    string
	Pos     string
	Minutes int
	Points  int
	Bonus   int
	Prov    int
	Goals   int
	Assists int
	Start   bool
}

type matchDetail struct {
	Home, Away         string
	HS, AS, Minute     int
	Finished           bool
	HomeXI, AwayXI     []clubPlayer
	HomeSubs, AwaySubs []clubPlayer
}

// tickerStat is the per-player counting state the events ticker diffs.
type tickerStat struct {
	Name, Club                          string
	TeamID                              int
	Goals, Assists, Yellow, Red, Points int
}

// eventRow is one line of the live events feed.
type eventRow struct {
	Minute int    // fixture clock when observed; -1 = before the TUI opened
	Kind   string // G goal, A assist, Y yellow, R red, ± unexplained swing
	Name   string
	Club   string
	Delta  int    // points moved with the event
	Owner  string // rostering team short name, "" = unowned
	Mine   bool
	Opp    bool // owned by this week's opponent
}

// bonusRow / bonusFixture are the live BPS race per in-play fixture.
type bonusRow struct {
	Elem       int
	Name, Club string
	Bps, Award int
}
type bonusFixture struct {
	Label string // "NEW 2-1 LIV 72'"
	Rows  []bonusRow
}

type snapshot struct {
	GW           int
	Matchups     []matchup
	Loaded       time.Time
	Deadline     *countdownEvent  // next deadline; header renders the live countdown
	Deadlines    []countdownEvent // all future deadlines, soonest first (Week panel)
	Standings    []standingRow
	Fixtures     []fixtureRow
	Transactions []txRow
	TxByManager  []managerTx
	NeedsYou     []railItem
	Matches      []matchDetail // one per in-play fixture, liveSel-aligned
	PlayerStats  map[int]tickerStat
	ClockByTeam  map[int]int
	PlayedToday  map[int]bool     // clubs whose fixture kicked off today (EST)
	OwnerByElem  map[int]eventRow // Owner/Mine/Opp template per rostered element
	BonusRace    []bonusFixture
}

type model struct {
	dir       string // season raw dir
	derived   string // season derived dir
	league    int
	entry     int
	gwArg     int
	snap      snapshot
	selected  int
	focus     int // 0 = matchup, 1 = live, 2 = league, 3 = transactions
	liveSel   int
	txSel     int
	sugSel    int
	gamesPage int // index into panelPages()
	events    []eventRow
	seeded    bool
	eventsDay string // EST date the ticker belongs to; rolls over each matchday
	matchSel  int    // index into snap.Matches while the match view is open
	matchView bool
	txView    bool
	sugView   bool
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
			ID         int    `json:"id"`
			EntryID    int    `json:"entry_id"`
			EntryName  string `json:"entry_name"`
			FirstName  string `json:"player_first_name"`
			LastName   string `json:"player_last_name"`
			WaiverPick int    `json:"waiver_pick"`
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
				Bonus       int `json:"bonus"`
				Bps         int `json:"bps"`
				Goals       int `json:"goals_scored"`
				Assists     int `json:"assists"`
				YellowCards int `json:"yellow_cards"`
				RedCards    int `json:"red_cards"`
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
	// Merge per team across a double gameweek: started once any fixture has
	// kicked off, finished only when every fixture is done.
	fixtureByTeam := map[int]fxState{}
	for _, f := range live.Fixtures {
		done := f.Finished || f.FinishedProv
		for _, team := range []int{f.TeamH, f.TeamA} {
			cur, seen := fixtureByTeam[team]
			if !seen {
				fixtureByTeam[team] = fxState{f.Started, done}
				continue
			}
			fixtureByTeam[team] = fxState{cur.started || f.Started, cur.finished && done}
		}
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
		info[e.ID] = playerRow{ID: e.ID, Name: e.WebName, Pos: positions[e.ElementType],
			Team: teamShort[e.Team], TeamID: e.Team, Avail: e.Status}
	}

	// Provisional bonus per live fixture: rank appeared players by BPS; ties
	// share the higher award (FPL's rule, approximated).
	teamFixture := map[int]int{}
	for i, f := range live.Fixtures {
		teamFixture[f.TeamH] = i
		teamFixture[f.TeamA] = i
	}
	type bpsEntry struct{ id, bps int }
	byFixture := map[int][]bpsEntry{}
	for _, e := range bootstrap.Elements {
		st := live.Elements[fmt.Sprintf("%d", e.ID)].Stats
		if fi, ok := teamFixture[e.Team]; ok && st.Minutes > 0 {
			byFixture[fi] = append(byFixture[fi], bpsEntry{e.ID, st.Bps})
		}
	}
	provBonus := map[int]int{}
	raceByFixture := map[int][]bpsEntry{}
	for fi, entries := range byFixture {
		if fi < 0 || fi >= len(live.Fixtures) {
			continue
		}
		f := live.Fixtures[fi]
		if !f.Started || f.Finished || f.FinishedProv {
			continue
		}
		sort.Slice(entries, func(a, b int) bool { return entries[a].bps > entries[b].bps })
		raceByFixture[fi] = entries
		award, prevBps, prevAward := 3, -1, 3
		for rank, en := range entries {
			if rank >= 3 && en.bps != prevBps {
				break
			}
			if en.bps == prevBps {
				provBonus[en.id] = prevAward
			} else {
				provBonus[en.id] = award
				prevAward = award
			}
			prevBps = en.bps
			award = 3 - rank - 1
			if award < 1 {
				award = 1
			}
		}
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
			Bonus: st.Bonus, Prov: provBonus[e.ID],
			Goals: st.Goals, Assists: st.Assists,
		})
		if st.Minutes > maxMin[e.Team] {
			maxMin[e.Team] = st.Minutes
		}
	}
	posOrder := map[string]int{"GKP": 0, "DEF": 1, "MID": 2, "FWD": 3}
	clockByTeam := map[int]int{} // fixture-wide match clock per club
	playedToday := map[int]bool{}
	var doneMatches []matchDetail // live matches list first, completed after
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
		clockByTeam[hID], clockByTeam[aID] = f.Minutes, f.Minutes
		if f.Started && !f.Kickoff.IsZero() &&
			f.Kickoff.In(eastern).Format("2006-01-02") == time.Now().In(eastern).Format("2006-01-02") {
			playedToday[hID], playedToday[aID] = true, true
		}
		if !f.Started {
			continue
		}
		md := matchDetail{Home: f.Home, Away: f.Away, HS: f.HS, AS: f.AS,
			Minute: f.Minutes, Finished: f.Finished}
		split := func(teamID int) (xi, subs []clubPlayer) {
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
			for _, p := range players {
				if p.Start {
					xi = append(xi, p)
				} else {
					subs = append(subs, p)
				}
			}
			return
		}
		md.HomeXI, md.HomeSubs = split(hID)
		md.AwayXI, md.AwaySubs = split(aID)
		if md.Finished {
			doneMatches = append(doneMatches, md)
		} else {
			snap.Matches = append(snap.Matches, md)
		}
	}
	snap.Matches = append(snap.Matches, doneMatches...)
	snap.ClockByTeam = clockByTeam
	snap.PlayedToday = playedToday

	// Per-player counting stats for the events ticker (anyone with anything
	// on the board — a few hundred rows at most).
	snap.PlayerStats = map[int]tickerStat{}
	for _, e := range bootstrap.Elements {
		st := live.Elements[fmt.Sprintf("%d", e.ID)].Stats
		if st.Minutes == 0 && st.Goals == 0 && st.Assists == 0 && st.YellowCards == 0 && st.RedCards == 0 {
			continue
		}
		snap.PlayerStats[e.ID] = tickerStat{
			Name: e.WebName, Club: teamShort[e.Team], TeamID: e.Team,
			Goals: st.Goals, Assists: st.Assists,
			Yellow: st.YellowCards, Red: st.RedCards, Points: st.TotalPoints,
		}
	}

	// The BPS race per in-play fixture, for the Bonus page.
	for fi, entries := range raceByFixture {
		f := live.Fixtures[fi]
		hs, as := 0, 0
		if f.TeamHScore != nil {
			hs = *f.TeamHScore
		}
		if f.TeamAScore != nil {
			as = *f.TeamAScore
		}
		label := fmt.Sprintf("%s %d-%d %s %s", teamShort[f.TeamH], hs, as,
			teamShort[f.TeamA], clockLabel(max(clockByTeam[f.TeamH], clockByTeam[f.TeamA])))
		bf := bonusFixture{Label: label}
		for i, en := range entries {
			if i >= 6 {
				break
			}
			ps := snap.PlayerStats[en.id]
			bf.Rows = append(bf.Rows, bonusRow{Elem: en.id, Name: ps.Name, Club: ps.Club,
				Bps: en.bps, Award: provBonus[en.id]})
		}
		snap.BonusRace = append(snap.BonusRace, bf)
	}
	sort.Slice(snap.BonusRace, func(a, b int) bool { return snap.BonusRace[a].Label < snap.BonusRace[b].Label })

	// Official team sheets (pulse), when released: element -> xi/bench, plus
	// which clubs have a sheet at all. A club with a sheet and a player in
	// neither list means "not in the squad".
	var squads struct {
		Fixtures []struct {
			Sheets map[string]struct {
				XI    []int `json:"xi"`
				Bench []int `json:"bench"`
			} `json:"sheets"`
		} `json:"fixtures"`
	}
	sheetRole := map[int]string{}
	clubSheet := map[int]bool{}
	shortToTeam := map[string]int{}
	for id, short := range teamShort {
		shortToTeam[short] = id
	}
	if err := readJSON(filepath.Join(dir, fmt.Sprintf("gw/%d/squads.json", gw)), &squads); err == nil {
		for _, fx := range squads.Fixtures {
			for club, sheet := range fx.Sheets {
				if len(sheet.XI) == 0 {
					continue
				}
				clubSheet[shortToTeam[club]] = true
				for _, id := range sheet.XI {
					sheetRole[id] = "xi"
				}
				for _, id := range sheet.Bench {
					sheetRole[id] = "bench"
				}
			}
		}
	}

	// A starter whose minutes froze below the match clock has been subbed
	// off — his day (and points) are done, so he gets the ✓ early. A sub who
	// came on also trails the clock but is still playing, hence MatchStart.
	subbedOff := func(p playerRow) bool {
		fx, known := fixtureByTeam[p.TeamID]
		return known && fx.started && !fx.finished &&
			p.MatchStart && p.Minutes > 0 && p.Minutes < clockByTeam[p.TeamID]-2
	}
	glyph := func(p playerRow) string {
		if !p.Starter {
			return "·"
		}
		flagged := p.Avail == "d" || p.Avail == "i" || p.Avail == "s" || p.Avail == "u"
		fx, known := fixtureByTeam[p.TeamID]
		finished := known && fx.finished
		if clubSheet[p.TeamID] && !finished {
			switch {
			case subbedOff(p):
				return "✓"
			case p.Minutes > 0:
				return "●"
			case sheetRole[p.ID] == "xi":
				return "●" // named in the starting XI
			case sheetRole[p.ID] == "bench":
				return "◉" // in the squad, on the club bench
			default:
				return "✗" // sheet is out and he is not in it
			}
		}
		switch {
		case known && fx.finished && p.Minutes > 0:
			return "✓"
		case known && fx.finished:
			return "✗" // did not play — the only red state
		case known && fx.started && subbedOff(p):
			return "✓"
		case known && fx.started && p.Minutes > 0:
			return "●"
		case known && fx.started && flagged:
			return "⚠" // flagged and not on the pitch — may not be in the squad
		case known && fx.started:
			return "◉" // could still come on
		case flagged:
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
			row.Bonus, row.Prov = st.Bonus, provBonus[p.Element]
			row.MatchStart = st.Starts > 0
			row.Glyph = glyph(row)
			if row.Starter {
				s.Total += row.Points
				// "Played" counts resolved starters: minutes on the board,
				// or a club whose GW is over (a confirmed DNP still counts —
				// that slot is done producing).
				fx, known := fixtureByTeam[row.TeamID]
				sheetOut := clubSheet[row.TeamID] && sheetRole[row.ID] == ""
				if row.Minutes > 0 || !known || fx.finished || (fx.started && sheetOut) {
					s.Played++
				}
			} else {
				s.Bench += row.Points
			}
			s.Players = append(s.Players, row)
		}
		s.Effective = s.Total
		// Project end-of-GW auto-subs (only meaningful once fixtures exist).
		if len(fixtureByTeam) > 0 {
			posCode := map[string]int{"GKP": autosub.GKP, "DEF": autosub.DEF, "MID": autosub.MID, "FWD": autosub.FWD}
			sq := make([]autosub.Player, 0, len(s.Players))
			for _, p := range s.Players {
				fx, known := fixtureByTeam[p.TeamID]
				sheetOut := clubSheet[p.TeamID] && sheetRole[p.ID] == ""
				sq = append(sq, autosub.Player{
					Slot: p.Slot, Pos: posCode[p.Pos], Minutes: p.Minutes,
					// Not in the released squad + game underway = confirmed
					// non-player; the auto-sub can be projected already.
					FixtureDone: !known || fx.finished || (fx.started && sheetOut),
				})
			}
			for _, sw := range autosub.Project(sq) {
				for i := range s.Players {
					if s.Players[i].Slot == sw.In {
						s.Players[i].SubIn = true
						s.Players[i].Glyph = "⇄"
						s.Effective += s.Players[i].Points
					}
				}
			}
		}
		// Projected final score: effective points + a per-position expected
		// value for every starter still to play. Position averages come from
		// the 25/26 correlation study; the match model replaces them with
		// real per-player xP once it ships.
		posXP := map[string]float64{"GKP": 3.4, "DEF": 3.0, "MID": 2.9, "FWD": 3.0}
		proj := float64(s.Effective)
		for _, p := range s.Players {
			if !p.Starter || p.Minutes > 0 || p.Glyph == "✗" {
				continue
			}
			fx, known := fixtureByTeam[p.TeamID]
			if known && fx.finished {
				continue
			}
			proj += posXP[p.Pos]
		}
		s.Proj = int(proj + 0.5)
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

	// Who rosters each element, and whether that is me or this week's
	// opponent — the ticker tags events with it.
	snap.OwnerByElem = map[int]eventRow{}
	for mi, mu := range snap.Matchups {
		for _, sd := range []side{mu.A, mu.B} {
			for _, p := range sd.Players {
				snap.OwnerByElem[p.ID] = eventRow{
					Owner: sd.Name,
					Mine:  sd.EntryID == entry,
					Opp:   mi == myIndex && sd.EntryID != entry,
				}
			}
		}
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
			Name:    nameByEntry[eid],
			Record:  fmt.Sprintf("%d-%d-%d", s.MatchesWon, s.MatchesDrawn, s.MatchesLost),
			Total:   s.PointsFor,
			Mine:    eid == entry,
			EntryID: eid,
		})
	}

	// Recent transactions for the rail (best effort).
	var txFile struct {
		Transactions []struct {
			Added      string `json:"added"`
			ElementIn  int    `json:"element_in"`
			ElementOut int    `json:"element_out"`
			Entry      int    `json:"entry"`
			Kind       string `json:"kind"`
			Result     string `json:"result"`
		} `json:"transactions"`
	}
	byMgr := map[string][]txRow{}
	if err := readJSON(filepath.Join(dir, fmt.Sprintf("league/%d/transactions.json", league)), &txFile); err == nil {
		txs := txFile.Transactions
		sort.Slice(txs, func(i, j int) bool { return txs[i].Added > txs[j].Added })
		for _, t := range txs {
			row := txRow{
				TeamName: nameByEntry[t.Entry],
				In:       info[t.ElementIn].Name,
				Out:      info[t.ElementOut].Name,
				Kind:     t.Kind,
				Accepted: t.Result == "a",
			}
			snap.Transactions = append(snap.Transactions, row)
			byMgr[row.TeamName] = append(byMgr[row.TeamName], row)
		}
	}
	// Every manager gets a row, ordered by this week's waiver pick.
	entryOrder := make([]int, 0, len(details.LeagueEntries))
	for i := range details.LeagueEntries {
		entryOrder = append(entryOrder, i)
	}
	sort.Slice(entryOrder, func(a, b int) bool {
		ea, eb := details.LeagueEntries[entryOrder[a]], details.LeagueEntries[entryOrder[b]]
		if ea.WaiverPick != eb.WaiverPick {
			return ea.WaiverPick < eb.WaiverPick
		}
		return ea.EntryName < eb.EntryName
	})
	for _, i := range entryOrder {
		e := details.LeagueEntries[i]
		snap.TxByManager = append(snap.TxByManager, managerTx{
			Name: e.EntryName, Pick: e.WaiverPick, Txs: byMgr[e.EntryName],
		})
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
			AddTeam    string  `json:"add_team"`
			Drop       string  `json:"drop"`
			Label      string  `json:"label"`
			SeasonGain float64 `json:"season_gain"`
			Next3Gain  float64 `json:"next3_gain"`
			AddROS     float64 `json:"add_ros"`
			DropROS    float64 `json:"drop_ros"`
			AddNext3   float64 `json:"add_next3_xp"`
			Confidence string  `json:"confidence"`
			News       string  `json:"news"`
		} `json:"recommendations"`
	}
	if err := readJSON(filepath.Join(derived, "ml/waiver_plan.json"), &plan); err == nil {
		for i, r := range plan.Recommendations {
			if i >= 3 {
				break
			}
			snap.NeedsYou = append(snap.NeedsYou, railItem{
				Glyph: "↑", Name: r.Add, Team: r.AddTeam,
				Note: fmt.Sprintf("wire · %s +%.0f", r.Label, r.SeasonGain),
				Drop: r.Drop, SeasonGain: r.SeasonGain, Next3Gain: r.Next3Gain,
				AddROS: r.AddROS, DropROS: r.DropROS, AddNext3: r.AddNext3,
				Confidence: r.Confidence, News: r.News})
		}
	}

	all := bootstrapEventsForCountdown(bootstrap.Events.Data)
	snap.Deadline = nextDeadlineEvent(all, time.Now())
	for _, ev := range all {
		if ev.At.After(time.Now()) {
			snap.Deadlines = append(snap.Deadlines, ev)
		}
	}
	sort.Slice(snap.Deadlines, func(a, b int) bool { return snap.Deadlines[a].At.Before(snap.Deadlines[b].At) })
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
func nextDeadlineEvent(events []countdownEvent, now time.Time) *countdownEvent {
	var best *countdownEvent
	for i := range events {
		if events[i].At.After(now) && (best == nil || events[i].At.Before(best.At)) {
			best = &events[i]
		}
	}
	return best
}

// fmtDeadline renders the countdown against the live clock, so it ticks.
func fmtDeadline(ev *countdownEvent, now time.Time) string {
	if ev == nil || !ev.At.After(now) {
		return ""
	}
	d := ev.At.Sub(now).Round(time.Minute)
	var in string
	if h := int(d.Hours()); h >= 24 {
		in = fmt.Sprintf("in %dd%dh", h/24, h%24)
	} else if h >= 1 {
		in = fmt.Sprintf("in %dh%02dm", h, int(d.Minutes())%60)
	} else {
		in = fmt.Sprintf("in %dm", int(d.Minutes()))
	}
	return fmt.Sprintf("%s %s EST (%s)", ev.Label, ev.At.In(eastern).Format("Mon 3:04 PM"), in)
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
	// The ticker covers one matchday: a new EST day wipes it and re-seeds
	// from that day's games only.
	if today := time.Now().In(eastern).Format("2006-01-02"); today != m.eventsDay {
		m.events, m.seeded, m.eventsDay = nil, false, today
	}
	m.ingestEvents(snap)
	m.snap, m.loadErr = snap, ""
	if first {
		m.selected = myIndex
	}
	if m.selected >= len(snap.Matchups) {
		m.selected = 0
	}
}

// ingestEvents diffs the incoming per-player stats against the previous
// snapshot and appends ticker rows. The first snapshot seeds a summary of
// what already happened today (minute -1, shown without a clock).
func (m *model) ingestEvents(snap snapshot) {
	prev := m.snap.PlayerStats
	tag := func(id int) eventRow { return snap.OwnerByElem[id] }
	emit := func(id int, kind string, n, delta int) {
		ps := snap.PlayerStats[id]
		minute := snap.ClockByTeam[ps.TeamID]
		if !m.seeded {
			minute = -1
		}
		for range max(1, n) {
			ev := tag(id)
			ev.Minute, ev.Kind, ev.Name, ev.Club, ev.Delta = minute, kind, ps.Name, ps.Club, delta
			m.events = append(m.events, ev)
		}
	}
	for id, cur := range snap.PlayerStats {
		was, seen := prev[id]
		if m.seeded && !seen {
			was = tickerStat{}
		}
		if !m.seeded {
			// Seed pass: summarise today only — yesterday's games belong to
			// yesterday's ticker.
			was = tickerStat{Points: cur.Points}
			if !snap.PlayedToday[cur.TeamID] {
				continue
			}
		}
		perGoal := cur.Points - was.Points
		if g := cur.Goals - was.Goals; g > 0 {
			emit(id, "G", g, perGoal)
		}
		if a := cur.Assists - was.Assists; a > 0 {
			emit(id, "A", a, perGoal)
		}
		if y := cur.Yellow - was.Yellow; y > 0 {
			emit(id, "Y", y, -1)
		}
		if r := cur.Red - was.Red; r > 0 {
			emit(id, "R", r, -3)
		}
	}
	if len(m.events) > 120 {
		m.events = m.events[len(m.events)-120:]
	}
	m.seeded = true
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
			// Success is silent — the header's "data" stamp is the truth.
			m.status = ""
		}
		return m, nil
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "ctrl+c":
			return m, tea.Quit
		case "enter":
			if games := m.gamesList(); m.focus == 1 && len(games) > 0 {
				m.matchView, m.txView = !m.matchView, false
				g := games[clamp(m.liveSel, 0, len(games)-1)]
				for i, md := range m.snap.Matches {
					if md.Home == g.Home && md.Away == g.Away {
						m.matchSel = i
					}
				}
			}
			if m.focus == 2 && len(m.snap.NeedsYou) > 0 {
				m.sugView = !m.sugView
			}
			if m.focus == 3 && len(m.snap.TxByManager) > 0 {
				m.txView, m.matchView = !m.txView, false
			}
		case "esc":
			m.matchView, m.txView, m.sugView = false, false, false
		case "tab":
			m.focus = (m.focus + 1) % 4
		case "up", "k":
			if m.focus == 1 {
				m.liveSel = (m.liveSel + max(1, len(m.gamesList())) - 1) % max(1, len(m.gamesList()))
			}
			if m.focus == 2 {
				m.sugSel = (m.sugSel + max(1, len(m.snap.NeedsYou)) - 1) % max(1, len(m.snap.NeedsYou))
			}
			if m.focus == 3 {
				m.txSel = (m.txSel + max(1, len(m.snap.TxByManager)) - 1) % max(1, len(m.snap.TxByManager))
			}
		case "down", "j":
			if m.focus == 1 {
				m.liveSel = (m.liveSel + 1) % max(1, len(m.gamesList()))
			}
			if m.focus == 2 {
				m.sugSel = (m.sugSel + 1) % max(1, len(m.snap.NeedsYou))
			}
			if m.focus == 3 {
				m.txSel = (m.txSel + 1) % max(1, len(m.snap.TxByManager))
			}
		case "left", "h":
			if m.matchView {
				if n := len(m.snap.Matches); n > 0 {
					m.matchSel = (m.matchSel + n - 1) % n
				}
			} else if m.focus == 1 && m.gamesPages() > 1 {
				m.gamesPage = (m.gamesPage + m.gamesPages() - 1) % m.gamesPages()
				m.liveSel = 0
			} else {
				m.selected = (m.selected + len(m.snap.Matchups) - 1) % max(1, len(m.snap.Matchups))
			}
		case "right", "l":
			if m.matchView {
				if n := len(m.snap.Matches); n > 0 {
					m.matchSel = (m.matchSel + 1) % n
				}
			} else if m.focus == 1 && m.gamesPages() > 1 {
				m.gamesPage = (m.gamesPage + 1) % m.gamesPages()
				m.liveSel = 0
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

// panelPages is the middle panel's page ring: live games, completed games,
// the events ticker, and the bonus race — pages exist only when they have
// something to show (Events always does, if only to say it is watching).
func (m *model) panelPages() []string {
	var pages []string
	if liveCount(m.snap) > 0 {
		pages = append(pages, "live")
	}
	if liveCount(m.snap) < len(m.snap.Matches) {
		pages = append(pages, "played")
	}
	pages = append(pages, "events")
	if len(m.snap.BonusRace) > 0 {
		pages = append(pages, "bonus")
	}
	return pages
}

// currentPage clamps gamesPage into the ring.
func (m *model) currentPage() string {
	pages := m.panelPages()
	return pages[clamp(m.gamesPage, 0, len(pages)-1)]
}

// gamesList is the selectable game list for the current page (nil on the
// events/bonus pages — enter has nothing to open there).
func (m *model) gamesList() []matchDetail {
	if m.mode() == full {
		return m.snap.Matches
	}
	var inPlay, done []matchDetail
	for _, g := range m.snap.Matches {
		if g.Finished {
			done = append(done, g)
		} else {
			inPlay = append(inPlay, g)
		}
	}
	switch m.currentPage() {
	case "live":
		return inPlay
	case "played":
		return done
	}
	return nil
}

// gamesPages is the page count for the ←/→ ring (full screen shows every
// page as its own panel, so the ring collapses).
func (m *model) gamesPages() int {
	if m.mode() == full {
		return 1
	}
	return len(m.panelPages())
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
