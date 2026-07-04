package parser

import (
	"os"
	"path/filepath"
	"strings"
	"unicode/utf8"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// ---------------------------------------------------------------------------
// C2a — index-time body-channel mining (GT_SEM_BODY-gated).
//
// The parser mines 3 additive property KINDS carrying a symbol's DOMAIN VOCABULARY:
//   string_literals — string-literal values (dedup, first-appearance, capped)
//   body_terms      — identifier + comment-word tokens (dedup, capped)
//   calls           — callee names (reuse of the extracted call refs)
// Gated behind GT_SEM_BODY so graph.db is BYTE-IDENTICAL when off; is_test symbols are
// excluded at source (leak=0). ≥2 languages (Python + Go).
// ---------------------------------------------------------------------------

func parseSrc(t *testing.T, ext, src string, isTest bool) *ParseResult {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture"+ext)
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(ext)
	if spec == nil {
		t.Skipf("no spec registered for %s", ext)
	}
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, isTest)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	return res
}

func propValue(res *ParseResult, kind string) (string, bool) {
	for _, p := range res.Properties {
		if p.Kind == kind {
			return p.Value, true
		}
	}
	return "", false
}

var c2aFixtures = []struct {
	name, ext, src string
	wantTerms      []string // domain vocab that MUST survive in body_terms
	dedupOut       []string // G8: name/param/callee tokens that must NOT be in body_terms
}{
	{
		name: "python", ext: ".py",
		src: "def connect(host):\n" +
			"    # establish redis over tls handshake\n" +
			"    url = \"redis://localhost:6379\"\n" +
			"    verify_certificate(host)\n" +
			"    return open_socket(url)\n",
		wantTerms: []string{"redis", "tls", "handshake", "url"},
		dedupOut:  []string{"connect", "host", "verify_certificate", "open_socket"},
	},
	{
		name: "go", ext: ".go",
		src: "package p\n" +
			"func Connect(host string) error {\n" +
			"    // establish redis over tls handshake\n" +
			"    url := \"redis://localhost:6379\"\n" +
			"    verifyCertificate(host)\n" +
			"    return openSocket(url)\n" +
			"}\n",
		wantTerms: []string{"redis", "tls", "handshake", "url"},
		dedupOut:  []string{"Connect", "host", "verifyCertificate", "openSocket"},
	},
}

func TestBodyChannels_ON_MinesVocabulary(t *testing.T) {
	os.Setenv("GT_SEM_BODY", "1")
	defer os.Unsetenv("GT_SEM_BODY")

	for _, fx := range c2aFixtures {
		t.Run(fx.name, func(t *testing.T) {
			res := parseSrc(t, fx.ext, fx.src, false)

			strs, ok := propValue(res, "string_literals")
			if !ok || !strings.Contains(strs, "redis://localhost:6379") {
				t.Errorf("[%s] string_literals missing the URL literal; got %q (ok=%v)", fx.name, strs, ok)
			}
			terms, ok := propValue(res, "body_terms")
			if !ok {
				t.Fatalf("[%s] body_terms missing", fx.name)
			}
			// comment words + local identifiers must appear (domain vocabulary).
			for _, want := range fx.wantTerms {
				if !strings.Contains(terms, want) {
					t.Errorf("[%s] body_terms missing %q; got %q", fx.name, want, terms)
				}
			}
			// G8 cross-channel dedup: the symbol name, its params (in the passage HEAD
			// `{name} {signature}`), and callee names (the `calls` channel) must NOT be
			// duplicated in body_terms. Exact-token match (Fields) avoids substring noise.
			termToks := map[string]bool{}
			for _, tok := range strings.Fields(terms) {
				termToks[tok] = true
			}
			for _, gone := range fx.dedupOut {
				if termToks[gone] {
					t.Errorf("[%s] body_terms duplicates head/calls token %q; got %q", fx.name, gone, terms)
				}
			}
			calls, ok := propValue(res, "calls")
			if !ok {
				t.Fatalf("[%s] calls missing", fx.name)
			}
			for _, want := range []string{"verify", "open"} { // verify_certificate/openSocket etc.
				if !strings.Contains(calls, want) {
					t.Errorf("[%s] calls missing a %q callee; got %q", fx.name, want, calls)
				}
			}
			t.Logf("[%s] strings=%q\n  terms=%q\n  calls=%q", fx.name, strs, terms, calls)
		})
	}
}

