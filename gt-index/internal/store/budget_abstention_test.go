package store

import (
	"encoding/json"
	"path/filepath"
	"strings"
	"testing"
)

// budgetAbstentionRow builds a minimal two-candidate interface callsite. The
// caller supplies the abstention reason so a test can express "this callsite
// stopped because a budget stopped it" without also changing the shape of the
// evidence around it.
func budgetAbstentionRow(t *testing.T, db *DB, callsiteID, reason string) AttachedResolution {
	t.Helper()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetA, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplA.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetB, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplB.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	return AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{
			CallsiteID: callsiteID, SourceID: sourceID, SourceFile: "main.go", SourceLine: 8,
			Callee: "Run", DispatchState: "candidate_only", DispatchForm: "interface",
			CandidateCount: 2, Mechanism: "impl_method", AbstentionReason: reason,
			PassCoverage: []ResolutionPassCoverage{{PassKind: "vta", Status: "partial", Reason: reason}},
		},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "impl_method", DeclaredScope: "ImplA.Run"},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "impl_method", DeclaredScope: "ImplB.Run"},
		},
	}
}

// A budget that stops an analysis must leave evidence that it stopped it.
// Silence is indistinguishable from a complete analysis that found nothing, so
// the reason has to reach both surfaces a reader actually consults: the
// HAS_CALLSITE edge column and the Callsite node's signature JSON.
func TestBudgetAbstentionIsPersistedOnCallsiteEdgeAndNode(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	row := budgetAbstentionRow(t, db, "budget-abstention", "budget_exhausted")
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var edgeReason string
	if err := db.db.QueryRow(`SELECT COALESCE(abstention_reason,'') FROM edges WHERE type='HAS_CALLSITE'`).Scan(&edgeReason); err != nil {
		t.Fatal(err)
	}
	if edgeReason != "budget_exhausted" {
		t.Fatalf("HAS_CALLSITE abstention_reason=%q, want budget_exhausted -- a budgeted abstention published as silence", edgeReason)
	}
	var signature string
	if err := db.db.QueryRow(`SELECT COALESCE(signature,'') FROM nodes WHERE label='Callsite'`).Scan(&signature); err != nil {
		t.Fatal(err)
	}
	var meta map[string]any
	if err := json.Unmarshal([]byte(signature), &meta); err != nil {
		t.Fatalf("callsite signature is not the JSON fact envelope: %v", err)
	}
	if meta["abstention_reason"] != "budget_exhausted" {
		t.Fatalf("callsite node abstention_reason=%v, want budget_exhausted", meta["abstention_reason"])
	}
}

// The abstention vocabulary is closed on purpose: a reason nobody can enumerate
// cannot be queried, and a typo would publish a graph that reads as degraded
// for a reason that does not exist.
func TestUnknownCallsiteAbstentionReasonIsRejectedByName(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	row := budgetAbstentionRow(t, db, "bad-abstention", "budget_exceeeded")
	err = db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row})
	if err == nil {
		t.Fatal("an abstention reason outside the closed vocabulary was published")
	}
	if !strings.Contains(err.Error(), "budget_exceeeded") {
		t.Fatalf("rejection did not name the offending reason: %v", err)
	}
}

