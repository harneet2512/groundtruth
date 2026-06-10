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
		{"unique_method", 0.85, true},
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
	}
	for _, tc := range cases {
		e := &store.Edge{ResolutionMethod: tc.method, Confidence: tc.conf}
		if got := isVerifiedEdge(e); got != tc.want {
			t.Errorf("isVerifiedEdge(%s, %.2f) = %v, want %v", tc.method, tc.conf, got, tc.want)
		}
	}
}
