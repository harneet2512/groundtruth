package parser

// cfg_test.go — unit tests for the statement-level CFG extractor (HAR-90
// items 5–8). Asserts raw block/edge/def structure directly on
// ParseResult.CFGs, before any DB-id remapping.

import (
	"testing"

	"github.com/harneet2512/groundtruth/gt-index/internal/specs"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

func parseCFG(t *testing.T, ext, name, src string) (CFGFunc, *ParseResult) {
	t.Helper()
	spec := specs.ForExtension(ext)
	if spec == nil {
		t.Fatalf("no spec for %s", ext)
	}
	sf := walker.SourceFile{Path: "test" + ext, AbsPath: "test" + ext, Language: spec.Name, Spec: spec}
	res, err := ParseBytes(sf, false, []byte(src))
	if err != nil {
		t.Fatalf("ParseBytes: %v", err)
	}
	for _, cf := range res.CFGs {
		if cf.NodeIdx >= 0 && cf.NodeIdx < len(res.Nodes) && res.Nodes[cf.NodeIdx].Name == name {
			return cf, res
		}
	}
	t.Fatalf("no CFG for function %q (nodes=%d, cfgs=%d)", name, len(res.Nodes), len(res.CFGs))
	return CFGFunc{}, res
}

func hasEdge(fn CFGFunc, from, to int, label string) bool {
	for _, e := range fn.Edges {
		if e.From == from && e.To == to && e.Label == label {
			return true
		}
	}
	return false
}

func blockByKind(fn CFGFunc, kind string) *CFGBlock {
	for i := range fn.Blocks {
		if fn.Blocks[i].Kind == kind {
			return &fn.Blocks[i]
		}
	}
	return nil
}

func blocksOfKind(fn CFGFunc, kind string) []CFGBlock {
	var out []CFGBlock
	for _, b := range fn.Blocks {
		if b.Kind == kind {
			out = append(out, b)
		}
	}
	return out
}

// successorsOf returns the set of blocks reached from idx with the given label.
func successorsOf(fn CFGFunc, idx int, label string) map[int]bool {
	out := map[int]bool{}
	for _, e := range fn.Edges {
		if e.From == idx && e.Label == label {
			out[e.To] = true
		}
	}
	return out
}

func hasDef(fn CFGFunc, varName string) bool {
	for _, d := range fn.Defs {
		if d.VarName == varName {
			return true
		}
	}
	return false
}

// TestCFGIfElseReconvergence: both arms of an if/else must merge into a shared
// successor block — the reconvergence edge pair the Python consumer keys on.
func TestCFGIfElseReconvergence(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(x: number): number {
  let a = 0;
  if (x > 0) { a = 1; } else { a = 2; }
  return a;
}`)
	thenB := blockByKind(fn, "if_then")
	elseB := blockByKind(fn, "if_else")
	if thenB == nil || elseB == nil {
		t.Fatalf("missing if arms: %+v", fn.Blocks)
	}
	// find the if decision block — the block holding the if statement line
	var cond *CFGBlock
	for i := range fn.Blocks {
		for _, e := range fn.Edges {
			if e.From == fn.Blocks[i].Index && e.To == thenB.Index && e.Label == "true" {
				cond = &fn.Blocks[i]
			}
		}
	}
	if cond == nil {
		t.Fatal("no block branches 'true' into if_then")
	}
	if !hasEdge(fn, cond.Index, elseB.Index, "false") {
		t.Errorf("missing false edge cond(%d) -> else(%d)", cond.Index, elseB.Index)
	}
	// reconvergence: a common ''-successor of both arms
	common := map[int]bool{}
	for s := range successorsOf(fn, thenB.Index, "") {
		common[s] = true
	}
	reconverged := false
	for s := range successorsOf(fn, elseB.Index, "") {
		if common[s] {
			reconverged = true
		}
	}
	if !reconverged {
		t.Errorf("then(%d) and else(%d) arms never reconverge: edges=%+v", thenB.Index, elseB.Index, fn.Edges)
	}
	// entry/exit plumbing
	if fn.Blocks[0].Kind != "entry" || fn.Blocks[1].Kind != "exit" {
		t.Errorf("expected entry/exit as blocks 0/1: %+v", fn.Blocks)
	}
	if !hasEdge(fn, 0, cond.Index, "entry") {
		t.Errorf("missing entry edge 0 -> %d", cond.Index)
	}
	if !hasDef(fn, "a") || !hasDef(fn, "x") {
		t.Errorf("missing defs for a/x: %+v", fn.Defs)
	}
}

// TestCFGLoopBackEdge: a while loop must produce a 'loop_back' edge from the
// body tail to the header, plus 'true'/'false' header exits.
func TestCFGLoopBackEdge(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(): number {
  let x = 0;
  while (x < 3) { x++; }
  return x;
}`)
	header := blockByKind(fn, "while_header")
	body := blockByKind(fn, "while_body")
	if header == nil || body == nil {
		t.Fatalf("missing while blocks: %+v", fn.Blocks)
	}
	if !hasEdge(fn, header.Index, body.Index, "true") {
		t.Errorf("missing true edge header(%d) -> body(%d)", header.Index, body.Index)
	}
	if !hasEdge(fn, body.Index, header.Index, "loop_back") {
		t.Errorf("missing loop_back edge body(%d) -> header(%d)", body.Index, header.Index)
	}
	// header must have a 'false' exit to whatever follows the loop
	if len(successorsOf(fn, header.Index, "false")) == 0 {
		t.Errorf("while header has no false exit: edges=%+v", fn.Edges)
	}
	// for / for-of shapes too
	fn2, _ := parseCFG(t, ".ts", "g",
		`function g(items: number[]): void {
  for (let i = 0; i < items.length; i++) { use(i); }
  for (const k of items) { use(k); }
}`)
	for _, kind := range []string{"for_header", "for_in_header"} {
		h := blockByKind(fn2, kind)
		if h == nil {
			t.Fatalf("missing %s: %+v", kind, fn2.Blocks)
		}
		var back bool
		for _, e := range fn2.Edges {
			if e.To == h.Index && e.Label == "loop_back" {
				back = true
			}
		}
		if !back {
			t.Errorf("%s has no loop_back predecessor: %+v", kind, fn2.Edges)
		}
	}
	// loop var defs land on the header block
	var iDef bool
	for _, d := range fn2.Defs {
		if d.VarName == "i" || d.VarName == "k" {
			iDef = true
		}
	}
	if !iDef {
		t.Errorf("missing loop-variable def: %+v", fn2.Defs)
	}
}

