package main

// theme.go — nothing outside this file names a raw colour. The palette is
// "neon night": deep navy chrome, electric cyan brand, hot magenta focus.
// AdaptiveColor keeps it legible on light terminals.

import "github.com/charmbracelet/lipgloss"

var (
	accentC = lipgloss.AdaptiveColor{Light: "#0F7B8A", Dark: "#7DF9FF"} // brand, you, titles
	focusC  = lipgloss.AdaptiveColor{Light: "#B3368C", Dark: "#FF6AC1"} // focused panel border
	liveC   = lipgloss.AdaptiveColor{Light: "#1F8A4C", Dark: "#3DF57C"} // played / in play
	alertC  = lipgloss.AdaptiveColor{Light: "#C13B37", Dark: "#FF5C57"} // DNP, losing, deadline
	flagC   = lipgloss.AdaptiveColor{Light: "#B26A00", Dark: "#FFB86C"} // availability warning
	tmrwC   = lipgloss.AdaptiveColor{Light: "#2E7BB5", Dark: "#8FD6FF"} // has not played yet
	fgC     = lipgloss.AdaptiveColor{Light: "#131816", Dark: "#E6EDF3"}
	mutedC  = lipgloss.AdaptiveColor{Light: "#5C6661", Dark: "#7B88A8"}
	ruleC   = lipgloss.AdaptiveColor{Light: "#C9D2E3", Dark: "#2A3354"} // unfocused borders
	barBgC  = lipgloss.AdaptiveColor{Light: "#E8ECF5", Dark: "#1A2038"} // panel title bars
	selBgC  = lipgloss.AdaptiveColor{Light: "#D0DAF0", Dark: "#24304F"} // selected rows
)

var (
	styTitle = lipgloss.NewStyle().Bold(true).Foreground(accentC)
	styDim   = lipgloss.NewStyle().Foreground(mutedC)
	styScore = lipgloss.NewStyle().Bold(true).Foreground(liveC)
	styLive  = lipgloss.NewStyle().Foreground(liveC)
	styWarn  = lipgloss.NewStyle().Foreground(alertC)
	styFlag  = lipgloss.NewStyle().Foreground(flagC)
	styFg    = lipgloss.NewStyle().Foreground(fgC)
	styYou   = lipgloss.NewStyle().Bold(true).Foreground(accentC)
	styTmrw  = lipgloss.NewStyle().Foreground(tmrwC)
	stySel   = lipgloss.NewStyle().Bold(true).Foreground(fgC).Background(selBgC)

	// Title-bar segments — everything on the bar carries its background.
	styBarTitle = lipgloss.NewStyle().Bold(true).Foreground(accentC).Background(barBgC)
	styBarHint  = lipgloss.NewStyle().Foreground(mutedC).Background(barBgC)
	styBarFill  = lipgloss.NewStyle().Background(barBgC)

	// The diverging score bar: lit cells cyan, rest navy.
	styBarOn  = lipgloss.NewStyle().Foreground(accentC)
	styBarOff = lipgloss.NewStyle().Foreground(ruleC)
)
