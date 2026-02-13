package model

type Squad struct {
	EntryID   int   `json:"entry_id"`
	PlayerIDs []int `json:"player_ids"`
}
