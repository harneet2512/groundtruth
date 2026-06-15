package resolver

import (
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// promoteFixture builds a tiny graph.db with nodes + the seven promotable
// property kinds, mirroring the gt_gt §2.6 RED->GREEN depth proof on a controlled
// input. It exercises BOTH the resolvable (-> edge) and unresolvable (-> stays
// property) branch of each class so the non-invention assertion is meaningful.
//
//	nodes:
//	  10 Class    Serializer       ser.py:1
//	  11 Method   to_json          ser.py:5   parent=10
//	  12 Method   from_json        ser.py:12  parent=10
//	  13 Class    MyError          err.py:1
//	  14 Method   validate         err.py:5   parent=13
//	  15 Function helper           helpers.py:1
//	  16 Function caller           helpers.py:10
//	  17 Class    errors           errpkg.py:1   (decoy: the module-prefix a dotted
//	                                              'errors.New' must NOT be reduced onto)
func promoteFixture(t *testing.T) *store.DB {
	t.Helper()
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}

	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (10,'Class',   'Serializer','ser.py',     1,'',                 'python',0),
	  (11,'Method',  'to_json',   'ser.py',     5,'def to_json(self)','python',10),
	  (12,'Method',  'from_json', 'ser.py',    12,'def from_json(s)', 'python',10),
	  (13,'Class',   'MyError',   'err.py',     1,'',                 'python',0),
	  (14,'Method',  'validate',  'err.py',     5,'def validate(self)','python',13),
	  (15,'Function','helper',    'helpers.py', 1,'def helper()',     'python',0),
	  (16,'Function','caller',    'helpers.py',10,'def caller()',     'python',0),
	  (17,'Class',   'errors',    'errpkg.py',  1,'',                 'python',0)`)

	// class_field declared on MyError (id 13) — the class that OWNS validate
	// (id 14). This lifts validate's self.count READS/WRITES confidence to 0.9
	// (the read/written field is a declared field of the owning class).
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (13,'class_field','count: int',2,1.0)`)

	// CO_SERIALIZES: to_json <-> from_json (two rows, undirected => ONE edge).
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (11,'serialization_pair','partner:from_json@file:12|sig:def from_json(s):',5,0.8),
	  (12,'serialization_pair','partner:to_json@file:5|sig:def to_json(self):',12,0.8)`)

	// READS: validate reads self.count — a declared class_field of MyError, the
	// owning class => ONE edge validate(14) -> MyError(13) at conf 0.9. (Multiple
	// field reads from the same method to the same owning class collapse to one
	// READS relation; the field name rides in metadata — that is correct edge-
	// level dedup, not a dropped fact.)
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (14,'field_read','reads: self.count [in_condition]',6,0.9)`)
	// READS unresolvable: helper (a Function, parent=0) has no owning Class -> STAYS property.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (15,'field_read','reads: x.field [in_return]',2,0.9)`)

	// WRITES: validate mutates self.count -> MyError.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (14,'side_effect','mutates: self.count = count',8,1.0)`)
	// WRITES unresolvable: a non-field side_effect (no 'recv.field =' shape) -> STAYS property.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (15,'side_effect','calls external subprocess.run',3,1.0)`)

	// RAISES: validate raises MyError (internal class -> edge), ValueError (builtin ->
	// NO edge), and 'errors.New' (DOTTED/qualified -> DROPPED, must NOT reduce to the
	// module-prefix 'errors' (node 17) and mint a wrong edge — the D3 drop-dotted bar).
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (14,'exception_type','MyError',9,1.0),
	  (14,'exception_type','ValueError',10,1.0),
	  (14,'exception_type','errors.New',11,1.0)`)

	// DATA_FLOW: caller -> helper() (resolvable, unique => 0.8). And a pure value
	// flow with no callee -> SUPPRESS.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (16,'data_flow','x -> helper(x) | return x',11,0.8),
	  (16,'data_flow','y -> y + 1',12,0.8)`)

	// PRECEDES: caller does helper -> validate (two distinct internal nodes).
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (16,'call_order','self: helper -> validate',13,0.6)`)

	// USES: caller's call to helper is iterated (annotate the CALLS edge if it exists).
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (16,'caller_usage','iterated:helper|for r in helper(x)',11,0.8)`)
	// Pre-existing CALLS edge caller(16) -> helper(15) for USES to annotate.
	execSQL(t, db, `INSERT INTO edges (source_id,target_id,type,resolution_method,confidence,trust_tier)
	  VALUES (16,15,'CALLS','import',1.0,'CERTIFIED')`)

	return db
}

