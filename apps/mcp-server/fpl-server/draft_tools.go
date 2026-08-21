package main

// Decision-layer tools (the serving layer for the ML pipeline).
//
// These tools serve the artifacts the Python pipeline writes — they do no
// modeling themselves, keeping the repo's boundary intact (Python computes,
// Go serves local JSON):
//
//	draft_board  <- data/derived/ml/projections_2627.json
//	player_card  <- projections + data/derived/ml/player_history.json
//	             + data/raw/<season>/bootstrap/bootstrap-static.json (live news)
//	waiver_plan  <- data/derived/<season>/ml/waiver_plan.json
//	my_week      <- data/derived/<season>/ml/my_week.json
//	drop_radar   <- data/derived/<season>/ml/ownership_events.json
//
// Season-aware paths use ServerConfig.DefaultSeason unless the call passes an
// explicit season. The legacy flat roots remain the 2025-26 archive.

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ---------------------------------------------------------------------------
// Shared plumbing
// ---------------------------------------------------------------------------

// projectionRow mirrors one entry of projections_2627.json (written by
// backend.ml.projection --project).
type projectionRow struct {
	Code            int     `json:"code"`
	WebName         string  `json:"web_name"`
	Position        string  `json:"position"`
	ProjectedPoints float64 `json:"projected_points"`
	Tier            int     `json:"tier"`
	Vor             float64 `json:"vor"`
	Confidence      string  `json:"confidence"`
	RiskFlags       string  `json:"risk_flags"`
	PositionRank    int     `json:"position_rank"`
	Drivers         string  `json:"drivers"`
}

func (cfg ServerConfig) season(override string) string {
	if override != "" {
		return override
	}
	return cfg.DefaultSeason
}

// ArchiveSeason is the season stored at the flat roots (data/raw, data/derived)
// from before the season-nested layout existed. It never gets a season segment.
const ArchiveSeason = "2025-26"

// rawDir resolves the raw-data directory for a season: <root>/<season> for
// current seasons, the flat root for the 2025-26 archive (or no season at all).
func (cfg ServerConfig) rawDir(override string) string {
	if s := cfg.season(override); s != "" && s != ArchiveSeason {
		return filepath.Join(cfg.RawRoot, s)
	}
	return cfg.RawRoot
}

// derivedDir is rawDir for the derived root.
func (cfg ServerConfig) derivedDir(override string) string {
	if s := cfg.season(override); s != "" && s != ArchiveSeason {
		return filepath.Join(cfg.DerivedRoot, s)
	}
	return cfg.DerivedRoot
}

func readJSONFile(path string, v any) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return fmt.Errorf("read %s: %w (run the pipeline that writes it first)", path, err)
	}
	return json.Unmarshal(raw, v)
}

func loadProjections(cfg ServerConfig) ([]projectionRow, error) {
	var rows []projectionRow
	err := readJSONFile(filepath.Join(cfg.DerivedRoot, "ml/projections_2627.json"), &rows)
	return rows, err
}

// ---------------------------------------------------------------------------
// draft_board
// ---------------------------------------------------------------------------

type DraftBoardArgs struct {
	Position string `json:"position,omitempty" jsonschema:"Filter to one position: GKP DEF MID or FWD (empty = all)"`
	Top      int    `json:"top,omitempty" jsonschema:"Players per position to return (default 20)"`
}

func draftBoardHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, DraftBoardArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args DraftBoardArgs) (*mcp.CallToolResult, any, error) {
		rows, err := loadProjections(cfg)
		if err != nil {
			return toolError(err), nil, nil
		}
		top := args.Top
		if top <= 0 {
			top = 20
		}
		position := strings.ToUpper(strings.TrimSpace(args.Position))
		out := make([]projectionRow, 0, top*4)
		for _, row := range rows {
			if position != "" && row.Position != position {
				continue
			}
			if row.PositionRank <= top {
				out = append(out, row)
			}
		}
		sort.Slice(out, func(i, j int) bool {
			if out[i].Position != out[j].Position {
				return out[i].Position < out[j].Position
			}
			return out[i].PositionRank < out[j].PositionRank
		})
		return toolMarshal(map[string]any{
			"note":  "projected_points = next-season projection (see PROJECTION_MODEL_SPEC); vor = value over replacement at 12-team starter depth",
			"board": out,
		})
	}
}

