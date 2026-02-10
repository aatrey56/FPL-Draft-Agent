package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sort"
	"time"

	"github.com/aatrey56/FPL-Draft-Agent/apps/mcp-server/internal/draftapi"
)

func main() {
	var (
		leagueID = flag.Int("league", 14204, "draft league id")
		gw       = flag.Int("gw", 1, "gameweek for /event/{gw}/live")
		refresh  = flag.Bool("refresh", false, "bypass cache")
		cacheDir = flag.String("cache-dir", "data-cache", "cache directory")
		maxDepth = flag.Int("depth", 8, "max depth for schema walk")
	)
	flag.Parse()

	c := draftapi.NewClient(*cacheDir)

	dump("LEAGUE_DETAILS",mustRaw(c.GetJSON(fmt.Sprintf("league_%d_details_raw", *leagueID),fmt.Sprintf("https://draft.premierleague.com/api/league/%d/details", *leagueID),30*time.Minute,*refresh,)),*maxDepth,)
	dump("BOOTSTRAP_STATIC", mustRaw(c.GetJSON("bootstrap_static", "https://draft.premierleague.com/api/bootstrap-static", 30*time.Minute, *refresh)), *maxDepth)
	dump("GAME", mustJSON(c.GetGame(*refresh)), *maxDepth)
	dump(fmt.Sprintf("EVENT_%d_LIVE", *gw), mustRaw(c.GetJSON(fmt.Sprintf("event_%d_live", *gw), fmt.Sprintf("https://draft.premierleague.com/api/event/%d/live", *gw), 30*time.Second, *refresh)), *maxDepth)
}

func mustJSON(v any, err error) any {
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	return v
}

func mustRaw(b []byte, err error) any {
	if err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
	var v any
	if err := json.Unmarshal(b, &v); err != nil {
		fmt.Fprintln(os.Stderr, "json error:", err)
		os.Exit(1)
	}
	return v
}

func dump(title string, v any, maxDepth int) {
	fmt.Println("\n================================================================================")
	fmt.Println(title)
	fmt.Println("================================================================================")
	walk(v, "$", 0, maxDepth)
}

func walk(v any, path string, depth, maxDepth int) {
	if depth > maxDepth {
		fmt.Printf("%-60s %s\n", path, "(max depth)")
		return
	}

	switch x := v.(type) {
	case map[string]any:
		fmt.Printf("%-60s dict keys=%d\n", path, len(x))
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			walk(x[k], path+"."+k, depth+1, maxDepth)
		}
	case []any:
		fmt.Printf("%-60s list len=%d\n", path, len(x))
		if len(x) > 0 {
			walk(x[0], path+"[]", depth+1, maxDepth)
		}
	case string:
		fmt.Printf("%-60s str\n", path)
	case bool:
		fmt.Printf("%-60s bool\n", path)
	case float64:
		// JSON numbers decode to float64 in interface{} form.
		// We keep it simple here.
		fmt.Printf("%-60s number\n", path)
	case nil:
		fmt.Printf("%-60s null\n", path)
	default:
		fmt.Printf("%-60s %T\n", path, v)
	}
}