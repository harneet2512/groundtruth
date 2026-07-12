package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// END-TO-END (whole binary) proof of the Go receiver-type resolution lever, driven through
// the compiled gt-index (parse → resolve → store → graph.db), NOT the resolver in isolation.
// The B2 typeflow-fixpoint unit tests hand-feed AssignmentRefs + NodeMeta, so they PASS even
// though — before the parser expression_list unwrap — the Go binary produced ZERO assignments
// and the whole assignment-based receiver-typing family (Strategy 1.96 + the fixpoint) was
// dead on Go. This test closes that Integration/Plumbing gap on a real Go source fixture.
//
// The fixture is a CHAINED receiver flow whose deepest hop only the fixpoint can type:
//
//	func driver() {
//	    m := makeMaker()   // m : Maker    (base seed;  bare factory return)
//	    t := m.build()     // t : Thing    (depth-1 chain; qualified receiver "m.build")
//	    t.run()            // <-- resolves to Thing.run ONLY once t:Thing is inferred
//	}
//
// `run` is defined in FOUR structs (Thing + 3 decoys) so neither the few-implementor
// (cap 3) nor unique-method rung can fire — only the fixpoint proves t:Thing. Assertions:
//   - flag OFF  → t.run() is name_match (chained receiver unproven).
//   - flag ON   → t.run() is type_flow/fixpoint onto Thing.run at conf 0.9 (the RIGHT target).
//   - byte-identical-off: the OFF vs ON edge sets differ in EXACTLY the t.run() edge; every
//     other edge is identical, and two OFF indexings are identical (determinism).
//   - flag-INDEPENDENT win: `m.build()` is type_flow/assignment_tracked in BOTH modes —
//     proof the parser unwrap resurrected single-shot 1.96 receiver typing on Go (pre-fix it
//     was an impl_method single_implementor guess with an UNPROVEN receiver).
func TestReceiverFixpoint_GoChainedReceiver_EndToEnd(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}

	tmp := t.TempDir()
	repo := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	// One Go file. `run` in 4 structs (Thing + Alpha/Beta/Gamma) forces the ambiguity that
	// makes the fixpoint the ONLY rung able to resolve t.run().
	src := "package app\n\n" +
		"type Thing struct{}\n\n" +
		"func (t Thing) run() int { return 1 }\n\n" +
		"type Maker struct{}\n\n" +
		"func (m Maker) build() Thing { return Thing{} }\n\n" +
		"type Alpha struct{}\n\n" +
		"func (a Alpha) run() int { return 2 }\n\n" +
		"type Beta struct{}\n\n" +
		"func (b Beta) run() int { return 3 }\n\n" +
		"type Gamma struct{}\n\n" +
		"func (g Gamma) run() int { return 4 }\n\n" +
		"func makeMaker() Maker { return Maker{} }\n\n" +
		"func driver() {\n" +
		"\tm := makeMaker()\n" +
		"\tt := m.build()\n" +
		"\tt.run()\n" +
		"}\n"
	if err := os.WriteFile(filepath.Join(repo, "app.go"), []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}

	bin := filepath.Join(tmp, "gt-index")
	if runtime.GOOS == "windows" {
		bin += ".exe"
	}
	build := exec.Command("go", "build", "-tags", "sqlite_fts5", "-o", bin, ".")
	build.Env = append(os.Environ(), "CGO_ENABLED=1")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build gt-index: %v\n%s", err, out)
	}

	offDB := filepath.Join(tmp, "off.db")
	off2DB := filepath.Join(tmp, "off2.db")
	onDB := filepath.Join(tmp, "on.db")

	indexInto := func(db string, fixpointOn bool) {
		t.Helper()
		cmd := exec.Command(bin, "-root", repo, "-output", db)
		// Build a clean env: strip any inherited GT_TYPEFLOW_FIXPOINT so OFF is truly off.
		env := make([]string, 0, len(os.Environ())+2)
		for _, kv := range os.Environ() {
			if strings.HasPrefix(kv, "GT_TYPEFLOW_FIXPOINT=") {
				continue
			}
			env = append(env, kv)
		}
		env = append(env, "CGO_ENABLED=1")
		if fixpointOn {
			env = append(env, "GT_TYPEFLOW_FIXPOINT=1")
		}
		cmd.Env = env
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("index (fixpoint=%v): %v\n%s", fixpointOn, err, out)
		}
	}

	indexInto(offDB, false)
	indexInto(off2DB, false)
	indexInto(onDB, true)

	// t.run() — the chained call. OFF must be name_match; ON must be type_flow/fixpoint to
	// Thing.run (the target whose start_line is that of the FIRST `run`, id-lowest) at 0.9.
	offMethod, _, _ := driverRunEdge(t, offDB)
	if offMethod != "name_match" {
		t.Fatalf("OFF: chained t.run() must be name_match (chained receiver unproven without the fixpoint); got %q", offMethod)
	}
	onMethod, onEvidence, onConf := driverRunEdge(t, onDB)
	if onMethod != "type_flow" || onEvidence != "fixpoint" {
		t.Fatalf("ON: chained t.run() must resolve type_flow/fixpoint; got method=%q evidence=%q", onMethod, onEvidence)
	}
	if onConf != 0.9 {
		t.Errorf("ON: fixpoint edge must carry conf 0.9 (the 1.96 type_flow FACT boundary); got %v", onConf)
	}
	if tgtLine := driverRunTargetLine(t, onDB); tgtLine != thingRunLine(t, onDB) {
		t.Errorf("ON: fixpoint must resolve to Thing.run specifically; target start_line=%d, Thing.run start_line=%d", tgtLine, thingRunLine(t, onDB))
	}

	// Flag-INDEPENDENT win: m.build() has a PROVEN receiver in both modes (parser unwrap
	// resurrected single-shot 1.96). Pre-fix this was impl_method/single_implementor.
	for _, db := range []string{offDB, onDB} {
		if m := buildEdgeMethod(t, db); m != "type_flow" {
			t.Errorf("m.build() must be type_flow (receiver proven via the extracted assignment) in %s; got %q", filepath.Base(db), m)
		}
	}

	// Byte-identical-off + determinism: OFF==OFF2, and OFF vs ON differ in EXACTLY the
	// t.run() edge (name_match → type_flow), nothing else moves.
	offEdges := allCallEdges(t, offDB)
	off2Edges := allCallEdges(t, off2DB)
	onEdges := allCallEdges(t, onDB)

	if !equalStrs(offEdges, off2Edges) {
		t.Fatalf("OFF determinism: two OFF indexings differ:\n OFF : %v\n OFF2: %v", offEdges, off2Edges)
	}

	offOnly, onOnly := symmetricDiff(offEdges, onEdges)
	if len(offOnly) != 1 || len(onOnly) != 1 {
		t.Fatalf("flag byte-identical-off: OFF↔ON must differ in EXACTLY one edge; got OFF-only=%v ON-only=%v", offOnly, onOnly)
	}
	if !strings.Contains(offOnly[0], "tgt=run") || !strings.Contains(offOnly[0], "method=name_match") {
		t.Errorf("the single OFF-only edge must be the chained t.run() name_match; got %q", offOnly[0])
	}
	if !strings.Contains(onOnly[0], "tgt=run") || !strings.Contains(onOnly[0], "method=type_flow") ||
		!strings.Contains(onOnly[0], "evidence=fixpoint") {
		t.Errorf("the single ON-only edge must be the chained t.run() type_flow/fixpoint; got %q", onOnly[0])
	}
}

