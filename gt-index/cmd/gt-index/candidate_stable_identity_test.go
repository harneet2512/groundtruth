package main

import (
	"reflect"
	"testing"
)

// A candidate is identified by its target's stable id, so one callsite cannot
// carry the same target twice. Several distinct nodes can nonetheless share a
// stable id -- aiomonitor resolves seven distinct nodes named "set" to one --
// and publishing each as its own candidate produced duplicate
// CANDIDATE_TARGET edge ids, a UNIQUE violation on edges.stable_id that
// discarded the ENTIRE graph:
//
//	attach graph-native resolution evidence: insert v2 candidate edge:
//	UNIQUE constraint failed: edges.stable_id
//
// It is not only a crash. candidate_count is what decides candidate_only
// versus ambiguous dispatch, so the duplicates also pushed callsites with one
// real target into ambiguous.

func stableIDs(m map[int64]string) func(int64) (string, bool) {
	return func(id int64) (string, bool) {
		s, ok := m[id]
		return s, ok
	}
}

func TestDuplicateStableIdentitiesCollapseToOneCandidate(t *testing.T) {
	lookup := stableIDs(map[int64]string{
		1444: "set-stable",
		1474: "set-stable",
		1519: "set-stable",
		9001: "other-stable",
	})
	got := dedupeCandidatesByStableIdentity([]int64{1444, 1474, 9001, 1519}, nil, lookup)
	want := []int64{1444, 9001}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// Dropping the selected node would leave the callsite with no candidate marked
// selected, so the selected node must survive as its identity's representative.
func TestSelectedNodeSurvivesAsItsIdentityRepresentative(t *testing.T) {
	lookup := stableIDs(map[int64]string{
		1444: "set-stable",
		1474: "set-stable",
		1519: "set-stable",
	})
	selected := int64(1519)
	got := dedupeCandidatesByStableIdentity([]int64{1444, 1474, 1519}, &selected, lookup)
	want := []int64{1519}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// Order is evidence: it decides candidate ordinals, so survivors must keep
// their original relative order rather than being reordered by the collapse.
func TestSurvivingCandidatesKeepTheirOrder(t *testing.T) {
	lookup := stableIDs(map[int64]string{
		10: "a", 11: "b", 12: "a", 13: "c", 14: "b",
	})
	got := dedupeCandidatesByStableIdentity([]int64{10, 11, 12, 13, 14}, nil, lookup)
	want := []int64{10, 11, 13}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

// A node absent from the symbol table has no stable identity to compare, and
// the publication loop already skips it. Dropping it here instead would break
// the candidate-conservation check, which compares the two lengths.
func TestUnknownNodesAreLeftForTheConservationCheck(t *testing.T) {
	lookup := stableIDs(map[int64]string{10: "a"})
	got := dedupeCandidatesByStableIdentity([]int64{10, 77}, nil, lookup)
	want := []int64{10, 77}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}
