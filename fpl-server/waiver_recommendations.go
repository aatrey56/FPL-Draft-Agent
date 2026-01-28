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

	"fpl-draft-mcp/internal/summary"
)

type WaiverRecommendationsArgs struct {
	LeagueID       int     `json:"league_id" jsonschema:"Draft league id (required)"`
	EntryID        int     `json:"entry_id" jsonschema:"Entry id (required)"`
	GW             int     `json:"gw" jsonschema:"Target gameweek for waivers (0 = next gameweek)"`
	Horizon        int     `json:"horizon" jsonschema:"Rolling horizon in GWs (default 5)"`
	WeightFixtures float64 `json:"weight_fixtures" jsonschema:"Weight for fixture score (default 0.35)"`
	WeightForm     float64 `json:"weight_form" jsonschema:"Weight for form score (default 0.25)"`
	WeightTotal    float64 `json:"weight_total_points" jsonschema:"Weight for total points (default 0.25)"`
	WeightXG       float64 `json:"weight_xg" jsonschema:"Weight for expected goals (default 0.15)"`
	Limit          int     `json:"limit" jsonschema:"How many add recommendations (default 5)"`
}

type WaiverRecommendationsReport struct {
	LeagueID       int     `json:"league_id"`
	EntryID        int     `json:"entry_id"`
	AsOfGW         int     `json:"as_of_gw"`
	TargetGW       int     `json:"target_gw"`
	Horizon        int     `json:"horizon"`
	WeightFixtures float64 `json:"weight_fixtures"`
	WeightForm     float64 `json:"weight_form"`
	WeightTotal    float64 `json:"weight_total_points"`
	WeightXG       float64 `json:"weight_xg"`
	Filters        struct {
		Minutes60Last3  int `json:"minutes_60_last3_required"`
		Minutes60Season int `json:"minutes_60_season_required"`
	} `json:"filters"`
	Adds  []AddRecommendation  `json:"top_adds"`
	Drops []DropRecommendation `json:"drop_candidates"`
	Notes []string             `json:"notes"`
}

type ScoreComponents struct {
	FixturesRaw   float64 `json:"fixtures_raw"`
	FormRaw       float64 `json:"form_raw"`
	TotalRaw      float64 `json:"total_raw"`
	XGRaw         float64 `json:"xg_raw"`
	FixturesNorm  float64 `json:"fixtures_norm"`
	FormNorm      float64 `json:"form_norm"`
	TotalNorm     float64 `json:"total_norm"`
	XGNorm        float64 `json:"xg_norm"`
	WeightedScore float64 `json:"weighted_score"`
}

type FixtureContext struct {
	OpponentID    int    `json:"opponent_id"`
	OpponentShort string `json:"opponent_short"`
	Venue         string `json:"venue"`
}

type AvailabilityInfo struct {
	Minutes60Last3  int `json:"minutes_60_last3"`
	Minutes60Season int `json:"minutes_60_season"`
}

