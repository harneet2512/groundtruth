package parser

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func TestComputedCallTargetIsMarkedDynamic(t *testing.T) {
	path := filepath.Join(t.TempDir(), "sample.js")
	source := "function run(name) { return handlers[name](); }\n"
	if err := os.WriteFile(path, []byte(source), 0o600); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".js")
	result, err := ParseFile(walker.SourceFile{Path: "sample.js", AbsPath: path, Language: spec.Name, Spec: spec}, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(result.Calls) != 1 {
		t.Fatalf("calls=%d, want 1: %+v", len(result.Calls), result.Calls)
	}
	if !result.Calls[0].DynamicDispatch {
		t.Fatalf("computed call target was not marked dynamic: %+v", result.Calls[0])
	}
	call := result.Calls[0]
	if call.ASTPath == "" || call.ByteStart >= call.ByteEnd || call.DispatchForm != "dynamic_name" || call.ColumnStart == 0 && call.Line == 0 {
		t.Fatalf("callsite identity inputs were not captured from the AST: %+v", call)
	}
	rebuilt, err := ParseFile(walker.SourceFile{Path: "sample.js", AbsPath: path, Language: spec.Name, Spec: spec}, false)
	if err != nil || len(rebuilt.Calls) != 1 {
		t.Fatalf("repeat parse failed: result=%+v err=%v", rebuilt, err)
	}
	if rebuilt.Calls[0].ASTPath != call.ASTPath || rebuilt.Calls[0].ByteStart != call.ByteStart || rebuilt.Calls[0].ByteEnd != call.ByteEnd {
		t.Fatalf("callsite identity inputs changed across rebuild: first=%+v second=%+v", call, rebuilt.Calls[0])
	}
}

func TestMalformedSourceIsRetainedAsParserIncomplete(t *testing.T) {
	path := filepath.Join(t.TempDir(), "broken.js")
	// A complete call followed by invalid syntax produces a recoverable tree with
	// an ERROR node while retaining the partial callsite evidence.
	source := "function run() { target(); }\n@\n"
	if err := os.WriteFile(path, []byte(source), 0o600); err != nil {
		t.Fatal(err)
	}
	spec := specs.ForExtension(".js")
	result, err := ParseFile(walker.SourceFile{Path: "broken.js", AbsPath: path, Language: spec.Name, Spec: spec}, false)
	if err != nil {
		t.Fatal(err)
	}
	if !result.ParserIncomplete {
		t.Fatal("malformed source was reported parser-complete")
	}
	if len(result.Calls) == 0 {
		t.Fatal("malformed fixture did not retain its complete callsite")
	}
	for _, call := range result.Calls {
		if !call.ParserIncomplete {
			t.Fatalf("partial call leaked parser authority: %+v", call)
		}
	}
}

func TestStaticGenericAndTemplateCallsAreNotMarkedDynamic(t *testing.T) {
	tests := []struct {
		name, extension, source string
	}{
		{"python", ".py", "def f():\n    return target()\n"},
		{"typescript", ".ts", "function f() { return target<string>(); }\n"},
		{"go_generic", ".go", "package p\nfunc f() { Run[int]() }\n"},
		{"rust_generic", ".rs", "fn f() { run::<i32>(); }\n"},
		{"cpp_template", ".cpp", "void f() { run<int>(); }\n"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "sample"+tc.extension)
			if err := os.WriteFile(path, []byte(tc.source), 0o600); err != nil {
				t.Fatal(err)
			}
			spec := specs.ForExtension(tc.extension)
			result, err := ParseFile(walker.SourceFile{Path: filepath.Base(path), AbsPath: path, Language: spec.Name, Spec: spec}, false)
			if err != nil {
				t.Fatal(err)
			}
			if len(result.Calls) == 0 {
				t.Fatalf("fixture produced no call: %+v", result)
			}
			for _, call := range result.Calls {
				if call.DynamicDispatch {
					t.Fatalf("static call classified dynamic: %+v", call)
				}
			}
		})
	}
}
