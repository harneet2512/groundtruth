package main

import "testing"

func TestCandidateContextListsEncodeNilAsEmptyArray(t *testing.T) {
	if got := marshalResolutionStringList(nil); got != "[]" {
		t.Fatalf("nil candidate context encoded as %s, want []", got)
	}
	if got := marshalResolutionStringList([]string{"a", "b"}); got != `["a","b"]` {
		t.Fatalf("candidate context encoded as %s", got)
	}
}
