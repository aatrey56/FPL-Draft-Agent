package pulse

import (
	"reflect"
	"testing"
	"time"
)

var rolesNow = time.Date(2026, 10, 3, 15, 0, 0, 0, time.UTC)

func ko(d time.Duration) string { return rolesNow.Add(d).Format(time.RFC3339) }

// match builds a fixture with ARS XI/bench ids and a CHE sheet (XI 200, bench
// 201), or only the club's sheet when withSheets is false.
func match(id int, kickoff string, arsXI, arsBench []int, withSheets bool) FixtureSquads {
	fx := FixtureSquads{PulseID: id, Home: "ARS", Away: "CHE", KickoffUTC: kickoff, Sheets: map[string]Sheet{}}
	if withSheets {
		fx.Sheets["ARS"] = Sheet{XI: arsXI, Bench: arsBench}
		fx.Sheets["CHE"] = Sheet{XI: []int{200}, Bench: []int{201}}
	}
	return fx
}

// (a) DGW: match 1 finished with sheets, match 2 inside the lead window but
// its sheet is not published: no roles for the club.
func TestCurrentRolesDGWSecondSheetNotPublished(t *testing.T) {
	f := File{Fixtures: []FixtureSquads{
		match(1, ko(-48*time.Hour), []int{1, 2}, []int{3}, true),
		// Match 2 kicks off in 90 minutes; only a partial record exists.
		{PulseID: 2, Home: "ARS", Away: "LIV", KickoffUTC: ko(90 * time.Minute), Sheets: map[string]Sheet{}},
	}}
	roles, clubs := CurrentRoles(f, rolesNow)
	if clubs["ARS"] || len(roles) != 2 || roles[1] != "" || roles[2] != "" || roles[3] != "" {
		t.Fatalf("ARS must have no sheet/roles yet, got roles %v clubs %v", roles, clubs)
	}
	// CHE has no second fixture in this file, so its sheet stays current.
	if !clubs["CHE"] || roles[200] != "xi" {
		t.Fatalf("CHE roles lost: %v %v", roles, clubs)
	}
}

// (b) The same DGW once match 2's sheet is out: only match-2 roles remain,
// and a match-1 starter absent from match 2 has no role.
func TestCurrentRolesDGWSecondSheetReplacesFirst(t *testing.T) {
	second := FixtureSquads{PulseID: 2, Home: "ARS", Away: "LIV", KickoffUTC: ko(60 * time.Minute),
		Sheets: map[string]Sheet{"ARS": {XI: []int{2, 4}, Bench: []int{5}}}}
	f := File{Fixtures: []FixtureSquads{
		match(1, ko(-48*time.Hour), []int{1, 2}, []int{3}, true),
		second,
	}}
	roles, clubs := CurrentRoles(f, rolesNow)
	want := map[int]string{2: "xi", 4: "xi", 5: "bench", 200: "xi", 201: "bench"}
	if !reflect.DeepEqual(roles, want) || !clubs["ARS"] {
		t.Fatalf("got roles %v clubs %v, want %v", roles, clubs, want)
	}
	if _, ok := roles[1]; ok {
		t.Fatal("match-1 starter absent from match 2 must have no role")
	}
}

// (c) Single-fixture GW: same roles as the old flatten.
func TestCurrentRolesSingleFixture(t *testing.T) {
	f := File{Fixtures: []FixtureSquads{match(1, ko(time.Hour), []int{1, 2}, []int{3}, true)}}
	roles, clubs := CurrentRoles(f, rolesNow)
	want := map[int]string{1: "xi", 2: "xi", 3: "bench", 200: "xi", 201: "bench"}
	if !reflect.DeepEqual(roles, want) || !clubs["ARS"] || !clubs["CHE"] {
		t.Fatalf("got roles %v clubs %v", roles, clubs)
	}
}

// (d) Old file without kickoff times: treated as current, like before.
func TestCurrentRolesOldFileWithoutKickoff(t *testing.T) {
	f := File{Fixtures: []FixtureSquads{match(1, "", []int{1, 2}, []int{3}, true)}}
	roles, clubs := CurrentRoles(f, rolesNow)
	want := map[int]string{1: "xi", 2: "xi", 3: "bench", 200: "xi", 201: "bench"}
	if !reflect.DeepEqual(roles, want) || !clubs["ARS"] {
		t.Fatalf("got roles %v clubs %v", roles, clubs)
	}
}

// A fixture beyond the lead window is not yet current: the earlier one stays.
func TestCurrentRolesFutureFixtureBeyondLeadIgnored(t *testing.T) {
	f := File{Fixtures: []FixtureSquads{
		match(1, ko(-time.Hour), []int{1}, nil, true),
		match(2, ko(48*time.Hour), []int{9}, nil, true),
	}}
	roles, _ := CurrentRoles(f, rolesNow)
	if roles[1] != "xi" || roles[9] != "" {
		t.Fatalf("got %v", roles)
	}
}
