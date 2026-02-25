package summary

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/ledger"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/model"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/points"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/reconcile"
	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/store"
)

type PlayerMeta struct {
	ID           int    `json:"id"`
	Name         string `json:"name"`
	PositionType int    `json:"position_type"`
	TeamID       int    `json:"team_id"`
	TeamShort    string `json:"team_short"`
	Status       string `json:"status"`
}

type RosterPlayer struct {
	Element      int    `json:"element"`
	Name         string `json:"name"`
	Team         string `json:"team"`
	Position     int    `json:"position"`
	PositionType int    `json:"position_type"`
	Role         string `json:"role"`
}

type Record struct {
	Wins   int `json:"wins"`
	Draws  int `json:"draws"`
	Losses int `json:"losses"`
}

type PointsSummary struct {
	Starters int `json:"starters"`
	Bench    int `json:"bench"`
}

type ManagerWeekSummary struct {
	EntryID         int            `json:"entry_id"`
	EntryName       string         `json:"entry_name"`
	OpponentID      int            `json:"opponent_entry_id"`
	OpponentName    string         `json:"opponent_name"`
	ScoreFor        int            `json:"score_for"`
	ScoreAgainst    int            `json:"score_against"`
	Result          string         `json:"result"`
	Record          Record         `json:"record"`
	Points          PointsSummary  `json:"points"`
	Roster          []RosterPlayer `json:"roster"`
	MissingOpponent bool           `json:"missing_opponent"`
}

type LeagueWeekSummary struct {
	LeagueID       int                  `json:"league_id"`
	Gameweek       int                  `json:"gameweek"`
	GeneratedAtUTC string               `json:"generated_at_utc"`
	Entries        []ManagerWeekSummary `json:"entries"`
}

type PositionPoints struct {
	GK  int `json:"gk"`
	DEF int `json:"def"`
	MID int `json:"mid"`
	FWD int `json:"fwd"`
}

type MatchupBreakdown struct {
	EntryID       int            `json:"entry_id"`
	EntryName     string         `json:"entry_name"`
	OpponentID    int            `json:"opponent_entry_id"`
	OpponentName  string         `json:"opponent_name"`
	Points        PositionPoints `json:"points"`
	Opponent      PositionPoints `json:"opponent"`
	Diff          PositionPoints `json:"diff"`
	Total         int            `json:"total"`
	OpponentTotal int            `json:"opponent_total"`
	Result        string         `json:"result"`
}

type MatchupSummary struct {
	LeagueID       int                `json:"league_id"`
	Gameweek       int                `json:"gameweek"`
	GeneratedAtUTC string             `json:"generated_at_utc"`
	Matchups       []MatchupBreakdown `json:"matchups"`
}

type PlayerForm struct {
	Element      int     `json:"element"`
	Name         string  `json:"name"`
	Team         string  `json:"team"`
	PositionType int     `json:"position_type"`
	Minutes      int     `json:"minutes"`
	Points       int     `json:"points"`
	PointsPerGW  float64 `json:"points_per_gw"`
	MinutesPerGW float64 `json:"minutes_per_gw"`
	Ownership    int     `json:"ownership"`
	OwnershipPct float64 `json:"ownership_pct"`
	RiskScore    float64 `json:"risk_score"`
}

type PlayerFormSummary struct {
	LeagueID       int          `json:"league_id"`
	AsOfGW         int          `json:"as_of_gw"`
	Horizon        int          `json:"horizon"`
	GeneratedAtUTC string       `json:"generated_at_utc"`
	Players        []PlayerForm `json:"players"`
}

type WaiverTarget struct {
	Element      int     `json:"element"`
	Name         string  `json:"name"`
	Team         string  `json:"team"`
	PositionType int     `json:"position_type"`
	Minutes      int     `json:"minutes"`
	Points       int     `json:"points"`
	PointsPerGW  float64 `json:"points_per_gw"`
	RiskScore    float64 `json:"risk_score"`
	Score        float64 `json:"score"`
}

type WaiverTargetsSummary struct {
	LeagueID       int            `json:"league_id"`
	Gameweek       int            `json:"gameweek"`
	Horizon        int            `json:"horizon"`
	RiskLevel      string         `json:"risk"`
	GeneratedAtUTC string         `json:"generated_at_utc"`
	Targets        []WaiverTarget `json:"targets"`
}

type LeagueDetails struct {
	LeagueEntries []struct {
		ID        int    `json:"id"`
		EntryID   int    `json:"entry_id"`
		EntryName string `json:"entry_name"`
	} `json:"league_entries"`
	Matches []struct {
		Event              int  `json:"event"`
		Finished           bool `json:"finished"`
		Started            bool `json:"started"`
		LeagueEntry1       int  `json:"league_entry_1"`
		LeagueEntry1Points int  `json:"league_entry_1_points"`
		LeagueEntry2       int  `json:"league_entry_2"`
		LeagueEntry2Points int  `json:"league_entry_2_points"`
	} `json:"matches"`
}

type StandingsRow struct {
	EntryID        int    `json:"entry_id"`
	EntryName      string `json:"entry_name"`
	Rank           int    `json:"rank"`
	Played         int    `json:"played"`
	Wins           int    `json:"wins"`
	Draws          int    `json:"draws"`
	Losses         int    `json:"losses"`
	PointsFor      int    `json:"points_for"`
	PointsAgainst  int    `json:"points_against"`
	MatchPoints    int    `json:"match_points"`
	TotalFPLPoints int    `json:"total_fpl_points"`
}

type StandingsSummary struct {
	LeagueID       int            `json:"league_id"`
	Gameweek       int            `json:"gameweek"`
	GeneratedAtUTC string         `json:"generated_at_utc"`
	Rows           []StandingsRow `json:"rows"`
}