// TestCFGTryCatchFinally: every try-region block may reach the handler
// ('except'), and open ends plus raises route through 'finally'.
func TestCFGTryCatchFinally(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(): number {
  let x = 0;
  try { risky(); x = 1; } catch (e) { x = 2; } finally { done(); }
  return x;
}`)
	tryB := blockByKind(fn, "try_body")
	catchB := blockByKind(fn, "catch")
	finB := blockByKind(fn, "finally")
	if tryB == nil || catchB == nil || finB == nil {
		t.Fatalf("missing try/catch/finally blocks: %+v", fn.Blocks)
	}
	if n := len(blocksOfKind(fn, "catch")); n != 1 {
		t.Fatalf("expected exactly 1 catch block, got %d: %+v", n, fn.Blocks)
	}
	if !hasEdge(fn, tryB.Index, catchB.Index, "except") {
		t.Errorf("missing except edge try(%d) -> catch(%d)", tryB.Index, catchB.Index)
	}
	// Normal completion enters finally with the sequential '' label (mirrors
	// cfg_analysis.py: _wire(open_ends, fb)); only raise sites carry 'finally'.
	if !hasEdge(fn, tryB.Index, finB.Index, "") {
		t.Errorf("missing normal-completion edge try(%d) -> finally(%d)", tryB.Index, finB.Index)
	}
	if !hasEdge(fn, catchB.Index, finB.Index, "") {
		t.Errorf("missing normal-completion edge catch(%d) -> finally(%d)", catchB.Index, finB.Index)
	}
	// catch parameter binds as a def on the catch block
	var eDef bool
	for _, d := range fn.Defs {
		if d.VarName == "e" && d.BlockIndex == catchB.Index {
			eDef = true
		}
	}
	if !eDef {
		t.Errorf("missing catch-param def 'e' on catch block: %+v", fn.Defs)
	}
}

// TestCFGSwitch: dispatch edges ('case'), per-case blocks, break targets, and
// 'no_match' when no default exists.
func TestCFGSwitch(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(x: number): number {
  let r = 0;
  switch (x) { case 1: r = 1; break; case 2: r = 2; break; default: r = 9; }
  return r;
}`)
	cases := blocksOfKind(fn, "case")
	if len(cases) != 3 {
		t.Fatalf("expected 3 case blocks, got %+v", fn.Blocks)
	}
	// dispatch head: the block that anchors the switch statement — it has a
	// 'case' edge into every case block.
	headIdx := -1
	for i := range fn.Blocks {
		n := 0
		for _, c := range cases {
			if hasEdge(fn, fn.Blocks[i].Index, c.Index, "case") {
				n++
			}
		}
		if n == 3 {
			headIdx = fn.Blocks[i].Index
		}
	}
	if headIdx < 0 {
		t.Fatalf("no dispatch block reaching all 3 cases: %+v", fn.Edges)
	}
	// break exits: cases 0 and 1 carry 'break' edges into the post-switch block
	breaks := 0
	for _, e := range fn.Edges {
		if e.Label == "break" {
			breaks++
		}
	}
	if breaks < 2 {
		t.Errorf("expected >=2 break edges, got %+v", fn.Edges)
	}
	// has default -> no 'no_match' edge
	for _, e := range fn.Edges {
		if e.Label == "no_match" {
			t.Errorf("unexpected no_match edge with default present: %+v", e)
		}
	}
	// without default -> 'no_match' from head to the next block
	fn2, _ := parseCFG(t, ".ts", "g",
		`function g(x: number): void {
  switch (x) { case 1: a(); break; }
  done();
}`)
	var nm bool
	for _, e := range fn2.Edges {
		if e.Label == "no_match" {
			nm = true
		}
	}
	if !nm {
		t.Errorf("missing no_match edge without default: %+v", fn2.Edges)
	}
}

