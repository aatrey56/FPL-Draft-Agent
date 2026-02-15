package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/model"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/reconcile"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/summary"
)

type WaiverRecommendationsArgs struct {
	LeagueID       int      `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID        *int     `json:"entry_id,omitempty" jsonschema:"Entry id (required if entry_name not provided)"`
	EntryName      *string  `json:"entry_name,omitempty" jsonschema:"Entry name (if entry_id not provided)"`
	First          *string  `json:"first,omitempty" jsonschema:"First name (optional helper)"`
	Last           *string  `json:"last,omitempty" jsonschema:"Last name (optional helper)"`
	GW             *int     `json:"gw,omitempty" jsonschema:"Target gameweek for waivers (0 = next gameweek)"`
	Horizon        *int     `json:"horizon,omitempty" jsonschema:"Rolling horizon in GWs (default 5)"`
	WeightFixtures *float64 `json:"weight_fixtures,omitempty" jsonschema:"Weight for fixture score (default 0.35)"`
	WeightForm     *float64 `json:"weight_form,omitempty" jsonschema:"Weight for form score (default 0.25)"`
	WeightTotal    *float64 `json:"weight_total_points,omitempty" jsonschema:"Weight for total points (default 0.25)"`
	WeightXG       *float64 `json:"weight_xg,omitempty" jsonschema:"Weight for expected goals (default 0.15)"`
	Limit          *int     `json:"limit,omitempty" jsonschema:"How many add recommendations (default 5)"`
	UndroppableIDs *[]int   `json:"undroppable_ids,omitempty" jsonschema:"Element ids that should never be dropped"`
	TargetPosition *int     `json:"target_position,omitempty" jsonschema:"Position to target (1=GK,2=DEF,3=MID,4=FWD)"`
	TargetType     *string  `json:"target_type,omitempty" jsonschema:"overall|next_fixture|consistency (default overall)"`
	ConsistencyK   *float64 `json:"consistency_k,omitempty" jsonschema:"Penalty factor for consistency score (default 0.63)"`
}

type WaiverRecommendationsReport struct {
	LeagueID            int     `json:"league_id"`
	EntryID             int     `json:"entry_id"`
	AsOfGW              int     `json:"as_of_gw"`
	TargetGW            int     `json:"target_gw"`
	Horizon             int     `json:"horizon"`
	WeightFixtures      float64 `json:"weight_fixtures"`
	WeightForm          float64 `json:"weight_form"`
	WeightTotal         float64 `json:"weight_total_points"`
	WeightXG            float64 `json:"weight_xg"`
	FixtureSeasonWeight float64 `json:"fixture_season_weight"`
	FixtureRecentWeight float64 `json:"fixture_recent_weight"`
	ScoringFormula      string  `json:"scoring_formula"`
	TargetPosition      int     `json:"target_position,omitempty"`
	TargetType          string  `json:"target_type,omitempty"`
	ConsistencyK        float64 `json:"consistency_k"`
	Filters             struct {
		Minutes60Last3  int `json:"minutes_60_last3_required"`
		Minutes60Season int `json:"minutes_60_season_required"`
	} `json:"filters"`
	Adds            []AddRecommendation             `json:"top_adds"`
	Drops           []DropRecommendation            `json:"drop_candidates"`
	DropsByPosition map[string][]DropRecommendation `json:"drop_candidates_by_position,omitempty"`
	Warnings        []string                        `json:"warnings,omitempty"`
	Notes           []string                        `json:"notes"`
}

type ScoreComponents struct {
	FixturesRaw      float64 `json:"fixtures_raw"`
	FixturesSeason   float64 `json:"fixtures_season"`
	FixturesRecent   float64 `json:"fixtures_recent"`
	FormRaw          float64 `json:"form_raw"`
	TotalRaw         float64 `json:"total_raw"`
	XGRaw            float64 `json:"xg_raw"`
	AvgPoints        float64 `json:"avg_points"`
	StdDevPoints     float64 `json:"stddev_points"`
	ConsistencyScore float64 `json:"consistency_score"`
	FixturesNorm     float64 `json:"fixtures_norm"`
	FormNorm         float64 `json:"form_norm"`
	TotalNorm        float64 `json:"total_norm"`
	XGNorm           float64 `json:"xg_norm"`
	WeightedScore    float64 `json:"weighted_score"`
}

type FixtureContext struct {
	FixtureID     int    `json:"fixture_id,omitempty"`
	Event         int    `json:"event,omitempty"`
	TeamID        int    `json:"team_id,omitempty"`
	TeamShort     string `json:"team_short,omitempty"`
	OpponentID    int    `json:"opponent_id"`
	OpponentShort string `json:"opponent_short"`
	Venue         string `json:"venue"`
}

type AvailabilityInfo struct {
	Minutes60Last3  int `json:"minutes_60_last3"`
	Minutes60Season int `json:"minutes_60_season"`
}

type AddRecommendation struct {
	Element            int                 `json:"element"`
	Name               string              `json:"name"`
	Team               string              `json:"team"`
	PositionType       int                 `json:"position_type"`
	Fixture            FixtureContext      `json:"fixture"`
	Availability       AvailabilityInfo    `json:"availability"`
	Score              ScoreComponents     `json:"score"`
	PreviousOwners     []string            `json:"previous_owners,omitempty"`
	PreviousOwnerCount int                 `json:"previous_owner_count,omitempty"`
	SuggestedDrop      *DropRecommendation `json:"suggested_drop,omitempty"`
	Reasons            []string            `json:"reasons"`
}

type DropRecommendation struct {
	Element      int     `json:"element"`
	Name         string  `json:"name"`
	Team         string  `json:"team"`
	PositionType int     `json:"position_type"`
	Score        float64 `json:"score"`
	Reason       string  `json:"reason"`
}

type scoredPlayer struct {
	info         elementInfo
	score        ScoreComponents
	fixture      FixtureContext
	availability AvailabilityInfo
}

type elementInfo struct {
	ID           int
	Name         string
	TeamID       int
	PositionType int
	Status       string
	TotalPoints  int
}

type fixture struct {
	ID    int
	Event int
	TeamH int
	TeamA int
}

type liveStats struct {
	Minutes     int
	TotalPoints int
	XG          float64
}

func buildWaiverRecommendations(cfg ServerConfig, args WaiverRecommendationsArgs) ([]byte, error) {
	if args.LeagueID == 0 {
		return nil, fmt.Errorf("league_id is required")
	}

	entryID := 0
	if args.EntryID != nil {
		entryID = *args.EntryID
	}
	if entryID == 0 {
		name := ""
		if args.EntryName != nil {
			name = strings.TrimSpace(*args.EntryName)
		} else {
			first := ""
			last := ""
			if args.First != nil {
				first = strings.TrimSpace(*args.First)
			}
			if args.Last != nil {
				last = strings.TrimSpace(*args.Last)
			}
			name = strings.TrimSpace(strings.Join([]string{first, last}, " "))
		}
		if name == "" {
			return nil, fmt.Errorf("entry_id or entry_name is required")
		}
		st := store.NewJSONStore(cfg.RawRoot)
		ld, _, err := loadLeagueDetails(st, args.LeagueID)
		if err != nil {
			return nil, err
		}
		for _, e := range ld.LeagueEntries {
			if strings.EqualFold(e.EntryName, name) {
				entryID = e.EntryID
				break
			}
		}
		if entryID == 0 {
			return nil, fmt.Errorf("entry not found for name: %s", name)
		}
	}
	h := 0
	if args.Horizon != nil {
		h = *args.Horizon
	}
	if h <= 0 {
		h = 5
	}
	limit := 0
	if args.Limit != nil {
		limit = *args.Limit
	}
	if limit <= 0 {
		limit = 5
	}
	wFix := 0.0
	wForm := 0.0
	wTotal := 0.0
	wXG := 0.0
	if args.WeightFixtures != nil {
		wFix = *args.WeightFixtures
	}
	if args.WeightForm != nil {
		wForm = *args.WeightForm
	}
	if args.WeightTotal != nil {
		wTotal = *args.WeightTotal
	}
	if args.WeightXG != nil {
		wXG = *args.WeightXG
	}
	if wFix == 0 && wForm == 0 && wTotal == 0 && wXG == 0 {
		wFix, wForm, wTotal, wXG = 0.35, 0.25, 0.25, 0.15
	}
	weightSum := wFix + wForm + wTotal + wXG
	if weightSum == 0 {
		weightSum = 1
	}
	wFix /= weightSum
	wForm /= weightSum
	wTotal /= weightSum
	wXG /= weightSum

	consistencyK := 0.0
	if args.ConsistencyK != nil {
		consistencyK = *args.ConsistencyK
	}
	if consistencyK == 0 {
		consistencyK = 0.63
	}

	targetType := ""
	if args.TargetType != nil {
		targetType = *args.TargetType
	}
	targetType = strings.TrimSpace(strings.ToLower(targetType))
	if targetType == "" {
		targetType = "overall"
	}
	if targetType != "overall" && targetType != "next_fixture" && targetType != "consistency" {
		targetType = "overall"
	}

	targetPosition := 0
	if args.TargetPosition != nil {
		targetPosition = *args.TargetPosition
	}
	if targetPosition < 0 || targetPosition > 4 {
		targetPosition = 0
	}

	nextGWArg := 0
	if args.GW != nil {
		nextGWArg = *args.GW
	}
	asOfGW, nextGW, err := resolveAsOfAndNextGW(cfg, 0, nextGWArg)
	if err != nil {
		return nil, err
	}
	targetGW := nextGW

	bootstrap, teamShort, fixturesByGW, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return nil, err
	}
	fixtureByTeam := buildFixtureIndex(fixturesByGW[targetGW], teamShort)

	owned, roster, err := buildOwnershipAndRoster(cfg, args.LeagueID, entryID, asOfGW, bootstrap, teamShort)
	if err != nil {
		return nil, err
	}

	formSummary, err := loadPlayerFormSummary(cfg, args.LeagueID, asOfGW, h)
	if err != nil {
		return nil, err
	}
	formByElement := make(map[int]summary.PlayerForm, len(formSummary.Players))
	for _, p := range formSummary.Players {
		formByElement[p.Element] = p
	}

	seasonMinutes60, last3Minutes60, xgByElement, err := computeAvailabilityAndXG(cfg.RawRoot, bootstrap, asOfGW, h)
	if err != nil {
		return nil, err
	}

	avgPtsByElement, stddevPtsByElement, err := computeConsistencyStats(cfg.RawRoot, bootstrap, asOfGW, h)
	if err != nil {
		return nil, err
	}

	seasonWeight, recentWeight := horizonWeights(h)
	concededSeason := computePointsConcededByPosition(cfg.RawRoot, bootstrap, fixturesByGW, asOfGW, asOfGW)
	concededRecent := computePointsConcededByPosition(cfg.RawRoot, bootstrap, fixturesByGW, asOfGW, h)

	everOwnersByElement, err := buildEverOwners(cfg, args.LeagueID)
	if err != nil {
		return nil, err
	}

	undroppable := make(map[int]bool)
	if args.UndroppableIDs != nil {
		for _, id := range *args.UndroppableIDs {
			if id > 0 {
				undroppable[id] = true
			}
		}
	}

	candidates := make([]scoredPlayer, 0)
	for _, info := range bootstrap {
		if info.PositionType == 0 {
			continue
		}
		if targetPosition != 0 && info.PositionType != targetPosition {
			continue
		}
		if info.Status != "a" {
			continue
		}
		if owned[info.ID] {
			continue
		}
		last3 := last3Minutes60[info.ID]
		season := seasonMinutes60[info.ID]
		if last3 < 3 && season < 10 {
			continue
		}
		fixtureCtx, ok := fixtureByTeam[info.TeamID]
		if !ok {
			continue
		}
		seasonScore, recentScore, blended := blendedFixtureScore(concededSeason, concededRecent, fixtureCtx.OpponentID, fixtureCtx.Venue, info.PositionType, seasonWeight, recentWeight)
		form := formByElement[info.ID]
		xg := xgByElement[info.ID]
		avgPts := avgPtsByElement[info.ID]
		stddev := stddevPtsByElement[info.ID]
		consistency := avgPts - consistencyK*stddev
		candidates = append(candidates, scoredPlayer{
			info:    info,
			fixture: fixtureCtx,
			availability: AvailabilityInfo{
				Minutes60Last3:  last3,
				Minutes60Season: season,
			},
			score: ScoreComponents{
				FixturesRaw:      blended,
				FixturesSeason:   seasonScore,
				FixturesRecent:   recentScore,
				FormRaw:          form.PointsPerGW,
				TotalRaw:         float64(info.TotalPoints),
				XGRaw:            xg,
				AvgPoints:        avgPts,
				StdDevPoints:     stddev,
				ConsistencyScore: consistency,
			},
		})
	}

	minmax := normalizeScores(candidates)
	for i := range candidates {
		candidates[i].score.WeightedScore =
			wFix*candidates[i].score.FixturesNorm +
				wForm*candidates[i].score.FormNorm +
				wTotal*candidates[i].score.TotalNorm +
				wXG*candidates[i].score.XGNorm
	}
	sort.Slice(candidates, func(i, j int) bool {
		switch targetType {
		case "next_fixture":
			if candidates[i].score.FixturesRaw != candidates[j].score.FixturesRaw {
				return candidates[i].score.FixturesRaw > candidates[j].score.FixturesRaw
			}
			return candidates[i].score.WeightedScore > candidates[j].score.WeightedScore
		case "consistency":
			if candidates[i].score.ConsistencyScore != candidates[j].score.ConsistencyScore {
				return candidates[i].score.ConsistencyScore > candidates[j].score.ConsistencyScore
			}
			return candidates[i].score.WeightedScore > candidates[j].score.WeightedScore
		default:
			return candidates[i].score.WeightedScore > candidates[j].score.WeightedScore
		}
	})
	if len(candidates) > limit {
		candidates = candidates[:limit]
	}

	rosterScored := scoreRoster(bootstrap, teamShort, formByElement, xgByElement, fixtureByTeam, roster, concededSeason, concededRecent, seasonWeight, recentWeight, minmax, wFix, wForm, wTotal, wXG)
	dropsByPos, warnings := pickDropCandidatesByPosition(rosterScored, undroppable, candidates, targetPosition)
	dropCandidates := flattenDrops(dropsByPos)

	adds := make([]AddRecommendation, 0, len(candidates))
	for _, c := range candidates {
		reasons := []string{
			fmt.Sprintf("fixture score %.2f vs %s (%s)", c.score.FixturesRaw, c.fixture.OpponentShort, strings.ToLower(c.fixture.Venue)),
			fmt.Sprintf("form %.2f pts/GW", c.score.FormRaw),
			fmt.Sprintf("season points %.0f", c.score.TotalRaw),
			fmt.Sprintf("xG %.2f", c.score.XGRaw),
		}
		prevOwners := everOwnersByElement[c.info.ID]
		add := AddRecommendation{
			Element:            c.info.ID,
			Name:               c.info.Name,
			Team:               teamShort[c.info.TeamID],
			PositionType:       c.info.PositionType,
			Fixture:            c.fixture,
			Availability:       c.availability,
			Score:              c.score,
			PreviousOwners:     prevOwners,
			PreviousOwnerCount: len(prevOwners),
			Reasons:            reasons,
		}
		if drop := bestDropForPosition(dropsByPos, c.info.PositionType, c.score.WeightedScore); drop != nil {
			add.SuggestedDrop = drop
		}
		adds = append(adds, add)
	}

	report := WaiverRecommendationsReport{
		LeagueID:            args.LeagueID,
		EntryID:             entryID,
		AsOfGW:              asOfGW,
		TargetGW:            targetGW,
		Horizon:             h,
		WeightFixtures:      wFix,
		WeightForm:          wForm,
		WeightTotal:         wTotal,
		WeightXG:            wXG,
		FixtureSeasonWeight: seasonWeight,
		FixtureRecentWeight: recentWeight,
		ScoringFormula:      "weighted_score = w_fix*fixture_norm + w_form*form_norm + w_total*total_norm + w_xg*xg_norm (each norm is min-max across the candidate pool)",
		Adds:                adds,
		Drops:               dropCandidates,
		DropsByPosition:     dropsByPos,
		Warnings:            warnings,
		Notes: []string{
			"Uses unrostered pool only, status=available (status 'a').",
			"Eligibility: 60+ mins in each of last 3 GWs OR 60+ mins in at least 10 GWs this season.",
			"Fixture score uses opponent points conceded by position, split home/away, blended season and recent horizon.",
		},
	}
	report.Filters.Minutes60Last3 = 3
	report.Filters.Minutes60Season = 10
	report.TargetPosition = targetPosition
	report.TargetType = targetType
	report.ConsistencyK = consistencyK

	return json.MarshalIndent(report, "", "  ")
}

func loadLeagueSummary(cfg ServerConfig, leagueID int, gw int) (summary.LeagueWeekSummary, error) {
	relPath := fmt.Sprintf("summary/league/%d/gw/%d.json", leagueID, gw)
	raw, err := loadSummaryFile(cfg, leagueID, gw, relPath, nil, nil)
	if err != nil {
		return summary.LeagueWeekSummary{}, err
	}
	var out summary.LeagueWeekSummary
	if err := json.Unmarshal(raw, &out); err != nil {
		return summary.LeagueWeekSummary{}, err
	}
	return out, nil
}

func loadPlayerFormSummary(cfg ServerConfig, leagueID int, gw int, horizon int) (summary.PlayerFormSummary, error) {
	relPath := fmt.Sprintf("summary/player_form/%d/h%d.json", leagueID, horizon)
	raw, err := loadSummaryFile(cfg, leagueID, gw, relPath, []int{horizon}, []string{"low", "med", "high"})
	if err != nil {
		return summary.PlayerFormSummary{}, err
	}
	var out summary.PlayerFormSummary
	if err := json.Unmarshal(raw, &out); err != nil {
		return summary.PlayerFormSummary{}, err
	}
	return out, nil
}

func buildOwnershipAndRoster(cfg ServerConfig, leagueID int, entryID int, asOfGW int, elements []elementInfo, teamShort map[int]string) (map[int]bool, []summary.RosterPlayer, error) {
	st := store.NewJSONStore(cfg.RawRoot)
	if err := ensureLedger(st, cfg.DerivedRoot, leagueID); err != nil {
		return nil, nil, err
	}
	ledgerPath := filepath.Join(cfg.DerivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	raw, err := os.ReadFile(ledgerPath)
	if err != nil {
		return nil, nil, err
	}
	var ledgerOut model.DraftLedger
	if err := json.Unmarshal(raw, &ledgerOut); err != nil {
		return nil, nil, err
	}
	transactions, err := loadTransactionsRaw(st, leagueID)
	if err != nil {
		return nil, nil, err
	}
	trades, err := loadTradesRaw(st, leagueID)
	if err != nil {
		return nil, nil, err
	}

	ownership := reconcile.BuildOwnershipMapAtGW(&ledgerOut, transactions, trades, asOfGW)
	owned := make(map[int]bool)
	for _, roster := range ownership {
		for elementID := range roster {
			owned[elementID] = true
		}
	}

	elementByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		elementByID[e.ID] = e
	}

	entryRoster := ownership[entryID]
	roster := make([]summary.RosterPlayer, 0, len(entryRoster))
	for elementID := range entryRoster {
		info := elementByID[elementID]
		if info.ID == 0 {
			continue
		}
		roster = append(roster, summary.RosterPlayer{
			Element:      info.ID,
			Name:         info.Name,
			Team:         teamShort[info.TeamID],
			PositionType: info.PositionType,
		})
	}
	sort.Slice(roster, func(i, j int) bool {
		if roster[i].PositionType != roster[j].PositionType {
			return roster[i].PositionType < roster[j].PositionType
		}
		if roster[i].Team != roster[j].Team {
			return roster[i].Team < roster[j].Team
		}
		return roster[i].Name < roster[j].Name
	})
	return owned, roster, nil
}

func buildEverOwners(cfg ServerConfig, leagueID int) (map[int][]string, error) {
	st := store.NewJSONStore(cfg.RawRoot)
	ld, _, err := loadLeagueDetails(st, leagueID)
	if err != nil {
		return nil, err
	}
	entryNameByID := make(map[int]string, len(ld.LeagueEntries))
	for _, e := range ld.LeagueEntries {
		entryNameByID[e.EntryID] = e.EntryName
	}

	if err := ensureLedger(st, cfg.DerivedRoot, leagueID); err != nil {
		return nil, err
	}
	ledgerPath := filepath.Join(cfg.DerivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	raw, err := os.ReadFile(ledgerPath)
	if err != nil {
		return nil, err
	}
	var ledgerOut model.DraftLedger
	if err := json.Unmarshal(raw, &ledgerOut); err != nil {
		return nil, err
	}

	transactions, err := loadTransactionsRaw(st, leagueID)
	if err != nil {
		return nil, err
	}
	trades, err := loadTradesRaw(st, leagueID)
	if err != nil {
		return nil, err
	}

	ever := make(map[int]map[int]bool)
	addOwner := func(elementID int, entryID int) {
		if elementID == 0 || entryID == 0 {
			return
		}
		if _, ok := ever[elementID]; !ok {
			ever[elementID] = make(map[int]bool)
		}
		ever[elementID][entryID] = true
	}

	for _, squad := range ledgerOut.Squads {
		for _, pid := range squad.PlayerIDs {
			addOwner(pid, squad.EntryID)
		}
	}

	for _, tx := range transactions {
		if tx.Result != "a" {
			continue
		}
		if tx.ElementIn != 0 {
			addOwner(tx.ElementIn, tx.Entry)
		}
		if tx.ElementOut != 0 {
			addOwner(tx.ElementOut, tx.Entry)
		}
	}

	for _, tr := range trades {
		if tr.State != "p" {
			continue
		}
		for _, item := range tr.TradeItems {
			if item.ElementOut != 0 {
				addOwner(item.ElementOut, tr.OfferedEntry)
				addOwner(item.ElementOut, tr.ReceivedEntry)
			}
			if item.ElementIn != 0 {
				addOwner(item.ElementIn, tr.ReceivedEntry)
				addOwner(item.ElementIn, tr.OfferedEntry)
			}
		}
	}

	out := make(map[int][]string, len(ever))
	for elementID, owners := range ever {
		names := make([]string, 0, len(owners))
		for entryID := range owners {
			if name, ok := entryNameByID[entryID]; ok && name != "" {
				names = append(names, name)
			}
		}
		sort.Strings(names)
		out[elementID] = names
	}

	return out, nil
}

func loadBootstrapData(rawRoot string) ([]elementInfo, map[int]string, map[int][]fixture, error) {
	path := filepath.Join(rawRoot, "bootstrap", "bootstrap-static.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, nil, nil, err
	}
	var resp struct {
		Elements []struct {
			ID          int    `json:"id"`
			WebName     string `json:"web_name"`
			Team        int    `json:"team"`
			ElementType int    `json:"element_type"`
			Status      string `json:"status"`
			TotalPoints int    `json:"total_points"`
		} `json:"elements"`
		Teams []struct {
			ID        int    `json:"id"`
			ShortName string `json:"short_name"`
		} `json:"teams"`
		Fixtures map[string][]struct {
			ID    int `json:"id"`
			Event int `json:"event"`
			TeamH int `json:"team_h"`
			TeamA int `json:"team_a"`
		} `json:"fixtures"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, nil, nil, err
	}

	teams := make(map[int]string, len(resp.Teams))
	for _, t := range resp.Teams {
		teams[t.ID] = t.ShortName
	}

	elements := make([]elementInfo, 0, len(resp.Elements))
	for _, e := range resp.Elements {
		elements = append(elements, elementInfo{
			ID:           e.ID,
			Name:         e.WebName,
			TeamID:       e.Team,
			PositionType: e.ElementType,
			Status:       e.Status,
			TotalPoints:  e.TotalPoints,
		})
	}

	fixtures := make(map[int][]fixture)
	for k, list := range resp.Fixtures {
		gw, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		for _, f := range list {
			fixtures[gw] = append(fixtures[gw], fixture{
				ID:    f.ID,
				Event: gw,
				TeamH: f.TeamH,
				TeamA: f.TeamA,
			})
		}
	}
	return elements, teams, fixtures, nil
}