// Byte-identical-off: with the flag OFF the parser emits NONE of the 3 new kinds, so
// graph.db is byte-identical to today. Drives the REAL ParseFile.
func TestBodyChannels_OFF_ByteIdentical(t *testing.T) {
	os.Unsetenv("GT_SEM_BODY")
	for _, fx := range c2aFixtures {
		t.Run(fx.name, func(t *testing.T) {
			res := parseSrc(t, fx.ext, fx.src, false)
			for _, kind := range []string{"string_literals", "body_terms", "calls"} {
				if v, ok := propValue(res, kind); ok {
					t.Errorf("[%s] OFF must emit NO %s property, but got %q", fx.name, kind, v)
				}
			}
		})
	}
}

// Determinism: two parses (each mode) yield identical property (kind,value) sequences.
func TestBodyChannels_Determinism(t *testing.T) {
	for _, on := range []bool{false, true} {
		if on {
			os.Setenv("GT_SEM_BODY", "1")
		} else {
			os.Unsetenv("GT_SEM_BODY")
		}
		for _, fx := range c2aFixtures {
			a := parseSrc(t, fx.ext, fx.src, false)
			b := parseSrc(t, fx.ext, fx.src, false)
			if len(a.Properties) != len(b.Properties) {
				t.Fatalf("[%s on=%v] property count differs %d vs %d", fx.name, on, len(a.Properties), len(b.Properties))
			}
			for i := range a.Properties {
				if a.Properties[i].Kind != b.Properties[i].Kind || a.Properties[i].Value != b.Properties[i].Value {
					t.Fatalf("[%s on=%v] property %d differs: %+v vs %+v", fx.name, on, i, a.Properties[i], b.Properties[i])
				}
			}
		}
	}
	os.Unsetenv("GT_SEM_BODY")
}

// leak=0: a test symbol is excluded at source — no channels are mined for it even ON.
func TestBodyChannels_TestSymbolExcluded(t *testing.T) {
	os.Setenv("GT_SEM_BODY", "1")
	defer os.Unsetenv("GT_SEM_BODY")
	src := "def test_gold_thing():\n" +
		"    # FAIL_TO_PASS marker text\n" +
		"    assert compute(\"secret-gold-token\") == 1\n"
	res := parseSrc(t, ".py", src, true) // isTest=true
	for _, kind := range []string{"string_literals", "body_terms", "calls"} {
		if v, ok := propValue(res, kind); ok {
			t.Errorf("is_test symbol must mine NO %s (leak=0), got %q", kind, v)
		}
	}
}

// MUTATION companion (parser side): if the GT_SEM_BODY gate were removed (always mine),
// the OFF byte-identical assertion reddens. This test pins the gate: OFF => zero channels.
func TestBodyChannels_Mutation_GateGovernsOff(t *testing.T) {
	os.Unsetenv("GT_SEM_BODY")
	res := parseSrc(t, ".py", c2aFixtures[0].src, false)
	n := 0
	for _, p := range res.Properties {
		if p.Kind == "string_literals" || p.Kind == "body_terms" || p.Kind == "calls" {
			n++
		}
	}
	if n != 0 {
		t.Errorf("MUTATION GUARD: OFF emitted %d body-channel props — the GT_SEM_BODY gate was neutered", n)
	}
}

// F10 (RED→GREEN): the per-string byte cap must truncate on a RUNE boundary. A long
// string literal whose 400th byte falls INSIDE a multibyte rune was pre-fix sliced
// mid-sequence (v[:400]) — invalid UTF-8 written into graph.db, which makes Python's
// sqlite3 (default text_factory=str) raise "Could not decode to UTF-8" on the whole
// properties fetch, silently disabling EVERY body channel for the repo. Post-fix the
// cut backs off to the rune start and the stored value is valid UTF-8.
func TestBodyChannels_LongMultibyteLiteralStaysValidUTF8(t *testing.T) {
	os.Setenv("GT_SEM_BODY", "1")
	defer os.Unsetenv("GT_SEM_BODY")

	// 399 ASCII bytes then multibyte runes: byte 400 lands mid-rune ("é" = 0xC3 0xA9).
	long := strings.Repeat("a", 399) + strings.Repeat("é", 10)
	src := "def handler():\n" +
		"    msg = \"" + long + "\"\n" +
		"    return msg\n"
	res := parseSrc(t, ".py", src, false)
	v, ok := propValue(res, "string_literals")
	if !ok {
		t.Fatalf("string_literals missing")
	}
	if !utf8.ValidString(v) {
		t.Errorf("F10: string_literals value is INVALID UTF-8 (mid-rune byte truncation) — "+
			"Python sqlite3 consumers cannot decode it; value tail=%q", v[len(v)-8:])
	}
	if len(v) > 400 {
		t.Errorf("F10: value must stay within the 400-byte cap; got %d bytes", len(v))
	}
}
