package parser

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// TestExtractDataFlow verifies the data_flow base (per-parameter forward slice) is
// extracted on all 5 Tier-1 languages — the value-provenance dimension the call
// graph lacks. Each fixture passes a parameter through a call, a comparison, and a
// return; the data_flow property must record where the input value flows.
func TestExtractDataFlow(t *testing.T) {
	cases := []struct {
		name  string
		ext   string
		param string
		src   string
		// substrings the data_flow value for `param` must contain (any-of per slot)
		wantFlow []string
	}{
		{
			name:  "python",
			ext:   ".py",
			param: "count",
			src: "def handle(count):\n" +
				"    validate(count)\n" +
				"    if count != 1:\n" +
				"        return count + 1\n",
			wantFlow: []string{"validate(count)", "count != 1", "count + 1"},
		},
		{
			name:  "javascript",
			ext:   ".js",
			param: "count",
			src: "function handle(count) {\n" +
				"  validate(count);\n" +
				"  if (count !== 1) { return count + 1; }\n" +
				"}\n",
			wantFlow: []string{"validate(count)", "count !== 1", "count + 1"},
		},
		{
			name:  "typescript",
			ext:   ".ts",
			param: "count",
			src: "function handle(count: number): number {\n" +
				"  validate(count);\n" +
				"  if (count !== 1) { return count + 1; }\n" +
				"  return 0;\n" +
				"}\n",
			wantFlow: []string{"validate(count)", "count !== 1", "count + 1"},
		},
		{
			name:  "go",
			ext:   ".go",
			param: "count",
			src: "package p\n" +
				"func Handle(count int) int {\n" +
				"    validate(count)\n" +
				"    if count != 1 {\n" +
				"        return count + 1\n" +
				"    }\n" +
				"    return 0\n" +
				"}\n",
			wantFlow: []string{"validate(count)", "count != 1", "count + 1"},
		},
		{
			name:  "rust",
			ext:   ".rs",
			param: "count",
			src: "fn handle(count: i32) -> i32 {\n" +
				"    validate(count);\n" +
				"    if count != 1 { return count + 1; }\n" +
				"    0\n" +
				"}\n",
			wantFlow: []string{"validate(count)", "count != 1", "count + 1"},
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			path := filepath.Join(dir, "fixture"+tc.ext)
			if err := os.WriteFile(path, []byte(tc.src), 0o644); err != nil {
				t.Fatal(err)
			}
			spec := specs.ForExtension(tc.ext)
			if spec == nil {
				t.Skipf("no spec registered for %s", tc.ext)
			}
			sf := walker.SourceFile{Path: path, AbsPath: path, Language: spec.Name, Spec: spec}
			res, err := ParseFile(sf, false)
			if err != nil {
				t.Fatalf("ParseFile: %v", err)
			}
			var got string
			for _, p := range res.Properties {
				if p.Kind == "data_flow" && strings.HasPrefix(p.Value, tc.param+" ->") {
					got = p.Value
					break
				}
			}
			if got == "" {
				t.Fatalf("[%s] no data_flow property for param %q (props: %d)", tc.name, tc.param, len(res.Properties))
			}
			// at least one expected flow context must appear
			hit := false
			for _, w := range tc.wantFlow {
				if strings.Contains(got, w) {
					hit = true
					break
				}
			}
			if !hit {
				t.Errorf("[%s] data_flow=%q, expected one of %v", tc.name, got, tc.wantFlow)
			}
			t.Logf("[%s] data_flow: %s", tc.name, got)
		})
	}
}