func loadTransactionsRaw(st *store.JSONStore, leagueID int) ([]reconcile.Transaction, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/transactions.json", leagueID))
	if err != nil {
		return nil, err
	}
	var resp reconcile.TransactionsResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}
	return resp.Transactions, nil
}

func loadTradesRaw(st *store.JSONStore, leagueID int) ([]reconcile.Trade, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/trades.json", leagueID))
	if err != nil {
		return nil, err
	}
	var resp reconcile.TradesResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}
	return resp.Trades, nil
}

func buildFixtureIndex(fixtures []fixture, teamShort map[int]string) map[int]FixtureContext {
	out := make(map[int]FixtureContext)
	for _, f := range fixtures {
		out[f.TeamH] = FixtureContext{
			FixtureID:     f.ID,
			Event:         f.Event,
			TeamID:        f.TeamH,
			TeamShort:     teamShort[f.TeamH],
			OpponentID:    f.TeamA,
			OpponentShort: teamShort[f.TeamA],
			Venue:         "HOME",
		}
		out[f.TeamA] = FixtureContext{
			FixtureID:     f.ID,
			Event:         f.Event,
			TeamID:        f.TeamA,
			TeamShort:     teamShort[f.TeamA],
			OpponentID:    f.TeamH,
			OpponentShort: teamShort[f.TeamH],
			Venue:         "AWAY",
		}
	}
	return out
}

