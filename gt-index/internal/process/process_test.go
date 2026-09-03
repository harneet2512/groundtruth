package process

import (
	"context"
	"database/sql"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// fixtureSchema is the subset of the store schema this package reads. It is
// copied field-for-field from internal/store/sqlite.go for the columns used,
// so the tests fail if the real schema drifts away from what Derive assumes.
const fixtureSchema = `
CREATE TABLE nodes (
	id INTEGER PRIMARY KEY,
	label TEXT NOT NULL,
	name TEXT NOT NULL,
	qualified_name TEXT,
	file_path TEXT NOT NULL,
	start_line INTEGER,
	end_line INTEGER,
	is_test BOOLEAN DEFAULT 0,
	language TEXT NOT NULL,
	stable_id TEXT
);

CREATE TABLE edges (
	id INTEGER PRIMARY KEY,
	source_id INTEGER NOT NULL REFERENCES nodes(id),
	target_id INTEGER NOT NULL REFERENCES nodes(id),
	type TEXT NOT NULL,
	resolution_method TEXT,
	confidence REAL DEFAULT 0.0,
	trust_tier TEXT DEFAULT 'SPECULATIVE'
);

CREATE TABLE resolution_symbols (
	stable_id TEXT PRIMARY KEY,
	native_id TEXT NOT NULL UNIQUE,
	native_kind TEXT NOT NULL,
	normalized_kind TEXT NOT NULL,
	language TEXT NOT NULL,
	path TEXT NOT NULL,
	qualified_name TEXT NOT NULL,
	start_line INTEGER NOT NULL,
	end_line INTEGER NOT NULL,
	export_status TEXT NOT NULL
);

CREATE TABLE assertions (
	id INTEGER PRIMARY KEY,
	test_node_id INTEGER NOT NULL REFERENCES nodes(id),
	target_node_id INTEGER DEFAULT 0,
	resolution_score REAL DEFAULT 0.0,
	kind TEXT NOT NULL,
	expression TEXT NOT NULL,
	expected TEXT,
	line INTEGER
);

CREATE TABLE closure (
	source_id INTEGER,
	target_id INTEGER,
	depth INTEGER,
	min_confidence REAL,
	PRIMARY KEY(source_id, target_id, depth)
);
`

// graph is the fixture the tests build against.
//
//	 n1 test_throws        (test)
//	 n2 entry              --CERTIFIED--> n3 --CERTIFIED--> n4 (sink)
//	                       --CERTIFIED--> n5 (sink)
//	                       --CANDIDATE--> n6 --CERTIFIED--> n7 (sink)
//	 n8 lonely             (no outgoing edges at all)
//	 n9 candidate_only     --CANDIDATE--> n5
//	n10 deep0 -> deep1 -> deep2 -> deep3 -> deep4 (all CERTIFIED, ids 10..14)
//
// Assertions:
//
//	a1 n1 -> n2   (witnesses the entry)
//	a2 n1 -> n8   (target is a sink: no process)
//	a3 n1 -> n9   (only a CANDIDATE edge leaves it: no process)
//	a4 n1 -> 0    (unlinked: not scanned as a witness, still counted)
//	a5 n1 -> n10  (depth-4 chain, used for the MaxDepth bound test)
func newFixture(t *testing.T) *sql.DB {
	t.Helper()

	db, err := sql.Open("sqlite3", filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })

	if _, err := db.Exec(fixtureSchema); err != nil {
		t.Fatalf("create fixture schema: %v", err)
	}
	if err := EnsureSchema(db); err != nil {
		t.Fatalf("ensure process schema: %v", err)
	}

	// Nodes. stable_id is deliberately left NULL on every code symbol, exactly
	// as the real arktype graph has it, so the resolution_symbols fallback is
	// the path under test rather than a dead branch.
	nodes := []struct {
		id       int64
		label    string
		name     string
		file     string
		line     int
		isTest   int
		stableID any
	}{
		{1, "Function", "test_throws", "test/a.test.ts", 10, 1, nil},
		{2, "Function", "entry", "src/a.ts", 20, 0, nil},
		{3, "Function", "mid", "src/a.ts", 30, 0, nil},
		{4, "Function", "sink_deep", "src/a.ts", 40, 0, nil},
		{5, "Function", "sink_shallow", "src/a.ts", 50, 0, nil},
		{6, "Function", "candidate_mid", "src/b.ts", 60, 0, nil},
		{7, "Function", "candidate_sink", "src/b.ts", 70, 0, nil},
		{8, "Function", "lonely", "src/c.ts", 80, 0, nil},
		{9, "Function", "candidate_only", "src/c.ts", 90, 0, nil},
		{10, "Function", "deep0", "src/d.ts", 100, 0, nil},
		{11, "Function", "deep1", "src/d.ts", 110, 0, nil},
		{12, "Function", "deep2", "src/d.ts", 120, 0, nil},
		{13, "Function", "deep3", "src/d.ts", 130, 0, nil},
		{14, "Function", "deep4", "src/d.ts", 140, 0, nil},
	}
	for _, n := range nodes {
		if _, err := db.Exec(
			`INSERT INTO nodes (id, label, name, qualified_name, file_path, start_line, end_line, is_test, language, stable_id)
			 VALUES (?,?,?,?,?,?,?,?, 'typescript', ?)`,
			n.id, n.label, n.name, n.name, n.file, n.line, n.line+5, n.isTest, n.stableID,
		); err != nil {
			t.Fatalf("insert node %d: %v", n.id, err)
		}
		// The resolution overlay names the same symbol by (path, start_line).
		// Stable ids are "sid-NN" so assertions on ordering stay readable.
		if _, err := db.Exec(
			`INSERT INTO resolution_symbols
			   (stable_id, native_id, native_kind, normalized_kind, language, path,
			    qualified_name, start_line, end_line, export_status)
			 VALUES (?,?, 'function', 'function', 'typescript', ?, ?, ?, ?, 'exported')`,
			sid(n.id), strconv.FormatInt(n.id, 10), n.file, n.name, n.line, n.line+5,
		); err != nil {
			t.Fatalf("insert resolution_symbol %d: %v", n.id, err)
		}
	}

	edges := []struct {
		src, tgt int64
		tier     string
		conf     float64
	}{
		{2, 3, "CERTIFIED", 1.0},
		{3, 4, "CERTIFIED", 0.95},
		{2, 5, "CERTIFIED", 0.9},
		{2, 6, "CANDIDATE", 0.6}, // must never be walked
		{6, 7, "CERTIFIED", 1.0}, // reachable only through the CANDIDATE hop
		{9, 5, "CANDIDATE", 0.6}, // the only edge out of n9
		{10, 11, "CERTIFIED", 1.0},
		{11, 12, "CERTIFIED", 1.0},
		{12, 13, "CERTIFIED", 1.0},
		{13, 14, "CERTIFIED", 1.0},
	}
	for i, e := range edges {
		if _, err := db.Exec(
			`INSERT INTO edges (id, source_id, target_id, type, resolution_method, confidence, trust_tier)
			 VALUES (?,?,?, 'CALLS', 'import', ?, ?)`,
			i+1, e.src, e.tgt, e.conf, e.tier,
		); err != nil {
			t.Fatalf("insert edge %d: %v", i, err)
		}
	}

	assertions := []struct {
		id     int64
		test   int64
		target int64
		kind   string
	}{
		{1, 1, 2, "throws"},
		{2, 1, 8, "throws"},
		{3, 1, 9, "equals"},
		{4, 1, 0, "equals"},
		{5, 1, 10, "equals"},
	}
	for _, a := range assertions {
		if _, err := db.Exec(
			`INSERT INTO assertions (id, test_node_id, target_node_id, resolution_score, kind, expression, line)
			 VALUES (?,?,?, 3.5, ?, 'expect(x).toThrow()', 1)`,
			a.id, a.test, a.target, a.kind,
		); err != nil {
			t.Fatalf("insert assertion %d: %v", a.id, err)
		}
	}
	return db
}

