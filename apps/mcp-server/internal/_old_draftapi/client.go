package draftapi

import (
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

type Client struct {
	http     *http.Client
	cacheDir string
}

func NewClient(cacheDir string) *Client {
	return &Client{
		http: &http.Client{
			Timeout: 15 * time.Second,
		},
		cacheDir: cacheDir,
	}
}

// GetJSON fetches a URL and caches the response to disk.
// If refresh is false and cache is fresh, it returns cached data.
func (c *Client) GetJSON(cacheKey string, url string, ttl time.Duration, refresh bool) ([]byte, error) {
	if err := os.MkdirAll(c.cacheDir, 0o755); err != nil {
		return nil, err
	}

	cachePath := filepath.Join(c.cacheDir, cacheKey+".json")

	if !refresh && ttl > 0 {
		if info, err := os.Stat(cachePath); err == nil {
			if time.Since(info.ModTime()) < ttl {
				return os.ReadFile(cachePath)
			}
		}
	}

	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(resp.Body)
		return nil, fmt.Errorf("GET %s failed: %s (%s)", url, resp.Status, string(b))
	}

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	if ttl > 0 {
		_ = os.WriteFile(cachePath, body, 0o644)
	}

	return body, nil
}