func computeAvailabilityAndXG(rawRoot string, elements []elementInfo, asOfGW int, horizon int) (map[int]int, map[int]int, map[int]float64, error) {
	season60 := make(map[int]int)
	last3 := make(map[int]int)
	xg := make(map[int]float64)
	xgMinutes := make(map[int]int)

	startH := asOfGW - horizon + 1
	if startH < 1 {
		startH = 1
	}
	for gw := 1; gw <= asOfGW; gw++ {
		live, err := loadLiveStats(rawRoot, gw)
		if err != nil {
			continue
		}
		for id, stats := range live {
			if stats.Minutes >= 60 {
				season60[id]++
				if gw >= asOfGW-2 {
					last3[id]++
				}
			}
			if gw >= startH {
				xg[id] += stats.XG
				xgMinutes[id] += stats.Minutes
			}
		}
	}
	for id, mins := range xgMinutes {
		if mins > 0 {
			xg[id] = (xg[id] / float64(mins)) * 90
		}
	}
	return season60, last3, xg, nil
}

func computeConsistencyStats(rawRoot string, elements []elementInfo, asOfGW int, horizon int) (map[int]float64, map[int]float64, error) {
	if asOfGW < 1 {
		return map[int]float64{}, map[int]float64{}, nil
	}
	start := asOfGW - horizon + 1
	if start < 1 {
		start = 1
	}

	type agg struct {
		sum   float64
		sumSq float64
		count float64
	}

	stats := make(map[int]*agg, len(elements))
	for _, e := range elements {
		stats[e.ID] = &agg{}
	}

	for gw := start; gw <= asOfGW; gw++ {
		live, err := loadLiveStats(rawRoot, gw)
		if err != nil {
			continue
		}
		for _, e := range elements {
			points := 0.0
			if s, ok := live[e.ID]; ok {
				points = float64(s.TotalPoints)
			}
			cur := stats[e.ID]
			cur.sum += points
			cur.sumSq += points * points
			cur.count++
		}
	}

	avg := make(map[int]float64, len(elements))
	stddev := make(map[int]float64, len(elements))
	for _, e := range elements {
		cur := stats[e.ID]
		if cur.count == 0 {
			continue
		}
		mean := cur.sum / cur.count
		variance := (cur.sumSq / cur.count) - (mean * mean)
		if variance < 0 {
			variance = 0
		}
		avg[e.ID] = mean
		stddev[e.ID] = math.Sqrt(variance)
	}
	return avg, stddev, nil
}

