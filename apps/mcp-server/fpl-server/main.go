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

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/config"
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
	DefaultSeason  string // season used by the decision tools when a call omits one
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
		defaultSeason  = flag.String("default-season", "2026-27", "season the decision tools (draft_board/player_card/waiver_plan/drop_radar) read when a call omits one")
		writeDerived   = flag.Bool("write-derived", true, "write computed summaries to derived root")
		computeMissing = flag.Bool("compute-missing", true, "compute summaries if missing")
		requireAuth    = flag.Bool("require-auth", true, "require API key auth via FPL_MCP_API_KEY")
		authHeader     = flag.String("auth-header", "X-API-Key", "HTTP header to read API key from")
	)
	flag.Parse()

	// Pick up FPL_MCP_API_KEY (and friends) from the repo .env so the server
	// starts without exported environment variables. Real env always wins.
	if err := config.FindAndLoadDotEnv(); err != nil {
		log.Printf("warning: could not load .env: %v", err)
	}

	cfg := ServerConfig{
		RawRoot:        *rawRoot,
		DerivedRoot:    *derivedRoot,
		DefaultSeason:  *defaultSeason,
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
		Name:        "current_roster",
		Description: "Show a manager's current squad (starters + bench) with player names, teams, and positions",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args CurrentRosterArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildCurrentRoster(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolMarshal(out)
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "player_gw_stats",
		Description: "Per-gameweek stats for a specific player: minutes, points, goals, assists, xG, xA across a GW range",
	}, func(ctx context.Context, req *mcp.CallToolRequest, args PlayerGWStatsArgs) (*mcp.CallToolResult, any, error) {
		out, err := buildPlayerGWStats(cfg, args)
		if err != nil {
			return toolError(err), nil, nil
		}
		return toolMarshal(out)
	})

	addTool(server, &registry, &mcp.Tool{
		Name:        "draft_board",
		Description: "The 2026-27 draft board: players ranked per position with projected points, tiers, VOR (12-team), confidence, risk flags, and drivers",
	}, draftBoardHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "player_card",
		Description: "Everything about one player: projection with drivers, multi-season history (judge dips/spikes), and live availability news",
	}, playerCardHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "waiver_plan",
		Description: "Roster-aware add/drop recommendations: best-XI evaluation plus adds paired with drops, each labeled upgrade/stream/hold with next-3-GW and season gains",
	}, waiverPlanHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "drop_radar",
		Description: "Recent league ownership changes (who got dropped/added/traded) from element-status snapshot diffs",
	}, dropRadarHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "my_week",
		Description: "Start/sit for the next gameweek: best XI + bench by per-GW expected points, with attention flags (injuries, blanks, unprojected players)",
	}, myWeekHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "trade_check",
		Description: "Evaluate a proposed trade: give vs get compared on season projection and VOR (starter scarcity), with warnings for unprojected players",
	}, tradeCheckHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "team_env",
		Description: "Per-team match environment: FPL points/xG generated and conceded (by position, home/away) — shootout vs stalemate context for fixtures",
	}, teamEnvHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "gw_live",
		Description: "Live gameweek matchup tracker: my XI vs my H2H opponent with in-play points per player, bench points, and who has played (refresh the fetcher during matches)",
	}, gwLiveHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "league_pulse",
		Description: "League state in one call: standings, recent transactions (named), and the game clock (current/next GW, waivers status)",
	}, leaguePulseHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "manager_card",
		Description: "Everything about one manager: season record, form streak, upcoming schedule, optional head-to-head vs an opponent and original draft picks",
	}, managerCardHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "epl",
		Description: "The real Premier League: standings table and/or fixture results for a gameweek",
	}, eplHandler(cfg))

	addTool(server, &registry, &mcp.Tool{
		Name:        "gw_report",
		Description: "Post-gameweek review: points by position per matchup (why you won/lost) and lineup efficiency (bench points, zero-minute starters)",
	}, gwReportHandler(cfg))

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
		b, err := json.MarshalIndent(map[string]any{"tools": registry}, "", "  ")
		if err != nil {
			http.Error(w, `{"error":"failed to marshal tool list"}`, http.StatusInternalServerError)
			return
		}
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
	gamePath := filepath.Join(cfg.rawDir(""), "game", "game.json")
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
	absPath := filepath.Join(cfg.derivedDir(""), relPath)
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
	root := cfg.derivedDir("")
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

	st := store.NewJSONStore(cfg.rawDir(""))
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

func toolJSON(res []byte, err error) (*mcp.CallToolResult, any, error) {
	if err != nil {
		return toolError(err), nil, nil
	}
	return toolJSONBytes(res), nil, nil
}

// toolMarshal JSON-encodes v and returns a tool result. If marshaling fails
// (e.g. due to an un-serializable field type), the error is surfaced as a
// structured tool error rather than silently returning an empty body.
func toolMarshal(v any) (*mcp.CallToolResult, any, error) {
	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return toolError(fmt.Errorf("marshal response: %w", err)), nil, nil
	}
	return toolJSONBytes(b), nil, nil
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
