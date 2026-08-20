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

// /league/{league_id}/element-status — ownership of every element in a draft
// league: owner (entry id or null = free agent), waiver status, trade flag.
// This is the feed for the drop-radar: latest.json is overwritten each fetch,
// and ArchiveElementStatus keeps timestamped snapshots for diffing.
func (c *Client) LeagueElementStatus(leagueID int, force bool) ([]byte, error) {
	return c.FetchRaw(
		fmt.Sprintf("/league/%d/element-status", leagueID),
		fmt.Sprintf("league/%d/element-status.json", leagueID),
		force,
	)
}

// ArchiveElementStatus writes an already-fetched element-status body to a
// timestamped snapshot path (UTC, minute precision) so consecutive snapshots
// can be diffed into ownership events (drops, adds, trades).
func (c *Client) ArchiveElementStatus(leagueID int, body []byte, ts string) error {
	if c.DisableWrite {
		return nil
	}
	return c.Store.WriteRaw(
		fmt.Sprintf("league/%d/element_status_history/%s.json", leagueID, ts),
		body,
		c.PrettyWrite,
	)
}

// /element-summary/{element_id} — per-player upcoming fixtures + per-GW history.
func (c *Client) ElementSummary(elementID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/element-summary/%d", elementID),
		fmt.Sprintf("element-summary/%d.json", elementID),
		force,
	)
	return err
}

// /entry/{entry_id}/public — public profile for a manager entry.
func (c *Client) EntryPublic(entryID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/entry/%d/public", entryID),
		fmt.Sprintf("entry/%d/public.json", entryID),
		force,
	)
	return err
}

// /entry/{entry_id}/history — per-GW points history for a manager entry.
func (c *Client) EntryHistory(entryID int, force bool) error {
	_, err := c.FetchRaw(
		fmt.Sprintf("/entry/%d/history", entryID),
		fmt.Sprintf("entry/%d/history.json", entryID),
		force,
	)
	return err
}