func loadLiveStats(rawRoot string, gw int) (map[int]liveStats, error) {
	path := filepath.Join(rawRoot, "gw", strconv.Itoa(gw), "live.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var resp struct {
		Elements map[string]struct {
			Stats map[string]any `json:"stats"`
		} `json:"elements"`
	}
	if err := dec.Decode(&resp); err != nil {
		return nil, err
	}
	out := make(map[int]liveStats, len(resp.Elements))
	for k, v := range resp.Elements {
		id, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		minutes := int(asNumber(v.Stats["minutes"]))
		total := int(asNumber(v.Stats["total_points"]))
		xg := asFloat(v.Stats["expected_goals"])
		out[id] = liveStats{
			Minutes:     minutes,
			TotalPoints: total,
			XG:          xg,
		}
	}
	return out, nil
}

func asNumber(v any) float64 {
	switch t := v.(type) {
	case json.Number:
		f, _ := t.Float64()
		return f
	case float64:
		return t
	case string:
		f, _ := strconv.ParseFloat(t, 64)
		return f
	default:
		return 0
	}
}

func asFloat(v any) float64 {
	return asNumber(v)
}

func computePointsConcededByPosition(rawRoot string, elements []elementInfo, fixturesByGW map[int][]fixture, asOfGW int, horizon int) map[int]map[string]map[int]avgStat {
	elementTeam := make(map[int]int, len(elements))
	elementPos := make(map[int]int, len(elements))
	for _, e := range elements {
		elementTeam[e.ID] = e.TeamID
		elementPos[e.ID] = e.PositionType
	}

	start := asOfGW - horizon + 1
	if start < 1 {
		start = 1
	}
	conceded := make(map[int]map[string]map[int]avgStat)
	for gw := start; gw <= asOfGW; gw++ {
		live, err := loadLiveStats(rawRoot, gw)
		if err != nil {
			continue
		}
		pointsByTeamPos := make(map[int]map[int]int)
		for id, stats := range live {
			team := elementTeam[id]
			pos := elementPos[id]
			if team == 0 || pos == 0 {
				continue
			}
			if _, ok := pointsByTeamPos[team]; !ok {
				pointsByTeamPos[team] = make(map[int]int)
			}
			pointsByTeamPos[team][pos] += stats.TotalPoints
		}

		for _, f := range fixturesByGW[gw] {
			home := f.TeamH
			away := f.TeamA
			homePts := pointsByTeamPos[home]
			awayPts := pointsByTeamPos[away]

			for pos, pts := range awayPts {
				addConceded(conceded, home, "HOME", pos, float64(pts))
			}
			for pos, pts := range homePts {
				addConceded(conceded, away, "AWAY", pos, float64(pts))
			}
		}
	}
	return conceded
}

