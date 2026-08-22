package main

// view.go — the render layer. Everything derives from the stored terminal
// width (WindowSizeMsg): a breakpoint ladder picks the layout, column widths
// are computed, and names are truncated with ansi.Truncate, never %-16s.

import (
	"fmt"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

type mode int

const (
	minimal mode = iota
	narrow
	medium
	wide
)

func (m *model) mode() mode {
	switch {
	case m.w >= 120:
		return wide // matchup + side rail
	case m.w >= 96:
		return medium // matchup only, both squads, bars
	case m.w >= 72:
		return narrow // both squads, no bars, bench collapsed
	default:
		return minimal // scoreboard + top scorers only
	}
}

// clockLabel renders a live match clock, showing HT at the interval.
func clockLabel(minute int) string {
	if minute == 45 {
		return "HT"
	}
	return fmt.Sprintf("%d'", minute)
}

// ptsLabel shows points (confirmed bonus stripped out) plus a RESERVED
// 4-char bonus slot — "(3)" or blanks — so every row is exactly 8 wide and
// the columns stay aligned.
func ptsLabel(points, bonus, prov int) string {
	base := points - bonus
	shown := bonus
	if shown == 0 {
		shown = prov
	}
	suffix := "    "
	if shown > 0 {
		suffix = styYou.Render(fmt.Sprintf("(%d)", shown)) + " "
	}
	return fmt.Sprintf("%3d ", base) + suffix
}

const ptsSlotW = 8

func clamp(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// barStr renders points as an eighth-block bar scaled to maxPts over width chars.
func barStr(pts, maxPts, width int) string {
	if width <= 0 || maxPts <= 0 || pts <= 0 {
		return ""
	}
	eighths := []rune(" ▏▎▍▌▋▊▉")
	frac := float64(pts) / float64(maxPts) * float64(width)
	full := int(frac)
	rem := int((frac - float64(full)) * 8)
	s := strings.Repeat("█", clamp(full, 0, width))
	if full < width && rem > 0 {
		s += string(eighths[rem])
	}
	return s
}

// scoreBar renders the diverging matchup bar: my share in green, rest dim.
func scoreBar(me, opp, width int) string {
	total := me + opp
	if total == 0 {
		return styDim.Render(strings.Repeat("░", width))
	}
	filled := clamp(int(float64(me)/float64(total)*float64(width)+0.5), 0, width)
	return styLive.Render(strings.Repeat("█", filled)) + styDim.Render(strings.Repeat("░", width-filled))
}

func glyphStyle(g string, pts int) lipgloss.Style {
	switch g {
	case "●", "◉":
		return styLive
	case "✓":
		if pts > 0 {
			return styLive
		}
		return styFg
	case "⚠":
		return styWarn
	case "·", "–":
		return styDim
	default:
		return styDim
	}
}

func playerLine(p playerRow, half, barW, maxPts int) string {
	pts := ptsLabel(p.Points, p.Bonus, p.Prov)
	// Everything except the name is constant-width; the name gets the rest.
	fixedTail := fmt.Sprintf(" %-3s %2d' ", p.Team, p.Minutes)
	overhead := 2 + 5 + lipgloss.Width(fixedTail) + ptsSlotW + barW + boolToInt(barW > 0)
	nameW := clamp(half-overhead, 6, 20)
	name := ansi.Truncate(p.Name, nameW, "…")
	base := p.Glyph + " " + fmt.Sprintf("%-4s", p.Pos) +
		name + strings.Repeat(" ", max(0, nameW-lipgloss.Width(name))) + fixedTail
	sty := glyphStyle(p.Glyph, p.Points)
	if !p.Starter {
		sty = styDim
	}
	line := sty.Render(base) + pts
	if barW > 0 {
		line += " " + styLive.Render(barStr(p.Points, maxPts, barW))
	}
	return line
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func maxPoints(mu matchup) int {
	maxPts := 1
	for _, s := range []side{mu.A, mu.B} {
		for _, p := range s.Players {
			if p.Points > maxPts {
				maxPts = p.Points
			}
		}
	}
	return maxPts
}

// squadColumn renders one side's rows for the given half-width.
func squadColumn(s side, half int, showBars, collapseBench bool, maxPts int) string {
	barW := 0
	if showBars && half > 40 {
		barW = clamp(half-40, 0, 7)
	}
	var b strings.Builder
	benched := 0
	for _, p := range s.Players {
		if !p.Starter {
			if collapseBench {
				benched++
				continue
			}
			if p.Slot == 12 {
				b.WriteString(styDim.Render("─ bench "+strings.Repeat("─", clamp(half-9, 0, 40))) + "\n")
			}
		}
		b.WriteString(playerLine(p, half, barW, maxPts) + "\n")
	}
	if collapseBench && benched > 0 {
		b.WriteString(styDim.Render(fmt.Sprintf("↓ bench · %d more", benched)) + "\n")
	}
	return strings.TrimRight(b.String(), "\n")
}

func sideHeader(s side, mine bool, alignRight bool, w int) string {
	name := s.Name
	if mine {
		name = styYou.Render(name + " ◆you")
	} else {
		name = styFg.Bold(true).Render(name)
	}
	mgr := styDim.Render(s.Manager)
	meta := styDim.Render(fmt.Sprintf("%d/11 played · bench %d", s.Played, s.Bench))
	lines := []string{name, mgr, meta}
	if alignRight {
		for i, ln := range lines {
			pad := w - lipgloss.Width(ln)
			if pad > 0 {
				lines[i] = strings.Repeat(" ", pad) + ln
			}
		}
	}
	return strings.Join(lines, "\n")
}

func (m *model) matchupBody(width int) string {
	mu := m.snap.Matchups[m.selected]
	mp := maxPoints(mu)
	md := m.mode()
	half := (width - 3) / 2

	left := sideHeader(mu.A, mu.A.EntryID == m.entry, false, half)
	right := sideHeader(mu.B, mu.B.EntryID == m.entry, true, half)
	head := lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(left), " ",
		lipgloss.NewStyle().Width(half).Render(right))

	// Diverging score bar with margin label.
	barW := clamp(width-30, 10, 40)
	margin := mu.A.Total - mu.B.Total
	if mu.B.EntryID == m.entry {
		margin = -margin
	}
	marginLabel := styDim.Render("level")
	if margin > 0 {
		marginLabel = styLive.Render(fmt.Sprintf("you +%d", margin))
	} else if margin < 0 {
		marginLabel = styWarn.Render(fmt.Sprintf("you %d", margin))
	}
	if mu.A.EntryID != m.entry && mu.B.EntryID != m.entry {
		marginLabel = styDim.Render(fmt.Sprintf("Δ %+d", mu.A.Total-mu.B.Total))
	}
	score := fmt.Sprintf("%s  %s  %s   %s",
		styScore.Render(fmt.Sprintf("%3d", mu.A.Total)),
		scoreBar(mu.A.Total, mu.B.Total, barW),
		styFg.Bold(true).Render(fmt.Sprintf("%d", mu.B.Total)),
		marginLabel)
	if pad := (width - lipgloss.Width(score)) / 2; pad > 0 {
		score = strings.Repeat(" ", pad) + score
	}

	collapse := md <= narrow
	bars := md >= medium
	cols := lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(squadColumn(mu.A, half, bars, collapse, mp)),
		styDim.Render("│ "),
		squadColumn(mu.B, half, bars, collapse, mp))

	return head + "\n\n" + score + "\n\n" + cols
}

// liveScore looks up a manager's current-GW matchup and returns their score
// first, coloured by state: green winning, red losing, dim level.
func (m *model) liveScore(entryID int) string {
	for _, mu := range m.snap.Matchups {
		mine, opp := -1, -1
		switch entryID {
		case mu.A.EntryID:
			mine, opp = mu.A.Total, mu.B.Total
		case mu.B.EntryID:
			mine, opp = mu.B.Total, mu.A.Total
		}
		if mine < 0 {
			continue
		}
		sty := styDim
		if mine > opp {
			sty = styLive
		} else if mine < opp {
			sty = styWarn
		}
		return sty.Render(fmt.Sprintf("%3d-%-3d", mine, opp))
	}
	return strings.Repeat(" ", 7)
}

func (m *model) railBody(width int) string {
	var b strings.Builder
	if len(m.snap.Standings) > 0 {
		nameW := clamp(width-22, 8, 17)
		b.WriteString(ansi.Truncate(styDim.Render(fmt.Sprintf(" #  %-*s %-5s %s", nameW, "TEAM", "W-D-L", "  LIVE")), width, "") + "\n")
		for i, s := range m.snap.Standings {
			name := ansi.Truncate(s.Name, nameW, "…")
			pad := strings.Repeat(" ", max(0, nameW-lipgloss.Width(name)))
			marker, sty := " ", styFg
			if s.Mine {
				marker, sty = "◆", styYou
			}
			line := sty.Render(fmt.Sprintf("%s%2d %s%s %-5s ", marker, i+1, name, pad, s.Record)) +
				m.liveScore(s.EntryID)
			b.WriteString(line + "\n")
		}
	}
	if len(m.snap.NeedsYou) > 0 {
		b.WriteString("\n" + styDim.Render("─ suggestions "+strings.Repeat("─", clamp(width-16, 0, 30))) + "\n")
		for _, r := range m.snap.NeedsYou {
			sty := styDim
			switch r.Glyph {
			case "⚠":
				sty = styWarn
			case "↑":
				sty = styLive
			}
			name := ansi.Truncate(r.Name, 13, "…")
			note := ansi.Truncate(r.Note, max(6, width-17), "…")
			b.WriteString(fmt.Sprintf("%s %s %s\n", sty.Render(r.Glyph),
				name+strings.Repeat(" ", max(0, 13-lipgloss.Width(name))), styDim.Render(note)))
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// txBody lists every manager with their latest successful move; ↑↓ selects,
// enter opens the full week (waivers + free agents) for that manager.
func (m *model) txBody(width int, focused bool) string {
	if len(m.snap.TxByManager) == 0 {
		return styDim.Render("no transactions yet")
	}
	var b strings.Builder
	for i, mgr := range m.snap.TxByManager {
		latest := ""
		for _, t := range mgr.Txs {
			if t.Accepted {
				latest = t.In
				break
			}
		}
		cursor := "  "
		if focused && i == m.txSel {
			cursor = styYou.Render("▸ ")
		}
		team := ansi.Truncate(mgr.Name, width-4, "…")
		b.WriteString(cursor + styFg.Render(team) + "\n")
		if latest != "" {
			b.WriteString(styLive.Render(ansi.Truncate("    +"+latest, width, "…")) + "\n")
		} else {
			b.WriteString(styDim.Render("    no move landed") + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// txDetailBody shows every transaction the selected manager made this week.
func (m *model) txDetailBody(width int) string {
	if len(m.snap.TxByManager) == 0 {
		return styDim.Render("no transactions")
	}
	sel := clamp(m.txSel, 0, len(m.snap.TxByManager)-1)
	mgr := m.snap.TxByManager[sel]
	kinds := map[string]string{"w": "waiver", "f": "free agent", "t": "trade"}
	var b strings.Builder
	b.WriteString(styFg.Bold(true).Render(mgr.Name) + styDim.Render(fmt.Sprintf("  ·  %d moves this week", len(mgr.Txs))) + "\n\n")
	for _, t := range mgr.Txs {
		mark := styLive.Render("✓")
		if !t.Accepted {
			mark = styDim.Render("✗")
		}
		kind := kinds[t.Kind]
		if kind == "" {
			kind = t.Kind
		}
		b.WriteString(fmt.Sprintf("%s %-10s %s  %s\n", mark, styDim.Render(kind),
			styLive.Render("+"+t.In), styDim.Render("−"+t.Out)))
	}
	return strings.TrimRight(b.String(), "\n")
}

// matchBody renders the match view: both lineups as lists (full names,
// minutes, points). ←/→ switches between live matches.
func (m *model) matchBody(width int) string {
	if len(m.snap.Matches) == 0 {
		return styDim.Render("no live matches")
	}
	sel := clamp(m.liveSel, 0, len(m.snap.Matches)-1)
	md := m.snap.Matches[sel]

	scoreLine := fmt.Sprintf("%s %s %s",
		styScore.Render(fmt.Sprintf("%s %d", md.Home, md.HS)),
		styDim.Render("—"),
		styScore.Render(fmt.Sprintf("%d %s", md.AS, md.Away)))
	if md.Finished {
		scoreLine += "  " + styDim.Render("FT")
	} else if md.Minute > 0 {
		scoreLine += "  " + styLive.Render(clockLabel(md.Minute))
	}
	if len(m.snap.Matches) > 1 {
		scoreLine += "   " + styDim.Render(fmt.Sprintf("‹ %d/%d ›", sel+1, len(m.snap.Matches)))
	}
	if pad := (width - lipgloss.Width(scoreLine)) / 2; pad > 0 {
		scoreLine = strings.Repeat(" ", pad) + scoreLine
	}
	return scoreLine + "\n\n" + m.matchLineups(md, width)
}

// matchLineups: both clubs side by side, list form, names uncut.
func (m *model) matchLineups(md matchDetail, width int) string {
	half := (width - 3) / 2
	col := func(club string, xi, subs []clubPlayer) string {
		var b strings.Builder
		b.WriteString(styFg.Bold(true).Render(club) + "\n")
		row := func(glyph string, sty lipgloss.Style, p clubPlayer) string {
			pts := ptsLabel(p.Points, p.Bonus, p.Prov)
			clock := fmt.Sprintf("%4s", clockLabel(p.Minutes))
			overhead := 2 + 5 + 1 + 4 + 1 + ptsSlotW
			nameW := clamp(half-overhead, 6, 20)
			name := ansi.Truncate(p.Name, nameW, "…")
			return sty.Render(glyph+" "+fmt.Sprintf("%-4s", p.Pos)+name+
				strings.Repeat(" ", max(0, nameW-lipgloss.Width(name)))+" "+clock+" ") + pts
		}
		for _, p := range xi {
			switch {
			case p.Minutes > 0 && p.Minutes < md.Minute-2:
				b.WriteString(row("◐", styDim, p) + "\n")
			case p.Minutes > 0:
				b.WriteString(row("●", styLive, p) + "\n")
			default:
				b.WriteString(row("○", styDim, p) + "\n")
			}
		}
		if len(subs) > 0 {
			b.WriteString(styDim.Render("─ subs on ─────────") + "\n")
			for _, p := range subs {
				b.WriteString(row("◒", styDim, p) + "\n")
			}
		}
		return strings.TrimRight(b.String(), "\n")
	}
	return lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(col(md.Home, md.HomeXI, md.HomeSubs)),
		styDim.Render("│ "),
		col(md.Away, md.AwayXI, md.AwaySubs))
}

// scoresStrip shows finished and upcoming fixtures (live ones get their own
// panel at wide widths). Today's games render green, tomorrow's baby blue.
func (m *model) scoresStrip(width int, includeLive bool) string {
	if len(m.snap.Fixtures) == 0 {
		return ""
	}
	var finished, liveParts, upcoming []string
	for _, f := range m.snap.Fixtures {
		switch {
		case f.Finished:
			finished = append(finished, styDim.Render(fmt.Sprintf("%s %d-%d %s ✓", f.Home, f.HS, f.AS, f.Away)))
		case f.Started:
			if !includeLive {
				continue
			}
			liveParts = append(liveParts, styLive.Render(fmt.Sprintf("%s %d-%d %s %s ●", f.Home, f.HS, f.AS, f.Away, clockLabel(f.Minutes))))
		default:
			label := "TBD"
			sty := styDim
			if !f.Kickoff.IsZero() {
				koDay := f.Kickoff.In(eastern).Format("2006-01-02")
				now := time.Now().In(eastern)
				switch koDay {
				case now.Format("2006-01-02"):
					sty = styLive
					label = f.Kickoff.In(eastern).Format("3:04PM")
				case now.AddDate(0, 0, 1).Format("2006-01-02"):
					sty = styTmrw
					label = f.Kickoff.In(eastern).Format("Mon 3:04PM")
				default:
					label = f.Kickoff.In(eastern).Format("Mon 3:04PM")
				}
			}
			upcoming = append(upcoming, sty.Render(fmt.Sprintf("%s v %s %s", f.Home, f.Away, label)))
		}
	}
	parts := append(append(finished, liveParts...), upcoming...)
	sep := styDim.Render("  ·  ")
	var lines []string
	cur := ""
	for _, p := range parts {
		candidate := cur
		if candidate != "" {
			candidate += sep
		}
		candidate += p
		if lipgloss.Width(candidate) > width-4 && cur != "" {
			lines = append(lines, cur)
			cur = p
			continue
		}
		cur = candidate
	}
	if cur != "" {
		lines = append(lines, cur)
	}
	return " " + strings.Join(lines, "\n ")
}

// liveBody lists every started game — in-play first (green, with clock),
// completed after (dim, FT). Any of them opens the lineups view with enter.
func (m *model) liveBody(width int, focused bool) string {
	if len(m.snap.Matches) == 0 {
		return styDim.Render("no games yet")
	}
	var b strings.Builder
	for i, g := range m.snap.Matches {
		label, sty := clockLabel(g.Minute), styLive
		if g.Finished {
			label, sty = "FT", styDim
		}
		line := fmt.Sprintf("%s %d-%d %s %s", g.Home, g.HS, g.AS, g.Away, label)
		if focused && i == m.liveSel {
			b.WriteString(styYou.Render("▸ "+line) + "\n")
		} else {
			b.WriteString(sty.Render("  "+line) + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m *model) header(width int) string {
	title := styFg.Bold(true).Render(fmt.Sprintf("FPL · GW%d", m.snap.GW)) + "  " + styLive.Render("◍ LIVE")
	due := ""
	if m.snap.NextDue != "" {
		due = styYou.Render(m.snap.NextDue)
	}
	clock := styFg.Bold(true).Render(time.Now().In(eastern).Format("3:04:05 PM") + " EST")
	stamp := styDim.Render("data "+m.snap.Loaded.Format("15:04:05")) + " " + styLive.Render("●")
	if m.status != "" {
		stamp += "  " + styDim.Render(m.status)
	}
	parts := []string{title, due, clock, stamp}
	line := " "
	for _, p := range parts {
		if p == "" {
			continue
		}
		line += " " + p + "  "
	}
	if pad := width - lipgloss.Width(line) - 2; pad > 0 {
		line += strings.Repeat(" ", pad)
	}
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(ruleC).Width(width - 2)
	return box.Render(line)
}

func (m *model) footer() string {
	return " " + styDim.Render("←→ matchup    tab panel    ↑↓ select    ↵ open    esc back    r refresh    q quit")
}

func (m *model) View() string {
	if m.loadErr != "" {
		return fmt.Sprintf("cannot load matchday data: %s\n(run `make fetch` once, then retry — q to quit)\n", m.loadErr)
	}
	if len(m.snap.Matchups) == 0 {
		return "loading…\n"
	}
	md := m.mode()
	w := m.w

	if md == minimal {
		mu := m.snap.Matchups[m.selected]
		return m.header(w) + "\n" +
			Panel(fmt.Sprintf("Matchup %d/%d", m.selected+1, len(m.snap.Matchups)), "← →",
				sideHeader(mu.A, mu.A.EntryID == m.entry, false, w-6)+"\n\n"+
					fmt.Sprintf("%s  vs  %s", styScore.Render(fmt.Sprintf("%d", mu.A.Total)), styFg.Bold(true).Render(fmt.Sprintf("%d", mu.B.Total))),
				w, m.focus == 0) + "\n" + m.footer()
	}

	leagueW, liveW, txW := 0, 0, 0
	if md == wide {
		leagueW = clamp(w/4, 30, 36)
		if len(m.snap.Matches) > 0 {
			liveW = clamp(w/6, 20, 24)
		}
		txW = clamp(w-66-leagueW-liveW-3, 0, 30)
		if txW < 18 {
			txW = 0
		}
	}
	mainW := w - leagueW - liveW - txW
	for _, pw := range []int{leagueW, liveW, txW} {
		if pw > 0 {
			mainW--
		}
	}
	if md == wide && mainW > 66 {
		slack := mainW - 66
		mainW = 66
		leagueW = clamp(leagueW+slack, 30, 44)
	}

	mainTitle := fmt.Sprintf("Matchup %d/%d", m.selected+1, len(m.snap.Matchups))
	mainBody := m.matchupBody(mainW - 4)
	if m.matchView && len(m.snap.Matches) > 0 {
		mainTitle = "Match"
		mainBody = m.matchBody(mainW - 4)
	} else if m.txView && len(m.snap.TxByManager) > 0 {
		mainTitle = "Transactions"
		mainBody = m.txDetailBody(mainW - 4)
	}
	matchupPanel := Panel(mainTitle, "← →", mainBody, mainW, m.focus == 0 || m.matchView || m.txView)

	screen := matchupPanel
	if liveW > 0 {
		gamesTitle := "Live"
		if liveCount(m.snap) == 0 {
			gamesTitle = "Played"
		}
		livePanel := Panel(gamesTitle, "↑↓ ↵", m.liveBody(liveW-4, m.focus == 1), liveW, m.focus == 1)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, livePanel, " ", screen)
	}
	if leagueW > 0 {
		league := Panel("League", "tab", m.railBody(leagueW-4), leagueW, m.focus == 2)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, screen, " ", league)
	}
	if txW > 0 {
		tx := Panel("Transactions", "↑↓ ↵", m.txBody(txW-4, m.focus == 3), txW, m.focus == 3)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, screen, " ", tx)
	}

	out := m.header(w)
	if strip := m.scoresStrip(w, md != wide || liveW == 0); strip != "" {
		out += "\n" + strip
	}
	return out + "\n" + screen + "\n" + m.footer()
}
