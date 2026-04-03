package resolver

import "testing"

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
