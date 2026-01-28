package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"fpl-draft-mcp/internal/fetch"
	"fpl-draft-mcp/internal/ledger"
	"fpl-draft-mcp/internal/model"
	"fpl-draft-mcp/internal/points"
	"fpl-draft-mcp/internal/reconcile"
	"fpl-draft-mcp/internal/store"
	"fpl-draft-mcp/internal/summary"
)

type GameMeta struct {
	CurrentEvent         int  `json:"current_event"`
	CurrentEventFinished bool `json:"current_event_finished"`
	WaiversProcessed     bool `json:"waivers_processed"`
}

func main() {
	var (
		leagueID        = flag.Int("league", 14204, "draft league id")
		gwMin           = flag.Int("gw-min", 1, "minimum gameweek to fetch (default 1)")
		gwMax           = flag.Int("gw-max", 0, "maximum gameweek to fetch (0 = current)")
		rawRoot         = flag.String("raw-root", "data/raw", "root directory for raw JSON")
		derivedRoot     = flag.String("derived-root", "data/derived", "root directory for derived JSON")
		pretty          = flag.Bool("pretty", true, "pretty-print JSON to disk")
		sleepMS         = flag.Int("sleep-ms", 250, "sleep between requests in ms")
		refreshMode     = flag.String("refresh", "scheduled", "refresh mode: none|scheduled|all")
		live            = flag.Bool("live", false, "disable cache and disk writes")
		refreshNow      = flag.Bool("refresh-now", false, "force refresh regardless of schedule")
		deriveDraft     = flag.Bool("derive-draft", true, "build draft ledger from choices")
		deriveSnaps     = flag.Bool("derive-snapshots", true, "build entry snapshots from raw entry events")
		reconcileOn     = flag.Bool("reconcile", true, "compare draft ledger vs snapshots and write mismatch report")
		summaryHorizons = flag.String("summary-horizons", "5,10,20", "comma-separated horizons in GWs for summaries")
		summaryRisks    = flag.String("summary-risks", "low,med,high", "comma-separated risk levels for summaries")
	)
	flag.Parse()

	st := store.NewJSONStore(*rawRoot)
	client := fetch.NewClient(st)
	client.PrettyWrite = *pretty && !*live
	client.Sleep = time.Duration(*sleepMS) * time.Millisecond
	client.UseCache = !*live
	client.DisableWrite = *live

	now := time.Now()
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		log.Fatal(err)
	}

	// Determine refresh policy.
	mode := *refreshMode
	if mode != "none" && mode != "scheduled" && mode != "all" {
		log.Fatalf("invalid refresh mode: %s", mode)
	}

	scheduledActive := mode == "scheduled" && isScheduledWindow(now.In(loc))
	forceAll := mode == "all" || *refreshNow

	// Always fetch game meta; force refresh only when needed to gate decisions.
	gameBody, err := client.GameMeta(forceAll || scheduledActive)
	must(err)

	var game GameMeta
	if err := json.Unmarshal(gameBody, &game); err != nil {
		log.Fatal(err)
	}

	refreshBootstrap := forceAll || scheduledActive
	refreshDraftChoices := forceAll
	refreshTransactions := forceAll || (scheduledActive && game.WaiversProcessed)
	refreshLeagueDetails := forceAll || (scheduledActive && (game.WaiversProcessed || game.CurrentEventFinished))
	refreshLive := forceAll || (scheduledActive && game.CurrentEventFinished)
	refreshEntry := refreshLive

	log.Printf("Refresh mode=%s scheduled=%v finished=%v waivers=%v\n",
		mode, scheduledActive, game.CurrentEventFinished, game.WaiversProcessed)

	must(client.BootstrapStatic(refreshBootstrap))
	must(client.DraftChoices(*leagueID, refreshDraftChoices))
	must(client.LeagueTransactions(*leagueID, refreshTransactions))
	must(client.LeagueTrades(*leagueID, refreshTransactions))
	must(client.LeagueDetails(*leagueID, refreshLeagueDetails))

	// Read league details from disk to get entry IDs.
	ldPath := fmt.Sprintf("league/%d/details.json", *leagueID)
	raw, err := st.ReadRaw(ldPath)
	must(err)

	var ld summary.LeagueDetails
	must(json.Unmarshal(raw, &ld))

	entryIDs := make([]int, 0, len(ld.LeagueEntries))
	for _, e := range ld.LeagueEntries {
		entryIDs = append(entryIDs, e.EntryID)
	}
	log.Printf("Found %d entry IDs\n", len(entryIDs))

	minGW := *gwMin
	maxGW := *gwMax
	if maxGW == 0 {
		maxGW = game.CurrentEvent
	}
	if minGW == 0 {
		minGW = 1
	}

	for gw := minGW; gw <= maxGW; gw++ {
		log.Printf("Fetching GW %d live...\n", gw)
		must(client.EventLive(gw, refreshLive))

		for _, entryID := range entryIDs {
			must(client.EntryEvent(entryID, gw, refreshEntry))
		}
	}

	if *deriveDraft {
		if client.DisableWrite {
			log.Println("derive-draft skipped in live mode")
		} else {
			must(buildDraftLedger(st, *derivedRoot, *leagueID))
		}
	}

	if *deriveSnaps {
		if client.DisableWrite {
			log.Println("derive-snapshots skipped in live mode")
		} else {
			must(buildEntrySnapshots(st, *derivedRoot, *leagueID, entryIDs, minGW, maxGW))
		}
	}

	if *reconcileOn {
		if client.DisableWrite {
			log.Println("reconcile skipped in live mode")
		} else {
			must(buildReconcileReports(st, *derivedRoot, *leagueID, entryIDs, minGW, maxGW))
		}
	}

	if client.DisableWrite {
		log.Println("derive-points skipped in live mode")
	} else {
		must(buildPointsResults(st, *derivedRoot, *leagueID, entryIDs, minGW, maxGW))
	}

	if client.DisableWrite {
		log.Println("derive-summaries skipped in live mode")
	} else {
		horizons, err := summary.ParseHorizons(*summaryHorizons)
		must(err)
		riskLevels := summary.ParseRiskLevels(*summaryRisks)
		must(summary.BuildLeagueSummaries(st, *derivedRoot, *leagueID, ld, entryIDs, minGW, maxGW, horizons, riskLevels))
	}

	log.Println("Done.")
}