// sid is the fixture's stable id for a node id.
func sid(nodeID int64) string {
	return "sid-" + string(rune('a'+nodeID-1))
}

// paths renders a result as "entry>...>terminal@witness" strings so an
// expectation reads as the flow it asserts.
func paths(res Result) []string {
	out := make([]string, 0, len(res.Processes))
	for _, p := range res.Processes {
		out = append(out, strings.Join(p.Path, ">")+"@"+itoa(p.WitnessAssertionID))
	}
	return out
}

func itoa(v int64) string {
	if v == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return string(buf[i:])
}

func derive(t *testing.T, db *sql.DB, opts Options) Result {
	t.Helper()
	res, err := Derive(context.Background(), db, opts)
	if err != nil {
		t.Fatalf("Derive: %v", err)
	}
	return res
}

// (a) Only CERTIFIED paths are used, and (c) a path through a CANDIDATE edge is
// rejected even when every other hop on it is certified.
func TestDeriveWalksCertifiedEdgesOnly(t *testing.T) {
	db := newFixture(t)
	res := derive(t, db, Options{})

	got := paths(res)
	want := []string{
		// witness a1 from entry n2 (sid-b)
		"sid-b>sid-c>sid-d@1", // entry -> mid -> sink_deep
		"sid-b>sid-e@1",       // entry -> sink_shallow
		// witness a5 from deep0 (sid-j); truncated at MaxDepth 3, so deep4 is
		// unreachable and no terminal is found -- see the MaxDepth test.
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("processes = %v; want %v", got, want)
	}

	// n7 (candidate_sink, sid-g) is reachable from the entry ONLY through the
	// CANDIDATE edge n2->n6. If it appears, the walk laundered a candidate.
	for _, p := range res.Processes {
		for _, step := range p.Path {
			if step == sid(6) || step == sid(7) {
				t.Fatalf("process %v traverses a CANDIDATE-only region", p.Path)
			}
		}
		if p.TrustFloor != TrustFloorCertified {
			t.Fatalf("trust floor = %q; want %q", p.TrustFloor, TrustFloorCertified)
		}
		if p.WitnessAssertionID == 0 {
			t.Fatal("emitted a process with no witnessing assertion")
		}
	}
}