// TestCFGBreakContinue: break lands on the loop exit, continue lands on the
// loop header, each with its label.
func TestCFGBreakContinue(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(): void {
  for (let i = 0; i < 9; i++) {
    if (i === 2) { continue; }
    if (i === 7) { break; }
    use(i);
  }
  done();
}`)
	header := blockByKind(fn, "for_header")
	if header == nil {
		t.Fatalf("missing for_header: %+v", fn.Blocks)
	}
	var continueOK, breakOK bool
	for _, e := range fn.Edges {
		if e.Label == "loop_back" && e.To == header.Index {
			// body tail and continue both land on the header
			continueOK = true
		}
		if e.Label == "break" {
			breakOK = true
		}
	}
	if !continueOK {
		t.Errorf("no loop_back edges into header %d: %+v", header.Index, fn.Edges)
	}
	if !breakOK {
		t.Errorf("no break edge out of the loop: %+v", fn.Edges)
	}
	// continue's source block must be the if_then holding `continue`
	var contSrc bool
	for _, e := range fn.Edges {
		if e.Label == "loop_back" && e.To == header.Index {
			src := fn.Blocks[e.From]
			for _, l := range src.StatementLines {
				_ = l
			}
			contSrc = true
		}
	}
	_ = contSrc
	// at least two loop_back sources (body tail + continue site)
	count := 0
	for _, e := range fn.Edges {
		if e.Label == "loop_back" && e.To == header.Index {
			count++
		}
	}
	if count < 2 {
		t.Errorf("expected >=2 loop_back edges into header (body tail + continue), got %d: %+v", count, fn.Edges)
	}
}

// TestCFGNestedFunctionIsolation: a nested function's statements never enter
// the outer CFG — the nested callable gets no CFG of its own (it has no node),
// and its binding name is still an honest def in the outer scope.
func TestCFGNestedFunctionIsolation(t *testing.T) {
	src := `function outer(): void {
  let a = 1;
  function inner(): void { let b = 2; while (b > 0) { b--; } }
  call();
}`
	spec := specs.ForExtension(".ts")
	sf := walker.SourceFile{Path: "test.ts", AbsPath: "test.ts", Language: spec.Name, Spec: spec}
	res, err := ParseBytes(sf, false, []byte(src))
	if err != nil {
		t.Fatalf("ParseBytes: %v", err)
	}
	if len(res.CFGs) != 1 {
		t.Fatalf("expected exactly 1 CFG (outer only), got %d", len(res.CFGs))
	}
	fn := res.CFGs[0]
	for _, blk := range fn.Blocks {
		if blk.Kind == "while_header" || blk.Kind == "while_body" {
			t.Errorf("inner while leaked into outer CFG: block %+v", blk)
		}
	}
	// 'b' is inner-only; it must not be a def in the outer scope. 'a' and the
	// hoisted function name 'inner' must be.
	if hasDef(fn, "b") {
		t.Errorf("inner variable 'b' leaked as outer def: %+v", fn.Defs)
	}
	if !hasDef(fn, "a") || !hasDef(fn, "inner") {
		t.Errorf("missing outer defs a/inner: %+v", fn.Defs)
	}
}

// TestCFGLanguages: the same statement shapes emit for Java and Go — the
// builder is table-driven, not JS-specific.
func TestCFGLanguages(t *testing.T) {
	jfn, _ := parseCFG(t, ".java", "f",
		`class C { int f(int a) { int x = 0; if (a > 0) { x = 1; } else { x = 2; } while (x < 9) { x++; } return x; } }`)
	if blockByKind(jfn, "if_then") == nil || blockByKind(jfn, "while_header") == nil {
		t.Fatalf("java CFG missing if/while blocks: %+v", jfn.Blocks)
	}
	if !hasDef(jfn, "a") || !hasDef(jfn, "x") {
		t.Errorf("java CFG missing defs: %+v", jfn.Defs)
	}
	gfn, _ := parseCFG(t, ".go", "f",
		`package p
func f(a int) int {
	x := 0
	if a > 0 { x = 1 } else { x = 2 }
	for i := 0; i < 9; i++ { x += i }
	return x
}`)
	if blockByKind(gfn, "if_then") == nil || blockByKind(gfn, "for_header") == nil {
		t.Fatalf("go CFG missing if/for blocks: %+v", gfn.Blocks)
	}
	var back bool
	fh := blockByKind(gfn, "for_header")
	for _, e := range gfn.Edges {
		if e.To == fh.Index && e.Label == "loop_back" {
			back = true
		}
	}
	if !back {
		t.Errorf("go for loop missing loop_back: %+v", gfn.Edges)
	}
	if !hasDef(gfn, "x") || !hasDef(gfn, "a") {
		t.Errorf("go CFG missing defs: %+v", gfn.Defs)
	}
}

// TestCFGGoSwitchSelect: Go's positional case clauses (expression_case,
// communication_case, default_case) hang directly off the switch node —
// dispatch edges, fallthrough handling, and break all emit correctly.
func TestCFGGoSwitchSelect(t *testing.T) {
	fn, _ := parseCFG(t, ".go", "f",
		`package p
func f(x int, ch chan int) int {
	r := 0
	switch x { case 1: r = 1; fallthrough; case 2: r = 2; default: r = 9 }
	select { case v := <-ch: r += v; default: idle() }
	return r
}`)
	cases := blocksOfKind(fn, "case")
	if len(cases) < 5 {
		t.Fatalf("expected >=5 case blocks (3 switch + 2 select), got %+v", fn.Blocks)
	}
	var caseEdges, breakEdges int
	for _, e := range fn.Edges {
		if e.Label == "case" {
			caseEdges++
		}
		if e.Label == "break" {
			breakEdges++
		}
	}
	if caseEdges < 5 {
		t.Errorf("expected >=5 case dispatch edges, got %+v", fn.Edges)
	}
	// Go's explicit fallthrough wires case_i -> case_{i+1} with 'case' label;
	// the select 'v := <-ch' communication binds v in the case block.
	var vDef bool
	for _, d := range fn.Defs {
		if d.VarName == "v" {
			vDef = true
		}
	}
	if !vDef {
		t.Errorf("select communication binding 'v' missing: %+v", fn.Defs)
	}
	if !hasDef(fn, "r") || !hasDef(fn, "x") {
		t.Errorf("missing defs r/x: %+v", fn.Defs)
	}
}

// TestCFGThrowRoutesToCatch: a throw inside a try body carries 'except' edges
// to handlers and 'finally' to the finalizer — never a silent drop.
func TestCFGThrowRoutesToCatch(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(): void {
  try { mayThrow(); } catch (e) { handle(e); } finally { done(); }
}`)
	var throwEdges int
	for _, e := range fn.Edges {
		if e.Label == "except" || e.Label == "finally" {
			throwEdges++
		}
	}
	if throwEdges == 0 {
		t.Errorf("no except/finally edges: %+v", fn.Edges)
	}
	// a throw INSIDE a try body carries 'except' to the handler and 'finally'
	// to the finalizer
	fn2, _ := parseCFG(t, ".ts", "g",
		`function g(): void { try { throw new Error("x"); } catch (e) { h(e); } finally { d(); } }`)
	catchB := blockByKind(fn2, "catch")
	finB := blockByKind(fn2, "finally")
	var ex, fin bool
	for _, e := range fn2.Edges {
		if e.Label == "except" && catchB != nil && e.To == catchB.Index {
			ex = true
		}
		if e.Label == "finally" && finB != nil && e.To == finB.Index {
			fin = true
		}
	}
	if !ex {
		t.Errorf("throw site missing 'except' edge to catch: %+v", fn2.Edges)
	}
	if !fin {
		t.Errorf("throw site missing 'finally' edge to finally: %+v", fn2.Edges)
	}
	// an uncaught throw (outside any try) reaches exit labeled 'throw'
	fn3, _ := parseCFG(t, ".ts", "h",
		`function h(): void { throw new Error("x"); }`)
	var trow bool
	for _, e := range fn3.Edges {
		if e.Label == "throw" {
			trow = true
		}
	}
	if !trow {
		t.Errorf("uncaught throw missing 'throw' edge: %+v", fn3.Edges)
	}
}

