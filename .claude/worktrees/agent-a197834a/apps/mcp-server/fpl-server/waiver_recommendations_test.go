package main

import "testing"

func TestResolveRosterGW(t *testing.T) {
	tests := []struct {
		name   string
		asOf   int
		target int
		want   int
	}{
		{
			name:   "UseTargetMinusOneWhenAhead",
			asOf:   25,
			target: 27,
			want:   26,
		},
		{
			name:   "KeepAsOfWhenAlreadyCurrent",
			asOf:   26,
			target: 27,
			want:   26,
		},
		{
			name:   "KeepAsOfForEarlyTarget",
			asOf:   1,
			target: 1,
			want:   1,
		},
		{
			name:   "ClampToOne",
			asOf:   0,
			target: 1,
			want:   1,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveRosterGW(tc.asOf, tc.target)
			if got != tc.want {
				t.Fatalf("resolveRosterGW(%d, %d)=%d want %d", tc.asOf, tc.target, got, tc.want)
			}
		})
	}
}