// The per-candidate existence check is hoisted into a transaction-scoped cache
// so a fact shared across candidates costs one SELECT. A cache that forgets the
// binding it cached would let a second, conflicting definition of the same fact
// ID through -- which is exactly the corruption the SELECT existed to stop.
func TestHoistedVTAFactCacheStillRejectsConflictingRebinding(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	sourceID, err := db.InsertNode(&Node{Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetA, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplA.Run", FilePath: "a.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	targetB, err := db.InsertNode(&Node{Label: "Method", Name: "Run", QualifiedName: "ImplB.Run", FilePath: "b.go", Language: "go"})
	if err != nil {
		t.Fatal(err)
	}
	const factID = "vta_source_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	row := AttachedResolution{
		Source: &Node{ID: sourceID, Label: "Function", Name: "caller", QualifiedName: "caller", FilePath: "main.go", Language: "go"},
		Callsite: &ResolutionCallsite{
			CallsiteID: "rebinding", SourceID: sourceID, SourceFile: "main.go", SourceLine: 8,
			Callee: "Run", DispatchState: "ambiguous", DispatchForm: "interface",
			CandidateCount: 2, Mechanism: "vta",
			PassCoverage: []ResolutionPassCoverage{{PassKind: "vta", Status: "partial", Reason: "candidate_only_flow_evidence"}},
		},
		Candidates: []*ResolutionCandidate{
			{TargetID: targetA, TargetStableID: "a", TargetNativeID: "a", Ordinal: 0, Mechanism: "vta",
				FlowSourceStableIDs: []string{factID},
				FlowSourceFacts:     []ResolutionFlowFact{{StableID: factID, Kind: "source", CallsiteID: "rebinding", TargetStableID: "a", Payload: "runner=ImplA"}}},
			{TargetID: targetB, TargetStableID: "b", TargetNativeID: "b", Ordinal: 1, Mechanism: "vta",
				FlowSourceStableIDs: []string{factID},
				FlowSourceFacts:     []ResolutionFlowFact{{StableID: factID, Kind: "source", CallsiteID: "rebinding", TargetStableID: "b", Payload: "runner=ImplB"}}},
		},
	}
	err = db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row})
	if err == nil {
		t.Fatal("a second candidate rebound an already-published VTA fact ID")
	}
	if !strings.Contains(err.Error(), "conflicting graph data") {
		t.Fatalf("rebinding was rejected for the wrong reason: %v", err)
	}
}

// pass_coverage is a callsite fact. Copying it onto every candidate edge stored
// the same blob once per candidate -- 713 MB of the 845 MB of candidate-edge
// JSON on a 250-file boa sample, and the reason an atomic publication of the
// whole repository never terminated. The authoritative copy belongs on the
// HAS_CALLSITE edge; the candidate edge carries the empty projection the
// graph query already substitutes for that column.
func TestCandidateEdgesDoNotDuplicateCallsiteCoverage(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	row := budgetAbstentionRow(t, db, "coverage-projection", "")
	if err := db.AttachResolutionGraph(testGraphIdentity("rev"), []AttachedResolution{row}); err != nil {
		t.Fatal(err)
	}
	var callsiteCoverage string
	if err := db.db.QueryRow(`SELECT COALESCE(pass_coverage,'') FROM edges WHERE type='HAS_CALLSITE'`).Scan(&callsiteCoverage); err != nil {
		t.Fatal(err)
	}
	var coverage []ResolutionPassCoverage
	if err := json.Unmarshal([]byte(callsiteCoverage), &coverage); err != nil || len(coverage) == 0 {
		t.Fatalf("HAS_CALLSITE lost the authoritative pass coverage: %q (%v)", callsiteCoverage, err)
	}
	rows, err := db.db.Query(`SELECT COALESCE(pass_coverage,'') FROM edges WHERE type='CANDIDATE'`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	seen := 0
	for rows.Next() {
		var candidateCoverage string
		if err := rows.Scan(&candidateCoverage); err != nil {
			t.Fatal(err)
		}
		seen++
		if candidateCoverage != "[]" {
			t.Fatalf("candidate edge duplicated %d bytes of callsite coverage: %q", len(candidateCoverage), candidateCoverage)
		}
	}
	if seen != 2 {
		t.Fatalf("expected two candidate edges, saw %d", seen)
	}
	// The coverage a reader consumes is rebuilt from the completeness facts, so
	// removing the duplicate costs no evidence.
	var completeness int
	if err := db.db.QueryRow(`SELECT count(*) FROM nodes WHERE node_type='completeness_fact'`).Scan(&completeness); err != nil {
		t.Fatal(err)
	}
	if completeness == 0 {
		t.Fatal("coverage is no longer reconstructible: no completeness facts were published")
	}
}
