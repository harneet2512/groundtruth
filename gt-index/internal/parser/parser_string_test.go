package parser

import "testing"

func TestClassifyAssertion(t *testing.T) {
	tests := []struct {
		name        string
		qualified   string
		simple      string
		wantKind    string
		wantIsAssert bool
	}{
		// Python unittest
		{"py assertEqual", "self.assertEqual", "assertEqual", "assertEqual", true},
		{"py assertRaises", "self.assertRaises", "assertRaises", "assertRaises", true},
		{"py assertIn", "self.assertIn", "assertIn", "assertIn", true},
		{"py assertTrue", "self.assertTrue", "assertTrue", "assertTrue", true},
		{"py assertIsNone", "self.assertIsNone", "assertIsNone", "assertIsNone", true},

		// Python pytest
		{"pytest raises", "pytest.raises", "raises", "raises", true},

		// Go testify
		{"go assert.Equal", "assert.Equal", "Equal", "Equal", true},
		{"go assert.NoError", "assert.NoError", "NoError", "NoError", true},
		{"go require.NotNil", "require.NotNil", "NotNil", "NotNil", true},

		// Go testing.T
		{"go t.Error", "t.Error", "Error", "Error", true},
		{"go t.Fatal", "t.Fatal", "Fatal", "Fatal", true},
		{"go t.Errorf", "t.Errorf", "Errorf", "Errorf", true},
		{"go t.FailNow", "t.FailNow", "FailNow", "FailNow", true},

		// JS/TS expect
		{"js expect", "expect", "expect", "expect", true},

		// JS/TS assert
		{"js assert.strictEqual", "assert.strictEqual", "strictEqual", "strictEqual", true},
		{"js assert.deepEqual", "assert.deepEqual", "deepEqual", "deepEqual", true},

		// C# Assert
		{"csharp Assert.AreEqual", "Assert.AreEqual", "AreEqual", "AreEqual", true},
		{"csharp Assert.That", "Assert.That", "That", "That", true},
		{"csharp Assert.IsTrue", "Assert.IsTrue", "IsTrue", "IsTrue", true},

		// JUnit / Kotlin
		{"junit assertEquals", "assertEquals", "assertEquals", "assertEquals", true},
		{"junit assertNotNull", "assertNotNull", "assertNotNull", "assertNotNull", true},
		{"kotlin assertThrows", "assertThrows", "assertThrows", "assertThrows", true},

		// PHP
		{"php assertEquals", "this->assertEquals", "assertEquals", "assertEquals", true},
		{"php assertSame", "this->assertSame", "assertSame", "assertSame", true},

		// Ruby RSpec
		{"ruby expect", "expect", "expect", "expect", true},
		{"ruby should", "should", "should", "should", true},

		// Jest/Vitest matchers: expect(x).toBe(y)
		{"jest toBe", "expect(x).toBe", "toBe", "toBe", true},
		{"jest toEqual", "expect(x).toEqual", "toEqual", "toEqual", true},
		{"jest toHaveLength", "expect(arr).toHaveLength", "toHaveLength", "toHaveLength", true},
		{"jest toBeDefined", "expect(x).toBeDefined", "toBeDefined", "toBeDefined", true},
		{"jest toThrow", "expect(fn).toThrow", "toThrow", "toThrow", true},
		{"jest toContain", "expect(arr).toContain", "toContain", "toContain", true},
		{"jest not.toBe", "expect(x).not.toBe", "toBe", "toBe", true},

		// Swift XCTest
		{"swift XCTAssertEqual", "XCTAssertEqual", "XCTAssertEqual", "XCTAssertEqual", true},
		{"swift XCTAssertTrue", "XCTAssertTrue", "XCTAssertTrue", "XCTAssertTrue", true},
		{"swift XCTAssertNil", "XCTAssertNil", "XCTAssertNil", "XCTAssertNil", true},

		// Non-assertions
		{"not assert: print", "print", "print", "", false},
		{"not assert: fmt.Println", "fmt.Println", "Println", "", false},
		{"not assert: len", "len", "len", "", false},
		{"not assert: append", "append", "append", "", false},
		{"not assert: make", "make", "make", "", false},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			kind, isAssert := classifyAssertion(tc.qualified, tc.simple)
			if isAssert != tc.wantIsAssert {
				t.Errorf("classifyAssertion(%q, %q) isAssert = %v, want %v",
					tc.qualified, tc.simple, isAssert, tc.wantIsAssert)
			}
			if isAssert && kind != tc.wantKind {
				t.Errorf("classifyAssertion(%q, %q) kind = %q, want %q",
					tc.qualified, tc.simple, kind, tc.wantKind)
			}
		})
	}
}

func TestLastDotComponent(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"com.foo.Bar", "Bar"},
		{"Bar", "Bar"},
		{"a.b.c.d", "d"},
		{"", ""},
	}
	for _, tc := range tests {
		got := lastDotComponent(tc.input)
		if got != tc.want {
			t.Errorf("lastDotComponent(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

func TestLastSlashComponent(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"foo/bar/baz", "baz"},
		{"baz", "baz"},
		{"a/b", "b"},
	}
	for _, tc := range tests {
		got := lastSlashComponent(tc.input)
		if got != tc.want {
			t.Errorf("lastSlashComponent(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

func TestLastColonComponent(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"crate::foo::bar", "bar"},
		{"bar", "bar"},
		{"a::b", "b"},
	}
	for _, tc := range tests {
		got := lastColonComponent(tc.input)
		if got != tc.want {
			t.Errorf("lastColonComponent(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

func TestStripQuotes(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{`"hello"`, "hello"},
		{`'world'`, "world"},
		{"`backtick`", "backtick"},
		{"noquotes", "noquotes"},
		{`""`, ""},
		{"x", "x"},
	}
	for _, tc := range tests {
		got := stripQuotes(tc.input)
		if got != tc.want {
			t.Errorf("stripQuotes(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}