func countEdges(t *testing.T, db *store.DB, where string) int {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var n int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM edges WHERE ` + where).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestPromote_RedGreenDepth(t *testing.T) {
	db := promoteFixture(t)
	defer db.Close()

	// RED: no promoted edges before the pass.
	if got := countEdges(t, db, `resolution_method LIKE 'promote_%'`); got != 0 {
		t.Fatalf("RED precondition: expected 0 promoted edges, got %d", got)
	}

	propsBefore := propertyCounts(t, db)

	n, err := PromotePropertyEdges(db)
	if err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}
	if n == 0 {
		t.Fatal("GREEN: expected promoted edges, got 0")
	}

	// Completeness per class (controlled-input exact counts).
	cases := []struct {
		typ  string
		want int
	}{
		{"CO_SERIALIZES", 1}, // 2 rows, undirected -> 1
		{"READS", 1},         // validate -> MyError (self.count)
		{"WRITES", 1},        // validate -> MyError (self.count)
		{"RAISES", 1},        // MyError only (ValueError builtin excluded)
		{"DATA_FLOW", 0},     // D1: caller->helper RIDES the pre-existing CALLS 16->15
		// edge (annotated, not minted); the value flow 'y -> y+1' has no callee. So
		// ZERO standalone DATA_FLOW edges — the hop is a CALLS.metadata annotation
		// (asserted below), per gt_gt §2.6 line 262.
		{"PRECEDES", 1}, // helper -> validate
	}
	for _, c := range cases {
		if got := countEdges(t, db, `type = '`+c.typ+`' AND resolution_method LIKE 'promote_%'`); got != c.want {
			t.Errorf("%s: want %d edges, got %d", c.typ, c.want, got)
		}
	}

	// Traversability: ZERO orphans (every promoted edge JOINs nodes on both ends).
	orphans := orphanCount(t, db)
	if orphans != 0 {
		t.Errorf("traversability: %d orphan promoted edges (target/source_id with no node)", orphans)
	}

	// Non-invention: no RAISES edge points at a builtin name target.
	if got := countEdges(t, db, `type='RAISES' AND resolution_method LIKE 'promote_%' AND target_id IN (SELECT id FROM nodes WHERE name='ValueError')`); got != 0 {
		t.Errorf("non-invention: %d RAISES edges minted onto builtin ValueError", got)
	}

	// D3 drop-dotted: 'errors.New' must be DROPPED, never reduced to its module prefix
	// 'errors' and minted onto the same-named project class (node 17). ZERO such edges.
	// (RED before the fix: cleanExceptionBase cut 'errors.New'->'errors' -> a wrong edge.)
	if got := countEdges(t, db, `type='RAISES' AND resolution_method LIKE 'promote_%' AND target_id=17`); got != 0 {
		t.Errorf("D3 drop-dotted: %d RAISES edges minted onto module-prefix class 'errors' from 'errors.New'", got)
	}

	// Trust tiers from §2.3 thresholds:
	//   READS self.count -> MyError, count is a declared class_field => 0.9 CERTIFIED.
	//   WRITES self.count -> MyError, same => 0.9 CERTIFIED.
	//   PRECEDES -> 0.5 CANDIDATE (call_order is weak ordering evidence).
	assertTier(t, db, "READS", 13, "count", 0.9, "CERTIFIED")
	assertTier(t, db, "WRITES", 13, "count", 0.9, "CERTIFIED")
	assertTier(t, db, "PRECEDES", 0, "", 0.5, "CANDIDATE")

	// Additive: properties row counts unchanged (contract_map.py read preserved).
	propsAfter := propertyCounts(t, db)
	for k, before := range propsBefore {
		if propsAfter[k] != before {
			t.Errorf("additive violation: property kind %q changed %d -> %d", k, before, propsAfter[k])
		}
	}

	// USES: the existing CALLS edge (16->15) carries the usage annotation.
	if !callsEdgeHasUsage(t, db, 16, 15, "usage=iterated") {
		t.Error("USES: CALLS edge 16->15 not annotated with usage=iterated")
	}

	// DATA_FLOW (D1): the caller->helper hop has a pre-existing CALLS 16->15 edge, so
	// it is ANNOTATED onto that edge's metadata, NOT minted as a standalone edge.
	if !callsEdgeHasUsage(t, db, 16, 15, "dataflow=helper") {
		t.Error("DATA_FLOW: CALLS edge 16->15 not annotated with dataflow=helper")
	}

	// IDEMPOTENT: a second run converges to the identical promoted-edge count.
	firstCount := countEdges(t, db, `resolution_method LIKE 'promote_%'`)
	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("re-run PromotePropertyEdges: %v", err)
	}
	secondCount := countEdges(t, db, `resolution_method LIKE 'promote_%'`)
	if firstCount != secondCount {
		t.Errorf("idempotency: promoted edge count drifted %d -> %d on re-run", firstCount, secondCount)
	}
}

// TestPromote_RaisesThrowFlow_GapA proves GAP A: a conditional `throw [new] <Type>`
// (JS/TS/Java) carried in an `exception_flow` value now mints a RAISES edge to the
// internal error class — which the raise-only regex used to miss — while keeping ALL
// non-invention guards: builtin throws, dotted throws, and Go `panic(...)` (a value, not
// a named internal class) stay properties.
func TestPromote_RaisesThrowFlow_GapA(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// 20 Class InternalError (internal JS error class), 21 Method handle (raiser),
	// 22 Class RangeError-decoy is a BUILTIN name (must NOT mint even if a node exists).
	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (20,'Class', 'InternalError','e.js', 1,'',                'javascript',0),
	  (21,'Method','handle',       'e.js', 5,'handle(req)',     'javascript',20),
	  (22,'Class', 'RangeError',   'e.js',20,'',                'javascript',0)`)

	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (21,'exception_flow','WHEN x < 0: throw new InternalError("bad")',6,1.0),
	  (21,'exception_flow','WHEN y == 0: throw new RangeError("oob")',7,1.0),
	  (21,'exception_flow','WHEN z: throw errors.Wrap(e)',8,1.0),
	  (21,'exception_flow','WHEN q: panic("boom")',9,1.0)`)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}

	// GREEN: the `throw new InternalError` flow mints exactly ONE RAISES edge 21->20.
	if got := countEdges(t, db, `type='RAISES' AND source_id=21 AND target_id=20 AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("GAP A: throw-new-InternalError must mint 1 RAISES edge 21->20, got %d", got)
	}
	// Non-invention: builtin RangeError (even with a same-named node) mints NO edge.
	if got := countEdges(t, db, `type='RAISES' AND target_id=22 AND resolution_method LIKE 'promote_%'`); got != 0 {
		t.Errorf("non-invention: builtin RangeError throw must mint 0 edges, got %d", got)
	}
	// Dotted `errors.Wrap` and value `panic(...)` -> NO RAISES edge at all beyond the one above.
	if got := countEdges(t, db, `type='RAISES' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Errorf("only the internal InternalError throw resolves; dotted/panic stay properties; total RAISES want 1, got %d", got)
	}
}

// TestPromote_PrecedesAmbiguousDemoted proves the honest-trust demotion branch
// (promote.go:833-836, §2.6 non-invention): when a `call_order` step's callee name is
// AMBIGUOUS (>1 same-named candidate across files), resolveByName returns an arbitrary
// first-match, so the minted PRECEDES edge must carry the REAL candidate_count (>1) and be
// demoted to confidence 0.4 / tier SPECULATIVE — BELOW the 0.5 consumer gate, so it is
// filtered, not laundered as a fact. The RedGreenDepth fixture only exercises the cc=1 /
// 0.5 / CANDIDATE path; this is the missing red→green for the demotion's whole point.
func TestPromote_PrecedesAmbiguousDemoted(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// `step` is DEFINED TWICE (a.py and b.py) -> the call_order callee "step" is ambiguous
	// (cc=2). `setup` is unique. The chain `setup -> step` mints setup(20) -> step(21,
	// same-file first-match) but at the ambiguous confidence.
	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (20,'Function','setup','a.py',1,'def setup()','python',0),
	  (21,'Function','step', 'a.py',5,'def step()', 'python',0),
	  (22,'Function','step', 'b.py',5,'def step()', 'python',0)`)
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (20,'call_order','self: setup -> step',2,0.6)`)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}

	// Exactly ONE promoted PRECEDES edge (setup -> the same-file step), and it is the
	// DEMOTED one: candidate_count=2, confidence 0.4, tier SPECULATIVE.
	if got := countEdges(t, db, `type='PRECEDES' AND resolution_method LIKE 'promote_%'`); got != 1 {
		t.Fatalf("want exactly 1 promoted PRECEDES edge, got %d", got)
	}
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var (
		srcID, tgtID int64
		cc           int
		conf         float64
		tier         string
	)
	if err := tx.QueryRow(
		`SELECT source_id, target_id, candidate_count, confidence, trust_tier
		   FROM edges WHERE type='PRECEDES' AND resolution_method LIKE 'promote_%' LIMIT 1`,
	).Scan(&srcID, &tgtID, &cc, &conf, &tier); err != nil {
		t.Fatalf("read PRECEDES edge: %v", err)
	}
	if srcID != 20 || tgtID != 21 {
		t.Errorf("PRECEDES endpoints: want 20->21 (same-file step), got %d->%d", srcID, tgtID)
	}
	if cc <= 1 {
		t.Errorf("ambiguous step: want candidate_count>1, got %d (the real ambiguity must be carried, not stamped 1)", cc)
	}
	if conf != 0.4 {
		t.Errorf("ambiguous PRECEDES: want confidence 0.4 (demoted), got %.2f", conf)
	}
	if tier != "SPECULATIVE" {
		t.Errorf("ambiguous PRECEDES: want tier SPECULATIVE (below the 0.5 gate), got %q", tier)
	}
}

func propertyCounts(t *testing.T, db *store.DB) map[string]int {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT kind, COUNT(*) FROM properties GROUP BY kind`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[string]int)
	for rows.Next() {
		var k string
		var n int
		if err := rows.Scan(&k, &n); err != nil {
			t.Fatal(err)
		}
		out[k] = n
	}
	return out
}

