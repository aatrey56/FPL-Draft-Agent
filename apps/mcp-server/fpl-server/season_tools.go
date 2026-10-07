package main

// Season decision tools: trade_check and league_pulse.
//
// Same boundary as the rest of the decision layer — local JSON only:
//
//	trade_check  <- data/derived/<season>/ml/player_values.json (match-model xP
//	                + role-adjusted ROS; Python backend.ml.player_values), checked
//	                fresh against the season bootstrap's next GW and last finished
//	                GW (panel_max_gw); falls back to projections (season value +
//	                VOR) with value_source "heuristic"
//	league_pulse <- league details + transactions + game meta + ownership events

import (
	"context"
	"fmt"
	"math"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// ---------------------------------------------------------------------------
// trade_check
// ---------------------------------------------------------------------------

type TradeCheckArgs struct {
	Give   []string `json:"give" jsonschema:"Names of the players I would send away (required)"`
	Get    []string `json:"get" jsonschema:"Names of the players I would receive (required)"`
	Season string   `json:"season,omitempty" jsonschema:"Season for availability news (default: server default season)"`
}

// resolveByName finds exactly one row by web name. An exact
// (case-insensitive) web-name match wins outright — "Wood" must not be
// ambiguous with "Hinshelwood" — otherwise unique-substring semantics apply:
// ambiguous -> error, absent -> (nil, nil) so callers can handle unvalued
// players explicitly.
func resolveByName[T any](rows []T, name string, webName func(*T) string) (*T, error) {
	needle := strings.ToLower(strings.TrimSpace(name))
	if needle == "" {
		return nil, fmt.Errorf("empty player name")
	}
	for i := range rows {
		if strings.ToLower(webName(&rows[i])) == needle {
			return &rows[i], nil
		}
	}
	var match *T
	for i := range rows {
		if strings.Contains(strings.ToLower(webName(&rows[i])), needle) {
			if match != nil {
				return nil, fmt.Errorf("ambiguous name %q (matches %s and %s) — be more specific",
					name, webName(match), webName(&rows[i]))
			}
			match = &rows[i]
		}
	}
	return match, nil
}

// resolveProjection is resolveByName over the preseason projections.
func resolveProjection(rows []projectionRow, name string) (*projectionRow, error) {
	return resolveByName(rows, name, func(r *projectionRow) string { return r.WebName })
}

// playerValueRow is one player of player_values.json. Pointer fields are
// null when the pipeline has no value (no projection and no model row).
type playerValueRow struct {
	Code       int      `json:"code"`
	WebName    string   `json:"web_name"`
	Position   string   `json:"position"`
	Team       string   `json:"team"`
	Status     string   `json:"status"`
	XpNext     *float64 `json:"xp_next"`
	XpSource   string   `json:"xp_source"`
	XpH3       *float64 `json:"xp_h3"`
	XpH3Source string   `json:"xp_h3_source"`
	RosPoints  *float64 `json:"ros_points"`
	RosAdj     *float64 `json:"ros_adj"`
	PStart     *float64 `json:"p_start"`
}

// playerValuesDoc is player_values.json (written by backend.ml.player_values).
type playerValuesDoc struct {
	Season                string           `json:"season"`
	GW                    int              `json:"gw"`
	PanelMaxGW            *int             `json:"panel_max_gw"`
	GeneratedAt           string           `json:"generated_at"`
	Scorer                string           `json:"scorer"`
	XpFallback            bool             `json:"xp_fallback"`
	XpFallbackReason      *string          `json:"xp_fallback_reason"`
	HorizonFallback       bool             `json:"horizon_fallback"`
	HorizonFallbackReason *string          `json:"horizon_fallback_reason"`
	Players               []playerValueRow `json:"players"`
}

// planningEvent mirrors matchmodel.next_gameweek (Python) for the draft
// bootstrap shape: the current event while it is unfinished and its deadline
// is still ahead, else events.next, else the lowest-id unfinished event whose
// deadline is ahead (min over ids, not array order — the API does not promise
// sorted events); 0 when none.
func planningEvent(cal bootstrapCalendar, now time.Time) int {
	ahead := func(e deadlineEvent) bool {
		deadline, err := time.Parse(time.RFC3339, e.DeadlineTime)
		return err == nil && !e.Finished && deadline.After(now)
	}
	for _, e := range cal.Events.Data {
		if e.ID == cal.Events.Current && cal.Events.Current != 0 && ahead(e) {
			return e.ID
		}
	}
	if cal.Events.Next != 0 {
		return cal.Events.Next
	}
	planning := 0
	for _, e := range cal.Events.Data {
		if ahead(e) && (planning == 0 || e.ID < planning) {
			planning = e.ID
		}
	}
	return planning
}

// lastFinishedEvent is the highest finished event id in the bootstrap, 0
// before GW1 finishes (Python: max(finished_gameweeks(bootstrap), default=0)).
func lastFinishedEvent(cal bootstrapCalendar) int {
	last := 0
	for _, e := range cal.Events.Data {
		if e.Finished && e.ID > last {
			last = e.ID
		}
	}
	return last
}

// loadPlayerValues returns the model values for the season, or (nil, reason)
// when trade_check must fall back to the heuristic: the file is missing or
// unreadable, its gw is not the bootstrap's next gameweek (stale), its
// panel_max_gw is not the bootstrap's last finished gameweek (stale panel —
// the same contract as Python waiver.read_gw_xp), or it was itself built
// without the match model.
func loadPlayerValues(cfg ServerConfig, season string, now time.Time) (*playerValuesDoc, string) {
	path := filepath.Join(cfg.derivedDir(season), "ml/player_values.json")
	var doc playerValuesDoc
	if err := readJSONFile(path, &doc); err != nil {
		return nil, fmt.Sprintf("player_values.json unavailable (%v) — run make derive", err)
	}
	cal, err := loadCalendar(cfg.rawDir(season))
	if err != nil {
		return nil, fmt.Sprintf("cannot check player_values.json freshness: %v", err)
	}
	next := planningEvent(cal, now)
	if next == 0 {
		return nil, "no upcoming gameweek in the bootstrap (season over?)"
	}
	if doc.GW != next {
		return nil, fmt.Sprintf("player_values.json is stale: built for GW%d, bootstrap's next GW is %d — run make derive", doc.GW, next)
	}
	// A GW N export built mid-GW N-1 trains on a panel through N-2; once N-1
	// finishes that panel is a gameweek behind and the values must be rebuilt.
	lastFinished := lastFinishedEvent(cal)
	if doc.PanelMaxGW == nil {
		return nil, "player_values.json has no panel_max_gw (built before the panel check) — run make derive"
	}
	if *doc.PanelMaxGW != lastFinished {
		return nil, fmt.Sprintf("player_values.json is stale: trained on a panel through GW%d, but the last finished GW is %d (panel not refreshed) — run make derive",
			*doc.PanelMaxGW, lastFinished)
	}
	if doc.XpFallback || doc.Scorer != "model" {
		reason := "unknown"
		if doc.XpFallbackReason != nil {
			reason = *doc.XpFallbackReason
		}
		return nil, fmt.Sprintf("player_values.json was built without the match model (%s)", reason)
	}
	return &doc, ""
}

// Verdict thresholds for the model path. ros_adj is rest-of-season points;
// xp_h3 is the next three gameweeks' expected points.
const (
	tradeRosMargin = 10.0 // ROS points: below this a trade is a close call
	tradeH3Margin  = 2.0  // 3-GW xP: a short-term cost/gain worth flagging
)

// tradeSide sums one side of a trade on the three horizons. Nil values are
// left out of the totals and reported in warnings, never counted as zero.
type tradeSide struct {
	Players []map[string]any `json:"players"`
	XpNext  float64          `json:"xp_next"`
	XpH3    float64          `json:"xp_h3"`
	RosAdj  float64          `json:"ros_adj"`
}

func modelTradeSide(doc *playerValuesDoc, names []string) (tradeSide, []string, error) {
	side := tradeSide{Players: []map[string]any{}}
	warnings := []string{}
	for _, name := range names {
		match, err := resolveByName(doc.Players, name, func(r *playerValueRow) string { return r.WebName })
		if err != nil {
			return side, nil, err
		}
		if match == nil {
			side.Players = append(side.Players, map[string]any{"web_name": name, "values": nil})
			warnings = append(warnings, fmt.Sprintf("%s is not in player_values.json — not counted; check the name", name))
			continue
		}
		side.Players = append(side.Players, map[string]any{
			"web_name": match.WebName, "position": match.Position, "team": match.Team,
			"status": match.Status, "p_start": match.PStart,
			"xp_next": match.XpNext, "xp_source": match.XpSource,
			"xp_h3": match.XpH3, "xp_h3_source": match.XpH3Source,
			"ros_adj": match.RosAdj, "ros_points": match.RosPoints,
		})
		add := func(total *float64, value *float64, label string) {
			if value == nil {
				warnings = append(warnings, fmt.Sprintf("%s has no %s value — not counted; value them manually", match.WebName, label))
				return
			}
			*total += *value
		}
		add(&side.XpNext, match.XpNext, "xp_next")
		add(&side.XpH3, match.XpH3, "xp_h3")
		add(&side.RosAdj, match.RosAdj, "ros_adj")
		if match.XpSource != "model" || match.XpH3Source != "model" {
			warnings = append(warnings, fmt.Sprintf("%s: short-term values are %s/%s, not the match model — lower scale", match.WebName, match.XpSource, match.XpH3Source))
		}
	}
	return side, warnings, nil
}

// modelVerdict leads with rest-of-season value and uses the 3-GW horizon to
// separate a clean upgrade from one that costs points now (or a rental), and
// to break near-ties on ROS value.
func modelVerdict(rosGain, h3Gain float64) string {
	switch {
	case rosGain >= tradeRosMargin && h3Gain >= -tradeH3Margin:
		return "accept: clear rest-of-season gain without a short-term cost"
	case rosGain >= tradeRosMargin:
		return "lean accept: rest-of-season gain, but you lose points over the next 3 GWs"
	case rosGain <= -tradeRosMargin && h3Gain >= tradeH3Margin:
		return "short-term rental: better next 3 GWs, clearly worse rest of season"
	case rosGain <= -tradeRosMargin:
		return "reject: you are giving up clearly more rest-of-season value"
	case h3Gain >= tradeH3Margin:
		return "lean accept: similar rest-of-season value, better next 3 GWs"
	case h3Gain <= -tradeH3Margin:
		return "lean reject: similar rest-of-season value, worse next 3 GWs"
	}
	return "close call — judge positional needs, fixtures and the warnings"
}

func round2(v float64) float64 { return math.Round(v*100) / 100 }

func modelTradeCheck(doc *playerValuesDoc, args TradeCheckArgs) (*mcp.CallToolResult, any, error) {
	give, giveWarn, err := modelTradeSide(doc, args.Give)
	if err != nil {
		return toolError(err), nil, nil
	}
	get, getWarn, err := modelTradeSide(doc, args.Get)
	if err != nil {
		return toolError(err), nil, nil
	}
	gain := map[string]float64{
		"xp_next": round2(get.XpNext - give.XpNext),
		"xp_h3":   round2(get.XpH3 - give.XpH3),
		"ros_adj": round2(get.RosAdj - give.RosAdj),
	}
	return toolMarshal(map[string]any{
		"value_source": "model",
		"source": map[string]any{
			"file": "player_values.json", "gw": doc.GW, "panel_max_gw": doc.PanelMaxGW,
			"generated_at": doc.GeneratedAt, "scorer": doc.Scorer,
			"horizon_fallback": doc.HorizonFallback, "horizon_fallback_reason": doc.HorizonFallbackReason,
		},
		"give":     give,
		"get":      get,
		"gain":     gain,
		"verdict":  modelVerdict(gain["ros_adj"], gain["xp_h3"]),
		"warnings": append(giveWarn, getWarn...),
		"note": fmt.Sprintf("Horizons: xp_next = GW%d match-model xP, xp_h3 = next 3 GWs, ros_adj = role-adjusted rest of season. "+
			"Verdict leads on ros_adj (margin %.0f) and flags 3-GW swings over %.0f. Totals are raw sums: in a 2-for-1 the extra player only helps if he would start for you.",
			doc.GW, tradeRosMargin, tradeH3Margin),
	})
}

func tradeCheckHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, TradeCheckArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args TradeCheckArgs) (*mcp.CallToolResult, any, error) {
		return evaluateTrade(cfg, args, time.Now())
	}
}

