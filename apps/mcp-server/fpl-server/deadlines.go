package main

// Deadline intelligence: the draft API publishes, per event, trades_time,
// waivers_time and deadline_time (lineup lock), and per-fixture kickoffs.
// buildDeadlines turns that into the week's calendar — served inside
// league_pulse and reused by the TUI countdown. Times are emitted in both
// UTC (ISO) and US Eastern (the user's zone).

import (
	"fmt"
	"path/filepath"
	"sort"
	"time"
	_ "time/tzdata" // embed the tz database so America/New_York always resolves
)

var eastern = func() *time.Location {
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		return time.UTC
	}
	return loc
}()

var london = func() *time.Location {
	loc, err := time.LoadLocation("Europe/London")
	if err != nil {
		return time.UTC
	}
	return loc
}()

type deadlineEvent struct {
	ID           int    `json:"id"`
	Name         string `json:"name"`
	Finished     bool   `json:"finished"`
	DeadlineTime string `json:"deadline_time"`
	WaiversTime  string `json:"waivers_time"`
	TradesTime   string `json:"trades_time"`
}

type bootstrapCalendar struct {
	Events struct {
		Current int             `json:"current"`
		Next    int             `json:"next"`
		Data    []deadlineEvent `json:"data"`
	} `json:"events"`
	Fixtures map[string][]struct {
		KickoffTime string `json:"kickoff_time"`
	} `json:"fixtures"`
}

func loadCalendar(rawDir string) (bootstrapCalendar, error) {
	var cal bootstrapCalendar
	err := readJSONFile(filepath.Join(rawDir, "bootstrap/bootstrap-static.json"), &cal)
	return cal, err
}

func fmtBoth(iso string) map[string]string {
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return map[string]string{"utc": iso}
	}
	return map[string]string{
		"utc": t.UTC().Format(time.RFC3339),
		"est": t.In(eastern).Format("Mon Jan 2, 3:04 PM") + " EST",
	}
}

// gwDeadlines assembles one event's calendar entry.
func gwDeadlines(cal bootstrapCalendar, id int) map[string]any {
	var ev *deadlineEvent
	for i := range cal.Events.Data {
		if cal.Events.Data[i].ID == id {
			ev = &cal.Events.Data[i]
			break
		}
	}
	if ev == nil {
		return nil
	}
	out := map[string]any{
		"gw":                ev.ID,
		"finished":          ev.Finished,
		"trades_due":        fmtBoth(ev.TradesTime),
		"waivers_due":       fmtBoth(ev.WaiversTime),
		"free_agency_open":  fmtBoth(ev.WaiversTime), // FA window = waiver processing -> lineup lock
		"free_agency_close": fmtBoth(ev.DeadlineTime),
		"lineup_lock":       fmtBoth(ev.DeadlineTime),
	}
	// Kickoff window + estimated points-final (bootstrap fixtures cover the
	// next few events only; absent = omit rather than guess).
	if fixtures, ok := cal.Fixtures[fmt.Sprintf("%d", id)]; ok && len(fixtures) > 0 {
		kicks := make([]time.Time, 0, len(fixtures))
		for _, f := range fixtures {
			if t, err := time.Parse(time.RFC3339, f.KickoffTime); err == nil {
				kicks = append(kicks, t)
			}
		}
		if len(kicks) > 0 {
			sort.Slice(kicks, func(i, j int) bool { return kicks[i].Before(kicks[j]) })
			first, last := kicks[0], kicks[len(kicks)-1]
			out["first_kickoff"] = fmtBoth(first.Format(time.RFC3339))
			out["last_kickoff"] = fmtBoth(last.Format(time.RFC3339))
			// 2026-27 rule: scores lock 09:00 UK the morning after the final
			// match — computed in Europe/London so BST vs GMT (festive
			// period!) resolves correctly.
			lockDay := last.Add(2*time.Hour).In(london).AddDate(0, 0, 1)
			lock := time.Date(lockDay.Year(), lockDay.Month(), lockDay.Day(), 9, 0, 0, 0, london)
			out["points_final_estimate"] = fmtBoth(lock.Format(time.RFC3339))
		}
	}
	return out
}

// buildDeadlines returns the calendar for the current and next events.
func buildDeadlines(rawDir string) (map[string]any, error) {
	cal, err := loadCalendar(rawDir)
	if err != nil {
		return nil, err
	}
	current := cal.Events.Current
	if current == 0 {
		current = cal.Events.Next
	}
	out := map[string]any{"current": gwDeadlines(cal, current)}
	if next := current + 1; gwDeadlines(cal, next) != nil {
		out["next"] = gwDeadlines(cal, next)
	}
	out["note"] = "trades_due -> waivers_due -> lineup_lock, each 24h apart (league settings); free agency runs waivers_due -> lineup_lock, first-come first-served. points_final_estimate = scores lock ~09:00 UK the morning after the GW's last match."
	return out, nil
}
