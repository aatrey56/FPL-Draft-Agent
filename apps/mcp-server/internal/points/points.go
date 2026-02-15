package points

import (
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
)

type LiveStats struct {
	Minutes     int `json:"minutes"`
	TotalPoints int `json:"total_points"`
}

type PlayerPoints struct {
	Element    int `json:"element"`
	Position   int `json:"position"`
	Minutes    int `json:"minutes"`
	Points     int `json:"points"`
	Multiplier int `json:"multiplier"`
	Total      int `json:"total"`
}

type Result struct {
	LeagueID       int            `json:"league_id"`
	EntryID        int            `json:"entry_id"`
	Gameweek       int            `json:"gameweek"`
	GeneratedAtUTC string         `json:"generated_at_utc"`
	Players        []PlayerPoints `json:"players"`
	TotalPoints    int            `json:"total_points"`
}

func BuildResult(leagueID int, entryID int, gw int, snap *ledger.EntrySnapshot, liveByElement map[int]LiveStats) *Result {
	players := make([]PlayerPoints, 0, 11)
	total := 0

	for _, p := range snap.Picks {
		if p.Position > 11 {
			continue
		}
		live := liveByElement[p.Element]
		pp := PlayerPoints{
			Element:    p.Element,
			Position:   p.Position,
			Minutes:    live.Minutes,
			Points:     live.TotalPoints,
			Multiplier: p.Multiplier,
			Total:      live.TotalPoints * p.Multiplier,
		}
		players = append(players, pp)
		total += pp.Total
	}

	return &Result{
		LeagueID:       leagueID,
		EntryID:        entryID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Players:        players,
		TotalPoints:    total,
	}
}

func WriteResult(path string, result *Result) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(result, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}
