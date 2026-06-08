package resolver

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
)

func TestBuildFileMap(t *testing.T) {
	tests := []struct {
		name     string
		files    []string
		langs    []string
		wantKeys map[string]string // key → expected file path
	}{
		{
			name:  "python dotted module",
			files: []string{"foo/bar/baz.py"},
			langs: []string{"python"},
			wantKeys: map[string]string{
				"foo.bar.baz": "foo/bar/baz.py",
				"bar.baz":     "foo/bar/baz.py",
				"baz":         "foo/bar/baz.py",
			},
		},
		{
			name:  "python __init__",
			files: []string{"foo/bar/__init__.py"},
			langs: []string{"python"},
			wantKeys: map[string]string{
				"foo.bar": "foo/bar/__init__.py",
				"bar":     "foo/bar/__init__.py",
			},
		},
		{
			name:  "java standard path",
			files: []string{"src/main/java/com/foo/Bar.java"},
			langs: []string{"java"},
			wantKeys: map[string]string{
				"com.foo.Bar": "src/main/java/com/foo/Bar.java",
				"com.foo":     "src/main/java/com/foo/Bar.java",
			},
		},
		{
			name:  "kotlin same as java",
			files: []string{"src/main/kotlin/com/app/Service.kt"},
			langs: []string{"kotlin"},
			wantKeys: map[string]string{
				"com.app.Service": "src/main/kotlin/com/app/Service.kt",
				"com.app":         "src/main/kotlin/com/app/Service.kt",
			},
		},
		{
			name:  "scala same as java",
			files: []string{"src/main/scala/com/data/Model.scala"},
			langs: []string{"scala"},
			wantKeys: map[string]string{
				"com.data.Model": "src/main/scala/com/data/Model.scala",
				"com.data":       "src/main/scala/com/data/Model.scala",
			},
		},
		{
			name:  "groovy same as java",
			files: []string{"src/main/groovy/com/build/Task.groovy"},
			langs: []string{"groovy"},
			wantKeys: map[string]string{
				"com.build.Task": "src/main/groovy/com/build/Task.groovy",
				"com.build":      "src/main/groovy/com/build/Task.groovy",
			},
		},
		{
			name:  "csharp namespace path",
			files: []string{"MyApp/Services/UserService.cs"},
			langs: []string{"csharp"},
			wantKeys: map[string]string{
				"MyApp.Services.UserService": "MyApp/Services/UserService.cs",
				"Services.UserService":       "MyApp/Services/UserService.cs",
				"UserService":                "MyApp/Services/UserService.cs",
			},
		},
		{
			name:  "php psr4",
			files: []string{"src/App/Http/Controllers/UserController.php"},
			langs: []string{"php"},
			wantKeys: map[string]string{
				`App\Http\Controllers\UserController`: "src/App/Http/Controllers/UserController.php",
				"App/Http/Controllers/UserController":  "src/App/Http/Controllers/UserController.php",
				"UserController":                       "src/App/Http/Controllers/UserController.php",
			},
		},
		{
			name:  "c include path",
			files: []string{"include/foo/bar.h"},
			langs: []string{"c"},
			wantKeys: map[string]string{
				"include/foo/bar.h": "include/foo/bar.h",
				"foo/bar.h":         "include/foo/bar.h",
				"bar":               "include/foo/bar.h",
			},
		},
		{
			name:  "cpp include path",
			files: []string{"src/utils/helper.hpp"},
			langs: []string{"cpp"},
			wantKeys: map[string]string{
				"src/utils/helper.hpp": "src/utils/helper.hpp",
				"utils/helper.hpp":     "src/utils/helper.hpp",
				"helper":               "src/utils/helper.hpp",
			},
		},
		{
			name:  "swift module",
			files: []string{"Sources/MyModule/Foo.swift"},
			langs: []string{"swift"},
			wantKeys: map[string]string{
				"Sources/MyModule": "Sources/MyModule/Foo.swift",
				"MyModule":         "Sources/MyModule/Foo.swift",
			},
		},
		{
			name:  "ocaml module name",
			files: []string{"lib/parser.ml"},
			langs: []string{"ocaml"},
			wantKeys: map[string]string{
				"Parser": "lib/parser.ml",
				"parser": "lib/parser.ml",
			},
		},
		{
			name:  "rust crate path",
			files: []string{"src/foo/bar.rs"},
			langs: []string{"rust"},
			wantKeys: map[string]string{
				"crate::foo::bar": "src/foo/bar.rs",
				"foo::bar":        "src/foo/bar.rs",
				"bar":             "src/foo/bar.rs",
			},
		},
		{
			name:  "go directory path",
			files: []string{"pkg/auth/jwt.go"},
			langs: []string{"go"},
			wantKeys: map[string]string{
				"pkg/auth": "pkg/auth/jwt.go",
				"auth":     "pkg/auth/jwt.go",
			},
		},
		{
			name:  "js strip src prefix",
			files: []string{"src/utils/helpers.js"},
			langs: []string{"javascript"},
			wantKeys: map[string]string{
				"src/utils/helpers": "src/utils/helpers.js",
				"utils/helpers":     "src/utils/helpers.js",
				"helpers":           "src/utils/helpers.js",
			},
		},
		{
			name:  "ruby lib path",
			files: []string{"lib/foo/bar.rb"},
			langs: []string{"ruby"},
			wantKeys: map[string]string{
				"foo/bar": "lib/foo/bar.rb",
				"bar":     "lib/foo/bar.rb",
			},
		},
		{
			name:  "elixir module path",
			files: []string{"lib/my_app/user.ex"},
			langs: []string{"elixir"},
			wantKeys: map[string]string{
				"MyApp.User": "lib/my_app/user.ex",
				"User":       "lib/my_app/user.ex",
			},
		},
		{
			name:  "lua dotted module",
			files: []string{"lua/foo/bar.lua"},
			langs: []string{"lua"},
			wantKeys: map[string]string{
				"foo.bar": "lua/foo/bar.lua",
				"bar":     "lua/foo/bar.lua",
			},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			fm := BuildFileMap(tc.files, tc.langs)
			for key, wantFile := range tc.wantKeys {
				files, ok := fm[key]
				if !ok {
					t.Errorf("key %q not found in file map", key)
					continue
				}
				found := false
				for _, f := range files {
					if f == wantFile {
						found = true
						break
					}
				}
				if !found {
					t.Errorf("key %q: want file %q, got %v", key, wantFile, files)
				}
			}
		})
	}
}