// ---------------------------------------------------------------------------
// player_card
// ---------------------------------------------------------------------------

type PlayerCardArgs struct {
	Name   string `json:"name" jsonschema:"Player web name to look up (case-insensitive, substring ok; required)"`
	Season string `json:"season,omitempty" jsonschema:"Season for live availability news (default: server default season)"`
}

// historyRow mirrors one season entry of player_history.json (written by
// backend.ml.serve_export).
type historyRow struct {
	Season      string   `json:"season"`
	TeamName    string   `json:"team_name"`
	Minutes     *float64 `json:"minutes"`
	TotalPoints *float64 `json:"total_points"`
	Goals       *float64 `json:"goals_scored"`
	Assists     *float64 `json:"assists"`
	Xg          *float64 `json:"expected_goals"`
	Xa          *float64 `json:"expected_assists"`
}

func playerCardHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, PlayerCardArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args PlayerCardArgs) (*mcp.CallToolResult, any, error) {
		needle := strings.ToLower(strings.TrimSpace(args.Name))
		if needle == "" {
			return toolError(fmt.Errorf("name is required")), nil, nil
		}
		rows, err := loadProjections(cfg)
		if err != nil {
			return toolError(err), nil, nil
		}
		match, err := resolveProjection(rows, args.Name)
		if err != nil {
			return toolError(err), nil, nil
		}
		// Multi-season history — the reversion safeguard: no card is served
		// without the player's history next to it.
		history := map[string][]historyRow{}
		historyPath := filepath.Join(cfg.DerivedRoot, "ml/player_history.json")
		if err := readJSONFile(historyPath, &history); err != nil {
			return toolError(err), nil, nil
		}

		// Season bootstrap: live availability, and the identity fallback for
		// players the model refused to project (best effort for the former).
		type bootstrapElement struct {
			Code                     int    `json:"code"`
			WebName                  string `json:"web_name"`
			Status                   string `json:"status"`
			News                     string `json:"news"`
			ChanceOfPlayingNextRound *int   `json:"chance_of_playing_next_round"`
		}
		var bootstrap struct {
			Elements []bootstrapElement `json:"elements"`
		}
		bootstrapPath := filepath.Join(cfg.rawDir(args.Season), "bootstrap/bootstrap-static.json")
		bootstrapErr := readJSONFile(bootstrapPath, &bootstrap)

		availability := func(el bootstrapElement) map[string]any {
			return map[string]any{
				"status": el.Status, "news": el.News,
				"chance_of_playing_next_round": el.ChanceOfPlayingNextRound,
			}
		}

		if match == nil {
			// The Maddison case: under the minutes floor last season (long
			// injury), promoted, or newly signed — the model refuses to guess,
			// but the card must still show who they were and how they are now.
			var el *bootstrapElement
			for i := range bootstrap.Elements {
				if strings.ToLower(bootstrap.Elements[i].WebName) == needle {
					el = &bootstrap.Elements[i]
					break
				}
			}
			if el == nil {
				for i := range bootstrap.Elements {
					if strings.Contains(strings.ToLower(bootstrap.Elements[i].WebName), needle) {
						if el != nil {
							return toolError(fmt.Errorf("ambiguous name %q (matches %s and %s) — be more specific",
								args.Name, el.WebName, bootstrap.Elements[i].WebName)), nil, nil
						}
						el = &bootstrap.Elements[i]
					}
				}
			}
			if el == nil {
				if bootstrapErr != nil {
					return toolError(fmt.Errorf("no projected player matches %q (and no season bootstrap to fall back to: %v)", args.Name, bootstrapErr)), nil, nil
				}
				return toolError(fmt.Errorf("no player matches %q in projections or the current season", args.Name)), nil, nil
			}
			return toolMarshal(map[string]any{
				"projection":     nil,
				"web_name":       el.WebName,
				"season_history": history[fmt.Sprintf("%d", el.Code)],
				"availability":   availability(*el),
				"note":           "No projection: under 500 league minutes in 2025-26 (long injury), promoted, or new to the league — the model refuses to guess rather than assume zero. Judge from season_history and availability/news; treat early-season minutes as the real signal.",
			})
		}

		news := map[string]any{}
		if bootstrapErr == nil {
			for _, el := range bootstrap.Elements {
				if el.Code == match.Code {
					news = availability(el)
					break
				}
			}
		}

		return toolMarshal(map[string]any{
			"projection":     match,
			"season_history": history[fmt.Sprintf("%d", match.Code)],
			"availability":   news,
			"note":           "Judge the projection against season_history: a one-season dip (injury) or spike may revert — the model has no multi-year reversion (see ISSUES.md).",
		})
	}
}

