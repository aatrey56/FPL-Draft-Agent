package summary

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/model"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/reconcile"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// writeLiveJSON writes a minimal gw/<gw>/live.json with the given element stats.
func writeLiveJSON(t *testing.T, rawRoot string, gw int, elements map[string]any) {
	t.Helper()
	dir := filepath.Join(rawRoot, "gw", itoa(gw))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	payload := map[string]any{"elements": elements}
	b, err := json.Marshal(payload)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if err := os.WriteFile(filepath.Join(dir, "live.json"), b, 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}
}

// itoa converts int to its decimal string representation.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	b := make([]byte, 0, 10)
	for n > 0 {
		b = append([]byte{byte('0' + n%10)}, b...)
		n /= 10
	}
	if neg {
		b = append([]byte{'-'}, b...)
	}
	return string(b)
}

// ---------------------------------------------------------------------------
// buildPlayerForm — NaN/+Inf guard when entryIDs is empty
// ---------------------------------------------------------------------------

// TestBuildPlayerForm_EmptyEntryIDs ensures that an empty entryIDs slice does
// not produce NaN or +Inf ownership percentages that would cause
// json.MarshalIndent to return an error.
func TestBuildPlayerForm_EmptyEntryIDs(t *testing.T) {
	rawRoot := t.TempDir()
	writeLiveJSON(t, rawRoot, 1, map[string]any{
		"100": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 10}},
		"200": map[string]any{"stats": map[string]any{"minutes": 45, "total_points": 5}},
	})

	st := store.NewJSONStore(rawRoot)
	meta := map[int]PlayerMeta{
		100: {ID: 100, Name: "Player A", PositionType: 3, TeamShort: "ARS"},
		200: {ID: 200, Name: "Player B", PositionType: 4, TeamShort: "CHE"},
	}

	// Empty entryIDs is the critical edge case: without the guard,
	// float64(n) / float64(0) produces +Inf which json.Marshal rejects.
	summary, err := buildPlayerForm(
		meta,
		model.DraftLedger{},
		[]reconcile.Transaction{},
		[]reconcile.Trade{},
		[]int{}, // empty — triggers division by zero without the guard
		1,       // gw
		1,       // horizon
		st,
	)
	if err != nil {
		t.Fatalf("buildPlayerForm returned error: %v", err)
	}

	for _, p := range summary.Players {
		if math.IsNaN(p.OwnershipPct) {
			t.Errorf("player %d: OwnershipPct is NaN, expected 0.0", p.Element)
		}
		if math.IsInf(p.OwnershipPct, 0) {
			t.Errorf("player %d: OwnershipPct is +/-Inf, expected 0.0", p.Element)
		}
		if p.OwnershipPct != 0.0 {
			t.Errorf("player %d: OwnershipPct = %f, want 0.0 for empty league", p.Element, p.OwnershipPct)
		}
	}

	// Also verify the result is JSON-serialisable (MarshalIndent fails on NaN/Inf).
	if _, err := json.MarshalIndent(summary, "", "  "); err != nil {
		t.Errorf("json.MarshalIndent failed: %v (NaN/Inf likely present)", err)
	}
}

// TestBuildPlayerForm_NormalLeague verifies ownership percentages are calculated
// correctly when entryIDs is non-empty (regression guard).
func TestBuildPlayerForm_NormalLeague(t *testing.T) {
	rawRoot := t.TempDir()
	writeLiveJSON(t, rawRoot, 5, map[string]any{
		"10": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 12}},
		"20": map[string]any{"stats": map[string]any{"minutes": 90, "total_points": 8}},
	})

	st := store.NewJSONStore(rawRoot)
	meta := map[int]PlayerMeta{
		10: {ID: 10, Name: "Salah", PositionType: 3, TeamShort: "LIV"},
		20: {ID: 20, Name: "Haaland", PositionType: 4, TeamShort: "MCI"},
	}

	summary, err := buildPlayerForm(
		meta,
		model.DraftLedger{},
		[]reconcile.Transaction{},
		[]reconcile.Trade{},
		[]int{101, 102, 103, 104}, // 4 entries, nobody owns anyone
		5,
		1,
		st,
	)
	if err != nil {
		t.Fatalf("buildPlayerForm returned error: %v", err)
	}

	for _, p := range summary.Players {
		if math.IsNaN(p.OwnershipPct) || math.IsInf(p.OwnershipPct, 0) {
			t.Errorf("player %d: OwnershipPct is %f, expected finite value", p.Element, p.OwnershipPct)
		}
		// No one owns either player, so ownership must be 0.
		if p.OwnershipPct != 0.0 {
			t.Errorf("player %d: OwnershipPct = %f, want 0.0 (unowned)", p.Element, p.OwnershipPct)
		}
	}

	if _, err := json.MarshalIndent(summary, "", "  "); err != nil {
		t.Errorf("json.MarshalIndent failed: %v", err)
	}
}
