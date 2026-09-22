package main

import (
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/mattn/go-sqlite3"
)

// W1 (HAR-90 canonical) — producer call-resolution correctness, asserted on the
// CALLS edges a REAL gt-index build publishes over a temp fixture repo. Every
// fixture is parsed by tree-sitter and resolved by the production ladder; no
// hand-inserted rows.

type w1Edge struct {
	Src, SrcFile, Tgt, TgtFile, Method, Tier, Evidence string
	Conf                                               float64
}

func (e w1Edge) String() string {
	return fmt.Sprintf("%s(%s) -> %s(%s) [%s/%s %.2f %s]", e.Src, e.SrcFile, e.Tgt, e.TgtFile, e.Method, e.Evidence, e.Conf, e.Tier)
}

// w1IndexCalls writes files into a temp repo, runs the real gt-index binary,
// and returns every published CALLS edge keyed by qualified names.
func w1IndexCalls(t *testing.T, files map[string]string) []w1Edge {
	t.Helper()
	if testing.Short() {
		t.Skip("builds the gt-index binary; skipped under -short")
	}
	repo := filepath.Join(t.TempDir(), "repo")
	for name, body := range files {
		p := filepath.Join(repo, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	bin := buildTwoPhaseIndexBinary(t)
	dbPath := filepath.Join(t.TempDir(), "graph.db")
	index := exec.Command(bin, "-root", repo, "-output", dbPath, "-workers", "2")
	if out, err := index.CombinedOutput(); err != nil {
		t.Fatalf("index fixture: %v\n%s", err, out)
	}
	db, err := sql.Open("sqlite3", dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	rows, err := db.Query(`SELECT s.qualified_name, s.file_path, t.qualified_name, t.file_path,
		COALESCE(e.resolution_method,''), COALESCE(e.confidence,0), COALESCE(e.trust_tier,''), COALESCE(e.evidence_type,'')
		FROM edges e JOIN nodes s ON s.id=e.source_id JOIN nodes t ON t.id=e.target_id
		WHERE e.type='CALLS' ORDER BY s.qualified_name, t.qualified_name`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	var out []w1Edge
	for rows.Next() {
		var e w1Edge
		if err := rows.Scan(&e.Src, &e.SrcFile, &e.Tgt, &e.TgtFile, &e.Method, &e.Conf, &e.Tier, &e.Evidence); err != nil {
			t.Fatal(err)
		}
		out = append(out, e)
	}
	return out
}

func w1Find(edges []w1Edge, src, tgt, tgtFile string) *w1Edge {
	for i := range edges {
		e := edges[i]
		if e.Src == src && e.Tgt == tgt && (tgtFile == "" || e.TgtFile == tgtFile) {
			return &edges[i]
		}
	}
	return nil
}

func w1Dump(edges []w1Edge) string {
	var b strings.Builder
	for _, e := range edges {
		b.WriteString("\n  ")
		b.WriteString(e.String())
	}
	return b.String()
}

// Defect 1: a qualified call whose receiver is an external module or an
// untyped value must never become a CERTIFIED same_file edge to an unrelated
// same-file method that merely shares the leaf name.
func TestW1QualifiedCallOnUnprovenReceiverIsNeverCertifiedSameFile(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"pkg/a.py": `import requests
import subprocess


class Session:
    def get(self, url):
        return url

    def run(self):
        return 1

    def refresh(self):
        return self.get("r")


def fetch():
    return requests.get("x")


def use(sess):
    return sess.get("y")


def shell(cmd):
    subprocess.run(cmd)


def direct():
    return Session.run(None)


def helper():
    return 1


def bare():
    return helper()
`,
	})
	for _, bad := range []struct{ src, tgt string }{
		{"fetch", "Session.get"},
		{"use", "Session.get"},
		{"shell", "Session.run"},
	} {
		if e := w1Find(edges, bad.src, bad.tgt, ""); e != nil && (e.Tier == "CERTIFIED" || e.Method == "same_file") {
			t.Errorf("%s -> %s published as %s; a leaf-name match on an unproven receiver is not a fact%s", bad.src, bad.tgt, e, w1Dump(edges))
		}
	}
	// An imported EXTERNAL module receiver (subprocess.run) proves the call
	// leaves the repository: at most the sub-0.5 SPECULATIVE name hint, never
	// a CANDIDATE-tier implementor guess.
	if e := w1Find(edges, "shell", "Session.run", ""); e != nil && (e.Conf >= 0.5 || e.Tier != "SPECULATIVE") {
		t.Errorf("subprocess.run resolved into the repo above SPECULATIVE: %s", e)
	}
	// Receiver-proven and bare calls keep their certified resolution.
	for _, good := range []struct{ src, tgt string }{
		{"bare", "helper"},
		{"Session.refresh", "Session.get"},
		{"direct", "Session.run"},
	} {
		e := w1Find(edges, good.src, good.tgt, "")
		if e == nil || e.Tier != "CERTIFIED" {
			t.Errorf("%s -> %s = %v, want a CERTIFIED edge%s", good.src, good.tgt, e, w1Dump(edges))
		}
	}
}

// Defect 2: receiver-proving rungs (assignment flow) must run before the
// receiver-unproven impl_method guess. Q overrides go; q = Q(); q.go() is Q.go.
func TestW1AssignmentFlowBeatsImplMethodGuess(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"m.py": `class P:
    def go(self):
        return 1


class Q(P):
    def go(self):
        return 2


class Store:
    def save(self):
        return 3


def main():
    q = Q()
    q.go()


def persist():
    s = Store()
    s.save()
`,
	})
	if e := w1Find(edges, "main", "P.go", ""); e != nil {
		t.Errorf("q = Q(); q.go() resolved to the overridden P.go: %s%s", e, w1Dump(edges))
	}
	if e := w1Find(edges, "main", "Q.go", ""); e == nil || e.Method != "type_flow" {
		t.Errorf("main -> Q.go = %v, want type_flow%s", e, w1Dump(edges))
	}
	if e := w1Find(edges, "persist", "Store.save", ""); e == nil || e.Method != "type_flow" {
		t.Errorf("persist -> Store.save = %v, want type_flow (assignment-proven receiver)%s", e, w1Dump(edges))
	}
}

