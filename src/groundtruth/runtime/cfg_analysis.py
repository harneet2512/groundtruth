"""Intraprocedural control-flow and dataflow analysis for Python functions.

Substrate for PDG construction and program slicing (HAR-90 P1 items 5-9).
Python-only by design: the analyzer consumes stdlib ``ast`` trees, so every
construct it handles is a real Python construct; other languages are out of
scope rather than approximated.

Provenance / honesty rules (house conventions):

- Pure stdlib ``ast``. No graph.db dependency, no I/O, fully deterministic:
  identical source text always yields identical structures.
- Unsupported or exotic statements degrade to sequential flow — analysis never
  crashes on a parseable function, but every approximation is surfaced.
- What the analysis cannot see is reported, not hidden: ``limitations`` names
  dynamic features (exec/eval/getattr/setattr/globals/locals), escaping scopes
  (global/nonlocal, nested defs), approximated exception edges, un-routed
  ``finally`` blocks on abrupt exits, and unreachable code.
- Calls are never inlined. A call site inside a slice is reported as a call
  site; interprocedural effects are a standing limitation.
  ``interprocedural_slice`` composes bounded cross-function slices through a
  caller-supplied callee index; every composed hop is summary-level
  (``interprocedural_summary``), never type-proven.

CFG edge labels: ``entry`` (entry block -> first block), ``next`` (sequential
fall-through), ``true``/``false`` (if arms), ``loop`` (loop header -> body),
``back`` (loop-body tail -> header), ``exit`` (loop header -> after loop),
``break``/``continue``, ``try`` (into try body), ``except`` (try body -> handler
and raise -> handler), ``finally``, ``raise``/``raise_uncaught``,
``assert_fail``, ``case``/``case_body``/``no_match`` (match dispatch),
``end`` (function tail -> exit block).
"""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Protocol, Sequence


class CFGAnalysisError(Exception):
    """Raised when analysis cannot produce a trustworthy result.

    Callers should treat this as "no evidence", never as partial evidence:
    the module never returns a silently truncated analysis.
    """


# Names whose dynamic behavior the static analysis cannot model.
_DYNAMIC_CALLS = frozenset(
    {"exec", "eval", "compile", "setattr", "delattr", "globals", "locals", "vars", "getattr", "__import__"}
)


@dataclass(frozen=True, slots=True)
class _Def:
    """One definition site. ``weak`` marks mutations (``a.b = x``, ``a[i] = x``)
    that do not rebind the name and therefore do not kill prior definitions."""

    name: str
    line: int
    weak: bool = False


@dataclass(frozen=True, slots=True)
class _Use:
    name: str
    line: int


@dataclass(slots=True)
class BasicBlock:
    """One basic block: a maximal sequence of items with single entry/exit.

    ``statements`` holds the ast nodes anchored to this block — simple
    statements, the header node of a compound statement (``If`` in its branch
    block, ``While``/``For`` in the loop-header block, ``Match`` in the
    dispatch block), plus non-statement items where honesty requires it:
    match ``pattern`` nodes (they bind names), ``case.guard`` expressions, and
    ``ExceptHandler`` nodes (``type`` is a use, ``name`` is a def). Every item
    carries real file line numbers via ``node.lineno``.
    """

    id: int
    label: str = "block"
    statements: list[ast.AST] = field(default_factory=list)
    successors: list[int] = field(default_factory=list)
    predecessors: list[int] = field(default_factory=list)
    is_entry: bool = False
    is_exit: bool = False

    @property
    def line_numbers(self) -> list[int]:
        return [n.lineno for n in self.statements if getattr(n, "lineno", None)]

    @property
    def start_line(self) -> int | None:
        lines = self.line_numbers
        return min(lines) if lines else None

    @property
    def end_line(self) -> int | None:
        ends = [
            getattr(n, "end_lineno", None) or n.lineno
            for n in self.statements
            if getattr(n, "lineno", None)
        ]
        return max(ends) if ends else None


@dataclass(slots=True)
class CFG:
    """Control-flow graph over one function body.

    ``blocks`` is insertion-ordered by construction; ``edges`` is the
    deterministic append order of construction. ``item_block`` /
    ``item_effects`` key statements by ``id(node)`` — the same identity the
    ast nodes keep for the life of the parse.
    """

    blocks: dict[int, BasicBlock]
    entry_id: int
    exit_id: int
    edges: list[tuple[int, int, str]]
    func_node: ast.AST
    function_name: str
    def_line: int
    params: tuple[str, ...]
    source_lines: tuple[str, ...] = ()
    item_block: dict[int, int] = field(default_factory=dict)
    item_effects: dict[int, tuple[list[_Def], list[_Use]]] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def block_of(self, node: ast.AST) -> int:
        return self.item_block[id(node)]

    def statements_at_line(self, line: int) -> list[ast.AST]:
        """Items whose source span covers ``line`` (multi-line statements
        match on every covered line). Deterministic order: block id, then
        position inside the block."""
        out: list[ast.AST] = []
        for bid in sorted(self.blocks):
            for item in self.blocks[bid].statements:
                if item is self.func_node:
                    continue  # pseudo-item spans the whole def; never a seed
                lineno = getattr(item, "lineno", None)
                if lineno is None:
                    continue
                end = getattr(item, "end_lineno", None) or lineno
                if lineno <= line <= end:
                    out.append(item)
        return out

    def effects(self, node: ast.AST) -> tuple[list[_Def], list[_Use]]:
        return self.item_effects.get(id(node), ([], []))


@dataclass(slots=True)
class DominatorTree:
    """Dominator sets plus immediate dominators. ``idom[root]`` is None."""

    dom: dict[int, set[int]]
    idom: dict[int, int | None]


@dataclass(slots=True)
class ReachingDefinitions:
    """Per-block reaching definitions as ``var -> {(var, line)}`` maps.

    Keys of the definition sets are ``(variable, line)`` pairs — the same key
    space the use-def chains use.
    """

    in_: dict[int, dict[str, set[tuple[str, int]]]]
    out: dict[int, dict[str, set[tuple[str, int]]]]
    all_defs: dict[str, set[tuple[str, int]]]


@dataclass(slots=True)
class UseDefChains:
    """use -> defs and def -> uses, keyed by ``(variable, line)`` on both
    sides. A use key identifies a use *site*; a def key identifies a
    definition *site*."""

    use_to_defs: dict[tuple[str, int], set[tuple[str, int]]]
    def_to_uses: dict[tuple[str, int], set[tuple[str, int]]]


# ---------------------------------------------------------------------------
# Effect extraction: which names a block item defines and uses.
# ---------------------------------------------------------------------------


