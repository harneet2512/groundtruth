package store

import "testing"

func TestResolutionStableIdentityIsDeterministic(t *testing.T) {
	n := Node{Language: "python", FilePath: "src/a.py", QualifiedName: "a.run", Label: "Function", StartLine: 2, EndLine: 4}
	a := StableResolutionSymbolID(n)
	if a == "" || a != StableResolutionSymbolID(n) {
		t.Fatalf("unstable symbol identity: %q", a)
	}
	c := StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run")
	if c == "" || c != StableResolutionCallsiteID("rev", a, "src/a.py", 3, 3, "run") {
		t.Fatalf("unstable callsite identity: %q", c)
	}
}
