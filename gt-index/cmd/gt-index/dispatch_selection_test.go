package main

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/resolver"
)

func TestSelectedTargetAuthorityRequiresUniqueDispatch(t *testing.T) {
	selected := int64(42)
	for _, state := range []resolver.DispatchState{
		resolver.DispatchCandidateOnly,
		resolver.DispatchAmbiguous,
		resolver.DispatchDynamic,
		resolver.DispatchZero,
	} {
		if got := selectedTargetForDispatch(string(state), &selected); got != nil {
			t.Fatalf("dispatch %q retained selected target %d", state, *got)
		}
	}
	if got := selectedTargetForDispatch(string(resolver.DispatchUnique), &selected); got == nil || *got != selected {
		t.Fatalf("unique dispatch lost selected target: %v", got)
	}
}

func TestCandidateDispatchStatePreservesZeroCandidateAbstention(t *testing.T) {
	cases := []struct {
		count int
		want  resolver.DispatchState
	}{
		{count: 0, want: resolver.DispatchZero},
		{count: 1, want: resolver.DispatchCandidateOnly},
		{count: 2, want: resolver.DispatchAmbiguous},
	}
	for _, tc := range cases {
		if got := candidateDispatchStateForCount(tc.count); got != string(tc.want) {
			t.Fatalf("candidate count %d: dispatch=%q want %q", tc.count, got, tc.want)
		}
	}
}