// evaluateTrade prefers the match-model values (player_values.json) and falls
// back loudly — value_source "heuristic" plus value_source_reason — to the
// preseason projection / VOR comparison.
func evaluateTrade(cfg ServerConfig, args TradeCheckArgs, now time.Time) (*mcp.CallToolResult, any, error) {
	if len(args.Give) == 0 || len(args.Get) == 0 {
		return toolError(fmt.Errorf("both give and get player lists are required")), nil, nil
	}
	doc, reason := loadPlayerValues(cfg, args.Season, now)
	if doc != nil {
		return modelTradeCheck(doc, args)
	}
	return heuristicTradeCheck(cfg, args, reason)
}

// heuristicTradeCheck is the pre-model trade_check: preseason projected
// season points and VOR. Kept as the fallback when player_values.json is
// missing or stale.
func heuristicTradeCheck(cfg ServerConfig, args TradeCheckArgs, reason string) (*mcp.CallToolResult, any, error) {
	rows, err := loadProjections(cfg)
	if err != nil {
		return toolError(err), nil, nil
	}

	side := func(names []string) (players []map[string]any, ros float64, vor float64, warnings []string, fail error) {
		for _, name := range names {
			match, err := resolveProjection(rows, name)
			if err != nil {
				return nil, 0, 0, nil, err
			}
			if match == nil {
				players = append(players, map[string]any{
					"web_name": name, "projection": nil,
				})
				warnings = append(warnings,
					fmt.Sprintf("%s has no projection (injury-shortened 2025-26, promoted, or new) — value them from player_card history, not as zero", name))
				continue
			}
			players = append(players, map[string]any{
				"web_name": match.WebName, "position": match.Position,
				"ros_points": match.ProjectedPoints, "tier": match.Tier,
				"vor": match.Vor, "risk_flags": match.RiskFlags,
			})
			ros += match.ProjectedPoints
			vor += match.Vor
		}
		return players, ros, vor, warnings, nil
	}

	give, giveRos, giveVor, giveWarn, err := side(args.Give)
	if err != nil {
		return toolError(err), nil, nil
	}
	get, getRos, getVor, getWarn, err := side(args.Get)
	if err != nil {
		return toolError(err), nil, nil
	}

	verdict := "close call — judge positional needs and the warnings"
	switch {
	case getVor-giveVor > 15:
		verdict = "accept: clear value gain at starter-relevance (VOR)"
	case giveVor-getVor > 15:
		verdict = "reject: you are giving up clearly more starter value"
	}

	return toolMarshal(map[string]any{
		"value_source":        "heuristic",
		"value_source_reason": reason,
		"give":                map[string]any{"players": give, "ros_points": giveRos, "vor": giveVor},
		"get":                 map[string]any{"players": get, "ros_points": getRos, "vor": getVor},
		"season_gain":         getRos - giveRos,
		"vor_gain":            getVor - giveVor,
		"verdict":             verdict,
		"warnings":            append(giveWarn, getWarn...),
		"note":                "HEURISTIC FALLBACK (see value_source_reason): preseason projections, no current form or fixtures. vor_gain weighs starter scarcity (12-team league) and beats raw season_gain for 2-for-1 trades: a bench player's points don't help you on game day. Unprojected players are NOT counted in totals — value them manually.",
	})
}

