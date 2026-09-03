package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// The `step` projection is an ordinal into the pass order. If the resolver kept
// its own copy of that order, a step could name a pass the resolver never ran
// in that position. One slice, read by both.
func TestResolverAndStoreShareOnePassOrder(t *testing.T) {
	if len(resolutionPassOrder) != len(store.ResolutionPassOrder) {
		t.Fatalf("pass order lengths differ: resolver %d, store %d", len(resolutionPassOrder), len(store.ResolutionPassOrder))
	}
	for i := range resolutionPassOrder {
		if resolutionPassOrder[i] != store.ResolutionPassOrder[i] {
			t.Fatalf("pass %d differs: resolver %q, store %q", i, resolutionPassOrder[i], store.ResolutionPassOrder[i])
		}
	}
	// Every pass the tracker can report must have a step, or a HAS_CALLSITE edge
	// won by that pass would project an unmapped step.
	for _, pass := range resolutionPassOrder {
		if _, mapped := store.ResolutionStepOrdinal(pass); !mapped {
			t.Fatalf("pass %q has no step ordinal", pass)
		}
	}
}

// Narrowing and MRO are projections over a candidate set, not passes that can
// resolve a callsite. They must never appear in the pass order, because
// resolutionCoverage promotes the matching pass to the edge's pass_kind.
func TestProjectionPassesAreNotResolutionPasses(t *testing.T) {
	for _, pass := range resolutionPassOrder {
		if pass == OverloadNarrowingPass || pass == MROPass {
			t.Fatalf("projection pass %q is in the resolution pass order", pass)
		}
	}
	if _, mapped := store.ResolutionStepOrdinal(OverloadNarrowingPass); mapped {
		t.Fatal("overload narrowing was given a resolution step")
	}
	if _, mapped := store.ResolutionStepOrdinal(MROPass); mapped {
		t.Fatal("mro was given a resolution step")
	}
	if NarrowingStatusNarrowed == "completed_match" || MROStatusOrdered == "completed_match" {
		t.Fatal("a projection status can win a callsite's pass_kind")
	}
}
