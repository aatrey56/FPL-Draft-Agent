package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/summary"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

type ServerConfig struct {
	RawRoot        string
	DerivedRoot    string
	WriteDerived   bool
	ComputeMissing bool
}

type LeagueGWArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	GW       int `json:"gw" jsonschema:"Gameweek (0 = current)"`
}

type LeagueGWAndHorizonArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	GW       int `json:"gw" jsonschema:"Gameweek (0 = current)"`
	Horizon  int `json:"horizon" jsonschema:"Rolling horizon in GWs (default 5)"`
}

type LeagueGWAndRiskArgs struct {
	LeagueID int    `json:"league_id" jsonschema:"Draft league id (required)"`
	GW       int    `json:"gw" jsonschema:"Gameweek (0 = current)"`
	Horizon  int    `json:"horizon" jsonschema:"Rolling horizon in GWs (default 5)"`
	Risk     string `json:"risk" jsonschema:"Risk level: low|med|high (default med)"`
}

type PlayerFormArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	Horizon  int `json:"horizon" jsonschema:"Rolling horizon in GWs (default 5)"`
	AsOfGW   int `json:"as_of_gw" jsonschema:"As-of gameweek (0 = current)"`
}

type FixturesArgs struct {
	LeagueID int  `json:"league_id" jsonschema:"Draft league id (required)"`
	AsOfGW   *int `json:"as_of_gw,omitempty" jsonschema:"Start from gameweek (0 = current)"`
	GW       *int `json:"gw,omitempty" jsonschema:"Alias for as_of_gw"`
	Horizon  *int `json:"horizon,omitempty" jsonschema:"How many GWs forward (default 5)"`
}

