package main

// Per-GW fetch planning: which /event/{gw}/live and /entry/{id}/event/{gw}
// requests a run actually has to make.
//
// Without this every forced refresh (make fetch passes --refresh-now, and the
// autopilot runs it every 15 minutes) re-downloaded every GW since GW1 for
// every entry. A finished GW's data stops changing, so a settled GW whose
// files are already on disk is skipped; --refetch-all restores the full pull.
//
// "Settled" — the draft API has no data_checked flag (bootstrap events.data
// carries only id/name/deadline_time/finished/...), so it is defined from the
// fields that do exist:
//
//  1. gw < game.current_event: a later GW's deadline has passed, so this is
//     never the current/live GW (the current GW is always refetched, even
//     between gameweeks while it is still current);
//  2. the bootstrap event for gw has finished == true;
//  3. the cached gw/<n>/live.json is a complete payload (see
//     validateLivePayload: elements with per-player stats) and lists
//     fixtures that all have finished == true — FPL sets a fixture's finished
//     (as opposed to finished_provisional) once bonus is confirmed, so points
//     have stopped moving. A cached file that predates confirmation, or that
//     is partial, keeps the GW unsettled and it is refetched until the
//     confirmed version lands.
//
// Independently of settlement, a cached file that fails validation (live
// payload without player stats, entry-event file that is not a full squad) is
// force-redownloaded with a structured "forcing refetch" log line, because
// downstream builders silently turn a partial file into zero points or a
// missing GW.

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/fetch"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

// bootstrapEvents is the slice of bootstrap-static.json the settled check reads.
type bootstrapEvents struct {
	Events struct {
		Data []struct {
			ID       int  `json:"id"`
			Finished bool `json:"finished"`
		} `json:"data"`
	} `json:"events"`
}

// draftSquadSize is the number of picks a draft entry-event always carries
// (11 starters + 4 bench); anything else is a partial or corrupt download.
const draftSquadSize = 15

// livePayload is the slice of gw/<n>/live.json the settled check reads.
type livePayload struct {
	Elements map[string]struct {
		Stats map[string]json.RawMessage `json:"stats"`
	} `json:"elements"`
	Fixtures []struct {
		Finished bool `json:"finished"`
	} `json:"fixtures"`
}

func parseLivePayload(raw []byte) (livePayload, error) {
	var live livePayload
	if err := json.Unmarshal(raw, &live); err != nil {
		return live, fmt.Errorf("invalid JSON: %w", err)
	}
	return live, nil
}

// validateLivePayload reports why a gw/<n>/live.json body cannot be trusted
// as player points: unparseable, no elements, or an element missing the
// total_points / minutes stats that loadLiveStatsForPoints turns into totals.
func validateLivePayload(raw []byte) error {
	live, err := parseLivePayload(raw)
	if err != nil {
		return err
	}
	if len(live.Elements) == 0 {
		return errors.New("no elements")
	}
	for id, el := range live.Elements {
		for _, key := range []string{"total_points", "minutes"} {
			if _, ok := el.Stats[key]; !ok {
				return fmt.Errorf("element %s missing stats.%s", id, key)
			}
		}
	}
	return nil
}

// validateEntryEvent reports why a /entry/{id}/event/{gw} body cannot be
// trusted: unparseable or not a full draft squad of picks.
func validateEntryEvent(raw []byte) error {
	var ev struct {
		Picks []json.RawMessage `json:"picks"`
	}
	if err := json.Unmarshal(raw, &ev); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if len(ev.Picks) != draftSquadSize {
		return fmt.Errorf("picks has %d entries, want %d", len(ev.Picks), draftSquadSize)
	}
	return nil
}

