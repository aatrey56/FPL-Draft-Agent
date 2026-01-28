package model

type DraftPick struct {
	EntryID    int    `json:"entry_id"`
	EntryName  string `json:"entry_name"`
	Element    int    `json:"element"`
	Round      int    `json:"round"`
	Pick       int    `json:"pick"`
	Index      int    `json:"index"`
	ChoiceTime string `json:"choice_time"`
	WasAuto    bool   `json:"was_auto"`
}

type DraftLedger struct {
	LeagueID       int        `json:"league_id"`
	Event          int        `json:"event"`
	GeneratedAtUTC string     `json:"generated_at_utc"`
	Managers       []Manager  `json:"managers"`
	Squads         []Squad    `json:"squads"`
	Picks          []DraftPick `json:"picks"`
}
