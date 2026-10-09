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

// The waiver_plan note has a ranking part, which differs between the modes of
// backend.ml.waiver.recommend (its rank_by), and a part shared by all.
const (
	// rank_by "next1": the match model's xP, ranked on the next GW.
	waiverNoteModelRanking = "scorer = model, ranked on the next GW (rank_by next1: --horizon 1, or --horizon 3 without a usable horizon file — see horizon_fallback): labels and ordering are next-GW based. next1_gain = next-GW xP of the add minus the drop (add_xp_source on each rec says whether the add's value came from the model or, for a player the model does not cover, the heuristic). labels: upgrade = better next GW and all season; stream = next-GW help only (plan to re-drop); hold = no next-GW gain but better rest-of-season. Ordered by next1_gain, with holds listed after the others. next3_gain (3-GW xP of the add minus the drop) is shown for context and does not drive labels or order. A free agent without a ROS projection (e.g. promoted club) can only be a stream, with season_gain null and season_unknown true (no ROS projection to compare)."
	// rank_by "next3": the match model's xP, ranked on its 3-GW horizon.
	waiverNoteModelHorizon = "scorer = model, ranked on the next 3 GWs (rank_by next3, the default --horizon 3): labels and ordering are 3-GW-horizon based. next3_gain = 3-GW xP of the add minus the drop. labels: upgrade = better over the next 3 GWs and all season; stream = 3-GW help only (plan to re-drop); hold = no 3-GW gain but better rest-of-season. Ordered by next3_gain, with holds listed after the others. next1_gain (next-GW xP of the add minus the drop) is shown for context and does not drive labels or order. A free agent without a ROS projection (e.g. promoted club) can only be a stream, with season_gain null and season_unknown true (no ROS projection to compare)."
	// rank_by "ros": the match model's xP, ranked on the rest of the season.
	waiverNoteModelSeason = "scorer = model, ranked on the rest of the season (rank_by ros, --horizon ros): ordering is rest-of-season based — by season_gain (role-adjusted ROS of the add minus the drop), ties by next3_gain; holds are not demoted. Labels are the 3-GW ones: upgrade = better over the next 3 GWs and all season; stream = 3-GW help only (plan to re-drop); hold = no 3-GW gain but better rest-of-season. A free agent without a ROS projection has season_gain null and season_unknown true and ranks as a zero season gain."
	// rank_by "legacy": the heuristic scorer, requested or after a fallback.
	waiverNoteHeuristicRanking = "scorer = heuristic (when xp_fallback is true the model was requested but its xP file was missing, unreadable or stale — see xp_fallback_reason): every value is heuristic, --horizon is ignored, and labels and ordering are next-3-GW based, not next-GW. next3_gain = heuristic 3-GW xP of the add minus the drop. labels: upgrade = better over the next 3 GWs and all season; stream = next-3-GW help only (plan to re-drop); hold = no next-3-GW gain but better rest-of-season. Ordered by the larger of next3_gain and season_gain. next1_gain (heuristic next-GW xP of the add minus the drop) is shown for context and does not drive labels or order. A free agent without a ROS projection is never recommended."
	waiverNoteCommon           = "Horizons: gains = {gw1, gw3, ros} on each rec repeats next1_gain / next3_gain / season_gain (null = unknown at that horizon, never zero). add_next3_source says where the add's 3-GW value came from: model = the match model's xP summed over horizon_events (GW N..N+2; a blank GW adds 0, a double both fixtures), scored with the player's form frozen at GW N — a schedule view that ignores form drift — and today's availability applied to all three GWs; heuristic = ROS/38 × 3-GW fixture load × availability. horizon_fallback true (see horizon_fallback_reason) = the model ran without a usable horizon file, so every 3-GW value is heuristic. unprojected_squad = squad players with neither a ROS projection nor any model value (next-GW or 3-GW xP) — never auto-dropped, so never also a drop pick; check their player_card. Drop pick: a departed (status u) squad player is always the drop at his position; otherwise, under the model scorer, the drop is the squad player with the lowest value on the ranking horizon (xp_next for next1, next3_xp for next3, ros_adj for ros), ties broken by ros_adj, so an injured or benched player with model xP but no ROS projection (drop_ros_unknown true, drop_xp_source) can be the drop — the rec's season_gain is then null with season_unknown true (add_ros_unknown / drop_ros_unknown say which side has no ROS projection), and its next3_gain is null when the drop has no 3-GW value either (a departed drop counts as 0); under the heuristic scorer only players with a ROS projection are ever the drop. recommendations = the overall ranking, diversified under the model scorer: at most max_recs_per_drop (3) recs share one drop player, and recs past that cap only backfill, after the others, when the list would otherwise be short (rank 1 never changes; the heuristic list is not diversified). best_by_position = {GKP, DEF, MID, FWD} → the best 3 swaps at that position, each against that position's drop pick, in ranking order (an empty list = no swap beats the drop there) — read it so a run of swaps at one position does not hide the best move elsewhere. Role signals: club_moved = current club differs from last season's (null = new to the league); add_expected_minutes = mean minutes over the last 5 finished GWs (through minutes_through_gw); for available (status a) club-movers only, add_ros_adj = add_ros × clip(expected_minutes/60, 0.15, 1) and season_gain compares ros_adj, so a transferred player who is not playing is no longer rated on his old club's role (an injured or doubtful club-mover keeps his full ROS — his absence, not his role, explains the minutes). drop_candidates = the 3 most droppable squad players per position, in the drop-pick order above. never_drop (squad_prefs.json, hand-maintained) names squad players the user has decided to keep: a protected player is never a drop pick or a drop_candidates row, the drop at his position falls to the next eligible squad player, and a position whose every player is protected has no swaps (best_by_position empty there); never_drop_applied / never_drop_unmatched / never_drop_expired say what happened to every entry (until_gw passed = expired, no longer protected). role_overrides.json entries replace a player's next-GW p_start, and re-weight his model 3-GW value only for the GWs an entry covers (undated: the next GW only; valid_through_gw: through that GW; return_gw: every GW before it counts 0) (overrides_applied / overrides_unmatched / overrides_expired / overrides_stale / overrides_blocked — an entry with neither return_gw nor valid_through_gw is stale after the first deadline following its as_of (file updated/mtime fallback); an override never lifts the availability gate: a player with availability 0 is blocked; add_role_override carries the fact). Regenerate with: python -m backend.ml.waiver [--horizon {1,3,ros}]"
)

