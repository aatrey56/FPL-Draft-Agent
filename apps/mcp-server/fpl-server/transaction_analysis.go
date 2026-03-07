package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// TransactionAnalysisArgs are the input arguments for the transaction_analysis tool.
type TransactionAnalysisArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	GW       int `json:"gw" jsonschema:"Gameweek to analyse (0 = current)"`
}

// TxPlayerSummary describes a single player mentioned in transactions.
type TxPlayerSummary struct {
	Element      int    `json:"element"`
	PlayerName   string `json:"player_name"`
	Team         string `json:"team"`
	PositionType int    `json:"position_type"`
	Count        int    `json:"count"`
}

// TxPositionBreakdown holds add/drop counts per position.
type TxPositionBreakdown struct {
	Added   int `json:"added"`
	Dropped int `json:"dropped"`
}

// TxManagerActivity summarises one manager's transactions in this GW.
type TxManagerActivity struct {
	EntryID   int              `json:"entry_id"`
	EntryName string           `json:"entry_name"`
	Added     []TxPlayerDetail `json:"added"`
	Dropped   []TxPlayerDetail `json:"dropped"`
}

// TxPlayerDetail is a single player in a manager's transaction list.
type TxPlayerDetail struct {
	Element      int    `json:"element"`
	PlayerName   string `json:"player_name"`
	Team         string `json:"team"`
	PositionType int    `json:"position_type"`
	Kind         string `json:"kind"` // "w"=waiver, "f"=free agent
}

// TransactionAnalysisOutput is the output of the transaction_analysis tool.
type TransactionAnalysisOutput struct {
	LeagueID          int                            `json:"league_id"`
	Gameweek          int                            `json:"gameweek"`
	TotalTransactions int                            `json:"total_transactions"`
	PositionBreakdown map[string]TxPositionBreakdown `json:"position_breakdown"`
	TopAdded          []TxPlayerSummary              `json:"top_added"`
	TopDropped        []TxPlayerSummary              `json:"top_dropped"`
	ManagerActivity   []TxManagerActivity            `json:"manager_activity"`
}

