package draftapi

// LeagueDetails represents the response from
// /api/league/{league_id}/details
type LeagueDetails struct {
	League LeagueMeta `json:"league"`

	LeagueEntries []LeagueEntry `json:"league_entries"`
	Matches       []Match       `json:"matches"`
	Standings     []Standing    `json:"standings"`
}

type LeagueMeta struct {
	ID   int    `json:"id"`
	Name string `json:"name"`
}

type LeagueEntry struct {
	// IMPORTANT:
	// ID is the league_entry_id used everywhere else
	ID        int    `json:"id"`
	EntryID   int    `json:"entry_id"`
	EntryName string `json:"entry_name"`
	ShortName string `json:"short_name"`
	WaiverPick int   `json:"waiver_pick"`
}

type Match struct {
	Event    int  `json:"event"`
	Finished bool `json:"finished"`
	Started  bool `json:"started"`

	LeagueEntry1       int `json:"league_entry_1"`
	LeagueEntry1Points int `json:"league_entry_1_points"`

	LeagueEntry2       int `json:"league_entry_2"`
	LeagueEntry2Points int `json:"league_entry_2_points"`
}

type Standing struct {
	LeagueEntry   int `json:"league_entry"`
	Rank          int `json:"rank"`
	Total         int `json:"total"`
	PointsFor     int `json:"points_for"`
	PointsAgainst int `json:"points_against"`
	MatchesWon    int `json:"matches_won"`
	MatchesDrawn  int `json:"matches_drawn"`
	MatchesLost   int `json:"matches_lost"`
}