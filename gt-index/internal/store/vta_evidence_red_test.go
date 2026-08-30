package store

import "testing"

func TestVTAVariableFlowIsOpenEvidence(t *testing.T) {
	_, evidence, reason, err := resolutionDerivation("vta", "candidate_only", 1)
	if err != nil {
		t.Fatal(err)
	}
	if evidence != "open" || reason == "" {
		t.Fatalf("VTA evidence=%q reason=%q, want open with an explicit reason", evidence, reason)
	}
}
