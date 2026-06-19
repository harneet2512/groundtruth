package resolver

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// parseStorePromote parses one source file, persists its nodes + properties into a
// fresh graph.db (mapping the parser's NodeIdx -> the assigned DB row id, exactly as
// the full-index pipeline does), then runs the property->edge promote pass. It returns
// the live DB so the test can assert the materialized edges. This is the end-to-end
// chain the gap lives on: parser EXTRACTION (the part under repair) -> store -> the
// existing promoteRaises. A bug anywhere in the chain shows up as a missing/wrong edge.
func parseStorePromote(t *testing.T, ext, src string) *store.DB {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture"+ext)
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(ext)
	if spec == nil {
		t.Fatalf("no spec registered for %s", ext)
	}
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := parser.ParseFile(sf, false)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}

	db, err := store.Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}

	// Persist nodes; capture NodeIdx -> dbid (BatchInsertNodes returns ids in input order).
	nodes := make([]*store.Node, len(res.Nodes))
	for i := range res.Nodes {
		n := res.Nodes[i]
		nodes[i] = &n
	}
	// Parent ids in parser output are 1-based NodeIdx+1 conventions handled upstream; for
	// these flat fixtures functions/classes are top-level (parent 0), which is all the
	// RAISES promote needs (it resolves the TARGET class by name, not via parent).
	dbids, err := db.BatchInsertNodes(nodes)
	if err != nil {
		t.Fatalf("BatchInsertNodes: %v", err)
	}

	// Persist properties, remapping NodeIdx -> dbid.
	var props []*store.Property
	for _, p := range res.Properties {
		if p.NodeIdx < 0 || p.NodeIdx >= len(dbids) {
			continue
		}
		props = append(props, &store.Property{
			NodeID:     dbids[p.NodeIdx],
			Kind:       p.Kind,
			Value:      p.Value,
			Line:       p.Line,
			Confidence: p.Confidence,
		})
	}
	if err := db.BatchInsertProperties(props); err != nil {
		t.Fatalf("BatchInsertProperties: %v", err)
	}

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}
	return db
}

// raisesEdgeToClassExists reports whether a promoted RAISES edge points at the node
// named className (the internal error class). It also returns the resolution_method so
// the test can prove the edge is a PROMOTED FACT, never a name_match guess.
func raisesEdgeToClassExists(t *testing.T, db *store.DB, className string) (bool, string) {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`
		SELECT e.resolution_method
		  FROM edges e
		  JOIN nodes t ON t.id = e.target_id
		 WHERE e.type = 'RAISES' AND t.name = ?`, className)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	for rows.Next() {
		var method string
		if err := rows.Scan(&method); err != nil {
			t.Fatal(err)
		}
		return true, method
	}
	return false, ""
}

func raisesEdgeCountToName(t *testing.T, db *store.DB, name string) int {
	t.Helper()
	return countEdges(t, db, `type='RAISES' AND target_id IN (SELECT id FROM nodes WHERE name='`+name+`')`)
}

