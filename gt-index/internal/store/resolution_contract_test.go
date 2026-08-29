package store

import "testing"

func TestResolutionStableIdentitiesMatchHAR6Vectors(t *testing.T) {
	node := Node{Label: "Function", Language: "python", FilePath: "src/mod.py", QualifiedName: "mod.run", StartLine: 4, EndLine: 7}
	got := StableResolutionSymbolID(node)
	const want = "663d01630b4ccc8b6470d5cc7d0ad49653ed2200e46193434986120ba9662e6d"
	if got != want {
		t.Fatalf("stable symbol ID = %s; want HAR-6 vector %s", got, want)
	}
	callsite := StableResolutionCallsiteID("r1", got, "src/mod.py", 10, 10, "helper")
	const wantCallsite = "d556290a086a0ebd453fea057c9aa38e883dad297b015b4a651ffa4f1a0859d7"
	if callsite != wantCallsite {
		t.Fatalf("stable callsite ID = %s; want HAR-6 vector %s", callsite, wantCallsite)
	}
}
