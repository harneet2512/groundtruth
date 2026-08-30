package store

import "testing"

func TestVTAPassCarriesTypedFlowIDs(t *testing.T) {
	pass := ResolutionPassCoverage{PassKind: "vta", FlowTypeStableIDs: []string{"flow-type"}}
	if len(pass.FlowTypeStableIDs) != 1 || pass.FlowTypeStableIDs[0] != "flow-type" {
		t.Fatalf("typed VTA flow facts were not retained: %+v", pass)
	}
}
