package main

// panel.go — the whole look in one function: a double-line box whose border
// signals focus (navy → magenta) with a background-tinted title bar as the
// first interior row. minHeight pads the body so panels sharing a grid row
// end on the same line.

import (
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// Panel renders body inside a box of exactly `width` columns. The title sits
// on a tinted bar; focused switches the border to the focus colour. minHeight
// is the minimum body height in rows (0 = natural).
func Panel(title, hint, body string, width int, focused bool, minHeight int) string {
	bc := ruleC
	if focused {
		bc = focusC
	}
	inner := width - 4 // border + padding on each side

	bar := styBarTitle.Render("▍" + title)
	hintSeg := ""
	if hint != "" {
		hintSeg = styBarHint.Render(hint + " ")
	}
	fill := inner - lipgloss.Width(bar) - lipgloss.Width(hintSeg)
	titleRow := bar + styBarFill.Render(strings.Repeat(" ", max(0, fill))) + hintSeg

	for lipgloss.Height(body) < minHeight {
		body += "\n"
	}

	box := lipgloss.NewStyle().
		Border(lipgloss.DoubleBorder()).
		BorderForeground(bc).
		Width(width-2).
		Padding(0, 1)
	return box.Render(titleRow + "\n" + body)
}
