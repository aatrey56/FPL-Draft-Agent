package pulse

import (
	"reflect"
	"testing"
)

func TestMatchNamesResolvesAccentsAndVariants(t *testing.T) {
	pool := []Element{
		{ID: 1, First: "Lukáš", Second: "Horníček", Web: "Horníček"},
		{ID: 2, First: "Alexis", Second: "Mac Allister", Web: "Mac Allister"},
		{ID: 3, First: "Virgil", Second: "van Dijk", Web: "Virgil"},
	}
	names := []Name{
		{Display: "Lukás Hornícek", First: "Lukás", Last: "Hornícek"}, // pulse drops some accents
		{Display: "Alexis Mac Allister", First: "Alexis", Last: "Mac Allister"},
		{Display: "Virgil van Dijk", First: "Virgil", Last: "van Dijk"},
	}
	ids, unmatched := MatchNames(names, pool)
	if !reflect.DeepEqual(ids, []int{1, 2, 3}) || len(unmatched) != 0 {
		t.Fatalf("ids %v unmatched %v", ids, unmatched)
	}
}

func TestMatchNamesAmbiguityIsSurfacedNotGuessed(t *testing.T) {
	pool := []Element{
		{ID: 1, First: "Gabriel", Second: "Silva", Web: "G.Silva"},
		{ID: 2, First: "Bernardo", Second: "Silva", Web: "B.Silva"},
	}
	ids, unmatched := MatchNames([]Name{{Display: "Silva", Last: "Silva"}}, pool)
	if len(ids) != 0 || !reflect.DeepEqual(unmatched, []string{"Silva"}) {
		t.Fatalf("ambiguous name must go to unmatched: ids %v unmatched %v", ids, unmatched)
	}
	// A full display name still resolves despite the shared surname.
	ids, unmatched = MatchNames([]Name{{Display: "Bernardo Silva", Last: "Silva"}}, pool)
	if !reflect.DeepEqual(ids, []int{2}) || len(unmatched) != 0 {
		t.Fatalf("full name should disambiguate: ids %v unmatched %v", ids, unmatched)
	}
}

func TestSheetCompleteness(t *testing.T) {
	fs := FixtureSquads{Sheets: map[string]Sheet{
		"NEW": {XI: make([]int, 11)}, "LIV": {XI: make([]int, 11)},
	}}
	if !fs.complete() {
		t.Fatal("two full XIs should be complete")
	}
	fs.Sheets["LIV"] = Sheet{XI: make([]int, 10)}
	if fs.complete() {
		t.Fatal("partial XI must trigger refetch")
	}
}
