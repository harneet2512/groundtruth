package walker

import "testing"

func TestIsTestFile(t *testing.T) {
	tests := []struct {
		name string
		path string
		want bool
	}{
		// Python
		{"python test_ prefix", "test_users.py", true},
		{"python _test suffix", "users_test.py", true},
		{"python normal", "users.py", false},
		// Python pytest/Django FILES the prefix/suffix rules miss (Fable 2026-07-03 leak):
		{"python conftest nested", "tests/conftest.py", true},
		{"python conftest root", "conftest.py", true},
		{"python django tests.py", "app/tests.py", true},
		{"python bare test.py", "pkg/test.py", true},
		{"python _tests plural", "app/foo_tests.py", true},
		// must NOT over-mark: "contests" ends in "tests" but not "_tests"
		{"python contests not a test", "contests.py", false},
		// JS/TS plural .tests./.specs.
		{"ts plural tests", "src/users.tests.ts", true},
		{"js plural specs", "src/users.specs.js", true},

		// Go
		{"go test file", "pkg/users_test.go", true},
		{"go normal", "pkg/users.go", false},

		// JS/TS
		{"js test file", "src/users.test.js", true},
		{"ts spec file", "src/users.spec.ts", true},
		{"js normal", "src/users.js", false},

		// Java
		{"java test", "src/test/java/UserTest.java", true},
		{"java tests suffix", "UserTests.java", true},
		{"java normal", "User.java", false},

		// Kotlin
		{"kotlin test", "UserTest.kt", true},
		{"kotlin normal", "User.kt", false},

		// Scala
		{"scala test", "UserTest.scala", true},
		{"scala spec", "UserSpec.scala", true},
		{"scala normal", "User.scala", false},

		// C#
		{"csharp test", "UserTest.cs", true},
		{"csharp tests", "UserTests.cs", true},
		{"csharp normal", "User.cs", false},

		// PHP
		{"php test", "UserTest.php", true},
		{"php normal", "User.php", false},

		// Swift
		{"swift test", "UserTests.swift", true},
		{"swift normal", "User.swift", false},

		// Ruby
		{"ruby spec", "user_spec.rb", true},
		{"ruby normal", "user.rb", false},

		// Directory-based
		{"tests dir", "tests/test_foo.py", true},
		{"__tests__ dir", "src/__tests__/foo.js", true},
		{"test subdir", "project/tests/foo.js", true},
		{"spec dir", "spec/user_spec.rb", true},
		{"src/test dir", "src/test/java/UserTest.java", true},
		// Underscore-wrapped test dirs (JS/TS): csstree "__tests/", "__test__/"
		{"__tests dir (csstree)", "lib/__tests/clone.js", true},
		{"__test__ dir", "src/__test__/foo.js", true},
		{"specs dir", "specs/user.js", true},
		// Rust: tests.rs and *_test.rs / *_tests.rs (wasmi-leak fix)
		{"rust tests.rs", "crates/wasmi/src/tests.rs", true},
		{"rust test.rs", "src/test.rs", true},
		{"rust _test.rs suffix", "crates/foo/src/parser_test.rs", true},
		{"rust _tests.rs suffix", "crates/foo/src/parser_tests.rs", true},
		{"rust normal lib.rs", "src/lib.rs", false},
		{"rust normal mod", "crates/wasmi/src/engine/mod.rs", false},
		// Fuzz dir (wasmi-leak fix)
		{"fuzz dir", "crates/fuzz/src/config.rs", true},
		{"fuzz_targets dir", "fuzz_targets/fuzz_main.rs", true},
		{"fuzzing dir", "fuzzing/corpus/seed.rs", true},
		// C++: *_test.cc / *_test.cpp
		{"cpp gtest", "src/parser_test.cc", true},
		{"cpp gtest cpp", "src/engine_test.cpp", true},
		// Negative guards: underscore-trim must not over-match
		{"contests not test", "src/contests/foo.js", false},
		{"attestations not test", "lib/attestations/sign.js", false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := IsTestFile(tc.path)
			if got != tc.want {
				t.Errorf("IsTestFile(%q) = %v, want %v", tc.path, got, tc.want)
			}
		})
	}
}

