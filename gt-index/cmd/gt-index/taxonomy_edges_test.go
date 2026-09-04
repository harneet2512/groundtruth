package main

import (
	"database/sql"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	_ "github.com/mattn/go-sqlite3"
)

// Item 11, delta row 9. internal/taxonomy derives the additive edge kinds and
// is unit-tested there, but a pure function nobody calls produces no graph.
// The publication path is reachable only through main(), so this drives the
// compiled binary over a real repository and asserts the rows are IN THE
// DATABASE — the wiring, not the derivation.
//
// It also pins the property the item may not violate: a syntactic kind is
// never CERTIFIED beyond what its mechanism grants. That is asserted against
// the persisted rows, because a tier is only a claim once it is stored.
//
// The fixture is a single TypeScript file using the three constructs the
// pinned grammar states syntactically: an `implements` clause (its own
// implements_clause node), an `override` keyword, and a declared return type.
func TestTaxonomyEdgesReachTheGraph(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}

	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	source := `export interface Shape {
  area(): number;
}

export class Base {
  area(): number {
    return 0;
  }
}

export class Circle extends Base implements Shape {
  override area(): number {
    return 1;
  }
}

export function build(): Circle {
  return new Circle();
}
`
	if err := os.WriteFile(filepath.Join(repo, "shapes.ts"), []byte(source), 0o644); err != nil {
		t.Fatal(err)
	}

	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}

	dbPath := filepath.Join(tmp, "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath)
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("index: %v\n%s", err, out)
	}

	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	counts := map[string]int{}
	rows, err := db.Query(`SELECT type, COUNT(*) FROM edges WHERE type IN (?,?,?,?,?) GROUP BY type`,
		specs.EdgeDeclaredImplements, specs.EdgeOverrides, specs.EdgeDecorates,
		specs.EdgeReturnsType, specs.EdgeParamType)
	if err != nil {
		t.Fatal(err)
	}
	for rows.Next() {
		var kind string
		var n int
		if err := rows.Scan(&kind, &n); err != nil {
			t.Fatal(err)
		}
		counts[kind] = n
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	_ = rows.Close()

	for _, kind := range []string{
		specs.EdgeDeclaredImplements, specs.EdgeOverrides, specs.EdgeReturnsType,
	} {
		if counts[kind] == 0 {
			t.Errorf("no %s edge reached the graph; internal/taxonomy.DeriveEdges is not wired into publication", kind)
		}
	}

	// Every persisted taxonomy row must carry a mechanism its kind grants and a
	// tier that mechanism earns. CERTIFIED here would mean a syntactic reading
	// was promoted, which is the one thing this item may not do.
	inspect, err := db.Query(
		`SELECT type, resolution_method, trust_tier, confidence, candidate_count,
		        verification_status, evidence_type, COALESCE(metadata,'')
		   FROM edges WHERE type IN (?,?,?,?,?)`,
		specs.EdgeDeclaredImplements, specs.EdgeOverrides, specs.EdgeDecorates,
		specs.EdgeReturnsType, specs.EdgeParamType)
	if err != nil {
		t.Fatal(err)
	}
	defer inspect.Close()

	seen := 0
	for inspect.Next() {
		var kind, mechanism, tier, status, evidence, metadata string
		var confidence float64
		var candidates int
		if err := inspect.Scan(&kind, &mechanism, &tier, &confidence, &candidates,
			&status, &evidence, &metadata); err != nil {
			t.Fatal(err)
		}
		seen++
		granted := false
		for _, m := range specs.TaxonomyEdgeMechanisms[kind] {
			if m == mechanism {
				granted = true
			}
		}
		if !granted {
			t.Errorf("%s row carries mechanism %q, which its kind does not grant", kind, mechanism)
		}
		if tier != specs.TierCandidate && tier != specs.TierSpeculative {
			t.Errorf("%s row carries tier %q; a syntactic kind is never promoted", kind, tier)
		}
		if confidence >= 1.0 {
			t.Errorf("%s row has confidence %v; a syntactic reading is not certain", kind, confidence)
		}
		if status != "unverified" {
			t.Errorf("%s row is %q; a syntactic reading is never verified", kind, status)
		}
		if evidence != "syntax" {
			t.Errorf("%s row has evidence_type %q, want syntax", kind, evidence)
		}
		if candidates < 1 {
			t.Errorf("%s row records candidate_count %d", kind, candidates)
		}
		var meta map[string]any
		if err := json.Unmarshal([]byte(metadata), &meta); err != nil {
			t.Errorf("%s row metadata is not JSON: %v", kind, err)
		} else if meta["mechanism"] != mechanism {
			t.Errorf("%s row metadata mechanism %v disagrees with the column %q", kind, meta["mechanism"], mechanism)
		}
	}
	if err := inspect.Err(); err != nil {
		t.Fatal(err)
	}
	if seen == 0 {
		t.Error("no taxonomy edge rows to inspect")
	}
}

// TestTaxonomyDoesNotMoveAPreExistingEdgeTotal is the additivity check at the
// graph boundary rather than in a unit test: on the same fixture, the kinds
// that existed before item 11 must be exactly what they were. CONTAINS follows
// from parent links, CALLS from the constructor call, EXTENDS from the regex
// inheritance pass. The taxonomy adds rows beside them and moves none.
func TestTaxonomyDoesNotMoveAPreExistingEdgeTotal(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	source := `export class Base {
  area(): number {
    return 0;
  }
}

export class Circle extends Base {
  area(): number {
    return 1;
  }
}
`
	if err := os.WriteFile(filepath.Join(repo, "shapes.ts"), []byte(source), 0o644); err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-ldflags", testBuildLDFlags, "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}
	dbPath := filepath.Join(tmp, "graph.db")
	if out, err := exec.Command(bin, "-root", repo, "-output", dbPath).CombinedOutput(); err != nil {
		t.Fatalf("index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// Two classes, two methods: two CONTAINS, one EXTENDS. These are the counts
	// the parser and the relationship pass produced before item 11 and must
	// keep producing after it.
	for _, want := range []struct {
		kind string
		n    int
	}{{"CONTAINS", 2}, {"EXTENDS", 1}} {
		var got int
		if err := db.QueryRow(`SELECT COUNT(*) FROM edges WHERE type = ?`, want.kind).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != want.n {
			t.Errorf("%s = %d, want %d (item 11 must not move a pre-existing total)", want.kind, got, want.n)
		}
	}
	// Labels the parser assigned before item 11 are untouched: two Class, two
	// Method, and no Function.
	for _, want := range []struct {
		label string
		n     int
	}{{"Class", 2}, {"Method", 2}, {"Function", 0}} {
		var got int
		if err := db.QueryRow(`SELECT COUNT(*) FROM nodes WHERE label = ?`, want.label).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != want.n {
			t.Errorf("label %s = %d, want %d (item 11 must not relabel a node)", want.label, got, want.n)
		}
	}
}
