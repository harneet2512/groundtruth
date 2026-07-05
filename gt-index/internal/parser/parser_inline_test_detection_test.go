package parser

import "testing"

// isTestOf returns (found, IsTest) for the single Function/Method node named `name`.
func isTestOf(res *ParseResult, name string) (bool, bool) {
	for i := range res.Nodes {
		n := &res.Nodes[i]
		if (n.Label == "Function" || n.Label == "Method") && n.Name == name {
			return true, n.IsTest
		}
	}
	return false, false
}

// RED→GREEN (Fable #2/#3): inline tests were indexed with is_test=0 because test detection
// was file/path-level only — no #[test]/#[cfg(test)]/@Test handling. Their bodies polluted
// the content-BM25 IDF and exposed test names via CONTENT_SEED witnesses. The node-level
// attribute detector flips is_test for the whole annotated subtree. Names below deliberately
// DO NOT contain "test" so only the ANNOTATION (not a name heuristic) can mark them.
func TestInlineTestDetection_Rust_CfgTestMod(t *testing.T) {
	src := "" +
		"pub fn real_work(x: i32) -> i32 { x + 1 }\n" +
		"\n" +
		"#[cfg(test)]\n" +
		"mod tests {\n" +
		"    use super::*;\n" +
		"    #[test]\n" +
		"    fn it_works() {\n" +
		"        assert_eq!(real_work(1), 2);\n" +
		"    }\n" +
		"}\n"
	res := parseFixture(t, "lib.rs", src)
	if res == nil {
		return // rust spec not registered (skipped)
	}
	if found, isT := isTestOf(res, "real_work"); !found || isT {
		t.Errorf("real_work: found=%v isTest=%v — production fn must NOT be is_test", found, isT)
	}
	if found, isT := isTestOf(res, "it_works"); !found || !isT {
		t.Errorf("it_works: found=%v isTest=%v — #[test] in #[cfg(test)] mod MUST be is_test", found, isT)
	}
}

// Rust integration-test style: a bare `#[test] fn` at top level (no cfg(test) mod).
func TestInlineTestDetection_Rust_BareTestAttr(t *testing.T) {
	src := "" +
		"pub fn helper() -> bool { true }\n" +
		"\n" +
		"#[test]\n" +
		"fn checks_helper() {\n" +
		"    assert!(helper());\n" +
		"}\n"
	res := parseFixture(t, "it.rs", src)
	if res == nil {
		return
	}
	if found, isT := isTestOf(res, "helper"); !found || isT {
		t.Errorf("helper: found=%v isTest=%v — must NOT be is_test", found, isT)
	}
	if found, isT := isTestOf(res, "checks_helper"); !found || !isT {
		t.Errorf("checks_helper: found=%v isTest=%v — #[test] fn MUST be is_test", found, isT)
	}
}

// GENERALIZATION proof (not Rust-hardcoded): Java @Test via the `modifiers` wrapper. Method
// name `verifiesAddition` has no "test" token, so only the @Test annotation can mark it.
func TestInlineTestDetection_Java_TestAnnotation(t *testing.T) {
	src := "" +
		"class Calc {\n" +
		"    public int add(int a, int b) { return a + b; }\n" +
		"\n" +
		"    @Test\n" +
		"    public void verifiesAddition() {\n" +
		"        assertEquals(2, add(1, 1));\n" +
		"    }\n" +
		"}\n"
	res := parseFixture(t, "Calc.java", src)
	if res == nil {
		return // java spec not registered (skipped) — Rust cases still prove the mechanism
	}
	if found, isT := isTestOf(res, "add"); !found || isT {
		t.Errorf("add: found=%v isTest=%v — production method must NOT be is_test", found, isT)
	}
	if found, isT := isTestOf(res, "verifiesAddition"); !found || !isT {
		t.Errorf("verifiesAddition: found=%v isTest=%v — @Test method MUST be is_test", found, isT)
	}
}

// Guard against OVER-marking: a non-test attribute/annotation whose name merely CONTAINS a
// substring must not be flagged. `#[derive(...)]` and a `factory`-named fn stay production.
func TestInlineTestDetection_NoFalsePositive(t *testing.T) {
	src := "" +
		"#[derive(Debug, Clone)]\n" +
		"pub struct Widget { n: i32 }\n" +
		"\n" +
		"#[inline]\n" +
		"pub fn factory() -> Widget { Widget { n: 0 } }\n"
	res := parseFixture(t, "w.rs", src)
	if res == nil {
		return
	}
	if found, isT := isTestOf(res, "factory"); !found || isT {
		t.Errorf("factory: found=%v isTest=%v — #[inline] fn 'factory' must NOT be is_test", found, isT)
	}
}
