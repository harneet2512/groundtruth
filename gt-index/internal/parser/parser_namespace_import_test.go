package parser

import "testing"

// B1-followup — `import * as X from './m'` must emit the namespace ALIAS X as an
// ImportRef (not only the "*" wildcard). Otherwise the resolver's package-alias
// branch cannot resolve a qualified `X.foo()` to m.foo, and the B1 receiver-blind
// guard (correctly skipping the bare-name lookup for qualified calls) drops it to
// name_match — migrating a real `import`/1.0 edge to name_match and risking a Gate-1
// (name_match > deterministic) flip on TS repos. CJS `const X = require()` already
// emits the alias; this brings ES namespace imports to parity.
// Mutation: emitting only "*" (the old behavior) leaves hasAlias=false → RED.
func TestImports_JS_NamespaceImportEmitsAlias(t *testing.T) {
	src := "import * as helpers from './helpers'\nexport function run() { return helpers.foo() }\n"
	res := parseFixture(t, "m.ts", src)
	var hasAlias bool
	for _, imp := range res.Imports {
		if imp.ImportedName == "helpers" {
			hasAlias = true
		}
	}
	if !hasAlias {
		t.Errorf("`import * as helpers` must emit an alias ImportRef (ImportedName=helpers) "+
			"so X.foo() resolves via the package-alias branch; imports=%+v", res.Imports)
	}
}