type AddRecommendation struct {
	Element       int                 `json:"element"`
	Name          string              `json:"name"`
	Team          string              `json:"team"`
	PositionType  int                 `json:"position_type"`
	Fixture       FixtureContext      `json:"fixture"`
	Availability  AvailabilityInfo    `json:"availability"`
	Score         ScoreComponents     `json:"score"`
	SuggestedDrop *DropRecommendation `json:"suggested_drop,omitempty"`
	Reasons       []string            `json:"reasons"`
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
	if args.LeagueID == 0 || args.EntryID == 0 {
		return nil, fmt.Errorf("league_id and entry_id are required")
	}
	h := args.Horizon
	if h <= 0 {
		h = 5
	}
	limit := args.Limit
	if limit <= 0 {
		limit = 5
	}
	wFix := args.WeightFixtures
	wForm := args.WeightForm
	wTotal := args.WeightTotal
	wXG := args.WeightXG
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

	currentGW, err := resolveGW(cfg, 0)
	if err != nil {
		return nil, err
	}
	targetGW := args.GW
	if targetGW <= 0 {
		targetGW = currentGW + 1
	}
	asOfGW := targetGW - 1
	if asOfGW < 1 {
		asOfGW = 1
	}

	bootstrap, teamShort, fixturesByGW, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return nil, err
	}
	fixtureByTeam := buildFixtureIndex(fixturesByGW[targetGW], teamShort)

	leagueSummary, err := loadLeagueSummary(cfg, args.LeagueID, asOfGW)
	if err != nil {
		return nil, err
	}
	owned, roster := buildOwnershipAndRoster(leagueSummary, args.EntryID)

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

	conceded := computePointsConcededByPosition(cfg.RawRoot, bootstrap, fixturesByGW, asOfGW, h)

	candidates := make([]scoredPlayer, 0)
	for _, info := range bootstrap {
		if info.PositionType == 0 {
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
		fixtureScore := fixtureDifficulty(conceded, fixtureCtx.OpponentID, fixtureCtx.Venue, info.PositionType)
		form := formByElement[info.ID]
		xg := xgByElement[info.ID]
		candidates = append(candidates, scoredPlayer{
			info:    info,
			fixture: fixtureCtx,
			availability: AvailabilityInfo{
				Minutes60Last3:  last3,
				Minutes60Season: season,
			},
			score: ScoreComponents{
				FixturesRaw: fixtureScore,
				FormRaw:     form.PointsPerGW,
				TotalRaw:    float64(info.TotalPoints),
				XGRaw:       xg,
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
		return candidates[i].score.WeightedScore > candidates[j].score.WeightedScore
	})
	if len(candidates) > limit {
		candidates = candidates[:limit]
	}

	rosterScored := scoreRoster(bootstrap, teamShort, formByElement, xgByElement, fixtureByTeam, roster, conceded, minmax, wFix, wForm, wTotal, wXG)
	dropCandidates := pickDropCandidates(rosterScored)

	adds := make([]AddRecommendation, 0, len(candidates))
	for _, c := range candidates {
		reasons := []string{
			fmt.Sprintf("fixture score %.2f vs %s (%s)", c.score.FixturesRaw, c.fixture.OpponentShort, strings.ToLower(c.fixture.Venue)),
			fmt.Sprintf("form %.2f pts/GW", c.score.FormRaw),
			fmt.Sprintf("season points %.0f", c.score.TotalRaw),
			fmt.Sprintf("xG %.2f", c.score.XGRaw),
		}
		add := AddRecommendation{
			Element:      c.info.ID,
			Name:         c.info.Name,
			Team:         teamShort[c.info.TeamID],
			PositionType: c.info.PositionType,
			Fixture:      c.fixture,
			Availability: c.availability,
			Score:        c.score,
			Reasons:      reasons,
		}
		if drop := bestDropForPosition(rosterScored, c.info.PositionType); drop != nil {
			add.SuggestedDrop = drop
		}
		adds = append(adds, add)
	}

	report := WaiverRecommendationsReport{
		LeagueID:       args.LeagueID,
		EntryID:        args.EntryID,
		AsOfGW:         asOfGW,
		TargetGW:       targetGW,
		Horizon:        h,
		WeightFixtures: wFix,
		WeightForm:     wForm,
		WeightTotal:    wTotal,
		WeightXG:       wXG,
		Adds:           adds,
		Drops:          dropCandidates,
		Notes: []string{
			"Uses unrostered pool only, status=available (status 'a').",
			"Eligibility: 60+ mins in each of last 3 GWs OR 60+ mins in at least 10 GWs this season.",
			"Fixture score uses opponent points conceded by position, split home/away, over recent horizon.",
		},
	}
	report.Filters.Minutes60Last3 = 3
	report.Filters.Minutes60Season = 10

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

func buildOwnershipAndRoster(summaryData summary.LeagueWeekSummary, entryID int) (map[int]bool, []summary.RosterPlayer) {
	owned := make(map[int]bool)
	var roster []summary.RosterPlayer
	for _, entry := range summaryData.Entries {
		for _, r := range entry.Roster {
			owned[r.Element] = true
		}
		if entry.EntryID == entryID {
			roster = entry.Roster
		}
	}
	return owned, roster
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
				Event: gw,
				TeamH: f.TeamH,
				TeamA: f.TeamA,
			})
		}
	}
	return elements, teams, fixtures, nil
}

func buildFixtureIndex(fixtures []fixture, teamShort map[int]string) map[int]FixtureContext {
	out := make(map[int]FixtureContext)
	for _, f := range fixtures {
		out[f.TeamH] = FixtureContext{
			OpponentID:    f.TeamA,
			OpponentShort: teamShort[f.TeamA],
			Venue:         "HOME",
		}
		out[f.TeamA] = FixtureContext{
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

func scoreRoster(elements []elementInfo, teamShort map[int]string, form map[int]summary.PlayerForm, xg map[int]float64, fixtures map[int]FixtureContext, roster []summary.RosterPlayer, conceded map[int]map[string]map[int]avgStat, minmax scoreMinMax, wFix, wForm, wTotal, wXG float64) []DropRecommendation {
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
		fixtureScore := fixtureDifficulty(conceded, fx.OpponentID, fx.Venue, info.PositionType)
		formScore := form[info.ID].PointsPerGW
		totalScore := float64(info.TotalPoints)
		xgScore := xg[info.ID]
		weighted := wFix*minMax(fixtureScore, minmax.FixMin, minmax.FixMax) +
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

func pickDropCandidates(drops []DropRecommendation) []DropRecommendation {
	n := 3
	if len(drops) < n {
		n = len(drops)
	}
	out := make([]DropRecommendation, 0, n)
	for i := 0; i < n; i++ {
		d := drops[i]
		d.Reason = "Lowest weighted score on roster"
		out = append(out, d)
	}
	return out
}

func bestDropForPosition(drops []DropRecommendation, pos int) *DropRecommendation {
	for _, d := range drops {
		if d.PositionType == pos {
			out := d
			out.Reason = "Lowest weighted score at position"
			return &out
		}
	}
	return nil
}
