package draftapi

import (
	"encoding/json"
	"fmt"
	"time"
)

func (c *Client) GetLeagueDetails(leagueID int, refresh bool) (*LeagueDetails, error) {
	url := fmt.Sprintf(
		"https://draft.premierleague.com/api/league/%d/details",
		leagueID,
	)

	b, err := c.GetJSON(
		fmt.Sprintf("league_%d_details", leagueID),
		url,
		30*time.Minute,
		refresh,
	)
	if err != nil {
		return nil, err
	}

	var details LeagueDetails
	if err := json.Unmarshal(b, &details); err != nil {
		return nil, err
	}

	return &details, nil
}