type ManagerLookupArgs struct {
	LeagueID int `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID  int `json:"entry_id" jsonschema:"Entry id (required)"`
}

type PlayerLookupArgs struct {
	ElementID int `json:"element_id" jsonschema:"Player element id (required)"`
}

type toolInfo struct {
	Name        string `json:"name"`
	Description string `json:"description"`
}

func main() {
	var (
		addr           = flag.String("addr", ":8080", "HTTP listen address")
		mcpPath        = flag.String("path", "/mcp", "HTTP path for MCP endpoint")
		rawRoot        = flag.String("raw-root", "data/raw", "root directory for raw JSON")
		derivedRoot    = flag.String("derived-root", "data/derived", "root directory for derived JSON")
		writeDerived   = flag.Bool("write-derived", true, "write computed summaries to derived root")
		computeMissing = flag.Bool("compute-missing", true, "compute summaries if missing")
		requireAuth    = flag.Bool("require-auth", true, "require API key auth via FPL_MCP_API_KEY")
		authHeader     = flag.String("auth-header", "X-API-Key", "HTTP header to read API key from")
	)
	flag.Parse()

	cfg := ServerConfig{
		RawRoot:        *rawRoot,
		DerivedRoot:    *derivedRoot,
		WriteDerived:   *writeDerived,
		ComputeMissing: *computeMissing,
	}

	server := mcp.NewServer(
		&mcp.Implementation{
			Name:    "fpl-draft-mcp",
			Version: "0.2.0",
		},
		nil,
	)

	registry := make([]toolInfo, 0, 16)

	addTool(server, &registry, &mcp.Tool{
		Name:        "player_form",
		Description: "Rolling points/minutes/ownership for each player",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args PlayerFormArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		h := args.Horizon
		if h <= 0 {
			h = 5
		}
		gw, err := resolveGW(cfg, args.AsOfGW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/player_form/%d/h%d.json", leagueID, h)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, []int{h}, []string{"low", "med", "high"}))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "waiver_targets",
		Description: "Ranked add suggestions for your league",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWAndRiskArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		h := args.Horizon
		if h <= 0 {
			h = 5
		}
		risk := normalizeRisk(args.Risk)
		relPath := fmt.Sprintf("summary/waiver_targets/%d/gw/%d_h%d_risk-%s.json", leagueID, gw, h, risk)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, []int{h}, []string{risk}))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "waiver_recommendations",
		Description: "Personalized waiver report (fixtures/form/points/xG) with drop suggestions",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args WaiverRecommendationsArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildWaiverRecommendations(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolJSONBytes(out), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "league_summary",
		Description: "League weekly summary (roster, points, bench, record, opponent)",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/league/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "matchup_breakdown",
		Description: "Points by position for each matchup (why you won/lost)",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/matchup/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "standings",
		Description: "League standings table snapshot for a gameweek",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/standings/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "transactions",
		Description: "Weekly waivers/free agents/trades digest per manager",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/transactions/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "lineup_efficiency",
		Description: "Bench points, bench points played, and zero-minute starters",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/lineup_efficiency/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "strength_of_schedule",
		Description: "Past/future opponent difficulty based on standings at a gameweek",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/strength_of_schedule/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "ownership_scarcity",
		Description: "Ownership counts by position and hoarders",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueGWArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		gw, err := resolveGW(cfg, args.GW)
		if err != nil {
			return toolError(err), nil, nil
		}
		relPath := fmt.Sprintf("summary/ownership_scarcity/%d/gw/%d.json", leagueID, gw)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "fixtures",
		Description: "Upcoming fixtures from bootstrap-static",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args FixturesArgs) (*mcp.CallToolResult, any, error) {
		leagueID := args.LeagueID
		if leagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		asOf := 0
		if args.AsOfGW != nil {
			asOf = *args.AsOfGW
		} else if args.GW != nil {
			asOf = *args.GW
		}
		gw, err := resolveGW(cfg, asOf)
		if err != nil {
			return toolError(err), nil, nil
		}
		h := 0
		if args.Horizon != nil {
			h = *args.Horizon
		}
		if h <= 0 {
			h = 5
		}
		relPath := fmt.Sprintf("summary/fixtures/%d/from_gw/%d_h%d.json", leagueID, gw, h)
		return toolJSON(loadSummaryFile(cfg, leagueID, gw, relPath, []int{h}, []string{"low", "med", "high"}))
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "fixture_difficulty",
		Description: "Rank next-gameweek fixtures by opponent points conceded per position (home/away), with season/recent blend",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args FixtureDifficultyArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildFixtureDifficulty(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		return toolJSONBytes(b), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "player_lookup",
		Description: "Lookup a player by element id",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args PlayerLookupArgs) (*mcp.CallToolResult, any, error) {
		if args.ElementID == 0 {
			return toolError(fmt.Errorf("element_id is required")), nil, nil
		}
		out, err := lookupPlayer(cfg, args.ElementID)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolJSONBytes(out), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "manager_lookup",
		Description: "Lookup a manager by entry id",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args ManagerLookupArgs) (*mcp.CallToolResult, any, error) {
		if args.LeagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		if args.EntryID == 0 {
			return toolError(fmt.Errorf("entry_id is required")), nil, nil
		}
		out, err := lookupManager(cfg, args.LeagueID, args.EntryID)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolJSONBytes(out), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "manager_schedule",
		Description: "Manager schedule from league details (no entry snapshots required)",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args ManagerScheduleArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildManagerSchedule(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		return toolJSONBytes(b), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "manager_streak",
		Description: "Win-streak stats for a manager using league details",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args ManagerStreakArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildManagerStreak(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		return toolJSONBytes(b), nil, nil
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "league_entries",
		Description: "List league teams (entry id/name) from league details",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args LeagueEntriesArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildLeagueEntries(cfg, args.LeagueID)
		if err != nil {
			return toolError(err), nil, nil
		}
		b, _ := json.MarshalIndent(out, "", "  ")
		return toolJSONBytes(b), nil, nil
	})

	handler := mcp.NewStreamableHTTPHandler(func(r *http.Request) *mcp.Server {
		return server
	}, &mcp.StreamableHTTPOptions{JSONResponse: true})

	apiKey := strings.TrimSpace(os.Getenv("FPL_MCP_API_KEY"))
	if *requireAuth && apiKey == "" {
		log.Fatal("FPL_MCP_API_KEY is required (set env var or run with --require-auth=false)")
	}

	withAuth := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if apiKey == "" {
				next(w, r)
				return
			}
			key := strings.TrimSpace(r.Header.Get(*authHeader))
			if key == "" {
				if authz := r.Header.Get("Authorization"); strings.HasPrefix(strings.ToLower(authz), "bearer ") {
					key = strings.TrimSpace(authz[7:])
				}
			}
			if subtle.ConstantTimeCompare([]byte(key), []byte(apiKey)) != 1 {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(http.StatusUnauthorized)
				w.Write([]byte(`{"error":"unauthorized"}`))
				return
			}
			next(w, r)
		}
	}

	http.HandleFunc("/health", withAuth(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		w.Write([]byte(`{"status":"ok"}`))
	}))

	http.HandleFunc("/tools", withAuth(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		b, _ := json.MarshalIndent(map[string]any{"tools": registry}, "", "  ")
		w.Write(b)
	}))

	http.HandleFunc(*mcpPath, withAuth(func(w http.ResponseWriter, r *http.Request) {
		handler.ServeHTTP(w, r)
	}))

	log.Printf("MCP HTTP server listening on %s%s", *addr, *mcpPath)
	if err := http.ListenAndServe(*addr, nil); err != nil {
		log.Fatal(err)
	}
}

func addTool[T any](server *mcp.Server, registry *[]toolInfo, tool *mcp.Tool, handler func(context.Context, *mcp.CallToolRequest, T) (*mcp.CallToolResult, any, error)) {
	*registry = append(*registry, toolInfo{Name: tool.Name, Description: tool.Description})
	mcp.AddTool(server, tool, handler)
}

func resolveGW(cfg ServerConfig, gw int) (int, error) {
	if gw > 0 {
		return gw, nil
	}
	gamePath := filepath.Join(cfg.RawRoot, "game", "game.json")
	raw, err := os.ReadFile(gamePath)
	if err != nil {
		return 0, fmt.Errorf("missing game meta: %w", err)
	}
	var game struct {
		CurrentEvent int `json:"current_event"`
	}
	if err := json.Unmarshal(raw, &game); err != nil {
		return 0, err
	}
	if game.CurrentEvent == 0 {
		return 0, fmt.Errorf("current_event missing in game.json")
	}
	return game.CurrentEvent, nil
}

func normalizeRisk(r string) string {
	r = strings.TrimSpace(strings.ToLower(r))
	if r == "" {
		return "med"
	}
	if r == "medium" {
		return "med"
	}
	switch r {
	case "low", "med", "high":
		return r
	default:
		return "med"
	}
}

func loadSummaryFile(cfg ServerConfig, leagueID int, gw int, relPath string, horizons []int, risks []string) ([]byte, error) {
	if leagueID == 0 {
		return nil, fmt.Errorf("league_id is required")
	}
	if gw == 0 {
		return nil, fmt.Errorf("gw is required")
	}
	absPath := filepath.Join(cfg.DerivedRoot, relPath)
	if b, err := os.ReadFile(absPath); err == nil {
		return b, nil
	}
	if !cfg.ComputeMissing {
		return nil, fmt.Errorf("missing summary file: %s", absPath)
	}
	h := horizons
	if len(h) == 0 {
		h = []int{5}
	}
	r := risks
	if len(r) == 0 {
		r = []string{"low", "med", "high"}
	}
	root := cfg.DerivedRoot
	cleanup := func() {}
	if !cfg.WriteDerived {
		tmp, err := os.MkdirTemp("", "fpl-summary-*")
		if err != nil {
			return nil, err
		}
		root = tmp
		cleanup = func() { _ = os.RemoveAll(tmp) }
	}
	defer cleanup()

	st := store.NewJSONStore(cfg.RawRoot)
	if strings.HasPrefix(relPath, "summary/transactions/") {
		if err := summary.BuildTransactionsSummary(st, root, leagueID, gw); err != nil {
			return nil, err
		}
		return os.ReadFile(filepath.Join(root, relPath))
	}
	ld, entryIDs, err := loadLeagueDetails(st, leagueID)
	if err != nil {
		return nil, err
	}
	if err := ensureLedger(st, root, leagueID); err != nil {
		return nil, err
	}
	if err := ensureSnapshots(st, root, leagueID, entryIDs, gw, gw); err != nil {
		return nil, err
	}

	if err := summary.BuildLeagueSummaries(st, root, leagueID, ld, entryIDs, gw, gw, h, r); err != nil {
		return nil, err
	}
	return os.ReadFile(filepath.Join(root, relPath))
}

func loadLeagueDetails(st *store.JSONStore, leagueID int) (summary.LeagueDetails, []int, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/details.json", leagueID))
	if err != nil {
		return summary.LeagueDetails{}, nil, err
	}
	var ld summary.LeagueDetails
	if err := json.Unmarshal(raw, &ld); err != nil {
		return summary.LeagueDetails{}, nil, err
	}
	entryIDs := make([]int, 0, len(ld.LeagueEntries))
	for _, e := range ld.LeagueEntries {
		entryIDs = append(entryIDs, e.EntryID)
	}
	return ld, entryIDs, nil
}

func ensureLedger(st *store.JSONStore, derivedRoot string, leagueID int) error {
	ledgerPath := filepath.Join(derivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	if _, err := os.Stat(ledgerPath); err == nil {
		return nil
	}
	raw, err := st.ReadRaw(fmt.Sprintf("draft/%d/choices.json", leagueID))
	if err != nil {
		return err
	}
	var resp ledger.DraftChoicesResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return err
	}
	out := ledger.BuildDraftLedger(leagueID, resp.Choices)
	return ledger.WriteDraftLedger(ledgerPath, out)
}

func ensureSnapshots(st *store.JSONStore, derivedRoot string, leagueID int, entryIDs []int, minGW int, maxGW int) error {
	for gw := minGW; gw <= maxGW; gw++ {
		for _, entryID := range entryIDs {
			snapPath := filepath.Join(derivedRoot, fmt.Sprintf("snapshots/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
			if _, err := os.Stat(snapPath); err == nil {
				continue
			}
			raw, err := st.ReadRaw(fmt.Sprintf("entry/%d/gw/%d.json", entryID, gw))
			if err != nil {
				return err
			}
			var resp ledger.EntryEventRaw
			if err := json.Unmarshal(raw, &resp); err != nil {
				return err
			}
			snap := ledger.BuildEntrySnapshot(leagueID, entryID, gw, resp)
			if err := ledger.WriteEntrySnapshot(snapPath, snap); err != nil {
				return err
			}
		}
	}
	return nil
}

func lookupPlayer(cfg ServerConfig, elementID int) ([]byte, error) {
	raw, err := os.ReadFile(filepath.Join(cfg.RawRoot, "bootstrap", "bootstrap-static.json"))
	if err != nil {
		return nil, err
	}
	var resp struct {
		Elements []struct {
			ID          int    `json:"id"`
			FirstName   string `json:"first_name"`
			SecondName  string `json:"second_name"`
			WebName     string `json:"web_name"`
			Team        int    `json:"team"`
			ElementType int    `json:"element_type"`
			Status      string `json:"status"`
		} `json:"elements"`
		Teams []struct {
			ID        int    `json:"id"`
			ShortName string `json:"short_name"`
		} `json:"teams"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}
	teamShort := make(map[int]string, len(resp.Teams))
	for _, t := range resp.Teams {
		teamShort[t.ID] = t.ShortName
	}
	for _, e := range resp.Elements {
		if e.ID != elementID {
			continue
		}
		name := e.WebName
		if name == "" {
			name = strings.TrimSpace(e.FirstName + " " + e.SecondName)
		}
		out := map[string]any{
			"id":            e.ID,
			"name":          name,
			"team_id":       e.Team,
			"team_short":    teamShort[e.Team],
			"position_type": e.ElementType,
			"status":        e.Status,
		}
		return json.MarshalIndent(out, "", "  ")
	}
	return nil, fmt.Errorf("player not found: %d", elementID)
}