type avgStat struct {
	Sum   float64
	Count int
}

func addConceded(store map[int]map[string]map[int]avgStat, teamID int, venue string, pos int, val float64) {
	if _, ok := store[teamID]; !ok {
		store[teamID] = map[string]map[int]avgStat{"HOME": {}, "AWAY": {}}
	}
	cur := store[teamID][venue][pos]
	cur.Sum += val
	cur.Count++
	store[teamID][venue][pos] = cur
}

func fixtureDifficulty(conceded map[int]map[string]map[int]avgStat, opponentID int, venue string, pos int) float64 {
	if opponentID == 0 {
		return 0
	}
	venue = strings.ToUpper(venue)
	stats := conceded[opponentID][venue][pos]
	if stats.Count > 0 {
		return stats.Sum / float64(stats.Count)
	}
	// fallback to overall (both venues)
	home := conceded[opponentID]["HOME"][pos]
	away := conceded[opponentID]["AWAY"][pos]
	totalSum := home.Sum + away.Sum
	totalCount := home.Count + away.Count
	if totalCount == 0 {
		return 0
	}
	return totalSum / float64(totalCount)
}

type scoreMinMax struct {
	FixMin, FixMax     float64
	FormMin, FormMax   float64
	TotalMin, TotalMax float64
	XGMin, XGMax       float64
}

