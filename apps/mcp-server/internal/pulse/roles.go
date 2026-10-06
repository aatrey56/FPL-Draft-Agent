package pulse

import "time"

// LineupLead is how far ahead of kickoff a fixture counts as a club's current
// one. It matches the fetch window's look-ahead in RefreshSquads (now+2h):
// official sheets publish ~1h before kickoff, so any sheet we could have
// stored belongs to a fixture inside this lead.
const LineupLead = 2 * time.Hour

// CurrentRoles flattens a gw/<n>/squads.json into per-player roles ("xi" or
// "bench") using only each club's CURRENT fixture, so a double gameweek's
// first-match sheet never colours the second match. A club's current fixture
// is its latest one kicking off at or before now+LineupLead. If that fixture
// has no sheet for the club yet, the club has no sheet and contributes no
// roles (nothing is inherited from older fixtures). clubsWithSheet is keyed
// by FPL club short name.
//
// Fixtures without a kickoff (files written before it was stored) count as
// current and win over dated ones, which matches the old flatten-everything
// behaviour for single-fixture gameweeks.
func CurrentRoles(f File, now time.Time) (roles map[int]string, clubsWithSheet map[string]bool) {
	roles = map[int]string{}
	clubsWithSheet = map[string]bool{}
	cutoff := now.Add(LineupLead)

	type pick struct {
		fx    FixtureSquads
		ko    time.Time
		dated bool
	}
	current := map[string]pick{}
	for _, fx := range f.Fixtures {
		ko, err := time.Parse(time.RFC3339, fx.KickoffUTC)
		dated := err == nil
		if dated && ko.After(cutoff) {
			continue
		}
		clubs := map[string]bool{fx.Home: true, fx.Away: true}
		for club := range fx.Sheets {
			clubs[club] = true
		}
		for club := range clubs {
			if club == "" {
				continue
			}
			cur, have := current[club]
			if !have || !dated || (cur.dated && !ko.Before(cur.ko)) {
				current[club] = pick{fx: fx, ko: ko, dated: dated}
			}
		}
	}

	for club, p := range current {
		sheet, ok := p.fx.Sheets[club]
		if !ok || len(sheet.XI) == 0 {
			continue
		}
		clubsWithSheet[club] = true
		for _, id := range sheet.XI {
			roles[id] = "xi"
		}
		for _, id := range sheet.Bench {
			roles[id] = "bench"
		}
	}
	return roles, clubsWithSheet
}
