package resolver

import (
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ---------------------------------------------------------------------------
// FIX 1 — resolver.go last-chance qualified-unresolved block.
// A QUALIFIED call whose receiver TYPE could not be proven, but whose single
// globally-unique target is import-reachable, must be a CANDIDATE (name_match 0.6),
// NOT a CERTIFIED verified_unique 0.9. Import/dir reachability is not receiver-type
// proof. Mutation: reverting to verified_unique/0.9/CERTIFIED flips this RED.
// ---------------------------------------------------------------------------
func TestResolve_QualifiedImportedSingleCandidate_CappedAtCandidate(t *testing.T) {
	files := []string{"app/caller.py", "lib/target.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// The caller imports the target MODULE (via a DIFFERENT symbol name), so the module
	// is import-reachable, but the callee name itself is never a specific import and the
	// receiver `x` never types to a class. The only way it can resolve is the last-chance
	// block.
	imports := []parser.ImportRef{
		{ImportedName: "othersym", ModulePath: "lib.target", File: "app/caller.py", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "uniquemethod", CalleeQualified: "x.uniquemethod", Line: 5, File: "app/caller.py"},
	}
	nodeIDs := map[string][]int64{"caller": {1}, "uniquemethod": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"app/caller.py": {"caller": {1}},
		"lib/target.py": {"uniquemethod": {2}},
	}
	callerIDs := []int64{1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "app/caller.py"},
		2: {Label: "Function", Name: "uniquemethod", File: "lib/target.py"},
	}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved call, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.Method == "verified_unique" || r.Confidence >= 0.9 || r.TrustTier == "CERTIFIED" {
		t.Errorf("qualified receiver-UNPROVEN imported call was CERTIFIED as a fact "+
			"(method=%q conf=%.2f tier=%q); want CANDIDATE name_match (receiver-type not proven)",
			r.Method, r.Confidence, r.TrustTier)
	}
	if r.Method != "name_match" || r.Confidence != 0.6 || r.TrustTier != "CANDIDATE" ||
		r.EvidenceType != "name_match_qualified_imported" {
		t.Errorf("got (method=%q conf=%.2f tier=%q ev=%q); want (name_match, 0.6, CANDIDATE, name_match_qualified_imported)",
			r.Method, r.Confidence, r.TrustTier, r.EvidenceType)
	}
}

// ---------------------------------------------------------------------------
// FIX 3 — buildImportIndex must NOT seed the bare-name "*" wildcard for a SPECIFIC
// named import. `from lib.mod import foo` binds only `foo`; a bare call to `bar`
// (also in lib.mod, but NOT imported) must not resolve via "*" at import conf 1.0.
// Mutation: re-seeding "*" for every named import flips `bar` to method "import"/1.0.
// ---------------------------------------------------------------------------
func TestResolve_SpecificNamedImport_NoWildcardLaundering(t *testing.T) {
	files := []string{"app/caller.py", "lib/mod.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{ImportedName: "foo", ModulePath: "lib.mod", File: "app/caller.py", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "foo", Line: 5, File: "app/caller.py"},
		{CallerNodeIdx: 0, CalleeName: "bar", CalleeQualified: "bar", Line: 6, File: "app/caller.py"},
	}
	nodeIDs := map[string][]int64{"caller": {1}, "foo": {2}, "bar": {3}}
	fileNodeIDs := map[string]map[string][]int64{
		"app/caller.py": {"caller": {1}},
		"lib/mod.py":    {"foo": {2}, "bar": {3}},
	}
	callerIDs := []int64{1, 1}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", Name: "caller", File: "app/caller.py"},
		2: {Label: "Function", Name: "foo", File: "lib/mod.py"},
		3: {Label: "Function", Name: "bar", File: "lib/mod.py"},
	}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	byTarget := map[int64]ResolvedCall{}
	for _, r := range resolved {
		byTarget[r.TargetNodeID] = r
	}
	// foo IS a specific import → resolves via import (the specific entry, unaffected).
	if r, ok := byTarget[2]; !ok || r.Method != "import" {
		t.Errorf("imported name foo: got %+v, want method=import (specific entry must still work)", r)
	}
	// bar was NEVER imported → must NOT be laundered via the "*" wildcard at import 1.0.
	if r, ok := byTarget[3]; ok {
		if r.Method == "import" {
			t.Errorf("bar (never imported) resolved via the '*' wildcard as method=import conf=%.2f — "+
				"a specific named import must not seed the bare-name wildcard", r.Confidence)
		}
	}
}

