package fetch

import (
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

type Client struct {
	HTTP        *http.Client
	Store       *store.JSONStore
	BaseURL     string
	UserAgent   string
	Sleep       time.Duration
	PrettyWrite bool
	UseCache    bool
	DisableWrite bool
}

func NewClient(st *store.JSONStore) *Client {
	return &Client{
		HTTP:        &http.Client{Timeout: 20 * time.Second},
		Store:       st,
		BaseURL:     "https://draft.premierleague.com/api",
		UserAgent:   "fpl-draft-raw/1.0",
		Sleep:       250 * time.Millisecond,
		PrettyWrite: true,
		UseCache:    true,
	}
}

// FetchRaw downloads urlPath (like "/game") and writes it to relPath.
// Returns raw bytes (from cache or network).
func (c *Client) FetchRaw(urlPath string, relPath string, force bool) ([]byte, error) {
	if !force && c.UseCache && c.Store.Exists(relPath) {
		return c.Store.ReadRaw(relPath)
	}

	if c.Sleep > 0 {
		time.Sleep(c.Sleep)
	}

	req, err := http.NewRequest("GET", c.BaseURL+urlPath, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", c.UserAgent)
	req.Header.Set("Accept", "application/json")

	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("GET %s failed: %d body=%s", urlPath, resp.StatusCode, string(body))
	}

	if !c.DisableWrite {
		if err := c.Store.WriteRaw(relPath, body, c.PrettyWrite); err != nil {
			return nil, err
		}
	}
	return body, nil
}
