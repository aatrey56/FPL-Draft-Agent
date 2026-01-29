package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"sort"
	"strings"
)

type GameMeta struct {
	CurrentEvent         int  `json:"current_event"`
	CurrentEventFinished bool `json:"current_event_finished"`
	NextEvent            int  `json:"next_event"`
}

type FixtureDifficultyArgs struct {
	LeagueID   int  `json:"league_id" jsonschema:"Draft league id (required)"`
	AsOfGW     int  `json:"as_of_gw" jsonschema:"As-of gameweek for stats (0 = auto)"`
	NextGW     int  `json:"next_gw" jsonschema:"Gameweek to rank fixtures for (0 = next_event)"`
	Horizon    int  `json:"horizon" jsonschema:"Rolling horizon in GWs (default 5)"`
	Limit      int  `json:"limit" jsonschema:"Limit fixtures per position (0 = all)"`
	IncludeRaw bool `json:"include_raw" jsonschema:"Include raw blended/season/recent scores"`
}

type FixtureDifficultyOutput struct {
	LeagueID int `json:"league_id"`
	AsOfGW   int `json:"as_of_gw"`
	NextGW   int `json:"next_gw"`
	Horizon  int `json:"horizon"`
	Weights  struct {
		Season float64 `json:"season"`
		Recent float64 `json:"recent"`
	} `json:"weights"`
	Positions map[string][]FixtureDifficultyItem `json:"positions"`
}

type FixtureDifficultyItem struct {
	Rank          int      `json:"rank"`
	FixtureID     int      `json:"fixture_id"`
	Event         int      `json:"event"`
	TeamID        int      `json:"team_id"`
	TeamShort     string   `json:"team_short"`
	OpponentID    int      `json:"opponent_id"`
	OpponentShort string   `json:"opponent_short"`
	Venue         string   `json:"venue"`
	Score         *float64 `json:"score,omitempty"`
	SeasonScore   *float64 `json:"season_score,omitempty"`
	RecentScore   *float64 `json:"recent_score,omitempty"`
}

type fixtureRankItem struct {
	FixtureID     int
	Event         int
	TeamID        int
	TeamShort     string
	OpponentID    int
	OpponentShort string
	Venue         string
	Score         float64
	SeasonScore   float64
	RecentScore   float64
}

func loadGameMeta(cfg ServerConfig) (GameMeta, error) {
	path := fmt.Sprintf("%s/game/game.json", strings.TrimRight(cfg.RawRoot, "/"))
	raw, err := os.ReadFile(path)
	if err != nil {
		return GameMeta{}, err
	}
	var meta GameMeta
	if err := json.Unmarshal(raw, &meta); err != nil {
		return GameMeta{}, err
	}
	return meta, nil
}

func resolveAsOfAndNextGW(cfg ServerConfig, asOfGW int, nextGW int) (int, int, error) {
	if asOfGW > 0 && nextGW > 0 {
		return asOfGW, nextGW, nil
	}
	meta, err := loadGameMeta(cfg)
	if err != nil {
		return 0, 0, err
	}

	resolvedAsOf := asOfGW
	if resolvedAsOf <= 0 {
		resolvedAsOf = meta.CurrentEvent
		if !meta.CurrentEventFinished {
			resolvedAsOf--
		}
		if resolvedAsOf < 1 {
			resolvedAsOf = 1
		}
	}

	resolvedNext := nextGW
	if resolvedNext <= 0 {
		if meta.NextEvent > 0 {
			resolvedNext = meta.NextEvent
		} else {
			resolvedNext = meta.CurrentEvent + 1
		}
	}

	return resolvedAsOf, resolvedNext, nil
}

func horizonWeights(h int) (float64, float64) {
	switch {
	case h >= 20:
		return 0.40, 0.60
	case h >= 10:
		return 0.50, 0.50
	default:
		return 0.55, 0.45
	}
}

func positionLabel(pos int) string {
	switch pos {
	case 1:
		return "GK"
	case 2:
		return "DEF"
	case 3:
		return "MID"
	case 4:
		return "FWD"
	default:
		return "UNK"
	}
}

func blendedFixtureScore(concededSeason map[int]map[string]map[int]avgStat, concededRecent map[int]map[string]map[int]avgStat, opponentID int, venue string, pos int, seasonWeight float64, recentWeight float64) (float64, float64, float64) {
	seasonScore := fixtureDifficulty(concededSeason, opponentID, venue, pos)
	recentScore := fixtureDifficulty(concededRecent, opponentID, venue, pos)
	blended := seasonWeight*seasonScore + recentWeight*recentScore
	if math.IsNaN(blended) || math.IsInf(blended, 0) {
		blended = 0
	}
	return seasonScore, recentScore, blended
}