// Scheduled refresh window:
// - Tuesday after 11:00am EST
// - Friday after 7:00pm EST
func isScheduledWindow(t time.Time) bool {
	switch t.Weekday() {
	case time.Tuesday:
		return t.Hour() > 11 || (t.Hour() == 11 && t.Minute() >= 0)
	case time.Friday:
		return t.Hour() > 19 || (t.Hour() == 19 && t.Minute() >= 0)
	default:
		return false
	}
}

func buildDraftLedger(st *store.JSONStore, derivedRoot string, leagueID int) error {
	raw, err := st.ReadRaw(fmt.Sprintf("draft/%d/choices.json", leagueID))
	if err != nil {
		return err
	}

	var resp ledger.DraftChoicesResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return err
	}

	out := ledger.BuildDraftLedger(leagueID, resp.Choices)
	outPath := filepath.Join(derivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	return ledger.WriteDraftLedger(outPath, out)
}

func buildEntrySnapshots(st *store.JSONStore, derivedRoot string, leagueID int, entryIDs []int, minGW int, maxGW int) error {
	for gw := minGW; gw <= maxGW; gw++ {
		for _, entryID := range entryIDs {
			raw, err := st.ReadRaw(fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw))
			if err != nil {
				return err
			}

			var resp ledger.EntryEventRaw
			if err := json.Unmarshal(raw, &resp); err != nil {
				return err
			}

			snap := ledger.BuildEntrySnapshot(leagueID, entryID, gw, resp)
			outPath := filepath.Join(derivedRoot, fmt.Sprintf("snapshots/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
			if err := ledger.WriteEntrySnapshot(outPath, snap); err != nil {
				return err
			}
		}
	}
	return nil
}

func buildReconcileReports(st *store.JSONStore, derivedRoot string, leagueID int, entryIDs []int, minGW int, maxGW int) error {
	ledgerPath := filepath.Join(derivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	ledgerRaw, err := os.ReadFile(ledgerPath)
	if err != nil {
		return err
	}

	var ledgerOut model.DraftLedger
	if err := json.Unmarshal(ledgerRaw, &ledgerOut); err != nil {
		return err
	}

	transactions, err := loadTransactions(st, leagueID)
	if err != nil {
		return err
	}

	trades, err := loadTrades(st, leagueID)
	if err != nil {
		return err
	}

	for gw := minGW; gw <= maxGW; gw++ {
		snapshots := make(map[int]*ledger.EntrySnapshot)
		for _, entryID := range entryIDs {
			snapPath := filepath.Join(derivedRoot, fmt.Sprintf("snapshots/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
			raw, err := os.ReadFile(snapPath)
			if err != nil {
				log.Printf("snapshot missing: %s", snapPath)
				continue
			}

			var snap ledger.EntrySnapshot
			if err := json.Unmarshal(raw, &snap); err != nil {
				log.Printf("snapshot parse error: %s (%v)", snapPath, err)
				continue
			}
			snapshots[entryID] = &snap
		}

		report := reconcile.BuildReport(leagueID, gw, &ledgerOut, transactions, trades, snapshots, entryIDs)
		outPath := filepath.Join(derivedRoot, fmt.Sprintf("reconcile/%d/gw/%d.json", leagueID, gw))
		if err := reconcile.WriteReport(outPath, report); err != nil {
			return err
		}
	}

	return nil
}

func loadTransactions(st *store.JSONStore, leagueID int) ([]reconcile.Transaction, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/transactions.json", leagueID))
	if err != nil {
		return nil, err
	}

	var resp reconcile.TransactionsResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}

	return resp.Transactions, nil
}

func loadTrades(st *store.JSONStore, leagueID int) ([]reconcile.Trade, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/trades.json", leagueID))
	if err != nil {
		return nil, err
	}

	var resp reconcile.TradesResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}

	return resp.Trades, nil
}

type bootstrapResponse struct {
	Elements []struct {
		ID          int `json:"id"`
		ElementType int `json:"element_type"`
	} `json:"elements"`
}

type liveResponse struct {
	Elements map[string]struct {
		Stats struct {
			Minutes     int `json:"minutes"`
			TotalPoints int `json:"total_points"`
		} `json:"stats"`
	} `json:"elements"`
}

func loadLiveStatsForPoints(st *store.JSONStore, gw int) (map[int]points.LiveStats, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("gw/%d/live.json", gw))
	if err != nil {
		return nil, err
	}

	var resp liveResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}

	out := make(map[int]points.LiveStats, len(resp.Elements))
	for k, v := range resp.Elements {
		id, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		out[id] = points.LiveStats{
			Minutes:     v.Stats.Minutes,
			TotalPoints: v.Stats.TotalPoints,
		}
	}
	return out, nil
}

func buildPointsResults(st *store.JSONStore, derivedRoot string, leagueID int, entryIDs []int, minGW int, maxGW int) error {
	for gw := minGW; gw <= maxGW; gw++ {
		liveByElement, err := loadLiveStatsForPoints(st, gw)
		if err != nil {
			return err
		}

		for _, entryID := range entryIDs {
			snapPath := filepath.Join(derivedRoot, fmt.Sprintf("snapshots/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
			raw, err := os.ReadFile(snapPath)
			if err != nil {
				return err
			}

			var snap ledger.EntrySnapshot
			if err := json.Unmarshal(raw, &snap); err != nil {
				return err
			}

			result := points.BuildResult(leagueID, entryID, gw, &snap, liveByElement)
			outPath := filepath.Join(derivedRoot, fmt.Sprintf("points/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
			if err := points.WriteResult(outPath, result); err != nil {
				return err
			}
		}
	}

	return nil
}

func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}

func must(err error) {
	if err != nil {
		if os.IsNotExist(err) {
			log.Fatal("missing cached data; run with --refresh=all or --refresh=scheduled during a refresh window")
		}
		log.Fatal(err)
	}
}
