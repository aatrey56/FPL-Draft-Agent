package insights

import "github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/draftapi"

// TeamNameMap builds a lookup: league_entry_id -> team name
func TeamNameMap(details *draftapi.LeagueDetails) map[int]string {
	out := make(map[int]string)
	for _, e := range details.LeagueEntries {
		out[e.ID] = e.EntryName
	}
	return out
}