func TestFindGoModulePath(t *testing.T) {
	dir := t.TempDir()
	goModContent := "module example.com/project\n\ngo 1.22\n"
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte(goModContent), 0644); err != nil {
		t.Fatal(err)
	}
	got := FindGoModulePath(dir)
	if got != "example.com/project" {
		t.Errorf("FindGoModulePath = %q, want %q", got, "example.com/project")
	}

	// No go.mod → empty string
	got2 := FindGoModulePath(t.TempDir())
	if got2 != "" {
		t.Errorf("FindGoModulePath(no go.mod) = %q, want empty", got2)
	}
}

func TestRegisterGoModulePaths(t *testing.T) {
	fm := BuildFileMap(
		[]string{"auth/login.go", "auth/jwt.go", "utils/crypto.go"},
		[]string{"go", "go", "go"},
	)
	RegisterGoModulePaths(fm, "example.com/project")

	// Module-prefixed keys should now exist
	for _, key := range []string{"example.com/project/auth", "example.com/project/utils"} {
		if _, ok := fm[key]; !ok {
			t.Errorf("expected key %q in file map after RegisterGoModulePaths", key)
		}
	}
	// Original short keys should still work
	if _, ok := fm["auth"]; !ok {
		t.Error("original key 'auth' should still exist")
	}
}

func TestResolve_GoImport(t *testing.T) {
	// Simulate: main.go imports "example.com/project/auth", calls auth.Login()
	// auth/login.go defines Login
	files := []string{"main.go", "auth/login.go", "auth/jwt.go"}
	langs := []string{"go", "go", "go"}
	fm := BuildFileMap(files, langs)
	RegisterGoModulePaths(fm, "example.com/project")

	imports := []parser.ImportRef{
		{ImportedName: "auth", ModulePath: "example.com/project/auth", File: "main.go", Line: 3},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "Login", CalleeQualified: "auth.Login", Line: 10, File: "main.go"},
	}
	// Node IDs: 1=main(), 2=Login, 3=SignToken
	nodeIDs := map[string][]int64{
		"main":      {1},
		"Login":     {2},
		"SignToken": {3},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"main.go":       {"main": {1}},
		"auth/login.go": {"Login": {2}},
		"auth/jwt.go":   {"SignToken": {3}},
	}
	callerIDs := []int64{1} // main() is the caller

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm)

	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved call, got %d", len(resolved))
	}
	r := resolved[0]
	if r.Method != "import" {
		t.Errorf("resolution method = %q, want %q", r.Method, "import")
	}
	if r.Confidence != 1.0 {
		t.Errorf("confidence = %f, want 1.0", r.Confidence)
	}
	if r.TargetNodeID != 2 {
		t.Errorf("target node ID = %d, want 2 (Login)", r.TargetNodeID)
	}
}