func buildFixtureDifficulty(cfg ServerConfig, args FixtureDifficultyArgs) (FixtureDifficultyOutput, error) {
	if args.LeagueID == 0 {
		return FixtureDifficultyOutput{}, fmt.Errorf("league_id is required")
	}
	h := args.Horizon
	if h <= 0 {
		h = 5
	}

	asOfGW, nextGW, err := resolveAsOfAndNextGW(cfg, args.AsOfGW, args.NextGW)
	if err != nil {
		return FixtureDifficultyOutput{}, err
	}

	elements, teamShort, fixturesByGW, err := loadBootstrapData(cfg.RawRoot)
	if err != nil {
		return FixtureDifficultyOutput{}, err
	}

	seasonWeight, recentWeight := horizonWeights(h)
	concededSeason := computePointsConcededByPosition(cfg.RawRoot, elements, fixturesByGW, asOfGW, asOfGW)
	concededRecent := computePointsConcededByPosition(cfg.RawRoot, elements, fixturesByGW, asOfGW, h)

	fixtureList := fixturesByGW[nextGW]
	contexts := buildFixtureContexts(fixtureList, teamShort)

	positions := map[string][]FixtureDifficultyItem{}
	for pos := 1; pos <= 4; pos++ {
		rows := make([]fixtureRankItem, 0, len(contexts))
		for _, ctx := range contexts {
			seasonScore, recentScore, blended := blendedFixtureScore(concededSeason, concededRecent, ctx.OpponentID, ctx.Venue, pos, seasonWeight, recentWeight)
			rows = append(rows, fixtureRankItem{
				FixtureID:     ctx.FixtureID,
				Event:         ctx.Event,
				TeamID:        ctx.TeamID,
				TeamShort:     ctx.TeamShort,
				OpponentID:    ctx.OpponentID,
				OpponentShort: ctx.OpponentShort,
				Venue:         ctx.Venue,
				Score:         blended,
				SeasonScore:   seasonScore,
				RecentScore:   recentScore,
			})
		}

		sort.Slice(rows, func(i, j int) bool {
			if rows[i].Score != rows[j].Score {
				return rows[i].Score > rows[j].Score
			}
			if rows[i].TeamShort != rows[j].TeamShort {
				return rows[i].TeamShort < rows[j].TeamShort
			}
			return rows[i].OpponentShort < rows[j].OpponentShort
		})

		limit := args.Limit
		if limit <= 0 || limit > len(rows) {
			limit = len(rows)
		}

		out := make([]FixtureDifficultyItem, 0, limit)
		for i := 0; i < limit; i++ {
			r := rows[i]
			item := FixtureDifficultyItem{
				Rank:          i + 1,
				FixtureID:     r.FixtureID,
				Event:         r.Event,
				TeamID:        r.TeamID,
				TeamShort:     r.TeamShort,
				OpponentID:    r.OpponentID,
				OpponentShort: r.OpponentShort,
				Venue:         r.Venue,
			}
			if args.IncludeRaw {
				score := r.Score
				season := r.SeasonScore
				recent := r.RecentScore
				item.Score = &score
				item.SeasonScore = &season
				item.RecentScore = &recent
			}
			out = append(out, item)
		}

		positions[positionLabel(pos)] = out
	}

	out := FixtureDifficultyOutput{
		LeagueID:  args.LeagueID,
		AsOfGW:    asOfGW,
		NextGW:    nextGW,
		Horizon:   h,
		Positions: positions,
	}
	out.Weights.Season = seasonWeight
	out.Weights.Recent = recentWeight

	return out, nil
}

func buildFixtureContexts(fixtures []fixture, teamShort map[int]string) []FixtureContext {
	out := make([]FixtureContext, 0, len(fixtures)*2)
	for _, f := range fixtures {
		out = append(out, FixtureContext{
			FixtureID:     f.ID,
			Event:         f.Event,
			TeamID:        f.TeamH,
			TeamShort:     teamShort[f.TeamH],
			OpponentID:    f.TeamA,
			OpponentShort: teamShort[f.TeamA],
			Venue:         "HOME",
		})
		out = append(out, FixtureContext{
			FixtureID:     f.ID,
			Event:         f.Event,
			TeamID:        f.TeamA,
			TeamShort:     teamShort[f.TeamA],
			OpponentID:    f.TeamH,
			OpponentShort: teamShort[f.TeamH],
			Venue:         "AWAY",
		})
	}
	return out
}