func lookupManager(cfg ServerConfig, leagueID int, entryID int) ([]byte, error) {
	raw, err := os.ReadFile(filepath.Join(cfg.RawRoot, fmt.Sprintf("league/%d/details.json", leagueID)))
	if err != nil {
		return nil, err
	}
	var resp struct {
		LeagueEntries []struct {
			ID        int    `json:"id"`
			EntryID   int    `json:"entry_id"`
			EntryName string `json:"entry_name"`
			ShortName string `json:"short_name"`
		} `json:"league_entries"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}
	for _, e := range resp.LeagueEntries {
		if e.EntryID == entryID {
			out := map[string]any{
				"entry_id":        e.EntryID,
				"entry_name":      e.EntryName,
				"short_name":      e.ShortName,
				"league_entry_id": e.ID,
			}
			return json.MarshalIndent(out, "", "  ")
		}
	}
	return nil, fmt.Errorf("manager not found: %d", entryID)
}

func toolJSON(res []byte, err error) (*mcp.CallToolResult, any, error) {
	if err != nil {
		return toolError(err), nil, nil
	}
	return toolJSONBytes(res), nil, nil
}

func toolJSONBytes(res []byte) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		Content: []mcp.Content{
			&mcp.TextContent{Text: string(res)},
		},
	}
}

func toolError(err error) *mcp.CallToolResult {
	return &mcp.CallToolResult{
		IsError: true,
		Content: []mcp.Content{
			&mcp.TextContent{Text: fmt.Sprintf("error: %v", err)},
		},
	}
}
