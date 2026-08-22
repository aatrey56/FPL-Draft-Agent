package main

// theme.go — nothing outside this file names a raw colour. AdaptiveColor
// picks per the terminal background so the dashboard reads on light themes.

import "github.com/charmbracelet/lipgloss"

var (
	accentC = lipgloss.AdaptiveColor{Light: "#8F6408", Dark: "#E7B24C"} // focus, you, brand
	liveC   = lipgloss.AdaptiveColor{Light: "#256B39", Dark: "#5FBE72"} // in play, points on
	alertC  = lipgloss.AdaptiveColor{Light: "#A93A22", Dark: "#DE6A50"} // injury, deadline near
	fgC     = lipgloss.AdaptiveColor{Light: "#131816", Dark: "#DCE3DE"}
	mutedC  = lipgloss.AdaptiveColor{Light: "#5C6661", Dark: "#8B968F"}
	ruleC   = lipgloss.AdaptiveColor{Light: "#DCE2DB", Dark: "#28312D"}
)

var (
	styTitle = lipgloss.NewStyle().Bold(true).Foreground(accentC)
	styDim   = lipgloss.NewStyle().Foreground(mutedC)
	styScore = lipgloss.NewStyle().Bold(true).Foreground(liveC)
	styLive  = lipgloss.NewStyle().Foreground(liveC)
	styWarn  = lipgloss.NewStyle().Foreground(alertC)
	styFg    = lipgloss.NewStyle().Foreground(fgC)
	styYou   = lipgloss.NewStyle().Bold(true).Foreground(accentC)
	styTmrw  = lipgloss.NewStyle().Foreground(lipgloss.AdaptiveColor{Light: "#2E7BB5", Dark: "#9BD3F0"})
)
