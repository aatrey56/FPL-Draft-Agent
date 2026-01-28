package store

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
)

type JSONStore struct {
	Root string // e.g. "data/raw"
}

func NewJSONStore(root string) *JSONStore {
	return &JSONStore{Root: root}
}

func (s *JSONStore) Path(rel string) string {
	return filepath.Join(s.Root, rel)
}

func (s *JSONStore) Exists(rel string) bool {
	_, err := os.Stat(s.Path(rel))
	return err == nil
}

func (s *JSONStore) WriteRaw(rel string, body []byte, pretty bool) error {
	path := s.Path(rel)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	if pretty {
		var v any
		if err := json.Unmarshal(body, &v); err == nil {
			buf := &bytes.Buffer{}
			enc := json.NewEncoder(buf)
			enc.SetIndent("", "  ")
			_ = enc.Encode(v)
			body = buf.Bytes()
		}
	}

	return os.WriteFile(path, body, 0o644)
}

func (s *JSONStore) ReadRaw(rel string) ([]byte, error) {
	path := s.Path(rel)
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	return b, err
}