// ---------------------------------------------------------------------------
// team_env
// ---------------------------------------------------------------------------

type TeamEnvArgs struct {
	Team string `json:"team,omitempty" jsonschema:"Optional team name filter (case-insensitive substring; empty = all 20 teams)"`
}

func teamEnvHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, TeamEnvArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args TeamEnvArgs) (*mcp.CallToolResult, any, error) {
		var payload struct {
			Season string                    `json:"season"`
			Teams  map[string]map[string]any `json:"teams"`
		}
		path := filepath.Join(cfg.DerivedRoot, "ml/team_env.json")
		if err := readJSONFile(path, &payload); err != nil {
			return toolError(err), nil, nil
		}
		teams := payload.Teams
		if needle := strings.ToLower(strings.TrimSpace(args.Team)); needle != "" {
			teams = map[string]map[string]any{}
			for name, env := range payload.Teams {
				if strings.Contains(strings.ToLower(name), needle) {
					teams[name] = env
				}
			}
			if len(teams) == 0 {
				return toolError(fmt.Errorf("no team matches %q", args.Team)), nil, nil
			}
		}
		return toolMarshal(map[string]any{
			"season": payload.Season,
			"teams":  teams,
			"note":   "Match-environment rates per fixture: attack (pts/xG generated) vs defense (pts/xG conceded, by position and venue). High combined attack xG = shootout potential (players feast); two strong defenses = stalemate risk. Regenerate with: python -m backend.ml.teamenv",
		})
	}
}