func normalizeScores(players []scoredPlayer) scoreMinMax {
	var minFix, maxFix = math.Inf(1), math.Inf(-1)
	var minForm, maxForm = math.Inf(1), math.Inf(-1)
	var minTotal, maxTotal = math.Inf(1), math.Inf(-1)
	var minXG, maxXG = math.Inf(1), math.Inf(-1)
	for _, p := range players {
		minFix = math.Min(minFix, p.score.FixturesRaw)
		maxFix = math.Max(maxFix, p.score.FixturesRaw)
		minForm = math.Min(minForm, p.score.FormRaw)
		maxForm = math.Max(maxForm, p.score.FormRaw)
		minTotal = math.Min(minTotal, p.score.TotalRaw)
		maxTotal = math.Max(maxTotal, p.score.TotalRaw)
		minXG = math.Min(minXG, p.score.XGRaw)
		maxXG = math.Max(maxXG, p.score.XGRaw)
	}
	for i := range players {
		players[i].score.FixturesNorm = minMax(players[i].score.FixturesRaw, minFix, maxFix)
		players[i].score.FormNorm = minMax(players[i].score.FormRaw, minForm, maxForm)
		players[i].score.TotalNorm = minMax(players[i].score.TotalRaw, minTotal, maxTotal)
		players[i].score.XGNorm = minMax(players[i].score.XGRaw, minXG, maxXG)
	}
	return scoreMinMax{
		FixMin: minFix, FixMax: maxFix,
		FormMin: minForm, FormMax: maxForm,
		TotalMin: minTotal, TotalMax: maxTotal,
		XGMin: minXG, XGMax: maxXG,
	}
}