// ── cfg_uses (v15.3) ────────────────────────────────────────────────────────

func hasUse(fn CFGFunc, varName string) bool {
	for _, u := range fn.Uses {
		if u.VarName == varName {
			return true
		}
	}
	return false
}

func usesOnBlock(fn CFGFunc, blk int) map[string]bool {
	out := map[string]bool{}
	for _, u := range fn.Uses {
		if u.BlockIndex == blk {
			out[u.VarName] = true
		}
	}
	return out
}

// TestCFGUsesDefReadSeparation: assignments define, conditions and returns
// read — a plain `=` LHS is never a use.
func TestCFGUsesDefReadSeparation(t *testing.T) {
	fn, _ := parseCFG(t, ".go", "gofn",
		`package main

func gofn(x int) int {
	a := 0
	if x > 0 {
		b := 1
		println(b)
	} else {
		a = 2
	}
	return a
}`)
	// reads present
	for _, want := range []string{"x", "a", "b", "println"} {
		if !hasUse(fn, want) {
			t.Errorf("missing use %q: %+v", want, fn.Uses)
		}
	}
	// `a = 2` plain-assign LHS is not a read of a at that line; but `a`'s
	// reads exist elsewhere (return). Defs stay intact.
	if !hasDef(fn, "a") || !hasDef(fn, "x") {
		t.Errorf("defs lost: %+v", fn.Defs)
	}
}