// TestPromoteRaises_RustErrorModel proves the Rust error model now mints a RAISES edge
// to the INTERNAL error class (struct/enum) — via Err(<Type>), Err(<Enum>::Variant),
// <Type>{..}.into(), and the bail! macro — while a builtin/external std::io::Error raise
// mints NOTHING (correct-or-quiet, non-invention). RED before the parser extracts the
// internal type name into exception_type/exception_flow (today it emits Err(...)/panic
// markers that resolve to no class), GREEN after.
func TestPromoteRaises_RustErrorModel(t *testing.T) {
	// MyError (struct) and ConfigError (enum) are the internal error classes. parse()
	// raises both conditionally + an external std::io::Error (must stay a property).
	// build() raises MyError via the .into() idiom. check() raises ConfigError via bail!.
	src := `struct MyError;

enum ConfigError { Bad }

fn parse(s: &str) -> Result<i32, MyError> {
    if s.is_empty() {
        return Err(MyError);
    }
    if s == "cfg" {
        return Err(ConfigError::Bad);
    }
    if s == "io" {
        return Err(std::io::Error::new(std::io::ErrorKind::Other, "x"));
    }
    Ok(0)
}

fn build() -> Result<i32, MyError> {
    if true {
        return MyError{}.into();
    }
    Ok(1)
}

fn check(n: i32) -> Result<(), ConfigError> {
    if n < 0 {
        bail!(ConfigError::Bad);
    }
    Ok(())
}
`
	db := parseStorePromote(t, ".rs", src)
	defer db.Close()

	// (1) RAISES -> MyError exists and is a PROMOTED edge (not name_match).
	ok, method := raisesEdgeToClassExists(t, db, "MyError")
	if !ok {
		t.Errorf("Rust: expected a RAISES edge to internal class MyError, got none")
	} else if method == "name_match" {
		t.Errorf("Rust: RAISES->MyError must be a promoted FACT, got resolution_method=name_match")
	}

	// (2) RAISES -> ConfigError exists (enum, via Err(Enum::Variant) and bail!).
	if ok, _ := raisesEdgeToClassExists(t, db, "ConfigError"); !ok {
		t.Errorf("Rust: expected a RAISES edge to internal enum ConfigError, got none")
	}

	// (3) Non-invention: the external std::io::Error raise mints ZERO edges. There is no
	//     Error node in this graph, so any RAISES edge naming Error / Io / std is a leak.
	for _, ext := range []string{"Error", "Io", "std", "io"} {
		if got := raisesEdgeCountToName(t, db, ext); got != 0 {
			t.Errorf("Rust non-invention: external std::io::Error raise minted %d RAISES edge(s) onto %q (expected 0)", got, ext)
		}
	}

	// (4) The full RAISES set is exactly the four raising-site -> internal-class
	//     relations, all promoted facts (none name_match), and NOTHING else
	//     (the external std::io::Error raise contributes zero):
	//       parse -> MyError      (Err(MyError) conditional)
	//       parse -> ConfigError  (Err(ConfigError::Bad) conditional, enum variant)
	//       build -> MyError      (MyError{}.into() conversion idiom)
	//       check -> ConfigError  (bail!(ConfigError::Bad) macro)
	if got := countEdges(t, db, `type='RAISES'`); got != 4 {
		t.Errorf("Rust: expected exactly 4 RAISES edges, got %d", got)
	}
	if got := countEdges(t, db, `type='RAISES' AND resolution_method='promote_raises'`); got != 4 {
		t.Errorf("Rust: all 4 RAISES edges must be promoted facts, got %d with method=promote_raises", got)
	}
}

// TestPromoteRaises_GoConditionalError proves a Go conditional `return ..., &<Type>{..}`
// (a custom internal error type returned inside an if-block) now mints a RAISES edge to
// that type, while the builtin/value errors.New(...) raise stays a property (no edge).
func TestPromoteRaises_GoConditionalError(t *testing.T) {
	src := `package p

import "errors"

type ConfigError struct{ msg string }

func (e *ConfigError) Error() string { return e.msg }

func Parse(s string) (int, error) {
	if s == "" {
		return 0, errors.New("empty")
	}
	if s == "bad" {
		return 0, &ConfigError{msg: "bad config"}
	}
	return 1, nil
}
`
	db := parseStorePromote(t, ".go", src)
	defer db.Close()

	// (1) RAISES -> ConfigError exists and is a promoted fact.
	ok, method := raisesEdgeToClassExists(t, db, "ConfigError")
	if !ok {
		t.Errorf("Go: expected a RAISES edge to internal type ConfigError (conditional return &ConfigError{}), got none")
	} else if method == "name_match" {
		t.Errorf("Go: RAISES->ConfigError must be a promoted FACT, got resolution_method=name_match")
	}

	// (2) Non-invention: errors.New(...) is a value, not a named internal class -> ZERO
	//     edges onto a fabricated "errors"/"New"/"error" target.
	for _, ext := range []string{"errors", "New", "error"} {
		if got := raisesEdgeCountToName(t, db, ext); got != 0 {
			t.Errorf("Go non-invention: errors.New raise minted %d RAISES edge(s) onto %q (expected 0)", got, ext)
		}
	}

	// (3) Exactly one RAISES edge (ConfigError).
	if got := countEdges(t, db, `type='RAISES'`); got != 1 {
		t.Errorf("Go: expected exactly 1 RAISES edge (ConfigError), got %d", got)
	}
}
