package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// GameStatusArgs is the input schema for game_status (no parameters).
type GameStatusArgs struct{}

// FixtureProgress tracks how many fixtures have started/finished in a GW.
type FixtureProgress struct {
	Total    int `json:"total"`
	Started  int `json:"started"`
	Finished int `json:"finished"`
}

// GameStatusResult is the output of the game_status tool.
type GameStatusResult struct {
	CurrentGW          int             `json:"current_gw"`
	CurrentGWFinished  bool            `json:"current_gw_finished"`
	NextGW             int             `json:"next_gw"`
	WaiversProcessed   bool            `json:"waivers_processed"`
	ProcessingStatus   string          `json:"processing_status"`
	NextDeadline       string          `json:"next_deadline"`
	NextWaiversDue     string          `json:"next_waivers_due"`
	NextTradesDue      string          `json:"next_trades_due"`
	NextGWFirstKickoff string          `json:"next_gw_first_kickoff,omitempty"`
	CurrentGWFixtures  FixtureProgress `json:"current_gw_fixtures"`
	PointsStatus       string          `json:"points_status"`
}

// gameStatusMeta extends GameMeta with additional fields from game.json.
type gameStatusMeta struct {
	CurrentEvent         int    `json:"current_event"`
	CurrentEventFinished bool   `json:"current_event_finished"`
	NextEvent            int    `json:"next_event"`
	WaiversProcessed     bool   `json:"waivers_processed"`
	ProcessingStatus     string `json:"processing_status"`
}

// bootstrapEvent represents one entry in events.data[] from bootstrap-static.json.
type bootstrapEvent struct {
	ID           int    `json:"id"`
	Finished     bool   `json:"finished"`
	DeadlineTime string `json:"deadline_time"`
	WaiversTime  string `json:"waivers_time"`
	TradesTime   string `json:"trades_time"`
}

// bootstrapFixture represents one fixture from bootstrap-static.json fixtures map.
type bootstrapFixture struct {
	ID          int    `json:"id"`
	Event       int    `json:"event"`
	KickoffTime string `json:"kickoff_time"`
	Started     bool   `json:"started"`
	Finished    bool   `json:"finished"`
}

// liveFixture is the subset of fields from a live.json fixture entry
// needed for fixture progress tracking.
type liveFixture struct {
	ID       int  `json:"id"`
	Event    int  `json:"event"`
	Started  bool `json:"started"`
	Finished bool `json:"finished"`
}

// loadLiveFixtures loads the fixtures array from gw/{gw}/live.json.
func loadLiveFixtures(dataDir string, gw int) ([]liveFixture, error) {
	path := filepath.Join(dataDir, "gw", strconv.Itoa(gw), "live.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var data struct {
		Fixtures []liveFixture `json:"fixtures"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		return nil, fmt.Errorf("parse gw/%d/live.json fixtures: %w", gw, err)
	}
	return data.Fixtures, nil
}

// loadGameStatusMeta reads game/game.json with the full set of status fields.
func loadGameStatusMeta(cfg ServerConfig) (gameStatusMeta, error) {
	path := fmt.Sprintf("%s/game/game.json", strings.TrimRight(cfg.RawRoot, "/"))
	raw, err := os.ReadFile(path)
	if err != nil {
		return gameStatusMeta{}, err
	}
	var meta gameStatusMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return gameStatusMeta{}, err
	}
	return meta, nil
}

// loadBootstrapEvents reads events.data[] from bootstrap-static.json.
func loadBootstrapEvents(rawRoot string) ([]bootstrapEvent, error) {
	path := filepath.Join(rawRoot, "bootstrap", "bootstrap-static.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("bootstrap-static.json: %w", err)
	}
	var resp struct {
		Events struct {
			Data []bootstrapEvent `json:"data"`
		} `json:"events"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("parse bootstrap events: %w", err)
	}
	return resp.Events.Data, nil
}

