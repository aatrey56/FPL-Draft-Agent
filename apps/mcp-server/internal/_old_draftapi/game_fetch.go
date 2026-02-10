package draftapi

import (
	"encoding/json"
	"time"
)

func (c *Client) GetGame(refresh bool) (*Game, error) {
	b, err := c.GetJSON(
		"game",
		"https://draft.premierleague.com/api/game",
		30*time.Second,
		refresh,
	)
	if err != nil {
		return nil, err
	}

	var g Game
	if err := json.Unmarshal(b, &g); err != nil {
		return nil, err
	}
	return &g, nil
}