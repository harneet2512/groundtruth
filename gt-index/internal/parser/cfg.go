package parser

// cfg.go — statement-level per-function control-flow extraction (HAR-90 items
// 5–8). For every function/method node the parser emits, build the raw CFG:
// basic blocks over tree-sitter statement nodes, directed edges between them,
// and the per-block variable definitions a consumer needs to compose reaching
// definitions / use-def chains.
//
// Semantics mirror src/groundtruth/runtime/cfg_analysis.py (the Python
// substrate this feeds): same construction order, same may-approximations —
// every block inside a try region can reach each handler; abrupt exits are
// recorded as raw edges. Dominators, post-dominators and control dependence
// are deliberately NOT computed here — that composition stays with the
// consumer.
//
// Edge labels (closed vocabulary):
//
//	""           sequential fall-through; also case -> next case (fall-through)
//	"entry"      entry block -> first body block
//	"end"        last open ends -> exit block
//	"true"       condition holds: if -> then arm, loop header -> body
//	"false"      condition fails: if -> else arm or join, loop header -> after
//	"loop_back"  loop body tail -> header; continue -> header / do-while cond
//	"break"      break -> loop or switch exit
//	"case"       switch dispatch -> case block; Go fallthrough -> next case
//	"no_match"   switch without default -> after-switch
//	"except"     try-region block -> catch handler; throw -> handler
//	"finally"    try open ends / raises -> finally block
//	"throw"      uncaught throw -> exit
//	"return"     return -> exit
//	"goto"       go goto -> labeled block head
//
// Block kinds: entry, exit, block, if_then, if_else, for_header, for_body,
// for_in_header, for_in_body, while_header, while_body, do_body, do_cond,
// case, try_body, catch, finally.

import (
	"strings"

	sitter "github.com/smacker/go-tree-sitter"
	"github.com/harneet2512/groundtruth/gt-index/internal/walker"
)

// CFGBlock is one basic block of a function's statement-level CFG.
// StatementLines holds the 1-based start lines of the tree-sitter statement
// nodes anchored to this block, in source order.
type CFGBlock struct {
	Index          int
	Kind           string
	StartLine      int
	EndLine        int
	StatementLines []int
}

// CFGEdge is a directed block-level edge between two block indexes of the
// same function CFG.
type CFGEdge struct {
	From  int
	To    int
	Label string
}

// CFGDef records one variable definition anchored to a block: honest
// definitions only (assignments, declarations, updates, catch/loop bindings).
// No PHI nodes — joins are the consumer's problem.
type CFGDef struct {
	BlockIndex int
	VarName    string
	Line       int
}

// CFGUse records one variable READ anchored to a block: the parser-exact
// complement of CFGDef. Identifier loads plus member-access chains
// (``a.b.c`` emits the receiver prefixes ``a``, ``a.b``, ``a.b.c`` so the
// consumer's chain keys line up with member-target defs). Augmented-assignment
// LHS counts as a use (``x += y`` reads x); a plain ``=`` LHS does not.
// Type positions are skipped — a declared type is not a runtime read.
type CFGUse struct {
	BlockIndex int
	VarName    string
	Line       int
}

// CFGFunc is the complete CFG payload for one emitted function/method node.
// NodeIdx is the file-local index into ParseResult.Nodes — remapped to a
// database node id by the caller after nodes are inserted.
type CFGFunc struct {
	NodeIdx int
	Blocks  []CFGBlock
	Edges   []CFGEdge
	Defs    []CFGDef
	Uses    []CFGUse
}

// cfgLangSpec describes one language's statement-level node/field vocabulary.
type cfgLangSpec struct {
	blockTypes     map[string]bool // statement containers whose named children are statements
	elseClause     string          // wrapper whose single named child is the else body ("" = alternative is direct)
	ifStmt         string
	forStmt        string
	forIn          string // for-in/of/enhanced-for ("" if none)
	while          string
	do             string
	switchStmts    map[string]bool // all switch/select shapes
	tryStmts       map[string]bool
	catchClause    string
	finallyClause  string
	labeled        string
	breakStmt      string
	continueStmt   string
	returnStmts    map[string]bool
	throwStmts     map[string]bool
	gotoStmt       string // go only ("" elsewhere)
	fallthroughStmt string // go only ("" elsewhere)
	switchBodyKind string // node type holding case clauses ("" = cases are direct children of the switch node)

	// scope boundaries: nested callable/class declarations. Def collection and
	// statement emission never descend into them (a nested function's body
	// belongs to a different CFG even when it has no node of its own).
	boundary map[string]bool
	// switch case clause types
	caseTypes    map[string]bool
	defaultTypes map[string]bool
	// case clause types whose open ends fall through into the next case
	// (JS/Java statement groups; Go/Java-rules do not fall through implicitly)
	fallThroughCases map[string]bool
}

func cfgJSTSLang() *cfgLangSpec {
	return &cfgLangSpec{
		blockTypes:     map[string]bool{"statement_block": true},
		elseClause:     "else_clause",
		ifStmt:         "if_statement",
		forStmt:        "for_statement",
		forIn:          "for_in_statement",
		while:          "while_statement",
		do:             "do_statement",
		switchStmts:    map[string]bool{"switch_statement": true},
		tryStmts:       map[string]bool{"try_statement": true},
		catchClause:    "catch_clause",
		finallyClause:  "finally_clause",
		labeled:        "labeled_statement",
		breakStmt:      "break_statement",
		continueStmt:   "continue_statement",
		returnStmts:    map[string]bool{"return_statement": true},
		throwStmts:     map[string]bool{"throw_statement": true},
		switchBodyKind: "switch_body",
		boundary: map[string]bool{
			"function_declaration": true, "generator_function_declaration": true,
			"function_expression": true, "arrow_function": true,
			"method_definition": true, "generator_function": true,
			"class_declaration": true, "class": true,
		},
		caseTypes:        map[string]bool{"switch_case": true, "switch_default": true},
		defaultTypes:     map[string]bool{"switch_default": true},
		fallThroughCases: map[string]bool{"switch_case": true, "switch_default": true},
	}
}

func cfgJavaLang() *cfgLangSpec {
	return &cfgLangSpec{
		blockTypes:    map[string]bool{"block": true},
		ifStmt:        "if_statement",
		forStmt:       "for_statement",
		forIn:         "enhanced_for_statement",
		while:         "while_statement",
		do:            "do_statement",
		switchStmts:   map[string]bool{"switch_expression": true, "switch_statement": true},
		tryStmts:      map[string]bool{"try_statement": true, "try_with_resources_statement": true},
		catchClause:   "catch_clause",
		finallyClause: "finally_clause",
		labeled:       "labeled_statement",
		breakStmt:     "break_statement",
		continueStmt:  "continue_statement",
		returnStmts:   map[string]bool{"return_statement": true},
		throwStmts:    map[string]bool{"throw_statement": true},
		switchBodyKind: "switch_block",
		boundary: map[string]bool{
			"method_declaration": true, "constructor_declaration": true,
			"lambda_expression": true, "class_declaration": true,
			"interface_declaration": true, "record_declaration": true,
			"annotation_type_declaration": true, "enum_declaration": true,
			"compact_constructor_declaration": true,
		},
		caseTypes:        map[string]bool{"switch_rule": true, "switch_block_statement_group": true},
		fallThroughCases: map[string]bool{"switch_block_statement_group": true},
	}
}

