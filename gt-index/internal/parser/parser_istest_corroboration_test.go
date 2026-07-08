package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// Content-corroboration of a NAME-only is_test flag (test-runner collection semantics:
// filename gate ∧ unit gate). Proves production test-infra (base_test.py) is no longer
// deleted from the localizer's search space, while REAL tests stay excluded (no leak).

func TestIsTestUnitName_Convention(t *testing.T) {
	cases := []struct {
		lang, name string
		want       bool
	}{
		{"python", "test_foo", true},     // pytest
		{"python", "testFoo", true},      // unittest
		{"python", "setup_test", false},  // base_test.py method — NOT a test
		{"python", "teardown_test", false},
		{"python", "build_fixture", false},
		{"go", "TestThing", true},
		{"go", "BenchmarkX", true},
		{"go", "ExampleY", true},
		{"go", "FuzzZ", true},
		{"go", "Testable", false}, // Go's own rule: next char lowercase ⇒ not a test
		{"go", "setupHelper", false},
		{"java", "testLogin", true},
		{"php", "testPayment", true},
	}
	for _, c := range cases {
		if got := isTestUnitName(c.lang, c.name); got != c.want {
			t.Errorf("isTestUnitName(%q,%q)=%v want %v", c.lang, c.name, got, c.want)
		}
	}
}

func TestIsTestByStructure(t *testing.T) {
	cases := []struct {
		path string
		want bool
	}{
		{"pkg/base_test.py", false},        // name-only ⇒ NOT structural
		{"tests/base_test.py", true},       // test dir ⇒ structural
		{"src/test/java/FooTest.java", true},
		{"a/__tests__/x.js", true},
		{"mobly/base_test.py", false},
	}
	for _, c := range cases {
		if got := walker.IsTestByStructure(filepath.FromSlash(c.path)); got != c.want {
			t.Errorf("IsTestByStructure(%q)=%v want %v", c.path, got, c.want)
		}
	}
}

// parseNamed writes src to <tmp>/<subdir>/<filename>, computes is_test exactly like the
// indexer (walker.IsTestFile), and drives the real ParseFile.
func parseNamed(t *testing.T, subdir, filename, src string) *ParseResult {
	t.Helper()
	root, err := os.MkdirTemp("", "gtcorrob")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	dir := filepath.Join(root, subdir)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, filename)
	if err := os.WriteFile(path, []byte(src), 0o644); err != nil {
		t.Fatal(err)
	}
	ext := filepath.Ext(filename)
	spec := specs.ForExtension(ext)
	if spec == nil {
		t.Skipf("no spec for %s", ext)
	}
	isTest := walker.IsTestFile(path) || walker.IsNonSourceFile(path)
	sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
	res, err := ParseFile(sf, isTest)
	if err != nil {
		t.Fatalf("ParseFile: %v", err)
	}
	return res
}

func anyDefNode(res *ParseResult) bool {
	for _, n := range res.Nodes {
		if n.Label == "Function" || n.Label == "Method" {
			return true
		}
	}
	return false
}

func allDefsIsTest(res *ParseResult, want bool) (bool, int) {
	n := 0
	for _, nd := range res.Nodes {
		if nd.Label == "Function" || nd.Label == "Method" {
			n++
			if nd.IsTest != want {
				return false, n
			}
		}
	}
	return true, n
}

func TestCorroboration_ParseFile(t *testing.T) {
	const pyBase = "class BaseTestClass:\n" +
		"    def setup_class(self):\n        return 1\n" +
		"    def setup_test(self):\n        return 2\n" +
		"    def teardown_test(self):\n        return 3\n"
	const pyRealTest = "def test_foo():\n    assert 1 == 1\n"
	const pyHelper = "def build_fixture():\n    return 1\n"
	const goHelper = "package p\nfunc setupHelper() int { return 1 }\n"
	const goRealTest = "package p\nimport \"testing\"\nfunc TestThing(t *testing.T) { _ = t }\n"
	const jsSpec = "function makeClient() { return 1; }\n"

	cases := []struct {
		name, subdir, filename, src string
		wantIsTest                  bool // expected node is_test AFTER corroboration
	}{
		// RED→GREEN core: production base class named *_test, no test methods ⇒ downgraded.
		{"py_base_test_downgraded", "pkg", "base_test.py", pyBase, false},
		{"py_helpers_test_downgraded", "pkg", "helpers_test.py", pyHelper, false},
		{"go_base_test_downgraded", "pkg", "base_test.go", goHelper, false},
		// Leak-safety: REAL tests keep is_test=1 (assertions/test names stay excluded).
		{"py_real_test_kept", "pkg", "foo_test.py", pyRealTest, true},
		{"go_real_test_kept", "pkg", "thing_test.go", goRealTest, true},
		// Structural flag is trusted as-is: base_test.py UNDER tests/ stays is_test=1.
		{"py_base_test_in_testsdir_kept", "tests", "base_test.py", pyBase, true},
		// Call-based language (not corroborated): name flag stands ⇒ stays is_test=1.
		{"js_spec_not_corroborated", "pkg", "client.spec.js", jsSpec, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := parseNamed(t, c.subdir, c.filename, c.src)
			if !anyDefNode(res) {
				t.Fatalf("no Function/Method nodes parsed for %s", c.filename)
			}
			ok, at := allDefsIsTest(res, c.wantIsTest)
			if !ok {
				t.Fatalf("%s: node %d is_test != %v (corroboration wrong)", c.filename, at, c.wantIsTest)
			}
		})
	}
}
