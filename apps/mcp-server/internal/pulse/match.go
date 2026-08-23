// Package pulse reads official Premier League team sheets (the pulselive API
// behind premierleague.com) and resolves them to FPL element ids. Team sheets
// publish ~an hour before kickoff with the starting XI and the bench — the
// only public source that separates "on the bench" from "not in the squad".
package pulse

import (
	"strings"
	"unicode"

	"golang.org/x/text/runes"
	"golang.org/x/text/transform"
	"golang.org/x/text/unicode/norm"
)

// Element is the slice of an FPL bootstrap element the matcher needs.
type Element struct {
	ID     int
	First  string
	Second string
	Web    string
}

// Name is a player entry from a pulse team list.
type Name struct {
	Display string
	First   string
	Last    string
}

var deaccent = transform.Chain(norm.NFD, runes.Remove(runes.In(unicode.Mn)), norm.NFC)

// normName lowercases, strips diacritics, and collapses punctuation so
// "Lukás Hornícek" matches "Lukáš Horníček" and "Mac Allister" variants agree.
func normName(s string) string {
	out, _, err := transform.String(deaccent, s)
	if err != nil {
		out = s
	}
	out = strings.ToLower(out)
	out = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == ' ' {
			return r
		}
		return ' '
	}, out)
	return strings.Join(strings.Fields(out), " ")
}

// ResolveNames maps each name to an FPL element id in input order, 0 where no
// confident match exists — for zipping against a parallel slice (e.g. pulse
// person ids).
func ResolveNames(names []Name, candidates []Element) []int {
	ids, _ := matchInto(names, candidates)
	return ids
}

// MatchNames resolves pulse names to FPL element ids within one club's
// candidate pool. Unresolvable names are returned verbatim so the caller can
// surface them instead of silently dropping players.
func MatchNames(names []Name, candidates []Element) (ids []int, unmatched []string) {
	all, miss := matchInto(names, candidates)
	for _, id := range all {
		if id != 0 {
			ids = append(ids, id)
		}
	}
	return ids, miss
}

// matchInto returns an id per input name (0 = unmatched) plus the display
// names that did not resolve.
func matchInto(names []Name, candidates []Element) (ids []int, unmatched []string) {
	byFull := map[string][]int{}
	byLast := map[string][]int{}
	byWeb := map[string][]int{}
	for _, e := range candidates {
		byFull[normName(e.First+" "+e.Second)] = append(byFull[normName(e.First+" "+e.Second)], e.ID)
		byLast[normName(e.Second)] = append(byLast[normName(e.Second)], e.ID)
		byWeb[normName(e.Web)] = append(byWeb[normName(e.Web)], e.ID)
	}
	unique := func(m map[string][]int, key string) (int, bool) {
		if v := m[key]; len(v) == 1 {
			return v[0], true
		}
		return 0, false
	}
	resolve := func(n Name) int {
		if id, ok := unique(byFull, normName(n.Display)); ok {
			return id
		}
		if id, ok := unique(byLast, normName(n.Last)); ok {
			return id
		}
		if id, ok := unique(byWeb, normName(n.Last)); ok {
			return id
		}
		// Last resort: a unique candidate whose web name appears inside the
		// display name ("Mac Allister" in "Alexis Mac Allister").
		var hits []int
		nd := normName(n.Display)
		for _, e := range candidates {
			if w := normName(e.Web); w != "" && strings.Contains(nd, w) {
				hits = append(hits, e.ID)
			}
		}
		if len(hits) == 1 {
			return hits[0]
		}
		return 0
	}
	ids = make([]int, len(names))
	for i, n := range names {
		ids[i] = resolve(n)
		if ids[i] == 0 {
			unmatched = append(unmatched, n.Display)
		}
	}
	return ids, unmatched
}
