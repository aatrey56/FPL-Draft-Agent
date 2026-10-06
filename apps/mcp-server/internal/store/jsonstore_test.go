package store

import (
	"os"
	"path/filepath"
	"testing"
)

func TestWriteRawIsAtomicAndLeavesNoTempFile(t *testing.T) {
	st := NewJSONStore(t.TempDir())
	if err := st.WriteRaw("gw/1/squads.json", []byte(`{"a":1}`), false); err != nil {
		t.Fatal(err)
	}
	if err := st.WriteRaw("gw/1/squads.json", []byte(`{"a":2}`), false); err != nil {
		t.Fatal(err)
	}
	got, err := st.ReadRaw("gw/1/squads.json")
	if err != nil || string(got) != `{"a":2}` {
		t.Fatalf("got %q, err %v", got, err)
	}
	entries, err := os.ReadDir(filepath.Dir(st.Path("gw/1/squads.json")))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "squads.json" {
		t.Fatalf("expected only squads.json, got %v", entries)
	}
	info, err := os.Stat(st.Path("gw/1/squads.json"))
	if err != nil || info.Mode().Perm() != 0o644 {
		t.Fatalf("mode %v, err %v", info.Mode(), err)
	}
}

func TestWriteRawFailureLeavesNoTempFile(t *testing.T) {
	st := NewJSONStore(t.TempDir())
	// Make the target a non-empty directory so the final rename fails.
	if err := os.MkdirAll(st.Path("x/f.json"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(st.Path("x/f.json"), "keep"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := st.WriteRaw("x/f.json", []byte("new"), false); err == nil {
		t.Fatal("expected rename error")
	}
	entries, err := os.ReadDir(st.Path("x"))
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 {
		t.Fatalf("temp file left behind: %v", entries)
	}
}
