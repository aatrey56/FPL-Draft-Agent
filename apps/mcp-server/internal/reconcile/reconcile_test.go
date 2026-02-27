package reconcile

import (
	"testing"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/model"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func makeLedger(squads ...struct {
	entryID   int
	playerIDs []int
}) *model.DraftLedger {
	s := make([]model.Squad, 0, len(squads))
	for _, sq := range squads {
		s = append(s, model.Squad{EntryID: sq.entryID, PlayerIDs: sq.playerIDs})
	}
	return &model.DraftLedger{Squads: s}
}

func makeWaiverTx(id, entry, elementIn, elementOut, event int) Transaction {
	return Transaction{
		ID:         id,
		Entry:      entry,
		ElementIn:  elementIn,
		ElementOut: elementOut,
		Event:      event,
		Kind:       "w",
		Result:     "a", // accepted
	}
}

// ---------------------------------------------------------------------------
// BuildOwnershipMap
// ---------------------------------------------------------------------------

func TestBuildOwnershipMap_Empty(t *testing.T) {
	l := makeLedger()
	m := BuildOwnershipMap(l)
	if len(m) != 0 {
		t.Errorf("map len = %d, want 0", len(m))
	}
}

func TestBuildOwnershipMap_SingleEntry(t *testing.T) {
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20, 30}})

	m := BuildOwnershipMap(l)

	if !m[1][10] || !m[1][20] || !m[1][30] {
		t.Error("entry 1 should own players 10, 20, 30")
	}
	if m[1][99] {
		t.Error("entry 1 should not own player 99")
	}
}

func TestBuildOwnershipMap_MultipleEntries(t *testing.T) {
	l := makeLedger(
		struct {
			entryID   int
			playerIDs []int
		}{1, []int{10, 20}},
		struct {
			entryID   int
			playerIDs []int
		}{2, []int{30, 40}},
	)

	m := BuildOwnershipMap(l)

	if m[1][30] {
		t.Error("entry 1 should not own player 30 (belongs to entry 2)")
	}
	if !m[2][30] || !m[2][40] {
		t.Error("entry 2 should own players 30 and 40")
	}
}

// ---------------------------------------------------------------------------
// BuildOwnershipMapAtGW
// ---------------------------------------------------------------------------

func TestBuildOwnershipMapAtGW_NoTransactions(t *testing.T) {
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})

	m := BuildOwnershipMapAtGW(l, nil, nil, 5)

	if !m[1][10] || !m[1][20] {
		t.Error("without transactions, ownership should match draft-day ledger")
	}
}

func TestBuildOwnershipMapAtGW_WaiverApplied(t *testing.T) {
	// Entry 1 drops player 10, adds player 99 in GW 3.
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})
	txs := []Transaction{makeWaiverTx(1, 1, 99, 10, 3)}

	m := BuildOwnershipMapAtGW(l, txs, nil, 5)

	if m[1][10] {
		t.Error("player 10 should have been dropped by entry 1")
	}
	if !m[1][99] {
		t.Error("player 99 should have been added by entry 1")
	}
	if !m[1][20] {
		t.Error("player 20 should still be owned by entry 1")
	}
}

func TestBuildOwnershipMapAtGW_FutureTransactionIgnored(t *testing.T) {
	// Transaction in GW 10 — querying at GW 5 should not apply it.
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})
	txs := []Transaction{makeWaiverTx(1, 1, 99, 10, 10)} // future GW

	m := BuildOwnershipMapAtGW(l, txs, nil, 5)

	if !m[1][10] {
		t.Error("player 10 should still be owned — GW 10 transaction should not apply at GW 5")
	}
	if m[1][99] {
		t.Error("player 99 should not be added — GW 10 transaction should not apply at GW 5")
	}
}

func TestBuildOwnershipMapAtGW_NonAcceptedTransactionIgnored(t *testing.T) {
	// Only accepted (Result="a") transactions should be applied.
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})
	txs := []Transaction{{
		ID: 1, Entry: 1, ElementIn: 99, ElementOut: 10, Event: 3,
		Kind: "w", Result: "n", // NOT accepted
	}}

	m := BuildOwnershipMapAtGW(l, txs, nil, 5)

	if !m[1][10] {
		t.Error("player 10 should still be owned — rejected transaction must not apply")
	}
	if m[1][99] {
		t.Error("player 99 should not be added — rejected transaction must not apply")
	}
}

