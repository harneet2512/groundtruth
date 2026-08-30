package closure

import (
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// #B7: the closure is documented as "over VERIFIED CALLS" (gt_gt §2.1), but the
// old admission rule (conf >= 0.5 OR deterministic method) let 0.6 GUESSES in —
// 2-candidate name_match, ambiguous import picks — and propagated them
// transitively. The rule is now deterministic-method AND conf >= 0.7.
func TestClosure_ExcludesGuessesAdmitsVerified(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		(1, 'Function', 'a', 'a.py', 'python'),
		(2, 'Function', 'b', 'b.py', 'python'),
		(3, 'Function', 'c', 'c.py', 'python'),
		(4, 'Function', 'd', 'd.py', 'python'),
		(5, 'Method',   'e', 'e.py', 'python')`); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	edges := []*store.Edge{
		// VERIFIED chain: 1 -> 2 (same_file 1.0), 2 -> 5 (inherited 0.95).
		{SourceID: 1, TargetID: 2, Type: "CALLS", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"},
		{SourceID: 2, TargetID: 5, Type: "CALLS", ResolutionMethod: "inherited", Confidence: 0.95, TrustTier: "CERTIFIED"},
		// GUESSES that must NOT enter (or propagate through) the closure:
		// 2-candidate name_match at 0.6 — the exact bug.
		{SourceID: 2, TargetID: 3, Type: "CALLS", ResolutionMethod: "name_match", Confidence: 0.6, TrustTier: "CANDIDATE", CandidateCount: 2},
		// Ambiguous import pick at 0.6 (deterministic METHOD, sub-floor conf).
		{SourceID: 1, TargetID: 4, Type: "CALLS", ResolutionMethod: "import", Confidence: 0.6, TrustTier: "CANDIDATE", CandidateCount: 2},
		// High-conf name_match (cc==1 restore shape) — method is categorical: out.
		{SourceID: 3, TargetID: 4, Type: "CALLS", ResolutionMethod: "name_match", Confidence: 0.9, TrustTier: "CERTIFIED", CandidateCount: 1},
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		t.Fatal(err)
	}

	// minConf 0.0 so admission is decided by isVerifiedEdge alone.
	n, err := ComputeTransitiveClosure(db, "CALLS", 3, 0.0)
	if err != nil {
		t.Fatal(err)
	}
	if n == 0 {
		t.Fatal("no closure rows written")
	}

	tx2, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx2.Rollback()
	rows, err := tx2.Query(`SELECT source_id, target_id FROM closure`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := map[[2]int64]bool{}
	for rows.Next() {
		var s, tgt int64
		if err := rows.Scan(&s, &tgt); err != nil {
			t.Fatal(err)
		}
		got[[2]int64{s, tgt}] = true
	}

	// Verified reach present: 1->2 (direct), 2->5 (direct), 1->5 (2 hops).
	for _, want := range [][2]int64{{1, 2}, {2, 5}, {1, 5}} {
		if !got[want] {
			t.Errorf("verified reach %v missing from closure", want)
		}
	}
	// Guesses absent: the 0.6 name_match, its transitive extension, the 0.6
	// ambiguous import, and the 0.9 name_match.
	for _, bad := range [][2]int64{{2, 3}, {1, 3}, {1, 4}, {3, 4}, {2, 4}} {
		if got[bad] {
			t.Errorf("guess-derived reach %v admitted into the verified-only closure", bad)
		}
	}
}

// G16 (I2 invariant): the transitive closure closes over the CALLS relation
// ONLY. Promoted DEPTH edges (DATA_FLOW / READS / WRITES / RAISES /
// CO_SERIALIZES / PRECEDES, provenance `promote_%`) are intra-procedural /
// blast-fact DEPTH — they must NEVER enter the closure, so depth can never ride
// the closure into any reach/RANK consumer (invariant I2: "depth never enters
// RANK"). They are excluded twice over: by the `e.Type != edgeType` filter
// (edgeType=="CALLS") AND by `isVerifiedEdge` (promote_% is not a verified
// method). This test pins BOTH so a future consumer that unions edge types or
// loosens the method set cannot silently let intra-procedural data-flow depth
// enter ranking.
func TestClosure_ExcludesPromotedDepthEdges(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		(1, 'Function', 'a', 'a.py', 'python'),
		(2, 'Function', 'b', 'b.py', 'python'),
		(3, 'Class',    'C', 'c.py', 'python'),
		(4, 'Class',    'D', 'd.py', 'python')`); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	// The promoted DEPTH edge types — mirrors the Python D5 blacklist
	// (graph_localizer._PROMOTED_EDGE_TYPES). Each is minted at FULL confidence
	// with a promote_% method so the ONLY thing keeping it out is the I2 guard.
	depthTypes := []string{"DATA_FLOW", "READS", "WRITES", "RAISES", "CO_SERIALIZES", "PRECEDES"}

	edges := []*store.Edge{
		// VERIFIED CALLS chain that MUST be in the closure: 1 -> 2.
		{SourceID: 1, TargetID: 2, Type: "CALLS", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"},
	}
	// Layer every promoted depth edge type on top, full confidence. None may
	// enter the closure or extend reach: 2 -> 3 (depth) must not give 1 -> 3,
	// and 3 -> 4 (depth) must not appear at all.
	for _, dt := range depthTypes {
		edges = append(edges,
			&store.Edge{SourceID: 2, TargetID: 3, Type: dt, ResolutionMethod: "promote_" + dt, Confidence: 1.0, TrustTier: "CERTIFIED"},
			&store.Edge{SourceID: 3, TargetID: 4, Type: dt, ResolutionMethod: "promote_" + dt, Confidence: 1.0, TrustTier: "CERTIFIED"},
		)
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		t.Fatal(err)
	}

	n, err := ComputeTransitiveClosure(db, "CALLS", 3, 0.0)
	if err != nil {
		t.Fatal(err)
	}
	if n == 0 {
		t.Fatal("no closure rows written")
	}

	tx2, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx2.Rollback()
	rows, err := tx2.Query(`SELECT source_id, target_id FROM closure`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := map[[2]int64]bool{}
	for rows.Next() {
		var s, tgt int64
		if err := rows.Scan(&s, &tgt); err != nil {
			t.Fatal(err)
		}
		got[[2]int64{s, tgt}] = true
	}

	// The verified CALLS edge IS present.
	if !got[[2]int64{1, 2}] {
		t.Error("verified CALLS reach 1->2 missing from closure")
	}
	// NO depth-derived reach may appear: the direct depth hops 2->3 and 3->4,
	// nor any transitive extension through them (1->3, 1->4, 2->4).
	for _, bad := range [][2]int64{{2, 3}, {3, 4}, {1, 3}, {1, 4}, {2, 4}} {
		if got[bad] {
			t.Errorf("promoted DEPTH reach %v leaked into the CALLS closure (I2 violation)", bad)
		}
	}
}

// isVerifiedEdge unit contract: AND semantics, categorical name_match exclusion.
func TestIsVerifiedEdge(t *testing.T) {
	cases := []struct {
		method string
		conf   float64
		want   bool
	}{
		{"same_file", 1.0, true},
		{"import", 1.0, true},
		{"type_flow", 0.9, true},
		{"inherited", 0.95, true},
		{"return_type", 0.85, true},
		{"lsp", 0.95, true},
		// Sub-floor variants of deterministic methods (ambiguity demotes): out.
		{"same_file", 0.6, false},
		{"import", 0.6, false},
		// name_match: out at ANY confidence.
		{"name_match", 0.9, false},
		{"name_match", 0.6, false},
		{"name_match", 0.2, false},
		// impl_method never proves the receiver: out.
		{"impl_method", 0.6, false},
		// unique_method is the SAME receiver-unproven class as impl_method (global
		// method-name uniqueness, no receiver-type proof — resolver.go rung 1.98's
		// own comment: "1.98 does not prove the receiver"). It must be OUT of the
		// verified closure exactly like impl_method, on BOTH gates: the method is
		// not in verifiedMethods, AND the resolver now caps its confidence at 0.6
		// (matching 1.94's CANDIDATE tier) so it cannot clear the 0.7 floor either.
		{"unique_method", 0.85, false},
		{"unique_method", 0.6, false},
	}
	for _, tc := range cases {
		e := &store.Edge{ResolutionMethod: tc.method, Confidence: tc.conf}
		if got := isVerifiedEdge(e); got != tc.want {
			t.Errorf("isVerifiedEdge(%s, %.2f) = %v, want %v", tc.method, tc.conf, got, tc.want)
		}
	}
}

// TestClosure_ParallelEdgeUsesBestConfidence is the BITING test for the closure dedup
// bug (closure.go: bestEdgeConf was computed but the adjacency conf was frozen at the
// FIRST-SCANNED edge). The generic closure engine is exercised with a synthetic
// relation because the production schema now forbids parallel CALLS endpoints.
// Node 1 reaches node 2 via TWO parallel TEST_CALLS edges: the
// first-inserted (id-ASC, scanned first) is the WEAKER one (inherited 0.85); a second,
// STRONGER edge (same_file 1.0) is inserted after. Node 2 -> node 3 is 1.0. The
// weakest-link min_confidence of the 2-hop path 1->3 must reflect the STRONGER 1->2
// edge (1.0), not the first-seen weaker one (0.85). Before the fix the BFS propagated
// 0.85; after, it propagates 1.0.
func TestClosure_ParallelEdgeUsesBestConfidence(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`INSERT INTO nodes (id, label, name, file_path, language) VALUES
		(1, 'Function', 'a', 'a.py', 'python'),
		(2, 'Function', 'b', 'b.py', 'python'),
		(3, 'Function', 'c', 'c.py', 'python')`); err != nil {
		tx.Rollback()
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	edges := []*store.Edge{
		// WEAKER 1->2 inserted FIRST (lower id, scanned first by the adjacency build).
		{SourceID: 1, TargetID: 2, Type: "TEST_CALLS", ResolutionMethod: "inherited", Confidence: 0.85, TrustTier: "CERTIFIED"},
		// STRONGER 1->2 inserted SECOND — both are admitted (verified method, conf>=0.7).
		{SourceID: 1, TargetID: 2, Type: "TEST_CALLS", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"},
		// 2->3 at full confidence; the 2-hop weakest link is therefore the 1->2 edge.
		{SourceID: 2, TargetID: 3, Type: "TEST_CALLS", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"},
	}
	if err := db.BatchInsertEdges(edges); err != nil {
		t.Fatal(err)
	}

	if _, err := ComputeTransitiveClosure(db, "TEST_CALLS", 3, 0.0); err != nil {
		t.Fatal(err)
	}

	tx2, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx2.Rollback()

	// Direct 1->2 row must carry the STRONGER edge's confidence (1.0), not 0.85.
	var directConf float64
	if err := tx2.QueryRow(`SELECT min_confidence FROM closure WHERE source_id=1 AND target_id=2`).Scan(&directConf); err != nil {
		t.Fatal(err)
	}
	if directConf != 1.0 {
		t.Errorf("direct 1->2 min_confidence: want 1.0 (strongest parallel edge), got %v (first-seen weaker edge leaked)", directConf)
	}
	// THE BITE: the 2-hop path 1->3's weakest link must be 1.0, not the stale 0.85.
	var pathConf float64
	if err := tx2.QueryRow(`SELECT min_confidence FROM closure WHERE source_id=1 AND target_id=3`).Scan(&pathConf); err != nil {
		t.Fatal(err)
	}
	if pathConf != 1.0 {
		t.Errorf("path 1->3 min_confidence: want 1.0 (strongest 1->2 parallel edge governs the weakest link), got %v", pathConf)
	}
}
