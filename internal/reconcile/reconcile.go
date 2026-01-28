package reconcile

import (
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"time"

	"fpl-draft-mcp/internal/ledger"
	"fpl-draft-mcp/internal/model"
)

type EntryMismatch struct {
	EntryID         int   `json:"entry_id"`
	Gameweek        int   `json:"gameweek"`
	NotOwned        []int `json:"not_owned"`
	TotalPicks      int   `json:"total_picks"`
	TotalOwned      int   `json:"total_owned"`
	MissingSnapshot bool  `json:"missing_snapshot"`
}

type Report struct {
	LeagueID       int             `json:"league_id"`
	Gameweek       int             `json:"gameweek"`
	GeneratedAtUTC string          `json:"generated_at_utc"`
	Entries        []EntryMismatch `json:"entries"`
}

type TransactionsResponse struct {
	Transactions []Transaction `json:"transactions"`
}

type Transaction struct {
	Added      string `json:"added"`
	ElementIn  int    `json:"element_in"`
	ElementOut int    `json:"element_out"`
	Entry      int    `json:"entry"`
	Event      int    `json:"event"`
	ID         int    `json:"id"`
	Kind       string `json:"kind"`
	Result     string `json:"result"`
}

type TradesResponse struct {
	Trades []Trade `json:"trades"`
}

type Trade struct {
	Event         int         `json:"event"`
	ID            int         `json:"id"`
	OfferedEntry  int         `json:"offered_entry"`
	ReceivedEntry int         `json:"received_entry"`
	ResponseTime  string      `json:"response_time"`
	State         string      `json:"state"`
	TradeItems    []TradeItem `json:"tradeitem_set"`
}

type TradeItem struct {
	ElementIn  int `json:"element_in"`
	ElementOut int `json:"element_out"`
}

func BuildOwnershipMap(ledgerIn *model.DraftLedger) map[int]map[int]bool {
	out := make(map[int]map[int]bool)
	for _, squad := range ledgerIn.Squads {
		if _, ok := out[squad.EntryID]; !ok {
			out[squad.EntryID] = make(map[int]bool)
		}
		for _, playerID := range squad.PlayerIDs {
			out[squad.EntryID][playerID] = true
		}
	}
	return out
}

func BuildReport(leagueID int, gw int, ledgerIn *model.DraftLedger, transactions []Transaction, trades []Trade, snapshots map[int]*ledger.EntrySnapshot, entryIDs []int) *Report {
	owned := BuildOwnershipMapAtGW(ledgerIn, transactions, trades, gw)
	entries := make([]EntryMismatch, 0)

	for _, entryID := range entryIDs {
		snap := snapshots[entryID]
		if snap == nil {
			entries = append(entries, EntryMismatch{
				EntryID:         entryID,
				Gameweek:        gw,
				MissingSnapshot: true,
			})
			continue
		}

		notOwned := make([]int, 0)
		for _, p := range snap.Picks {
			if !owned[entryID][p.Element] {
				notOwned = append(notOwned, p.Element)
			}
		}

		if len(notOwned) > 0 {
			entries = append(entries, EntryMismatch{
				EntryID:    entryID,
				Gameweek:   gw,
				NotOwned:   notOwned,
				TotalPicks: len(snap.Picks),
				TotalOwned: len(owned[entryID]),
			})
		}
	}

	return &Report{
		LeagueID:       leagueID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Entries:        entries,
	}
}

func BuildOwnershipMapAtGW(ledgerIn *model.DraftLedger, transactions []Transaction, trades []Trade, gw int) map[int]map[int]bool {
	owned := BuildOwnershipMap(ledgerIn)

	type ledgerOp struct {
		event int
		time  string
		id    int
		kind  string
		tx    *Transaction
		tr    *Trade
	}

	ops := make([]ledgerOp, 0, len(transactions)+len(trades))
	for i := range transactions {
		tx := transactions[i]
		if tx.Event <= gw && tx.Result == "a" && (tx.Kind == "w" || tx.Kind == "f") {
			ops = append(ops, ledgerOp{
				event: tx.Event,
				time:  tx.Added,
				id:    tx.ID,
				kind:  "tx",
				tx:    &tx,
			})
		}
	}

	for i := range trades {
		tr := trades[i]
		if tr.Event <= gw && tr.State == "p" {
			ops = append(ops, ledgerOp{
				event: tr.Event,
				time:  tr.ResponseTime,
				id:    tr.ID,
				kind:  "trade",
				tr:    &tr,
			})
		}
	}

	sort.Slice(ops, func(i, j int) bool {
		if ops[i].event != ops[j].event {
			return ops[i].event < ops[j].event
		}
		if ops[i].time != ops[j].time {
			return ops[i].time < ops[j].time
		}
		if ops[i].id != ops[j].id {
			return ops[i].id < ops[j].id
		}
		return ops[i].kind < ops[j].kind
	})

	for _, op := range ops {
		if op.tx != nil {
			tx := op.tx
			if _, ok := owned[tx.Entry]; !ok {
				owned[tx.Entry] = make(map[int]bool)
			}
			if tx.ElementOut != 0 {
				delete(owned[tx.Entry], tx.ElementOut)
			}
			if tx.ElementIn != 0 {
				owned[tx.Entry][tx.ElementIn] = true
			}
			continue
		}

		if op.tr != nil {
			tr := op.tr
			if _, ok := owned[tr.OfferedEntry]; !ok {
				owned[tr.OfferedEntry] = make(map[int]bool)
			}
			if _, ok := owned[tr.ReceivedEntry]; !ok {
				owned[tr.ReceivedEntry] = make(map[int]bool)
			}
			for _, item := range tr.TradeItems {
				if item.ElementOut != 0 {
					delete(owned[tr.OfferedEntry], item.ElementOut)
					owned[tr.ReceivedEntry][item.ElementOut] = true
				}
				if item.ElementIn != 0 {
					delete(owned[tr.ReceivedEntry], item.ElementIn)
					owned[tr.OfferedEntry][item.ElementIn] = true
				}
			}
		}
	}

	return owned
}

func WriteReport(path string, report *Report) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(report, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}
