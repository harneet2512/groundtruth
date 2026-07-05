package resolver

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// queryExtends returns the EXTENDS edges keyed by (source,target).
func queryExtends(t *testing.T, db *store.DB) map[[2]int64]store.Edge {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	rows, err := tx.Query(`SELECT source_id, target_id, COALESCE(resolution_method,''),
	        COALESCE(confidence,0), COALESCE(trust_tier,''), COALESCE(evidence_type,'')
	   FROM edges WHERE type = 'EXTENDS'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := make(map[[2]int64]store.Edge)
	for rows.Next() {
		var e store.Edge
		if err := rows.Scan(&e.SourceID, &e.TargetID, &e.ResolutionMethod, &e.Confidence,
			&e.TrustTier, &e.EvidenceType); err != nil {
			t.Fatal(err)
		}
		out[[2]int64{e.SourceID, e.TargetID}] = e
	}
	return out
}

// Fable RS9 (RED→GREEN): the EXTENDS/IMPLEMENTS base resolver fell back to an arbitrary
// GLOBAL FIRST-MATCH (resolveClassNode → entries[0]) at confidence 1.0 "inheritance" when a
// base name existed in >1 file. A wrong parent contaminates CHA (lookupMethodWithInheritance)
// for EVERY method call on the subclass. The fix routes the base through
// resolveClassNodeSameFileOrUnique, which abstains on a cross-file AMBIGUOUS name. A cross-file
// UNIQUE base must still wire (no over-abstention). RED before the fix (Child→arbitrary Base
// edge exists at 1.0); GREEN after (no Child EXTENDS edge; the unique Grand→UniqueBase remains).
func TestResolveRelationships_RS9_AmbiguousBaseAbstains_UniqueBaseWires(t *testing.T) {
	root := t.TempDir()
	src := map[string]string{
		"a.py": "class Base:\n    pass\n",                 // Base #1 (ambiguous with b.py)
		"b.py": "class Base:\n    pass\n",                 // Base #2 (ambiguous with a.py)
		"c.py": "class Child(Base):\n    pass\n",          // Child(Base) — base is AMBIGUOUS
		"e.py": "class UniqueBase:\n    pass\n",           // the only UniqueBase (unique)
		"d.py": "class Grand(UniqueBase):\n    pass\n",    // Grand(UniqueBase) — base is UNIQUE
	}
	var files []walker.SourceFile
	for name, content := range src {
		p := filepath.Join(root, name)
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
		files = append(files, walker.SourceFile{Path: name, AbsPath: p, Language: "python"})
	}

	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })

	execSQL(t, db, `INSERT INTO nodes (id, label, name, file_path, start_line, signature, language, parent_id) VALUES
		(1, 'Class', 'Base',       'a.py', 1, 'class Base:',            'python', NULL),
		(2, 'Class', 'Base',       'b.py', 1, 'class Base:',            'python', NULL),
		(3, 'Class', 'Child',      'c.py', 1, 'class Child(Base):',     'python', NULL),
		(4, 'Class', 'UniqueBase', 'e.py', 1, 'class UniqueBase:',      'python', NULL),
		(5, 'Class', 'Grand',      'd.py', 1, 'class Grand(UniqueBase):','python', NULL)`)

	if _, err := ResolveRelationships(db, files, root); err != nil {
		t.Fatal(err)
	}
	ext := queryExtends(t, db)

	// Child's base "Base" is cross-file AMBIGUOUS (a.py + b.py) → must ABSTAIN: no EXTENDS edge
	// from Child to EITHER Base. Pre-RS9 this laundered Child→Base#1 (first-match) at conf 1.0.
	if e, ok := ext[[2]int64{3, 1}]; ok {
		t.Errorf("RS9: Child EXTENDS an ARBITRARY first-match Base (a.py, id 1) conf=%.2f method=%q — "+
			"an ambiguous cross-file base must abstain", e.Confidence, e.ResolutionMethod)
	}
	if _, ok := ext[[2]int64{3, 2}]; ok {
		t.Errorf("RS9: Child EXTENDS the other Base (b.py, id 2) — an ambiguous cross-file base must abstain")
	}
	// Control: Grand's base "UniqueBase" is cross-file UNIQUE → must STILL wire (no over-abstention).
	if _, ok := ext[[2]int64{5, 4}]; !ok {
		t.Errorf("RS9 over-abstained: Grand→UniqueBase (cross-file UNIQUE) should still wire, got edges %+v", ext)
	}
}
