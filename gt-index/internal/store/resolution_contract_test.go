package store

import (
	"math/rand"
	"path/filepath"
	"strings"
	"testing"
)

func testGraphIdentity(revision string) GraphCompletionIdentity {
	return GraphCompletionIdentity{
		Schema: GraphCompletionSchema, RepositoryRevision: revision,
		BuildInfoSchema: "gt-index.build.v1", BuildID: "build", GitCommit: "commit",
		BuildTimeUTC: "2026-08-29T00:00:00Z", SourceFingerprint: "source",
		ExecutableSHA256: "exe", GoToolchain: "go1.test", BuildTags: "sqlite_fts5",
		GraphSchemaVersion: "schema", ResolutionContract: ResolutionProducerContract,
		ResolutionAuthoritySchema: "gt-index.resolution-authority.v1", Complete: true,
	}
}

func TestStableCallsiteV2IDGoldenVectorAndClosedDispatch(t *testing.T) {
	got, err := StableCallsiteV2ID("repo", "rev", "src/main.go#7", "0/2/1", 10, 20, "virtual", "Run")
	if err != nil {
		t.Fatal(err)
	}
	const want = "36b2374df87109c2c54c01a801bbd46d00bf80a9a62dd13bc9367cd0dcf392c0"
	if got != want {
		t.Fatalf("v2 callsite ID mismatch: got %s want %s", got, want)
	}
	if _, err := StableCallsiteV2ID("repo", "rev", "src/main.go#7", "0/2/1", 10, 20, "invented", "Run"); err == nil {
		t.Fatal("unknown dispatch form was accepted")
	}
}

func TestV2CandidateOrdinalsStableAcrossOneHundredShuffles(t *testing.T) {
	callsite := &ResolutionCallsite{CallsiteOrdinal: 7, SourceID: 1, SourceLine: 4, SourceFile: "main.py", Callee: "run", DispatchState: "ambiguous", CandidateCount: 3, Mechanism: "name_match", RepoID: "repo", FileNodeID: 9, CallerSymbolID: "caller", ASTPath: "0/1", ByteStart: 10, ByteEnd: 20, DispatchForm: "virtual", ParseState: "complete"}
	base := []*ResolutionCandidate{
		{TargetID: 3, TargetStableID: "cccc", TargetNativeID: "c", Mechanism: "name_match"},
		{TargetID: 1, TargetStableID: "aaaa", TargetNativeID: "a", Mechanism: "name_match"},
		{TargetID: 2, TargetStableID: "bbbb", TargetNativeID: "b", Mechanism: "name_match"},
	}
	want := []string{"aaaa", "bbbb", "cccc"}
	for seed := int64(0); seed < 100; seed++ {
		shuffled := append([]*ResolutionCandidate(nil), base...)
		rand.New(rand.NewSource(seed)).Shuffle(len(shuffled), func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
		publication, err := prepareResolutionV2(testGraphIdentity("rev"), callsite, shuffled)
		if err != nil {
			t.Fatal(err)
		}
		for ordinal, candidate := range publication.Candidates {
			if candidate.Candidate.Ordinal != ordinal || candidate.Candidate.TargetStableID != want[ordinal] || len(candidate.EdgeID) != 64 {
				t.Fatalf("seed %d unstable candidate order: %+v", seed, publication.Candidates)
			}
		}
	}
}

func TestV2CompletenessKeepsExecutedWinnerWhenLegacyPassesShareCanonicalKind(t *testing.T) {
	callsite := &ResolutionCallsite{
		CallsiteOrdinal: 1, SourceID: 1, SourceLine: 4, SourceFile: "main.py", Callee: "run",
		DispatchState: "unique", CandidateCount: 1, Mechanism: "impl_method", RepoID: "repo",
		FileNodeID: 9, CallerSymbolID: "caller", ASTPath: "0/1", ByteStart: 10, ByteEnd: 20,
		DispatchForm: "interface", ParseState: "complete",
		PassCoverage: []ResolutionPassCoverage{
			{PassKind: "declared_type", Version: "1", Status: "not_run", Reason: "no_execution_event"},
			{PassKind: "implementation_set", Version: "1", Status: "completed_match"},
		},
	}
	publication, err := prepareResolutionV2(testGraphIdentity("rev"), callsite, []*ResolutionCandidate{{TargetID: 2, TargetStableID: "target", TargetNativeID: "2", Mechanism: "impl_method"}})
	if err != nil {
		t.Fatal(err)
	}
	for _, fact := range publication.Completeness {
		if fact.PassKind == "scope_binding" {
			if fact.Status != "closed" || fact.Known == nil || fact.Covered != 1 {
				t.Fatalf("executed implementation-set winner lost behind not-run declared-type alias: %+v", fact)
			}
			return
		}
	}
	t.Fatal("canonical scope_binding completeness fact missing")
}

func TestResolutionStableIdentityIsDeterministic(t *testing.T) {
	n := Node{Language: "python", FilePath: "src/a.py", QualifiedName: "a.run", Label: "Function", StartLine: 2, EndLine: 4}
	a := StableResolutionSymbolID(n)
	if a == "" || a != StableResolutionSymbolID(n) {
		t.Fatalf("unstable symbol identity: %q", a)
	}
	c := StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run")
	if c == "" || c != StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run") {
		t.Fatalf("unstable callsite identity: %q", c)
	}
}

