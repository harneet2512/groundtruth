package parser

import (
	"crypto/sha256"
	"encoding/hex"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/store"
)

// ─────────────────────────────────────────────────────────────────────────────
// Content addressing (final_hardening item 2).
//
// A graph that copies source text onto the node duplicates the repository and
// goes stale silently: the copy keeps answering long after the file changed.
// GT stores an ADDRESS instead -- (file_hash, byte_start, byte_end) -- and
// resolves it at delivery against the file on disk. Staleness then surfaces as
// a hash MISMATCH the reader can name, not as wrong bytes nobody notices.
//
// The address is only worth anything if EVERY code symbol carries it: a symbol
// with a partial address cannot be verified, and an unverifiable symbol is
// exactly the silent-rot case the design exists to remove. tree-sitter already
// hands every one of the 30 grammars a node byte range, and ParseFile has
// already read the file bytes it must hash, so there is no per-symbol cost.
// ─────────────────────────────────────────────────────────────────────────────

func fileDigest(src string) string {
	sum := sha256.Sum256([]byte(src))
	return hex.EncodeToString(sum[:])
}

func isCodeSymbol(n store.Node) bool {
	switch n.Label {
	case "Function", "Method", "Class", "Interface":
		return true
	}
	return false
}

func addressOf(t *testing.T, res *ParseResult, name string) store.Node {
	t.Helper()
	for _, n := range res.Nodes {
		if n.Name == name && isCodeSymbol(n) {
			return n
		}
	}
	t.Fatalf("symbol %q not found", name)
	return store.Node{}
}

// Every code symbol carries all three parts of the address. A symbol missing
// any one of them is undeliverable under verification, which is worse than not
// addressing it at all -- it looks addressed and cannot be checked.
func TestEveryCodeSymbolCarriesAWholeAddress(t *testing.T) {
	src := "class Repo:\n" +
		"    def fetch(self, url):\n" +
		"        return url\n" +
		"\n" +
		"def main():\n" +
		"    return Repo()\n"
	res := parseFixture(t, "repo.py", src)
	want := fileDigest(src)
	seen := 0
	for _, n := range res.Nodes {
		if !isCodeSymbol(n) {
			continue
		}
		seen++
		if n.FileHash != want {
			t.Errorf("%s %s: file_hash = %q, want sha256 of the indexed bytes %q", n.Label, n.Name, n.FileHash, want)
		}
		if n.ByteEnd <= n.ByteStart {
			t.Errorf("%s %s: byte range [%d,%d) is not a range", n.Label, n.Name, n.ByteStart, n.ByteEnd)
		}
		if int(n.ByteEnd) > len(src) {
			t.Errorf("%s %s: byte_end %d is past the end of the file (%d)", n.Label, n.Name, n.ByteEnd, len(src))
		}
	}
	if seen == 0 {
		t.Fatalf("fixture produced no code symbols")
	}
}

// The address must resolve to the symbol itself, not to some enclosing span.
// This is the property delivery depends on: given a matching file hash, the
// byte range IS the snippet, with no re-parse and no line-number arithmetic.
func TestAddressResolvesToTheExactDeclaration(t *testing.T) {
	src := "class Repo:\n" +
		"    def fetch(self, url):\n" +
		"        return url\n" +
		"\n" +
		"def main():\n" +
		"    return Repo()\n"
	res := parseFixture(t, "repo.py", src)

	fetch := addressOf(t, res, "fetch")
	if got := src[fetch.ByteStart:fetch.ByteEnd]; got != "def fetch(self, url):\n        return url" {
		t.Errorf("fetch resolved to %q", got)
	}
	main := addressOf(t, res, "main")
	if got := src[main.ByteStart:main.ByteEnd]; got != "def main():\n    return Repo()" {
		t.Errorf("main resolved to %q", got)
	}
	repo := addressOf(t, res, "Repo")
	if got := src[repo.ByteStart:repo.ByteEnd]; got[:len("class Repo:")] != "class Repo:" {
		t.Errorf("Repo resolved to %q", got)
	}
}

// The byte range comes off the tree-sitter node, so it is grammar-independent.
// Pinning several languages keeps a future per-language extraction path from
// quietly addressing only the one language a fixture happened to cover.
func TestAddressIsGrammarIndependent(t *testing.T) {
	cases := []struct{ file, src, symbol, head string }{
		{"m.go", "package p\n\nfunc Handle(x int) int {\n\treturn x\n}\n", "Handle", "func Handle(x int) int {"},
		{"lib.rs", "fn handle(x: i32) -> i32 {\n    x\n}\n", "handle", "fn handle(x: i32) -> i32 {"},
		{"app.ts", "export function handle(x: number): number {\n  return x;\n}\n", "handle", "function handle(x: number): number {"},
		{"App.java", "class App {\n    int handle(int x) {\n        return x;\n    }\n}\n", "handle", "int handle(int x) {"},
	}
	for _, tc := range cases {
		res := parseFixture(t, tc.file, tc.src)
		n := addressOf(t, res, tc.symbol)
		if n.FileHash != fileDigest(tc.src) {
			t.Errorf("%s: file_hash = %q, want %q", tc.file, n.FileHash, fileDigest(tc.src))
			continue
		}
		if n.ByteEnd <= n.ByteStart || int(n.ByteEnd) > len(tc.src) {
			t.Errorf("%s: byte range [%d,%d) unusable against %d bytes", tc.file, n.ByteStart, n.ByteEnd, len(tc.src))
			continue
		}
		got := tc.src[n.ByteStart:n.ByteEnd]
		if len(got) < len(tc.head) || got[:len(tc.head)] != tc.head {
			t.Errorf("%s: address resolved to %q, want it to start at %q", tc.file, got, tc.head)
		}
	}
}
