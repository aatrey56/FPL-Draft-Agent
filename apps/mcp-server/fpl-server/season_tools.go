package main

// Season decision tools: trade_check and league_pulse.
//
// Same boundary as the rest of the decision layer — local JSON only:
//
//	trade_check  <- projections (season value) + season bootstrap (availability)
//	league_pulse <- league details + transactions + game meta + ownership events

import (
	"context"
	"fmt"
	"path/filepath"
	"sort"
	"strings"

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

// resolveProjection finds exactly one projected player by name. An exact
// (case-insensitive) web-name match wins outright — "Wood" must not be
// ambiguous with "Hinshelwood" — otherwise unique-substring semantics apply:
// ambiguous -> error, absent -> (nil, nil) so callers can handle unprojected
// players explicitly.
func resolveProjection(rows []projectionRow, name string) (*projectionRow, error) {
	needle := strings.ToLower(strings.TrimSpace(name))
	if needle == "" {
		return nil, fmt.Errorf("empty player name")
	}
	for i := range rows {
		if strings.ToLower(rows[i].WebName) == needle {
			return &rows[i], nil
		}
	}
	var match *projectionRow
	for i := range rows {
		if strings.Contains(strings.ToLower(rows[i].WebName), needle) {
			if match != nil {
				return nil, fmt.Errorf("ambiguous name %q (matches %s and %s) — be more specific",
					name, match.WebName, rows[i].WebName)
			}
			match = &rows[i]
		}
	}
	return match, nil
}

func tradeCheckHandler(cfg ServerConfig) func(context.Context, *mcp.CallToolRequest, TradeCheckArgs) (*mcp.CallToolResult, any, error) {
	return func(_ context.Context, _ *mcp.CallToolRequest, args TradeCheckArgs) (*mcp.CallToolResult, any, error) {
		if len(args.Give) == 0 || len(args.Get) == 0 {
			return toolError(fmt.Errorf("both give and get player lists are required")), nil, nil
		}
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
			"give":        map[string]any{"players": give, "ros_points": giveRos, "vor": giveVor},
			"get":         map[string]any{"players": get, "ros_points": getRos, "vor": getVor},
			"season_gain": getRos - giveRos,
			"vor_gain":    getVor - giveVor,
			"verdict":     verdict,
			"warnings":    append(giveWarn, getWarn...),
			"note":        "vor_gain weighs starter scarcity (12-team league) and beats raw season_gain for 2-for-1 trades: a bench player's points don't help you on game day. Unprojected players are NOT counted in totals — value them manually.",
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

		return toolMarshal(map[string]any{
			"standings":    standings,
			"transactions": transactions,
			"game":         game,
			"note":         "kind: w=waiver, f=free agent, t=trade; result: a=accepted, d=denied. Pair with drop_radar for who is newly on the wire.",
		})
	}
}
