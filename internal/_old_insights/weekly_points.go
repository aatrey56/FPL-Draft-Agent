package insights

import "fpl-draft-mcp/internal/draftapi"

// WeeklyPoints maps: gameweek → league_entry_id → points scored
type WeeklyPoints map[int]map[int]int

// ComputeWeeklyPoints extracts per-team points for each gameweek.
func ComputeWeeklyPoints(details *draftapi.LeagueDetails) WeeklyPoints {
	out := make(WeeklyPoints)

	for _, m := range details.Matches {
		if !m.Started {
			continue
		}

		if _, ok := out[m.Event]; !ok {
			out[m.Event] = make(map[int]int)
		}

		out[m.Event][m.LeagueEntry1] = m.LeagueEntry1Points
		out[m.Event][m.LeagueEntry2] = m.LeagueEntry2Points
	}

	return out
}