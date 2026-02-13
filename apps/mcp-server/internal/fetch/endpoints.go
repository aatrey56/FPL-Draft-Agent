package fetch

import "fmt"

// /league/{league_id}/details
func (c *Client) LeagueDetails(leagueID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/league/%d/details", leagueID),
		fmt.Sprintf("league/%d/details.json", leagueID),
		force,
	)
	return err
}

// /bootstrap-static
func (c *Client) BootstrapStatic(force bool) error {
	_, err := c.FetchRaw(
		"/bootstrap-static",
		"bootstrap/bootstrap-static.json",
		force,
	)
	return err
}

// /draft/{league_id}/choices
func (c *Client) DraftChoices(leagueID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/draft/%d/choices", leagueID),
		fmt.Sprintf("draft/%d/choices.json", leagueID),
		force,
	)
	return err
}

// /draft/league/{league_id}/transactions
func (c *Client) LeagueTransactions(leagueID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/draft/league/%d/transactions", leagueID),
		fmt.Sprintf("league/%d/transactions.json", leagueID),
		force,
	)
	return err
}

// /draft/league/{league_id}/trades
func (c *Client) LeagueTrades(leagueID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/draft/league/%d/trades", leagueID),
		fmt.Sprintf("league/%d/trades.json", leagueID),
		force,
	)
	return err
}

// /game
func (c *Client) GameMeta(force bool) ([]byte, error) {
	return c.FetchRaw("/game", "game/game.json", force)
}

// /event/{gw}/live
func (c *Client) EventLive(gw int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/event/%d/live", gw),
		fmt.Sprintf("gw/%d/live.json", gw),
		force,
	)
	return err
}

// /entry/{entry_id}/event/{gw}
func (c *Client) EntryEvent(entryID int, gw int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/entry/%d/event/%d", entryID, gw),
		fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw),
		force,
	)
	return err
}