func TestIsNonSourceFile(t *testing.T) {
	// RUN VERDICT: 10,444 non-source-as-FACT CALLS edges in the live graphs because
	// nodes under benchmark/ examples/ testdata/ docs/ vendor/ fixtures/ etc. were
	// NOT marked is_test — their edges entered the fact surface and pollute reach /
	// localization (e.g. mashumaro benchmark/libs/dacite → from_dict). IsNonSourceFile
	// flags ANY path with a non-source DIRECTORY SEGMENT (whole-segment, case-
	// insensitive, underscore-trimmed) so the existing is_test filters exclude them.
	// Whole-segment matching rejects false positives like "benchmarking/"/"docstore/".
	tests := []struct {
		name string
		path string
		want bool
	}{
		// Test dirs (already covered by IsTestFile, must also be non-source)
		{"tests dir", "tests/foo.py", true},
		{"test dir", "test/foo.py", true},
		{"spec dir", "spec/foo.rb", true},
		// Demo / example dirs
		{"benchmark dir", "benchmark/libs/dacite/common.py", true},
		{"benchmarks dir", "benchmarks/run.py", true},
		{"benches dir", "benches/bench.rs", true},
		{"bench dir", "bench/x.go", true},
		{"example dir", "example/main.go", true},
		{"examples dir", "examples/demo/app.ts", true},
		{"demo dir", "demo/index.js", true},
		{"demos dir", "demos/x.py", true},
		{"sample dir", "sample/a.py", true},
		{"samples dir", "samples/b.py", true},
		// Fixtures / test data
		{"testdata dir", "internal/parser/testdata/foo.go", true},
		{"fixtures dir", "src/fixtures/data.py", true},
		{"fixture dir", "src/fixture/data.py", true},
		// Docs
		{"docs dir", "docs/guide.py", true},
		{"doc dir", "doc/x.py", true},
		{"docs_src dir", "docs_src/tut.py", true},
		{"tutorial dir", "tutorial/step1.py", true},
		{"tutorials dir", "tutorials/step1.py", true},
		{"documentation dir", "documentation/x.py", true},
		// Vendored / third-party / build
		{"vendor dir", "vendor/lib/x.go", true},
		{"node_modules dir", "node_modules/pkg/index.js", true},
		{"third_party dir", "third_party/lib/x.cc", true},
		{"dist dir", "dist/bundle.js", true},
		{"build dir", "build/out.js", true},
		// Fuzz dirs (wasmi-leak fix — Part 1 generalized segments)
		{"fuzz dir", "crates/fuzz/src/config.rs", true},
		{"fuzzing dir", "fuzzing/main.rs", true},
		{"fuzz_targets dir", "fuzz_targets/fuzz_parse.rs", true},
		{"corpus dir", "corpus/seed.bin.go", true},
		{"testcases dir", "testcases/case001.go", true},
		{"conformance dir", "conformance/spec_test.go", true},
		{"compat dir", "compat/old_api.py", true},
		{"integration_tests dir", "integration_tests/flow_test.py", true},
		{"e2e_tests dir", "e2e_tests/login.ts", true},
		// Underscore-wrapped variants
		{"__tests__ dir", "src/__tests__/foo.js", true},
		// Negative guards: whole-segment, never substring
		{"normal source", "src/users.py", false},
		{"benchmarking not bench", "src/benchmarking/x.py", false},
		{"docstore not doc", "lib/docstore/x.py", false},
		{"sampler not sample", "ml/sampler/x.py", false},
		{"contests not test", "src/contests/foo.js", false},
		{"vendored substring", "src/vendored_helper.py", false},
		{"deep normal", "a/b/c/d/service.go", false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := IsNonSourceFile(tc.path)
			if got != tc.want {
				t.Errorf("IsNonSourceFile(%q) = %v, want %v", tc.path, got, tc.want)
			}
		})
	}
}

func TestIsIgnored(t *testing.T) {
	tests := []struct {
		name     string
		relPath  string
		patterns []string
		want     bool
	}{
		{"_test dir", "_test/foo.go", []string{"_test"}, true},
		{"_test.go NOT ignored", "bind_test.go", []string{"_test"}, false},
		{"nested _test.go NOT ignored", "pkg/bind_test.go", []string{"_test"}, false},
		{"vendor dir", "vendor/foo/bar.go", []string{"vendor"}, true},
		{"not vendor file", "myvendor.go", []string{"vendor"}, false},
		{"glob *.out", "test.out", []string{"*.out"}, true},
		{"glob no match", "test.go", []string{"*.out"}, false},
		{".DS_Store", ".DS_Store", []string{".DS_Store"}, true},
		{"coverage.txt", "coverage.txt", []string{"coverage.txt"}, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := isIgnored(tc.relPath, tc.patterns)
			if got != tc.want {
				t.Errorf("isIgnored(%q, %v) = %v, want %v", tc.relPath, tc.patterns, got, tc.want)
			}
		})
	}
}
