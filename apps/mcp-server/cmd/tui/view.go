package main

// view.go — the render layer. Everything derives from the stored terminal
// width (WindowSizeMsg): a breakpoint ladder picks the layout, column widths
// are computed, and names are truncated with ansi.Truncate, never %-16s.

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/charmbracelet/lipgloss"
	"github.com/charmbracelet/x/ansi"
)

type mode int

const (
	minimal mode = iota
	narrow
	wide
	full
)

func (m *model) mode() mode {
	switch {
	case m.w >= 140:
		return full // fullscreen: every tracker gets its own panel
	case m.w >= 90:
		return wide // compact stack — fits a half-screen terminal split
	case m.w >= 72:
		return narrow // matchup only, no bars, bench collapsed
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

// gaLabel renders goal involvement ("2G 1A") padded to a fixed slot so the
// lineup columns stay aligned whatever the scoreline.
func gaLabel(goals, assists, width int) string {
	parts := []string{}
	if goals > 0 {
		parts = append(parts, fmt.Sprintf("%dG", goals))
	}
	if assists > 0 {
		parts = append(parts, fmt.Sprintf("%dA", assists))
	}
	label := strings.Join(parts, " ")
	pad := strings.Repeat(" ", max(0, width-lipgloss.Width(label)))
	if label == "" {
		return pad
	}
	return styScore.Render(label) + pad
}

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

// scoreBar renders the diverging matchup bar: my share lit cyan, rest navy.
// Solid blocks — fancier glyphs render unevenly in some terminal fonts.
func scoreBar(me, opp, width int) string {
	total := me + opp
	if total == 0 {
		return styBarOff.Render(strings.Repeat("░", width))
	}
	filled := clamp(int(float64(me)/float64(total)*float64(width)+0.5), 0, width)
	return styBarOn.Render(strings.Repeat("█", filled)) + styBarOff.Render(strings.Repeat("░", width-filled))
}

// glyphStyle is the status colour language: green = played, playing, or in
// a live game and able to come on (◉ — the club is playing, the slot is
// alive); baby blue = kickoff still ahead (○); red ✗ = confirmed DNP;
// orange ⚠ = availability flag; dim = FPL bench.
func glyphStyle(g string) lipgloss.Style {
	switch g {
	case "●", "⇄", "✓", "◉":
		return styLive
	case "○":
		return styTmrw
	case "⚠":
		return styFlag
	case "✗":
		return styWarn
	default:
		return styDim
	}
}

func playerLine(p playerRow, half, barW, maxPts int) string {
	pts := ptsLabel(p.Points, p.Bonus, p.Prov)
	// ◉ (live game, on the club bench) draws as an open circle — colour
	// alone separates it from ○ (kickoff ahead): green vs baby blue.
	glyph := p.Glyph
	if glyph == "◉" {
		glyph = "○"
	}
	// Everything except the name is constant-width; the name gets the rest.
	fixedTail := fmt.Sprintf(" %-3s %2d' ", p.Team, p.Minutes)
	overhead := 2 + 5 + lipgloss.Width(fixedTail) + ptsSlotW + barW + boolToInt(barW > 0)
	nameW := clamp(half-overhead, 6, 20)
	name := ansi.Truncate(p.Name, nameW, "…")
	base := glyph + " " + fmt.Sprintf("%-4s", p.Pos) +
		name + strings.Repeat(" ", max(0, nameW-lipgloss.Width(name))) + fixedTail
	sty := glyphStyle(p.Glyph)
	if !p.Starter && !p.SubIn {
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

	// Diverging score bar with margin label. Totals include projected
	// auto-subs; a ⇄ marks a side where one is live.
	barW := clamp(width-30, 10, 40)
	margin := mu.A.Effective - mu.B.Effective
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
		marginLabel = styDim.Render(fmt.Sprintf("Δ %+d", mu.A.Effective-mu.B.Effective))
	}
	mark := func(s side) string {
		if s.Effective != s.Total {
			return styTmrw.Render("⇄")
		}
		return ""
	}
	score := fmt.Sprintf("%s%s  %s  %s%s   %s",
		styScore.Render(fmt.Sprintf("%3d", mu.A.Effective)), mark(mu.A),
		scoreBar(mu.A.Effective, mu.B.Effective, barW),
		styFg.Bold(true).Render(fmt.Sprintf("%d", mu.B.Effective)), mark(mu.B),
		marginLabel)
	if pad := (width - lipgloss.Width(score)) / 2; pad > 0 {
		score = strings.Repeat(" ", pad) + score
	}

	collapse := md <= narrow
	bars := md == wide
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
			mine, opp = mu.A.Effective, mu.B.Effective
		case mu.B.EntryID:
			mine, opp = mu.B.Effective, mu.A.Effective
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

// wrapText greedily wraps words to the given width.
func wrapText(text string, width int) []string {
	var lines []string
	cur := ""
	for _, word := range strings.Fields(text) {
		candidate := cur
		if candidate != "" {
			candidate += " "
		}
		candidate += word
		if lipgloss.Width(candidate) > width && cur != "" {
			lines = append(lines, cur)
			cur = word
			continue
		}
		cur = candidate
	}
	if cur != "" {
		lines = append(lines, cur)
	}
	return lines
}

// sugDetailBody replaces the League body with the selected suggestion in
// full. Wire recommendations explain the whole trade: who to drop, what the
// gain numbers mean, and the confidence behind them.
func (m *model) sugDetailBody(width int) string {
	if len(m.snap.NeedsYou) == 0 {
		return styDim.Render("no suggestions")
	}
	r := m.snap.NeedsYou[clamp(m.sugSel, 0, len(m.snap.NeedsYou)-1)]
	sty := styDim
	switch r.Glyph {
	case "⚠":
		sty = styWarn
	case "↑":
		sty = styLive
	}
	var b strings.Builder
	b.WriteString(sty.Render(r.Glyph) + " " + styFg.Bold(true).Render(r.Name))
	if r.Team != "" {
		b.WriteString(" " + styDim.Render(r.Team))
	}
	b.WriteString("\n\n")
	para := func(text string) {
		for _, ln := range wrapText(text, width-1) {
			b.WriteString(styFg.Render(ln) + "\n")
		}
	}
	if r.Drop != "" {
		para(fmt.Sprintf("Add %s, drop %s.", r.Name, r.Drop))
		b.WriteString("\n")
		para(fmt.Sprintf("Projected rest-of-season: %s %.0f pts vs %s %.0f pts — the +%.0f is that gap, the season points you gain by making the swap.",
			r.Name, r.AddROS, r.Drop, r.DropROS, r.SeasonGain))
		b.WriteString("\n")
		para(fmt.Sprintf("Next 3 GWs: %s projects %.1f xP, +%.1f over %s.",
			r.Name, r.AddNext3, r.Next3Gain, r.Drop))
		if r.Confidence != "" {
			b.WriteString("\n" + styDim.Render("confidence: "+r.Confidence) + "\n")
		}
		if r.News != "" {
			para("news: " + r.News)
		}
	} else {
		for _, ln := range wrapText(r.Note, width-1) {
			b.WriteString(styFg.Render(ln) + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// projScore is the projected final matchup score for a manager, own score
// first, coloured by the projected result.
func (m *model) projScore(entryID int) string {
	for _, mu := range m.snap.Matchups {
		mine, opp := -1, -1
		switch entryID {
		case mu.A.EntryID:
			mine, opp = mu.A.Proj, mu.B.Proj
		case mu.B.EntryID:
			mine, opp = mu.B.Proj, mu.A.Proj
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

func (m *model) railBody(width int, focused bool) string {
	var b strings.Builder
	if len(m.snap.Standings) > 0 {
		showProj := width >= 38
		overhead := 22
		projHead := ""
		if showProj {
			overhead, projHead = 30, "    PROJ"
		}
		nameW := clamp(width-overhead, 8, 17)
		b.WriteString(ansi.Truncate(styDim.Render(fmt.Sprintf(" #  %-*s %-5s %s%s", nameW, "TEAM", "W-D-L", "  LIVE", projHead)), width, "") + "\n")
		for i, s := range m.snap.Standings {
			name := ansi.Truncate(s.Name, nameW, "…")
			pad := strings.Repeat(" ", max(0, nameW-lipgloss.Width(name)))
			marker, sty := " ", styFg
			if s.Mine {
				marker, sty = "◆", styYou
			}
			line := sty.Render(fmt.Sprintf("%s%2d %s%s %-5s ", marker, i+1, name, pad, s.Record)) +
				m.liveScore(s.EntryID)
			if showProj {
				line += " " + m.projScore(s.EntryID)
			}
			b.WriteString(line + "\n")
		}
	}
	if len(m.snap.NeedsYou) > 0 {
		b.WriteString("\n" + styDim.Render("─ Suggestions "+strings.Repeat("─", clamp(width-16, 0, 30))) + "\n")
		for i, r := range m.snap.NeedsYou {
			sty := styDim
			switch r.Glyph {
			case "⚠":
				sty = styWarn
			case "↑":
				sty = styLive
			}
			if focused && i == m.sugSel {
				row := "▸ " + r.Glyph + " " + ansi.Truncate(r.Name, width-6, "…")
				b.WriteString(stySel.Render(row+strings.Repeat(" ", max(0, width-lipgloss.Width(row)))) + "\n")
			} else {
				b.WriteString(sty.Render(r.Glyph) + " " + styFg.Render(ansi.Truncate(r.Name, width-4, "…")) + "\n")
			}
			b.WriteString("  " + styDim.Render(ansi.Truncate(r.Note, width-3, "…")) + "\n")
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
		landed, missed := "", ""
		for _, t := range mgr.Txs {
			if t.Accepted && landed == "" {
				landed = t.In
			}
			if !t.Accepted && missed == "" {
				missed = t.In
			}
		}
		team := ansi.Truncate(mgr.Name, width-7, "…")
		if focused && i == m.txSel {
			row := fmt.Sprintf("▸ %2d %s", mgr.Pick, team)
			b.WriteString(stySel.Render(row+strings.Repeat(" ", max(0, width-lipgloss.Width(row)))) + "\n")
		} else {
			b.WriteString("  " + styDim.Render(fmt.Sprintf("%2d ", mgr.Pick)) + styFg.Render(team) + "\n")
		}
		switch {
		case landed != "":
			b.WriteString(styLive.Render(ansi.Truncate("    +"+landed, width, "…")) + "\n")
		case missed != "":
			b.WriteString(styWarn.Render(ansi.Truncate("    ✗"+missed, width, "…")) + "\n")
		default:
			b.WriteString(styDim.Render("    no transactions") + "\n")
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
	games := m.snap.Matches
	if len(games) == 0 {
		return styDim.Render("no live matches")
	}
	sel := clamp(m.matchSel, 0, len(games)-1)
	md := games[sel]

	scoreLine := fmt.Sprintf("%s %s %s",
		styScore.Render(fmt.Sprintf("%s %d", md.Home, md.HS)),
		styDim.Render("—"),
		styScore.Render(fmt.Sprintf("%d %s", md.AS, md.Away)))
	if md.Finished {
		scoreLine += "  " + styDim.Render("FT")
	} else if md.Minute > 0 {
		scoreLine += "  " + styLive.Render(clockLabel(md.Minute))
	}
	if len(games) > 1 {
		scoreLine += "   " + styDim.Render(fmt.Sprintf("‹ %d/%d ›", sel+1, len(games)))
	}
	if pad := (width - lipgloss.Width(scoreLine)) / 2; pad > 0 {
		scoreLine = strings.Repeat(" ", pad) + scoreLine
	}
	return scoreLine + "\n\n" + m.matchLineups(md, width)
}

// matchLineups: both clubs side by side, list form, names uncut. Narrow
// widths stack the clubs vertically instead.
func (m *model) matchLineups(md matchDetail, width int) string {
	col := func(club string, xi, subs []clubPlayer, colW int) string {
		gaW := 0
		if colW >= 34 {
			gaW = 6 // reserved "3G 2A" slot so goal rows stay aligned
		}
		var b strings.Builder
		b.WriteString(styFg.Bold(true).Render(club) + "\n")
		row := func(glyph string, sty lipgloss.Style, p clubPlayer) string {
			pts := ptsLabel(p.Points, p.Bonus, p.Prov)
			clock := fmt.Sprintf("%4s", clockLabel(p.Minutes))
			overhead := 2 + 5 + 1 + 4 + 1 + ptsSlotW
			if gaW > 0 {
				overhead += gaW + 1
			}
			nameW := clamp(colW-overhead, 6, 20)
			name := ansi.Truncate(p.Name, nameW, "…")
			line := sty.Render(glyph + " " + fmt.Sprintf("%-4s", p.Pos) + name +
				strings.Repeat(" ", max(0, nameW-lipgloss.Width(name))) + " " + clock + " ")
			if gaW > 0 {
				line += gaLabel(p.Goals, p.Assists, gaW) + " "
			}
			return line + pts
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
	if width < 60 {
		// Narrow panel: one club above the other, full-width rows.
		return col(md.Home, md.HomeXI, md.HomeSubs, width) + "\n" +
			styDim.Render(strings.Repeat("─", max(0, width-2))) + "\n" +
			col(md.Away, md.AwayXI, md.AwaySubs, width)
	}
	half := (width - 3) / 2
	return lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(col(md.Home, md.HomeXI, md.HomeSubs, half)),
		styDim.Render("│ "),
		col(md.Away, md.AwayXI, md.AwaySubs, half))
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

// panelBody renders the middle panel's current page.
func (m *model) panelBody(width int, focused bool) string {
	switch m.currentPage() {
	case "events":
		if m.evView {
			return m.eventDetailBody(width)
		}
		return m.eventsBody(width, 14, focused)
	case "bonus":
		return m.bonusBody(width)
	}
	return m.liveBody(width, focused)
}

// eventsBody is the live feed, newest first: minute, kind, player, points.
func (m *model) eventsBody(width, rows int, focused bool) string {
	if len(m.events) == 0 {
		return styDim.Render("watching for goals, assists, cards…")
	}
	kindSty := map[string]lipgloss.Style{
		"G": styScore, "A": lipgloss.NewStyle().Bold(true).Foreground(accentC),
		"Y": styFlag, "R": styWarn,
	}
	n := len(m.events)
	sel := clampEvSel(m.evSel, n)
	// Chronological order (oldest → newest). Show a window that keeps the
	// selected row in view; unfocused, anchor to the newest events.
	top := 0
	if n > rows {
		if focused {
			top = clamp(sel-rows/2, 0, n-rows)
		} else {
			top = n - rows
		}
	}
	var b strings.Builder
	if top > 0 {
		b.WriteString(styDim.Render(fmt.Sprintf("  ↑ %d earlier", top)) + "\n")
	}
	for i := top; i < n && i < top+rows; i++ {
		ev := m.events[i]
		clock := " ⋯ "
		if ev.Min != "" {
			clock = fmt.Sprintf("%4s", ev.Min)
		}
		sty, ok := kindSty[ev.Kind]
		if !ok {
			sty = styDim
		}
		delta := "   "
		if ev.Delta > 0 {
			delta = styLive.Render(fmt.Sprintf("+%d", ev.Delta))
		} else if ev.Delta < 0 {
			delta = styWarn.Render(fmt.Sprintf("%d", ev.Delta))
		}
		who := ""
		switch {
		case ev.Mine:
			who = styYou.Render("◆you")
		case ev.Opp:
			who = styWarn.Render("◇opp")
		case ev.Owner != "":
			who = styDim.Render(ansi.Truncate(ev.Owner, 10, "…"))
		}
		name := ansi.Truncate(ev.Name, clamp(width-22, 6, 18), "…")
		line := fmt.Sprintf("%s %s %s %s %s %s",
			styDim.Render(clock), sty.Render(ev.Kind), styFg.Render(name),
			styDim.Render(ev.Club), delta, who)
		if focused && i == sel {
			plain := fmt.Sprintf("%3s %s %s %s %s %s", clock, ev.Kind, name, ev.Club,
				strings.TrimSpace(ansi.Strip(delta)), ansi.Strip(who))
			b.WriteString(stySel.Render(ansi.Truncate(plain, width, "…")+
				strings.Repeat(" ", max(0, width-lipgloss.Width(ansi.Truncate(plain, width, "…"))))) + "\n")
			continue
		}
		b.WriteString(ansi.Truncate(line, width, "…") + "\n")
	}
	if end := n - (top + rows); end > 0 {
		b.WriteString(styDim.Render(fmt.Sprintf("  ↓ %d more", end)))
	}
	return strings.TrimRight(b.String(), "\n")
}

// eventDetailBody expands the selected event: the action, when it happened in
// the game and in EST wall-clock, whose player, and what it moved.
func (m *model) eventDetailBody(width int) string {
	if len(m.events) == 0 {
		return styDim.Render("no events yet")
	}
	ev := m.events[clampEvSel(m.evSel, len(m.events))]
	kindName := map[string]string{"G": "Goal", "A": "Assist", "Y": "Yellow card", "R": "Red card"}
	sty := map[string]lipgloss.Style{"G": styScore, "A": styTitle, "Y": styFlag, "R": styWarn}[ev.Kind]
	if sty.GetForeground() == nil {
		sty = styFg
	}
	var b strings.Builder
	head := kindName[ev.Kind]
	if head == "" {
		head = ev.Kind
	}
	b.WriteString(sty.Bold(true).Render(head) + " — " + styFg.Bold(true).Render(ev.Name) +
		" " + styDim.Render(ev.Pos+" "+ev.Club) + "\n\n")
	para := func(k, v string) {
		b.WriteString(styDim.Render(fmt.Sprintf("%-11s", k)) + styFg.Render(v) + "\n")
	}
	if ev.Min != "" {
		para("game time", ev.Min)
	}
	if !ev.Wall.IsZero() {
		para("occurred", ev.Wall.In(eastern).Format("3:04 PM EST"))
	}
	delta := "0"
	if ev.Delta > 0 {
		delta = fmt.Sprintf("+%d", ev.Delta)
	} else if ev.Delta < 0 {
		delta = fmt.Sprintf("%d", ev.Delta)
	}
	para("fpl points", delta)
	owner := "free agent (unowned)"
	switch {
	case ev.Mine:
		owner = "YOUR player"
	case ev.Opp:
		owner = "your opponent this week"
	case ev.Owner != "":
		owner = ev.Owner
	}
	para("owned by", owner)
	b.WriteString("\n")
	note := ""
	switch {
	case ev.Mine && ev.Delta > 0:
		note = fmt.Sprintf("This added %s to your score.", delta)
	case ev.Opp && ev.Delta > 0:
		note = fmt.Sprintf("This added %s to your opponent — it cuts your margin.", delta)
	case ev.Owner == "" && ev.Delta > 0:
		note = "A free agent returning — a potential waiver target if the form holds."
	}
	if note != "" {
		for _, ln := range wrapText(note, width-1) {
			b.WriteString(styDim.Render(ln) + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// bonusBody is the live BPS race per in-play fixture — who holds 3/2/1.
// bestBody is the GW top-scorer leaderboard — the Bonus panel's idle face.
func (m *model) bestBody(width, rows int) string {
	type scored struct {
		id int
		st tickerStat
	}
	var all []scored
	for id, st := range m.snap.PlayerStats {
		if st.Points != 0 {
			all = append(all, scored{id, st})
		}
	}
	if len(all) == 0 {
		return styDim.Render("no points on the board yet")
	}
	sort.Slice(all, func(a, b int) bool {
		if all[a].st.Points != all[b].st.Points {
			return all[a].st.Points > all[b].st.Points
		}
		return all[a].st.Name < all[b].st.Name
	})
	recs := map[string]bool{}
	for _, r := range m.snap.NeedsYou {
		if r.Glyph == "↑" {
			recs[r.Name] = true
		}
	}
	var b strings.Builder
	for i, sc := range all {
		if i >= rows {
			break
		}
		owner := styFree.Render("free")
		if recs[sc.st.Name] {
			owner += " " + styFg.Bold(true).Render("rec")
		}
		if tag, ok := m.snap.OwnerByElem[sc.id]; ok {
			switch {
			case tag.Mine:
				owner = styYou.Render("◆you")
			case tag.Opp:
				owner = styWarn.Render("◇opp")
			default:
				owner = styDim.Render(ansi.Truncate(tag.Owner, 10, "…"))
			}
		}
		name := ansi.Truncate(sc.st.Name, clamp(width-16, 6, 16), "…")
		line := fmt.Sprintf("%s %s %s %s", styScore.Render(fmt.Sprintf("%3d", sc.st.Points)),
			styFg.Render(name), styDim.Render(sc.st.Club), owner)
		b.WriteString(ansi.Truncate(line, width, "…") + "\n")
	}
	return strings.TrimRight(b.String(), "\n")
}

// weekBody is the full-screen Week panel: every coming deadline with a live
// countdown, then who is still to play on each side of my matchup.
func (m *model) weekBody(width int) string {
	var b strings.Builder
	now := time.Now()
	for i, ev := range m.snap.Deadlines {
		if i >= 3 {
			break
		}
		b.WriteString(styYou.Render(ansi.Truncate(fmtDeadline(&ev, now), width, "…")) + "\n")
	}
	fixtureOf := map[string]fixtureRow{}
	for _, f := range m.snap.Fixtures {
		fixtureOf[f.Home], fixtureOf[f.Away] = f, f
	}
	label := func(short string) string {
		f, ok := fixtureOf[short]
		if !ok {
			return "no fixture"
		}
		if f.Started && !f.Finished {
			return fmt.Sprintf("%s v %s %s", f.Home, f.Away, clockLabel(f.Minutes))
		}
		return fmt.Sprintf("%s v %s %s", f.Home, f.Away, f.Kickoff.In(eastern).Format("Mon 3:04PM"))
	}
	for _, mu := range m.snap.Matchups {
		mine := mu.A.EntryID == m.entry || mu.B.EntryID == m.entry
		if !mine {
			continue
		}
		for _, sd := range []side{mu.A, mu.B} {
			head := styFg.Bold(true).Render(ansi.Truncate(sd.Name, width-14, "…"))
			if sd.EntryID == m.entry {
				head = styYou.Render("◆ still to play")
			} else {
				head = styWarn.Render("◇ " + ansi.Truncate(sd.Name, width-4, "…"))
			}
			b.WriteString("\n" + head + "\n")
			left := 0
			for _, p := range sd.Players {
				if !p.Starter || p.Minutes > 0 || p.Glyph == "✗" {
					continue
				}
				name := ansi.Truncate(p.Name, clamp(width-20, 6, 14), "…")
				b.WriteString(fmt.Sprintf("%s %s %s\n", glyphStyle(p.Glyph).Render(p.Glyph),
					styFg.Render(name), styDim.Render(ansi.Truncate(label(p.Team), width-4-lipgloss.Width(name), "…"))))
				left++
			}
			if left == 0 {
				b.WriteString(styDim.Render("  all done") + "\n")
			}
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m *model) bonusBody(width int) string {
	if len(m.snap.BonusRace) == 0 {
		return styDim.Render("no bonus race — nothing in play")
	}
	var b strings.Builder
	for fi, f := range m.snap.BonusRace {
		if fi > 0 {
			b.WriteString("\n")
		}
		b.WriteString(styDim.Render(f.Label) + "\n")
		for _, r := range f.Rows {
			award := "   "
			if r.Award > 0 {
				award = styYou.Render(fmt.Sprintf("(%d)", r.Award))
			}
			owner := ""
			if tag, ok := m.snap.OwnerByElem[r.Elem]; ok {
				switch {
				case tag.Mine:
					owner = styYou.Render(" ◆you")
				case tag.Opp:
					owner = styWarn.Render(" ◇opp")
				default:
					owner = styDim.Render(" " + ansi.Truncate(tag.Owner, 8, "…"))
				}
			}
			name := ansi.Truncate(r.Name, clamp(width-16, 6, 16), "…")
			line := fmt.Sprintf("%s %3d %s %s%s", award, r.Bps, styFg.Render(name),
				styDim.Render(r.Club), owner)
			b.WriteString(ansi.Truncate(line, width, "…") + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// liveBody renders one game-list page (live or completed). Enter opens the
// lineups view.
func (m *model) liveBody(width int, focused bool) string {
	games := m.gamesList()
	if len(games) == 0 {
		return styDim.Render("no games yet")
	}
	var b strings.Builder
	for i, g := range games {
		label, sty := clockLabel(g.Minute), styLive
		if g.Finished {
			label, sty = "FT", styDim
			if !g.Kickoff.IsZero() {
				label = "FT · " + g.Kickoff.In(eastern).Format("Mon 3:04PM")
			}
		}
		line := fmt.Sprintf("%s %d-%d %s %s", g.Home, g.HS, g.AS, g.Away, label)
		if focused && i == m.liveSel {
			row := "▸ " + line
			b.WriteString(stySel.Render(row+strings.Repeat(" ", max(0, width-lipgloss.Width(row)))) + "\n")
		} else {
			b.WriteString(sty.Render("  "+line) + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m *model) header(width int) string {
	// Full-width tinted banner; every segment carries the bar background.
	on := func(sty lipgloss.Style) lipgloss.Style { return sty.Background(barBgC) }
	title := on(styTitle).Render(fmt.Sprintf(" FPL · GW%d ", m.snap.GW)) + on(styLive).Render("◍ LIVE")
	due := ""
	if d := fmtDeadline(m.snap.Deadline, time.Now()); d != "" {
		due = on(styYou).Render(d)
	}
	clock := on(styFg.Bold(true)).Render(time.Now().In(eastern).Format("3:04:05 PM") + " EST")
	stamp := on(styDim).Render("data "+m.snap.Loaded.Format("15:04:05")) + on(styLive).Render(" ●")
	if m.status != "" {
		stamp += on(styDim).Render("  " + m.status)
	}
	gap := on(styBarFill).Render("   ")
	line := title + gap + due + gap + clock + gap + stamp
	if pad := width - lipgloss.Width(line); pad > 0 {
		line += styBarFill.Render(strings.Repeat(" ", pad))
	}
	return line
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
				w, m.focus == 0, 0) + "\n" + m.footer()
	}

	// Wide layout: the Matchup spans the full width on top; League, Live,
	// and Transactions sit beneath it in three equal columns, padded to one
	// height so every edge lines up. Fullscreen instead gives every tracker
	// its own panel: Matchup | Games | Week over four columns.
	mainW := w
	weekW := 0
	if md == full {
		weekW = clamp(w/5, 26, 34)
		mainW = clamp(w-weekW-24-2, 66, 96)
	}

	mainTitle := fmt.Sprintf("Matchup %d/%d", m.selected+1, len(m.snap.Matchups))
	mainBody := m.matchupBody(mainW - 4)
	if m.matchView && len(m.snap.Matches) > 0 && md != full {
		mainTitle = "Match"
		mainBody = m.matchBody(mainW - 4)
	} else if m.txView && len(m.snap.TxByManager) > 0 {
		mainTitle = "Transactions"
		mainBody = m.txDetailBody(mainW - 4)
	}
	screen := Panel(mainTitle, "← →", mainBody, mainW, m.focus == 0 || m.matchView || m.txView, 0)

	if md == full {
		gamesW := w - mainW - weekW - 2

		gamesTitle, gamesHint := "Live", "↑↓ ↵"
		if games := m.gamesList(); len(games) > 0 && games[0].Finished {
			gamesTitle = "Played"
		}
		gamesBody := m.liveBody(gamesW-4, m.focus == 1)
		if m.matchView && len(m.snap.Matches) > 0 {
			// Fullscreen opens the lineups inside this panel itself.
			gamesTitle, gamesHint = "Match", "←→ esc"
			gamesBody = m.matchBody(gamesW - 4)
		}
		weekBody := m.weekBody(weekW - 4)
		topH := max(lipgloss.Height(m.matchupBody(mainW-4)), max(lipgloss.Height(gamesBody), lipgloss.Height(weekBody)))
		mainPanel := Panel(mainTitle, "← →", mainBody, mainW, m.focus == 0 || m.matchView || m.txView, topH)
		gamesPanel := Panel(gamesTitle, gamesHint, gamesBody, gamesW, m.focus == 1, topH)
		weekPanel := Panel("Week", "", weekBody, weekW, false, topH)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, mainPanel, " ", gamesPanel, " ", weekPanel)

		// League gets extra width so the PROJ column fits; the other three
		// split the remainder evenly.
		leagueW := clamp((w-3)/4+8, 40, 52)
		rest := (w - 3 - leagueW) / 3
		eventsW, bonusW := rest, rest
		txW := w - 3 - leagueW - eventsW - bonusW
		leagueTitle, leagueHint := "League", "tab ↑↓ ↵"
		leagueBody := m.railBody(leagueW-4, m.focus == 2)
		if m.sugView {
			leagueTitle, leagueHint = "Suggestion", "esc"
			leagueBody = m.sugDetailBody(leagueW - 4)
		}
		eventsTitle, eventsHint := "Events", ""
		eventsBody := m.eventsBody(eventsW-4, 22, m.focus == 4)
		if m.focus == 4 {
			eventsHint = "↑↓ ↵"
		}
		if m.evView {
			eventsTitle, eventsHint = "Event", "esc"
			eventsBody = m.eventDetailBody(eventsW - 4)
		}
		bonusTitle, bonusBody := "Bonus", m.bonusBody(bonusW-4)
		if len(m.snap.BonusRace) == 0 {
			bonusTitle, bonusBody = "Best today", m.bestBody(bonusW-4, 12)
		}
		txBody := m.txBody(txW-4, m.focus == 3)
		botH := max(max(lipgloss.Height(leagueBody), lipgloss.Height(eventsBody)),
			max(lipgloss.Height(bonusBody), lipgloss.Height(txBody)))
		row2 := lipgloss.JoinHorizontal(lipgloss.Top,
			Panel(leagueTitle, leagueHint, leagueBody, leagueW, m.focus == 2, botH), " ",
			Panel(eventsTitle, eventsHint, eventsBody, eventsW, m.focus == 4, botH), " ",
			Panel(bonusTitle, "", bonusBody, bonusW, false, botH), " ",
			Panel("Transactions", "↑↓ ↵", txBody, txW, m.focus == 3, botH))
		screen += "\n" + row2
	}

	if md == wide {
		third := (w - 2) / 3
		leagueW, liveW := third, third
		txW := w - 2 - leagueW - liveW

		leagueTitle, leagueHint := "League", "tab ↑↓ ↵"
		leagueBody := m.railBody(leagueW-4, m.focus == 2)
		if m.sugView {
			leagueTitle, leagueHint = "Suggestion", "esc"
			leagueBody = m.sugDetailBody(leagueW - 4)
		}
		titles := map[string]string{"live": "Live", "played": "Played", "events": "Events", "bonus": "Bonus"}
		gamesTitle, gamesHint := titles[m.currentPage()], "↑↓ ↵"
		if n := m.gamesPages(); n > 1 {
			pageAt := clamp(m.gamesPage, 0, n-1) + 1
			gamesTitle = fmt.Sprintf("%s %d/%d", gamesTitle, pageAt, n)
			gamesHint = "←→ ↑↓ ↵"
		}
		liveBody := m.panelBody(liveW-4, m.focus == 1)
		txBody := m.txBody(txW-4, m.focus == 3)

		botH := max(lipgloss.Height(leagueBody), max(lipgloss.Height(liveBody), lipgloss.Height(txBody)))
		league := Panel(leagueTitle, leagueHint, leagueBody, leagueW, m.focus == 2, botH)
		games := Panel(gamesTitle, gamesHint, liveBody, liveW, m.focus == 1, botH)
		tx := Panel("Transactions", "↑↓ ↵", txBody, txW, m.focus == 3, botH)
		screen += "\n" + lipgloss.JoinHorizontal(lipgloss.Top, league, " ", games, " ", tx)
	}

	out := m.header(w)
	if strip := m.scoresStrip(w, md == narrow); strip != "" {
		out += "\n" + strip
	}
	return out + "\n" + screen + "\n" + m.footer()
}
