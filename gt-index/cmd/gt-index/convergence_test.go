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
	pyCore := "def helper(x):\n    return x + 1\n\n\ndef logged(f):\n    return f\n\n\ndef compute(a, b):\n    y = helper(a)\n    return y + b\n\n\ndef to_delete(z):\n    return z * 2\n\n\nclass Store:\n    def __init__(self):\n        self.items = []\n\n    def add(self, item):\n        self.items.append(item)\n        return len(self.items)\n"
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
			{"modify_callee_body", map[string]string{
				"core.py": strings.Replace(pyCore, "def helper(x):\n    return x + 1", "def helper(x):\n    return (x + 1) * 2", 1)}},
			{"change_import", map[string]string{
				"gone.py": "import core\n\n\ndef legacy():\n    return core.helper(9)\n"}},
			{"route_decorator", map[string]string{
				"core.py": strings.Replace(pyCore, "class Store:", "@logged\nclass Store:", 1)}},
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
			{"modify_callee_body", map[string]string{
				"core/core.go": strings.Replace(goCore, "func Helper(x int) int {\n\treturn x + 1\n}", "func Helper(x int) int {\n\treturn (x + 1) * 2\n}", 1)}},
			{"change_import", map[string]string{
				"app/app.go": strings.NewReplacer(
					"import \"fx/core\"", "import c \"fx/core\"",
					"core.Store", "c.Store",
					"core.Compute", "c.Compute",
					"core.ToDelete", "c.ToDelete",
					"core.Helper", "c.Helper").Replace(goApp)}},
			// Go has no decorator/annotation construct: no route_decorator edit.
		},
	}

	tsCore := "export function helper(x: number): number {\n  return x + 1;\n}\n\nexport function logged(f: unknown): unknown {\n  return f;\n}\n\nexport function compute(a: number, b: number): number {\n  const y = helper(a);\n  return y + b;\n}\n\nexport function toDelete(z: number): number {\n  return z * 2;\n}\n\nexport class Store {\n  items: number[] = [];\n  add(item: number): number {\n    this.items.push(item);\n    return this.items.length;\n  }\n}\n"
	tsApp := "import { helper, compute, toDelete, Store } from \"./core\";\nimport { lateBound } from \"./extra\";\n\nexport function main(): number {\n  const s = new Store();\n  s.add(1);\n  return compute(1, 2) + toDelete(3);\n}\n\nexport function other(): number {\n  lateBound();\n  return helper(5);\n}\n"
	tsGone := "import { helper } from \"./core\";\n\nexport function legacy(): number {\n  return helper(9);\n}\n"
	// Express-style server/client pair: covers HANDLES_ROUTE, MIDDLEWARE_ON
	// (app.use), and API_CALL (fetch/axios path-matched to the route file).
	// client.ts calls /api/users before server.ts declares it — the base graph
	// has one API_CALL, the post-add_route graph two. The middleware lives in
	// mw.ts so its MIDDLEWARE_ON edge is foreign-sourced: amending mw.ts kills
	// the edge's source node while the edge's owner (server.ts) is untouched.
	tsMw := "export function authMiddleware(req: any, res: any, next: any): void { next(); }\n"
	tsServer := "import { authMiddleware } from \"./mw\";\n\nfunction listItems(req: any, res: any): void { res.json([]); }\n\nconst app = express();\napp.use(authMiddleware);\napp.get(\"/api/items\", listItems);\n"
	tsClient := "export function load(): void {\n  fetch(\"/api/items\");\n  axios.post(\"/api/users\", {});\n}\n"
	ts := convergenceFixture{
		lang: "typescript", comment: "//",
		files: map[string]string{
			"src/core.ts": tsCore, "src/app.ts": tsApp, "src/gone.ts": tsGone,
			"src/server.ts": tsServer, "src/client.ts": tsClient, "src/mw.ts": tsMw,
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
			{"modify_callee_body", map[string]string{
				"src/core.ts": strings.Replace(tsCore, "export function helper(x: number): number {\n  return x + 1;\n}", "export function helper(x: number): number {\n  return (x + 1) * 2;\n}", 1)}},
			{"change_import", map[string]string{
				"src/gone.ts": "import * as core from \"./core\";\n\nexport function legacy(): number {\n  return core.helper(9);\n}\n"}},
			{"route_decorator", map[string]string{
				"src/core.ts": strings.Replace(tsCore, "export class Store {", "@logged\nexport class Store {", 1)}},
			// Single-file edit: adds a route + handler to server.ts. The amend
			// must re-emit server.ts's HANDLES_ROUTE/MIDDLEWARE_ON edges AND
			// re-derive the inbound API_CALL edges from client.ts (whose edges
			// die because they target server.ts's File anchor).
			{"add_route", map[string]string{
				"src/server.ts": strings.Replace(tsServer,
					"app.get(\"/api/items\", listItems);",
					"app.get(\"/api/items\", listItems);\napp.post(\"/api/users\", createUser);\n\nfunction createUser(req: any, res: any): void { res.json({}); }", 1)}},
			// Amending the middleware's home file kills the MIDDLEWARE_ON edge's
			// SOURCE node while the edge's owner (server.ts) is untouched — the
			// scopeNodes re-derivation must re-emit it or it is silently lost.
			{"modify_middleware", map[string]string{
				"src/mw.ts": strings.Replace(tsMw, "next();", "res.setHeader('x-auth', '1'); next();", 1)}},
		},
	}

	javaCore := "package core;\n\npublic class Core {\n    public static int helper(int x) {\n        return x + 1;\n    }\n\n    public static int compute(int a, int b) {\n        int y = helper(a);\n        return y + b;\n    }\n\n    public static int toDelete(int z) {\n        return z * 2;\n    }\n}\n"
	javaStore := "package core;\n\npublic class Store {\n    private int items;\n\n    public int add(int item) {\n        items += item;\n        return items;\n    }\n}\n"
	javaLogged := "package core;\n\npublic @interface Logged {\n}\n"
	javaApp := "package app;\n\nimport core.Core;\nimport core.Store;\n\npublic class App {\n    public static int main2() {\n        Store s = new Store();\n        s.add(1);\n        return Core.compute(1, 2) + Core.toDelete(3);\n    }\n\n    public static int other() {\n        lateBound();\n        return Core.helper(5);\n    }\n}\n"
	javaGone := "package gone;\n\nimport core.Core;\n\npublic class Legacy {\n    public static int legacy() {\n        return Core.helper(9);\n    }\n}\n"
	java := convergenceFixture{
		lang: "java", comment: "//",
		files: map[string]string{
			"core/Core.java": javaCore, "core/Store.java": javaStore, "core/Logged.java": javaLogged,
			"app/App.java": javaApp, "gone/Legacy.java": javaGone,
			"app/AppTest.java": "package app;\n\npublic class AppTest {\n    public static void testMain() {\n        if (App.main2() != 10) {\n            throw new AssertionError(\"main\");\n        }\n    }\n}\n",
		},
		edits: []convergenceEdit{
			{"rename_callee", map[string]string{"core/Core.java": strings.NewReplacer(
				"public static int helper(int x) {\n        return x + 1;\n    }",
				"public static int helperV2(int x, int scale) {\n        return x * scale;\n    }",
				"int y = helper(a);", "int y = helperV2(a, 1);").Replace(javaCore)}},
			{"move_call", map[string]string{
				"app/App.java":     strings.Replace(javaApp, "return Core.compute(1, 2) + Core.toDelete(3);", "return Core.toDelete(3);", 1),
				"gone/Legacy.java": strings.Replace(javaGone, "return Core.helper(9);", "return Core.helper(9) + Core.compute(1, 2);", 1)}},
			{"delete_function", map[string]string{"core/Core.java": strings.Replace(javaCore, "\n    public static int toDelete(int z) {\n        return z * 2;\n    }\n", "", 1)}},
			{"add_file", map[string]string{"app/Extra.java": "package app;\n\nimport core.Core;\n\npublic class Extra {\n    public static int lateBound() {\n        return Core.compute(0, 0);\n    }\n}\n"}},
			{"delete_file", map[string]string{"gone/Legacy.java": ""}},
			{"rename_across", map[string]string{
				"core/Core.java": strings.Replace(javaCore, "public static int compute(", "public static int computeTotal(", 1),
				"app/App.java":   strings.Replace(javaApp, "Core.compute(1, 2)", "Core.computeTotal(1, 2)", 1)}},
			{"modify_callee_body", map[string]string{
				"core/Core.java": strings.Replace(javaCore, "return x + 1;", "return (x + 1) * 2;", 1)}},
			{"change_import", map[string]string{
				"gone/Legacy.java": strings.NewReplacer(
					"import core.Core;", "import core.Core;\nimport core.Store;",
					"return Core.helper(9);", "return Core.helper(9) + new Store().add(1);").Replace(javaGone)}},
			{"route_decorator", map[string]string{
				"app/App.java": strings.NewReplacer(
					"import core.Store;", "import core.Store;\nimport core.Logged;",
					"public class App {", "@Logged\npublic class App {").Replace(javaApp)}},
		},
	}
	return []convergenceFixture{py, gof, ts, java}
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