func minMax(v, min, max float64) float64 {
	if math.IsInf(min, 1) || math.IsInf(max, -1) || min == max {
		return 0
	}
	return (v - min) / (max - min)
}

func scoreRoster(elements []elementInfo, teamShort map[int]string, form map[int]summary.PlayerForm, xg map[int]float64, fixtures map[int]FixtureContext, roster []summary.RosterPlayer, concededSeason map[int]map[string]map[int]avgStat, concededRecent map[int]map[string]map[int]avgStat, seasonWeight float64, recentWeight float64, minmax scoreMinMax, wFix, wForm, wTotal, wXG float64) []DropRecommendation {
	elementByID := make(map[int]elementInfo, len(elements))
	for _, e := range elements {
		elementByID[e.ID] = e
	}
	drops := make([]DropRecommendation, 0)
	for _, r := range roster {
		info := elementByID[r.Element]
		if info.ID == 0 {
			continue
		}
		fx, ok := fixtures[info.TeamID]
		if !ok {
			continue
		}
		_, _, blended := blendedFixtureScore(concededSeason, concededRecent, fx.OpponentID, fx.Venue, info.PositionType, seasonWeight, recentWeight)
		formScore := form[info.ID].PointsPerGW
		totalScore := float64(info.TotalPoints)
		xgScore := xg[info.ID]
		weighted := wFix*minMax(blended, minmax.FixMin, minmax.FixMax) +
			wForm*minMax(formScore, minmax.FormMin, minmax.FormMax) +
			wTotal*minMax(totalScore, minmax.TotalMin, minmax.TotalMax) +
			wXG*minMax(xgScore, minmax.XGMin, minmax.XGMax)
		drops = append(drops, DropRecommendation{
			Element:      info.ID,
			Name:         info.Name,
			Team:         teamShort[info.TeamID],
			PositionType: info.PositionType,
			Score:        weighted,
		})
	}
	sort.Slice(drops, func(i, j int) bool {
		return drops[i].Score < drops[j].Score
	})
	return drops
}