func TestBuildOwnershipMapAtGW_TradeApplied(t *testing.T) {
	// Entry 1 and entry 2 trade: entry 1 gives player 20, receives player 30.
	l := makeLedger(
		struct {
			entryID   int
			playerIDs []int
		}{1, []int{10, 20}},
		struct {
			entryID   int
			playerIDs []int
		}{2, []int{30, 40}},
	)
	trades := []Trade{
		{
			ID:            1,
			OfferedEntry:  1,
			ReceivedEntry: 2,
			Event:         2,
			State:         "p", // processed/accepted
			ResponseTime:  "2024-09-01T10:00:00Z",
			TradeItems: []TradeItem{
				{ElementOut: 20, ElementIn: 30},
			},
		},
	}

	m := BuildOwnershipMapAtGW(l, nil, trades, 5)

	// Entry 1 should own 10 (unchanged) and 30 (received), not 20 (given away).
	if !m[1][10] {
		t.Error("entry 1 should still own player 10")
	}
	if !m[1][30] {
		t.Error("entry 1 should now own player 30 (received in trade)")
	}
	if m[1][20] {
		t.Error("entry 1 should no longer own player 20 (given away in trade)")
	}
	// Entry 2 should own 40 (unchanged) and 20 (received), not 30 (given away).
	if !m[2][20] {
		t.Error("entry 2 should now own player 20 (received in trade)")
	}
	if m[2][30] {
		t.Error("entry 2 should no longer own player 30 (given away in trade)")
	}
}

func TestBuildOwnershipMapAtGW_FutureTradeIgnored(t *testing.T) {
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}}, struct {
		entryID   int
		playerIDs []int
	}{2, []int{30}})

	trades := []Trade{{
		ID: 1, OfferedEntry: 1, ReceivedEntry: 2,
		Event: 10, State: "p", ResponseTime: "2024-10-01T00:00:00Z",
		TradeItems: []TradeItem{{ElementOut: 20, ElementIn: 30}},
	}}

	m := BuildOwnershipMapAtGW(l, nil, trades, 5)

	if !m[1][20] {
		t.Error("player 20 should still be owned by entry 1 (trade is in a future GW)")
	}
}

func TestBuildOwnershipMapAtGW_TransactionsAppliedInOrder(t *testing.T) {
	// Two sequential waivers for the same entry — order must be respected.
	// GW 2: entry 1 drops 10, adds 50.
	// GW 3: entry 1 drops 50, adds 70.
	// At GW 5: entry 1 should own 20, 70 (not 10 or 50).
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})
	txs := []Transaction{
		makeWaiverTx(1, 1, 50, 10, 2),
		makeWaiverTx(2, 1, 70, 50, 3),
	}

	m := BuildOwnershipMapAtGW(l, txs, nil, 5)

	if m[1][10] {
		t.Error("player 10 should have been dropped in GW 2")
	}
	if m[1][50] {
		t.Error("player 50 should have been dropped in GW 3")
	}
	if !m[1][70] {
		t.Error("player 70 should have been added in GW 3")
	}
	if !m[1][20] {
		t.Error("player 20 should still be owned")
	}
}

// ---------------------------------------------------------------------------
// BuildReport
// ---------------------------------------------------------------------------

func TestBuildReport_MissingSnapshot(t *testing.T) {
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10}})

	report := BuildReport(1, 3, l, nil, nil, map[int]*ledger.EntrySnapshot{}, []int{1})

	if len(report.Entries) != 1 {
		t.Fatalf("Entries len = %d, want 1", len(report.Entries))
	}
	if !report.Entries[0].MissingSnapshot {
		t.Error("entry with no snapshot should have MissingSnapshot=true")
	}
}

func TestBuildReport_CleanRoster_NoMismatch(t *testing.T) {
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10, 20}})

	snap := &ledger.EntrySnapshot{
		EntryID:  1,
		Gameweek: 3,
		Picks: []ledger.EntryPick{
			{Element: 10, Position: 1},
			{Element: 20, Position: 2},
		},
	}

	report := BuildReport(1, 3, l, nil, nil, map[int]*ledger.EntrySnapshot{1: snap}, []int{1})

	if len(report.Entries) != 0 {
		t.Errorf("Entries len = %d, want 0 (no mismatches)", len(report.Entries))
	}
}

func TestBuildReport_UnownedPlayerDetected(t *testing.T) {
	// Entry 1 owns [10] at draft. Snapshot shows player 99 in lineup (never owned).
	l := makeLedger(struct {
		entryID   int
		playerIDs []int
	}{1, []int{10}})

	snap := &ledger.EntrySnapshot{
		EntryID:  1,
		Gameweek: 3,
		Picks: []ledger.EntryPick{
			{Element: 10, Position: 1},
			{Element: 99, Position: 2}, // not owned
		},
	}

	report := BuildReport(1, 3, l, nil, nil, map[int]*ledger.EntrySnapshot{1: snap}, []int{1})

	if len(report.Entries) != 1 {
		t.Fatalf("Entries len = %d, want 1 (mismatch)", len(report.Entries))
	}
	if len(report.Entries[0].NotOwned) != 1 || report.Entries[0].NotOwned[0] != 99 {
		t.Errorf("NotOwned = %v, want [99]", report.Entries[0].NotOwned)
	}
}