func TestAttachResolutionGraphRejectsCountDivergenceTransactionally(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "cs", SourceID: sourceID, SourceFile: "main.py", SourceLine: 3, Callee: "target", DispatchState: "unique", CandidateCount: 1, Mechanism: "same_file"},
	}
	if err := AttachResolutionGraphTx(tx, testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("expected count/retained-row invariant failure")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	var nodes, complete int
	if err := db.db.QueryRow("SELECT count(*) FROM nodes WHERE label='Callsite'").Scan(&nodes); err != nil {
		t.Fatal(err)
	}
	if nodes != 0 {
		t.Fatalf("failed transaction published %d callsite nodes", nodes)
	}
	if err := db.db.QueryRow("SELECT count(*) FROM project_meta WHERE key='graph_resolution_complete'").Scan(&complete); err != nil {
		t.Fatal(err)
	}
	if complete != 0 {
		t.Fatal("failed transaction published graph completion metadata")
	}
}

func TestAttachResolutionGraphPersistsTypedFactsWithoutCandidateConfidence(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	targetA, err := db.InsertNode(&Node{Label: "Function", Name: "target", QualifiedName: "a.target", FilePath: "a.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	targetB, err := db.InsertNode(&Node{Label: "Function", Name: "target", QualifiedName: "b.target", FilePath: "b.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "ambiguous", SourceID: sourceID, SourceFile: "main.py", SourceLine: 3, Callee: "target", DispatchState: "ambiguous", CandidateCount: 2, Mechanism: "name_match"},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "name_match", VerificationStatus: "legacy_edge"},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "name_match", VerificationStatus: "legacy_edge"},
		},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var highConfidence int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type='CANDIDATE_TARGET' AND confidence IS NOT NULL`).Scan(&highConfidence); err != nil {
		t.Fatal(err)
	}
	if highConfidence != 0 {
		t.Fatalf("candidate edges published at verified confidence: %d", highConfidence)
	}
	var confidenceIsNull int
	if err := db.db.QueryRow(`SELECT confidence IS NULL FROM edges WHERE type='CANDIDATE_TARGET' LIMIT 1`).Scan(&confidenceIsNull); err != nil {
		t.Fatal(err)
	}
	if confidenceIsNull != 1 {
		t.Fatal("candidate viability persisted an invented scalar confidence")
	}
	var contract, kind, evidenceSet, buildID, sourceFingerprint string
	var siblings int
	if err := db.db.QueryRow(`SELECT derivation_contract,derivation_kind,evidence_set,sibling_count,producer_build_id,producer_source_fingerprint FROM edges WHERE type='CANDIDATE_TARGET' LIMIT 1`).Scan(&contract, &kind, &evidenceSet, &siblings, &buildID, &sourceFingerprint); err != nil {
		t.Fatal(err)
	}
	if contract != ResolutionDerivationContract || kind != "unique_name_fallback" || evidenceSet != "partial" || siblings != 2 || buildID == "" || sourceFingerprint == "" {
		t.Fatalf("typed derivation facts incomplete: %q %q %q %d %q %q", contract, kind, evidenceSet, siblings, buildID, sourceFingerprint)
	}
	var candidateMetadata string
	if err := db.db.QueryRow(`SELECT COALESCE(metadata,'') FROM edges WHERE type='CANDIDATE_TARGET' LIMIT 1`).Scan(&candidateMetadata); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{`"confidence"`, `"weight"`, `"score"`} {
		if strings.Contains(candidateMetadata, forbidden) {
			t.Fatalf("candidate metadata persisted forbidden scalar field %s: %s", forbidden, candidateMetadata)
		}
	}
	evidence, err := db.QueryAttachedCandidates("target")
	if err != nil {
		t.Fatal(err)
	}
	if len(evidence) != 2 || evidence[0].DispatchState != "ambiguous" || evidence[1].DispatchState != "ambiguous" {
		t.Fatalf("ambiguous evidence was not preserved for explicit consumers: %+v", evidence)
	}
	if evidence[0].PolicySelected || evidence[1].PolicySelected {
		t.Fatalf("ambiguous evidence published a selected candidate: %+v", evidence)
	}
	if evidence[0].PolicyID != ConservativeCandidatePolicy.ID || len(evidence[0].PassCoverage) == 0 || len(evidence[0].ProvenanceSteps) == 0 {
		t.Fatalf("main query omitted policy or derivation provenance: %+v", evidence[0])
	}
	var schemaVersion, candidateCount int
	var nodeType, candidateState, stableID string
	if err := db.db.QueryRow(`SELECT schema_version,node_type,candidate_state,candidate_count_v2,stable_id FROM nodes WHERE node_type='callsite' LIMIT 1`).Scan(&schemaVersion, &nodeType, &candidateState, &candidateCount, &stableID); err != nil {
		t.Fatal(err)
	}
	if schemaVersion != 2 || nodeType != "callsite" || candidateState != "ambiguous" || candidateCount != 2 || len(stableID) != 64 {
		t.Fatalf("canonical callsite v2 fields invalid: schema=%d type=%s state=%s count=%d id=%s", schemaVersion, nodeType, candidateState, candidateCount, stableID)
	}
	var unresolved, derivations, completeness int
	if err := db.db.QueryRow(`SELECT
		(SELECT count(*) FROM nodes WHERE node_type='unresolved_fact'),
		(SELECT count(*) FROM nodes WHERE node_type='derivation_fact'),
		(SELECT count(*) FROM nodes WHERE node_type='completeness_fact')`).Scan(&unresolved, &derivations, &completeness); err != nil {
		t.Fatal(err)
	}
	if unresolved != 1 || derivations != 2 || completeness == 0 {
		t.Fatalf("canonical fact nodes missing: unresolved=%d derivations=%d completeness=%d", unresolved, derivations, completeness)
	}
}

func TestAttachResolutionGraphPersistsCHAThenRTACompleteness(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetA, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "a.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetB, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "b.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{
			CallsiteID: "cha-rta", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8, Callee: "Run",
			DispatchState: "ambiguous", DispatchForm: "interface", CandidateCount: 2, Mechanism: "impl_method",
			PassCoverage: []ResolutionPassCoverage{
				{PassKind: "cha", Version: "1", Status: "completed_match", CandidateStableIDs: []string{"a", "b"}},
				{PassKind: "rta", Version: "1", Status: "partial", Reason: "hierarchy_open", CandidateStableIDs: []string{"a"}, AllocationTypeStableIDs: []string{"type-a"}, ReachableStableIDs: []string{"caller", "a"}},
			},
		},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "impl_method", DeclaredScope: "Runner", VerificationStatus: "candidate"},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "impl_method", DeclaredScope: "Runner", VerificationStatus: "candidate"},
		},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	rows, err := db.db.Query(`SELECT pass_kind,fact_status,reason_code,reachable_stable_ids FROM nodes WHERE node_type='completeness_fact' AND pass_kind IN ('cha','rta') ORDER BY pass_kind`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	got := make(map[string][3]string)
	for rows.Next() {
		var pass, status, reason, reachable string
		if err := rows.Scan(&pass, &status, &reason, &reachable); err != nil {
			t.Fatal(err)
		}
		got[pass] = [3]string{status, reason, reachable}
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	if got["cha"] != [3]string{"closed", "", "null"} {
		t.Fatalf("CHA completeness=%v, want closed", got["cha"])
	}
	if got["rta"] != [3]string{"partial", "hierarchy_open", `["caller","a"]`} {
		t.Fatalf("RTA completeness=%v, want partial/hierarchy_open", got["rta"])
	}
	evidence, err := db.QueryAttachedCandidates("Run")
	if err != nil {
		t.Fatal(err)
	}
	if len(evidence) != 2 || len(evidence[0].PassCoverage) == 0 {
		t.Fatalf("normal query did not expose hierarchy evidence: %+v", evidence)
	}
	coverage := make(map[string]ResolutionPassCoverage)
	for _, pass := range evidence[0].PassCoverage {
		coverage[pass.PassKind] = pass
	}
	if !strings.EqualFold(strings.Join(coverage["cha"].CandidateStableIDs, ","), "a,b") || !strings.EqualFold(strings.Join(coverage["rta"].CandidateStableIDs, ","), "a") || !strings.EqualFold(strings.Join(coverage["rta"].AllocationTypeStableIDs, ","), "type-a") || !strings.EqualFold(strings.Join(coverage["rta"].ReachableStableIDs, ","), "caller,a") {
		t.Fatalf("normal query hierarchy candidate evidence=%+v", coverage)
	}
}

func TestQueryPolicyChangesSelectionWithoutMutatingGraphFacts(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, _ := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	targetID, _ := db.InsertNode(&Node{Label: "Function", Name: "target", QualifiedName: "target", FilePath: "main.py", Language: "python"})
	selected := "target"
	row := AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "unique", SourceID: sourceID, SourceFile: "main.py", Callee: "target", DispatchState: "unique", CandidateCount: 1, Mechanism: "same_file", VerificationStatus: "source_supported", SelectedTargetStableID: &selected,
			PassCoverage: []ResolutionPassCoverage{
				{PassKind: "lexical_binding", Version: "1", Status: "completed_match"},
				{PassKind: "import_binding", Version: "1", Status: "not_run", Reason: "not_reached_after_resolution"},
			}},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "target", TargetNativeID: "target", Ordinal: 0, Mechanism: "same_file", VerificationStatus: "source_supported", Selected: true}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var before string
	if err := db.db.QueryRow(`SELECT quote(confidence)||'|'||derivation_kind||'|'||evidence_set||'|'||metadata FROM edges WHERE type='CANDIDATE'`).Scan(&before); err != nil {
		t.Fatal(err)
	}
	conservative, err := db.QueryAttachedCandidatesWithPolicy("target", ConservativeCandidatePolicy)
	if err != nil {
		t.Fatal(err)
	}
	inspection, err := db.QueryAttachedCandidatesWithPolicy("target", InspectAllCandidatePolicy)
	if err != nil {
		t.Fatal(err)
	}
	if len(conservative) != 1 || !conservative[0].PolicySelected || len(inspection) != 1 || inspection[0].PolicySelected {
		t.Fatalf("query policies did not own selection: conservative=%+v inspection=%+v", conservative, inspection)
	}
	var after string
	if err := db.db.QueryRow(`SELECT quote(confidence)||'|'||derivation_kind||'|'||evidence_set||'|'||metadata FROM edges WHERE type='CANDIDATE'`).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("query policy mutated persisted graph facts")
	}
	if conservative[0].PolicyVersionID == inspection[0].PolicyVersionID {
		t.Fatal("immutable policies did not receive distinct canonical IDs")
	}
	resolved, err := db.ResolveCandidates(conservative[0].CallsiteID, ConservativeCandidatePolicy.VersionID(), "rev")
	if err != nil || len(resolved) != 1 || !resolved[0].PolicySelected {
		t.Fatalf("canonical ResolveCandidates failed: result=%+v err=%v", resolved, err)
	}
	if _, err := db.ResolveCandidates(conservative[0].CallsiteID, ConservativeCandidatePolicy.VersionID(), "stale"); err == nil {
		t.Fatal("ResolveCandidates accepted a stale graph revision")
	}
}

func TestCandidatePolicyFilterUsesTypedFactIndex(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.db.Query(`EXPLAIN QUERY PLAN SELECT target_id FROM edges INDEXED BY idx_edges_candidate_policy
		WHERE type='CANDIDATE' AND derivation_kind=? AND evidence_set=? AND candidate_count=? ORDER BY target_id`, "declared_type", "closed", 1)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var details []string
	for rows.Next() {
		var id, parent, unused int
		var detail string
		if err := rows.Scan(&id, &parent, &unused, &detail); err != nil {
			t.Fatal(err)
		}
		details = append(details, detail)
	}
	if !strings.Contains(strings.Join(details, " "), "idx_edges_candidate_policy") {
		t.Fatalf("typed policy filter did not use its index: %v", details)
	}
}

func TestCanonicalV2QueriesUseRequiredIndexes(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	cases := []struct {
		query string
		args  []any
		index string
	}{
		{`SELECT id FROM nodes WHERE stable_id=?`, []any{"id"}, "idx_nodes_stable_id"},
		{`SELECT target_id FROM edges WHERE type='CANDIDATE_TARGET' AND callsite_stable_id=? AND viability='viable' ORDER BY target_symbol_id`, []any{"id"}, "idx_candidate_callsite_v2"},
		{`SELECT id FROM nodes WHERE node_type='derivation_fact' AND callsite_id=? AND target_symbol_id=? ORDER BY pass_kind,step_ordinal`, []any{"id", "target"}, "idx_derivation_fact_v2"},
		{`SELECT id FROM nodes WHERE node_type='completeness_fact' AND callsite_id=? ORDER BY pass_kind,fact_status`, []any{"id"}, "idx_completeness_fact_v2"},
		{`SELECT id FROM nodes WHERE node_type='query_policy_version' AND policy_name=? AND semantic_version=?`, []any{"policy", "1"}, "idx_policy_version_v2"},
	}
	for _, tc := range cases {
		rows, err := db.db.Query("EXPLAIN QUERY PLAN "+tc.query, tc.args...)
		if err != nil {
			t.Fatal(err)
		}
		var details []string
		for rows.Next() {
			var id, parent, unused int
			var detail string
			if err := rows.Scan(&id, &parent, &unused, &detail); err != nil {
				t.Fatal(err)
			}
			details = append(details, detail)
		}
		rows.Close()
		if !strings.Contains(strings.Join(details, " "), tc.index) {
			t.Fatalf("query did not use %s: %v", tc.index, details)
		}
	}
}

func TestCallsEndpointUniquenessCoalescesMigrationAndRejectsDuplicate(t *testing.T) {
	path := filepath.Join(t.TempDir(), "graph.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	targetID, err := db.InsertNode(&Node{Label: "Function", Name: "target", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(`DROP INDEX IF EXISTS idx_calls_unique_endpoint`); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err := db.db.Exec(`INSERT INTO edges(source_id,target_id,type,source_file) VALUES(?,?,'CALLS','main.py')`, sourceID, targetID); err != nil {
			t.Fatal(err)
		}
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	db, err = Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE source_id=? AND target_id=? AND type='CALLS'`, sourceID, targetID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("CALLS migration retained %d duplicate endpoint rows, want 1", count)
	}
	tx, err := db.db.Begin()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tx.Exec(`INSERT INTO edges(source_id,target_id,type,source_file) VALUES(?,?,'CALLS','main.py')`, sourceID, targetID); err == nil {
		_ = tx.Rollback()
		t.Fatal("CALLS endpoint uniqueness accepted a duplicate")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE source_id=? AND target_id=? AND type='CALLS'`, sourceID, targetID).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("rejected duplicate partially changed CALLS rows: %d", count)
	}
}

func TestAttachResolutionGraphRequiresExplicitDynamicAbstention(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.js", Language: "javascript"})
	if err != nil {
		t.Fatal(err)
	}
	targetID, err := db.InsertNode(&Node{Label: "Function", Name: "handler", QualifiedName: "handler", FilePath: "handlers.js", Language: "javascript"})
	if err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:     &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.js", Language: "javascript"},
		Callsite:   &ResolutionCallsite{CallsiteID: "dynamic", SourceID: sourceID, SourceFile: "main.js", SourceLine: 3, Callee: "handlers[name]", DispatchState: "dynamic", CandidateCount: 1, Mechanism: "dynamic"},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "handler", TargetNativeID: "handler", Ordinal: 0, Mechanism: "dynamic", DynamicDispatch: true, VerificationStatus: "candidate_only"}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("dynamic callsites must explicitly abstain with zero candidates")
	}
}

func TestQueryAttachedCandidatesReturnsZeroCandidateCallsites(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.js", Language: "javascript"})
	if err != nil {
		t.Fatal(err)
	}
	rows := []AttachedResolution{{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.js", Language: "javascript"},
		Callsite: &ResolutionCallsite{CallsiteID: "dynamic-zero", SourceID: sourceID, SourceFile: "main.js", SourceLine: 3, Callee: "handlers[name]", DispatchState: "dynamic", CandidateCount: 0, Mechanism: "dynamic", VerificationStatus: "abstained_dynamic"},
	}}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), rows); err != nil {
		t.Fatal(err)
	}
	evidence, err := db.QueryAttachedCandidates("handlers[name]")
	if err != nil {
		t.Fatal(err)
	}
	if len(evidence) != 1 || evidence[0].HasCandidate || evidence[0].TargetID != 0 || evidence[0].CandidateOrdinal != -1 || evidence[0].DispatchState != "unsupported" {
		t.Fatalf("zero-candidate callsite hidden or misrepresented: %+v", evidence)
	}
	if evidence[0].StructuralAuthority != "source_syntax" || evidence[0].TargetAuthority != "unsupported" {
		t.Fatalf("structural and target authority conflated: %+v", evidence[0])
	}
	if evidence[0].EvidenceSet != "partial" || evidence[0].DerivationKind != "dynamic_dispatch" || evidence[0].AbstentionReason == "" {
		t.Fatalf("dynamic refusal lost typed facts: %+v", evidence[0])
	}
	foundUnavailable := false
	for _, pass := range evidence[0].PassCoverage {
		if pass.PassKind == "legacy_unknown" && pass.Status == "unsupported" {
			foundUnavailable = true
		}
	}
	if !foundUnavailable {
		t.Fatalf("dynamic/framework pass coverage was hidden: %+v", evidence[0].PassCoverage)
	}
}

func TestAttachResolutionGraphRejectsUnknownReceiverOrigin(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, _ := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	targetID, _ := db.InsertNode(&Node{Label: "Method", Name: "run", QualifiedName: "T.run", FilePath: "main.py", Language: "python"})
	row := AttachedResolution{Source: &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"}, Callsite: &ResolutionCallsite{CallsiteID: "bad-origin", SourceID: sourceID, SourceFile: "main.py", Callee: "run", DispatchState: "candidate_only", CandidateCount: 1}, Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "t", TargetNativeID: "t", Ordinal: 0, ReceiverOrigin: "invented"}}}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("unknown receiver origin accepted")
	}
}

func TestAttachResolutionGraphRejectsUnknownDerivationKindAtomically(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, _ := db.InsertNode(&Node{Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"})
	targetID, _ := db.InsertNode(&Node{Label: "Function", Name: "run", FilePath: "main.py", Language: "python"})
	row := AttachedResolution{
		Source:     &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"},
		Callsite:   &ResolutionCallsite{CallsiteID: "unknown-kind", SourceID: sourceID, SourceFile: "main.py", Callee: "run", DispatchState: "candidate_only", CandidateCount: 1, Mechanism: "free_form_weighted_evidence"},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "target", TargetNativeID: "target", Ordinal: 0, Mechanism: "free_form_weighted_evidence"}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("unknown/free-form derivation kind was accepted")
	}
	var count int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type IN ('HAS_CALLSITE','CANDIDATE')`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("rejected derivation partially published %d graph edges", count)
	}
}

