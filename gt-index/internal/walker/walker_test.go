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