// (b) A target with no assertion yields no process, and a target whose only
// outgoing edge is a CANDIDATE yields no process.
func TestDeriveEmitsNothingWithoutAWitness(t *testing.T) {
	db := newFixture(t)

	// n3 (mid) has a certified path to a terminal but no assertion names it.
	// Nothing may be emitted for it.
	res := derive(t, db, Options{})
	for _, p := range res.Processes {
		if p.EntryStableID == sid(3) {
			t.Fatalf("emitted an unwitnessed process from %s", sid(3))
		}
	}

	// Strip every assertion and the derivation must produce nothing and say
	// why, rather than falling back to guessing entry points.
	if _, err := db.Exec(`DELETE FROM assertions`); err != nil {
		t.Fatal(err)
	}
	res = derive(t, db, Options{})
	if len(res.Processes) != 0 {
		t.Fatalf("processes = %d with no assertions; want 0", len(res.Processes))
	}
	if res.Reason != ReasonNoAssertions {
		t.Fatalf("reason = %q; want %q", res.Reason, ReasonNoAssertions)
	}
	if res.AssertionsScanned != 0 {
		t.Fatalf("AssertionsScanned = %d; want 0", res.AssertionsScanned)
	}
}

// Targets that witness nothing are counted, not silently dropped.
func TestDeriveAccounting(t *testing.T) {
	db := newFixture(t)
	res := derive(t, db, Options{})

	if res.AssertionsScanned != 5 {
		t.Fatalf("AssertionsScanned = %d; want 5", res.AssertionsScanned)
	}
	if res.AssertionsWithTarget != 4 {
		t.Fatalf("AssertionsWithTarget = %d; want 4", res.AssertionsWithTarget)
	}
	// n8 (sink), n9 (candidate-only) and n10 (chain longer than MaxDepth).
	if res.TargetsWithoutCertifiedPath != 3 {
		t.Fatalf("TargetsWithoutCertifiedPath = %d; want 3", res.TargetsWithoutCertifiedPath)
	}
	if res.Reason != "" {
		t.Fatalf("Reason = %q; want empty when processes were emitted", res.Reason)
	}
	if res.Truncated {
		t.Fatal("Truncated set without a MaxProcesses bound being hit")
	}
}

// The empty-result reason distinguishes "no certified edges" from "no path".
func TestDeriveReasonsForEmptyResults(t *testing.T) {
	t.Run("no linked targets", func(t *testing.T) {
		db := newFixture(t)
		if _, err := db.Exec(`UPDATE assertions SET target_node_id = 0`); err != nil {
			t.Fatal(err)
		}
		res := derive(t, db, Options{})
		if res.Reason != ReasonNoLinkedTargets {
			t.Fatalf("reason = %q; want %q", res.Reason, ReasonNoLinkedTargets)
		}
		if res.AssertionsScanned != 5 {
			t.Fatalf("AssertionsScanned = %d; want 5", res.AssertionsScanned)
		}
	})

	t.Run("no certified edges", func(t *testing.T) {
		db := newFixture(t)
		if _, err := db.Exec(`UPDATE edges SET trust_tier = 'CANDIDATE'`); err != nil {
			t.Fatal(err)
		}
		res := derive(t, db, Options{})
		if res.Reason != ReasonNoCertifiedEdges {
			t.Fatalf("reason = %q; want %q", res.Reason, ReasonNoCertifiedEdges)
		}
	})

	t.Run("no certified path", func(t *testing.T) {
		db := newFixture(t)
		// Point every assertion at the lonely sink.
		if _, err := db.Exec(`UPDATE assertions SET target_node_id = 8`); err != nil {
			t.Fatal(err)
		}
		res := derive(t, db, Options{})
		if len(res.Processes) != 0 {
			t.Fatalf("processes = %d; want 0", len(res.Processes))
		}
		if res.Reason != ReasonNoCertifiedPath {
			t.Fatalf("reason = %q; want %q", res.Reason, ReasonNoCertifiedPath)
		}
	})

	t.Run("no resolvable stable ids", func(t *testing.T) {
		db := newFixture(t)
		if _, err := db.Exec(`DELETE FROM resolution_symbols`); err != nil {
			t.Fatal(err)
		}
		res := derive(t, db, Options{})
		if len(res.Processes) != 0 {
			t.Fatalf("processes = %d; want 0", len(res.Processes))
		}
		if res.Reason != ReasonNoStableIDs {
			t.Fatalf("reason = %q; want %q", res.Reason, ReasonNoStableIDs)
		}
	})
}