func cfgGoLang() *cfgLangSpec {
	return &cfgLangSpec{
		blockTypes:   map[string]bool{"block": true},
		ifStmt:       "if_statement",
		forStmt:      "for_statement",
		switchStmts:  map[string]bool{
			"expression_switch_statement": true, "type_switch_statement": true,
			"select_statement": true,
		},
		tryStmts:     map[string]bool{},
		labeled:      "labeled_statement",
		breakStmt:    "break_statement",
		continueStmt: "continue_statement",
		returnStmts:  map[string]bool{"return_statement": true},
		throwStmts:   map[string]bool{},
		gotoStmt:     "goto_statement",
		fallthroughStmt: "fallthrough_statement",
		// switchBodyKind "" — Go case clauses are direct children of the switch node
		boundary: map[string]bool{
			"func_literal": true, "function_declaration": true,
			"method_declaration": true, "type_declaration": true,
		},
		caseTypes: map[string]bool{
			"expression_case": true, "default_case": true,
			"communication_case": true, "type_case": true,
		},
		defaultTypes: map[string]bool{"default_case": true},
	}
}

var cfgLangs = map[string]*cfgLangSpec{
	"javascript": cfgJSTSLang(),
	"typescript": cfgJSTSLang(),
	"java":       cfgJavaLang(),
	"go":         cfgGoLang(),
}

// cfgPending is an open outbound edge awaiting its destination block.
type cfgPending struct {
	from  int
	label string
}

// cfgBreakCtx accumulates break statements exiting one breakable construct.
type cfgBreakCtx struct {
	labels []string
	sites  []cfgPending
}

// cfgContinueCtx records where continues inside one loop land: either a fixed
// header block (block >= 0) or a pending list wired once the do-while
// condition block exists.
type cfgContinueCtx struct {
	labels  []string
	block   int
	pending *[]int
}

// cfgTryCtx accumulates throws raised inside one try region while handlers
// are still being emitted.
type cfgTryCtx struct {
	raises     []int // throw sites inside the try body
	lateRaises []int // throw sites inside handlers/finally
	phase      int   // 0=body, 1=handlers, 2=finally, 3=done
}

// cfgBuilder holds per-function construction state.
type cfgBuilder struct {
	src      []byte
	lang     *cfgLangSpec
	blocks   []CFGBlock
	blockDefs [][]CFGDef // parallel to blocks: defs anchored per block
	blockUses [][]CFGUse // parallel to blocks: parser-exact reads per block
	edges    []CFGEdge
	edgeSeen map[cfgEdgeKey]bool // dedupe exact (from,to,label) triples
	current  int                 // -1 = no open block
	exits    []cfgPending        // open ends feeding the next block
	breaks   []cfgBreakCtx       // breakable constructs (loop/switch/labeled)
	continues []cfgContinueCtx   // continuable loops
	tries    []*cfgTryCtx
	exitID   int

	pendingLabels []string       // labels awaiting a breakable construct
	gotoLabels    map[string]int // label -> target block (Go goto)
	pendingGotos  []cfgGotoSite
	fallthroughs  []int          // Go explicit fallthrough sites awaiting next case
}

type cfgEdgeKey struct {
	from, to int
	label    string
}

type cfgGotoSite struct {
	from  int
	label string
}

func cfgStartLine(n *sitter.Node) int { return int(n.StartPoint().Row) + 1 }
func cfgEndLine(n *sitter.Node) int   { return int(n.EndPoint().Row) + 1 }

func (b *cfgBuilder) newBlock(kind string) int {
	b.blocks = append(b.blocks, CFGBlock{Index: len(b.blocks), Kind: kind})
	b.blockDefs = append(b.blockDefs, nil)
	b.blockUses = append(b.blockUses, nil)
	return len(b.blocks) - 1
}

func (b *cfgBuilder) edge(from, to int, label string) {
	if from < 0 || to < 0 {
		return
	}
	key := cfgEdgeKey{from, to, label}
	if b.edgeSeen[key] {
		return
	}
	b.edgeSeen[key] = true
	b.edges = append(b.edges, CFGEdge{From: from, To: to, Label: label})
}

// open returns the current block, creating a fresh "block" wired from the
// pending exits when none is open.
func (b *cfgBuilder) open() int {
	if b.current < 0 {
		nb := b.newBlock("block")
		for _, p := range b.exits {
			b.edge(p.from, nb, p.label)
		}
		b.exits = nil
		b.current = nb
	}
	return b.current
}

// collectOpen returns the pending exits plus the current block as an
// unlabeled pending edge, and seals the current block.
func (b *cfgBuilder) collectOpen() []cfgPending {
	out := append([]cfgPending{}, b.exits...)
	if b.current >= 0 {
		out = append(out, cfgPending{from: b.current, label: ""})
		b.current = -1
	}
	b.exits = nil
	return out
}

// wire routes pending ends into dst keeping each pending's own label.
func (b *cfgBuilder) wire(dst int, pend []cfgPending) {
	for _, p := range pend {
		b.edge(p.from, dst, p.label)
	}
}

// wireAs routes pending ends into dst with one label.
func (b *cfgBuilder) wireAs(dst int, pend []cfgPending, label string) {
	for _, p := range pend {
		b.edge(p.from, dst, label)
	}
}

// anchor records a statement node into the current block: its start line and
// its harvested definitions.
func (b *cfgBuilder) anchor(s *sitter.Node) {
	blk := b.open()
	sl, el := cfgStartLine(s), cfgEndLine(s)
	b.blocks[blk].StatementLines = append(b.blocks[blk].StatementLines, sl)
	if b.blocks[blk].StartLine == 0 || sl < b.blocks[blk].StartLine {
		b.blocks[blk].StartLine = sl
	}
	if el > b.blocks[blk].EndLine {
		b.blocks[blk].EndLine = el
	}
	for _, d := range b.defsFor(s) {
		b.addDef(blk, d)
	}
	for _, u := range b.usesFor(s) {
		b.addUse(blk, u)
	}
}

func (b *cfgBuilder) addDef(blk int, d CFGDef) {
	for _, e := range b.blockDefs[blk] {
		if e.VarName == d.VarName && e.Line == d.Line {
			return
		}
	}
	b.blockDefs[blk] = append(b.blockDefs[blk], CFGDef{BlockIndex: blk, VarName: d.VarName, Line: d.Line})
}

func (b *cfgBuilder) addUse(blk int, u CFGUse) {
	for _, e := range b.blockUses[blk] {
		if e.VarName == u.VarName && e.Line == u.Line {
			return
		}
	}
	b.blockUses[blk] = append(b.blockUses[blk], CFGUse{BlockIndex: blk, VarName: u.VarName, Line: u.Line})
}

// field returns the first non-nil named child among the given field names.
func field(n *sitter.Node, names ...string) *sitter.Node {
	for _, name := range names {
		if c := n.ChildByFieldName(name); c != nil {
			return c
		}
	}
	return nil
}

