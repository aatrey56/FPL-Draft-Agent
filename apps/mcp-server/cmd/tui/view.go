package main

// view.go — the render layer. Everything derives from the stored terminal
// width (WindowSizeMsg): a breakpoint ladder picks the layout, column widths
// are computed, and names are truncated with ansi.Truncate, never %-16s.

import (
	"fmt"
	"strings"

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
	case "●":
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

func playerLine(p playerRow, nameW, barW, maxPts int) string {
	name := ansi.Truncate(p.Name, nameW, "…")
	base := p.Glyph + " " + fmt.Sprintf("%-4s", p.Pos) +
		name + strings.Repeat(" ", max(0, nameW-lipgloss.Width(name))) + " " +
		fmt.Sprintf("%-4s %3d' %3d", p.Team, p.Minutes, p.Points)
	line := glyphStyle(p.Glyph, p.Points).Render(base)
	if !p.Starter {
		line = styDim.Render(base)
	}
	if barW > 0 {
		line += " " + styLive.Render(barStr(p.Points, maxPts, barW))
	}
	return line
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
	fixed := 2 + 5 + 5 + 5 + 4 // glyph+space, pos, team, mins, pts
	nameW := clamp(half-fixed-10, 8, 18)
	barW := 0
	if showBars {
		barW = clamp(half-fixed-nameW-2, 0, 7)
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
		b.WriteString(playerLine(p, nameW, barW, maxPts) + "\n")
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

func (m *model) railBody(width int) string {
	var b strings.Builder
	if len(m.snap.Standings) > 0 {
		nameW := clamp(width-16, 8, 17)
		b.WriteString(ansi.Truncate(styDim.Render(" #  TEAM"+strings.Repeat(" ", nameW-3)+"W-D-L PF"), width, "") + "\n")
		for i, s := range m.snap.Standings {
			name := ansi.Truncate(s.Name, nameW, "…")
			pad := strings.Repeat(" ", max(0, nameW-lipgloss.Width(name)))
			marker, sty := " ", styFg
			if s.Mine {
				marker, sty = "◆", styYou
			}
			line := fmt.Sprintf("%s%2d %s%s %-5s %d", marker, i+1, name, pad, s.Record, s.Total)
			b.WriteString(sty.Render(ansi.Truncate(line, width, "…")) + "\n")
		}
	}
	if len(m.snap.Transactions) > 0 {
		b.WriteString("\n" + styDim.Render("─ transactions "+strings.Repeat("─", clamp(width-18, 0, 30))) + "\n")
		for _, t := range m.snap.Transactions {
			mark := styLive.Render("✓")
			if !t.Accepted {
				mark = styDim.Render("✗")
			}
			team := ansi.Truncate(t.TeamName, 12, "…")
			line := fmt.Sprintf("%s %s%s +%s −%s", mark,
				team, strings.Repeat(" ", max(0, 12-lipgloss.Width(team))),
				t.In, t.Out)
			b.WriteString(ansi.Truncate(line, width, "…") + "\n")
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

// scoresStrip renders the real PL scoreboard: live green with minutes,
// finished dim with a check, upcoming with EST kickoff.
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
			min := ""
			if f.Minutes > 0 {
				min = fmt.Sprintf(" %d'", f.Minutes)
			}
			liveParts = append(liveParts, styLive.Render(fmt.Sprintf("%s %d-%d %s%s ●", f.Home, f.HS, f.AS, f.Away, min)))
		default:
			label := "TBD"
			if !f.Kickoff.IsZero() {
				label = f.Kickoff.In(eastern).Format("Mon 3:04PM")
			}
			upcoming = append(upcoming, styDim.Render(fmt.Sprintf("%s v %s %s", f.Home, f.Away, label)))
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

// liveBody lists in-play fixtures; the selected one is highlighted when the
// panel holds focus.
func (m *model) liveBody(width int, focused bool) string {
	var b strings.Builder
	i := 0
	for _, f := range m.snap.Fixtures {
		if !f.Started || f.Finished {
			continue
		}
		min := ""
		if f.Minutes > 0 {
			min = fmt.Sprintf(" %d'", f.Minutes)
		}
		line := fmt.Sprintf("%s %d-%d %s%s", f.Home, f.HS, f.AS, f.Away, min)
		if focused && i == m.liveSel {
			b.WriteString(styYou.Render("▸ "+line) + "\n")
		} else {
			b.WriteString(styLive.Render("  "+line) + "\n")
		}
		i++
	}
	if i == 0 {
		b.WriteString(styDim.Render("no games in play"))
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m *model) header(width int) string {
	title := styFg.Bold(true).Render(fmt.Sprintf("FPL · GW%d", m.snap.GW)) + "  " + styLive.Render("◍ LIVE")
	due := ""
	if m.snap.NextDue != "" {
		due = styYou.Render(m.snap.NextDue)
	}
	stamp := styDim.Render("data "+m.snap.Loaded.Format("15:04:05")) + " " + styLive.Render("●")
	gap1 := width - lipgloss.Width(title) - lipgloss.Width(due) - lipgloss.Width(stamp) - 8
	if gap1 < 2 {
		due = ""
		gap1 = width - lipgloss.Width(title) - lipgloss.Width(stamp) - 8
	}
	half := max(1, gap1/2)
	line := "  " + title + strings.Repeat(" ", half) + due + strings.Repeat(" ", max(1, gap1-half)) + stamp + "  "
	box := lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(ruleC).Width(width - 2)
	return box.Render(line)
}

func (m *model) footer(width int) string {
	keys := styDim.Render("←→ matchup    tab panel    r refresh    q quit")
	right := ""
	if m.status != "" {
		right = styDim.Render(m.status) + " " + styLive.Render("●")
	}
	gap := width - lipgloss.Width(keys) - lipgloss.Width(right) - 2
	return " " + keys + strings.Repeat(" ", max(1, gap)) + right
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
				w, m.focus == 0) + "\n" + m.footer(w)
	}

	railW, liveW := 0, 0
	if md == wide {
		railW = clamp(w/4+4, 36, 44)
		if liveCount(m.snap) > 0 {
			liveW = clamp(w/6, 20, 26)
		}
	}
	mainW := w - railW - liveW
	if railW > 0 {
		mainW--
	}
	if liveW > 0 {
		mainW--
	}
	// Keep the matchup pane compact; the rail absorbs the slack.
	if md == wide && mainW > 66 {
		slack := mainW - 66
		mainW = 66
		railW = clamp(railW+slack, 36, 60)
	}

	matchupPanel := Panel(
		fmt.Sprintf("Matchup %d/%d", m.selected+1, len(m.snap.Matchups)), "← →",
		m.matchupBody(mainW-4), mainW, m.focus == 0)

	screen := matchupPanel
	if liveW > 0 {
		livePanel := Panel("Live", "← →", m.liveBody(liveW-4, m.focus == 1), liveW, m.focus == 1)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, screen, " ", livePanel)
	}
	if railW > 0 {
		rail := Panel("League · Transactions", "tab", m.railBody(railW-4), railW, m.focus == 2)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, screen, " ", rail)
	}

	out := m.header(w)
	if strip := m.scoresStrip(w, md != wide || liveW == 0); strip != "" {
		out += "\n" + strip
	}
	return out + "\n" + screen + "\n" + m.footer(w)
}