// loadBootstrapFixturesForGW reads fixtures[gw] from bootstrap-static.json.
// Returns nil (no error) if the GW key is absent (bootstrap drops current GW once started).
func loadBootstrapFixturesForGW(rawRoot string, gw int) ([]bootstrapFixture, error) {
	path := filepath.Join(rawRoot, "bootstrap", "bootstrap-static.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("bootstrap-static.json: %w", err)
	}
	var resp struct {
		Fixtures map[string][]bootstrapFixture `json:"fixtures"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("parse bootstrap fixtures: %w", err)
	}
	return resp.Fixtures[strconv.Itoa(gw)], nil
}

// currentGWFixtureProgress counts started/finished fixtures for a GW.
// Tries live.json first (bootstrap drops current GW once started),
// falls back to bootstrap fixtures.
func currentGWFixtureProgress(rawRoot string, gw int) FixtureProgress {
	// Try live.json first (always has current GW data during/after matches).
	liveFixtures, err := loadLiveFixtures(rawRoot, gw)
	if err == nil && len(liveFixtures) > 0 {
		progress := FixtureProgress{Total: len(liveFixtures)}
		for _, f := range liveFixtures {
			if f.Started {
				progress.Started++
			}
			if f.Finished {
				progress.Finished++
			}
		}
		return progress
	}

	// Fall back to bootstrap fixtures (available before GW starts).
	bsFixtures, err := loadBootstrapFixturesForGW(rawRoot, gw)
	if err != nil || len(bsFixtures) == 0 {
		return FixtureProgress{}
	}
	progress := FixtureProgress{Total: len(bsFixtures)}
	for _, f := range bsFixtures {
		if f.Started {
			progress.Started++
		}
		if f.Finished {
			progress.Finished++
		}
	}
	return progress
}

// derivePointsStatus determines whether points are final, live, or pending.
func derivePointsStatus(finished bool, fixtures FixtureProgress) string {
	if finished {
		return "final"
	}
	if fixtures.Started > 0 {
		return "live"
	}
	return "pending"
}

// earliestKickoff finds the earliest kickoff_time from bootstrap fixtures for a GW.
func earliestKickoff(rawRoot string, gw int) string {
	fixtures, err := loadBootstrapFixturesForGW(rawRoot, gw)
	if err != nil || len(fixtures) == 0 {
		return ""
	}
	earliest := ""
	for _, f := range fixtures {
		if f.KickoffTime == "" {
			continue
		}
		if earliest == "" || f.KickoffTime < earliest {
			earliest = f.KickoffTime
		}
	}
	return earliest
}

// buildGameStatus assembles the full game status response.
func buildGameStatus(cfg ServerConfig) (*GameStatusResult, error) {
	meta, err := loadGameStatusMeta(cfg)
	if err != nil {
		return nil, fmt.Errorf("game.json: %w", err)
	}

	events, err := loadBootstrapEvents(cfg.RawRoot)
	if err != nil {
		return nil, err
	}

	// Find the next unfinished event for deadline times.
	var nextEvent *bootstrapEvent
	for i := range events {
		if !events[i].Finished {
			nextEvent = &events[i]
			break
		}
	}

	result := &GameStatusResult{
		CurrentGW:         meta.CurrentEvent,
		CurrentGWFinished: meta.CurrentEventFinished,
		NextGW:            meta.NextEvent,
		WaiversProcessed:  meta.WaiversProcessed,
		ProcessingStatus:  meta.ProcessingStatus,
	}

	if nextEvent != nil {
		result.NextDeadline = nextEvent.DeadlineTime
		result.NextWaiversDue = nextEvent.WaiversTime
		result.NextTradesDue = nextEvent.TradesTime
	}

	result.NextGWFirstKickoff = earliestKickoff(cfg.RawRoot, meta.NextEvent)

	result.CurrentGWFixtures = currentGWFixtureProgress(cfg.RawRoot, meta.CurrentEvent)

	result.PointsStatus = derivePointsStatus(meta.CurrentEventFinished, result.CurrentGWFixtures)

	return result, nil
}

// gameStatusHandler is the MCP tool handler for game_status.
func gameStatusHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, GameStatusArgs) (*mcp.CallToolResult, any, error) {
	return func(ctx context.Context, req *mcp.CallToolRequest, args GameStatusArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildGameStatus(cfg)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolMarshal(out)
	}
}