// emitBody emits the named children of a block container (or a bare statement)
// as statements.
func (b *cfgBuilder) emitBody(node *sitter.Node) {
	for i := 0; i < int(node.NamedChildCount()); i++ {
		b.emit(node.NamedChild(i))
	}
}

// emitStmtOrBlock emits a node that is either a block container or a single
// statement.
func (b *cfgBuilder) emitStmtOrBlock(s *sitter.Node) {
	if s == nil {
		return
	}
	if b.lang.blockTypes[s.Type()] {
		b.emitBody(s)
		return
	}
	b.emit(s)
}

// emit dispatches one statement node.
func (b *cfgBuilder) emit(s *sitter.Node) {
	t := s.Type()
	switch {
	case t == b.lang.ifStmt:
		b.emitIf(s)
	case t == b.lang.forStmt:
		b.emitFor(s)
	case b.lang.forIn != "" && t == b.lang.forIn:
		b.emitForIn(s)
	case b.lang.while != "" && t == b.lang.while:
		b.emitLoop(s, "while")
	case b.lang.do != "" && t == b.lang.do:
		b.emitDoWhile(s)
	case b.lang.switchStmts[t]:
		b.emitSwitch(s)
	case b.lang.tryStmts[t]:
		b.emitTry(s)
	case b.lang.returnStmts[t]:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.edge(cur, b.exitID, "return")
	case b.lang.throwStmts[t]:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.routeThrow(cur)
	case t == b.lang.breakStmt:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.resolveBreak(s, cur)
	case t == b.lang.continueStmt:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.resolveContinue(s, cur)
	case t == b.lang.labeled:
		b.emitLabeled(s)
	case b.lang.gotoStmt != "" && t == b.lang.gotoStmt:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.pendingGotos = append(b.pendingGotos, cfgGotoSite{from: cur, label: b.stmtLabel(s)})
	case b.lang.fallthroughStmt != "" && t == b.lang.fallthroughStmt:
		b.anchor(s)
		cur := b.current
		b.current = -1
		b.fallthroughs = append(b.fallthroughs, cur)
	case b.lang.blockTypes[t]:
		// transparent inner block
		b.emitBody(s)
	default:
		b.anchor(s)
	}
}

// emitIf handles if/else chains for every language shape.
func (b *cfgBuilder) emitIf(s *sitter.Node) {
	b.anchor(s) // the if statement anchors its own decision block
	cond := b.current
	b.current = -1

	tb := b.newBlock("if_then")
	b.edge(cond, tb, "true")
	b.current = tb
	b.emitStmtOrBlock(field(s, "consequence"))
	thenOpen := b.collectOpen()

	var elseOpen []cfgPending
	if alt := field(s, "alternative"); alt != nil {
		eb := b.newBlock("if_else")
		b.edge(cond, eb, "false")
		b.current = eb
		b.emitElse(alt)
		elseOpen = b.collectOpen()
	} else {
		elseOpen = []cfgPending{{from: cond, label: "false"}}
	}
	b.exits = append(thenOpen, elseOpen...)
}

// emitElse emits the else arm, unwrapping a JS/TS else_clause.
func (b *cfgBuilder) emitElse(alt *sitter.Node) {
	if b.lang.elseClause != "" && alt.Type() == b.lang.elseClause {
		if inner := firstNamedChild(alt); inner != nil {
			b.emitStmtOrBlock(inner)
		}
		return
	}
	b.emitStmtOrBlock(alt)
}

// emitFor handles C-style for statements (JS/TS for_statement, Java
// for_statement, Go for_statement whose header children are
// for_clause/range_clause/bare expression).
func (b *cfgBuilder) emitFor(s *sitter.Node) {
	b.emitLoop(s, "for")
}

// emitForIn handles iteration loops (JS for-in/of, Java enhanced-for, Go
// range). Go's for_statement covers range too, so this only fires for the
// JS/Java node types.
func (b *cfgBuilder) emitForIn(s *sitter.Node) {
	b.emitLoop(s, "for_in")
}

// emitLoop builds header/body blocks for while/for/for_in-style loops.
func (b *cfgBuilder) emitLoop(s *sitter.Node, kind string) {
	pre := b.collectOpen()
	hb := b.newBlock(kind + "_header")
	b.wire(hb, pre)
	b.current = hb
	b.anchor(s) // header defs (initializer/condition/update or left/right)
	b.current = -1

	bb := b.newBlock(kind + "_body")
	b.edge(hb, bb, "true")
	b.current = bb

	labels := b.takeLabels()
	b.breaks = append(b.breaks, cfgBreakCtx{labels: labels})
	b.continues = append(b.continues, cfgContinueCtx{labels: labels, block: hb})

	b.emitStmtOrBlock(field(s, "body"))
	bodyOpen := b.collectOpen()
	b.wireAs(hb, bodyOpen, "loop_back")

	brk := b.breaks[len(b.breaks)-1]
	b.breaks = b.breaks[:len(b.breaks)-1]
	b.continues = b.continues[:len(b.continues)-1]

	exits := brk.sites
	if b.loopHasExit(s) {
		exits = append([]cfgPending{{from: hb, label: "false"}}, exits...)
	}
	b.exits = exits
}

// loopHasExit reports whether the loop header can fail out (has a real
// condition or iterates a collection). `for(;;)`/Go `for {}` have none.
func (b *cfgBuilder) loopHasExit(s *sitter.Node) bool {
	t := s.Type()
	if b.lang.forIn != "" && t == b.lang.forIn {
		return true // iteration exhausts
	}
	if t == b.lang.while || t == b.lang.do {
		return true
	}
	// for_statement: JS/Java use 'condition' field; Go uses for_clause's
	// 'condition' or range_clause or a bare expression child.
	if c := field(s, "condition"); c != nil {
		return true
	}
	for i := 0; i < int(s.NamedChildCount()); i++ {
		ch := s.NamedChild(i)
		ct := ch.Type()
		if ct == "body" || b.lang.blockTypes[ct] {
			continue
		}
		if ct == "for_clause" {
			if field(ch, "condition") != nil {
				return true
			}
			continue
		}
		if ct == "range_clause" {
			return true
		}
		// bare expression condition (Go `for cond {}`)
		return true
	}
	return false
}

// emitDoWhile handles `do { ... } while (cond)` (JS/TS/Java).
func (b *cfgBuilder) emitDoWhile(s *sitter.Node) {
	pre := b.collectOpen()
	bb := b.newBlock("do_body")
	b.wire(bb, pre)
	b.current = bb

	labels := b.takeLabels()
	pendingCont := []int{}
	b.breaks = append(b.breaks, cfgBreakCtx{labels: labels})
	b.continues = append(b.continues, cfgContinueCtx{labels: labels, block: -1, pending: &pendingCont})

	b.emitStmtOrBlock(field(s, "body"))
	bodyOpen := b.collectOpen()

	cb := b.newBlock("do_cond")
	b.wire(cb, bodyOpen)
	b.current = cb
	b.anchor(s) // do statement anchors at its condition evaluation
	b.current = -1
	for _, p := range pendingCont {
		b.edge(p, cb, "loop_back")
	}

	brk := b.breaks[len(b.breaks)-1]
	b.breaks = b.breaks[:len(b.breaks)-1]
	b.continues = b.continues[:len(b.continues)-1]

	b.edge(cb, bb, "loop_back") // cond true -> repeat body
	exits := append([]cfgPending{{from: cb, label: "false"}}, brk.sites...)
	b.exits = exits
}