// ---------------------------------------------------------------------------
// league_pulse
// ---------------------------------------------------------------------------

type LeaguePulseArgs struct {
	LeagueID     int    `json:"league_id" jsonschema:"Draft league id (required)"`
	Season       string `json:"season,omitempty" jsonschema:"Season (default: server default season)"`
	Transactions int    `json:"transactions,omitempty" jsonschema:"Recent transactions to include (default 10)"`
}

func leaguePulseHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, LeaguePulseArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args LeaguePulseArgs) (*mcp.CallToolResult, any, error) {
		if args.LeagueID == 0 {
			return toolError(fmt.Errorf("league_id is required")), nil, nil
		}
		rawDir := cfg.rawDir(args.Season)

		var details struct {
			LeagueEntries []struct {
				ID        int    `json:"id"`
				EntryID   int    `json:"entry_id"`
				EntryName string `json:"entry_name"`
			} `json:"league_entries"`
			Standings []struct {
				LeagueEntry   int `json:"league_entry"`
				Rank          any `json:"rank"`
				Total         int `json:"total"`
				PointsFor     int `json:"points_for"`
				PointsAgainst int `json:"points_against"`
				MatchesWon    int `json:"matches_won"`
				MatchesLost   int `json:"matches_lost"`
				MatchesDrawn  int `json:"matches_drawn"`
			} `json:"standings"`
		}
		detailsPath := filepath.Join(rawDir, fmt.Sprintf("league/%d/details.json", args.LeagueID))
		if err := readJSONFile(detailsPath, &details); err != nil {
			return toolError(err), nil, nil
		}
		nameByLeagueEntry := map[int]string{}
		nameByEntry := map[int]string{}
		for _, e := range details.LeagueEntries {
			nameByLeagueEntry[e.ID] = e.EntryName
			nameByEntry[e.EntryID] = e.EntryName
		}
		standings := make([]map[string]any, 0, len(details.Standings))
		for _, s := range details.Standings {
			standings = append(standings, map[string]any{
				"team": nameByLeagueEntry[s.LeagueEntry], "rank": s.Rank,
				"total": s.Total, "points_for": s.PointsFor,
				"points_against": s.PointsAgainst,
				"record":         fmt.Sprintf("%d-%d-%d", s.MatchesWon, s.MatchesDrawn, s.MatchesLost),
			})
		}

		// Player names for transaction readability (best effort).
		nameByElement := map[int]string{}
		var bootstrap struct {
			Elements []struct {
				ID      int    `json:"id"`
				WebName string `json:"web_name"`
			} `json:"elements"`
		}
		if err := readJSONFile(filepath.Join(rawDir, "bootstrap/bootstrap-static.json"), &bootstrap); err == nil {
			for _, el := range bootstrap.Elements {
				nameByElement[el.ID] = el.WebName
			}
		}

		limit := args.Transactions
		if limit <= 0 {
			limit = 10
		}
		var txFile struct {
			Transactions []struct {
				Added      string `json:"added"`
				ElementIn  int    `json:"element_in"`
				ElementOut int    `json:"element_out"`
				Entry      int    `json:"entry"`
				Event      int    `json:"event"`
				Kind       string `json:"kind"`
				Result     string `json:"result"`
			} `json:"transactions"`
		}
		transactions := []map[string]any{}
		txPath := filepath.Join(rawDir, fmt.Sprintf("league/%d/transactions.json", args.LeagueID))
		if err := readJSONFile(txPath, &txFile); err == nil {
			txs := txFile.Transactions
			sort.Slice(txs, func(i, j int) bool { return txs[i].Added > txs[j].Added })
			if len(txs) > limit {
				txs = txs[:limit]
			}
			for _, tx := range txs {
				transactions = append(transactions, map[string]any{
					"when": tx.Added, "team": nameByEntry[tx.Entry], "gw": tx.Event,
					"in": nameByElement[tx.ElementIn], "out": nameByElement[tx.ElementOut],
					"kind": tx.Kind, "result": tx.Result,
				})
			}
		}

		// Game meta: where the league clock stands (best effort).
		game := map[string]any{}
		_ = readJSONFile(filepath.Join(rawDir, "game/game.json"), &game)

		// The week's calendar: trades/waivers/lineup deadlines (best effort).
		deadlines, err := buildDeadlines(rawDir)
		if err != nil {
			deadlines = map[string]any{"error": err.Error()}
		}

		return toolMarshal(map[string]any{
			"standings":    standings,
			"transactions": transactions,
			"game":         game,
			"deadlines":    deadlines,
			"note":         "kind: w=waiver, f=free agent, t=trade; result: a=accepted, d=denied. Pair with drop_radar for who is newly on the wire.",
		})
	}
}
