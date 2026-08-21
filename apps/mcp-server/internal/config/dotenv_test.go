package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDotEnvSetsAndPreservesEnv(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	content := "# comment\nLEAGUE_ID=12345\nENTRY_ID=\"67890\"\nPRESET=file\n\nBROKENLINE\n"
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PRESET", "real-env")
	t.Setenv("LEAGUE_ID", "")
	t.Setenv("ENTRY_ID", "")

	if err := LoadDotEnv(path); err != nil {
		t.Fatal(err)
	}
	if got := os.Getenv("LEAGUE_ID"); got != "12345" {
		t.Fatalf("LEAGUE_ID = %q", got)
	}
	if got := os.Getenv("ENTRY_ID"); got != "67890" {
		t.Fatalf("quotes not stripped: %q", got)
	}
	if got := os.Getenv("PRESET"); got != "real-env" {
		t.Fatalf("real env must win over .env: %q", got)
	}
}

func TestLoadDotEnvMissingFileIsFine(t *testing.T) {
	if err := LoadDotEnv(filepath.Join(t.TempDir(), ".env")); err != nil {
		t.Fatal(err)
	}
}
