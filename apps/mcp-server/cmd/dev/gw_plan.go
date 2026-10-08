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
//     validateLivePayload: integer total_points/minutes for every element, and
//     every player picked in that GW's cached entry-event files present) and lists
//     fixtures that all have finished == true — FPL sets a fixture's finished
//     (as opposed to finished_provisional) once bonus is confirmed, so points
//     have stopped moving. A cached file that predates confirmation, or that
//     is partial, keeps the GW unsettled and it is refetched until the
//     confirmed version lands.
//
// Independently of settlement, a cached file that fails validation (live
// payload without usable player stats or missing a picked player, entry-event
// file that is not a well-formed full squad) is force-redownloaded with a
// structured "forcing refetch" log line, because downstream builders silently
// turn a partial file into zero points or a missing GW.
//
// Entry-event files are immutable-but-empty for gameweeks before an entry took
// part (a league that started after GW1, or a late joiner): see participation.

import (
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strconv"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/fetch"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
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
// as player points: unparseable, no elements, an element whose
// stats.total_points / stats.minutes are absent, null or not integers (the
// same int decode loadLiveStatsForPoints performs), or — when required is
// non-empty — a player in required (element ids picked by the league's
// entries that GW) with no element at all.
func validateLivePayload(raw []byte, required map[int]bool) error {
	live, err := parseLivePayload(raw)
	if err != nil {
		return err
	}
	if len(live.Elements) == 0 {
		return errors.New("no elements")
	}
	for id, el := range live.Elements {
		for _, key := range []string{"total_points", "minutes"} {
			stat, ok := el.Stats[key]
			if !ok {
				return fmt.Errorf("element %s missing stats.%s", id, key)
			}
			var v *int
			if err := json.Unmarshal(stat, &v); err != nil || v == nil {
				return fmt.Errorf("element %s stats.%s is not an integer: %s", id, key, stat)
			}
		}
	}
	var missing []int
	for id := range required {
		if _, ok := live.Elements[strconv.Itoa(id)]; !ok {
			missing = append(missing, id)
		}
	}
	if len(missing) > 0 {
		sort.Ints(missing)
		return fmt.Errorf("%d picked players missing from live payload (first: %d)", len(missing), missing[0])
	}
	return nil
}

// validateEntryEvent reports why a /entry/{id}/event/{gw} body cannot be
// trusted: unparseable, or not a well-formed full draft squad. Each pick is
// decoded into the struct buildEntrySnapshots uses and must carry element > 0
// and a position in 1..15, with elements and positions unique. allowEmpty
// accepts a response with no picks at all (a GW before the entry took part).
func validateEntryEvent(raw []byte, allowEmpty bool) error {
	var ev struct {
		Picks []json.RawMessage `json:"picks"`
	}
	if err := json.Unmarshal(raw, &ev); err != nil {
		return fmt.Errorf("invalid JSON: %w", err)
	}
	if len(ev.Picks) == 0 && allowEmpty {
		return nil
	}
	if len(ev.Picks) != draftSquadSize {
		return fmt.Errorf("picks has %d entries, want %d", len(ev.Picks), draftSquadSize)
	}
	elements := map[int]bool{}
	positions := map[int]bool{}
	for i, rawPick := range ev.Picks {
		var pick ledger.EntryPick
		if err := json.Unmarshal(rawPick, &pick); err != nil {
			return fmt.Errorf("pick %d: %w", i, err)
		}
		switch {
		case pick.Element <= 0:
			return fmt.Errorf("pick %d: element %d is not a player id", i, pick.Element)
		case pick.Position < 1 || pick.Position > draftSquadSize:
			return fmt.Errorf("pick %d: position %d outside 1..%d", i, pick.Position, draftSquadSize)
		case elements[pick.Element]:
			return fmt.Errorf("pick %d: duplicate element %d", i, pick.Element)
		case positions[pick.Position]:
			return fmt.Errorf("pick %d: duplicate position %d", i, pick.Position)
		}
		elements[pick.Element] = true
		positions[pick.Position] = true
	}
	return nil
}

// requiredElements returns the player ids picked in gw by the entries whose
// cached entry-event file is readable — the players whose live points
// BuildResult looks up. Picks are read leniently (a malformed file is
// refetched on its own); an empty result means no usable entry-event files
// exist yet, and the caller falls back to the payload-only check.
func requiredElements(st *store.JSONStore, entryIDs []int, gw int) map[int]bool {
	out := map[int]bool{}
	for _, entryID := range entryIDs {
		raw, err := st.ReadRaw(fetch.EntryEventPath(entryID, gw))
		if err != nil {
			continue
		}
		var ev ledger.EntryEventRaw
		if json.Unmarshal(raw, &ev) != nil {
			continue
		}
		for _, p := range ev.Picks {
			if p.Element > 0 {
				out[p.Element] = true
			}
		}
	}
	return out
}

// participation records when each entry's squad history begins, so that an
// empty /entry/{id}/event/{gw} response for a GW the entry never played is
// recognised as final instead of refetched on every run.
//
// Rule: an entry's first GW is the later of the league's start_event
// (league/<id>/details.json: league.start_event) and the first row of the
// entry's own history (entry/<id>/history.json: history[].event, which catches
// late joiners). A GW before that first GW may legitimately have no picks.
// Either source missing or unreadable counts as unknown (0); with both
// unknown, nothing is exempt and an empty picks response is always refetched.
type participation struct {
	leagueStart int
	entryStart  map[int]int
}

// firstGW is the first gameweek entryID took part in, 0 when unknown.
func (p participation) firstGW(entryID int) int {
	if p.entryStart[entryID] > p.leagueStart {
		return p.entryStart[entryID]
	}
	return p.leagueStart
}

// beforeStart reports whether gw precedes the entry's first known GW.
func (p participation) beforeStart(entryID, gw int) bool {
	start := p.firstGW(entryID)
	return start > 0 && gw < start
}

// loadParticipation reads the cached league details and entry histories.
func loadParticipation(st *store.JSONStore, leagueID int, entryIDs []int) participation {
	p := participation{entryStart: map[int]int{}}
	if raw, err := st.ReadRaw(fmt.Sprintf("league/%d/details.json", leagueID)); err == nil {
		var ld struct {
			League struct {
				StartEvent int `json:"start_event"`
			} `json:"league"`
		}
		if err := json.Unmarshal(raw, &ld); err != nil {
			slog.Warn("participation: league details unreadable", "error", err.Error())
		} else {
			p.leagueStart = ld.League.StartEvent
		}
	}
	for _, entryID := range entryIDs {
		raw, err := st.ReadRaw(fmt.Sprintf("entry/%d/history.json", entryID))
		if err != nil {
			continue
		}
		var h struct {
			History []struct {
				Event int `json:"event"`
			} `json:"history"`
		}
		if err := json.Unmarshal(raw, &h); err != nil {
			slog.Warn("participation: entry history unreadable", "entry", entryID, "error", err.Error())
			continue
		}
		for _, row := range h.History {
			if row.Event > 0 && (p.entryStart[entryID] == 0 || row.Event < p.entryStart[entryID]) {
				p.entryStart[entryID] = row.Event
			}
		}
	}
	return p
}

// settledGWs returns the gameweeks whose points can no longer change, read
// from the cached bootstrap and live files (see the package note above for
// the definition). It errors only when the bootstrap is unreadable.
func settledGWs(st *store.JSONStore, currentEvent int, entryIDs []int) (map[int]bool, error) {
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
		if ev.ID < currentEvent && ev.Finished && liveConfirmed(st, ev.ID, entryIDs) {
			settled[ev.ID] = true
		}
	}
	return settled, nil
}

