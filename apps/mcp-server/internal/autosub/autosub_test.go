package autosub

import (
	"reflect"
	"testing"
)

// xi builds a legal 3-5-2 XI (slots 1-11) where everyone played, then lets
// tests override individual slots.
func xi() []Player {
	squad := []Player{{Slot: 1, Pos: GKP, Minutes: 90, FixtureDone: true}}
	pos := []int{DEF, DEF, DEF, MID, MID, MID, MID, MID, FWD, FWD}
	for i, p := range pos {
		squad = append(squad, Player{Slot: i + 2, Pos: p, Minutes: 90, FixtureDone: true})
	}
	return squad
}

func TestOutfielderReplacedInBenchOrder(t *testing.T) {
	// The Kudus/Dalot case: a MID finishes the GW on 0 minutes; the first
	// outfield bench player who played comes on (a DEF — formation stays
	// legal at 4-4-2).
	squad := xi()
	squad[4].Minutes = 0 // slot 5, MID, fixture done
	squad = append(squad,
		Player{Slot: 12, Pos: GKP, Minutes: 90, FixtureDone: true},
		Player{Slot: 13, Pos: DEF, Minutes: 12, FixtureDone: true},
		Player{Slot: 14, Pos: MID, Minutes: 90, FixtureDone: true},
	)
	got := Project(squad)
	want := []Swap{{Out: 5, In: 13}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestKeeperOnlyReplacedByKeeper(t *testing.T) {
	squad := xi()
	squad[0].Minutes = 0 // the GKP did not play
	squad = append(squad,
		Player{Slot: 12, Pos: GKP, Minutes: 0, FixtureDone: true}, // backup also blanked
		Player{Slot: 13, Pos: DEF, Minutes: 90, FixtureDone: true},
	)
	if got := Project(squad); got != nil {
		t.Fatalf("outfielder must never replace a keeper, got %v", got)
	}
	squad[11].Minutes = 45 // backup keeper played
	if got, want := Project(squad), []Swap{{Out: 1, In: 12}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestFormationMinimumSkipsIllegalSwap(t *testing.T) {
	// A DEF drops out of a 3-DEF XI: replacing him with a MID would leave
	// 2 DEF, so the engine must skip ahead to the bench DEF.
	squad := xi()
	squad[1].Minutes = 0 // slot 2, DEF
	squad = append(squad,
		Player{Slot: 13, Pos: MID, Minutes: 90, FixtureDone: true},
		Player{Slot: 14, Pos: DEF, Minutes: 90, FixtureDone: true},
	)
	if got, want := Project(squad), []Swap{{Out: 2, In: 14}}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

func TestNoSwapUntilFixtureFinishedOrBenchPlayed(t *testing.T) {
	squad := xi()
	squad[4].Minutes, squad[4].FixtureDone = 0, false // match still live
	squad = append(squad, Player{Slot: 13, Pos: MID, Minutes: 90, FixtureDone: true})
	if got := Project(squad); got != nil {
		t.Fatalf("no projection while the starter's match is live, got %v", got)
	}
	squad[4].FixtureDone = true
	squad[11].Minutes = 0 // bench player has not played (yet)
	if got := Project(squad); got != nil {
		t.Fatalf("bench player without minutes must not come on, got %v", got)
	}
}

func TestMultipleSwapsShareTheBench(t *testing.T) {
	squad := xi()
	squad[4].Minutes = 0  // MID out
	squad[10].Minutes = 0 // FWD out (slot 11)
	squad = append(squad,
		Player{Slot: 13, Pos: MID, Minutes: 90, FixtureDone: true},
		Player{Slot: 14, Pos: FWD, Minutes: 30, FixtureDone: true},
	)
	got := Project(squad)
	want := []Swap{{Out: 5, In: 13}, {Out: 11, In: 14}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}