func TestResolve_GoImport_PreservesNameMatch(t *testing.T) {
	// When import resolution fails (external package), name_match should still work
	files := []string{"main.go", "utils/helpers.go"}
	langs := []string{"go", "go"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{ImportedName: "external", ModulePath: "github.com/other/external", File: "main.go", Line: 3},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "Helper", CalleeQualified: "external.Helper", Line: 10, File: "main.go"},
	}
	nodeIDs := map[string][]int64{
		"main":   {1},
		"Helper": {2},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"main.go":          {"main": {1}},
		"utils/helpers.go": {"Helper": {2}},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm)

	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved call via name_match fallback, got %d", len(resolved))
	}
	if resolved[0].Method != "verified_unique" && resolved[0].Method != "name_match" {
		t.Errorf("resolution method = %q, want verified_unique or name_match", resolved[0].Method)
	}
}

func TestResolve_QualifiedStdlibCall_NotDeterministic(t *testing.T) {
	// RUN VERDICT (beancount-931): `for ... in os.walk(rootdir):` in tools/x.py
	// name-matched the ONLY project `walk` (account.walk). Strategy 1.9
	// (verified-unique) tags a globally-unique bare name as deterministic
	// (Method "verified_unique", conf 0.95) WITHOUT checking the qualifier — so a
	// stdlib `os.walk` becomes a "fact" caller of account.walk (the laundering the
	// downstream categorical gate then trusts).
	//
	// A qualified call X.attr(...) that reached Strategy 1.9 did NOT resolve its
	// qualifier via the import/type stages above => X is stdlib/external/unknown,
	// and a bare-name unique match is a FALSE positive. It must be demoted to
	// name_match (low trust) or dropped — never a deterministic method.
	//
	// RED before the Strategy-1.9 qualifier guard; GREEN after.
	files := []string{"tools/x.py", "beancount/core/account.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{ImportedName: "os", ModulePath: "os", File: "tools/x.py", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "walk", CalleeQualified: "os.walk", Line: 5, File: "tools/x.py"},
	}
	nodeIDs := map[string][]int64{"find_files": {1}, "walk": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"tools/x.py":                {"find_files": {1}},
		"beancount/core/account.py": {"walk": {2}},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm)

	deterministic := map[string]bool{
		"same_file": true, "import": true, "verified_unique": true,
		"type_flow": true, "import_type": true, "lsp_verified": true, "lsp": true,
	}
	for _, r := range resolved {
		if deterministic[r.Method] {
			t.Errorf(
				"qualified stdlib call os.walk resolved to project walk with DETERMINISTIC method %q (conf %.2f) "+
					"— would launder as a confident caller fact; want name_match or no edge",
				r.Method, r.Confidence,
			)
		}
	}
}

func TestResolve_UnqualifiedUniqueCall_StaysVerifiedUnique(t *testing.T) {
	// Regression guard for the fix above: a BARE unqualified call to a
	// globally-unique name must STILL resolve as verified_unique (the ACG/ECOOP
	// 2022 property the strategy is built on) — the qualifier guard must only
	// affect QUALIFIED calls.
	files := []string{"a/caller.py", "b/target.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "uniquefunc", CalleeQualified: "uniquefunc", Line: 5, File: "a/caller.py"},
	}
	nodeIDs := map[string][]int64{"caller": {1}, "uniquefunc": {2}}
	fileNodeIDs := map[string]map[string][]int64{
		"a/caller.py": {"caller": {1}},
		"b/target.py": {"uniquefunc": {2}},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved call, got %d", len(resolved))
	}
	if resolved[0].Method != "verified_unique" {
		t.Errorf("unqualified unique call: method = %q, want verified_unique", resolved[0].Method)
	}
}