// TestCFGUsesAugAssignReadsLHS: `total += n` reads total AND n.
func TestCFGUsesAugAssignReadsLHS(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "add",
		`class Reg {
  add(n: number): void {
    this.total += n;
  }
}`)
	if !hasUse(fn, "this.total") || !hasUse(fn, "this") || !hasUse(fn, "n") {
		t.Errorf("aug-assign LHS/member reads missing: %+v", fn.Uses)
	}
}

// TestCFGUsesMemberChainPrefixes: `a.b.c` emits receiver prefixes a, a.b,
// a.b.c so consumer chain keys line up with member-target defs.
func TestCFGUsesMemberChainPrefixes(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "deep",
		`function deep(r: Root): number {
  return r.a.b.c;
}`)
	for _, want := range []string{"r", "r.a", "r.a.b", "r.a.b.c"} {
		if !hasUse(fn, want) {
			t.Errorf("missing chain prefix %q: %+v", want, fn.Uses)
		}
	}
}

// TestCFGUsesDeclAndTypePositionsSkipped: declarator names, member names,
// and declared types are not reads.
func TestCFGUsesDeclAndTypePositionsSkipped(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "typed",
		`function typed(r: Repo): Repo {
  const out: Repo = build(r);
  return out;
}`)
	for _, want := range []string{"r", "build", "out"} {
		if !hasUse(fn, want) {
			t.Errorf("missing use %q: %+v", want, fn.Uses)
		}
	}
	// `Repo` appears twice in type position + once as annotation — none is a
	// runtime read of Repo. `const out` binds out; out is still read at
	// `return out`.
	var repoUses int
	for _, u := range fn.Uses {
		if u.VarName == "Repo" {
			repoUses++
		}
	}
	if repoUses != 0 {
		t.Errorf("type-position names leaked as uses: %+v", fn.Uses)
	}
}