def _render_target(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return node.__class__.__name__


class _LoadCollect(ast.NodeVisitor):
    """Collect Load-context names with scope honesty.

    Comprehension and lambda bound names shadow enclosing uses inside their
    own subtree only. Nested def/class bodies are never descended into (they
    are separate scopes); lambda bodies *are* descended into — a may-use
    over-approximation, standard for static slicing.
    """

    def __init__(self) -> None:
        self.uses: list[_Use] = []
        self.defs: list[_Def] = []  # walrus targets inside expressions
        self._bound: list[set[str]] = [set()]

    def _is_bound(self, name: str) -> bool:
        return any(name in scope for scope in self._bound)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load) and not self._is_bound(node.id):
            self.uses.append(_Use(node.id, node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            name = _render_target(node)
            if not self._is_bound(name.split(".", 1)[0]):
                self.uses.append(_Use(name, node.lineno))
        self.generic_visit(node)  # also visits the base -> "a.b" yields "a.b" and "a"

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        if isinstance(node.target, ast.Name):
            self.defs.append(_Def(node.target.id, node.lineno))

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for d in node.args.defaults:
            self.visit(d)
        for d in node.args.kw_defaults:
            if d is not None:
                self.visit(d)
        self._bound.append(_arg_names(node.args))
        self.visit(node.body)
        self._bound.pop()

    def _comprehension(self, node: ast.AST, elt_nodes: Iterable[ast.AST]) -> None:
        bound: set[str] = set()
        for gen in node.generators:  # type: ignore[attr-defined]
            bound |= set(_target_names(gen.target))
        self._bound.append(bound)
        for gen in node.generators:  # type: ignore[attr-defined]
            self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for elt in elt_nodes:
            self.visit(elt)
        self._bound.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._comprehension(node, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._comprehension(node, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._comprehension(node, [node.key, node.value])

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._nested_def_header(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._nested_def_header(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        for kw in node.keywords:
            self.visit(kw.value)

    def _nested_def_header(self, node: ast.AST) -> None:
        for d in node.decorator_list:  # type: ignore[attr-defined]
            self.visit(d)
        args = node.args  # type: ignore[attr-defined]
        for d in args.defaults:
            self.visit(d)
        for d in args.kw_defaults:
            if d is not None:
                self.visit(d)
        returns = getattr(node, "returns", None)
        if returns is not None:
            self.visit(returns)


class _TargetCollect(ast.NodeVisitor):
    """Collect definitions from an assignment/loop/with target."""

    def __init__(self) -> None:
        self.defs: list[_Def] = []
        self.uses: list[_Use] = []

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.defs.append(_Def(node.id, node.lineno))
        else:
            self.uses.append(_Use(node.id, node.lineno))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.defs.append(_Def(_render_target(node), node.lineno, weak=True))
            base = _LoadCollect()
            base.visit(node.value)
            self.uses.extend(base.uses)
        else:
            base = _LoadCollect()
            base.visit(node)
            self.uses.extend(base.uses)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            # Mutation, not rebinding: weak def on the rendered target and on
            # the base object; both the base and the slice are uses.
            self.defs.append(_Def(_render_target(node), node.lineno, weak=True))
            base_name = _render_target(node.value)
            self.defs.append(_Def(base_name, node.lineno, weak=True))
            base = _LoadCollect()
            base.visit(node.value)
            self.uses.extend(base.uses)
            sl = _LoadCollect()
            sl.visit(node.slice)
            self.uses.extend(sl.uses)
        else:
            base = _LoadCollect()
            base.visit(node)
            self.uses.extend(base.uses)

    def visit_Starred(self, node: ast.Starred) -> None:
        self.visit(node.value)

    def visit_Tuple(self, node: ast.Tuple) -> None:
        for elt in node.elts:
            self.visit(elt)

    def visit_List(self, node: ast.List) -> None:
        for elt in node.elts:
            self.visit(elt)


def _arg_names(args: ast.arguments) -> set[str]:
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    if args.kwarg is not None:
        names.add(args.kwarg.arg)
    return names


def _target_names(target: ast.AST) -> list[str]:
    tc = _TargetCollect()
    tc.visit(target)
    return [d.name for d in tc.defs]


def _loads(node: ast.AST | None) -> tuple[list[_Def], list[_Use]]:
    if node is None:
        return [], []
    c = _LoadCollect()
    c.visit(node)
    return c.defs, c.uses


def _stores(target: ast.AST | None) -> tuple[list[_Def], list[_Use]]:
    if target is None:
        return [], []
    c = _TargetCollect()
    c.visit(target)
    return c.defs, c.uses


def _target_reads(target: ast.AST) -> list[_Use]:
    """Uses implied by reading an assignment target (AugAssign, Del).

    ``a.b += 1`` reads ``a.b`` and ``a``; ``a[i] += 1`` additionally reads
    ``i``. Plain names read themselves.
    """
    uses: list[_Use] = []
    if isinstance(target, ast.Name):
        uses.append(_Use(target.id, target.lineno))
    elif isinstance(target, ast.Attribute):
        uses.append(_Use(_render_target(target), target.lineno))
        _d, u = _loads(target.value)
        uses.extend(u)
    elif isinstance(target, ast.Subscript):
        uses.append(_Use(_render_target(target), target.lineno))
        _d, u = _loads(target.value)
        uses.extend(u)
        _d, u = _loads(target.slice)
        uses.extend(u)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            uses.extend(_target_reads(elt))
    elif isinstance(target, ast.Starred):
        uses.extend(_target_reads(target.value))
    return uses


def _pattern_effects(pattern: ast.AST) -> tuple[list[_Def], list[_Use]]:
    """Defs = names bound by the pattern; uses = names the pattern reads
    (class patterns, value expressions). Both keep real pattern line numbers."""
    defs: list[_Def] = []
    uses: list[_Use] = []
    for node in ast.walk(pattern):
        if isinstance(node, ast.MatchAs):
            if node.name:
                defs.append(_Def(node.name, node.lineno))
        elif isinstance(node, ast.MatchStar):
            if node.name:
                defs.append(_Def(node.name, node.lineno))
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                defs.append(_Def(node.rest, node.lineno))
        elif isinstance(node, ast.MatchClass):
            ld, lu = _loads(node.cls)
            defs.extend(ld)
            uses.extend(lu)
        elif isinstance(node, ast.MatchValue):
            ld, lu = _loads(node.value)
            defs.extend(ld)
            uses.extend(lu)
    return defs, uses


def _item_effects(item: ast.AST) -> tuple[list[_Def], list[_Use]]:
    """(defs, uses) contributed by one block item.

    Compound statements contribute only their *header* effects — bodies are
    separate blocks, so descending into them would double-count.
    """
    defs: list[_Def] = []
    uses: list[_Use] = []

    def load(node: ast.AST | None) -> None:
        d, u = _loads(node)
        defs.extend(d)
        uses.extend(u)

    def store(node: ast.AST | None) -> None:
        d, u = _stores(node)
        defs.extend(d)
        uses.extend(u)

    if isinstance(item, ast.If):
        load(item.test)
    elif isinstance(item, ast.While):
        load(item.test)
    elif isinstance(item, (ast.For, ast.AsyncFor)):
        load(item.iter)
        store(item.target)
    elif isinstance(item, (ast.With, ast.AsyncWith)):
        for w in item.items:
            load(w.context_expr)
            store(w.optional_vars)
    elif isinstance(item, ast.Match):
        load(item.subject)
    elif isinstance(item, (ast.Try,) + (() if not hasattr(ast, "TryStar") else (ast.TryStar,))):
        pass
    elif isinstance(item, ast.ExceptHandler):
        load(item.type)
        if item.name:
            defs.append(_Def(item.name, item.lineno))
    elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
        defs.append(_Def(item.name, item.lineno))
        for d in item.decorator_list:
            load(d)
        for d in item.args.defaults:
            load(d)
        for d in item.args.kw_defaults:
            if d is not None:
                load(d)
        if item.returns is not None:
            load(item.returns)
    elif isinstance(item, ast.ClassDef):
        defs.append(_Def(item.name, item.lineno))
        for d in item.decorator_list:
            load(d)
        for b in item.bases:
            load(b)
        for kw in item.keywords:
            load(kw.value)
    elif isinstance(item, ast.Return):
        load(item.value)
    elif isinstance(item, ast.Raise):
        load(item.exc)
        load(item.cause)
    elif isinstance(item, ast.Assert):
        load(item.test)
        load(item.msg)
    elif isinstance(item, ast.Assign):
        load(item.value)
        for t in item.targets:
            store(t)
    elif isinstance(item, ast.AugAssign):
        # Augmented assignment both reads and rebinds its target. The target
        # is Store-context, so its read is collected explicitly here.
        uses.extend(_target_reads(item.target))
        store(item.target)
        load(item.value)
    elif isinstance(item, ast.AnnAssign):
        # Local annotations are not evaluated at runtime; only a value
        # counts. `x: T` alone is neither a def nor a use.
        if item.value is not None:
            load(item.value)
            store(item.target)
    elif isinstance(item, ast.Delete):
        for t in item.targets:
            store(t)
    elif isinstance(item, ast.Expr):
        load(item.value)
    elif isinstance(item, ast.Import):
        for a in item.names:
            defs.append(_Def(a.asname or a.name.split(".", 1)[0], item.lineno))
    elif isinstance(item, ast.ImportFrom):
        for a in item.names:
            if a.name == "*":
                continue  # star-import limitation recorded by the builder
            defs.append(_Def(a.asname or a.name, item.lineno))
    elif hasattr(ast, "TypeAlias") and isinstance(item, ast.TypeAlias):
        store(item.name)
        load(item.value)
    elif isinstance(item, (ast.Global, ast.Nonlocal, ast.Pass, ast.Break, ast.Continue)):
        pass
    elif isinstance(item, ast.pattern):  # match-case pattern item
        d, u = _pattern_effects(item)
        defs.extend(d)
        uses.extend(u)
    elif isinstance(item, ast.expr):
        # Bare expression items (match guards).
        load(item)
    else:
        # Exotic/unknown statement: honest sequential approximation — count
        # every load it contains, and defs for any Store targets it carries.
        d, u = _loads(item)
        defs.extend(d)
        uses.extend(u)
        for node in ast.walk(item):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                defs.append(_Def(node.id, node.lineno))
    return defs, uses


# ---------------------------------------------------------------------------
# CFG construction.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _TryCtx:
    """Book-keeping for one active try region."""

    has_finally: bool
    phase: str = "body"  # "body" | "handlers" | "finally" | "done"
    handler_blocks: list[int] = field(default_factory=list)
    raises: list[int] = field(default_factory=list)  # raise sites inside body
    late_raises: list[int] = field(default_factory=list)  # raise inside handlers


class _Builder:
    def __init__(self, func_node: ast.AST, exit_id_holder: list[int]) -> None:
        self.func = func_node
        self.blocks: dict[int, BasicBlock] = {}
        self.order: list[int] = []
        self.edges: list[tuple[int, int, str]] = []
        self.item_block: dict[int, int] = {}
        self._counter = 0
        self.current: int | None = None
        self.exits: list[tuple[int, str]] = []
        self._breaks: list[list[tuple[int, str]]] = []
        self._continues: list[int] = []
        self._try_stack: list[_TryCtx] = []
        self.limitations: list[str] = []
        self.exit_id_holder = exit_id_holder

    # -- block/edge primitives ---------------------------------------------

    def _new_block(self, label: str = "block") -> int:
        bid = self._counter
        self._counter += 1
        self.blocks[bid] = BasicBlock(id=bid, label=label)
        self.order.append(bid)
        return bid

    def _edge(self, src: int, dst: int, label: str) -> None:
        self.edges.append((src, dst, label))
        if dst not in self.blocks[src].successors:
            self.blocks[src].successors.append(dst)
        if src not in self.blocks[dst].predecessors:
            self.blocks[dst].predecessors.append(src)

    def _limitation(self, key: str) -> None:
        if key not in self.limitations:
            self.limitations.append(key)

    def _open(self) -> int:
        """The block the next sequential statement appends to."""
        if self.current is None:
            b = self._new_block()
            for src, label in self.exits:
                self._edge(src, b, label)
            if not self.exits and self.order[:-1]:
                # No predecessor wired in: unreachable continuation.
                self._limitation("unreachable_code")
            self.exits = []
            self.current = b
        return self.current

    def _append(self, node: ast.AST) -> None:
        b = self._open()
        self.blocks[b].statements.append(node)
        self.item_block[id(node)] = b

    def _collect_open(self) -> list[tuple[int, str]]:
        """All dangling (block, label) exits that flow into whatever is next."""
        out = list(self.exits)
        if self.current is not None:
            out.append((self.current, "next"))
            self.current = None
        self.exits = []
        return out

    def _wire(self, exits: Iterable[tuple[int, str]], dst: int) -> None:
        for src, label in exits:
            self._edge(src, dst, label)

    # -- statement dispatch -------------------------------------------------

    def _emit_body(self, stmts: Sequence[ast.stmt]) -> None:
        for s in stmts:
            self._emit(s)

    def _emit(self, s: ast.stmt) -> None:
        if isinstance(s, ast.If):
            self._emit_if(s)
        elif isinstance(s, ast.While):
            self._emit_loop(s, "while")
        elif isinstance(s, (ast.For, ast.AsyncFor)):
            if isinstance(s, ast.AsyncFor):
                self._limitation("async_concurrency")
            self._emit_loop(s, "for")
        elif isinstance(s, ast.Try) or (hasattr(ast, "TryStar") and isinstance(s, ast.TryStar)):
            self._emit_try(s)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            self._append(s)
            self._limitation("with_suppress")
            if isinstance(s, ast.AsyncWith):
                self._limitation("async_concurrency")
        elif isinstance(s, ast.Match):
            self._emit_match(s)
        elif isinstance(s, ast.Return):
            self._append(s)
            cur, self.current = self.current, None
            if any(c.has_finally and c.phase != "done" for c in self._try_stack):
                self._limitation("finally_bypass")
            self._edge(cur, self.exit_id_holder[0], "return")
        elif isinstance(s, ast.Raise):
            self._append(s)
            cur, self.current = self.current, None
            self._route_raise(cur, "raise")
        elif isinstance(s, ast.Assert):
            self._append(s)
            # Failure path modeled like a raise; the pass path falls through.
            self._route_raise(self.current, "assert_fail")
        elif isinstance(s, ast.Break):
            self._append(s)
            cur, self.current = self.current, None
            if not self._breaks:
                raise CFGAnalysisError("'break' outside of loop")
            if any(c.has_finally and c.phase != "done" for c in self._try_stack):
                self._limitation("finally_bypass")
            self._breaks[-1].append((cur, "break"))
        elif isinstance(s, ast.Continue):
            self._append(s)
            cur, self.current = self.current, None
            if not self._continues:
                raise CFGAnalysisError("'continue' outside of loop")
            if any(c.has_finally and c.phase != "done" for c in self._try_stack):
                self._limitation("finally_bypass")
            self._edge(cur, self._continues[-1], "continue")
        elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._append(s)
            self._limitation("nested_def")
            if isinstance(s, ast.AsyncFunctionDef):
                self._limitation("async_concurrency")
        elif isinstance(s, (ast.Global, ast.Nonlocal)):
            self._append(s)
            self._limitation("global_nonlocal")
        elif isinstance(s, ast.ImportFrom) and any(a.name == "*" for a in s.names):
            self._append(s)
            self._limitation("star_import")
        else:
            self._append(s)
        self._scan_dynamic(s)

    def _route_raise(self, block: int, label: str) -> None:
        """Route an exceptional exit from ``block``.

        Inside a try body: edges to every handler plus the finally-or-exit
        uncaught path. Inside a handler: finally-or-exit only. Otherwise:
        straight to the function exit.
        """
        if self._try_stack and self._try_stack[-1].phase == "body":
            self._try_stack[-1].raises.append(block)
        elif self._try_stack and self._try_stack[-1].phase == "handlers":
            self._try_stack[-1].late_raises.append(block)
        else:
            self._edge(block, self.exit_id_holder[0], label)

    def _scan_dynamic(self, s: ast.stmt) -> None:
        for node in ast.walk(s):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if name in _DYNAMIC_CALLS:
                    self._limitation("dynamic_names")
            elif isinstance(node, ast.Await):
                self._limitation("async_concurrency")

    # -- compound statements --------------------------------------------------

    def _emit_if(self, s: ast.If) -> None:
        self._append(s)  # the If node anchors the branch decision + test uses
        cond = self.current
        self.current = None

        tb = self._new_block("if_then")
        self._edge(cond, tb, "true")
        self.current = tb
        self._emit_body(s.body)
        then_open = self._collect_open()

        if s.orelse:
            eb = self._new_block("if_else")
            self._edge(cond, eb, "false")
            self.current = eb
            self._emit_body(s.orelse)
            else_open = self._collect_open()
        else:
            else_open = [(cond, "false")]

        self.exits = then_open + else_open
        self.current = None

    def _emit_loop(self, s: ast.While | ast.For | ast.AsyncFor, kind: str) -> None:
        pre = self._collect_open()
        hb = self._new_block(f"{kind}_header")
        self._wire(pre, hb)
        self.current = hb
        self._append(s)  # loop header owns the test / iter+target effects
        self.current = None

        bb = self._new_block(f"{kind}_body")
        self._edge(hb, bb, "loop")
        self._breaks.append([])
        self._continues.append(hb)
        self.current = bb
        self._emit_body(s.body)
        body_open = self._collect_open()
        self._wire(body_open, hb)  # back-edge
        # Re-label the just-added back edges for readability.
        for i in range(len(self.edges) - len(body_open), len(self.edges)):
            src, dst, _ = self.edges[i]
            self.edges[i] = (src, dst, "back")
        breaks = self._breaks.pop()
        self._continues.pop()

        if s.orelse:
            ob = self._new_block(f"{kind}_else")
            self._edge(hb, ob, "exit")
            self.current = ob
            self._emit_body(s.orelse)
            else_open = self._collect_open()
        else:
            else_open = [(hb, "exit")]

        self.exits = else_open + breaks
        self.current = None

    def _emit_try(self, s: ast.Try) -> None:
        pre = self._collect_open()
        mark = len(self.order)
        tb = self._new_block("try_body")
        self._wire(pre, tb)
        ctx = _TryCtx(has_finally=bool(s.finalbody))
        self._try_stack.append(ctx)
        self.current = tb
        self._emit_body(s.body)
        body_open = self._collect_open()
        region = self.order[mark:]  # every block created inside the try body

        ctx.phase = "handlers"
        handler_open: list[tuple[int, str]] = []
        for h in s.handlers:
            hb = self._new_block("except")
            # May-raise approximation: an exception can leave the try body from
            # any of its blocks; each handler is a candidate catch site.
            for b in region:
                self._edge(b, hb, "except")
            ctx.handler_blocks.append(hb)
            self.current = hb
            self._append(h)  # ExceptHandler: `type` is a use, `name` is a def
            self._emit_body(h.body)
            handler_open.extend(self._collect_open())

        ctx.phase = "done"
        for rb in ctx.raises:
            for hb in ctx.handler_blocks:
                self._edge(rb, hb, "except")

        if s.orelse:
            ob = self._new_block("try_else")
            self._wire(body_open, ob)
            self.current = ob
            self._emit_body(s.orelse)
            else_open = self._collect_open()
        else:
            else_open = body_open

        open_ends = else_open + handler_open
        if s.finalbody:
            fb = self._new_block("finally")
            self._wire(open_ends, fb)
            for rb in ctx.raises + ctx.late_raises:
                self._edge(rb, fb, "finally")
            ctx.phase = "finally"
            self.current = fb
            self._emit_body(s.finalbody)
            fin_open = self._collect_open()
            ctx.phase = "done"
            self.exits = fin_open
        else:
            for rb in ctx.raises + ctx.late_raises:
                self._edge(rb, self.exit_id_holder[0], "raise_uncaught")
            self.exits = open_ends
        self.current = None
        self._try_stack.pop()
        self._limitation("try_exception_edges")

    def _emit_match(self, s: ast.Match) -> None:
        self._append(s)  # Match node: subject uses at the dispatch line
        head = self.current
        self.current = None
        prev_reject = head
        case_exits: list[tuple[int, str]] = []
        for case in s.cases:
            cb = self._new_block("match_case")
            # Pattern failure (or guard failure) falls through to the next
            # case — over-approximated as an edge even for irrefutable patterns.
            self._edge(prev_reject, cb, "case")
            self.current = cb
            self._append(case.pattern)
            if case.guard is not None:
                self._append(case.guard)
            self.current = None
            bb = self._new_block("match_body")
            self._edge(cb, bb, "case_body")
            self.current = bb
            self._emit_body(case.body)
            case_exits.extend(self._collect_open())
            prev_reject = cb
        # A non-matching subject falls through the whole match.
        self.exits = case_exits + [(prev_reject, "no_match")]
        self.current = None


def _param_defs(func: ast.AST) -> list[_Def]:
    line = func.lineno
    args: ast.arguments = func.args  # type: ignore[attr-defined]
    return [_Def(name, line) for name in sorted(_arg_names(args))]


def build_cfg(func_node: ast.AST, source_lines: Sequence[str] | None = None) -> CFG:
    """Build the intraprocedural CFG for one function's body.

    ``func_node`` is a ``FunctionDef``/``AsyncFunctionDef`` (any object with a
    ``body`` list of statements works). ``source_lines`` is the file's text
    split into lines; it is carried on the result for consumers that render
    evidence and is never required for correctness.
    """
    if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise CFGAnalysisError(
            f"build_cfg expects FunctionDef/AsyncFunctionDef, got {type(func_node).__name__}"
        )

    exit_holder = [-1]
    builder = _Builder(func_node, exit_holder)

    entry = builder._new_block("entry")
    builder.blocks[entry].is_entry = True
    # The function node itself anchors parameter definitions at the def line.
    builder.current = entry
    builder._append(func_node)
    builder.current = None

    exit_b = builder._new_block("exit")
    builder.blocks[exit_b].is_exit = True
    exit_holder[0] = exit_b

    builder.exits = [(entry, "entry")]
    builder.current = None
    builder._emit_body(func_node.body)
    tail = builder._collect_open()
    builder._wire(tail, exit_b)
    for i in range(len(builder.edges) - len(tail), len(builder.edges)):
        src, dst, _ = builder.edges[i]
        if dst == exit_b:
            builder.edges[i] = (src, dst, "end")

    item_effects: dict[int, tuple[list[_Def], list[_Use]]] = {}
    for bid in builder.order:
        for item in builder.blocks[bid].statements:
            if item is func_node:
                item_effects[id(item)] = (_param_defs(func_node), [])
            else:
                item_effects[id(item)] = _item_effects(item)

    if any(
        isinstance(n, ast.Call)
        for blk in builder.blocks.values()
        for it in blk.statements
        for n in ast.walk(it)
    ):
        builder._limitation("call_sites_not_inlined")

    return CFG(
        blocks=builder.blocks,
        entry_id=entry,
        exit_id=exit_b,
        edges=builder.edges,
        func_node=func_node,
        function_name=func_node.name,
        def_line=func_node.lineno,
        params=tuple(sorted(_arg_names(func_node.args))),
        source_lines=tuple(source_lines or ()),
        item_block=builder.item_block,
        item_effects=item_effects,
        limitations=builder.limitations,
    )


# ---------------------------------------------------------------------------
# Dominators / post-dominators (iterative dataflow, reverse postorder).
# ---------------------------------------------------------------------------


def _rpo(cfg: CFG, root: int, succ: dict[int, list[int]]) -> list[int]:
    seen: set[int] = set()
    post: list[int] = []
    stack: list[tuple[int, Iterator[int]]] = [(root, iter(succ.get(root, ())))]
    seen.add(root)
    while stack:
        node, it = stack[-1]
        advanced = False
        for nxt in it:
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, iter(succ.get(nxt, ()))))
                advanced = True
                break
        if not advanced:
            post.append(node)
            stack.pop()
    post.reverse()
    return post


def _dominators(
    nodes: list[int],
    root: int,
    preds: dict[int, list[int]],
    rpo: list[int],
) -> DominatorTree:
    all_nodes = set(nodes)
    dom: dict[int, set[int]] = {n: set(all_nodes) for n in nodes}
    dom[root] = {root}
    changed = True
    while changed:
        changed = False
        for n in rpo:
            if n == root:
                continue
            pred_doms = [dom[p] for p in preds.get(n, ()) if p in dom]
            if pred_doms:
                new = {n} | set.intersection(*pred_doms)
            else:
                new = {n}
            if new != dom[n]:
                dom[n] = new
                changed = True

    idom: dict[int, int | None] = {root: None}
    for n in nodes:
        if n == root:
            continue
        strict = dom[n] - {n}
        # The immediate dominator is the strict dominator dominated by every
        # other strict dominator — the closest one to n on the dom chain.
        candidate = None
        for d in strict:
            if all(s == d or s in dom[d] for s in strict):
                candidate = d
                break
        idom[n] = candidate
    return DominatorTree(dom=dom, idom=idom)


def dominators(cfg: CFG) -> DominatorTree:
    """Forward dominators rooted at the entry block. Entry dominates all."""
    succ = {b.id: list(b.successors) for b in cfg.blocks.values()}
    preds = {b.id: list(b.predecessors) for b in cfg.blocks.values()}
    nodes = list(cfg.blocks)
    rpo = _rpo(cfg, cfg.entry_id, succ)
    # Blocks unreachable from entry (dead code) still get dom sets; iterate
    # over creation order for them after the reachable RPO.
    remaining = [n for n in nodes if n not in rpo]
    return _dominators(nodes, cfg.entry_id, preds, rpo + remaining)


def post_dominators(cfg: CFG) -> DominatorTree:
    """Post-dominators = dominators of the reversed graph rooted at exit."""
    succ = {b.id: list(b.predecessors) for b in cfg.blocks.values()}  # reversed
    preds = {b.id: list(b.successors) for b in cfg.blocks.values()}
    nodes = list(cfg.blocks)
    rpo = _rpo(cfg, cfg.exit_id, succ)
    remaining = [n for n in nodes if n not in rpo]
    return _dominators(nodes, cfg.exit_id, preds, rpo + remaining)


# ---------------------------------------------------------------------------
# Control dependence (Ferrante–Ottenstein–Warren).
# ---------------------------------------------------------------------------


def control_dependence(cfg: CFG) -> set[tuple[int, int, str]]:
    """``{(dependent_block, controlling_block, edge_label)}``.

    For CFG edge A->B where B does not post-dominate A, every block on the
    post-dominator-tree path from B up to (excluding) ipdom(A) is control
    dependent on A via this edge's label.
    """
    pdom = post_dominators(cfg)
    ipdom = pdom.idom
    deps: set[tuple[int, int, str]] = set()
    for a, b, label in cfg.edges:
        if b in pdom.dom.get(a, ()):
            continue
        stop = ipdom.get(a)
        n: int | None = b
        while n is not None and n != stop:
            deps.add((n, a, label))
            n = ipdom.get(n)
    return deps


# ---------------------------------------------------------------------------
# Reaching definitions and use-def chains.
# ---------------------------------------------------------------------------


def reaching_definitions(cfg: CFG, func_node: ast.AST | None = None) -> ReachingDefinitions:
    """Classic gen/kill reaching definitions, transfer-function formulation.

    Per block, ``in`` maps each variable to the def sites that may reach the
    block head; ``out`` applies the block's def sequence in order. Strong defs
    (name rebinding) kill; weak defs (attribute/subscript mutation) accumulate.
    """
    seq: dict[int, list[_Def]] = {}
    all_defs: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for bid, blk in cfg.blocks.items():
        defs: list[_Def] = []
        for item in blk.statements:
            d, _u = cfg.effects(item)
            defs.extend(d)
        seq[bid] = defs
        for d in defs:
            all_defs[d.name].add((d.name, d.line))

    in_: dict[int, dict[str, set[tuple[str, int]]]] = {b: defaultdict(set) for b in cfg.blocks}
    out: dict[int, dict[str, set[tuple[str, int]]]] = {b: defaultdict(set) for b in cfg.blocks}
    preds = {b.id: list(b.predecessors) for b in cfg.blocks.values()}
    succ = {b.id: list(b.successors) for b in cfg.blocks.values()}
    rpo = _rpo(cfg, cfg.entry_id, succ)
    order = rpo + [n for n in cfg.blocks if n not in rpo]

    def transfer(bid: int, incoming: dict[str, set[tuple[str, int]]]) -> dict[str, set[tuple[str, int]]]:
        state: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for k, v in incoming.items():
            state[k] = set(v)
        for d in seq[bid]:
            if d.weak:
                state[d.name].add((d.name, d.line))
            else:
                state[d.name] = {(d.name, d.line)}
        return state

    changed = True
    while changed:
        changed = False
        for bid in order:
            if bid == cfg.entry_id:
                merged: dict[str, set[tuple[str, int]]] = defaultdict(set)
            else:
                merged = defaultdict(set)
                for p in preds.get(bid, ()):
                    for k, v in out[p].items():
                        merged[k] |= v
            new_in = {k: set(v) for k, v in merged.items()}
            new_out = transfer(bid, merged)
            if new_in != dict(in_[bid]) or new_out != dict(out[bid]):
                in_[bid] = defaultdict(set, new_in)
                out[bid] = defaultdict(set, new_out)
                changed = True

    return ReachingDefinitions(
        in_={b: dict(m) for b, m in in_.items()},
        out={b: dict(m) for b, m in out.items()},
        all_defs=dict(all_defs),
    )


def use_def_chains(cfg: CFG, rdefs: ReachingDefinitions) -> UseDefChains:
    """Resolve each use site to the def sites that reach it.

    Walks each block's items in order, starting from the block's ``in`` map —
    so a def earlier in the same block correctly shadows the incoming defs.
    """
    use_to_defs: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for bid in cfg.blocks:
        state: dict[str, set[tuple[str, int]]] = defaultdict(set)
        for k, v in rdefs.in_.get(bid, {}).items():
            state[k] = set(v)
        for item in cfg.blocks[bid].statements:
            defs, uses = cfg.effects(item)
            for u in uses:
                key = (u.name, u.line)
                use_to_defs.setdefault(key, set())
                reaching = state.get(u.name)
                if reaching:
                    use_to_defs[key] |= reaching
            for d in defs:
                if d.weak:
                    state[d.name].add((d.name, d.line))
                else:
                    state[d.name] = {(d.name, d.line)}

    def_to_uses: dict[tuple[str, int], set[tuple[str, int]]] = defaultdict(set)
    for use_key, def_keys in use_to_defs.items():
        for dk in def_keys:
            def_to_uses[dk].add(use_key)
    return UseDefChains(use_to_defs=use_to_defs, def_to_uses=dict(def_to_uses))


# ---------------------------------------------------------------------------
# Slicing.
# ---------------------------------------------------------------------------


def _items_by_def_key(cfg: CFG) -> dict[tuple[str, int], list[ast.AST]]:
    out: dict[tuple[str, int], list[ast.AST]] = defaultdict(list)
    for bid in sorted(cfg.blocks):
        for item in cfg.blocks[bid].statements:
            defs, _ = cfg.effects(item)
            for d in defs:
                out[(d.name, d.line)].append(item)
    return dict(out)


def _items_by_use_key(cfg: CFG) -> dict[tuple[str, int], list[ast.AST]]:
    out: dict[tuple[str, int], list[ast.AST]] = defaultdict(list)
    for bid in sorted(cfg.blocks):
        for item in cfg.blocks[bid].statements:
            _, uses = cfg.effects(item)
            for u in uses:
                out[(u.name, u.line)].append(item)
    return dict(out)


def _backward_included(
    cfg: CFG,
    ud: UseDefChains,
    seed_items: Iterable[ast.AST],
    control_deps: set[tuple[int, int, str]],
    seed_vars: set[str] | None = None,
) -> set[int]:
    """Included item ids for a backward slice over explicit seed items.

    When ``seed_vars`` is given, only those uses are followed from the seed
    items; every subsequently included item contributes all of its own uses
    (standard statement-granularity slice). Control dependence pulls in the
    predicate items controlling each included block, recursively.
    """
    def_items = _items_by_def_key(cfg)
    block_items = {b.id: list(b.statements) for b in cfg.blocks.values()}

    included: set[int] = set()  # id(item)
    seeds = list(seed_items)
    worklist: list[ast.AST] = list(seeds)
    seed_ids = {id(s) for s in seeds}

    while worklist:
        item = worklist.pop()
        iid = id(item)
        if iid in included:
            continue
        included.add(iid)
        defs, uses = cfg.effects(item)
        # Data dependence: follow reaching defs of this item's uses.
        for u in uses:
            if iid in seed_ids and seed_vars is not None and u.name not in seed_vars:
                continue
            for dk in ud.use_to_defs.get((u.name, u.line), ()):
                worklist.extend(def_items.get(dk, ()))
        # Control dependence: the predicate items of every block this item's
        # block is control-dependent on.
        blk = cfg.item_block.get(iid)
        if blk is None:
            continue
        for dep, ctrl, _label in control_deps:
            if dep == blk:
                worklist.extend(block_items.get(ctrl, ()))
    return included


def _forward_included(
    cfg: CFG,
    ud: UseDefChains,
    seed_items: Iterable[ast.AST],
    control_deps: set[tuple[int, int, str]],
    seed_vars: set[str] | None = None,
) -> set[int]:
    """Included item ids for a forward slice over explicit seed items —
    def -> uses via the chains, plus everything control-dependent on
    included predicate blocks."""
    use_items = _items_by_use_key(cfg)
    block_items = {b.id: list(b.statements) for b in cfg.blocks.values()}

    included: set[int] = set()
    seeds = list(seed_items)
    worklist: list[ast.AST] = list(seeds)
    seed_ids = {id(s) for s in seeds}

    while worklist:
        item = worklist.pop()
        iid = id(item)
        if iid in included:
            continue
        included.add(iid)
        defs, _uses = cfg.effects(item)
        for d in defs:
            if iid in seed_ids and seed_vars is not None and d.name not in seed_vars:
                continue
            for uk in ud.def_to_uses.get((d.name, d.line), ()):
                worklist.extend(use_items.get(uk, ()))
        blk = cfg.item_block.get(iid)
        if blk is None:
            continue
        for dep, ctrl, _label in control_deps:
            if ctrl == blk:
                worklist.extend(block_items.get(dep, ()))
    return included


def _included_lines(cfg: CFG, included: set[int]) -> set[int]:
    """Statement start lines of included items — the slice's line set."""
    lines: set[int] = set()
    for iid in included:
        node = _id_item(cfg, iid)
        if node is not None and getattr(node, "lineno", None):
            lines.add(node.lineno)
    return lines


def backward_slice(
    cfg: CFG,
    ud: UseDefChains,
    criterion: tuple[int, Iterable[str] | None],
    control_deps: set[tuple[int, int, str]] | None = None,
) -> set[int]:
    """Statement lines feeding ``criterion = (line, vars)``.

    Seeds = items covering ``line``. When ``vars`` is given, only those uses
    are followed from the seed items; every subsequently included item
    contributes all of its own uses (standard statement-granularity slice).
    Control dependence pulls in the predicate items controlling each included
    block, recursively.
    """
    line, vars_ = criterion
    if control_deps is None:
        control_deps = control_dependence(cfg)
    seeds = cfg.statements_at_line(line)
    if not seeds:
        raise CFGAnalysisError(f"no statement covers line {line}")
    included = _backward_included(
        cfg, ud, seeds, control_deps, set(vars_) if vars_ else None
    )
    return _included_lines(cfg, included)


def forward_slice(
    cfg: CFG,
    ud: UseDefChains,
    criterion: tuple[int, Iterable[str] | None],
    control_deps: set[tuple[int, int, str]] | None = None,
) -> set[int]:
    """Statement lines affected by ``criterion`` — symmetric to backward:
    def -> uses via the chains, plus everything control-dependent on included
    predicate blocks."""
    line, vars_ = criterion
    if control_deps is None:
        control_deps = control_dependence(cfg)
    seeds = cfg.statements_at_line(line)
    if not seeds:
        raise CFGAnalysisError(f"no statement covers line {line}")
    included = _forward_included(
        cfg, ud, seeds, control_deps, set(vars_) if vars_ else None
    )
    return _included_lines(cfg, included)


def _id_item(cfg: CFG, iid: int) -> ast.AST | None:
    for blk in cfg.blocks.values():
        for item in blk.statements:
            if id(item) == iid:
                return item
    return None


# ---------------------------------------------------------------------------
# Top-level driver.
# ---------------------------------------------------------------------------


def _find_function(tree: ast.AST, name: str, class_name: str | None) -> ast.AST:
    if class_name:
        scope = tree
        for part in class_name.split("."):
            found = None
            for node in ast.walk(scope):
                if isinstance(node, ast.ClassDef) and node.name == part:
                    found = node
                    break
            if found is None:
                raise CFGAnalysisError(f"class {part!r} not found (in {class_name!r})")
            scope = found
        matches = [
            n
            for n in scope.body  # type: ignore[attr-defined]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ]
    else:
        matches = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
        ]
    if not matches:
        raise CFGAnalysisError(f"function {name!r} not found")
    if len(matches) > 1:
        lines = sorted(n.lineno for n in matches)
        raise CFGAnalysisError(
            f"function {name!r} is ambiguous (definitions at lines {lines}); "
            "pass class_name or qualify the source"
        )
    return matches[0]


@dataclass(slots=True)
class FunctionAnalysis:
    """Everything the module computes for one function, plus slice methods.

    ``limitations`` is the honesty surface: any entry here means a caller
    deriving evidence from this analysis should mark that evidence incomplete.
    """

    function_name: str
    class_name: str | None
    def_line: int
    end_line: int
    cfg: CFG
    dominators: DominatorTree
    post_dominators: DominatorTree
    control_dependence: set[tuple[int, int, str]]
    reaching: ReachingDefinitions
    chains: UseDefChains
    limitations: list[str]

    def backward_slice(self, line: int, variables: Iterable[str] | None = None) -> set[int]:
        return backward_slice(self.cfg, self.chains, (line, variables), self.control_dependence)

    def forward_slice(self, line: int, variables: Iterable[str] | None = None) -> set[int]:
        return forward_slice(self.cfg, self.chains, (line, variables), self.control_dependence)

    def call_sites(self, lines: set[int] | None = None) -> list[dict[str, object]]:
        """Call sites inside ``lines`` (default: the whole function).

        Calls are never inlined — this is how a slice reports the boundary.
        """
        out: list[dict[str, object]] = []
        seen: set[tuple[int, str]] = set()
        for bid in sorted(self.cfg.blocks):
            for item in self.cfg.blocks[bid].statements:
                lineno = getattr(item, "lineno", None)
                if lineno is None:
                    continue
                end = getattr(item, "end_lineno", None) or lineno
                span = range(lineno, end + 1)
                for node in ast.walk(item):
                    if isinstance(node, ast.Call) and node.lineno in span:
                        if lines is not None and node.lineno not in lines:
                            continue
                        name = _render_target(node.func)
                        key = (node.lineno, name)
                        if key not in seen:
                            seen.add(key)
                            out.append({"line": node.lineno, "name": name})
        out.sort(key=lambda c: (c["line"], c["name"]))  # type: ignore[arg-type]
        return out


def _pipeline(
    func: ast.AST,
    function_name: str,
    class_name: str | None,
    source_lines: Sequence[str],
) -> FunctionAnalysis:
    """Full analysis pipeline over an already-located function node."""
    cfg = build_cfg(func, source_lines)
    dom = dominators(cfg)
    pdom = post_dominators(cfg)
    cd = control_dependence(cfg)
    rdefs = reaching_definitions(cfg, func)
    chains = use_def_chains(cfg, rdefs)
    return FunctionAnalysis(
        function_name=function_name,
        class_name=class_name,
        def_line=func.lineno,
        end_line=func.end_lineno or func.lineno,
        cfg=cfg,
        dominators=dom,
        post_dominators=pdom,
        control_dependence=cd,
        reaching=rdefs,
        chains=chains,
        limitations=list(cfg.limitations),
    )


def analyze_function(
    source: str,
    function_name: str,
    *,
    class_name: str | None = None,
) -> FunctionAnalysis:
    """Parse ``source``, locate ``function_name`` (a method when ``class_name``
    is given — dotted paths like ``"Outer.Inner"`` work), and run the full
    analysis pipeline. Line numbers are real file line numbers throughout."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CFGAnalysisError(f"source does not parse: {exc}") from exc
    func = _find_function(tree, function_name, class_name)
    return _pipeline(func, function_name, class_name, source.splitlines())


def slice_at_line(
    source: str,
    function_name: str,
    line: int,
    direction: str = "backward",
    *,
    class_name: str | None = None,
    variables: Iterable[str] | None = None,
) -> dict[str, object]:
    """Public convenience entry: slice ``function_name`` at ``line``.

    Returns ``{"lines", "variables", "blocks", "limitations", "call_sites"}``
    — all sorted/deterministic. ``call_sites`` marks the interprocedural
    boundary honestly: calls in the slice are reported, never inlined.
    """
    analysis = analyze_function(source, function_name, class_name=class_name)
    if direction == "backward":
        lines = analysis.backward_slice(line, variables)
    elif direction == "forward":
        lines = analysis.forward_slice(line, variables)
    else:
        raise CFGAnalysisError(f"direction must be 'backward' or 'forward', got {direction!r}")

    items: list[ast.AST] = []
    blocks: set[int] = set()
    for bid in sorted(analysis.cfg.blocks):
        for item in analysis.cfg.blocks[bid].statements:
            lineno = getattr(item, "lineno", None)
            if lineno in lines:
                items.append(item)
                blocks.add(bid)
    var_names: set[str] = set()
    for item in items:
        defs, uses = analysis.cfg.effects(item)
        var_names.update(d.name for d in defs)
        var_names.update(u.name for u in uses)

    return {
        "lines": sorted(lines),
        "variables": sorted(var_names),
        "blocks": sorted(blocks),
        "limitations": list(analysis.limitations),
        "call_sites": analysis.call_sites(lines),
    }


# ---------------------------------------------------------------------------
# Interprocedural slice composition (HAR-90 items 8-9).
#
# Bounded, honest composition over the intraprocedural substrate: call sites
# inside a slice are resolved through a caller-supplied index (this module
# stays I/O-free), actual argument expressions bind to callee formals by
# position and keyword name, and the callee is sliced at its return lines
# (backward) or from the bound formals' use sites (forward). Composition is a
# *summary* — name-matched, never type-proven — so every composed hop appends
# ``interprocedural_summary`` alongside the callee's own limitations.
# ---------------------------------------------------------------------------


class CalleeResolver(Protocol):
    """Callee candidates for one call site.

    Called with ``(file, function, call_line)`` — the file and function that
    contain the call and the call's own source line. Returns an iterable of
    ``(file, function, def_line, end_line)`` target tuples. Implementations
    are supplied by the caller (e.g. backed by graph.db ``CALLS`` edges);
    this module performs no I/O itself.
    """

    def __call__(
        self, file: str, function: str, call_line: int
    ) -> Iterable[tuple[str, str, int, int]]: ...


def _return_lines(func: ast.AST | None) -> list[int]:
    """Sorted lines of ``func``'s own ``return`` statements.

    Nested defs/classes are separate scopes — their returns are not
    descended into.
    """
    lines: set[int] = set()
    if func is None:
        return []
    stack: list[ast.AST] = list(getattr(func, "body", ()))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Return):
            lines.add(node.lineno)
            continue
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return sorted(lines)


def _last_statement_line(cfg: CFG) -> int:
    """The deepest statement line in ``cfg`` — the implicit-return line for
    functions with no ``return`` statement."""
    best = cfg.def_line
    for blk in cfg.blocks.values():
        for item in blk.statements:
            if item is cfg.func_node:
                continue
            lineno = getattr(item, "lineno", None)
            if lineno:
                best = max(best, lineno)
    return best


def _covered_lines(cfg: CFG, included: set[int]) -> set[int]:
    """Every source line spanned by an included item — multi-line statements
    cover each line of their range, not just their start."""
    out: set[int] = set()
    for blk in cfg.blocks.values():
        for item in blk.statements:
            if id(item) not in included:
                continue
            lineno = getattr(item, "lineno", None)
            if lineno is None:
                continue
            end = getattr(item, "end_lineno", None) or lineno
            out.update(range(lineno, end + 1))
    return out


def _call_nodes_in_lines(
    analysis: FunctionAnalysis, covered: set[int]
) -> list[tuple[int, int, str, ast.Call, ast.AST]]:
    """Call sites inside a slice region: ``(line, col, rendered_name,
    call_node, owning_item)``, sorted by position then name — the same
    boundary ``FunctionAnalysis.call_sites`` reports, but with the nodes
    needed for argument mapping."""
    out: list[tuple[int, int, str, ast.Call, ast.AST]] = []
    seen: set[int] = set()
    for bid in sorted(analysis.cfg.blocks):
        for item in analysis.cfg.blocks[bid].statements:
            if item is analysis.cfg.func_node:
                continue  # pseudo-item spans the whole def — never a call site
            for node in ast.walk(item):
                if (
                    isinstance(node, ast.Call)
                    and node.lineno in covered
                    and id(node) not in seen
                ):
                    seen.add(id(node))
                    out.append(
                        (
                            node.lineno,
                            node.col_offset,
                            _render_target(node.func),
                            node,
                            item,
                        )
                    )
    out.sort(key=lambda c: (c[0], c[1], c[2]))
    return out


def _map_actuals(
    call: ast.Call, callee_func: ast.AST
) -> tuple[dict[str, ast.expr], bool, int]:
    """Bind actual argument expressions to callee formals.

    Positional actuals bind by order to positional formals (posonly +
    regular); ``name=`` actuals bind by parameter name (positional or
    keyword-only). ``*args``/``**kwargs`` cannot be bound to provable
    formals — they set the spread flag and are skipped, as is every
    positional after the first ``*`` (positions unknowable statically).
    Returns ``(mapping, spread_seen, surplus_count)``.
    """
    args: ast.arguments = callee_func.args  # type: ignore[attr-defined]
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    all_names = _arg_names(args)
    mapping: dict[str, ast.expr] = {}
    spread = False
    surplus = 0
    pos = 0
    for actual in call.args:
        if isinstance(actual, ast.Starred):
            spread = True
            break
        if pos < len(positional):
            mapping.setdefault(positional[pos], actual)
        else:
            surplus += 1
        pos += 1
    for kw in call.keywords:
        if kw.arg is None:
            spread = True  # **kwargs — no provable formal binding
            continue
        if kw.arg in all_names:
            mapping.setdefault(kw.arg, kw.value)
        else:
            surplus += 1
    return mapping, spread, surplus


def _formal_reached(
    analysis: FunctionAnalysis, formal: str, covered: set[int]
) -> bool:
    """True when ``formal``'s parameter def (anchored at the callee's def
    line) reaches a use inside ``covered`` — i.e. the callee slice
    demonstrably flows this input to its criterion."""
    def_line = analysis.def_line
    for (uname, uline), def_keys in analysis.chains.use_to_defs.items():
        if uname == formal and uline in covered and (formal, def_line) in def_keys:
            return True
    return False


def interprocedural_slice(
    source_texts: Mapping[str, str],
    graph_index: CalleeResolver | None,
    entry_file: str,
    entry_function: str,
    line: int,
    direction: str = "backward",
    *,
    max_depth: int = 3,
    max_hops: int | None = None,
    entry_def_line: int | None = None,
) -> dict[str, object]:
    """Bounded interprocedural slice composition.

    ``source_texts`` maps file paths to their text — the only input the
    composition reads. ``graph_index`` resolves one call site
    ``(file, function, call_line)`` to candidate ``(file, function,
    def_line, end_line)`` targets. Both are caller-supplied so the module
    stays I/O-free and deterministic.

    Backward: the entry slice is computed at ``line``; for every call site
    inside it, actuals bind to callee formals (position, then ``name=``;
    ``*args``/``**kwargs`` mark ``spread_unmapped`` and skip binding), the
    callee is backward-sliced at its return/last lines, and the mapped
    formals the return slice actually reaches map back to the call site's
    actual variables — merged into the caller's slice as dependency lines of
    the call site.

    Forward: symmetric — a call site inside the slice forward-slices the
    callee from the bound formals' use sites; formals whose effect region
    reaches a return line report ``reaches_return``, and the caller's
    result-assignment propagation (``x = f()`` -> ``x``'s uses) is carried
    by the intraprocedural slice.

    Bounds: ``max_depth`` caps hop depth (``max_depth_cut``), a visited set
    on ``(function, call_line, callee)`` hop identity cuts cycles
    (``recursion_cut``), ``max_hops`` caps total composed hops
    (``hop_budget`` + ``truncated``). Every composed hop appends
    ``interprocedural_summary`` plus the callee's own limitations.

    Returns ``{"entry", "per_file", "cross_function", "limitations",
    "truncated"}`` — deterministic: sorted output, stable ordering, same
    limitations on repeat runs.
    """
    if direction not in {"backward", "forward"}:
        raise CFGAnalysisError(
            f"direction must be 'backward' or 'forward', got {direction!r}"
        )
    try:
        max_depth = int(max_depth)
    except (TypeError, ValueError):
        max_depth = 3

    limitations: list[str] = []

    def _lim(key: str) -> None:
        if key not in limitations:
            limitations.append(key)

    # -- caller-supplied authorities, cached so repeat lookups are stable ---
    _trees: dict[str, ast.AST | None] = {}
    _funcs: dict[tuple[str, str, int], ast.AST | None] = {}
    _analyses: dict[tuple[str, str, int], FunctionAnalysis | None] = {}

    def _text(file: str) -> str | None:
        get = getattr(source_texts, "get", None)
        if get is not None:
            try:
                return get(file)
            except Exception:
                return None
        try:
            return source_texts[file]  # type: ignore[index]
        except Exception:
            return None

    def _tree(file: str) -> ast.AST | None:
        if file not in _trees:
            text = _text(file)
            parsed: ast.AST | None = None
            if text is not None:
                try:
                    parsed = ast.parse(text)
                except SyntaxError:
                    parsed = None
            _trees[file] = parsed
        return _trees[file]

    def _func_at(file: str, function: str, def_line: int) -> ast.AST | None:
        key = (file, function, def_line)
        if key not in _funcs:
            node = None
            tree = _tree(file)
            if tree is not None:
                exact = [
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.lineno == def_line
                    and n.name == function
                ]
                if exact:
                    node = exact[0]
                else:
                    try:
                        node = _find_function(tree, function, None)
                    except CFGAnalysisError:
                        node = None
            _funcs[key] = node
        return _funcs[key]

    def _analysis(
        file: str, function: str, def_line: int
    ) -> FunctionAnalysis | None:
        key = (file, function, def_line)
        if key not in _analyses:
            node = _func_at(file, function, def_line)
            try:
                _analyses[key] = (
                    _pipeline(
                        node, function, None, (_text(file) or "").splitlines()
                    )
                    if node is not None
                    else None
                )
            except CFGAnalysisError:
                _analyses[key] = None
        return _analyses[key]

    # -- entry --------------------------------------------------------------
    if entry_def_line is not None:
        entry_node = _func_at(entry_file, entry_function, int(entry_def_line))
        if entry_node is None:
            raise CFGAnalysisError(
                f"entry function {entry_function!r} not found at"
                f" {entry_file}:{entry_def_line}"
            )
    else:
        tree = _tree(entry_file)
        if tree is None:
            raise CFGAnalysisError(
                f"entry source unavailable or unparsable: {entry_file}"
            )
        entry_node = _find_function(tree, entry_function, None)
    entry_analysis = _pipeline(
        entry_node,
        entry_function,
        None,
        (_text(entry_file) or "").splitlines(),
    )
    _funcs[(entry_file, entry_function, entry_node.lineno)] = entry_node
    _analyses[(entry_file, entry_function, entry_node.lineno)] = entry_analysis

    per_file: dict[str, set[int]] = defaultdict(set)
    cross: list[dict[str, object]] = []
    visited: set[tuple[str, str, int, str, str]] = set()
    truncated = False

    # Worklist entries: (file, function, def_line, kind, payload, depth, hop).
    #   "entry"  -> payload = criterion line (int)
    #   "callee" -> payload = (mapping formal->actual expr,
    #               (caller_file, caller_fn, caller_def_line, call_line))
    worklist: list[tuple[str, str, int, str, object, int, dict | None]] = [
        (
            entry_file,
            entry_function,
            entry_node.lineno,
            "entry",
            int(line),
            0,
            None,
        )
    ]
    while worklist:
        file, fn, def_line, kind, payload, depth, hop = worklist.pop()
        analysis = _analysis(file, fn, def_line)
        if analysis is None:
            _lim(f"callee_analysis_failed:{fn}")
            if hop is not None:
                hop["callee_slice_lines"] = []
            continue
        for lim in analysis.limitations:
            _lim(lim)

        forward_reached: list[str] = []
        if kind == "entry":
            seeds = analysis.cfg.statements_at_line(int(payload))  # type: ignore[arg-type]
            if not seeds:
                raise CFGAnalysisError(f"no statement covers line {payload}")
            if direction == "backward":
                included = _backward_included(
                    analysis.cfg, analysis.chains, seeds, analysis.control_dependence
                )
            else:
                included = _forward_included(
                    analysis.cfg, analysis.chains, seeds, analysis.control_dependence
                )
        elif direction == "backward":
            # Callee criterion: its return value — seed at every return line,
            # or the last statement for implicit-return functions.
            func = _func_at(file, fn, def_line)
            seed_lines = _return_lines(func) or [_last_statement_line(analysis.cfg)]
            seeds = [
                item
                for rl in seed_lines
                for item in analysis.cfg.statements_at_line(rl)
            ]
            included = _backward_included(
                analysis.cfg, analysis.chains, seeds, analysis.control_dependence
            )
        else:
            # Forward callee: the bound formals' use sites seed the slice —
            # what the incoming arguments can affect inside the callee.
            mapping = payload[0]  # type: ignore[index]
            func = _func_at(file, fn, def_line)
            ret_set = set(_return_lines(func)) or {
                _last_statement_line(analysis.cfg)
            }
            use_items = _items_by_use_key(analysis.cfg)
            included = set()
            for formal in sorted(mapping):
                f_seeds: list[ast.AST] = []
                for uk in analysis.chains.def_to_uses.get((formal, def_line), ()):
                    f_seeds.extend(use_items.get(uk, ()))
                f_inc = _forward_included(
                    analysis.cfg,
                    analysis.chains,
                    f_seeds,
                    analysis.control_dependence,
                )
                if _covered_lines(analysis.cfg, f_inc) & ret_set:
                    forward_reached.append(formal)
                included |= f_inc

        lines = _included_lines(analysis.cfg, included)
        covered = _covered_lines(analysis.cfg, included)
        per_file[file] |= lines

        if kind == "callee" and hop is not None:
            mapping, caller = payload  # type: ignore[misc]
            if direction == "backward":
                reached = sorted(
                    f for f in mapping if _formal_reached(analysis, f, covered)
                )
                hop["reached_formals"] = reached
                # Reached formals' actuals are dependencies of the call site:
                # merge their caller-side def lines into the caller's slice.
                # (Statement granularity already includes them — the merge is
                # recorded explicitly rather than assumed.)
                caller_analysis = _analysis(caller[0], caller[1], caller[2])
                if caller_analysis is not None:
                    def_items = _items_by_def_key(caller_analysis.cfg)
                    for formal in reached:
                        _d, uses = _loads(mapping[formal])
                        for u in uses:
                            for dk in caller_analysis.chains.use_to_defs.get(
                                (u.name, u.line), ()
                            ):
                                for it in def_items.get(dk, ()):
                                    lineno = getattr(it, "lineno", None)
                                    if lineno:
                                        per_file[caller[0]].add(lineno)
            else:
                hop["reached_formals"] = forward_reached
                hop["reaches_return"] = bool(forward_reached)
            hop["callee_slice_lines"] = sorted(lines)

        # -- compose deeper hops from call sites inside this slice ----------
        call_sites = _call_nodes_in_lines(analysis, covered)
        if not call_sites:
            continue
        if depth >= max_depth:
            _lim("max_depth_cut")
            continue
        if graph_index is None:
            _lim("callee_index_unavailable")
            continue
        for cline, _col, cname, cnode, citem in call_sites:
            try:
                raw = list(graph_index(file, fn, cline))
            except Exception:
                _lim("callee_resolution_failed")
                continue
            targets: list[tuple[str, str, int, int]] = []
            for t in raw:
                try:
                    targets.append(
                        (str(t[0]), str(t[1]), int(t[2]), int(t[3]))
                    )
                except (TypeError, ValueError, IndexError):
                    _lim("callee_target_malformed")
            tail = cname.rsplit(".", 1)[-1]
            named = [t for t in targets if t[1] == tail]
            if targets and not named:
                _lim("call_target_ambiguous")
                named = targets
            if not named:
                _lim(f"callee_unresolved:{cname}")
                continue
            for c_file, c_fn, c_def, _c_end in sorted(set(named)):
                if max_hops is not None and len(cross) >= max_hops:
                    truncated = True
                    _lim("hop_budget")
                    break
                hkey = (file, fn, cline, c_file, c_fn)
                if hkey in visited:
                    _lim("recursion_cut")
                    continue
                visited.add(hkey)
                c_func = _func_at(c_file, c_fn, c_def)
                if c_func is None:
                    _lim(f"callee_source_unavailable:{c_file}")
                    continue
                cmap, spread, surplus = _map_actuals(cnode, c_func)
                if spread:
                    _lim("spread_unmapped")
                if surplus:
                    _lim("actuals_unmapped")
                _lim("interprocedural_summary")
                c_defs, _c_uses = analysis.cfg.effects(citem)
                hop2: dict[str, object] = {
                    "caller_file": file,
                    "caller_fn": fn,
                    "call_line": cline,
                    "call_name": cname,
                    "callee_file": c_file,
                    "callee_fn": c_fn,
                    "callee_def_line": c_def,
                    "mapped_vars": {
                        f: _render_target(a) for f, a in sorted(cmap.items())
                    },
                    "result_vars": sorted({d.name for d in c_defs}),
                    "callee_slice_lines": [],
                }
                cross.append(hop2)
                worklist.append(
                    (
                        c_file,
                        c_fn,
                        c_def,
                        "callee",
                        (cmap, (file, fn, def_line, cline)),
                        depth + 1,
                        hop2,
                    )
                )

    cross.sort(
        key=lambda h: (
            str(h["caller_file"]),
            str(h["caller_fn"]),
            int(h["call_line"]),  # type: ignore[arg-type]
            str(h["callee_file"]),
            str(h["callee_fn"]),
        )
    )
    return {
        "entry": {
            "file": entry_file,
            "function": entry_function,
            "line": int(line),
            "direction": direction,
            "max_depth": max_depth,
        },
        "per_file": {p: sorted(ls) for p, ls in sorted(per_file.items())},
        "cross_function": cross,
        "limitations": limitations,
        "truncated": truncated,
    }


__all__ = [
    "BasicBlock",
    "CFG",
    "CFGAnalysisError",
    "CalleeResolver",
    "DominatorTree",
    "FunctionAnalysis",
    "ReachingDefinitions",
    "UseDefChains",
    "analyze_function",
    "backward_slice",
    "build_cfg",
    "control_dependence",
    "dominators",
    "forward_slice",
    "interprocedural_slice",
    "post_dominators",
    "reaching_definitions",
    "slice_at_line",
    "use_def_chains",
]