// emitSwitch handles JS switch_statement, Java switch_expression/statement,
// and Go expression/type switch + select.
func (b *cfgBuilder) emitSwitch(s *sitter.Node) {
	b.anchor(s) // dispatch decision anchors in the head block
	head := b.current
	b.current = -1

	// locate the case container
	var container *sitter.Node
	if b.lang.switchBodyKind != "" {
		for i := 0; i < int(s.NamedChildCount()); i++ {
			if ch := s.NamedChild(i); ch.Type() == b.lang.switchBodyKind {
				container = ch
				break
			}
		}
	} else {
		container = s // Go: cases are direct children of the switch node
	}

	labels := b.takeLabels()
	b.breaks = append(b.breaks, cfgBreakCtx{labels: labels})

	var pendingFall []cfgPending // previous case's open ends falling through
	var openToAfter []cfgPending // ends that exit the switch
	hasDefault := false
	fallthroughSites := []int{}

	if container != nil {
		for i := 0; i < int(container.NamedChildCount()); i++ {
			clause := container.NamedChild(i)
			ct := clause.Type()
			if !b.lang.caseTypes[ct] {
				continue
			}
			cb := b.newBlock("case")
			b.edge(head, cb, "case")
			for _, p := range pendingFall {
				b.edge(p.from, cb, "")
			}
			for _, p := range fallthroughSites {
				b.edge(p, cb, "case")
			}
			pendingFall = nil
			fallthroughSites = nil
			b.current = cb
			b.anchorCaseHeader(clause)
			b.emitCaseBody(clause)
			open := b.collectOpen()
			if b.lang.defaultTypes[ct] || b.javaDefaultLabel(clause) {
				hasDefault = true
			}
			if b.lang.fallThroughCases[ct] {
				pendingFall = open
			} else {
				// Go cases / Java arrow rules: open ends exit the switch unless
				// an explicit fallthrough was recorded.
				openToAfter = append(openToAfter, open...)
				fallthroughSites = append(fallthroughSites, b.fallthroughs...)
				b.fallthroughs = nil
			}
		}
	}

	brk := b.breaks[len(b.breaks)-1]
	b.breaks = b.breaks[:len(b.breaks)-1]

	exits := append(openToAfter, pendingFall...)       // trailing fall-through exits too
	for _, p := range fallthroughSites {               // fallthrough past last case exits
		exits = append(exits, cfgPending{from: p, label: ""})
	}
	exits = append(exits, brk.sites...)
	if !hasDefault && head >= 0 {
		exits = append(exits, cfgPending{from: head, label: "no_match"})
	}
	b.exits = exits
}

// anchorCaseHeader records the case clause's own line (and Go/Java label
// lines) in the case block.
func (b *cfgBuilder) anchorCaseHeader(clause *sitter.Node) {
	sl := cfgStartLine(clause)
	blk := b.current
	b.blocks[blk].StatementLines = append(b.blocks[blk].StatementLines, sl)
	if b.blocks[blk].StartLine == 0 || sl < b.blocks[blk].StartLine {
		b.blocks[blk].StartLine = sl
	}
	if el := cfgEndLine(clause); el > b.blocks[blk].EndLine {
		b.blocks[blk].EndLine = el
	}
}

// emitCaseBody emits the statements inside one case clause, skipping
// header-position children (case expressions, labels, type lists).
func (b *cfgBuilder) emitCaseBody(clause *sitter.Node) {
	for i := 0; i < int(clause.NamedChildCount()); i++ {
		ch := clause.NamedChild(i)
		if b.isCaseHeaderChild(clause, ch) {
			continue
		}
		b.emitStmtOrBlock(ch)
	}
}

// isCaseHeaderChild reports whether a child of a case clause is part of the
// case header rather than a body statement:
//   - JS/TS switch_case:  the "value" field (the case expression — a use)
//   - Java:               switch_label children (labels/patterns — uses)
//   - Go expression_case: the "value" field (expression_list — uses)
//   - Go communication_case: the "communication" field — it IS evaluated at
//     dispatch, so its defs (e.g. `case v := <-ch`) are collected
//   - Go type_case:       type-bearing children ("type" field / *_type nodes)
func (b *cfgBuilder) isCaseHeaderChild(clause, ch *sitter.Node) bool {
	ct := ch.Type()
	switch ct {
	case "switch_label":
		// Java label expressions are evaluated at dispatch — reads.
		// Pattern children (``case Foo f``) BIND names — not reads.
		for i := 0; i < int(ch.NamedChildCount()); i++ {
			lch := ch.NamedChild(i)
			if strings.Contains(lch.Type(), "pattern") {
				continue
			}
			for _, u := range b.collectUses(lch) {
				b.addUse(b.current, u)
			}
		}
		return true
	case "communication":
		for _, d := range b.collectDefs(ch) {
			b.addDef(b.current, d)
		}
		for _, u := range b.collectUses(ch) {
			b.addUse(b.current, u)
		}
		return true
	case "type_list":
		return true
	}
	if clause.Type() == "type_case" && strings.Contains(ct, "type") {
		return true
	}
	if v := field(clause, "value"); v != nil && sameNode(v, ch) {
		// case <expr> — the case expression is read at dispatch.
		for _, u := range b.collectUses(ch) {
			b.addUse(b.current, u)
		}
		return true
	}
	if v := field(clause, "type"); v != nil && sameNode(v, ch) {
		return true
	}
	return false
}

func sameNode(a, b *sitter.Node) bool {
	return a.StartByte() == b.StartByte() && a.EndByte() == b.EndByte()
}

// javaDefaultLabel reports whether a Java switch_rule carries a default label
// (switch_label with no named children).
func (b *cfgBuilder) javaDefaultLabel(clause *sitter.Node) bool {
	if clause.Type() != "switch_rule" {
		return false
	}
	for i := 0; i < int(clause.NamedChildCount()); i++ {
		ch := clause.NamedChild(i)
		if ch.Type() == "switch_label" && ch.NamedChildCount() == 0 {
			return true
		}
	}
	return false
}

