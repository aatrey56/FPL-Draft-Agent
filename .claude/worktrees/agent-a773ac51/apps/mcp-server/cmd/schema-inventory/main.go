package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"time"
)

type TypeSet map[string]struct{}

type SchemaMap map[string]TypeSet

type Inventory struct {
	GeneratedAtUTC string     `json:"generated_at_utc"`
	RawRoot        string     `json:"raw_root"`
	Endpoints      []Endpoint `json:"endpoints"`
}

type Endpoint struct {
	Name         string  `json:"name"`
	FilesScanned int     `json:"files_scanned"`
	Fields       []Field `json:"fields"`
}

type Field struct {
	Path  string   `json:"path"`
	Types []string `json:"types"`
}

func main() {
	var (
		rawRoot  = flag.String("raw-root", "data/raw", "root directory for raw JSON")
		outPath  = flag.String("out", "data/derived/schema_inventory.json", "output path")
		maxFiles = flag.Int("max-files", 0, "max files per endpoint (0 = no limit)")
	)
	flag.Parse()

	endpoints := []struct {
		Name string
		Glob string
	}{
		{"game", filepath.Join(*rawRoot, "game", "game.json")},
		{"bootstrap-static", filepath.Join(*rawRoot, "bootstrap", "bootstrap-static.json")},
		{"league-details", filepath.Join(*rawRoot, "league", "*", "details.json")},
		{"draft-choices", filepath.Join(*rawRoot, "draft", "*", "choices.json")},
		{"transactions", filepath.Join(*rawRoot, "league", "*", "transactions.json")},
		{"trades", filepath.Join(*rawRoot, "league", "*", "trades.json")},
		{"event-live", filepath.Join(*rawRoot, "gw", "*", "live.json")},
		{"entry-event", filepath.Join(*rawRoot, "entry", "*", "gw", "*.json")},
	}

	inv := Inventory{
		GeneratedAtUTC: time.Now().UTC().Format(time.RFC3339),
		RawRoot:        *rawRoot,
		Endpoints:      make([]Endpoint, 0, len(endpoints)),
	}

	for _, ep := range endpoints {
		files, err := filepath.Glob(ep.Glob)
		if err != nil {
			fmt.Fprintf(os.Stderr, "glob error for %s: %v\n", ep.Name, err)
			continue
		}
		sort.Strings(files)
		if *maxFiles > 0 && len(files) > *maxFiles {
			files = files[:*maxFiles]
		}
		if len(files) == 0 {
			fmt.Fprintf(os.Stderr, "no files for %s (%s)\n", ep.Name, ep.Glob)
			continue
		}

		schema := make(SchemaMap)
		for _, f := range files {
			raw, err := os.ReadFile(f)
			if err != nil {
				fmt.Fprintf(os.Stderr, "read error %s: %v\n", f, err)
				continue
			}
			var v any
			if err := json.Unmarshal(raw, &v); err != nil {
				fmt.Fprintf(os.Stderr, "json error %s: %v\n", f, err)
				continue
			}
			walkSchema(v, "$", schema)
		}

		inv.Endpoints = append(inv.Endpoints, Endpoint{
			Name:         ep.Name,
			FilesScanned: len(files),
			Fields:       schemaToFields(schema),
		})
	}

	if err := os.MkdirAll(filepath.Dir(*outPath), 0o755); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	payload, err := json.MarshalIndent(inv, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	payload = append(payload, '\n')
	if err := os.WriteFile(*outPath, payload, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
	fmt.Println("wrote", *outPath)
}

func walkSchema(v any, path string, schema SchemaMap) {
	switch x := v.(type) {
	case map[string]any:
		addType(schema, path, "object")
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			walkSchema(x[k], path+"."+k, schema)
		}
	case []any:
		addType(schema, path, "array")
		if len(x) > 0 {
			walkSchema(x[0], path+"[]", schema)
		} else {
			addType(schema, path+"[]", "unknown")
		}
	case string:
		addType(schema, path, "string")
	case bool:
		addType(schema, path, "bool")
	case float64:
		addType(schema, path, "number")
	case nil:
		addType(schema, path, "null")
	default:
		addType(schema, path, fmt.Sprintf("%T", v))
	}
}

func addType(schema SchemaMap, path string, typ string) {
	set, ok := schema[path]
	if !ok {
		set = make(TypeSet)
		schema[path] = set
	}
	set[typ] = struct{}{}
}

func schemaToFields(schema SchemaMap) []Field {
	paths := make([]string, 0, len(schema))
	for p := range schema {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	fields := make([]Field, 0, len(paths))
	for _, p := range paths {
		types := make([]string, 0, len(schema[p]))
		for t := range schema[p] {
			types = append(types, t)
		}
		sort.Strings(types)
		fields = append(fields, Field{
			Path:  p,
			Types: types,
		})
	}
	return fields
}
