package main

import (
	"database/sql"
	"os"
	"os/exec"
	"path/filepath"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// A virtual call whose qualifier was bound by an import (`from pkg.base
// import Base` + `Base.update(line, poly)`) is minted by the resolver as
// "import" with the base-class method as its candidate -- and that candidate
// carries a real chain. The hierarchy pass then owns the published candidate
// set for the callsite: the receiver type is not provable at this callsite,
// so CHA's conservative boundary substitutes the implementor methods
// (SubA.update, SubB.update) for the resolver's pick. Those candidates are
// hierarchy-derived, not import-derived: no import in the caller binds them.
//
// Publication used to keep the resolver's "import" mechanism over the
// substituted set. import_binding then demanded a per-candidate import chain
// the substituted candidates cannot have, the atomic attach aborted, and the
// whole analysis rolled back -- analysis_state=failed on every real
// repository. The mechanism must be restated to what the published set
// actually is: the conservative implementor set, which is what impl_method
// names.
func TestHierarchySubstitutedCandidatesDoNotClaimImportBinding(t *testing.T) {
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := filepath.Join(t.TempDir(), "repo")
	if err := os.MkdirAll(filepath.Join(repo, "pkg"), 0o755); err != nil {
		t.Fatal(err)
	}
	sources := map[string]string{
		"pkg/base.py": "class Base:\n    def update(self, other):\n        self.other = other\n",
		"pkg/sub_a.py": "from pkg.base import Base\n\n\nclass SubA(Base):\n    def update(self, other):\n        self.a = other\n",
		"pkg/sub_b.py": "from pkg.base import Base\n\n\nclass SubB(Base):\n    def update(self, other):\n        self.b = other\n",
		"caller.py":    "from pkg.base import Base\n\n\ndef swap(line, poly):\n    vis = line.get_visible()\n    Base.update(line, poly)\n    line.set_visible(vis)\n",
	}
	for name, source := range sources {
		if err := os.WriteFile(filepath.Join(repo, filepath.FromSlash(name)), []byte(source), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("full index: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// The analysis must publish: the substituted candidates are honestly
	// derivable, so a rollback is a defect, not a boundary.
	if state := twoPhaseMeta(t, db, "analysis_state"); state != "complete" {
		t.Fatalf("analysis_state = %q, want \"complete\" (failure reason: %q)",
			state, twoPhaseMeta(t, db, "analysis_failure_reason"))
	}

	// The substituted callsite must name the derivation that produced its
	// published candidate set -- the implementor set -- not the resolver's
	// pre-substitution "import" attribution.
	var mechanism, dispatch string
	var candidateCount int
	if err := db.QueryRow(`SELECT mechanism, dispatch_state, candidate_count
		FROM resolution_callsites WHERE callee='update'`).Scan(&mechanism, &dispatch, &candidateCount); err != nil {
		t.Fatalf("virtual callsite row: %v", err)
	}
	if mechanism != "impl_method" || dispatch != "ambiguous" || candidateCount != 2 {
		t.Fatalf("substituted callsite claims mechanism=%q dispatch=%q candidates=%d, want impl_method/ambiguous/2",
			mechanism, dispatch, candidateCount)
	}

	// The callsite edge carries the derivation kind the store validated:
	// the substituted implementor set is an interface_implementors derivation,
	// not the import_binding the resolver's mechanism would have claimed.
	var derivationKind, evidenceSet string
	if err := db.QueryRow(`SELECT e.derivation_kind, e.evidence_set
		FROM nodes callsite
		JOIN edges e ON e.target_id=callsite.id AND e.type='HAS_CALLSITE'
		WHERE callsite.node_type='callsite' AND callsite.callee_lexeme='update'`).Scan(&derivationKind, &evidenceSet); err != nil {
		t.Fatalf("callsite edge: %v", err)
	}
	if derivationKind != "interface_implementors" {
		t.Fatalf("substituted callsite derivation_kind=%q, want interface_implementors", derivationKind)
	}

	// Every published candidate is one of the two implementor methods, with a
	// declared scope and no import_binding claim. An implementor-set candidate
	// legitimately carries an empty import chain -- the rejected shape was an
	// import_binding derivation over a candidate with nothing to attest it.
	rows, err := db.Query(`SELECT e.derivation_kind, e.import_chain, e.declared_scope, s.qualified_name
		FROM nodes callsite
		JOIN edges e ON e.source_id=callsite.id AND e.type='CANDIDATE_TARGET'
		JOIN resolution_symbols s ON s.stable_id=e.target_symbol_id
		WHERE callsite.node_type='callsite' AND callsite.callee_lexeme='update'
		ORDER BY s.qualified_name`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var seen []string
	for rows.Next() {
		var kind, chain, scope, qualified string
		if err := rows.Scan(&kind, &chain, &scope, &qualified); err != nil {
			t.Fatal(err)
		}
		if kind == "import_binding" || scope == "" {
			t.Fatalf("candidate %s claims kind=%q chain=%q scope=%q", qualified, kind, chain, scope)
		}
		seen = append(seen, qualified)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if len(seen) != 2 {
		t.Fatalf("published candidates=%v, want the two implementor methods", seen)
	}

	// The defect's global form: no candidate anywhere may claim import_binding
	// without the chain that substantiates it.
	var orphaned int
	if err := db.QueryRow(`SELECT count(*) FROM edges
		WHERE type='CANDIDATE_TARGET' AND derivation_kind='import_binding'
		AND (import_chain='' OR import_chain='[]')`).Scan(&orphaned); err != nil {
		t.Fatal(err)
	}
	if orphaned != 0 {
		t.Fatalf("%d import_binding candidates carry no import chain", orphaned)
	}
}