func pickDropCandidatesByPosition(drops []DropRecommendation, undroppable map[int]bool, adds []scoredPlayer, targetPos int) (map[string][]DropRecommendation, []string) {
	bestAddByPos := make(map[int]float64)
	for _, a := range adds {
		if a.score.WeightedScore > bestAddByPos[a.info.PositionType] {
			bestAddByPos[a.info.PositionType] = a.score.WeightedScore
		}
	}

	byPos := make(map[string][]DropRecommendation)
	warnings := make([]string, 0)
	totalDroppable := 0

	for pos := 1; pos <= 4; pos++ {
		posLabel := positionLabel(pos)
		posDrops := make([]DropRecommendation, 0)
		for _, d := range drops {
			if d.PositionType != pos {
				continue
			}
			if undroppable[d.Element] {
				continue
			}
			posDrops = append(posDrops, d)
		}

		if len(posDrops) == 0 {
			if targetPos == 0 || targetPos == pos {
				warnings = append(warnings, fmt.Sprintf("All %s are undroppable. Make someone droppable if you want recommendations for this position.", posLabel))
			}
			continue
		}

		sort.Slice(posDrops, func(i, j int) bool {
			return posDrops[i].Score < posDrops[j].Score
		})
		totalDroppable += len(posDrops)

		pick := posDrops[0]
		if pos == 1 {
			bestAdd := bestAddByPos[pos]
			if bestAdd <= pick.Score {
				if targetPos == 0 || targetPos == pos {
					warnings = append(warnings, "No better GK add available; GK drops omitted.")
				}
				continue
			}
		}

		pick.Reason = "Lowest weighted score at position"
		byPos[posLabel] = []DropRecommendation{pick}
	}

	if totalDroppable == 0 {
		warnings = append(warnings, "All players are undroppable. Make someone droppable if you want recommendations.")
	}

	return byPos, warnings
}

func flattenDrops(byPos map[string][]DropRecommendation) []DropRecommendation {
	order := []string{"GK", "DEF", "MID", "FWD"}
	out := make([]DropRecommendation, 0)
	for _, pos := range order {
		if list, ok := byPos[pos]; ok {
			out = append(out, list...)
		}
	}
	return out
}

func bestDropForPosition(dropsByPos map[string][]DropRecommendation, pos int, addScore float64) *DropRecommendation {
	label := positionLabel(pos)
	list := dropsByPos[label]
	if len(list) == 0 {
		return nil
	}
	d := list[0]
	if addScore <= d.Score {
		return nil
	}
	out := d
	out.Reason = "Lowest weighted score at position"
	return &out
}
