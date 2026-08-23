// Package autosub projects FPL Draft end-of-gameweek automatic substitutions
// from a live squad state. The rules (draft edition):
//
//   - A starter who finishes the gameweek with 0 minutes is replaced by the
//     first eligible bench player, in bench-slot order.
//   - Goalkeepers only swap with goalkeepers; outfielders with outfielders.
//   - The resulting XI must stay a legal draft formation: 1 GKP, at least
//     3 DEF, at least 2 MID, at least 1 FWD (5-2-3 is legal). These minimums
//     come from the draft API itself — bootstrap settings.squad reports
//     min_play_DEF 3, min_play_MID 2, min_play_FWD 1, min/max_play_GKP 1.
//   - Only bench players who actually played (minutes > 0) come on.
//
// Mid-gameweek this yields a *projection*: a starter counts as a confirmed
// non-player only once their fixture is finished (or they have no fixture),
// and a bench player is only eligible once they have minutes on the board.
// The projection therefore only firms up as the gameweek completes — it never
// speculates about matches still to be played.
package autosub

import "sort"

// Position codes follow the FPL element_type convention.
const (
	GKP = 1
	DEF = 2
	MID = 3
	FWD = 4
)

// Formation minimums for a legal draft XI (the GKP count is pinned to exactly
// one by the keeper-for-keeper swap rule).
const (
	minDEF = 3
	minMID = 2
	minFWD = 1
)

// Player is one squad slot as the rules engine sees it.
type Player struct {
	Slot        int // 1-11 starters, 12-15 bench in priority order
	Pos         int // GKP/DEF/MID/FWD element_type code
	Minutes     int
	FixtureDone bool // the player's club has no unfinished fixture this GW
}

// Swap is one projected substitution, by squad slot.
type Swap struct {
	Out, In int
}

// Project returns the auto-substitutions the draft game would apply if the
// gameweek ended in the given state, in the order they fire.
func Project(squad []Player) []Swap {
	bySlot := map[int]Player{}
	xiCount := map[int]int{}
	var starters, bench []Player
	for _, p := range squad {
		bySlot[p.Slot] = p
		if p.Slot <= 11 {
			starters = append(starters, p)
			xiCount[p.Pos]++
		} else {
			bench = append(bench, p)
		}
	}
	// Slot order is the priority order on both sides of the swap.
	sort.Slice(starters, func(i, j int) bool { return starters[i].Slot < starters[j].Slot })
	sort.Slice(bench, func(i, j int) bool { return bench[i].Slot < bench[j].Slot })

	legal := func(count map[int]int) bool {
		return count[DEF] >= minDEF && count[MID] >= minMID && count[FWD] >= minFWD
	}

	used := map[int]bool{}
	var swaps []Swap
	for _, out := range starters {
		if !out.FixtureDone || out.Minutes > 0 {
			continue
		}
		for _, in := range bench {
			if used[in.Slot] || in.Minutes == 0 {
				continue
			}
			if (out.Pos == GKP) != (in.Pos == GKP) {
				continue
			}
			xiCount[out.Pos]--
			xiCount[in.Pos]++
			if !legal(xiCount) {
				xiCount[out.Pos]++
				xiCount[in.Pos]--
				continue
			}
			used[in.Slot] = true
			swaps = append(swaps, Swap{Out: out.Slot, In: in.Slot})
			break
		}
	}
	return swaps
}
