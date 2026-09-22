package main

// convergence_test.go — incremental-vs-clean convergence over real indexing.
//
// For each fixture language and each edit shape, the harness:
//   (a) builds a clean graph of v1 (the certified parent),
//   (b) applies the edit to the working tree (uncommitted, as an agent does),
//   (c) updates the parent the two ways the harness does
//       (gt_engine/indexer.py _ensure_index_incremental_unlocked):
//         - batch amend:   -root R -output C -workers N -closure=true -amend-parent P
//         - per-file:      copy P, then -root R -output C -file <rel> per dirty path
//   (d) builds a clean graph of v2 with no parse cache,
//   (e) compares id-free semantic snapshots (convergence_snapshot_test.go).
//
// The batch amend must converge exactly. The per-file path re-derives only the
// dirty files, so layers it cannot re-derive must be declared stale in
// project_meta and must not serve rows the clean rebuild would not serve.

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

type convergenceEdit struct {
	name  string
	write map[string]string // path -> new content ("" deletes the path)
}

type convergenceFixture struct {
	lang    string
	comment string
	files   map[string]string
	edits   []convergenceEdit
}

func convergenceFixtures() []convergenceFixture {
	pyCore := "def helper(x):\n    return x + 1\n\n\ndef compute(a, b):\n    y = helper(a)\n    return y + b\n\n\ndef to_delete(z):\n    return z * 2\n\n\nclass Store:\n    def __init__(self):\n        self.items = []\n\n    def add(self, item):\n        self.items.append(item)\n        return len(self.items)\n"
	pyApp := "from core import helper, compute, to_delete, Store\n\n\ndef main():\n    s = Store()\n    s.add(1)\n    return compute(1, 2) + to_delete(3)\n\n\ndef other():\n    late_bound()\n    return helper(5)\n"
	pyGone := "from core import helper\n\n\ndef legacy():\n    return helper(9)\n"
	py := convergenceFixture{
		lang: "python", comment: "#",
		files: map[string]string{
			"core.py": pyCore, "app.py": pyApp, "gone.py": pyGone,
			"test_app.py": "from app import main\n\n\ndef test_main():\n    assert main() == 10\n",
		},
		edits: []convergenceEdit{
			{"rename_callee", map[string]string{"core.py": strings.NewReplacer("def helper(x):\n    return x + 1", "def helper_v2(x, scale):\n    return x * scale", "y = helper(a)", "y = helper_v2(a, 1)").Replace(pyCore)}},
			{"move_call", map[string]string{
				"app.py":  strings.Replace(pyApp, "return compute(1, 2) + to_delete(3)", "return to_delete(3)", 1),
				"gone.py": "from core import helper, compute\n\n\ndef legacy():\n    return helper(9) + compute(1, 2)\n"}},
			{"delete_function", map[string]string{"core.py": strings.Replace(pyCore, "def to_delete(z):\n    return z * 2\n\n\n", "", 1)}},
			{"add_file", map[string]string{"extra.py": "from core import compute\n\n\ndef late_bound():\n    return compute(0, 0)\n"}},
			{"delete_file", map[string]string{"gone.py": ""}},
			{"rename_across", map[string]string{
				"core.py": strings.Replace(pyCore, "def compute(a, b):", "def compute_total(a, b):", 1),
				"app.py":  strings.NewReplacer("helper, compute, to_delete", "helper, compute_total, to_delete", "return compute(1, 2)", "return compute_total(1, 2)").Replace(pyApp)}},
		},
	}

	goCore := "package core\n\nfunc Helper(x int) int {\n\treturn x + 1\n}\n\nfunc Compute(a, b int) int {\n\ty := Helper(a)\n\treturn y + b\n}\n\nfunc ToDelete(z int) int {\n\treturn z * 2\n}\n\ntype Store struct {\n\tItems []int\n}\n\nfunc (s *Store) Add(item int) int {\n\ts.Items = append(s.Items, item)\n\treturn len(s.Items)\n}\n"
	goApp := "package app\n\nimport \"fx/core\"\n\nfunc Main() int {\n\ts := &core.Store{}\n\ts.Add(1)\n\treturn core.Compute(1, 2) + core.ToDelete(3)\n}\n\nfunc Other() int {\n\tLateBound()\n\treturn core.Helper(5)\n}\n"
	goGone := "package gone\n\nimport \"fx/core\"\n\nfunc Legacy() int {\n\treturn core.Helper(9)\n}\n"
	gof := convergenceFixture{
		lang: "go", comment: "//",
		files: map[string]string{
			"go.mod": "module fx\n\ngo 1.22\n", "core/core.go": goCore, "app/app.go": goApp, "gone/gone.go": goGone,
			"app/app_test.go": "package app\n\nimport \"testing\"\n\nfunc TestMain2(t *testing.T) {\n\tif Main() != 10 {\n\t\tt.Fatal(\"main\")\n\t}\n}\n",
		},
		edits: []convergenceEdit{
			{"rename_callee", map[string]string{"core/core.go": strings.NewReplacer("func Helper(x int) int {\n\treturn x + 1", "func HelperV2(x, scale int) int {\n\treturn x * scale", "y := Helper(a)", "y := HelperV2(a, 1)").Replace(goCore)}},
			{"move_call", map[string]string{
				"app/app.go":   strings.Replace(goApp, "return core.Compute(1, 2) + core.ToDelete(3)", "return core.ToDelete(3)", 1),
				"gone/gone.go": strings.Replace(goGone, "return core.Helper(9)", "return core.Helper(9) + core.Compute(1, 2)", 1)}},
			{"delete_function", map[string]string{"core/core.go": strings.Replace(goCore, "func ToDelete(z int) int {\n\treturn z * 2\n}\n\n", "", 1)}},
			{"add_file", map[string]string{"app/extra.go": "package app\n\nimport \"fx/core\"\n\nfunc LateBound() int {\n\treturn core.Compute(0, 0)\n}\n"}},
			{"delete_file", map[string]string{"gone/gone.go": ""}},
			{"rename_across", map[string]string{
				"core/core.go": strings.Replace(goCore, "func Compute(a, b int) int {", "func ComputeTotal(a, b int) int {", 1),
				"app/app.go":   strings.Replace(goApp, "core.Compute(1, 2)", "core.ComputeTotal(1, 2)", 1)}},
		},
	}

	tsCore := "export function helper(x: number): number {\n  return x + 1;\n}\n\nexport function compute(a: number, b: number): number {\n  const y = helper(a);\n  return y + b;\n}\n\nexport function toDelete(z: number): number {\n  return z * 2;\n}\n\nexport class Store {\n  items: number[] = [];\n  add(item: number): number {\n    this.items.push(item);\n    return this.items.length;\n  }\n}\n"
	tsApp := "import { helper, compute, toDelete, Store } from \"./core\";\nimport { lateBound } from \"./extra\";\n\nexport function main(): number {\n  const s = new Store();\n  s.add(1);\n  return compute(1, 2) + toDelete(3);\n}\n\nexport function other(): number {\n  lateBound();\n  return helper(5);\n}\n"
	tsGone := "import { helper } from \"./core\";\n\nexport function legacy(): number {\n  return helper(9);\n}\n"
	ts := convergenceFixture{
		lang: "typescript", comment: "//",
		files: map[string]string{
			"src/core.ts": tsCore, "src/app.ts": tsApp, "src/gone.ts": tsGone,
			"src/app.test.ts": "import { main } from \"./app\";\n\ntest(\"main\", () => {\n  expect(main()).toBe(10);\n});\n",
		},
		edits: []convergenceEdit{
			{"rename_callee", map[string]string{"src/core.ts": strings.NewReplacer("export function helper(x: number): number {\n  return x + 1;", "export function helperV2(x: number, scale: number): number {\n  return x * scale;", "const y = helper(a);", "const y = helperV2(a, 1);").Replace(tsCore)}},
			{"move_call", map[string]string{
				"src/app.ts":  strings.Replace(tsApp, "return compute(1, 2) + toDelete(3);", "return toDelete(3);", 1),
				"src/gone.ts": "import { helper, compute } from \"./core\";\n\nexport function legacy(): number {\n  return helper(9) + compute(1, 2);\n}\n"}},
			{"delete_function", map[string]string{"src/core.ts": strings.Replace(tsCore, "export function toDelete(z: number): number {\n  return z * 2;\n}\n\n", "", 1)}},
			{"add_file", map[string]string{"src/extra.ts": "import { compute } from \"./core\";\n\nexport function lateBound(): number {\n  return compute(0, 0);\n}\n"}},
			{"delete_file", map[string]string{"src/gone.ts": ""}},
			{"rename_across", map[string]string{
				"src/core.ts": strings.Replace(tsCore, "export function compute(", "export function computeTotal(", 1),
				"src/app.ts":  strings.NewReplacer("helper, compute, toDelete", "helper, computeTotal, toDelete", "return compute(1, 2)", "return computeTotal(1, 2)").Replace(tsApp)}},
		},
	}
	return []convergenceFixture{py, gof, ts}
}

