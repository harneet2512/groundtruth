package parser

import "testing"

// Fable P5 (RED→GREEN): `import numpy as np` binds the LOCAL name to the ALIAS `np`, which
// SHADOWS the module — a call `np.array()` uses `np`, so ImportedName must be the alias, not the
// module's last component. Pre-fix it recorded "numpy" → the resolver could not bind `np.*`
// (the import-verified rung matches the caller's qualifier against ImportedName). Rust already
// did this; this brings Python to parity.
//
// Mutation: reverting ImportedName to lastDotComponent(fullPath) records "numpy"/"path" → RED.
func TestImports_Py_AliasedModuleImportBindsAlias(t *testing.T) {
	src := "import numpy as np\nimport os.path as op\n\ndef run():\n    return np.array([1]) + op.join('a', 'b')\n"
	res := parseFixture(t, "m.py", src)
	byName := map[string]bool{}
	for _, imp := range res.Imports {
		byName[imp.ImportedName] = true
	}
	if !byName["np"] {
		t.Errorf("`import numpy as np` must bind the alias `np`, not the module; imports=%+v", res.Imports)
	}
	if byName["numpy"] {
		t.Errorf("`import numpy as np` must NOT bind `numpy` (the alias shadows it); imports=%+v", res.Imports)
	}
	if !byName["op"] {
		t.Errorf("`import os.path as op` must bind the alias `op`; imports=%+v", res.Imports)
	}
}

// Fable P6 (RED→GREEN): `from datetime import datetime` must emit an ImportRef for the imported
// symbol even though it shares the module's name. The old `name != modulePath` guard silently
// discarded it; the fix disambiguates by NODE POSITION (before/after the `import` keyword).
//
// Mutation: reverting the dotted_name guard to `name != modulePath` discards the import → RED.
func TestImports_Py_ImportedSymbolEqualsModuleName_NotDiscarded(t *testing.T) {
	src := "from datetime import datetime\n\ndef now():\n    return datetime.utcnow()\n"
	res := parseFixture(t, "m.py", src)
	var found bool
	for _, imp := range res.Imports {
		if imp.ImportedName == "datetime" && imp.ModulePath == "datetime" {
			found = true
		}
	}
	if !found {
		t.Errorf("`from datetime import datetime` must emit ImportRef{datetime, datetime}; imports=%+v", res.Imports)
	}
}
