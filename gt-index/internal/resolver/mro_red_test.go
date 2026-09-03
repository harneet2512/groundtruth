package resolver

import "testing"

// MRO ordering is ordering and nothing else. These tests pin that it reproduces
// the language's own linearisation where the language has one, that it refuses
// to invent an order for a hierarchy the language itself would reject, and that
// it never changes which candidates a callsite carries.

// diamond is the textbook C3 case:
//
//	   A(1)
//	  /    \
//	B(2)   C(3)
//	  \    /
//	   D(4)
//
// Python's linearisation of D is D, B, C, A.
func diamond() map[int64][]int64 {
	return map[int64][]int64{1: {}, 2: {1}, 3: {1}, 4: {2, 3}}
}

func equalMROIDs(a, b []int64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func TestC3LinearisesTheDiamondTheWayPythonDoes(t *testing.T) {
	got := LinearizeMRO("python", 4, diamond())
	if got.Status != MROStatusLinearized || got.Kind != MROKindC3 {
		t.Fatalf("unexpected linearisation outcome: %+v", got)
	}
	if want := []int64{4, 2, 3, 1}; !equalMROIDs(got.Order, want) {
		t.Fatalf("C3 order %v, want %v", got.Order, want)
	}
}

func TestAnInconsistentHierarchyIsPublishedAsInconsistentNotOrdered(t *testing.T) {
	// X(3) inherits (A,B); Y(4) inherits (B,A). Z(5) inheriting (X,Y) cannot be
	// linearised -- Python refuses to create the class. So does this.
	bases := map[int64][]int64{1: {}, 2: {}, 3: {1, 2}, 4: {2, 1}, 5: {3, 4}}
	got := LinearizeMRO("python", 5, bases)
	if got.Status != MROStatusInconsistent {
		t.Fatalf("status %q, want %q", got.Status, MROStatusInconsistent)
	}
	if len(got.Order) != 0 {
		t.Fatalf("an inconsistent hierarchy was ordered anyway: %v", got.Order)
	}
}

func TestAHierarchyCycleIsBoundedAndNamed(t *testing.T) {
	cyclic := map[int64][]int64{1: {2}, 2: {1}}
	if got := LinearizeMRO("python", 1, cyclic); got.Status != MROStatusCycle {
		t.Fatalf("C3 status %q, want %q", got.Status, MROStatusCycle)
	}
	if got := LinearizeMRO("java", 1, cyclic); got.Status != MROStatusCycle {
		t.Fatalf("single-chain status %q, want %q", got.Status, MROStatusCycle)
	}
}

func TestSingleInheritanceGrammarsLineariseTheChain(t *testing.T) {
	chain := map[int64][]int64{1: {}, 2: {1}, 3: {2}}
	got := LinearizeMRO("java", 3, chain)
	if got.Kind != MROKindSingleChain || got.Status != MROStatusLinearized {
		t.Fatalf("unexpected chain outcome: %+v", got)
	}
	if want := []int64{3, 2, 1}; !equalMROIDs(got.Order, want) {
		t.Fatalf("chain order %v, want %v", got.Order, want)
	}
	// Two declared bases in a single-inheritance grammar is a contradiction
	// between the grammar and the extraction. Saying so beats silently taking
	// the first base.
	if got := LinearizeMRO("java", 4, map[int64][]int64{1: {}, 2: {}, 4: {1, 2}}); got.Status != MROStatusInconsistent {
		t.Fatalf("multi-base single-inheritance status %q, want %q", got.Status, MROStatusInconsistent)
	}
}

func TestGrammarsWithoutLinearisationSaySoInsteadOfReturningNothing(t *testing.T) {
	for _, language := range []string{"yaml", "markdown", "sql", "toml", "css", ""} {
		if kind := MROKindForLanguage(language); kind != MROKindNone {
			t.Errorf("%s reported linearisation kind %q", language, kind)
		}
		got := LinearizeMRO(language, 4, diamond())
		if got.Status != MROStatusNoLanguageMRO || len(got.Order) != 0 {
			t.Errorf("%s: %+v, want a named empty result", language, got)
		}
	}
	if got := LinearizeMRO("python", 99, diamond()); got.Status != MROStatusClassUnknown {
		t.Fatalf("unknown class status %q, want %q", got.Status, MROStatusClassUnknown)
	}
	if got := LinearizeMRO("python", 1, diamond()); got.Status != MROStatusNoBases || !equalMROIDs(got.Order, []int64{1}) {
		t.Fatalf("a base-less class: %+v", got)
	}
	if kind := MROKindForLanguage("cpp"); kind != MROKindDepthFirst {
		t.Fatalf("C++ kind %q, want %q -- it has multiple inheritance but no linearisation rule", kind, MROKindDepthFirst)
	}
}

// mroCandidateMeta declares one `handle` method on each of B(2), C(3) and D(4).
func mroCandidateMeta() map[int64]NodeMeta {
	return map[int64]NodeMeta{
		20: {Name: "handle", File: "a.py", ParentID: 2},
		30: {Name: "handle", File: "a.py", ParentID: 3},
		40: {Name: "handle", File: "a.py", ParentID: 4},
	}
}

func TestInheritedCandidatesAreOrderedByTheLinearisationAndNothingElseChanges(t *testing.T) {
	candidates := []int64{30, 20, 40}
	got := OrderCandidatesByMRO("python", "inherited", candidates, diamond(), mroCandidateMeta())
	if got.Status != MROStatusOrdered || got.Kind != MROKindC3 {
		t.Fatalf("ordering did not run: %+v", got)
	}
	if want := []int64{40, 20, 30}; !equalMROIDs(got.Order, want) {
		t.Fatalf("MRO order %v, want %v (D, B, C)", got.Order, want)
	}
	if got.Ordered != 3 || len(got.Order) != len(candidates) {
		t.Fatalf("ordering changed the candidate set: %+v", got)
	}
}

func TestMROOrderingDeclinesWithAReasonRatherThanSilently(t *testing.T) {
	cases := []struct {
		name       string
		language   string
		mechanism  string
		candidates []int64
		meta       map[int64]NodeMeta
		wantReason string
	}{
		{"not an inherited callsite", "python", "name_match", []int64{30, 20}, mroCandidateMeta(), MROReasonNotInheritedMechanism},
		{"nothing to order", "python", "inherited", []int64{30}, mroCandidateMeta(), MROReasonBelowTwoCandidates},
		{"grammar has no linearisation", "yaml", "inherited", []int64{30, 20}, mroCandidateMeta(), MROStatusNoLanguageMRO},
		{"no candidate declares a class", "python", "inherited", []int64{50, 60}, map[int64]NodeMeta{50: {File: "a.py"}, 60: {File: "a.py"}}, MROReasonNoDefiningClass},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := OrderCandidatesByMRO(tc.language, tc.mechanism, tc.candidates, diamond(), tc.meta)
			if got.Reason != tc.wantReason {
				t.Fatalf("reason %q, want %q", got.Reason, tc.wantReason)
			}
			if got.Status != MROStatusNotApplicable || !equalMROIDs(got.Order, tc.candidates) {
				t.Fatalf("a declined ordering changed the set: %+v", got)
			}
		})
	}
}

func TestMROOrderingIsDeterministicUnderCandidateInputOrder(t *testing.T) {
	want := []int64{40, 20, 30}
	for _, input := range [][]int64{{20, 30, 40}, {40, 30, 20}, {30, 40, 20}} {
		got := OrderCandidatesByMRO("python", "inherited", append([]int64(nil), input...), diamond(), mroCandidateMeta())
		if !equalMROIDs(got.Order, want) {
			t.Fatalf("input %v ordered to %v, want %v", input, got.Order, want)
		}
	}
}