// (d) Determinism: two runs over the same graph are identical, including ids.
func TestDeriveIsDeterministic(t *testing.T) {
	db := newFixture(t)

	first := derive(t, db, Options{})
	for i := 0; i < 5; i++ {
		next := derive(t, db, Options{})
		if !reflect.DeepEqual(first, next) {
			t.Fatalf("run %d differs from run 0:\n%+v\n%+v", i+1, next, first)
		}
	}

	// Ids must be a pure function of the witness and the path, not of
	// insertion order or map iteration.
	for _, p := range first.Processes {
		if got := processID(p.WitnessAssertionID, p.Path); got != p.ID {
			t.Fatalf("id %s is not the content hash %s of its own contents", p.ID, got)
		}
	}
}

// (e) The MaxDepth bound is respected, and the bound truncates rather than
// manufacturing a terminal at the cut point.
func TestDeriveRespectsMaxDepth(t *testing.T) {
	db := newFixture(t)

	// The deep chain n10..n14 is 4 hops. At the default depth of 3 its only
	// sink (n14) is out of reach, so no process is emitted for it AND no
	// intermediate node is passed off as a terminal.
	res := derive(t, db, Options{})
	for _, p := range res.Processes {
		if p.EntryStableID == sid(10) {
			t.Fatalf("emitted %v from the over-long chain at MaxDepth 3", p.Path)
		}
		if p.Depth > DefaultMaxDepth {
			t.Fatalf("process depth %d exceeds MaxDepth %d", p.Depth, DefaultMaxDepth)
		}
		if p.Depth < MinProcessDepth {
			t.Fatalf("process depth %d below MinProcessDepth %d", p.Depth, MinProcessDepth)
		}
		if len(p.Path) != p.Depth+1 {
			t.Fatalf("path length %d does not match depth %d", len(p.Path), p.Depth)
		}
	}

	// Raising the bound to 4 reaches the chain's sink.
	deep := derive(t, db, Options{MaxDepth: 4})
	var found bool
	for _, p := range deep.Processes {
		if p.EntryStableID == sid(10) {
			found = true
			if p.Depth != 4 || p.TerminalStableID != sid(14) {
				t.Fatalf("chain process = depth %d terminal %s; want depth 4 terminal %s",
					p.Depth, p.TerminalStableID, sid(14))
			}
		}
	}
	if !found {
		t.Fatal("MaxDepth 4 did not reach the depth-4 chain terminal")
	}

	// Lowering the bound to 1 keeps only the single-hop flow.
	shallow := derive(t, db, Options{MaxDepth: 1})
	if got, want := paths(shallow), []string{"sid-b>sid-e@1"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("MaxDepth 1 processes = %v; want %v", got, want)
	}

	// Options zero values must fall back to the documented defaults, never to
	// an unbounded walk.
	var zero Options
	if zero.maxDepth() != DefaultMaxDepth ||
		zero.minConfidence() != DefaultMinConfidence ||
		zero.maxProcesses() != DefaultMaxProcesses {
		t.Fatal("zero Options did not resolve to the documented defaults")
	}
	neg := Options{MaxDepth: -1, MinConfidence: -1, MaxProcesses: -1}
	if neg.maxDepth() != DefaultMaxDepth ||
		neg.minConfidence() != DefaultMinConfidence ||
		neg.maxProcesses() != DefaultMaxProcesses {
		t.Fatal("negative Options did not resolve to the documented defaults")
	}
}

