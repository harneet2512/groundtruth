package main

import "testing"

// "vta" publishes the variable_type_flow derivation, and the store holds every
// candidate of such a callsite to carrying flow proof. That invariant holds
// only while the published candidate set IS the flow set.
//
// The partial-VTA branch deliberately merges conservative hierarchy candidates
// in so incomplete flow does not erase them. Those candidates have no flow
// proof, so claiming "vta" over them asserts evidence that does not exist. The
// store rejects it -- and because publication is atomic, that rejection
// discards the whole graph rather than one edge.
func TestVTACallsiteMechanismMatchesPublishedCandidateSet(t *testing.T) {
	cases := []struct {
		name      string
		vta       []int64
		published []int64
		want      string
	}{
		{"pure flow set claims flow", []int64{7, 9}, []int64{7, 9}, "vta"},
		{"single flow candidate claims flow", []int64{7}, []int64{7}, "vta"},
		{"merged hierarchy set claims implementors", []int64{7}, []int64{7, 9}, "impl_method"},
		{"no flow candidates claims nothing", nil, []int64{7, 9}, ""},
	}
	for _, tc := range cases {
		if got := vtaCallsiteMechanism(tc.vta, tc.published); got != tc.want {
			t.Errorf("%s: vtaCallsiteMechanism(%v, %v) = %q, want %q",
				tc.name, tc.vta, tc.published, got, tc.want)
		}
	}
}