type EntryTransactions struct {
	EntryID   int    `json:"entry_id"`
	EntryName string `json:"entry_name"`
	WaiverIn  []int  `json:"waiver_in"`
	WaiverOut []int  `json:"waiver_out"`
	FreeIn    []int  `json:"free_in"`
	FreeOut   []int  `json:"free_out"`
	TradeIn   []int  `json:"trade_in"`
	TradeOut  []int  `json:"trade_out"`
	TotalIn   int    `json:"total_in"`
	TotalOut  int    `json:"total_out"`
	Net       int    `json:"net"`
}

type TransactionsSummary struct {
	LeagueID       int                 `json:"league_id"`
	Gameweek       int                 `json:"gameweek"`
	GeneratedAtUTC string              `json:"generated_at_utc"`
	Entries        []EntryTransactions `json:"entries"`
}

type LineupEfficiencyEntry struct {
	EntryID                int    `json:"entry_id"`
	EntryName              string `json:"entry_name"`
	BenchPoints            int    `json:"bench_points"`
	BenchPointsPlayed      int    `json:"bench_points_played"`
	ZeroMinuteStarters     []int  `json:"zero_minute_starters"`
	ZeroMinuteStarterCount int    `json:"zero_minute_starter_count"`
	MissingSnapshot        bool   `json:"missing_snapshot"`
}

type LineupEfficiencySummary struct {
	LeagueID       int                     `json:"league_id"`
	Gameweek       int                     `json:"gameweek"`
	GeneratedAtUTC string                  `json:"generated_at_utc"`
	Entries        []LineupEfficiencyEntry `json:"entries"`
}

type PositionCounts struct {
	GK    int `json:"gk"`
	DEF   int `json:"def"`
	MID   int `json:"mid"`
	FWD   int `json:"fwd"`
	Total int `json:"total"`
}

type OwnershipEntrySummary struct {
	EntryID   int            `json:"entry_id"`
	EntryName string         `json:"entry_name"`
	Counts    PositionCounts `json:"counts"`
}

type PositionHoarder struct {
	EntryID   int    `json:"entry_id"`
	EntryName string `json:"entry_name"`
	Count     int    `json:"count"`
}

type OwnershipScarcitySummary struct {
	LeagueID       int                          `json:"league_id"`
	Gameweek       int                          `json:"gameweek"`
	GeneratedAtUTC string                       `json:"generated_at_utc"`
	LeagueTotals   PositionCounts               `json:"league_totals"`
	OwnedTotals    PositionCounts               `json:"owned_totals"`
	UnownedTotals  PositionCounts               `json:"unowned_totals"`
	Entries        []OwnershipEntrySummary      `json:"entries"`
	Hoarders       map[string][]PositionHoarder `json:"hoarders"`
}

type StrengthOfScheduleEntry struct {
	EntryID             int     `json:"entry_id"`
	EntryName           string  `json:"entry_name"`
	PastGames           int     `json:"past_games"`
	FutureGames         int     `json:"future_games"`
	PastOppAvgRank      float64 `json:"past_opponent_avg_rank"`
	FutureOppAvgRank    float64 `json:"future_opponent_avg_rank"`
	PastOppTopHalf      int     `json:"past_opponents_top_half"`
	PastOppBottomHalf   int     `json:"past_opponents_bottom_half"`
	FutureOppTopHalf    int     `json:"future_opponents_top_half"`
	FutureOppBottomHalf int     `json:"future_opponents_bottom_half"`
}

type StrengthOfScheduleSummary struct {
	LeagueID       int                       `json:"league_id"`
	Gameweek       int                       `json:"gameweek"`
	GeneratedAtUTC string                    `json:"generated_at_utc"`
	TopHalfCutoff  int                       `json:"top_half_cutoff"`
	Entries        []StrengthOfScheduleEntry `json:"entries"`
}

type FixtureSummary struct {
	FixtureID  int    `json:"fixture_id"`
	Event      int    `json:"event"`
	TeamH      int    `json:"team_h"`
	TeamA      int    `json:"team_a"`
	TeamHShort string `json:"team_h_short"`
	TeamAShort string `json:"team_a_short"`
	KickoffUTC string `json:"kickoff_utc"`
	Finished   bool   `json:"finished"`
	Started    bool   `json:"started"`
}

type UpcomingFixturesSummary struct {
	LeagueID       int              `json:"league_id"`
	AsOfGW         int              `json:"as_of_gw"`
	Horizon        int              `json:"horizon"`
	GeneratedAtUTC string           `json:"generated_at_utc"`
	Fixtures       []FixtureSummary `json:"fixtures"`
}

