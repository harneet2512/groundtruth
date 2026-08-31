package resolver

import "testing"

func TestVTAAdaptersAbstainTypedAndCanonicalizeEvidence(t *testing.T) {
	kinds := []VTAAdapterKind{
		VTAAdapterReflection,
		VTAAdapterDependencyInjection,
		VTAAdapterHigherOrder,
		VTAAdapterFFI,
		VTAAdapterGeneratedCode,
	}
	for _, kind := range kinds {
		t.Run(string(kind), func(t *testing.T) {
			incomplete := AnalyzeVTAAdapters([]VTAAdapterObservation{{
				Kind: kind, CallsiteID: "call-1", CandidateNodeIDs: []int64{5, 3, 5}, EvidenceIDs: []string{"edge-b", "edge-a", "edge-a"},
			}})
			if len(incomplete) != 1 || incomplete[0].Status != "abstained" || incomplete[0].AbstentionReason != "incomplete_adapter_evidence" {
				t.Fatalf("incomplete adapter=%+v, want typed abstention", incomplete)
			}
			complete := AnalyzeVTAAdapters([]VTAAdapterObservation{{
				Kind: kind, CallsiteID: "call-1", Complete: true, CandidateNodeIDs: []int64{5, 3, 5}, EvidenceIDs: []string{"edge-b", "edge-a", "edge-a"},
			}})
			if len(complete) != 1 || complete[0].Status != "adapted" || complete[0].AbstentionReason != "" {
				t.Fatalf("complete adapter=%+v, want adapted", complete)
			}
			if !equalInt64s(complete[0].CandidateNodeIDs, []int64{3, 5}) || len(complete[0].EvidenceIDs) != 2 || complete[0].EvidenceIDs[0] != "edge-a" || complete[0].EvidenceIDs[1] != "edge-b" {
				t.Fatalf("adapter canonicalization=%+v, want candidates [3 5] and evidence [edge-a edge-b]", complete[0])
			}
			if complete[0].SelectedTargetNodeID != nil {
				t.Fatalf("adapter selected a target without an independent selection proof: %+v", complete[0])
			}
		})
	}
}

