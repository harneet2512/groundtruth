package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func TestCLIParseCacheReusesUnchangedInputsWithoutChangingGraphFacts(t *testing.T) {
	bin := buildDerivedIndexer(t)
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeDerivedFixtureRepo(t, repo)
	cache := filepath.Join(root, "cache")
	build := func(name string, cached bool) (string, string) {
		db := filepath.Join(root, name+".db")
		cmd := exec.Command(bin, "-root", repo, "-output", db)
		cmd.Env = append(os.Environ(), "GT_PARSE_CACHE_ROOT=")
		if cached {
			cmd.Env = append(cmd.Env, "GT_PARSE_CACHE_ROOT="+cache)
		}
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("%s: %v\n%s", name, err, out)
		}
		return db, string(out)
	}
	_, cold := build("cold", true)
	if !strings.Contains(cold, "Parse cache: 0 hits, 3 misses") {
		t.Fatal(cold)
	}
	_, warm := build("warm", true)
	if !strings.Contains(warm, "Parse cache: 3 hits, 0 misses") {
		t.Fatal(warm)
	}
	path := filepath.Join(repo, "mod.py")
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(strings.Replace(string(data), "value + 1", "value + 2", 1)), 0600); err != nil {
		t.Fatal(err)
	}
	cachedPath, amended := build("cached-edit", true)
	if !strings.Contains(amended, "Parse cache: 2 hits, 1 misses") {
		t.Fatal(amended)
	}
	freshPath, _ := build("fresh-edit", false)
	for _, table := range []string{"nodes", "edges", "properties", "assertions", "resolution_symbols", "resolution_callsites", "resolution_candidates", "closure", "cochanges"} {
		cachedRows := cacheGraphRows(t, cachedPath, table)
		freshRows := cacheGraphRows(t, freshPath, table)
		if !reflect.DeepEqual(cachedRows, freshRows) {
			t.Errorf("cache changed %s", table)
		}
	}
}

func cacheGraphRows(t *testing.T, path, table string) []string {
	t.Helper()
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.Query(`SELECT * FROM "` + table + `"`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		t.Fatal(err)
	}
	var result []string
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err := rows.Scan(pointers...); err != nil {
			t.Fatal(err)
		}
		result = append(result, fmt.Sprint(values))
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	sort.Strings(result)
	return result
}