// ---------------------------------------------------------------------------
// waiver_plan
// ---------------------------------------------------------------------------

type WaiverPlanArgs struct {
	Season string `json:"season,omitempty" jsonschema:"Season (default: server default season)"`
}

func waiverPlanHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, WaiverPlanArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args WaiverPlanArgs) (*mcp.CallToolResult, any, error) {
		path := filepath.Join(cfg.derivedDir(args.Season), "ml/waiver_plan.json")
		var plan map[string]any
		if err := readJSONFile(path, &plan); err != nil {
			return toolError(err), nil, nil
		}
		plan["note"] = "labels: upgrade = better now and all season; stream = next-3-GW help only (plan to re-drop); hold = tough fixtures now but better rest-of-season. unprojected_squad = players the model cannot value (never auto-dropped — check their player_card). Regenerate with: python -m backend.ml.waiver"
		return toolMarshal(plan)
	}
}

// ---------------------------------------------------------------------------
// my_week
// ---------------------------------------------------------------------------

type MyWeekArgs struct {
	Season string `json:"season,omitempty" jsonschema:"Season (default: server default season)"`
}

func myWeekHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, MyWeekArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args MyWeekArgs) (*mcp.CallToolResult, any, error) {
		path := filepath.Join(cfg.derivedDir(args.Season), "ml/my_week.json")
		var week map[string]any
		if err := readJSONFile(path, &week); err != nil {
			return toolError(err), nil, nil
		}
		week["note"] = "gw_xp = projection/38 × fixture multiplier × availability (heuristic until the match xP model lands). attention = players needing a human call before the deadline; unprojected players are never scored as zero-value certainty. Regenerate with: python -m backend.ml.myweek"
		return toolMarshal(week)
	}
}

// ---------------------------------------------------------------------------
// drop_radar
// ---------------------------------------------------------------------------

type DropRadarArgs struct {
	Season string `json:"season,omitempty" jsonschema:"Season (default: server default season)"`
	Limit  int    `json:"limit,omitempty" jsonschema:"Most recent events to return (default 25)"`
}

func dropRadarHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, DropRadarArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args DropRadarArgs) (*mcp.CallToolResult, any, error) {
		path := filepath.Join(cfg.derivedDir(args.Season), "ml/ownership_events.json")
		var events []map[string]any
		if err := readJSONFile(path, &events); err != nil {
			return toolError(err), nil, nil
		}
		limit := args.Limit
		if limit <= 0 {
			limit = 25
		}
		if len(events) > limit {
			events = events[len(events)-limit:]
		}
		return toolMarshal(map[string]any{
			"note":   "ownership changes between element-status snapshots (drop = released to free agency). Regenerate with the fetcher + python -m backend.ml.ownership",
			"events": events,
		})
	}
}