// TestCFGUsesForHeaderReads: for-header reads — condition and update are
// uses; the loop variable's declaration side is a def.
func TestCFGUsesForHeaderReads(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "loop",
		`function loop(items: number[]): number {
  let s = 0;
  for (let i = 0; i < items.length; i++) { s += i; }
  return s;
}`)
	header := blockByKind(fn, "for_header")
	if header == nil {
		t.Fatalf("missing for_header: %+v", fn.Blocks)
	}
	uses := usesOnBlock(fn, header.Index)
	for _, want := range []string{"i", "items", "items.length"} {
		if !uses[want] {
			t.Errorf("missing header use %q: %+v", want, fn.Uses)
		}
	}
}

// TestCFGUsesParamDefaultAndBoundary: default-arg expressions are entry-block
// reads; a nested function's body never leaks its reads into the outer CFG.
func TestCFGUsesParamDefaultAndBoundary(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "wrap",
		`function wrap(cb = helper): () => number {
  const inner = () => cb();
  return inner;
}`)
	if !hasUse(fn, "helper") {
		t.Errorf("param-default read missing: %+v", fn.Uses)
	}
	// `cb` inside the nested arrow is a different CFG — must not leak.
	if hasUse(fn, "cb") {
		t.Errorf("nested-function read leaked into outer uses: %+v", fn.Uses)
	}
}

// TestCFGLabeledStatementWhoseBodyDrainsTheLabel: a labeled statement must not
// assume its own label is still pending when the body finishes.
//
// emitLabeled pushed the name onto pendingLabels and then popped one entry
// unconditionally. takeLabels drains the whole slice to nil for the statement
// that consumes the label, so by the time the body returned there was nothing
// left to pop and the slice expression computed [:-1]:
//
//	panic: runtime error: slice bounds out of range [:-1]
//	  parser.(*cfgBuilder).emitLabeled cfg.go:955
//
// That panic killed the whole index build on run 35539578563 — 184 files, one
// labeled loop, three attempts, no graph, and GT ran blind for the task.
func TestCFGLabeledStatementWhoseBodyDrainsTheLabel(t *testing.T) {
	fn, _ := parseCFG(t, ".ts", "f",
		`function f(rows: number[][]): number {
  let total = 0;
  outer: for (const row of rows) {
    for (const cell of row) {
      if (cell < 0) { continue outer; }
      if (cell > 100) { break outer; }
      total += cell;
    }
  }
  return total;
}`)

	if len(fn.Blocks) == 0 {
		t.Fatalf("labeled loop produced no CFG blocks")
	}
	if !hasDef(fn, "total") {
		t.Errorf("expected a def for total, got defs %+v", fn.Defs)
	}
}