func TestAttachResolutionGraphRejectsKnownDerivationMissingTypedDetails(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, _ := db.InsertNode(&Node{Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"})
	targetID, _ := db.InsertNode(&Node{Label: "Method", Name: "run", FilePath: "main.py", Language: "python"})
	row := AttachedResolution{
		Source:     &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"},
		Callsite:   &ResolutionCallsite{CallsiteID: "missing-details", SourceID: sourceID, SourceFile: "main.py", Callee: "run", DispatchState: "candidate_only", CandidateCount: 1, Mechanism: "type_flow"},
		Candidates: []*ResolutionCandidate{{TargetID: targetID, TargetStableID: "target", TargetNativeID: "target", Ordinal: 0, Mechanism: "type_flow"}},
	}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("known derivation missing receiver details was accepted")
	}
}

func TestQueryAttachedCandidatesRejectsTamperedCompletionReceipt(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, _ := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	row := AttachedResolution{Source: &Node{ID: sourceID, Label: "Function", Name: "caller", FilePath: "main.py", Language: "python"}, Callsite: &ResolutionCallsite{CallsiteID: "zero", SourceID: sourceID, SourceFile: "main.py", Callee: "missing", DispatchState: "zero", CandidateCount: 0}}
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.db.Exec(`UPDATE project_meta SET value='tampered' WHERE key='graph_completion_receipt'`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.QueryAttachedCandidates("missing"); err == nil {
		t.Fatal("tampered graph/build receipt was trusted")
	}
}

func TestResolutionPublicationRollbackRemovesLegacyAndAttachedFactsTogether(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	targetID, err := db.InsertNode(&Node{Label: "Function", Name: "target", QualifiedName: "target", FilePath: "main.py", Language: "python"})
	if err != nil {
		t.Fatal(err)
	}
	tx, err := db.BeginTx()
	if err != nil {
		t.Fatal(err)
	}
	legacy := &Edge{SourceID: sourceID, TargetID: targetID, Type: "CALLS", SourceFile: "main.py", ResolutionMethod: "same_file", Confidence: 1.0, TrustTier: "CERTIFIED"}
	if err := BatchInsertEdgesTx(tx, []*Edge{legacy}); err != nil {
		t.Fatal(err)
	}
	row := AttachedResolution{
		Source:   &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.py", Language: "python"},
		Callsite: &ResolutionCallsite{CallsiteID: "broken", SourceID: sourceID, SourceFile: "main.py", SourceLine: 3, Callee: "target", DispatchState: "unique", CandidateCount: 1},
	}
	if err := AttachResolutionGraphTx(tx, testGraphIdentity("rev"), []AttachedResolution{row}); err == nil {
		t.Fatal("expected attached graph invariant failure")
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	var edges, callsites, complete int
	if err := db.db.QueryRow("SELECT count(*) FROM edges WHERE type='CALLS'").Scan(&edges); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow("SELECT count(*) FROM nodes WHERE label='Callsite'").Scan(&callsites); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow("SELECT count(*) FROM project_meta WHERE key='graph_resolution_complete'").Scan(&complete); err != nil {
		t.Fatal(err)
	}
	if edges != 0 || callsites != 0 || complete != 0 {
		t.Fatalf("mixed publication survived rollback: calls=%d callsites=%d complete_meta=%d", edges, callsites, complete)
	}
}