// Defect 3 + 4 (Java): receiver calls keep the method name, and a declared
// formal parameter type proves the receiver.
func TestW1JavaReceiverCallsResolveToTheMethod(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"p/A.java": `package p;

class Base {
    void run() {}
}

class Other {
    void run() {}
}

class Helper {
    static void parse() {}
}

class A {
    void m(Base x) {
        x.run();
        Helper.parse();
        this.h();
    }

    void h() {}
}
`,
	})
	for _, want := range []struct{ tgt, method string }{
		{"Base.run", "type_flow"},
		{"Helper.parse", ""},
		{"A.h", ""},
	} {
		e := w1Find(edges, "A.m", want.tgt, "")
		if e == nil || e.Tier != "CERTIFIED" || (want.method != "" && e.Method != want.method) {
			t.Errorf("A.m -> %s = %v, want CERTIFIED %s%s", want.tgt, e, want.method, w1Dump(edges))
		}
	}
	if e := w1Find(edges, "A.m", "Other.run", ""); e != nil {
		t.Errorf("declared Base x; x.run() also reached Other.run: %s", e)
	}
}

// Defect 4 (Python / TypeScript): required annotated parameters type the
// receiver through the declared-parameter rung (evidence param_type). Each
// language is indexed alone so a cross-language class count cannot mask the
// receiver-unproven impl_method guess.
func TestW1DeclaredParameterTypesResolveReceivers(t *testing.T) {
	fixtures := map[string]string{
		"py/u.py": `class P:
    def go(self):
        return 1


class R:
    def go(self):
        return 2


def use(p: P):
    return p.go()
`,
		"ts/u.ts": `class P {
  go(): number { return 1; }
}

class R {
  go(): number { return 2; }
}

function use(p: P): number {
  return p.go();
}
`,
	}
	for file, body := range fixtures {
		edges := w1IndexCalls(t, map[string]string{file: body})
		got := w1Find(edges, "use", "P.go", file)
		if got == nil || got.Method != "type_flow" || got.Evidence != "param_type" {
			t.Errorf("%s: use -> P.go = %v, want type_flow/param_type (declared param)%s", file, got, w1Dump(edges))
		}
		if bad := w1Find(edges, "use", "R.go", ""); bad != nil {
			t.Errorf("%s: use(p: P) reached R.go: %s", file, bad)
		}
	}
}

