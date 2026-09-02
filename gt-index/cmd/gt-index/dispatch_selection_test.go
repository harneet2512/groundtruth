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
