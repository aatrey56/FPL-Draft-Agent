// Package config loads repo-local .env configuration so binaries pick up
// LEAGUE_ID / ENTRY_ID / FPL_MCP_API_KEY without the user exporting anything.
package config

import (
	"os"
	"path/filepath"
	"strings"
)

// LoadDotEnv reads KEY=VALUE lines from path into the process environment.
// Real environment variables win: a key already set is never overwritten.
// Comment lines (#) and blanks are skipped; surrounding quotes are stripped.
// A missing file is not an error — .env is optional.
func LoadDotEnv(path string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return err
	}
	for line := range strings.SplitSeq(string(raw), "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimSpace(value)
		value = strings.Trim(value, `"'`)
		if key == "" || os.Getenv(key) != "" {
			continue
		}
		if err := os.Setenv(key, value); err != nil {
			return err
		}
	}
	return nil
}

// FindAndLoadDotEnv walks up from the working directory looking for a .env
// (repo root when run from apps/*) and loads the first one found.
func FindAndLoadDotEnv() error {
	dir, err := os.Getwd()
	if err != nil {
		return err
	}
	for range [6]struct{}{} {
		candidate := filepath.Join(dir, ".env")
		if _, err := os.Stat(candidate); err == nil {
			return LoadDotEnv(candidate)
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return nil
}