// Defect 5: two call sites to the same target — a receiver-unproven guess
// first, a type-proven call second — must publish the STRONGEST resolution.
func TestW1EndpointDedupeKeepsStrongestResolution(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"d.py": `class Store:
    def save(self):
        return 1


class Other:
    def save(self):
        return 2


def main(x):
    x.save()
    s = Store()
    s.save()
`,
	})
	e := w1Find(edges, "main", "Store.save", "")
	if e == nil || e.Method != "type_flow" || e.Tier != "CERTIFIED" {
		t.Errorf("main -> Store.save = %v, want the type_flow CERTIFIED resolution of the second call site%s", e, w1Dump(edges))
	}
}

// Defect 6: callable-value alias chains resolve through the variable chain and
// never jump to a same-named function in another language.
func TestW1CallableAliasChainAndLanguageFilter(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"a.py": `def helper():
    return 1


def main():
    f = helper
    g = f
    g()


def loop():
    x = y
    y = x
    x()


def cross():
    return only_in_js()
`,
		"b.js": `function f() { return 2; }
function g() { return 3; }
function only_in_js() { return 4; }
`,
	})
	if e := w1Find(edges, "main", "helper", "a.py"); e == nil {
		t.Errorf("g = f; f = helper; g() did not resolve to helper%s", w1Dump(edges))
	}
	for _, e := range edges {
		if strings.HasSuffix(e.SrcFile, ".py") && strings.HasSuffix(e.TgtFile, ".js") {
			t.Errorf("Python call resolved into JavaScript: %s", e)
		}
	}
}

// Defect 7: module-level CommonJS require() binds the module for calls in the
// file's functions.
func TestW1ModuleLevelRequireBindsImports(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"js/h.js": `function helper() { return 1; }
module.exports = { helper };
`,
		"js/c.js": `function helper() { return 3; }
module.exports = { helper };
`,
		"js/b.js": `const h = require('./h');
const { helper } = require('./h');

function z() { return h.helper(); }
function y() { return helper(); }
`,
	})
	for _, src := range []string{"z", "y"} {
		e := w1Find(edges, src, "helper", "js/h.js")
		if e == nil || e.Method != "import" {
			t.Errorf("%s -> js/h.js::helper = %v, want import%s", src, e, w1Dump(edges))
		}
		if bad := w1Find(edges, src, "helper", "js/c.js"); bad != nil {
			t.Errorf("%s resolved to the unrequired js/c.js helper: %s", src, bad)
		}
	}
}

// Defect 3 regression guard: with Java receiver calls now extracted as
// (method, receiver.method), a SAM call on a callable VALUE (`r.run()` on a
// method reference, `cb.run()` on a Runnable formal) must still resolve
// through the value binding to the referenced method.
func TestW1JavaSAMCallOnCallableValueStillResolves(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"p/Items.java": `package p;

class Items {
    void handle() {
        Runnable r = this::work;
        r.run();
    }

    void register(Runnable cb) {
        cb.run();
    }

    void wire() {
        register(this::work);
    }

    void work() {}
}

class Job {
    void run() {}
}
`,
	})
	for _, src := range []string{"Items.handle", "Items.register"} {
		e := w1Find(edges, src, "Items.work", "")
		if e == nil || e.Method != "callable_value" {
			t.Errorf("%s -> Items.work = %v, want callable_value%s", src, e, w1Dump(edges))
		}
		if bad := w1Find(edges, src, "Job.run", ""); bad != nil {
			t.Errorf("%s: SAM call guessed the unrelated Job.run: %s", src, bad)
		}
	}
}

// Defect 1 companion: receivers that ARE proven — super() and cls — still
// certify once the same-file leaf-name shortcut no longer does it for them.
func TestW1SuperAndClsReceiversStayCertified(t *testing.T) {
	edges := w1IndexCalls(t, map[string]string{
		"s.py": `class Base:
    def persist(self):
        return 1

    @classmethod
    def make(cls):
        return cls.build()

    @classmethod
    def build(cls):
        return 2


class Child(Base):
    def save(self):
        return super().persist()
`,
	})
	for _, want := range []struct{ src, tgt string }{
		{"Child.save", "Base.persist"},
		{"Base.make", "Base.build"},
	} {
		e := w1Find(edges, want.src, want.tgt, "")
		if e == nil || e.Tier != "CERTIFIED" {
			t.Errorf("%s -> %s = %v, want CERTIFIED%s", want.src, want.tgt, e, w1Dump(edges))
		}
	}
}