// settledGWs returns the gameweeks whose points can no longer change, read
// from the cached bootstrap and live files (see the package note above for
// the definition). It errors only when the bootstrap is unreadable.
func settledGWs(st *store.JSONStore, currentEvent int) (map[int]bool, error) {
	raw, err := st.ReadRaw("bootstrap/bootstrap-static.json")
	if err != nil {
		return nil, err
	}
	var boot bootstrapEvents
	if err := json.Unmarshal(raw, &boot); err != nil {
		return nil, fmt.Errorf("parse bootstrap events: %w", err)
	}
	settled := map[int]bool{}
	for _, ev := range boot.Events.Data {
		if ev.ID < currentEvent && ev.Finished && liveConfirmed(st, ev.ID) {
			settled[ev.ID] = true
		}
	}
	return settled, nil
}

// liveConfirmed reports whether the cached live file for gw is a complete
// payload and every fixture in it is finished (bonus confirmed).
func liveConfirmed(st *store.JSONStore, gw int) bool {
	raw, err := st.ReadRaw(fetch.EventLivePath(gw))
	if err != nil || validateLivePayload(raw) != nil {
		return false
	}
	live, err := parseLivePayload(raw)
	if err != nil || len(live.Fixtures) == 0 {
		return false
	}
	for _, fx := range live.Fixtures {
		if !fx.Finished {
			return false
		}
	}
	return true
}

// gwFetchPlan is the per-GW request list for one run plus the counts logged.
type gwFetchPlan struct {
	Tasks      []fetchTask
	Fetched    int   // requests that will hit the network
	Skipped    int   // requests served from disk (settled or cached)
	SettledGWs []int // settled GWs in [minGW, maxGW], ascending
}

// planGWFetches builds the live + entry-event requests for GWs minGW..maxGW.
// refresh forces a re-download of every GW that is not settled; refetchAll
// forces every GW, settled or not. A request is skipped (no task) when it is
// not forced and its file is already cached and passes validate; a missing
// file is always fetched, and a cached file that fails validate is
// force-redownloaded whatever the refresh flags say.
func planGWFetches(client *fetch.Client, entryIDs []int, minGW, maxGW int, refresh, refetchAll bool, settled map[int]bool) gwFetchPlan {
	var plan gwFetchPlan
	add := func(label, relPath string, force bool, validate func([]byte) error, fn func(force bool) error) {
		if !force && client.UseCache && client.Store.Exists(relPath) {
			raw, err := client.Store.ReadRaw(relPath)
			if err == nil {
				err = validate(raw)
			}
			if err == nil {
				plan.Skipped++
				return
			}
			slog.Warn("forcing refetch", "request", label, "path", relPath, "reason", err.Error())
			force = true
		}
		plan.Fetched++
		plan.Tasks = append(plan.Tasks, fetchTask{label: label, fn: func() error { return fn(force) }})
	}
	for gw := minGW; gw <= maxGW; gw++ {
		gw := gw
		if settled[gw] {
			plan.SettledGWs = append(plan.SettledGWs, gw)
		}
		force := refetchAll || (refresh && !settled[gw])
		add(fmt.Sprintf("event_live gw=%d", gw), fetch.EventLivePath(gw), force, validateLivePayload, func(force bool) error {
			return client.EventLive(gw, force)
		})
		for _, entryID := range entryIDs {
			entryID := entryID
			add(fmt.Sprintf("entry_event entry=%d gw=%d", entryID, gw), fetch.EntryEventPath(entryID, gw), force, validateEntryEvent, func(force bool) error {
				return client.EntryEvent(entryID, gw, force)
			})
		}
	}
	return plan
}

// logGWFetchPlan emits one structured line summarising the plan.
func logGWFetchPlan(plan gwFetchPlan, minGW, maxGW int, refetchAll bool) {
	slog.Info("gw fetch plan",
		"gw_min", minGW,
		"gw_max", maxGW,
		"settled_gws", len(plan.SettledGWs),
		"requests_fetched", plan.Fetched,
		"requests_skipped", plan.Skipped,
		"refetch_all", refetchAll,
	)
}