// ---------------------------------------------------------------------------
// FIX 4c — KEEP-BEST-CONFIDENCE dedup. Two call sites resolve the SAME (caller,target)
// pair: an earlier unqualified call at LOW confidence (name_match) and a later qualified
// call at HIGH confidence (type_flow). The stored edge must keep the HIGHER confidence.
// Mutation: reverting to the first-wins `seen` bool keeps the low-conf name_match RED.
// ---------------------------------------------------------------------------
func TestResolve_KeepBestConfidence_LaterTypeFlowWins(t *testing.T) {
	files := []string{"app/svc.py", "lib/impl.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// foo is a METHOD of ClassB (lib/impl.py). Call 1 is a bare foo() (resolves LOW —
	// no provenance, cross-dir → demoted name_match). Call 2 is b.foo() where b is a
	// declared ClassB param (resolves HIGH — type_flow 0.9 via 1.94a).
	nodeIDs := map[string][]int64{"handler": {1}, "Service": {10}, "ClassB": {20}, "foo": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"app/svc.py":  {"handler": {1}, "Service": {10}},
		"lib/impl.py": {"ClassB": {20}, "foo": {2}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Method", Name: "handler", File: "app/svc.py", ParentID: 10},
		10: {Label: "Class", Name: "Service", File: "app/svc.py"},
		20: {Label: "Class", Name: "ClassB", File: "lib/impl.py"},
		2:  {Label: "Method", Name: "foo", File: "lib/impl.py", ParentID: 20},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "foo", Line: 5, File: "app/svc.py"},
		{CallerNodeIdx: 0, CalleeName: "foo", CalleeQualified: "b.foo", Line: 6, File: "app/svc.py"},
	}
	callerIDs := []int64{1, 1}

	SetParamTypeIndex(map[int64]map[string]string{1: {"b": "ClassB"}})
	defer SetParamTypeIndex(nil)

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected exactly 1 edge for the (caller,foo) pair, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.TargetNodeID != 2 {
		t.Fatalf("edge target = %d, want 2 (foo)", r.TargetNodeID)
	}
	if r.Method != "type_flow" || r.Confidence < 0.9 {
		t.Errorf("keep-best failed: stored (method=%q conf=%.2f); an earlier low-confidence "+
			"resolution suppressed the later type_flow 0.9 proof of the same pair", r.Method, r.Confidence)
	}
}

// small helper: read one edge's (confidence, trust_tier).
func oneEdge(t *testing.T, db *store.DB, where string) (float64, string) {
	t.Helper()
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback()
	var conf float64
	var tier string
	if err := tx.QueryRow(`SELECT confidence, trust_tier FROM edges WHERE `+where).Scan(&conf, &tier); err != nil {
		t.Fatalf("read edge (%s): %v", where, err)
	}
	return conf, tier
}

// ---------------------------------------------------------------------------
// FIX 2 — promoteRaises must gate RAISES confidence on the exception-name ambiguity
// (candidate_count). A name shared by TWO project classes is a guess → CANDIDATE (0.6),
// not a CERTIFIED fact; a uniquely-named class stays 0.9. Mutation: the old constant
// 0.9 makes the ambiguous edge CERTIFIED (RED).
// ---------------------------------------------------------------------------
func TestPromote_RaisesAmbiguousNameNotCertified(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// 60 raiser; DupError defined in TWO files (61,62) → cc=2; SoloError unique (63).
	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (60,'Method','run',     'a.py',5,'def run()','python',0),
	  (61,'Class', 'DupError', 'a.py',1,'',        'python',0),
	  (62,'Class', 'DupError', 'b.py',1,'',        'python',0),
	  (63,'Class', 'SoloError','a.py',9,'',        'python',0)`)
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (60,'exception_type','DupError',6,1.0),
	  (60,'exception_type','SoloError',7,1.0)`)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}

	// Ambiguous DupError (2 candidates) → CANDIDATE, never CERTIFIED.
	if got := countEdges(t, db, `type='RAISES' AND target_id=61 AND resolution_method='promote_raises'`); got != 1 {
		t.Fatalf("want 1 RAISES edge to same-file DupError(61), got %d", got)
	}
	conf, tier := oneEdge(t, db, `type='RAISES' AND target_id=61 AND resolution_method='promote_raises'`)
	if conf >= 0.9 || tier == "CERTIFIED" {
		t.Errorf("ambiguous exception name minted a CERTIFIED RAISES (conf=%.2f tier=%q); want CANDIDATE/lower", conf, tier)
	}
	if conf != 0.6 || tier != "CANDIDATE" {
		t.Errorf("cc==2 RAISES: got (conf=%.2f tier=%q), want (0.6, CANDIDATE)", conf, tier)
	}
	// Unique SoloError (1 candidate) → still a CERTIFIED fact.
	if got := countEdges(t, db, `type='RAISES' AND target_id=63 AND resolution_method='promote_raises'`); got != 1 {
		t.Fatalf("want 1 RAISES edge to unique SoloError(63), got %d", got)
	}
	conf, tier = oneEdge(t, db, `type='RAISES' AND target_id=63 AND resolution_method='promote_raises'`)
	if conf != 0.9 || tier != "CERTIFIED" {
		t.Errorf("unique exception RAISES: got (conf=%.2f tier=%q), want (0.9, CERTIFIED)", conf, tier)
	}
}