// liveConfirmed reports whether the cached live file for gw is a complete
// payload (covering every player picked by entryIDs that GW) and every fixture
// in it is finished (bonus confirmed).
func liveConfirmed(st *store.JSONStore, gw int, entryIDs []int) bool {
	raw, err := st.ReadRaw(fetch.EventLivePath(gw))
	if err != nil || validateLivePayload(raw, requiredElements(st, entryIDs, gw)) != nil {
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
// force-redownloaded whatever the refresh flags say. part supplies the
// per-entry first GW, so empty picks before it are not treated as corrupt.
func planGWFetches(client *fetch.Client, entryIDs []int, minGW, maxGW int, refresh, refetchAll bool, settled map[int]bool, part participation) gwFetchPlan {
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
		required := requiredElements(client.Store, entryIDs, gw)
		validateLive := func(raw []byte) error { return validateLivePayload(raw, required) }
		add(fmt.Sprintf("event_live gw=%d", gw), fetch.EventLivePath(gw), force, validateLive, func(force bool) error {
			return client.EventLive(gw, force)
		})
		for _, entryID := range entryIDs {
			entryID := entryID
			allowEmpty := part.beforeStart(entryID, gw)
			validateEntry := func(raw []byte) error { return validateEntryEvent(raw, allowEmpty) }
			add(fmt.Sprintf("entry_event entry=%d gw=%d", entryID, gw), fetch.EntryEventPath(entryID, gw), force, validateEntry, func(force bool) error {
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
