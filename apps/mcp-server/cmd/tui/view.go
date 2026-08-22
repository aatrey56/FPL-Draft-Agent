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
	half := (width - 7) / 2

	left := sideHeader(mu.A, mu.A.EntryID == m.entry, false, half)
	right := sideHeader(mu.B, mu.B.EntryID == m.entry, true, half)
	head := lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(left), "   ",
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

	collapse := md <= narrow
	bars := md >= medium
	cols := lipgloss.JoinHorizontal(lipgloss.Top,
		lipgloss.NewStyle().Width(half).Render(squadColumn(mu.A, half, bars, collapse, mp)),
		styDim.Render(" │ "),
		squadColumn(mu.B, half, bars, collapse, mp))

	return head + "\n\n" + score + "\n\n" + cols
}

func (m *model) railBody(width int) string {
	var b strings.Builder
	if len(m.snap.Standings) > 0 {
		b.WriteString(styDim.Render("   #  TEAM               W-D-L  PF") + "\n")
		for i, s := range m.snap.Standings {
			name := ansi.Truncate(s.Name, 17, "…")
			pad := strings.Repeat(" ", max(0, 17-lipgloss.Width(name)))
			marker, sty := "  ", styFg
			if s.Mine {
				marker, sty = "◆ ", styYou
			}
			b.WriteString(sty.Render(fmt.Sprintf("%s%2d  %s%s %-6s %d", marker, i+1, name, pad, s.Record, s.Total)) + "\n")
		}
	}
	if len(m.snap.NeedsYou) > 0 {
		b.WriteString("\n" + styDim.Render("─ needs you "+strings.Repeat("─", clamp(width-15, 0, 30))) + "\n")
		for _, r := range m.snap.NeedsYou {
			g := r.Glyph
			sty := styDim
			switch g {
			case "⚠":
				sty = styWarn
			case "↑":
				sty = styLive
			}
			name := ansi.Truncate(r.Name, 13, "…")
			note := ansi.Truncate(r.Note, max(6, width-18), "…")
			b.WriteString(fmt.Sprintf("%s %s %s\n",
				sty.Render(g),
				name+strings.Repeat(" ", max(0, 13-lipgloss.Width(name))),
				styDim.Render(note)))
		}
	}
	return strings.TrimRight(b.String(), "\n")
}

func (m *model) header(width int) string {
	title := styFg.Bold(true).Render(fmt.Sprintf("FPL DRAFT · GW%d", m.snap.GW)) + "  " + styLive.Render("◍ LIVE")
	due := ""
	if m.snap.NextDue != "" {
		due = styYou.Render("⏰ " + m.snap.NextDue)
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

	railW := 0
	if md == wide {
		railW = clamp(w/3, 34, 44)
	}
	mainW := w - railW
	if railW > 0 {
		mainW -= 1
	}

	matchupPanel := Panel(
		fmt.Sprintf("Matchup %d/%d", m.selected+1, len(m.snap.Matchups)), "← →",
		m.matchupBody(mainW-4), mainW, m.focus == 0)

	screen := matchupPanel
	if railW > 0 {
		rail := Panel("League · Needs You", "tab", m.railBody(railW-4), railW, m.focus == 1)
		screen = lipgloss.JoinHorizontal(lipgloss.Top, matchupPanel, " ", rail)
	}

	return m.header(w) + "\n" + screen + "\n" + m.footer(w)
}