// emitTry handles try/catch/finally for JS/TS and Java.
func (b *cfgBuilder) emitTry(s *sitter.Node) {
	pre := b.collectOpen()
	mark := len(b.blocks)

	tb := b.newBlock("try_body")
	b.wire(tb, pre)
	b.current = tb
	b.anchor(s) // try statement anchors where the guarded region begins

	ctx := &cfgTryCtx{}
	b.tries = append(b.tries, ctx)

	// Java try_with_resources: resource bindings are defs at the try head;
	// their initializer expressions are reads at the same point.
	if res := field(s, "resources"); res != nil {
		for _, d := range b.collectDefs(res) {
			b.addDef(tb, d)
		}
		for _, u := range b.collectUses(res) {
			b.addUse(tb, u)
		}
	}

	b.emitStmtOrBlock(field(s, "body"))
	bodyOpen := b.collectOpen()
	region := b.blockRange(mark)
	ctx.phase = 1

	// handlers: JS 'handler' field; Java positional catch_clause children.
	// The field child is ALSO a named child — dedupe by node identity.
	var handlers []*sitter.Node
	if h := field(s, "handler"); h != nil {
		handlers = append(handlers, h)
	}
	for i := 0; i < int(s.NamedChildCount()); i++ {
		ch := s.NamedChild(i)
		if ch.Type() != b.lang.catchClause {
			continue
		}
		dup := false
		for _, h := range handlers {
			if sameNode(h, ch) {
				dup = true
				break
			}
		}
		if !dup {
			handlers = append(handlers, ch)
		}
	}

	var handlerBlocks []int
	var handlerOpen []cfgPending
	for _, h := range handlers {
		hb := b.newBlock("catch")
		for _, rb := range region {
			b.edge(rb, hb, "except")
		}
		b.current = hb
		b.anchor(h) // catch clause line + parameter def
		if body := field(h, "body"); body != nil {
			b.emitStmtOrBlock(body)
		} else {
			b.emitStmtOrBlock(lastNamedChild(h))
		}
		handlerBlocks = append(handlerBlocks, hb)
		handlerOpen = append(handlerOpen, b.collectOpen()...)
	}
	ctx.phase = 2

	// raises in the try body may also reach each handler
	for _, rb := range ctx.raises {
		for _, hb := range handlerBlocks {
			b.edge(rb, hb, "except")
		}
	}

	// finalizer: JS 'finalizer' field; Java positional finally_clause child
	var fin *sitter.Node
	if f := field(s, "finalizer"); f != nil {
		fin = f
	}
	for i := 0; i < int(s.NamedChildCount()); i++ {
		if ch := s.NamedChild(i); ch.Type() == b.lang.finallyClause && (fin == nil || !sameNode(fin, ch)) {
			fin = ch
		}
	}

	b.tries = b.tries[:len(b.tries)-1]

	if fin != nil {
		fb := b.newBlock("finally")
		b.wire(fb, append(bodyOpen, handlerOpen...))
		for _, rb := range append(append([]int{}, ctx.raises...), ctx.lateRaises...) {
			b.edge(rb, fb, "finally")
		}
		b.current = fb
		// finally_clause wraps a block child in both grammars
		if body := field(fin, "body"); body != nil {
			b.emitStmtOrBlock(body)
		} else {
			b.emitStmtOrBlock(firstNamedChild(fin))
		}
		b.exits = b.collectOpen()
	} else {
		for _, rb := range append(append([]int{}, ctx.raises...), ctx.lateRaises...) {
			b.edge(rb, b.exitID, "throw")
		}
		b.exits = append(bodyOpen, handlerOpen...)
	}
}

// blockRange returns block indexes created since mark (the try region).
func (b *cfgBuilder) blockRange(mark int) []int {
	out := make([]int, 0, len(b.blocks)-mark)
	for i := mark; i < len(b.blocks); i++ {
		out = append(out, i)
	}
	return out
}

// emitLabeled handles `label: stmt`. The label marks a join point: seal the
// current flow, emit the body starting a fresh block, and record the label's
// target block for goto resolution (Go).
func (b *cfgBuilder) emitLabeled(s *sitter.Node) {
	name := b.stmtLabel(s)
	mark := len(b.blocks)
	b.exits = b.collectOpen() // label is a jump target: force a block boundary

	body := field(s, "body")
	if body == nil {
		// Java/Go: body is the positional child after the label
		for i := 0; i < int(s.NamedChildCount()); i++ {
			ch := s.NamedChild(i)
			if ct := ch.Type(); ct != "statement_identifier" && ct != "label_name" && ct != "identifier" {
				body = ch
				break
			}
		}
	}
	// Remember the depth rather than assuming this frame's label survives:
	// takeLabels drains the whole slice to nil for the statement that
	// consumes it, so an unconditional pop below computed [:-1] and
	// panicked, taking the entire index build with it.
	pendingDepth := len(b.pendingLabels)
	b.pendingLabels = append(b.pendingLabels, name)
	if body != nil && b.lang.blockTypes[body.Type()] {
		// labeled block is itself breakable
		b.breaks = append(b.breaks, cfgBreakCtx{labels: []string{name}})
		b.emitBody(body)
		open := b.collectOpen()
		brk := b.breaks[len(b.breaks)-1]
		b.breaks = b.breaks[:len(b.breaks)-1]
		b.exits = append(open, brk.sites...)
	} else {
		b.emitStmtOrBlock(body)
	}
	if len(b.pendingLabels) > pendingDepth {
		b.pendingLabels = b.pendingLabels[:pendingDepth]
	}

	if name != "" {
		if mark < len(b.blocks) {
			b.gotoLabels[name] = mark
		}
	}
}

// takeLabels drains labels pushed by an enclosing labeled_statement.
func (b *cfgBuilder) takeLabels() []string {
	if len(b.pendingLabels) == 0 {
		return nil
	}
	out := append([]string{}, b.pendingLabels...)
	b.pendingLabels = nil
	return out
}

// stmtLabel returns the label text of a labeled/break/continue statement.
func (b *cfgBuilder) stmtLabel(s *sitter.Node) string {
	if l := field(s, "label"); l != nil {
		return nodeText(l, b.src)
	}
	// positional label children (Java break (identifier), Go goto (label_name))
	for i := 0; i < int(s.NamedChildCount()); i++ {
		ch := s.NamedChild(i)
		if ct := ch.Type(); ct == "statement_identifier" || ct == "label_name" || ct == "identifier" {
			return nodeText(ch, b.src)
		}
	}
	return ""
}

// resolveBreak routes a break site to the innermost matching breakable ctx.
func (b *cfgBuilder) resolveBreak(s *sitter.Node, cur int) {
	label := b.stmtLabel(s)
	for i := len(b.breaks) - 1; i >= 0; i-- {
		if label == "" || contains(b.breaks[i].labels, label) {
			b.breaks[i].sites = append(b.breaks[i].sites, cfgPending{from: cur, label: "break"})
			return
		}
	}
	// no matching construct — dead end (invalid code); flow terminates
}

// resolveContinue routes a continue site to the innermost matching loop.
func (b *cfgBuilder) resolveContinue(s *sitter.Node, cur int) {
	label := b.stmtLabel(s)
	for i := len(b.continues) - 1; i >= 0; i-- {
		c := &b.continues[i]
		if label == "" || contains(c.labels, label) {
			if c.block >= 0 {
				b.edge(cur, c.block, "loop_back")
			} else if c.pending != nil {
				*c.pending = append(*c.pending, cur)
			}
			return
		}
	}
}

// routeThrow routes a throw site: to the enclosing try's raise lists, or to
// exit as an uncaught throw.
func (b *cfgBuilder) routeThrow(cur int) {
	if len(b.tries) > 0 {
		ctx := b.tries[len(b.tries)-1]
		if ctx.phase == 0 {
			ctx.raises = append(ctx.raises, cur)
			return
		}
		ctx.lateRaises = append(ctx.lateRaises, cur)
		return
	}
	b.edge(cur, b.exitID, "throw")
}