// waiverPlanNote describes the labels and ordering the served plan actually
// used. backend.ml.waiver records that as rank_by (next1 / next3 / ros under
// the match model, legacy for the heuristic scorer or after a model
// fallback). A file written before rank_by existed is keyed on its scorer
// field (the scorer that ran, not the one asked for): "model" ranked on the
// next GW, anything else — including no field at all — on the heuristic
// next 3 GWs.
func waiverPlanNote(plan map[string]any) string {
	rankBy, _ := plan["rank_by"].(string)
	if rankBy == "" {
		if scorer, _ := plan["scorer"].(string); scorer == "model" {
			rankBy = "next1"
		}
	}
	ranking := waiverNoteHeuristicRanking
	switch rankBy {
	case "next1":
		ranking = waiverNoteModelRanking
	case "next3":
		ranking = waiverNoteModelHorizon
	case "ros":
		ranking = waiverNoteModelSeason
	}
	return ranking + " " + waiverNoteCommon
}

func waiverPlanHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, WaiverPlanArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args WaiverPlanArgs) (*mcp.CallToolResult, any, error) {
		path := filepath.Join(cfg.derivedDir(args.Season), "ml/waiver_plan.json")
		var plan map[string]any
		if err := readJSONFile(path, &plan); err != nil {
			return toolError(err), nil, nil
		}
		plan["note"] = waiverPlanNote(plan)
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
		ifOut := "what FPL auto-subs do AUTOMATICALLY if he plays 0 minutes, given bench_order (automatic: true; auto_sub = {in, formation, xi_gw_xp, bench_slot}; the top-level formation/in/xi_gw_xp/text mirror it) — the user does nothing. Only when manual_if_ruled_out_before_lock is present (a hand-made XI beats the auto-sub result by more than 0.5 xP) should the user act, and only if he is ruled out before the lock."
		if week["if_out_mode"] != "autosub" {
			ifOut = "the fallback XI if he is ruled out — formation, in, benched, xi_gw_xp and a one-line text (this my_week.json predates automatic if_out rows: regenerate with python -m backend.ml.myweek)."
		}
		week["note"] = "xi = the best legal XI over every formation (DEF 3-5, MID 2-5, FWD 1-3) and formation is its shape (e.g. 5-3-2); bench is in auto-sub order (bench_slot 1 = reserve GKP, then outfielders by sub_value = gw_xp × chance a starter he could legally replace plays no minutes at all — 1 − p_appear, since a cameo blocks the auto-sub; heuristic rows use 1 − availability; names in bench_order); if_out = for each doubtful starter (status d, or p_start < 0.5 with a flag/override) " + ifOut + " gw_xp = next-GW expected points; each player's xp_source says whether it came from the match model or the projection/38 × fixture × availability heuristic (scorer = the scorer actually used — when xp_fallback is true the model was requested but its xP file was missing, unreadable or stale, see xp_fallback_reason, and every value is heuristic). attention = players needing a human call before the deadline (warnings are listed most actionable first, the scoring-source note heuristic_xp last; warning_codes holds a stable code per warning: departed, no_value, role_override, blank_gw, availability, heuristic_xp); a departed (status u) squad player is always listed, and a role_overrides.json entry replaces the player's p_start for this GW (gw_xp = p_start × xP if he starts; an override of 0 keeps him off the XI) with its fact listed here — overrides_applied / overrides_unmatched / overrides_expired / overrides_stale (undated and older than the last deadline) / overrides_blocked (availability 0 — the gate wins) say what happened to every entry. club_moved and expected_minutes (mean minutes over the last 5 finished GWs) are role signals on each row. unprojected_squad = squad players with neither a ROS projection nor model xP, never scored as zero-value certainty. Regenerate with: python -m backend.ml.myweek"
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