const convergenceCommits = 3

func writeConvergenceRepo(t *testing.T, repo string, fx convergenceFixture) {
	t.Helper()
	git := func(args ...string) {
		full := append([]string{"-C", repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
			"-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"}, args...)
		if out, err := exec.Command("git", full...).CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	for rev := 0; rev < convergenceCommits; rev++ {
		for name, body := range fx.files {
			content := body
			if rev > 0 && name != "go.mod" {
				content = body + fmt.Sprintf("%s rev %d\n", fx.comment, rev)
			}
			p := filepath.Join(repo, filepath.FromSlash(name))
			if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
				t.Fatal(err)
			}
		}
		if rev == 0 {
			git("init", "-q")
		}
		git("add", "-A")
		git("commit", "-q", "-m", fmt.Sprintf("rev %d", rev))
	}
}

func applyConvergenceEdit(t *testing.T, repo string, edit convergenceEdit) []string {
	t.Helper()
	var changed []string
	for name, body := range edit.write {
		p := filepath.Join(repo, filepath.FromSlash(name))
		if body == "" {
			if err := os.Remove(p); err != nil {
				t.Fatal(err)
			}
		} else if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		changed = append(changed, name)
	}
	sort.Strings(changed)
	return changed
}

func runConvergenceIndexer(t *testing.T, bin string, env []string, args ...string) string {
	t.Helper()
	cmd := exec.Command(bin, args...)
	cmd.Env = append(os.Environ(), env...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("gt-index %v: %v\n%s", args, err, out)
	}
	return string(out)
}

