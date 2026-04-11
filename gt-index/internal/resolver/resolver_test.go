package resolver

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/parser"
	"github.com/harneet2512/groundtruth/gt-index/internal/store"
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

// ── Python relative import resolution ────────────────────────────────────

func TestResolvePythonRelativeImport(t *testing.T) {
	tests := []struct {
		name    string
		modPath string
		file    string
		want    string
	}{
		{"single_dot", ".foo", "pkg/sub/mod.py", "pkg.sub.foo"},
		{"double_dot", "..utils", "pkg/sub/mod.py", "pkg.utils"},
		{"bare_dot", ".", "pkg/mod.py", "pkg"},
		{"triple_dot", "...core", "pkg/sub/deep/mod.py", "pkg.core"},
		{"astropy_relative", ".representation", "astropy/coordinates/sky_coordinate.py", "astropy.coordinates.representation"},
		{"astropy_up", "..units", "astropy/coordinates/sky_coordinate.py", "astropy.units"},
		{"root_level", ".foo", "setup.py", "foo"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := resolvePythonRelativeImport(tc.modPath, tc.file)
			if got != tc.want {
				t.Errorf("resolvePythonRelativeImport(%q, %q) = %q, want %q",
					tc.modPath, tc.file, got, tc.want)
			}
		})
	}
}

// ── Python import-resolved edge tests ────────────────────────────────────

func TestPythonImportResolution(t *testing.T) {
	filePaths := []string{
		"astropy/units/__init__.py",
		"astropy/units/quantity.py",
		"astropy/coordinates/__init__.py",
		"astropy/coordinates/representation.py",
		"astropy/coordinates/sky_coordinate.py",
	}
	fileLangs := []string{"python", "python", "python", "python", "python"}
	fileMap := BuildFileMap(filePaths, fileLangs)

	nodes := []store.Node{
		{ID: 1, Name: "Quantity", FilePath: "astropy/units/quantity.py", Label: "Class"},
		{ID: 2, Name: "CartesianRepresentation", FilePath: "astropy/coordinates/representation.py", Label: "Class"},
		{ID: 3, Name: "SkyCoord", FilePath: "astropy/coordinates/sky_coordinate.py", Label: "Class"},
		{ID: 4, Name: "transform_to", FilePath: "astropy/coordinates/sky_coordinate.py", Label: "Function"},
	}
	nodeDBIDs := []int64{1, 2, 3, 4}

	nameIndex := make(map[string][]int64)
	fileIndex := make(map[string]map[string]int64)
	for i, n := range nodes {
		id := nodeDBIDs[i]
		nameIndex[n.Name] = append(nameIndex[n.Name], id)
		if _, ok := fileIndex[n.FilePath]; !ok {
			fileIndex[n.FilePath] = make(map[string]int64)
		}
		fileIndex[n.FilePath][n.Name] = id
	}

	// Suppress unused variable warnings for the test helper imports
	_ = parser.ImportRef{}

	t.Run("alias_import_qualified_call", func(t *testing.T) {
		imports := []parser.ImportRef{
			{ImportedName: "u", ModulePath: "astropy.units", File: "astropy/coordinates/sky_coordinate.py", Line: 1},
		}
		calls := []parser.CallRef{
			{CalleeName: "Quantity", CalleeQualified: "u.Quantity", File: "astropy/coordinates/sky_coordinate.py", Line: 10, CallerNodeIdx: 0},
		}
		callerDBIDs := []int64{4}
		resolved := Resolve(calls, nameIndex, fileIndex, callerDBIDs, imports, fileMap)
		for _, r := range resolved {
			if r.TargetNodeID == 1 && r.Method == "import" {
				return // pass
			}
		}
		t.Errorf("alias import u.Quantity should resolve via import to Quantity(id=1), got %+v", resolved)
	})

	t.Run("relative_import_one_level", func(t *testing.T) {
		imports := []parser.ImportRef{
			{ImportedName: "CartesianRepresentation", ModulePath: ".representation", File: "astropy/coordinates/sky_coordinate.py", Line: 1},
		}
		calls := []parser.CallRef{
			{CalleeName: "CartesianRepresentation", CalleeQualified: "CartesianRepresentation", File: "astropy/coordinates/sky_coordinate.py", Line: 10, CallerNodeIdx: 0},
		}
		callerDBIDs := []int64{4}
		resolved := Resolve(calls, nameIndex, fileIndex, callerDBIDs, imports, fileMap)
		for _, r := range resolved {
			if r.TargetNodeID == 2 && r.Method == "import" {
				return
			}
		}
		t.Errorf("relative import .representation should resolve via import, got %+v", resolved)
	})

	t.Run("relative_import_two_levels", func(t *testing.T) {
		imports := []parser.ImportRef{
			{ImportedName: "Quantity", ModulePath: "..units", File: "astropy/coordinates/sky_coordinate.py", Line: 1},
		}
		calls := []parser.CallRef{
			{CalleeName: "Quantity", CalleeQualified: "Quantity", File: "astropy/coordinates/sky_coordinate.py", Line: 10, CallerNodeIdx: 0},
		}
		callerDBIDs := []int64{4}
		resolved := Resolve(calls, nameIndex, fileIndex, callerDBIDs, imports, fileMap)
		for _, r := range resolved {
			if r.TargetNodeID == 1 && r.Method == "import" {
				return
			}
		}
		t.Errorf("relative import ..units should resolve Quantity via import, got %+v", resolved)
	})

	t.Run("from_dot_import_submodule", func(t *testing.T) {
		imports := []parser.ImportRef{
			{ImportedName: "representation", ModulePath: ".", File: "astropy/coordinates/sky_coordinate.py", Line: 1},
		}
		calls := []parser.CallRef{
			{CalleeName: "CartesianRepresentation", CalleeQualified: "representation.CartesianRepresentation", File: "astropy/coordinates/sky_coordinate.py", Line: 10, CallerNodeIdx: 0},
		}
		callerDBIDs := []int64{4}
		resolved := Resolve(calls, nameIndex, fileIndex, callerDBIDs, imports, fileMap)
		for _, r := range resolved {
			if r.TargetNodeID == 2 && r.Method == "import" {
				return
			}
		}
		t.Errorf("from . import representation + qualified call should resolve, got %+v", resolved)
	})

	t.Run("package_reexport_init", func(t *testing.T) {
		imports := []parser.ImportRef{
			{ImportedName: "Quantity", ModulePath: "astropy.units", File: "astropy/coordinates/sky_coordinate.py", Line: 1},
		}
		calls := []parser.CallRef{
			{CalleeName: "Quantity", CalleeQualified: "Quantity", File: "astropy/coordinates/sky_coordinate.py", Line: 10, CallerNodeIdx: 0},
		}
		callerDBIDs := []int64{4}
		resolved := Resolve(calls, nameIndex, fileIndex, callerDBIDs, imports, fileMap)
		for _, r := range resolved {
			if r.TargetNodeID == 1 && r.Method == "import" {
				return
			}
		}
		t.Errorf("package re-export should resolve Quantity via sibling fallback, got %+v", resolved)
	})
}