func contains(xs []string, s string) bool {
	for _, x := range xs {
		if x == s {
			return true
		}
	}
	return false
}

func firstNamedChild(n *sitter.Node) *sitter.Node {
	if n.NamedChildCount() == 0 {
		return nil
	}
	return n.NamedChild(0)
}

func lastNamedChild(n *sitter.Node) *sitter.Node {
	if n.NamedChildCount() == 0 {
		return nil
	}
	return n.NamedChild(int(n.NamedChildCount()) - 1)
}

func nodeText(n *sitter.Node, src []byte) string {
	if n == nil {
		return ""
	}
	return n.Content(src)
}

// ── definition extraction ───────────────────────────────────────────────────

// defsFor returns the definitions a statement node introduces at its own
// position — header-only for compound statements (bodies are separate blocks),
// deep for simple statements.
func (b *cfgBuilder) defsFor(s *sitter.Node) []CFGDef {
	t := s.Type()
	var out []CFGDef
	switch {
	case t == b.lang.ifStmt || (b.lang.while != "" && t == b.lang.while):
		out = b.collectDefs(field(s, "condition"))
	case b.lang.do != "" && t == b.lang.do:
		out = b.collectDefs(field(s, "condition"))
	case t == b.lang.forStmt:
		out = b.forHeaderDefs(s)
	case b.lang.forIn != "" && t == b.lang.forIn:
		out = b.forInDefs(s)
	case b.lang.switchStmts[t]:
		// discriminant defs (rare assignments inside switch (x = f()))
		out = b.collectDefs(field(s, "value", "condition"))
	case b.lang.tryStmts[t]:
		// try bodies/handlers are separate blocks; nothing binds at the try head
		return nil
	case t == b.lang.catchClause:
		// catch (e) / catch (E e) binds the exception variable
		if p := field(s, "parameter"); p != nil {
			b.bindPattern(p, &out)
		} else {
			for i := 0; i < int(s.NamedChildCount()); i++ {
				if ch := s.NamedChild(i); ch.Type() == "catch_formal_parameter" {
					if nm := field(ch, "name"); nm != nil {
						b.bindPattern(nm, &out)
					}
				}
			}
		}
	case t == b.lang.labeled:
		return nil
	default:
		out = b.collectDefs(s)
	}
	return out
}

// forHeaderDefs collects defs from a for header's initializer/condition/update
// (JS/Java) or for_clause/range_clause/bare-expr children (Go).
func (b *cfgBuilder) forHeaderDefs(s *sitter.Node) []CFGDef {
	var out []CFGDef
	for _, fname := range []string{"initializer", "init", "condition", "increment", "update"} {
		if c := s.ChildByFieldName(fname); c != nil {
			out = append(out, b.collectDefs(c)...)
		}
	}
	if len(out) > 0 {
		return out
	}
	// Go: clause children are positional
	for i := 0; i < int(s.NamedChildCount()); i++ {
		ch := s.NamedChild(i)
		ct := ch.Type()
		if ct == "body" || b.lang.blockTypes[ct] {
			continue
		}
		switch ct {
		case "for_clause":
			for _, fname := range []string{"initializer", "condition", "update"} {
				if c := ch.ChildByFieldName(fname); c != nil {
					out = append(out, b.collectDefs(c)...)
				}
			}
		case "range_clause":
			if l := field(ch, "left"); l != nil {
				out = append(out, b.collectDefs(l)...)
			}
			out = append(out, b.collectDefs(field(ch, "right"))...)
		default:
			out = append(out, b.collectDefs(ch)...)
		}
	}
	return out
}

// forInDefs collects the loop-variable binding + iterable-side defs.
func (b *cfgBuilder) forInDefs(s *sitter.Node) []CFGDef {
	var out []CFGDef
	if l := field(s, "left"); l != nil {
		switch l.Type() {
		case "identifier", "member_expression", "subscript_expression":
			out = append(out, CFGDef{VarName: nodeText(l, b.src), Line: cfgStartLine(l)})
		default:
			// lexical_declaration / patterns
			out = append(out, b.collectDefs(l)...)
			if len(out) == 0 {
				b.bindPattern(l, &out)
			}
		}
	}
	if nm := field(s, "name"); nm != nil { // Java enhanced_for name field
		out = append(out, CFGDef{VarName: nodeText(nm, b.src), Line: cfgStartLine(nm)})
	}
	out = append(out, b.collectDefs(field(s, "right", "value"))...)
	return out
}

// collectDefs walks a subtree harvesting definitions, pruning nested scopes.
func (b *cfgBuilder) collectDefs(n *sitter.Node) []CFGDef {
	var out []CFGDef
	b.collectDefsInto(n, &out)
	return out
}

func (b *cfgBuilder) collectDefsInto(n *sitter.Node, out *[]CFGDef) {
	if n == nil || !n.IsNamed() {
		return
	}
	t := n.Type()
	if b.lang.boundary[t] {
		// a nested declaration's own NAME binds in this scope (JS function
		// declarations hoist, class declarations bind); its body is a
		// different CFG and never contributes defs.
		switch t {
		case "function_declaration", "generator_function_declaration", "class_declaration":
			if nm := field(n, "name"); nm != nil {
				*out = append(*out, CFGDef{VarName: nodeText(nm, b.src), Line: cfgStartLine(nm)})
			}
		}
		return
	}
	switch t {
	case "variable_declarator":
		if nm := field(n, "name"); nm != nil {
			b.bindPattern(nm, out)
		}
		b.collectDefsInto(field(n, "value"), out)
		return
	case "assignment_expression", "augmented_assignment_expression",
		"assignment_statement", "assignment", "update_expression",
		"inc_statement", "dec_statement":
		if l := field(n, "left"); l != nil {
			b.bindTarget(l, out)
			b.collectDefsInto(field(n, "right"), out)
			return
		}
		// update_expression/inc/dec: single operand
		for i := 0; i < int(n.NamedChildCount()); i++ {
			ch := n.NamedChild(i)
			if ct := ch.Type(); ct == "identifier" || ct == "member_expression" ||
				ct == "subscript_expression" || ct == "field_identifier" {
				*out = append(*out, CFGDef{VarName: nodeText(ch, b.src), Line: cfgStartLine(ch)})
			}
		}
		return
	case "short_var_declaration", "var_spec", "const_spec":
		// Go: left/name positions hold plain identifiers
		if l := field(n, "left"); l != nil {
			b.bindPattern(l, out)
			b.collectDefsInto(field(n, "right"), out)
			return
		}
		for i := 0; i < int(n.NamedChildCount()); i++ {
			ch := n.NamedChild(i)
			if ch.Type() == "identifier" {
				*out = append(*out, CFGDef{VarName: nodeText(ch, b.src), Line: cfgStartLine(ch)})
			} else {
				b.collectDefsInto(ch, out)
			}
		}
		return
	case "receive_statement", "send_statement":
		if l := field(n, "left"); l != nil {
			b.bindPattern(l, out)
		}
		b.collectDefsInto(field(n, "right"), out)
		return
	case "expression_list":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.bindPattern(n.NamedChild(i), out)
		}
		return
	}
	for i := 0; i < int(n.NamedChildCount()); i++ {
		b.collectDefsInto(n.NamedChild(i), out)
	}
}

