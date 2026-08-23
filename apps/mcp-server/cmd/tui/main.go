// matchday TUI — live H2H matchups in the terminal.
//
// Pure reader: renders the same local snapshots the MCP tools serve
// (gw/<n>/live.json, entry picks, league details, bootstrap). Freshness comes
// from the autopilot / make matchday loop; 'r' triggers one fetch via the
// sanctioned fetcher (cmd/dev). No direct FPL API calls from the TUI.
//
// Usage:  make tui        (league/entry ids from .env)
//
//	--once renders a single frame and exits (used by tests/CI).
package main

import (
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"time"

	tea "github.com/charmbracelet/bubbletea"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/config"
)

func main() {
	var (
		rawRoot     = flag.String("raw-root", "data/raw", "root directory for raw JSON")
		derivedRoot = flag.String("derived-root", "data/derived", "root directory for derived JSON (waiver/my_week rail)")
		season      = flag.String("season", "2026-27", "season label")
		league      = flag.Int("league", 0, "draft league id (default: LEAGUE_ID from .env)")
		entry       = flag.Int("entry", 0, "my entry id (default: ENTRY_ID from .env)")
		gw          = flag.Int("gw", 0, "gameweek (0 = current)")
		once        = flag.Bool("once", false, "render one frame to stdout and exit")
		width       = flag.Int("width", 130, "frame width for --once renders")
	)
	flag.Parse()

	if err := config.FindAndLoadDotEnv(); err != nil {
		fmt.Fprintln(os.Stderr, "warning:", err)
	}
	if *league == 0 {
		if v, err := strconv.Atoi(os.Getenv("LEAGUE_ID")); err == nil {
			*league = v
		}
	}
	if *entry == 0 {
		if v, err := strconv.Atoi(os.Getenv("ENTRY_ID")); err == nil {
			*entry = v
		}
	}
	if *league == 0 || *entry == 0 {
		fmt.Fprintln(os.Stderr, "league and entry ids required (--league/--entry or LEAGUE_ID/ENTRY_ID in .env)")
		os.Exit(2)
	}

	dir := filepath.Join(*rawRoot, *season)
	derived := filepath.Join(*derivedRoot, *season)
	m := newModel(dir, derived, *league, *entry, *gw)

	if *once {
		m.w = *width
		if err := m.reload(); err != nil {
			fmt.Fprintln(os.Stderr, "load:", err)
			os.Exit(1)
		}
		fmt.Println(m.View())
		return
	}

	p := tea.NewProgram(m, tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

// refreshDone is emitted when the background fetch finishes.
type refreshDone struct{ err error }

// tick drives the periodic file reload.
type tick time.Time

func doTick() tea.Cmd {
	return tea.Tick(time.Second, func(t time.Time) tea.Msg { return tick(t) })
}

// fetchTick drives the self-feeding live refresh: while the TUI is open it
// pulls the current GW's live points every minute (the --live-only path), so
// no separate matchday loop is needed for a near-live screen.
type fetchTick time.Time

func doFetchTick() tea.Cmd {
	return tea.Tick(60*time.Second, func(t time.Time) tea.Msg { return fetchTick(t) })
}

func runFetch() tea.Cmd {
	return func() tea.Msg {
		cmd := exec.Command("make", "livefetch")
		cmd.Dir = repoRootGuess()
		return refreshDone{err: cmd.Run()}
	}
}

// repoRootGuess walks up from cwd to the directory containing the Makefile.
func repoRootGuess() string {
	dir, _ := os.Getwd()
	for i := 0; i < 6; i++ {
		if _, err := os.Stat(filepath.Join(dir, "Makefile")); err == nil {
			return dir
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return "."
}