func buildTransactionAnalysis(cfg ServerConfig, args TransactionAnalysisArgs) (TransactionAnalysisOutput, error) {
	if args.LeagueID == 0 {
		return TransactionAnalysisOutput{}, fmt.Errorf("league_id is required")
	}

	gw, err := resolveGW(cfg, args.GW)
	if err != nil {
		return TransactionAnalysisOutput{}, err
	}

	// Load raw transactions.
	txPath := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/transactions.json", args.LeagueID))
	txRaw, err := os.ReadFile(txPath)
	if err != nil {
		return TransactionAnalysisOutput{}, fmt.Errorf("transactions not found for league %d: %w", args.LeagueID, err)
	}
	var txResp struct {
		Transactions []struct {
			Entry      int    `json:"entry"`
			ElementIn  int    `json:"element_in"`
			ElementOut int    `json:"element_out"`
			Event      int    `json:"event"`
			Kind       string `json:"kind"`
			Result     string `json:"result"`
		} `json:"transactions"`
	}
	if err := json.Unmarshal(txRaw, &txResp); err != nil {
		return TransactionAnalysisOutput{}, err
	}

	// Load league details for entry names.
	detailsPath := filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", args.LeagueID))
	detailsRaw, err := os.ReadFile(detailsPath)
	if err != nil {
		return TransactionAnalysisOutput{}, err
	}
	var details leagueDetailsRaw
	if err := json.Unmarshal(detailsRaw, &details); err != nil {
		return TransactionAnalysisOutput{}, err
	}
	nameByEntry := make(map[int]string, len(details.LeagueEntries))
	for _, e := range details.LeagueEntries {
		nameByEntry[e.EntryID] = e.EntryName
	}

	// Load player metadata.
	elements, teamShort, _, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return TransactionAnalysisOutput{}, err
	}
	playerByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		playerByID[e.ID] = e
	}

	posLabel := map[int]string{1: "GK", 2: "DEF", 3: "MID", 4: "FWD"}

	// Aggregate.
	addedCount := make(map[int]int)
	droppedCount := make(map[int]int)
	posBreakdown := make(map[string]*TxPositionBreakdown)
	for _, p := range []string{"GK", "DEF", "MID", "FWD"} {
		posBreakdown[p] = &TxPositionBreakdown{}
	}
	managerTx := make(map[int]*TxManagerActivity)
	total := 0

	for _, tx := range txResp.Transactions {
		if tx.Event != gw {
			continue
		}
		if tx.Result != "a" {
			continue
		}
		if tx.Kind != "w" && tx.Kind != "f" {
			continue
		}
		total++

		// Ensure manager entry.
		if _, ok := managerTx[tx.Entry]; !ok {
			managerTx[tx.Entry] = &TxManagerActivity{
				EntryID:   tx.Entry,
				EntryName: nameByEntry[tx.Entry],
				Added:     []TxPlayerDetail{},
				Dropped:   []TxPlayerDetail{},
			}
		}

		// Added player.
		if tx.ElementIn != 0 {
			meta := playerByID[tx.ElementIn]
			pos := posLabel[meta.PositionType]
			addedCount[tx.ElementIn]++
			if pb, ok := posBreakdown[pos]; ok {
				pb.Added++
			}
			managerTx[tx.Entry].Added = append(managerTx[tx.Entry].Added, TxPlayerDetail{
				Element:      tx.ElementIn,
				PlayerName:   meta.Name,
				Team:         teamShort[meta.TeamID],
				PositionType: meta.PositionType,
				Kind:         tx.Kind,
			})
		}

		// Dropped player.
		if tx.ElementOut != 0 {
			meta := playerByID[tx.ElementOut]
			pos := posLabel[meta.PositionType]
			droppedCount[tx.ElementOut]++
			if pb, ok := posBreakdown[pos]; ok {
				pb.Dropped++
			}
			managerTx[tx.Entry].Dropped = append(managerTx[tx.Entry].Dropped, TxPlayerDetail{
				Element:      tx.ElementOut,
				PlayerName:   meta.Name,
				Team:         teamShort[meta.TeamID],
				PositionType: meta.PositionType,
				Kind:         tx.Kind,
			})
		}
	}

	// Build top added/dropped lists.
	topAdded := buildTxRanking(addedCount, playerByID, teamShort, 10)
	topDropped := buildTxRanking(droppedCount, playerByID, teamShort, 10)

	// Flatten position breakdown.
	flatPos := make(map[string]TxPositionBreakdown, 4)
	for k, v := range posBreakdown {
		flatPos[k] = *v
	}

	// Collect manager activities, sorted by entry id.
	activities := make([]TxManagerActivity, 0, len(managerTx))
	for _, v := range managerTx {
		activities = append(activities, *v)
	}
	sort.Slice(activities, func(i, j int) bool {
		return activities[i].EntryID < activities[j].EntryID
	})

	return TransactionAnalysisOutput{
		LeagueID:          args.LeagueID,
		Gameweek:          gw,
		TotalTransactions: total,
		PositionBreakdown: flatPos,
		TopAdded:          topAdded,
		TopDropped:        topDropped,
		ManagerActivity:   activities,
	}, nil
}

// buildTxRanking returns up to limit players sorted by count desc.
func buildTxRanking(counts map[int]int, playerByID map[int]elementInfo, teamShort map[int]string, limit int) []TxPlayerSummary {
	type entry struct {
		id    int
		count int
	}
	items := make([]entry, 0, len(counts))
	for id, n := range counts {
		items = append(items, entry{id, n})
	}
	sort.Slice(items, func(i, j int) bool {
		if items[i].count != items[j].count {
			return items[i].count > items[j].count
		}
		return items[i].id < items[j].id
	})
	if limit > len(items) {
		limit = len(items)
	}
	out := make([]TxPlayerSummary, 0, limit)
	for _, it := range items[:limit] {
		meta := playerByID[it.id]
		out = append(out, TxPlayerSummary{
			Element:      it.id,
			PlayerName:   meta.Name,
			Team:         teamShort[meta.TeamID],
			PositionType: meta.PositionType,
			Count:        it.count,
		})
	}
	return out
}
