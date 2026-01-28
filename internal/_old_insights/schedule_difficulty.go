package insights

import (
	"sort"

	"fpl-draft-mcp/internal/draftapi"
)

type OpponentInfo struct {
	Gameweek int     `json:"gameweek"`
	Opponent int     `json:"opponent_id"`
	Form     float64 `json:"opponent_form"`
}

type ScheduleDifficultyRow struct {
	TeamID     int             `json:"team_id"`
	Difficulty float64         `json:"difficulty"`
	Opponents  []OpponentInfo  `json:"opponents"`
}

// ComputeScheduleDifficulty computes schedule difficulty for each team.
// Difficulty = average recent form of the next `lookahead` opponents.
func ComputeScheduleDifficulty(
	details *draftapi.LeagueDetails,
	weekly WeeklyPoints,
	asOfGW int,
	lookahead int,
	formWindow int,
) []ScheduleDifficultyRow {

	if lookahead <= 0 {
		lookahead = 3
	}
	if formWindow <= 0 {
		formWindow = 3
	}

	// opponentMap[gw][team] = opponent
	opponentMap := make(map[int]map[int]int)

	for _, m := range details.Matches {
		if !m.Started {
			continue
		}

		if _, ok := opponentMap[m.Event]; !ok {
			opponentMap[m.Event] = make(map[int]int)
		}

		opponentMap[m.Event][m.LeagueEntry1] = m.LeagueEntry2
		opponentMap[m.Event][m.LeagueEntry2] = m.LeagueEntry1
	}

	rows := make([]ScheduleDifficultyRow, 0)

	for _, entry := range details.LeagueEntries {
		row := ScheduleDifficultyRow{
			TeamID: entry.ID,
		}

		total := 0.0
		count := 0

		for gw := asOfGW + 1; gw <= 38 && count < lookahead; gw++ {
			oppByTeam, ok := opponentMap[gw]
			if !ok {
				continue
			}

			oppID, ok := oppByTeam[entry.ID]
			if !ok {
				continue
			}

			oppForm := Form(weekly, oppID, gw, formWindow)

			row.Opponents = append(row.Opponents, OpponentInfo{
				Gameweek: gw,
				Opponent: oppID,
				Form:     oppForm,
			})

			total += oppForm
			count++
		}

		if count > 0 {
			row.Difficulty = total / float64(count)
		} else {
			row.Difficulty = 0
		}

		rows = append(rows, row)
	}

	// Lower difficulty = easier schedule
	sort.Slice(rows, func(i, j int) bool {
		return rows[i].Difficulty < rows[j].Difficulty
	})

	return rows
}