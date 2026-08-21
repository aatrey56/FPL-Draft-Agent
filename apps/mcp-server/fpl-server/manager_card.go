package main

// manager_card — everything about one manager in a single call.
//
// Consolidates the former manager_lookup / manager_season / manager_streak /
// manager_schedule / head_to_head / draft_picks tools: one parameterized tool
// beats six narrow ones (the model picks it reliably, and "tell me about this
// manager" is one workflow, not six). Sections are best-effort: early in a
// season some sections have no data yet — they degrade to a section_error
// instead of failing the whole card.

import (
	"context"
	"fmt"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type ManagerCardArgs struct {
	LeagueID     int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID      *int    `json:"entry_id,omitempty" jsonschema:"Entry id of the manager"`
	EntryName    *string `json:"entry_name,omitempty" jsonschema:"Team name (if entry_id not provided)"`
	VsEntryID    *int    `json:"vs_entry_id,omitempty" jsonschema:"Opponent entry id: include the head-to-head record vs them"`
	VsEntryName  *string `json:"vs_entry_name,omitempty" jsonschema:"Opponent team name (if vs_entry_id not provided)"`
	IncludeDraft bool    `json:"include_draft,omitempty" jsonschema:"Include the manager's original draft picks"`
}

func managerCardHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, ManagerCardArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args ManagerCardArgs) (*mcp.CallToolResult, any, error) {
		if args.LeagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		if args.EntryID == nil && args.EntryName == nil {
			return toolError(fmt.Errorf("entry_id or entry_name is required")), nil, nil
		}
		card := map[string]any{}

		if season, err := buildManagerSeason(cfg, ManagerSeasonArgs{
			LeagueID: args.LeagueID, EntryID: args.EntryID, EntryName: args.EntryName,
		}); err == nil {
			card["season"] = season
		} else {
			card["season_error"] = err.Error()
		}

		if streak, err := buildManagerStreak(cfg, ManagerStreakArgs{
			LeagueID: args.LeagueID, EntryID: args.EntryID, EntryName: args.EntryName,
		}); err == nil {
			card["streak"] = streak
		} else {
			card["streak_error"] = err.Error()
		}

		if schedule, err := buildManagerSchedule(cfg, ManagerScheduleArgs{
			LeagueID: args.LeagueID, EntryID: args.EntryID, EntryName: args.EntryName,
		}); err == nil {
			card["schedule"] = schedule
		} else {
			card["schedule_error"] = err.Error()
		}

		if args.VsEntryID != nil || args.VsEntryName != nil {
			if h2h, err := buildHeadToHead(cfg, HeadToHeadArgs{
				LeagueID: args.LeagueID,
				EntryIDA: args.EntryID, EntryNameA: args.EntryName,
				EntryIDB: args.VsEntryID, EntryNameB: args.VsEntryName,
			}); err == nil {
				card["head_to_head"] = h2h
			} else {
				card["head_to_head_error"] = err.Error()
			}
		}

		if args.IncludeDraft {
			if picks, err := buildDraftPicks(cfg, DraftPicksArgs{
				LeagueID: args.LeagueID, EntryID: args.EntryID, EntryName: args.EntryName,
			}); err == nil {
				card["draft_picks"] = picks
			} else {
				card["draft_picks_error"] = err.Error()
			}
		}

		card["note"] = "Sections are best-effort: early-season cards may carry section_error where no finished-GW data exists yet. Pair with current_roster for their squad and gw_live for the live matchup."
		return toolMarshal(card)
	}
}