func orphanCount(t *testing.T, db *store.DB) int {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var n int
	if err := tx.QueryRow(`SELECT COUNT(*) FROM edges e
	    LEFT JOIN nodes s ON s.id = e.source_id
	    LEFT JOIN nodes t ON t.id = e.target_id
	    WHERE e.resolution_method LIKE 'promote_%' AND (s.id IS NULL OR t.id IS NULL)`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func assertTier(t *testing.T, db *store.DB, typ string, target int64, meta string, wantConf float64, wantTier string) {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	q := `SELECT confidence, trust_tier FROM edges WHERE type = ? AND resolution_method LIKE 'promote_%'`
	args := []interface{}{typ}
	if target != 0 {
		q += ` AND target_id = ? AND metadata = ?`
		args = append(args, target, meta)
	}
	q += ` LIMIT 1`
	var conf float64
	var tier string
	if err := tx.QueryRow(q, args...).Scan(&conf, &tier); err != nil {
		t.Errorf("assertTier %s/%s: no row: %v", typ, meta, err)
		return
	}
	if conf != wantConf || tier != wantTier {
		t.Errorf("%s/%s: want conf=%.2f tier=%s, got conf=%.2f tier=%s", typ, meta, wantConf, wantTier, conf, tier)
	}
}

func callsEdgeHasUsage(t *testing.T, db *store.DB, src, tgt int64, tag string) bool {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var meta string
	if err := tx.QueryRow(`SELECT COALESCE(metadata,'') FROM edges WHERE source_id=? AND target_id=? AND type='CALLS'`,
		src, tgt).Scan(&meta); err != nil {
		return false
	}
	return meta != "" && containsSubstr(meta, tag)
}

func containsSubstr(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