// ---------------------------------------------------------------------------
// FIX 5 — a `super` call_order receiver must resolve to the enclosing class's PARENT,
// not the subclass. Parent(70) owns m1(71),m2(72); Child(80).run does super.m1()->m2().
// With super->Parent, PRECEDES 71->72 is minted. Mutation: resolving super to the
// subclass (Child) finds neither method on Child → 0 edges (RED).
// ---------------------------------------------------------------------------
func TestPromote_PrecedesSuperResolvesToParentNotSubclass(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// Parent(70) with methods m1(71)/m2(72); Child(80) subclass with run(81). All in
	// cls.py so resolveClassMethod's same-file gate is satisfied. m1/m2 are members of
	// Parent (parent_id=70), NOT of Child.
	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (70,'Class', 'Parent','cls.py', 1,'','python',0),
	  (71,'Method','m1',    'cls.py', 3,'','python',70),
	  (72,'Method','m2',    'cls.py', 5,'','python',70),
	  (80,'Class', 'Child', 'cls.py',10,'','python',0),
	  (81,'Method','run',   'cls.py',12,'','python',80)`)
	// Child.run does super.m1() -> super.m2(); the call_order receiver token is `super`.
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (81,'call_order','super: m1 -> m2',13,0.6)`)

	// Child(80) EXTENDS Parent(70) — the inheritance map promotion reads for `super`.
	SetInheritanceMap(map[int64][]int64{80: {70}})
	defer SetInheritanceMap(nil)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}

	if got := countEdges(t, db, `type='PRECEDES' AND source_id=71 AND target_id=72 AND resolution_method='promote_precedes'`); got != 1 {
		t.Fatalf("super must resolve to the PARENT class: want 1 PRECEDES edge 71->72 (Parent.m1->Parent.m2), got %d", got)
	}
	// No stray edge onto the subclass and no other PRECEDES.
	if got := countEdges(t, db, `type='PRECEDES' AND resolution_method='promote_precedes'`); got != 1 {
		t.Errorf("want exactly 1 PRECEDES edge total, got %d", got)
	}
}

// FIX 5 negative control — with NO recorded superclass, `super` must ABSTAIN
// (correct-or-quiet), NOT fall back to minting an edge onto the subclass.
func TestPromote_PrecedesSuperAbstainsWhenSuperclassUnknown(t *testing.T) {
	root := t.TempDir()
	db, err := store.Open(filepath.Join(root, "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	// Child(80) defines m1/m2 ITSELF; if `super` wrongly resolved to Child, an edge would
	// be minted. With no inheritance entry for Child, super must abstain → 0 edges.
	execSQL(t, db, `INSERT INTO nodes (id,label,name,file_path,start_line,signature,language,parent_id) VALUES
	  (80,'Class', 'Child','cls.py',10,'','python',0),
	  (81,'Method','run',  'cls.py',12,'','python',80),
	  (82,'Method','m1',   'cls.py',14,'','python',80),
	  (83,'Method','m2',   'cls.py',16,'','python',80)`)
	execSQL(t, db, `INSERT INTO properties (node_id,kind,value,line,confidence) VALUES
	  (81,'call_order','super: m1 -> m2',13,0.6)`)

	SetInheritanceMap(nil)

	if _, err := PromotePropertyEdges(db); err != nil {
		t.Fatalf("PromotePropertyEdges: %v", err)
	}
	if got := countEdges(t, db, `type='PRECEDES' AND resolution_method='promote_precedes'`); got != 0 {
		t.Errorf("super with unknown superclass must abstain (0 edges), got %d — it wrongly resolved to the subclass", got)
	}
}
