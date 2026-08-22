package main

// panel.go — the whole feedtui look in one function: a titled box whose
// border colour signals focus. The title is spliced into the top edge so it
// costs zero interior rows.

import (
	"strings"

	"github.com/charmbracelet/lipgloss"
)

// Panel renders body inside a rounded box of exactly `width` columns with the
// title in the top border. focused switches the border to the accent colour.
func Panel(title, hint, body string, width int, focused bool) string {
	bc := ruleC
	if focused {
		bc = accentC
	}
	edge := lipgloss.NewStyle().Foreground(bc)

	left := "╭─ "
	rightHint := ""
	rightCap := "─╮"
	if hint != "" {
		rightHint = " " + hint + " "
	}
	fill := width - lipgloss.Width(left) - lipgloss.Width(title) - 1 -
		lipgloss.Width(rightHint) - lipgloss.Width(rightCap)
	if fill < 0 {
		fill = 0
	}
	top := edge.Render(left) + styTitle.Render(title) + edge.Render(" "+strings.Repeat("─", fill)) +
		styDim.Render(rightHint) + edge.Render(rightCap)

	box := lipgloss.NewStyle().
		Border(lipgloss.RoundedBorder(), false, true, true, true).
		BorderForeground(bc).
		Width(width-2).
		Padding(0, 1)

	return top + "\n" + box.Render(body)
}
