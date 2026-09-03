package store

import (
	"path/filepath"
	"strings"
	"testing"
)

// `reason` and `step` are projections. These tests pin that they are rendered
// from the derivation columns already on the edge -- never written alongside it
// -- so a justification cannot drift from what produced the edge.

func TestStepIsTheOrdinalOfThePassInThePassOrder(t *testing.T) {
	for index, pass := range ResolutionPassOrder {
		step, mapped := ResolutionStepOrdinal(pass)
		if !mapped || step != index+1 {
			t.Fatalf("pass %q projected step %d (mapped=%v), want %d", pass, step, mapped, index+1)
		}
	}
	// A derivation kind standing in for a pass that never reported is not a
	// step. It must be unmistakable, not zero.
	step, mapped := ResolutionStepOrdinal("variable_type_flow")
	if mapped || step != ResolutionStepUnmapped || step >= 0 {
		t.Fatalf("an unmapped pass projected step %d (mapped=%v)", step, mapped)
	}
}

func TestReasonIsRenderedFromTheDerivationFactsAlone(t *testing.T) {
	cases := []struct {
		name                                    string
		kind, evidenceSet, abstention, passKind string
		candidateCount                          int
		wantContains                            []string
	}{
		{
			name: "a closed lexical binding", kind: "lexical_binding", evidenceSet: "closed",
			passKind: "lexical_binding", candidateCount: 1,
			wantContains: []string{"step 1 of 7 (lexical_binding)", "lexical_binding derivation over 1 candidate", "evidence set closed", "no abstention"},
		},
		{
			name: "an abstaining dynamic dispatch", kind: "dynamic_dispatch", evidenceSet: "partial",
			abstention: "dynamic_target_not_statically_proven", passKind: "dynamic_framework", candidateCount: 0,
			wantContains: []string{"step 7 of 7 (dynamic_framework)", "over 0 candidates", "evidence set partial", "abstained: dynamic_target_not_statically_proven"},
		},
		{
			name: "a pass that never reported", kind: "variable_type_flow", evidenceSet: "open",
			abstention: "candidate_only_flow_evidence", passKind: "variable_type_flow", candidateCount: 4,
			wantContains: []string{"step unmapped (variable_type_flow)", "over 4 candidates"},
		},
		{
			name: "missing facts are named, not blank", kind: "", evidenceSet: "", passKind: "global_name", candidateCount: 2,
			wantContains: []string{"step 6 of 7 (global_name)", "unstated derivation", "evidence set unstated"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ResolutionReason(tc.kind, tc.evidenceSet, tc.abstention, tc.passKind, tc.candidateCount)
			for _, want := range tc.wantContains {
				if !strings.Contains(got, want) {
					t.Errorf("reason %q does not contain %q", got, want)
				}
			}
		})
	}
	if ResolutionReason("", "", "", "", 0) != "" {
		t.Fatal("an edge with no derivation at all was given a reason")
	}
}

// projectionCallsite is a two-candidate ambiguous callsite: enough to produce
// one HAS_CALLSITE and two CANDIDATE edges through the real attachment path.
func projectionCallsite() (*ResolutionCallsite, []*ResolutionCandidate, *Node) {
	callsite := &ResolutionCallsite{
		CallsiteID: "callsite-1", CallsiteOrdinal: 1, RepositoryRevision: "rev",
		SourceStableID: "src", SourceNativeID: "src-native", SourceID: 1,
		SourceLine: 4, SourceFile: "main.py", Callee: "handle", Language: "python",
		DispatchState: "ambiguous", CandidateCount: 2, Mechanism: "name_match",
		VerificationStatus: "candidate_only", RepoID: "repo", FileNodeID: 1,
		CallerSymbolID: "src", ASTPath: "0/1", ByteStart: 10, ByteEnd: 20,
		DispatchForm: "virtual", ParseState: "complete",
	}
	complete := true
	candidates := []*ResolutionCandidate{
		{CallsiteID: "callsite-1", TargetID: 2, TargetStableID: "t2", TargetNativeID: "n2", Ordinal: 0, Mechanism: "name_match", ParserComplete: &complete, VerificationStatus: "candidate_only"},
		{CallsiteID: "callsite-1", TargetID: 3, TargetStableID: "t3", TargetNativeID: "n3", Ordinal: 1, Mechanism: "name_match", ParserComplete: &complete, VerificationStatus: "candidate_only"},
	}
	source := &Node{Name: "caller", FilePath: "main.py", Language: "python", Label: "Function"}
	return callsite, candidates, source
}

func TestProjectionRendersReasonAndStepOntoPublishedResolutionEdges(t *testing.T) {
	db, err := Open(filepath.Join(t.TempDir(), "graph.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	callsite, candidates, source := projectionCallsite()
	for _, node := range []*Node{source, {Name: "handle", FilePath: "a.py", Language: "python", Label: "Function"}, {Name: "handle", FilePath: "b.py", Language: "python", Label: "Function"}} {
		if _, err := db.InsertNode(node); err != nil {
			t.Fatal(err)
		}
	}
	identity := testGraphIdentity("rev")
	if err := db.AttachResolutionGraph(identity, []AttachedResolution{{Callsite: callsite, Source: source, Candidates: candidates}}); err != nil {
		t.Fatal(err)
	}

	// Before the projection runs, every resolution edge has a NULL reason: an
	// unprojected graph must not read as a graph with empty justifications.
	var unprojected int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type IN ('HAS_CALLSITE','CANDIDATE') AND resolution_reason IS NOT NULL`).Scan(&unprojected); err != nil {
		t.Fatal(err)
	}
	if unprojected != 0 {
		t.Fatalf("%d resolution edges carried a reason before the projection ran", unprojected)
	}

	report, err := db.ProjectResolutionEdgeReasons()
	if err != nil {
		t.Fatal(err)
	}
	if report.CallsiteEdges != 1 || report.CandidateEdges != 2 {
		t.Fatalf("projected %d callsite and %d candidate edges, want 1 and 2", report.CallsiteEdges, report.CandidateEdges)
	}

	var reason string
	var step int
	if err := db.db.QueryRow(`SELECT resolution_reason, resolution_step FROM edges WHERE type='HAS_CALLSITE'`).Scan(&reason, &step); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(reason, "global_name derivation over 2 candidates") {
		t.Fatalf("HAS_CALLSITE reason %q does not restate its own derivation facts", reason)
	}
	wantStep, _ := ResolutionStepOrdinal("global_name")
	if step != wantStep {
		t.Fatalf("HAS_CALLSITE step %d, want %d", step, wantStep)
	}

	// The projection is a pure function of the stored columns, so re-running it
	// over the same graph must be a no-op on the values.
	before := reason
	if _, err := db.ProjectResolutionEdgeReasons(); err != nil {
		t.Fatal(err)
	}
	if err := db.db.QueryRow(`SELECT resolution_reason FROM edges WHERE type='HAS_CALLSITE'`).Scan(&reason); err != nil {
		t.Fatal(err)
	}
	if reason != before {
		t.Fatalf("projection is not idempotent: %q then %q", before, reason)
	}

	// It touches only resolution edges. Everything else keeps a NULL reason.
	var others int
	if err := db.db.QueryRow(`SELECT count(*) FROM edges WHERE type NOT IN ('HAS_CALLSITE','CANDIDATE') AND resolution_reason IS NOT NULL`).Scan(&others); err != nil {
		t.Fatal(err)
	}
	if others != 0 {
		t.Fatalf("%d non-resolution edges were given a reason", others)
	}
}