func copyConvergenceGraph(t *testing.T, src, dst string) {
	t.Helper()
	in, err := os.Open(src)
	if err != nil {
		t.Fatal(err)
	}
	defer in.Close()
	out, err := os.Create(dst)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(out, in); err != nil {
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
}

// convergenceScenario builds parent, batch amend, per-file amend and clean
// graphs for one edit and returns their paths.
type convergenceGraphs struct{ parent, batch, perFile, clean string }

func buildConvergenceScenario(t *testing.T, bin string, fx convergenceFixture, edit convergenceEdit) convergenceGraphs {
	t.Helper()
	root := t.TempDir()
	repo := filepath.Join(root, "repo")
	writeConvergenceRepo(t, repo, fx)
	cacheEnv := []string{"GT_PARSE_CACHE_ROOT=" + filepath.Join(root, "cache"), "GT_REQUIRE_FTS5=1"}
	g := convergenceGraphs{
		parent: filepath.Join(root, "parent.db"), batch: filepath.Join(root, "batch.db"),
		perFile: filepath.Join(root, "perfile.db"), clean: filepath.Join(root, "clean.db"),
	}
	full := []string{"-root", repo, "-workers", "2", "-closure=true"}
	runConvergenceIndexer(t, bin, cacheEnv, append(full, "-output", g.parent)...)
	changed := applyConvergenceEdit(t, repo, edit)
	runConvergenceIndexer(t, bin, cacheEnv, append(full, "-output", g.batch, "-amend-parent", g.parent)...)
	copyConvergenceGraph(t, g.parent, g.perFile)
	for _, rel := range changed {
		runConvergenceIndexer(t, bin, cacheEnv, "-root", repo, "-output", g.perFile, "-file", rel)
	}
	runConvergenceIndexer(t, bin, []string{"GT_REQUIRE_FTS5=1"}, append(full, "-output", g.clean)...)
	return g
}

// TestBatchAmendConvergesToCleanRebuild: the batch amend the harness runs on
// every dirty set must publish exactly what a clean rebuild of the same tree
// publishes — every table, every derived layer, the FTS indexes.
func TestBatchAmendConvergesToCleanRebuild(t *testing.T) {
	if testing.Short() {
		t.Skip("builds and runs the gt-index binary; skipped under -short")
	}
	bin := buildDerivedIndexer(t)
	for _, fx := range convergenceFixtures() {
		for _, edit := range fx.edits {
			fx, edit := fx, edit
			t.Run(fx.lang+"/"+edit.name, func(t *testing.T) {
				g := buildConvergenceScenario(t, bin, fx, edit)
				clean, batch := snapshotGraph(t, g.clean), snapshotGraph(t, g.batch)
				if len(clean["edges.CALLS"]) == 0 {
					t.Fatalf("fixture produced no CALLS edges: the comparison would be vacuous")
				}
				for _, d := range snapshotDiff(clean, batch, nil) {
					t.Errorf("batch amend diverges from clean rebuild — %s", d)
				}
				perFile := snapshotGraph(t, g.perFile)
				for _, d := range perFileDivergences(clean, perFile) {
					t.Errorf("per-file amend diverges from clean rebuild — %s", d)
				}
				if gaps := perFileKnownGaps(clean, perFile); len(gaps) > 0 {
					t.Logf("per-file amend: declared gap unedited_file_edge_rebinding accounts for %v", gaps)
				}
			})
		}
	}
}
