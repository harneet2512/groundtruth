package store

import (
	"encoding/json"
	"testing"
)

func TestVTACandidateProvenanceCarriesSourceAndEdgeFacts(t *testing.T) {
	candidate := &ResolutionCandidate{
		TargetID:           7,
		TargetStableID:     "target",
		TargetNativeID:     "target-native",
		Mechanism:          "vta",
		FlowSourceStableIDs: []string{"assign:runner"},
		FlowEdgeStableIDs:   []string{"edge:caller->runner"},
	}
	steps := candidateProvenance(nil, candidate)
	got := make(map[string]string)
	for _, step := range steps {
		got[step.Kind] = step.Value
	}
	for kind, want := range map[string][]string{
		"flow_source_stable_ids": {"assign:runner"},
		"flow_edge_stable_ids":   {"edge:caller->runner"},
	} {
		var values []string
		if err := json.Unmarshal([]byte(got[kind]), &values); err != nil {
			t.Fatalf("%s was not serialized as typed IDs: %q: %v", kind, got[kind], err)
		}
		if len(values) != len(want) || values[0] != want[0] {
			t.Fatalf("%s=%v, want %v", kind, values, want)
		}
	}
}
