package community

import (
	"reflect"
	"strings"
	"testing"
)

func TestHeuristicLabelTiers(t *testing.T) {
	cases := []struct {
		name    string
		members []string
		want    string
	}{
		{
			name:    "shared directory prefix wins",
			members: []string{"src/parser/lex.go", "src/parser/parse.go", "src/parser/ast.go"},
			want:    "src/parser/",
		},
		{
			name:    "longest COMMON prefix, not the deepest path",
			members: []string{"src/parser/lex.go", "src/parser/deep/ast.go", "src/emit/out.go"},
			want:    "src/",
		},
		{
			name:    "no shared prefix falls back to the plurality directory",
			members: []string{"a/one.go", "b/two.go", "b/three.go", "b/four.go"},
			want:    "b/*",
		},
		{
			name:    "scattered root files get an honestly poor label",
			members: []string{"main.go", "zeta.go"},
			want:    "main.go +1",
		},
		{
			name:    "a single member is named by itself",
			members: []string{"src/only.go"},
			want:    "src/only.go",
		},
		{
			name:    "windows separators do not produce a different label",
			members: []string{"src\\parser\\lex.go", "src\\parser\\parse.go"},
			want:    "src/parser/",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := heuristicLabel(c.members); got != c.want {
				t.Errorf("heuristicLabel(%v) = %q, want %q", c.members, got, c.want)
			}
		})
	}
}

// TestHeuristicLabelIsAlwaysNonEmpty: the whole point of keeping a heuristic
// label alongside the enriched one is that it is always there to fall back to.
func TestHeuristicLabelIsAlwaysNonEmpty(t *testing.T) {
	inputs := [][]string{
		{"a.go", "b.go"},
		{"x/y/z.go"},
		{"a/b/c.go", "d/e/f.go"},
		{"weird name.with.dots", "another one"},
	}
	for _, in := range inputs {
		if got := heuristicLabel(in); got == "" {
			t.Errorf("heuristicLabel(%v) returned empty", in)
		}
	}
}

func TestKeywordsAreRankedAndBounded(t *testing.T) {
	members := []string{
		"src/parser/lexNode.go",
		"src/parser/parseNode.go",
		"src/parser/astNode.go",
	}
	got := keywords(members)
	if len(got) == 0 {
		t.Fatal("no keywords")
	}
	// "parser", "src", "node" and "go" each appear in all three members, so
	// they rank first, ties broken lexicographically.
	want := []string{"go", "node", "parser", "src"}
	if !reflect.DeepEqual(got[:4], want) {
		t.Errorf("top keywords = %v, want %v (full list %v)", got[:4], want, got)
	}
	if len(got) > MaxKeywords {
		t.Errorf("%d keywords, cap is %d", len(got), MaxKeywords)
	}
	// A term counted once per member, not once per occurrence: "node" appears
	// in three members, so it cannot outrank "parser", which also appears in
	// three, on a per-occurrence count.
	for _, k := range got {
		if len(k) < minKeywordLength {
			t.Errorf("keyword %q is shorter than the floor", k)
		}
		if k != strings.ToLower(k) {
			t.Errorf("keyword %q is not lowercased", k)
		}
	}
}

func TestSplitWordsHandlesCamelAndSeparators(t *testing.T) {
	cases := map[string][]string{
		"parseNode":     {"parse", "node"},
		"parse_node":    {"parse", "node"},
		"HTTPServer":    {"httpserver"},
		"resolve-v2":    {"resolve", "v2"},
		"":              nil,
		"__":            nil,
		"alreadylower":  {"alreadylower"},
		"mixedCase_Two": {"mixed", "case", "two"},
	}
	for in, want := range cases {
		got := splitWords(in)
		if !reflect.DeepEqual(got, want) {
			t.Errorf("splitWords(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestCommonDirPrefix(t *testing.T) {
	cases := []struct {
		members []string
		want    string
	}{
		{[]string{"a/b/c.go", "a/b/d.go"}, "a/b"},
		{[]string{"a/b/c.go", "a/x/d.go"}, "a"},
		{[]string{"a/b/c.go", "x/y/d.go"}, ""},
		{[]string{"c.go", "d.go"}, ""},
		{[]string{"a/b/c.go", "c.go"}, ""},
	}
	for _, c := range cases {
		if got := commonDirPrefix(c.members); got != c.want {
			t.Errorf("commonDirPrefix(%v) = %q, want %q", c.members, got, c.want)
		}
	}
}

// TestCommunityIDIsContentAddressed: the same member set gets the same id, a
// different one gets a different id, and member ORDER does not matter because
// members are always sorted before hashing.
func TestCommunityIDIsContentAddressed(t *testing.T) {
	a := communityID([]string{"x.go", "y.go"})
	b := communityID([]string{"x.go", "y.go"})
	c := communityID([]string{"x.go", "z.go"})
	if a != b {
		t.Errorf("same members gave different ids: %s vs %s", a, b)
	}
	if a == c {
		t.Errorf("different members gave the same id: %s", a)
	}
	if !strings.HasPrefix(a, "community:") {
		t.Errorf("id %q is not namespaced", a)
	}
}

// TestDescribeStatesAbsenceExplicitly: a description that silently drops the
// cohesion reads like a community that was measured and passed.
func TestDescribeStatesAbsenceExplicitly(t *testing.T) {
	measured := describe(Community{
		HeuristicLabel:     "src/",
		Members:            []string{"a", "b"},
		StructuralCohesion: 0.8,
		Cohesion:           0.25,
		CohesionInterval:   Interval{Lo: 0.1, Hi: 0.5, N: 20},
		CohesionReason:     CohesionMeasured,
	})
	for _, want := range []string{"0.250", "n=20", "95% CI"} {
		if !strings.Contains(measured, want) {
			t.Errorf("description %q omits %q", measured, want)
		}
	}
	absent := describe(Community{
		HeuristicLabel:     "src/",
		Members:            []string{"a", "b"},
		StructuralCohesion: 0.8,
		Cohesion:           nan(),
		CohesionReason:     CohesionNoHoldoutPairs,
	})
	if !strings.Contains(absent, "unmeasured") || !strings.Contains(absent, CohesionNoHoldoutPairs) {
		t.Errorf("description %q does not state the absence and its reason", absent)
	}
}
