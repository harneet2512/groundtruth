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