// bindPattern collects bound names from a binding position (patterns,
// identifiers, parameter nodes).
func (b *cfgBuilder) bindPattern(n *sitter.Node, out *[]CFGDef) {
	if n == nil || !n.IsNamed() {
		return
	}
	switch n.Type() {
	case "identifier", "shorthand_property_identifier_pattern", "field_identifier":
		*out = append(*out, CFGDef{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
	case "object_pattern", "array_pattern":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.bindPattern(n.NamedChild(i), out)
		}
	case "pair_pattern":
		b.bindPattern(field(n, "value"), out)
	case "object_assignment_pattern", "assignment_pattern":
		b.bindPattern(field(n, "left"), out)
	case "rest_pattern", "spread_pattern", "parenthesized_expression":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.bindPattern(n.NamedChild(i), out)
		}
	case "required_parameter", "optional_parameter":
		if p := field(n, "pattern"); p != nil {
			b.bindPattern(p, out)
		} else if nm := field(n, "name"); nm != nil {
			b.bindPattern(nm, out)
		} else {
			b.bindPattern(firstNamedChild(n), out)
		}
	case "formal_parameter", "spread_parameter", "catch_formal_parameter":
		if nm := field(n, "name"); nm != nil {
			b.bindPattern(nm, out)
		}
	case "parameter_declaration", "variadic_parameter_declaration":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			if n.NamedChild(i).Type() == "identifier" {
				b.bindPattern(n.NamedChild(i), out)
			}
		}
	case "member_expression", "subscript_expression":
		// computed/property target — record the rendered target text
		*out = append(*out, CFGDef{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
	}
}

// bindTarget records the definition target of an assignment/update LHS.
func (b *cfgBuilder) bindTarget(n *sitter.Node, out *[]CFGDef) {
	if n == nil || !n.IsNamed() {
		return
	}
	switch n.Type() {
	case "identifier", "field_identifier":
		*out = append(*out, CFGDef{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
	case "member_expression", "subscript_expression":
		*out = append(*out, CFGDef{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		// index/member operands may themselves contain assignments
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.collectDefsInto(n.NamedChild(i), out)
		}
	case "object_pattern", "array_pattern":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.bindTarget(n.NamedChild(i), out)
		}
	case "pair_pattern":
		b.bindTarget(field(n, "value"), out)
	case "object_assignment_pattern", "assignment_pattern", "parenthesized_expression", "rest_pattern":
		b.bindTarget(firstNamedChild(n), out)
	case "expression_list":
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.bindTarget(n.NamedChild(i), out)
		}
	default:
		*out = append(*out, CFGDef{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
	}
}

// paramDefs returns the parameter bindings of a function node — anchored to
// the entry block as the function's initial definitions. Go method receivers
// bind in scope too.
func (b *cfgBuilder) paramDefs(funcNode *sitter.Node) []CFGDef {
	var out []CFGDef
	for _, fname := range []string{"receiver", "parameters", "formal_parameters"} {
		params := funcNode.ChildByFieldName(fname)
		if params == nil {
			continue
		}
		for i := 0; i < int(params.NamedChildCount()); i++ {
			b.bindPattern(params.NamedChild(i), &out)
		}
	}
	return out
}

// paramUses returns the reads inside a function's parameter list — default
// argument expressions only (``cb = helper`` reads ``helper``). Type
// annotations are declarations, not runtime reads, and are skipped.
func (b *cfgBuilder) paramUses(funcNode *sitter.Node) []CFGUse {
	var out []CFGUse
	for _, fname := range []string{"parameters", "formal_parameters"} {
		params := funcNode.ChildByFieldName(fname)
		if params == nil {
			continue
		}
		for i := 0; i < int(params.NamedChildCount()); i++ {
			if v := field(params.NamedChild(i), "value"); v != nil {
				b.collectUsesInto(v, &out)
			}
		}
	}
	return out
}

// ── use extraction ─────────────────────────────────────────────────────────
//
// usesFor returns the reads a statement node performs at its own position —
// header-only for compound statements (bodies anchor their own uses), deep
// for simple statements. The scoping mirrors defsFor exactly so def/use rows
// attach to the same block.

func (b *cfgBuilder) usesFor(s *sitter.Node) []CFGUse {
	t := s.Type()
	var out []CFGUse
	switch {
	case t == b.lang.ifStmt || (b.lang.while != "" && t == b.lang.while):
		// Go if/for initializers carry their own reads (`if x := f(); …`).
		b.collectUsesInto(field(s, "initializer", "init"), &out)
		b.collectUsesInto(field(s, "condition"), &out)
	case b.lang.do != "" && t == b.lang.do:
		b.collectUsesInto(field(s, "condition"), &out)
	case t == b.lang.forStmt:
		out = b.forHeaderUses(s)
	case b.lang.forIn != "" && t == b.lang.forIn:
		// left is the loop binding (a def); the iterable side is read.
		b.collectUsesInto(field(s, "right", "value"), &out)
	case b.lang.switchStmts[t]:
		b.collectUsesInto(field(s, "value", "condition"), &out)
	case b.lang.tryStmts[t]:
		// resources are handled inside emitTry; the try head reads nothing.
		return nil
	case t == b.lang.catchClause:
		// catch (E e): the exception type names a class — a type read that
		// still chains to class defs for the consumer.
		if p := field(s, "parameter"); p != nil {
			b.collectUsesInto(field(p, "type"), &out)
		} else {
			for i := 0; i < int(s.NamedChildCount()); i++ {
				if ch := s.NamedChild(i); ch.Type() == "catch_formal_parameter" {
					b.collectUsesInto(field(ch, "type"), &out)
				}
			}
		}
	case t == b.lang.labeled:
		return nil
	default:
		out = b.collectUses(s)
	}
	return out
}

// forHeaderUses collects reads from a for header's initializer/condition/
// update (JS/Java) or for_clause/range_clause/bare-expression children (Go).
func (b *cfgBuilder) forHeaderUses(s *sitter.Node) []CFGUse {
	var out []CFGUse
	for _, fname := range []string{"initializer", "init", "condition", "increment", "update"} {
		b.collectUsesInto(s.ChildByFieldName(fname), &out)
	}
	if len(out) > 0 {
		return out
	}
	for i := 0; i < int(s.NamedChildCount()); i++ {
		ch := s.NamedChild(i)
		ct := ch.Type()
		if ct == "body" || b.lang.blockTypes[ct] {
			continue
		}
		switch ct {
		case "for_clause":
			for _, fname := range []string{"initializer", "condition", "update"} {
				b.collectUsesInto(ch.ChildByFieldName(fname), &out)
			}
		case "range_clause":
			// left binds; right (the range expression) is read.
			b.collectUsesInto(field(ch, "right"), &out)
		default:
			b.collectUsesInto(ch, &out)
		}
	}
	return out
}

// collectUses walks a subtree harvesting reads, pruning nested scopes.
func (b *cfgBuilder) collectUses(n *sitter.Node) []CFGUse {
	var out []CFGUse
	b.collectUsesInto(n, &out)
	return out
}

// augAssign reports whether an assignment-shaped node's operator mutates —
// `x += y` reads x; `x = y` does not. The operator text sits between the
// left child's end byte and the right child's start byte across grammars.
func (b *cfgBuilder) augAssign(n *sitter.Node) bool {
	l := field(n, "left")
	r := field(n, "right")
	if l == nil || r == nil || l.EndByte() > r.StartByte() {
		return false
	}
	op := strings.TrimSpace(string(b.src[l.EndByte():r.StartByte()]))
	return op != "" && op != "=" && op != ":="
}

// collectUsesInto is the recursive worker: identifier loads and member-access
// chains are reads; def positions (plain-assignment LHS, declarator names,
// binding patterns) are not; member names after a receiver are not; declared
// types are not.
func (b *cfgBuilder) collectUsesInto(n *sitter.Node, out *[]CFGUse) {
	if n == nil || !n.IsNamed() {
		return
	}
	t := n.Type()
	if b.lang.boundary[t] {
		// A nested callable/class body is a different CFG; the declaration
		// name itself is not read here either (it binds, it isn't loaded).
		return
	}
	switch t {
	case "assignment_expression", "assignment_statement", "assignment":
		if b.augAssign(n) {
			// `x += y` — the LHS is read AND written.
			b.collectUsesInto(field(n, "left"), out)
		}
		b.collectUsesInto(field(n, "right"), out)
		return
	case "augmented_assignment_expression", "update_expression",
		"inc_statement", "dec_statement":
		// `x += …`, `x++` — the operand is read (and written by the def pass).
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.collectUsesInto(n.NamedChild(i), out)
		}
		return
	case "short_var_declaration", "var_spec", "const_spec",
		"local_variable_declaration", "variable_declaration",
		"lexical_declaration", "const_declaration":
		// Declaration: names/types bind or declare — only initializers read.
		b.collectUsesInto(field(n, "right", "value"), out)
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.collectUsesInto(field(n.NamedChild(i), "value"), out)
		}
		return
	case "variable_declarator":
		b.collectUsesInto(field(n, "value"), out)
		return
	case "range_clause", "receive_statement":
		// left binds; right/channel is read.
		b.collectUsesInto(field(n, "right", "value"), out)
		return
	case "send_statement":
		// `ch <- v` — channel and value are both read.
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.collectUsesInto(n.NamedChild(i), out)
		}
		return
	case "member_expression":
		// a.b(.c): emit the rendered chain, then recurse the object — each
		// nested level emits its own prefix (a, a.b, a.b.c).
		*out = append(*out, CFGUse{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		b.collectUsesInto(field(n, "object"), out)
		return
	case "field_access", "scoped_identifier":
		*out = append(*out, CFGUse{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		b.collectUsesInto(field(n, "object", "scope"), out)
		return
	case "selector_expression":
		*out = append(*out, CFGUse{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		b.collectUsesInto(field(n, "operand"), out)
		return
	case "subscript_expression", "index_expression", "array_access",
		"slice_expression":
		// a[i]: object AND index are both read; the rendered chain keeps
		// parity with member-target defs (`a[i]` can be a def).
		*out = append(*out, CFGUse{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		for i := 0; i < int(n.NamedChildCount()); i++ {
			b.collectUsesInto(n.NamedChild(i), out)
		}
		return
	case "identifier", "this", "self", "super",
		"shorthand_property_identifier", "type_identifier":
		// Leaf read. shorthand `obj = {x}` reads x; `this`/`self`/`super`
		// load the receiver; a type_identifier in expression position
		// (conversion, `new T()`, composite literal) reads the type name.
		*out = append(*out, CFGUse{VarName: nodeText(n, b.src), Line: cfgStartLine(n)})
		return
	case "field_identifier", "property_identifier",
		"private_property_identifier", "label_name", "statement_identifier":
		// Pure name positions — member names, labels — never a read.
		return
	case "object_pattern", "array_pattern", "pair_pattern",
		"object_assignment_pattern", "assignment_pattern", "rest_pattern",
		"spread_pattern", "list_splat_pattern", "dictionary_splat_pattern",
		"required_parameter", "optional_parameter", "formal_parameter",
		"spread_parameter", "catch_formal_parameter",
		"parameter_declaration", "variadic_parameter_declaration":
		// Binding positions — defs, not reads. Spread/rest inside a CALL is
		// a read of its operand though, so call sites never reach this case
		// (argument positions aren't binding patterns in these grammars).
		return
	}
	for i := 0; i < int(n.NamedChildCount()); i++ {
		b.collectUsesInto(n.NamedChild(i), out)
	}
}

// extractCFG builds the statement-level CFG for one function/method node and
// records it on the parse result. Called once per emitted function node where
// a body exists. Per-function isolation is inherent: nested function scopes
// are never descended into — their statements belong to a different CFG even
// when the nested callable has no node of its own.
func extractCFG(funcNode, bodyNode *sitter.Node, sf walker.SourceFile, src []byte, result *ParseResult, nodeIdx int) {
	lang := cfgLangs[sf.Language]
	if lang == nil || bodyNode == nil {
		return
	}
	b := &cfgBuilder{
		src:        src,
		lang:       lang,
		current:    -1,
		edgeSeen:   map[cfgEdgeKey]bool{},
		gotoLabels: map[string]int{},
	}
	entry := b.newBlock("entry")
	b.blocks[entry].StatementLines = []int{cfgStartLine(funcNode)}
	b.blocks[entry].StartLine = cfgStartLine(funcNode)
	b.blocks[entry].EndLine = cfgStartLine(funcNode)
	for _, d := range b.paramDefs(funcNode) {
		b.addDef(entry, d)
	}
	for _, u := range b.paramUses(funcNode) {
		b.addUse(entry, u)
	}
	b.exitID = b.newBlock("exit")
	b.exits = []cfgPending{{from: entry, label: "entry"}}

	if lang.blockTypes[bodyNode.Type()] {
		b.emitBody(bodyNode)
	} else {
		// expression-bodied arrow: the body IS one expression statement
		b.emit(bodyNode)
	}

	tail := b.collectOpen()
	b.wire(b.exitID, tail)
	// relabel the just-added tail edges reaching exit as "end"
	for i := len(b.edges) - len(tail); i < len(b.edges); i++ {
		if b.edges[i].To == b.exitID {
			b.edges[i].Label = "end"
		}
	}
	// Go goto fixups
	for _, g := range b.pendingGotos {
		if dst, ok := b.gotoLabels[g.label]; ok {
			b.edge(g.from, dst, "goto")
		}
	}

	fn := CFGFunc{NodeIdx: nodeIdx, Blocks: b.blocks, Edges: b.edges}
	for _, ds := range b.blockDefs {
		fn.Defs = append(fn.Defs, ds...)
	}
	for _, us := range b.blockUses {
		fn.Uses = append(fn.Uses, us...)
	}
	result.CFGs = append(result.CFGs, fn)
}