type bootstrapMeta struct {
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

func BuildLeagueSummaries(st *store.JSONStore, derivedRoot string, leagueID int, ld LeagueDetails, entryIDs []int, minGW int, maxGW int, horizons []int, riskLevels []string) error {
	meta, teamShort, err := loadBootstrapMeta(st)
	if err != nil {
		return err
	}
	entryNameByID := make(map[int]string)
	entryToLeagueEntry := make(map[int]int)
	leagueEntryToEntry := make(map[int]int)
	for _, e := range ld.LeagueEntries {
		entryNameByID[e.EntryID] = e.EntryName
		entryToLeagueEntry[e.EntryID] = e.ID
		leagueEntryToEntry[e.ID] = e.EntryID
	}

	ledgerPath := filepath.Join(derivedRoot, fmt.Sprintf("ledger/%d/event_0.json", leagueID))
	ledgerRaw, err := os.ReadFile(ledgerPath)
	if err != nil {
		return err
	}
	var ledgerOut model.DraftLedger
	if err := json.Unmarshal(ledgerRaw, &ledgerOut); err != nil {
		return err
	}
	transactions, err := loadTransactions(st, leagueID)
	if err != nil {
		return err
	}
	trades, err := loadTrades(st, leagueID)
	if err != nil {
		return err
	}

	for gw := minGW; gw <= maxGW; gw++ {
		liveByElement, err := loadLiveStatsForPoints(st, gw)
		if err != nil {
			return err
		}

		matchOpp := buildOpponentMap(ld.Matches, leagueEntryToEntry, gw)
		entryPointsByPos := make(map[int]PositionPoints)
		entryTotals := make(map[int]int)
		entryBenchTotals := make(map[int]int)
		entryRosters := make(map[int][]RosterPlayer)
		snapshotsByEntry := make(map[int]*ledger.EntrySnapshot)

		for _, entryID := range entryIDs {
			snap, err := loadSnapshot(derivedRoot, leagueID, entryID, gw)
			if err != nil {
				return err
			}
			snapshotsByEntry[entryID] = snap
			entryRosters[entryID] = buildRoster(meta, snap)
			entryTotals[entryID], entryBenchTotals[entryID], entryPointsByPos[entryID] = computePoints(meta, snap, liveByElement)
		}

		summary := LeagueWeekSummary{
			LeagueID:       leagueID,
			Gameweek:       gw,
			GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
			Entries:        make([]ManagerWeekSummary, 0, len(entryIDs)),
		}

		for _, entryID := range entryIDs {
			opp := matchOpp[entryID]
			rec := computeRecord(ld.Matches, entryToLeagueEntry[entryID], gw)
			ms := ManagerWeekSummary{
				EntryID:      entryID,
				EntryName:    entryNameByID[entryID],
				OpponentID:   opp.OpponentEntryID,
				OpponentName: entryNameByID[opp.OpponentEntryID],
				ScoreFor:     opp.ScoreFor,
				ScoreAgainst: opp.ScoreAgainst,
				Result:       opp.Result,
				Record:       rec,
				Points: PointsSummary{
					Starters: entryTotals[entryID],
					Bench:    entryBenchTotals[entryID],
				},
				Roster:          entryRosters[entryID],
				MissingOpponent: opp.Missing,
			}
			summary.Entries = append(summary.Entries, ms)
		}

		outPath := filepath.Join(derivedRoot, fmt.Sprintf("summary/league/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outPath, summary); err != nil {
			return err
		}

		matchup := MatchupSummary{
			LeagueID:       leagueID,
			Gameweek:       gw,
			GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
			Matchups:       make([]MatchupBreakdown, 0),
		}
		for _, m := range ld.Matches {
			if m.Event != gw {
				continue
			}
			if !m.Started {
				continue
			}
			aID := leagueEntryToEntry[m.LeagueEntry1]
			bID := leagueEntryToEntry[m.LeagueEntry2]
			aPts := entryPointsByPos[aID]
			bPts := entryPointsByPos[bID]
			breakdown := MatchupBreakdown{
				EntryID:       aID,
				EntryName:     entryNameByID[aID],
				OpponentID:    bID,
				OpponentName:  entryNameByID[bID],
				Points:        aPts,
				Opponent:      bPts,
				Diff:          diffPositionPoints(aPts, bPts),
				Total:         entryTotals[aID],
				OpponentTotal: entryTotals[bID],
				Result:        resultFromScore(entryTotals[aID], entryTotals[bID]),
			}
			matchup.Matchups = append(matchup.Matchups, breakdown)
		}
		outMatchup := filepath.Join(derivedRoot, fmt.Sprintf("summary/matchup/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outMatchup, matchup); err != nil {
			return err
		}

		standingsRows, standingsRank := computeStandings(ld.Matches, leagueEntryToEntry, entryNameByID, entryIDs, gw)
		standings := StandingsSummary{
			LeagueID:       leagueID,
			Gameweek:       gw,
			GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
			Rows:           standingsRows,
		}
		outStandings := filepath.Join(derivedRoot, fmt.Sprintf("summary/standings/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outStandings, standings); err != nil {
			return err
		}

		txSummary := buildTransactionsDigest(leagueID, gw, entryIDs, entryNameByID, transactions, trades)
		outTx := filepath.Join(derivedRoot, fmt.Sprintf("summary/transactions/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outTx, txSummary); err != nil {
			return err
		}

		lineup := buildLineupEfficiency(leagueID, gw, entryIDs, entryNameByID, snapshotsByEntry, liveByElement)
		outLineup := filepath.Join(derivedRoot, fmt.Sprintf("summary/lineup_efficiency/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outLineup, lineup); err != nil {
			return err
		}

		ownership := buildOwnershipScarcity(leagueID, gw, entryIDs, entryNameByID, meta, &ledgerOut, transactions, trades)
		outOwnership := filepath.Join(derivedRoot, fmt.Sprintf("summary/ownership_scarcity/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outOwnership, ownership); err != nil {
			return err
		}

		sos := buildStrengthOfSchedule(leagueID, gw, entryIDs, entryNameByID, ld.Matches, leagueEntryToEntry, standingsRank)
		outSoS := filepath.Join(derivedRoot, fmt.Sprintf("summary/strength_of_schedule/%d/gw/%d.json", leagueID, gw))
		if err := writeJSON(outSoS, sos); err != nil {
			return err
		}

		for _, horizon := range horizons {
			form, err := buildPlayerForm(meta, ledgerOut, transactions, trades, entryIDs, gw, horizon, st)
			if err != nil {
				return err
			}
			if gw == maxGW {
				outForm := filepath.Join(derivedRoot, fmt.Sprintf("summary/player_form/%d/h%d.json", leagueID, horizon))
				if err := writeJSON(outForm, form); err != nil {
					return err
				}
			}

			for _, risk := range riskLevels {
				targets, err := buildWaiverTargets(form, risk, entryIDs)
				if err != nil {
					return err
				}
				outTargets := filepath.Join(derivedRoot, fmt.Sprintf("summary/waiver_targets/%d/gw/%d_h%d_risk-%s.json", leagueID, gw, horizon, risk))
				if err := writeJSON(outTargets, targets); err != nil {
					return err
				}
			}
		}
	}

	for _, horizon := range horizons {
		fixtures, err := buildUpcomingFixtures(st, leagueID, maxGW, horizon, teamShort)
		if err != nil {
			return err
		}
		outFixtures := filepath.Join(derivedRoot, fmt.Sprintf("summary/fixtures/%d/from_gw/%d_h%d.json", leagueID, maxGW, horizon))
		if err := writeJSON(outFixtures, fixtures); err != nil {
			return err
		}
	}

	return nil
}

func buildPlayerForm(meta map[int]PlayerMeta, ledgerOut model.DraftLedger, transactions []reconcile.Transaction, trades []reconcile.Trade, entryIDs []int, gw int, horizon int, st *store.JSONStore) (PlayerFormSummary, error) {
	start := gw - horizon + 1
	if start < 1 {
		start = 1
	}
	rolling := make(map[int]struct {
		Points  int
		Minutes int
	})
	for g := start; g <= gw; g++ {
		liveByElement, err := loadLiveStatsForPoints(st, g)
		if err != nil {
			return PlayerFormSummary{}, err
		}
		for id, stats := range liveByElement {
			cur := rolling[id]
			cur.Points += stats.TotalPoints
			cur.Minutes += stats.Minutes
			rolling[id] = cur
		}
	}

	ownedByEntry := reconcile.BuildOwnershipMapAtGW(&ledgerOut, transactions, trades, gw)
	ownership := make(map[int]int)
	for _, players := range ownedByEntry {
		for id := range players {
			ownership[id]++
		}
	}

	players := make([]PlayerForm, 0, len(meta))
	for id, m := range meta {
		r := rolling[id]
		ppg := float64(r.Points) / float64(horizon)
		mpg := float64(r.Minutes) / float64(horizon)
		minutesPct := float64(r.Minutes) / float64(horizon*90)
		if minutesPct > 1 {
			minutesPct = 1
		}
		risk := 1 - minutesPct
		own := ownership[id]
		// Guard against empty league (len==0) which would produce NaN/+Inf that
		// json.Marshal cannot serialise, causing a runtime error.
		var ownPct float64
		if len(entryIDs) > 0 {
			ownPct = float64(own) / float64(len(entryIDs))
		}
		players = append(players, PlayerForm{
			Element:      id,
			Name:         m.Name,
			Team:         m.TeamShort,
			PositionType: m.PositionType,
			Minutes:      r.Minutes,
			Points:       r.Points,
			PointsPerGW:  ppg,
			MinutesPerGW: mpg,
			Ownership:    own,
			OwnershipPct: ownPct,
			RiskScore:    risk,
		})
	}
	sort.Slice(players, func(i, j int) bool {
		return players[i].PointsPerGW > players[j].PointsPerGW
	})
	return PlayerFormSummary{
		LeagueID:       ledgerOut.LeagueID,
		AsOfGW:         gw,
		Horizon:        horizon,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Players:        players,
	}, nil
}

func buildWaiverTargets(form PlayerFormSummary, risk string, entryIDs []int) (WaiverTargetsSummary, error) {
	thresholds := riskThresholds()
	thr, ok := thresholds[risk]
	if !ok {
		return WaiverTargetsSummary{}, fmt.Errorf("unknown risk level: %s", risk)
	}
	targets := make([]WaiverTarget, 0)
	for _, p := range form.Players {
		if p.Ownership > 0 {
			continue
		}
		if p.RiskScore > thr {
			continue
		}
		minutesPct := float64(p.Minutes) / float64(form.Horizon*90)
		if minutesPct > 1 {
			minutesPct = 1
		}
		score := p.PointsPerGW * minutesPct
		targets = append(targets, WaiverTarget{
			Element:      p.Element,
			Name:         p.Name,
			Team:         p.Team,
			PositionType: p.PositionType,
			Minutes:      p.Minutes,
			Points:       p.Points,
			PointsPerGW:  p.PointsPerGW,
			RiskScore:    p.RiskScore,
			Score:        score,
		})
	}
	sort.Slice(targets, func(i, j int) bool {
		return targets[i].Score > targets[j].Score
	})
	if len(targets) > 50 {
		targets = targets[:50]
	}
	return WaiverTargetsSummary{
		LeagueID:       form.LeagueID,
		Gameweek:       form.AsOfGW,
		Horizon:        form.Horizon,
		RiskLevel:      risk,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Targets:        targets,
	}, nil
}

func loadSnapshot(derivedRoot string, leagueID int, entryID int, gw int) (*ledger.EntrySnapshot, error) {
	snapPath := filepath.Join(derivedRoot, fmt.Sprintf("snapshots/%d/entry/%d/gw/%d.json", leagueID, entryID, gw))
	raw, err := os.ReadFile(snapPath)
	if err != nil {
		return nil, err
	}
	var snap ledger.EntrySnapshot
	if err := json.Unmarshal(raw, &snap); err != nil {
		return nil, err
	}
	return &snap, nil
}

func loadBootstrapMeta(st *store.JSONStore) (map[int]PlayerMeta, map[int]string, error) {
	raw, err := st.ReadRaw("bootstrap/bootstrap-static.json")
	if err != nil {
		return nil, nil, err
	}
	var resp bootstrapMeta
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, nil, err
	}
	teamShort := make(map[int]string, len(resp.Teams))
	for _, t := range resp.Teams {
		teamShort[t.ID] = t.ShortName
	}
	meta := make(map[int]PlayerMeta, len(resp.Elements))
	for _, e := range resp.Elements {
		name := e.WebName
		if name == "" {
			name = strings.TrimSpace(e.FirstName + " " + e.SecondName)
		}
		meta[e.ID] = PlayerMeta{
			ID:           e.ID,
			Name:         name,
			PositionType: e.ElementType,
			TeamID:       e.Team,
			TeamShort:    teamShort[e.Team],
			Status:       e.Status,
		}
	}
	return meta, teamShort, nil
}

func buildRoster(meta map[int]PlayerMeta, snap *ledger.EntrySnapshot) []RosterPlayer {
	roster := make([]RosterPlayer, 0, len(snap.Picks))
	for _, p := range snap.Picks {
		m := meta[p.Element]
		role := "bench"
		if p.Position <= 11 {
			role = "starter"
		}
		roster = append(roster, RosterPlayer{
			Element:      p.Element,
			Name:         m.Name,
			Team:         m.TeamShort,
			Position:     p.Position,
			PositionType: m.PositionType,
			Role:         role,
		})
	}
	sort.Slice(roster, func(i, j int) bool {
		return roster[i].Position < roster[j].Position
	})
	return roster
}

func computePoints(meta map[int]PlayerMeta, snap *ledger.EntrySnapshot, liveByElement map[int]points.LiveStats) (int, int, PositionPoints) {
	starter := 0
	bench := 0
	pos := PositionPoints{}
	for _, p := range snap.Picks {
		stats := liveByElement[p.Element]
		total := stats.TotalPoints
		if p.Position <= 11 {
			starter += total
			switch meta[p.Element].PositionType {
			case 1:
				pos.GK += total
			case 2:
				pos.DEF += total
			case 3:
				pos.MID += total
			case 4:
				pos.FWD += total
			}
		} else {
			bench += total
		}
	}
	return starter, bench, pos
}

type OpponentInfo struct {
	OpponentEntryID int
	ScoreFor        int
	ScoreAgainst    int
	Result          string
	Missing         bool
}

func buildOpponentMap(matches []struct {
	Event              int  `json:"event"`
	Finished           bool `json:"finished"`
	Started            bool `json:"started"`
	LeagueEntry1       int  `json:"league_entry_1"`
	LeagueEntry1Points int  `json:"league_entry_1_points"`
	LeagueEntry2       int  `json:"league_entry_2"`
	LeagueEntry2Points int  `json:"league_entry_2_points"`
}, leagueEntryToEntry map[int]int, gw int) map[int]OpponentInfo {
	out := make(map[int]OpponentInfo)
	for _, m := range matches {
		if m.Event != gw {
			continue
		}
		if !m.Started {
			continue
		}
		a := leagueEntryToEntry[m.LeagueEntry1]
		b := leagueEntryToEntry[m.LeagueEntry2]
		out[a] = OpponentInfo{
			OpponentEntryID: b,
			ScoreFor:        m.LeagueEntry1Points,
			ScoreAgainst:    m.LeagueEntry2Points,
			Result:          resultFromScore(m.LeagueEntry1Points, m.LeagueEntry2Points),
		}
		out[b] = OpponentInfo{
			OpponentEntryID: a,
			ScoreFor:        m.LeagueEntry2Points,
			ScoreAgainst:    m.LeagueEntry1Points,
			Result:          resultFromScore(m.LeagueEntry2Points, m.LeagueEntry1Points),
		}
	}
	for k, v := range out {
		if v.OpponentEntryID == 0 {
			v.Missing = true
			out[k] = v
		}
	}
	return out
}

func computeRecord(matches []struct {
	Event              int  `json:"event"`
	Finished           bool `json:"finished"`
	Started            bool `json:"started"`
	LeagueEntry1       int  `json:"league_entry_1"`
	LeagueEntry1Points int  `json:"league_entry_1_points"`
	LeagueEntry2       int  `json:"league_entry_2"`
	LeagueEntry2Points int  `json:"league_entry_2_points"`
}, leagueEntryID int, gw int) Record {
	rec := Record{}
	for _, m := range matches {
		if m.Event > gw || !m.Finished {
			continue
		}
		var forPts, againstPts int
		if m.LeagueEntry1 == leagueEntryID {
			forPts = m.LeagueEntry1Points
			againstPts = m.LeagueEntry2Points
		} else if m.LeagueEntry2 == leagueEntryID {
			forPts = m.LeagueEntry2Points
			againstPts = m.LeagueEntry1Points
		} else {
			continue
		}
		if forPts > againstPts {
			rec.Wins++
		} else if forPts < againstPts {
			rec.Losses++
		} else {
			rec.Draws++
		}
	}
	return rec
}

func resultFromScore(forPts int, againstPts int) string {
	if forPts > againstPts {
		return "W"
	}
	if forPts < againstPts {
		return "L"
	}
	return "D"
}

func diffPositionPoints(a PositionPoints, b PositionPoints) PositionPoints {
	return PositionPoints{
		GK:  a.GK - b.GK,
		DEF: a.DEF - b.DEF,
		MID: a.MID - b.MID,
		FWD: a.FWD - b.FWD,
	}
}

type standingsStat struct {
	played        int
	wins          int
	draws         int
	losses        int
	pointsFor     int
	pointsAgainst int
}

func computeStandings(matches []struct {
	Event              int  `json:"event"`
	Finished           bool `json:"finished"`
	Started            bool `json:"started"`
	LeagueEntry1       int  `json:"league_entry_1"`
	LeagueEntry1Points int  `json:"league_entry_1_points"`
	LeagueEntry2       int  `json:"league_entry_2"`
	LeagueEntry2Points int  `json:"league_entry_2_points"`
}, leagueEntryToEntry map[int]int, entryNameByID map[int]string, entryIDs []int, gw int) ([]StandingsRow, map[int]int) {
	stats := make(map[int]*standingsStat, len(entryIDs))
	for _, entryID := range entryIDs {
		stats[entryID] = &standingsStat{}
	}

	for _, m := range matches {
		if m.Event > gw || !m.Finished {
			continue
		}
		aID := leagueEntryToEntry[m.LeagueEntry1]
		bID := leagueEntryToEntry[m.LeagueEntry2]
		if aID == 0 || bID == 0 {
			continue
		}
		a := stats[aID]
		b := stats[bID]
		a.played++
		b.played++
		a.pointsFor += m.LeagueEntry1Points
		a.pointsAgainst += m.LeagueEntry2Points
		b.pointsFor += m.LeagueEntry2Points
		b.pointsAgainst += m.LeagueEntry1Points
		if m.LeagueEntry1Points > m.LeagueEntry2Points {
			a.wins++
			b.losses++
		} else if m.LeagueEntry1Points < m.LeagueEntry2Points {
			b.wins++
			a.losses++
		} else {
			a.draws++
			b.draws++
		}
	}

	rows := make([]StandingsRow, 0, len(entryIDs))
	for _, entryID := range entryIDs {
		s := stats[entryID]
		matchPoints := s.wins*3 + s.draws
		rows = append(rows, StandingsRow{
			EntryID:        entryID,
			EntryName:      entryNameByID[entryID],
			Played:         s.played,
			Wins:           s.wins,
			Draws:          s.draws,
			Losses:         s.losses,
			PointsFor:      s.pointsFor,
			PointsAgainst:  s.pointsAgainst,
			MatchPoints:    matchPoints,
			TotalFPLPoints: s.pointsFor,
		})
	}

	sort.Slice(rows, func(i, j int) bool {
		if rows[i].MatchPoints != rows[j].MatchPoints {
			return rows[i].MatchPoints > rows[j].MatchPoints
		}
		diffI := rows[i].PointsFor - rows[i].PointsAgainst
		diffJ := rows[j].PointsFor - rows[j].PointsAgainst
		if diffI != diffJ {
			return diffI > diffJ
		}
		if rows[i].PointsFor != rows[j].PointsFor {
			return rows[i].PointsFor > rows[j].PointsFor
		}
		return rows[i].EntryName < rows[j].EntryName
	})

	rankByEntry := make(map[int]int, len(rows))
	for i := range rows {
		rows[i].Rank = i + 1
		rankByEntry[rows[i].EntryID] = rows[i].Rank
	}

	return rows, rankByEntry
}

func buildTransactionsDigest(leagueID int, gw int, entryIDs []int, entryNameByID map[int]string, transactions []reconcile.Transaction, trades []reconcile.Trade) TransactionsSummary {
	byEntry := make(map[int]*EntryTransactions, len(entryIDs))
	for _, entryID := range entryIDs {
		byEntry[entryID] = &EntryTransactions{
			EntryID:   entryID,
			EntryName: entryNameByID[entryID],
		}
	}

	for i := range transactions {
		tx := transactions[i]
		if tx.Event != gw || tx.Result != "a" {
			continue
		}
		entry := byEntry[tx.Entry]
		if entry == nil {
			entry = &EntryTransactions{EntryID: tx.Entry, EntryName: entryNameByID[tx.Entry]}
			byEntry[tx.Entry] = entry
		}
		switch tx.Kind {
		case "w":
			if tx.ElementIn != 0 {
				entry.WaiverIn = append(entry.WaiverIn, tx.ElementIn)
			}
			if tx.ElementOut != 0 {
				entry.WaiverOut = append(entry.WaiverOut, tx.ElementOut)
			}
		case "f":
			if tx.ElementIn != 0 {
				entry.FreeIn = append(entry.FreeIn, tx.ElementIn)
			}
			if tx.ElementOut != 0 {
				entry.FreeOut = append(entry.FreeOut, tx.ElementOut)
			}
		}
	}

	for i := range trades {
		tr := trades[i]
		if tr.Event != gw || tr.State != "p" {
			continue
		}
		offered := byEntry[tr.OfferedEntry]
		if offered == nil {
			offered = &EntryTransactions{EntryID: tr.OfferedEntry, EntryName: entryNameByID[tr.OfferedEntry]}
			byEntry[tr.OfferedEntry] = offered
		}
		received := byEntry[tr.ReceivedEntry]
		if received == nil {
			received = &EntryTransactions{EntryID: tr.ReceivedEntry, EntryName: entryNameByID[tr.ReceivedEntry]}
			byEntry[tr.ReceivedEntry] = received
		}
		for _, item := range tr.TradeItems {
			if item.ElementOut != 0 {
				offered.TradeOut = append(offered.TradeOut, item.ElementOut)
				received.TradeIn = append(received.TradeIn, item.ElementOut)
			}
			if item.ElementIn != 0 {
				received.TradeOut = append(received.TradeOut, item.ElementIn)
				offered.TradeIn = append(offered.TradeIn, item.ElementIn)
			}
		}
	}

	entries := make([]EntryTransactions, 0, len(byEntry))
	for _, entryID := range entryIDs {
		entry := byEntry[entryID]
		if entry == nil {
			continue
		}
		entry.TotalIn = len(entry.WaiverIn) + len(entry.FreeIn) + len(entry.TradeIn)
		entry.TotalOut = len(entry.WaiverOut) + len(entry.FreeOut) + len(entry.TradeOut)
		entry.Net = entry.TotalIn - entry.TotalOut
		entries = append(entries, *entry)
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].EntryName < entries[j].EntryName
	})

	return TransactionsSummary{
		LeagueID:       leagueID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Entries:        entries,
	}
}

func BuildTransactionsSummary(st *store.JSONStore, derivedRoot string, leagueID int, gw int) error {
	if leagueID == 0 {
		return fmt.Errorf("league_id is required")
	}
	if gw == 0 {
		return fmt.Errorf("gw is required")
	}
	raw, err := st.ReadRaw(fmt.Sprintf("league/%d/details.json", leagueID))
	if err != nil {
		return err
	}
	var ld LeagueDetails
	if err := json.Unmarshal(raw, &ld); err != nil {
		return err
	}
	entryIDs := make([]int, 0, len(ld.LeagueEntries))
	entryNameByID := make(map[int]string, len(ld.LeagueEntries))
	for _, e := range ld.LeagueEntries {
		entryIDs = append(entryIDs, e.EntryID)
		entryNameByID[e.EntryID] = e.EntryName
	}
	transactions, err := loadTransactions(st, leagueID)
	if err != nil {
		return err
	}
	trades, err := loadTrades(st, leagueID)
	if err != nil {
		return err
	}
	txSummary := buildTransactionsDigest(leagueID, gw, entryIDs, entryNameByID, transactions, trades)
	outTx := filepath.Join(derivedRoot, fmt.Sprintf("summary/transactions/%d/gw/%d.json", leagueID, gw))
	return writeJSON(outTx, txSummary)
}

func buildLineupEfficiency(leagueID int, gw int, entryIDs []int, entryNameByID map[int]string, snapshots map[int]*ledger.EntrySnapshot, liveByElement map[int]points.LiveStats) LineupEfficiencySummary {
	out := LineupEfficiencySummary{
		LeagueID:       leagueID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Entries:        make([]LineupEfficiencyEntry, 0, len(entryIDs)),
	}
	for _, entryID := range entryIDs {
		snap := snapshots[entryID]
		if snap == nil {
			out.Entries = append(out.Entries, LineupEfficiencyEntry{
				EntryID:         entryID,
				EntryName:       entryNameByID[entryID],
				MissingSnapshot: true,
			})
			continue
		}
		benchPoints := 0
		benchPointsPlayed := 0
		zeroMinuteStarters := make([]int, 0)

		for _, p := range snap.Picks {
			stats := liveByElement[p.Element]
			if p.Position <= 11 {
				if stats.Minutes == 0 {
					zeroMinuteStarters = append(zeroMinuteStarters, p.Element)
				}
			} else {
				benchPoints += stats.TotalPoints
				if stats.Minutes > 0 {
					benchPointsPlayed += stats.TotalPoints
				}
			}
		}

		out.Entries = append(out.Entries, LineupEfficiencyEntry{
			EntryID:                entryID,
			EntryName:              entryNameByID[entryID],
			BenchPoints:            benchPoints,
			BenchPointsPlayed:      benchPointsPlayed,
			ZeroMinuteStarters:     zeroMinuteStarters,
			ZeroMinuteStarterCount: len(zeroMinuteStarters),
		})
	}
	return out
}

func buildOwnershipScarcity(leagueID int, gw int, entryIDs []int, entryNameByID map[int]string, meta map[int]PlayerMeta, ledgerOut *model.DraftLedger, transactions []reconcile.Transaction, trades []reconcile.Trade) OwnershipScarcitySummary {
	owned := reconcile.BuildOwnershipMapAtGW(ledgerOut, transactions, trades, gw)

	allTotals := PositionCounts{}
	for _, m := range meta {
		addPositionCount(&allTotals, m.PositionType)
	}

	ownedTotals := PositionCounts{}
	entrySummaries := make([]OwnershipEntrySummary, 0, len(entryIDs))
	for _, entryID := range entryIDs {
		counts := PositionCounts{}
		for elementID := range owned[entryID] {
			addPositionCount(&counts, meta[elementID].PositionType)
			addPositionCount(&ownedTotals, meta[elementID].PositionType)
		}
		entrySummaries = append(entrySummaries, OwnershipEntrySummary{
			EntryID:   entryID,
			EntryName: entryNameByID[entryID],
			Counts:    counts,
		})
	}

	unownedTotals := PositionCounts{
		GK:    allTotals.GK - ownedTotals.GK,
		DEF:   allTotals.DEF - ownedTotals.DEF,
		MID:   allTotals.MID - ownedTotals.MID,
		FWD:   allTotals.FWD - ownedTotals.FWD,
		Total: allTotals.Total - ownedTotals.Total,
	}

	hoarders := map[string][]PositionHoarder{
		"gk":  topHoarders(entrySummaries, func(c PositionCounts) int { return c.GK }),
		"def": topHoarders(entrySummaries, func(c PositionCounts) int { return c.DEF }),
		"mid": topHoarders(entrySummaries, func(c PositionCounts) int { return c.MID }),
		"fwd": topHoarders(entrySummaries, func(c PositionCounts) int { return c.FWD }),
	}

	return OwnershipScarcitySummary{
		LeagueID:       leagueID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		LeagueTotals:   allTotals,
		OwnedTotals:    ownedTotals,
		UnownedTotals:  unownedTotals,
		Entries:        entrySummaries,
		Hoarders:       hoarders,
	}
}

func buildStrengthOfSchedule(leagueID int, gw int, entryIDs []int, entryNameByID map[int]string, matches []struct {
	Event              int  `json:"event"`
	Finished           bool `json:"finished"`
	Started            bool `json:"started"`
	LeagueEntry1       int  `json:"league_entry_1"`
	LeagueEntry1Points int  `json:"league_entry_1_points"`
	LeagueEntry2       int  `json:"league_entry_2"`
	LeagueEntry2Points int  `json:"league_entry_2_points"`
}, leagueEntryToEntry map[int]int, rankByEntry map[int]int) StrengthOfScheduleSummary {
	topHalf := len(entryIDs) / 2
	if len(entryIDs)%2 != 0 {
		topHalf = (len(entryIDs) + 1) / 2
	}

	entries := make([]StrengthOfScheduleEntry, 0, len(entryIDs))
	for _, entryID := range entryIDs {
		pastCount := 0
		futureCount := 0
		pastSum := 0
		futureSum := 0
		pastTop := 0
		pastBottom := 0
		futureTop := 0
		futureBottom := 0

		for _, m := range matches {
			aID := leagueEntryToEntry[m.LeagueEntry1]
			bID := leagueEntryToEntry[m.LeagueEntry2]
			opp := 0
			if aID == entryID {
				opp = bID
			} else if bID == entryID {
				opp = aID
			}
			if opp == 0 {
				continue
			}
			rank := rankByEntry[opp]
			if rank == 0 {
				continue
			}
			if m.Event <= gw && m.Finished {
				pastCount++
				pastSum += rank
				if rank <= topHalf {
					pastTop++
				} else {
					pastBottom++
				}
			} else if m.Event > gw {
				futureCount++
				futureSum += rank
				if rank <= topHalf {
					futureTop++
				} else {
					futureBottom++
				}
			}
		}

		pastAvg := 0.0
		futureAvg := 0.0
		if pastCount > 0 {
			pastAvg = float64(pastSum) / float64(pastCount)
		}
		if futureCount > 0 {
			futureAvg = float64(futureSum) / float64(futureCount)
		}

		entries = append(entries, StrengthOfScheduleEntry{
			EntryID:             entryID,
			EntryName:           entryNameByID[entryID],
			PastGames:           pastCount,
			FutureGames:         futureCount,
			PastOppAvgRank:      pastAvg,
			FutureOppAvgRank:    futureAvg,
			PastOppTopHalf:      pastTop,
			PastOppBottomHalf:   pastBottom,
			FutureOppTopHalf:    futureTop,
			FutureOppBottomHalf: futureBottom,
		})
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].EntryName < entries[j].EntryName
	})

	return StrengthOfScheduleSummary{
		LeagueID:       leagueID,
		Gameweek:       gw,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		TopHalfCutoff:  topHalf,
		Entries:        entries,
	}
}

func buildUpcomingFixtures(st *store.JSONStore, leagueID int, asOfGW int, horizon int, teamShort map[int]string) (UpcomingFixturesSummary, error) {
	raw, err := st.ReadRaw("bootstrap/bootstrap-static.json")
	if err != nil {
		return UpcomingFixturesSummary{}, err
	}
	var resp struct {
		Fixtures map[string][]struct {
			ID          int    `json:"id"`
			Event       int    `json:"event"`
			TeamH       int    `json:"team_h"`
			TeamA       int    `json:"team_a"`
			KickoffTime string `json:"kickoff_time"`
			Finished    bool   `json:"finished"`
			Started     bool   `json:"started"`
		} `json:"fixtures"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return UpcomingFixturesSummary{}, err
	}

	fixtures := make([]FixtureSummary, 0)
	start := asOfGW
	if start < 1 {
		start = 1
	}
	end := asOfGW + horizon - 1
	for gw := start; gw <= end; gw++ {
		key := strconv.Itoa(gw)
		list := resp.Fixtures[key]
		for _, f := range list {
			fixtures = append(fixtures, FixtureSummary{
				FixtureID:  f.ID,
				Event:      f.Event,
				TeamH:      f.TeamH,
				TeamA:      f.TeamA,
				TeamHShort: teamShort[f.TeamH],
				TeamAShort: teamShort[f.TeamA],
				KickoffUTC: f.KickoffTime,
				Finished:   f.Finished,
				Started:    f.Started,
			})
		}
	}

	sort.Slice(fixtures, func(i, j int) bool {
		if fixtures[i].Event != fixtures[j].Event {
			return fixtures[i].Event < fixtures[j].Event
		}
		return fixtures[i].KickoffUTC < fixtures[j].KickoffUTC
	})

	return UpcomingFixturesSummary{
		LeagueID:       leagueID,
		AsOfGW:         asOfGW,
		Horizon:        horizon,
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		Fixtures:       fixtures,
	}, nil
}

func addPositionCount(c *PositionCounts, pos int) {
	switch pos {
	case 1:
		c.GK++
	case 2:
		c.DEF++
	case 3:
		c.MID++
	case 4:
		c.FWD++
	}
	c.Total++
}

func topHoarders(entries []OwnershipEntrySummary, getCount func(PositionCounts) int) []PositionHoarder {
	hoarders := make([]PositionHoarder, 0, len(entries))
	for _, e := range entries {
		hoarders = append(hoarders, PositionHoarder{
			EntryID:   e.EntryID,
			EntryName: e.EntryName,
			Count:     getCount(e.Counts),
		})
	}
	sort.Slice(hoarders, func(i, j int) bool {
		if hoarders[i].Count != hoarders[j].Count {
			return hoarders[i].Count > hoarders[j].Count
		}
		return hoarders[i].EntryName < hoarders[j].EntryName
	})
	if len(hoarders) > 3 {
		hoarders = hoarders[:3]
	}
	return hoarders
}

func ParseHorizons(s string) ([]int, error) {
	if strings.TrimSpace(s) == "" {
		return []int{5, 10, 20}, nil
	}
	parts := strings.Split(s, ",")
	out := make([]int, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p == "" {
			continue
		}
		v, err := strconv.Atoi(p)
		if err != nil || v <= 0 {
			return nil, fmt.Errorf("invalid horizon: %s", p)
		}
		out = append(out, v)
	}
	if len(out) == 0 {
		out = []int{5, 10, 20}
	}
	return out, nil
}

func ParseRiskLevels(s string) []string {
	if strings.TrimSpace(s) == "" {
		return []string{"low", "med", "high"}
	}
	parts := strings.Split(s, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		p = strings.TrimSpace(strings.ToLower(p))
		if p == "" {
			continue
		}
		out = append(out, p)
	}
	if len(out) == 0 {
		out = []string{"low", "med", "high"}
	}
	return out
}

func riskThresholds() map[string]float64 {
	return map[string]float64{
		"low":    0.3,
		"med":    0.6,
		"medium": 0.6,
		"high":   1.0,
	}
}

type liveResponse struct {
	Elements map[string]struct {
		Stats struct {
			Minutes     int `json:"minutes"`
			TotalPoints int `json:"total_points"`
		} `json:"stats"`
	} `json:"elements"`
}

func loadLiveStatsForPoints(st *store.JSONStore, gw int) (map[int]points.LiveStats, error) {
	raw, err := st.ReadRaw(fmt.Sprintf("gw/%d/live.json", gw))
	if err != nil {
		return nil, err
	}

	var resp liveResponse
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, err
	}

	out := make(map[int]points.LiveStats, len(resp.Elements))
	for k, v := range resp.Elements {
		id, err := strconv.Atoi(k)
		if err != nil {
			continue
		}
		out[id] = points.LiveStats{
			Minutes:     v.Stats.Minutes,
			TotalPoints: v.Stats.TotalPoints,
		}
	}
	return out, nil
}

func loadTransactions(st *store.JSONStore, leagueID int) ([]reconcile.Transaction, error) {
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

func loadTrades(st *store.JSONStore, leagueID int) ([]reconcile.Trade, error) {
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

func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}

	b, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}

	b = append(b, '\n')
	return os.WriteFile(path, b, 0o644)
}