func TestParseTSConfig(t *testing.T) {
	dir := t.TempDir()
	tsconfig := `{"compilerOptions":{"baseUrl":".","paths":{"@/*":["src/*"]}}}`
	if err := os.WriteFile(filepath.Join(dir, "tsconfig.json"), []byte(tsconfig), 0644); err != nil {
		t.Fatal(err)
	}
	cfg := ParseTSConfig(dir)
	if cfg == nil {
		t.Fatal("ParseTSConfig returned nil")
	}
	if cfg.BaseURL != "." {
		t.Errorf("baseUrl = %q, want %q", cfg.BaseURL, ".")
	}
	if _, ok := cfg.Paths["@/*"]; !ok {
		t.Error("expected @/* in paths")
	}

	// No tsconfig → nil
	if ParseTSConfig(t.TempDir()) != nil {
		t.Error("expected nil for missing tsconfig")
	}
}

func TestExpandTSConfigPath(t *testing.T) {
	cfg := &TSConfig{
		BaseURL: ".",
		Paths:   map[string][]string{"@/*": {"src/*"}},
	}
	tests := []struct {
		input string
		want  string
	}{
		{"@/auth/login", "src/auth/login"},
		{"@/utils/crypto", "src/utils/crypto"},
		{"./relative", ""},     // not an alias
		{"express", ""},        // not an alias
	}
	for _, tc := range tests {
		got := ExpandTSConfigPath(tc.input, cfg)
		if got != tc.want {
			t.Errorf("ExpandTSConfigPath(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

func TestBuildFileMap_TSIndexSuffix(t *testing.T) {
	fm := BuildFileMap(
		[]string{"src/auth/index.ts", "src/users/index.ts", "src/auth/login.ts"},
		[]string{"typescript", "typescript", "typescript"},
	)
	// Index files should register directory suffix variants
	for _, key := range []string{"auth", "users"} {
		if _, ok := fm[key]; !ok {
			t.Errorf("expected key %q in file map for index.ts barrel", key)
		}
	}
	// Full directory path should still work
	if _, ok := fm["src/auth"]; !ok {
		t.Error("expected full dir key 'src/auth'")
	}
}

func TestResolve_TSRelativeImport(t *testing.T) {
	files := []string{"src/index.ts", "src/auth/login.ts", "src/auth/index.ts"}
	langs := []string{"typescript", "typescript", "typescript"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{ImportedName: "login", ModulePath: "./auth/login", File: "src/index.ts", Line: 1},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "login", CalleeQualified: "login", Line: 5, File: "src/index.ts"},
	}
	nodeIDs := map[string][]int64{"start": {1}, "login": {2, 3}}
	fileNodeIDs := map[string]map[string][]int64{
		"src/index.ts":      {"start": {1}},
		"src/auth/login.ts": {"login": {2}},
		"src/auth/index.ts": {"login": {3}},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 resolved call, got %d", len(resolved))
	}
	if resolved[0].Method != "import" {
		t.Errorf("resolution method = %q, want %q", resolved[0].Method, "import")
	}
}

// ----------------------------------------------------------------------------
// SQUASH-BATCH tests (resolver.go items #1, #5, #6, #39, #40, #41).
// Each asserts the corrected trust labelling / drop behaviour. RED before the
// matching fix, GREEN after.
// ----------------------------------------------------------------------------

// tierForConf mirrors the central tierFor invariant so tests assert the contract
// (>=0.9 CERTIFIED, 0.5-0.9 CANDIDATE, <0.5 SPECULATIVE) rather than literals.
func tierForConf(c float64) string {
	switch {
	case c >= 0.9:
		return "CERTIFIED"
	case c >= 0.5:
		return "CANDIDATE"
	default:
		return "SPECULATIVE"
	}
}

// #1: tierFor — every emitted edge's TrustTier must follow its Confidence; a 0.85
// edge can NEVER be CERTIFIED. We exercise return_type (1.97, conf 0.85) which the
// old code stamped CERTIFIED, and assert it is now CANDIDATE via the central map.
//
// To force the call down to Strategy 1.97 (and not be caught by same_file / 1.9
// verified_unique / 1.94 impl_method first), `save` is a method on FOUR classes
// (>3 → 1.94 skips; multi-candidate → 1.9 single-candidate skips) and lives in a
// different file from the caller (same_file cannot match). get_user() returns User,
// so 1.97 bridges get_user().save() → User.save.
func TestResolve_TierFollowsConfidence_ReturnType085NotCertified(t *testing.T) {
	files := []string{"app.py", "models.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// ids: 1=caller(app.py), 2=get_user(models.py, ret User),
	// 10=User class, 11=A, 12=B, 13=C classes; 20..23 = save methods on each.
	nodeIDs := map[string][]int64{
		"caller":   {1},
		"get_user": {2},
		"User":     {10}, "A": {11}, "B": {12}, "C": {13},
		"save": {20, 21, 22, 23},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"app.py":    {"caller": {1}},
		"models.py": {"get_user": {2}, "User": {10}, "A": {11}, "B": {12}, "C": {13}, "save": {20, 21, 22, 23}},
	}
	meta := map[int64]NodeMeta{
		1:  {Label: "Function", File: "app.py", Name: "caller"},
		2:  {Label: "Function", File: "models.py", Name: "get_user", ReturnType: "User"},
		10: {Label: "Class", File: "models.py", Name: "User"},
		11: {Label: "Class", File: "models.py", Name: "A"},
		12: {Label: "Class", File: "models.py", Name: "B"},
		13: {Label: "Class", File: "models.py", Name: "C"},
		20: {Label: "Method", File: "models.py", Name: "save", ParentID: 10},
		21: {Label: "Method", File: "models.py", Name: "save", ParentID: 11},
		22: {Label: "Method", File: "models.py", Name: "save", ParentID: 12},
		23: {Label: "Method", File: "models.py", Name: "save", ParentID: 13},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "save", CalleeQualified: "get_user.save", Line: 5, File: "app.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var found bool
	for _, r := range resolved {
		if r.Method == "return_type" {
			found = true
			if r.TargetNodeID != 20 {
				t.Errorf("return_type target = %d, want 20 (User.save)", r.TargetNodeID)
			}
			if r.Confidence >= 0.9 {
				t.Errorf("return_type conf = %.2f, expected < 0.9", r.Confidence)
			}
			if r.TrustTier == "CERTIFIED" {
				t.Errorf("return_type (conf %.2f) stamped CERTIFIED — 0.85 must never be CERTIFIED", r.Confidence)
			}
			if r.TrustTier != tierForConf(r.Confidence) {
				t.Errorf("tier %q does not follow conf %.2f (want %q)", r.TrustTier, r.Confidence, tierForConf(r.Confidence))
			}
		}
	}
	if !found {
		t.Fatalf("expected a return_type edge; got %+v", resolved)
	}
}

// #5: impl_method resolved purely on global method-name uniqueness (no receiver-type
// proof) must be CANDIDATE (conf <= 0.6), never CERTIFIED.
func TestResolve_ImplMethod_NameUniquenessOnly_NotCertified(t *testing.T) {
	files := []string{"a.py", "b.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// `run` is a method of exactly ONE class (Command, id 3). A second free function
	// `run` (id 5) keeps Strategy 1.9 (single-candidate) from firing first, so the
	// call reaches Strategy 1.94 (impl_method). Receiver `obj` is NOT a known class.
	nodeIDs := map[string][]int64{
		"caller":  {1},
		"Command": {3},
		"run":     {4, 5}, // 4 = Command.run (method), 5 = free func run
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}},
		"b.py": {"Command": {3}, "run": {4, 5}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "a.py", Name: "caller"},
		3: {Label: "Class", File: "b.py", Name: "Command"},
		4: {Label: "Method", File: "b.py", Name: "run", ParentID: 3},
		5: {Label: "Function", File: "b.py", Name: "run"},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "run", CalleeQualified: "obj.run", Line: 5, File: "a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	var found bool
	for _, r := range resolved {
		if r.Method == "impl_method" {
			found = true
			if r.TrustTier == "CERTIFIED" {
				t.Errorf("impl_method (name-uniqueness only) stamped CERTIFIED — must be CANDIDATE; conf %.2f", r.Confidence)
			}
			if r.Confidence > 0.6 {
				t.Errorf("impl_method conf = %.2f, want <= 0.6 (no receiver-type proof)", r.Confidence)
			}
			if r.TrustTier != tierForConf(r.Confidence) {
				t.Errorf("tier %q does not follow conf %.2f", r.TrustTier, r.Confidence)
			}
		}
	}
	if !found {
		t.Fatalf("expected an impl_method edge; got %+v", resolved)
	}
}

// #6: a qualified single-candidate builtin-method call (obj.get()) whose receiver
// never resolved internally must be DROPPED (broad builtin set on the single-candidate
// path), not laundered into a name_match_qualified_unresolved edge.
func TestResolve_SingleCandidateQualifiedBuiltin_Dropped(t *testing.T) {
	files := []string{"a.py", "b.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// `get` has exactly ONE global definition (a method on some class) → would reach
	// Strategy 1.9's single-candidate path. `get` is in the broad builtinMethodNames
	// set, so a qualified obj.get() must be dropped, not emitted.
	nodeIDs := map[string][]int64{
		"caller": {1},
		"get":    {2},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.py": {"caller": {1}},
		"b.py": {"get": {2}},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "get", CalleeQualified: "cfg.get", Line: 5, File: "a.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm)
	for _, r := range resolved {
		if r.TargetNodeID == 2 {
			t.Errorf("qualified builtin cfg.get() laundered into edge (method %q, tier %q) — must be dropped", r.Method, r.TrustTier)
		}
	}
}

// #39: two same-file definitions of an UNQUALIFIED name must resolve to a best
// LOCAL candidate (same_file, CANDIDATE), never fall through to a cross-file name_match.
func TestResolve_SameFileAmbiguous_PrefersLocalOverCrossFile(t *testing.T) {
	files := []string{"local.py", "remote.py"}
	langs := []string{"python", "python"}
	fm := BuildFileMap(files, langs)

	// local.py defines `Foo` twice (a class id 2 and a factory function id 3).
	// remote.py also defines `Foo` (id 4). An unqualified call Foo() in local.py
	// must bind LOCAL (id 3, the callable) — not the cross-file remote id 4.
	nodeIDs := map[string][]int64{
		"caller": {1},
		"Foo":    {2, 3, 4},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"local.py":  {"caller": {1}, "Foo": {2, 3}},
		"remote.py": {"Foo": {4}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "local.py", Name: "caller"},
		2: {Label: "Class", File: "local.py", Name: "Foo"},
		3: {Label: "Function", File: "local.py", Name: "Foo"},
		4: {Label: "Function", File: "remote.py", Name: "Foo"},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "Foo", CalleeQualified: "Foo", Line: 5, File: "local.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, nil, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected exactly 1 resolved edge, got %d: %+v", len(resolved), resolved)
	}
	r := resolved[0]
	if r.Method != "same_file" {
		t.Errorf("method = %q, want same_file (locality must dominate)", r.Method)
	}
	if r.TargetNodeID != 3 {
		t.Errorf("target = %d, want 3 (the local callable Foo, not cross-file id 4)", r.TargetNodeID)
	}
	if r.TrustTier == "CERTIFIED" {
		t.Errorf("ambiguous same-file pick stamped CERTIFIED; want CANDIDATE (conf %.2f)", r.Confidence)
	}
}

// #40: an import that resolves to TWO candidate files with no same-dir winner must
// be demoted below CERTIFIED (ambiguous pick), and the pick must be deterministic.
func TestResolve_ImportAmbiguous_NoSameDirWinner_Demoted(t *testing.T) {
	files := []string{"top/main.py", "a/mod.py", "b/mod.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	// main.py imports `thing`; BOTH a/mod.py and b/mod.py export `thing`. Neither is
	// in the caller's dir (top/). The pick is ambiguous → not CERTIFIED, deterministic.
	imports := []parser.ImportRef{
		{ImportedName: "thing", ModulePath: "a.mod", File: "top/main.py", Line: 1},
		{ImportedName: "thing", ModulePath: "b.mod", File: "top/main.py", Line: 2},
	}
	nodeIDs := map[string][]int64{
		"main": {1}, "thing": {2, 3},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"top/main.py": {"main": {1}},
		"a/mod.py":    {"thing": {2}},
		"b/mod.py":    {"thing": {3}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "top/main.py", Name: "main"},
		2: {Label: "Function", File: "a/mod.py", Name: "thing"},
		3: {Label: "Function", File: "b/mod.py", Name: "thing"},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "thing", CalleeQualified: "thing", Line: 5, File: "top/main.py"},
	}
	callerIDs := []int64{1}

	var firstTarget int64
	for i := 0; i < 8; i++ {
		resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
		if len(resolved) != 1 {
			t.Fatalf("iter %d: expected 1 edge, got %d", i, len(resolved))
		}
		r := resolved[0]
		if r.Method == "import" && r.CandidateCount > 1 && r.TrustTier == "CERTIFIED" {
			t.Errorf("ambiguous multi-file import (count %d) stamped CERTIFIED; want demoted", r.CandidateCount)
		}
		if i == 0 {
			firstTarget = r.TargetNodeID
		} else if r.TargetNodeID != firstTarget {
			t.Errorf("non-deterministic import pick: iter0=%d iter%d=%d", firstTarget, i, r.TargetNodeID)
		}
	}
}

// #40 positive: when one candidate is in the caller's own directory it wins and stays
// CERTIFIED (the same-dir tie-break, deterministic).
func TestResolve_ImportSameDirWinner_Certified(t *testing.T) {
	files := []string{"pkg/main.py", "pkg/mod.py", "other/mod.py"}
	langs := []string{"python", "python", "python"}
	fm := BuildFileMap(files, langs)

	imports := []parser.ImportRef{
		{ImportedName: "thing", ModulePath: "pkg.mod", File: "pkg/main.py", Line: 1},
		{ImportedName: "thing", ModulePath: "other.mod", File: "pkg/main.py", Line: 2},
	}
	nodeIDs := map[string][]int64{"main": {1}, "thing": {2, 3}}
	fileNodeIDs := map[string]map[string][]int64{
		"pkg/main.py":   {"main": {1}},
		"pkg/mod.py":    {"thing": {2}}, // same dir as caller
		"other/mod.py":  {"thing": {3}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "pkg/main.py", Name: "main"},
		2: {Label: "Function", File: "pkg/mod.py", Name: "thing"},
		3: {Label: "Function", File: "other/mod.py", Name: "thing"},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "thing", CalleeQualified: "thing", Line: 5, File: "pkg/main.py"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	if len(resolved) != 1 {
		t.Fatalf("expected 1 edge, got %d", len(resolved))
	}
	r := resolved[0]
	if r.TargetNodeID != 2 {
		t.Errorf("same-dir target = %d, want 2 (pkg/mod.py in caller's dir)", r.TargetNodeID)
	}
	if r.Method == "import" && r.TrustTier != "CERTIFIED" {
		t.Errorf("same-dir import winner tier = %q, want CERTIFIED", r.TrustTier)
	}
}

// #41: Strategy 1.93 must exclude a "Self" qualifier (Rust Self::method() that slipped
// past 1.75). An imported class literally named "Self" must NOT be mis-scoped here.
func TestResolve_ImportType_GuardsSelfQualifier(t *testing.T) {
	files := []string{"a.rs", "b.rs"}
	langs := []string{"rust", "rust"}
	fm := BuildFileMap(files, langs)

	// Caller (id 1) has NO ParentID, so Strategy 1.75 cannot fire for Self::build().
	// A class named "Self" (id 2) with method build (id 3) is imported. With the #41
	// guard, 1.93 must NOT resolve Self::build() to this imported "Self" class.
	imports := []parser.ImportRef{
		{ImportedName: "Self", ModulePath: "b", File: "a.rs", Line: 1},
	}
	nodeIDs := map[string][]int64{
		"caller": {1}, "Self": {2}, "build": {3},
	}
	fileNodeIDs := map[string]map[string][]int64{
		"a.rs": {"caller": {1}},
		"b.rs": {"Self": {2}, "build": {3}},
	}
	meta := map[int64]NodeMeta{
		1: {Label: "Function", File: "a.rs", Name: "caller"}, // ParentID 0
		2: {Label: "Struct", File: "b.rs", Name: "Self"},
		3: {Label: "Method", File: "b.rs", Name: "build", ParentID: 2},
	}
	calls := []parser.CallRef{
		{CallerNodeIdx: 0, CalleeName: "build", CalleeQualified: "Self::build", Line: 5, File: "a.rs"},
	}
	callerIDs := []int64{1}

	resolved := Resolve(calls, nodeIDs, fileNodeIDs, callerIDs, imports, fm, meta)
	for _, r := range resolved {
		if r.Method == "import_type" {
			t.Errorf("Self::build() mis-resolved via import_type to a class named Self — #41 guard must exclude Self")
		}
	}
}