// driverRunEdge returns (method, evidence_type, confidence) of the CALLS edge from `driver`
// to a `run` node (the chained call), or ("","",0) if absent.
func driverRunEdge(t *testing.T, dbPath string) (string, string, float64) {
	t.Helper()
	db := openDB(t, dbPath)
	defer db.Close()
	var method, evidence string
	var conf float64
	err := db.QueryRow(
		`SELECT COALESCE(e.resolution_method,''), COALESCE(e.evidence_type,''), e.confidence
		   FROM edges e JOIN nodes src ON e.source_id=src.id JOIN nodes tgt ON e.target_id=tgt.id
		  WHERE e.type='CALLS' AND src.name='driver' AND tgt.name='run'
		  ORDER BY e.id LIMIT 1`).Scan(&method, &evidence, &conf)
	if err == sql.ErrNoRows {
		return "", "", 0
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(method), strings.TrimSpace(evidence), conf
}

func driverRunTargetLine(t *testing.T, dbPath string) int {
	t.Helper()
	db := openDB(t, dbPath)
	defer db.Close()
	var line int
	err := db.QueryRow(
		`SELECT tgt.start_line FROM edges e JOIN nodes src ON e.source_id=src.id
		   JOIN nodes tgt ON e.target_id=tgt.id
		  WHERE e.type='CALLS' AND src.name='driver' AND tgt.name='run'
		  ORDER BY e.id LIMIT 1`).Scan(&line)
	if err != nil {
		t.Fatal(err)
	}
	return line
}

// thingRunLine returns the start_line of Thing.run (the run method whose parent is Thing).
func thingRunLine(t *testing.T, dbPath string) int {
	t.Helper()
	db := openDB(t, dbPath)
	defer db.Close()
	var line int
	err := db.QueryRow(
		`SELECT m.start_line FROM nodes m JOIN nodes c ON m.parent_id=c.id
		  WHERE m.name='run' AND c.name='Thing' LIMIT 1`).Scan(&line)
	if err != nil {
		t.Fatal(err)
	}
	return line
}

// buildEdgeMethod returns the resolution_method of the driver → build edge.
func buildEdgeMethod(t *testing.T, dbPath string) string {
	t.Helper()
	db := openDB(t, dbPath)
	defer db.Close()
	var method string
	err := db.QueryRow(
		`SELECT COALESCE(e.resolution_method,'') FROM edges e
		   JOIN nodes src ON e.source_id=src.id JOIN nodes tgt ON e.target_id=tgt.id
		  WHERE e.type='CALLS' AND src.name='driver' AND tgt.name='build'
		  ORDER BY e.id LIMIT 1`).Scan(&method)
	if err == sql.ErrNoRows {
		return ""
	}
	if err != nil {
		t.Fatal(err)
	}
	return strings.TrimSpace(method)
}

// allCallEdges returns every CALLS edge as a stable, sorted, human-readable tuple string.
func allCallEdges(t *testing.T, dbPath string) []string {
	t.Helper()
	db := openDB(t, dbPath)
	defer db.Close()
	rows, err := db.Query(
		`SELECT src.name, e.source_line, tgt.name, tgt.start_line,
		        COALESCE(e.resolution_method,''), e.confidence,
		        COALESCE(e.evidence_type,''), COALESCE(e.candidate_count,0)
		   FROM edges e JOIN nodes src ON e.source_id=src.id JOIN nodes tgt ON e.target_id=tgt.id
		  WHERE e.type='CALLS'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var srcName, tgtName, method, evidence string
		var srcLine, tgtLine, cc int
		var conf float64
		if err := rows.Scan(&srcName, &srcLine, &tgtName, &tgtLine, &method, &conf, &evidence, &cc); err != nil {
			t.Fatal(err)
		}
		out = append(out, fmt.Sprintf("src=%s@%d tgt=%s@%d method=%s conf=%.4f evidence=%s cc=%d",
			srcName, srcLine, tgtName, tgtLine, method, conf, evidence, cc))
	}
	sort.Strings(out)
	return out
}

func openDB(t *testing.T, dbPath string) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	return db
}

func equalStrs(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// symmetricDiff returns (in a not in b, in b not in a) for two sorted string slices.
func symmetricDiff(a, b []string) ([]string, []string) {
	sa := map[string]bool{}
	sb := map[string]bool{}
	for _, x := range a {
		sa[x] = true
	}
	for _, x := range b {
		sb[x] = true
	}
	var aOnly, bOnly []string
	for _, x := range a {
		if !sb[x] {
			aOnly = append(aOnly, x)
		}
	}
	for _, x := range b {
		if !sa[x] {
			bOnly = append(bOnly, x)
		}
	}
	return aOnly, bOnly
}
