package store

import (
	"strings"
	"testing"
)

// Graph publication is atomic, so a candidate the store refuses to derive
// discards the entire index -- the run then proceeds with no graph at all.
// Two refusals were reachable on ordinary repositories and both fired on the
// DeepSWE arktype task.

// An empty mechanism is a supported state, not a defect. resolutionDerivation
// maps it onto the global_name derivation, alongside verified_unique,
// name_match, unknown_legacy and external, and it has already done so by the
// time validation runs, since the kind handed to the validator is its output.
func TestEmptyMechanismIsAcceptedBecauseDerivationMapsIt(t *testing.T) {
	kind, evidenceSet, _, err := resolutionDerivation("", "", 1)
	if err != nil {
		t.Fatalf("resolutionDerivation rejected an empty mechanism: %v", err)
	}
	if kind != "global_name" || evidenceSet != "partial" {
		t.Fatalf("empty mechanism mapped to %q/%q, want global_name/partial", kind, evidenceSet)
	}

	candidate := &ResolutionCandidate{
		TargetStableID: "stable-1",
		TargetNativeID: "native-1",
		Mechanism:      "",
	}
	if err := validateCandidateDerivation(kind, candidate); err != nil {
		t.Fatalf("validation rejected a candidate its own derivation mapping accepts: %v", err)
	}
}

// The target identities stay required -- nothing downstream can derive a
// candidate it cannot name -- but the error must say which one is absent.
// This aborts the whole index, so "one of these is empty" forces a full
// re-index to learn which, and the index is the expensive part.
func TestMissingTargetIdentitiesAreRejectedAndNamed(t *testing.T) {
	cases := []struct {
		missing   string
		candidate *ResolutionCandidate
	}{
		{"target_stable_id", &ResolutionCandidate{TargetNativeID: "native-1", Mechanism: "name_match"}},
		{"target_native_id", &ResolutionCandidate{TargetStableID: "stable-1", Mechanism: "name_match"}},
	}
	for _, tc := range cases {
		err := validateCandidateDerivation("global_name", tc.candidate)
		if err == nil {
			t.Errorf("%s: expected rejection, got none", tc.missing)
			continue
		}
		if !strings.Contains(err.Error(), tc.missing) {
			t.Errorf("%s: error did not name the missing field: %v", tc.missing, err)
		}
	}
}

func TestBothMissingIdentitiesAreReportedTogether(t *testing.T) {
	err := validateCandidateDerivation("global_name", &ResolutionCandidate{Mechanism: "name_match"})
	if err == nil {
		t.Fatal("expected rejection when both identities are absent")
	}
	for _, want := range []string{"target_stable_id", "target_native_id"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error did not name %s: %v", want, err)
		}
	}
}

// The store's mapper and the resolver's Method vocabulary are two lists that
// must agree. When they drifted, the graph did not degrade -- a single
// unmapped mechanism aborted the whole index. unique_method drifted that way.
//
// This pins every mechanism a ResolutionCallsite can carry: the resolver's
// ResolvedCall.Method values, the states the resolver writes onto the trace
// directly, cmd/gt-index's VTA attribution, and the unattributed default.
func TestEveryCallsiteMechanismIsMapped(t *testing.T) {
	mechanisms := []string{
		"same_file", "inherited", "import", "import_type", "type_flow",
		"return_type", "impl_method", "unique_method", "name_match",
		"verified_unique",
		"external", "unknown_legacy",
		"vta", "",
	}
	for _, m := range mechanisms {
		kind, evidenceSet, _, err := resolutionDerivation(m, "", 1)
		if err != nil {
			t.Errorf("mechanism %q is unmapped -- this aborts the whole graph: %v", m, err)
			continue
		}
		if kind == "" || evidenceSet == "" {
			t.Errorf("mechanism %q mapped to empty kind/evidenceSet (%q/%q)", m, kind, evidenceSet)
		}
	}
}

// unique_method is name-uniqueness with no receiver proof. The resolver runs
// it under the global_name pass and caps it at CANDIDATE (0.6); its emit site
// states outright that reading it as a type-derived fact is wrong, so it must
// not land on single_implementation.
func TestUniqueMethodIsNameDerivedNotTypeDerived(t *testing.T) {
	kind, evidenceSet, _, err := resolutionDerivation("unique_method", "", 1)
	if err != nil {
		t.Fatalf("unique_method unmapped: %v", err)
	}
	if kind != "global_name" || evidenceSet != "partial" {
		t.Fatalf("unique_method mapped to %q/%q, want global_name/partial", kind, evidenceSet)
	}
}