// MaxProcesses truncates and says so.
func TestDeriveRespectsMaxProcesses(t *testing.T) {
	db := newFixture(t)
	res := derive(t, db, Options{MaxProcesses: 1})
	if len(res.Processes) != 1 {
		t.Fatalf("processes = %d; want 1", len(res.Processes))
	}
	if !res.Truncated {
		t.Fatal("Truncated not set when MaxProcesses bound the result")
	}
}

// The confidence floor gates on top of the tier, so a CERTIFIED-tier edge with
// an implausibly low stored confidence still cannot be walked.
func TestDeriveAppliesConfidenceFloor(t *testing.T) {
	db := newFixture(t)
	if _, err := db.Exec(`UPDATE edges SET confidence = 0.4 WHERE source_id = 2 AND target_id = 5`); err != nil {
		t.Fatal(err)
	}
	res := derive(t, db, Options{})
	for _, p := range res.Processes {
		if p.TerminalStableID == sid(5) && p.Depth == 1 {
			t.Fatal("walked a CERTIFIED edge below the confidence floor")
		}
	}
}

// (f) Persist round-trips, and the NOT NULL witness column rejects a NULL.
func TestPersistRoundTrip(t *testing.T) {
	db := newFixture(t)
	res := derive(t, db, Options{})
	if len(res.Processes) == 0 {
		t.Fatal("fixture produced no processes to persist")
	}

	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(tx, res); err != nil {
		t.Fatalf("Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	loaded, err := Load(db)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if !reflect.DeepEqual(loaded, res.Processes) {
		t.Fatalf("round trip mismatch:\n got %+v\nwant %+v", loaded, res.Processes)
	}

	// The acceptance criterion from the plan, run as a query.
	var unwitnessed int
	if err := db.QueryRow(`SELECT count(*) FROM processes WHERE witness_assertion_id IS NULL`).Scan(&unwitnessed); err != nil {
		t.Fatal(err)
	}
	if unwitnessed != 0 {
		t.Fatalf("processes with a null witness = %d; want 0", unwitnessed)
	}

	// Persist is a replace, not an append: a second identical run leaves the
	// same row count rather than doubling it.
	tx, err = db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if err := Persist(tx, res); err != nil {
		t.Fatalf("second Persist: %v", err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRow(`SELECT count(*) FROM processes`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != len(res.Processes) {
		t.Fatalf("processes after re-persist = %d; want %d", count, len(res.Processes))
	}
}

// The invariant is in the schema, not only in the code: the database itself
// refuses an unwitnessed process.
func TestSchemaRejectsNullWitness(t *testing.T) {
	db := newFixture(t)

	_, err := db.Exec(`
		INSERT INTO processes
			(id, entry_stable_id, terminal_stable_id, witness_assertion_id,
			 test_stable_id, kind, depth, trust_floor)
		VALUES ('p-null', 'sid-b', 'sid-e', NULL, 'sid-a', 'throws', 1, 'CERTIFIED')`)
	if err == nil {
		t.Fatal("inserted a process with a NULL witness_assertion_id; the NOT NULL constraint is missing")
	}
	if !strings.Contains(strings.ToUpper(err.Error()), "NOT NULL") {
		t.Fatalf("insert failed for the wrong reason: %v", err)
	}

	// And the write path refuses before it reaches the driver.
	tx, err := db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	err = Persist(tx, Result{Processes: []Process{{
		ID:               "p-unwitnessed",
		EntryStableID:    "sid-b",
		TerminalStableID: "sid-e",
		Path:             []string{"sid-b", "sid-e"},
		TestStableID:     "sid-a",
		Depth:            1,
		TrustFloor:       TrustFloorCertified,
	}}})
	if err == nil {
		t.Fatal("Persist accepted a process with no witnessing assertion")
	}
	if !strings.Contains(err.Error(), "no witnessing assertion") {
		t.Fatalf("Persist failed for the wrong reason: %v", err)
	}
}

// The closure sidecar cannot supply a path, which is why the walk runs over
// edges. Pin the schema fact the decision rests on so a future closure that
// does carry path detail forces this test -- and the decision -- to be revisited.
func TestClosureCarriesNoPathDetail(t *testing.T) {
	db := newFixture(t)

	rows, err := db.Query(`SELECT name FROM pragma_table_info('closure')`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()

	var cols []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			t.Fatal(err)
		}
		cols = append(cols, name)
	}
	want := []string{"source_id", "target_id", "depth", "min_confidence"}
	if !reflect.DeepEqual(cols, want) {
		t.Fatalf("closure columns = %v; want %v -- if the closure now carries "+
			"intermediate nodes, Derive should walk it instead of `edges`", cols, want)
	}
}
