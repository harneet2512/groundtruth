"""DB-backed statement-level CFG analysis over the persisted ``cfg_*`` sidecar.

Complement to :mod:`groundtruth.runtime.cfg_analysis`, which analyzes Python
source via ``ast``.  This module consumes the raw per-function CFG the Go
indexer persists into graph.db — ``cfg_blocks`` / ``cfg_edges`` / ``cfg_defs``
(schema in ``gt-index/internal/store/cfg.go``) — and composes dominators,
post-dominators, control dependence, reaching definitions, and
statement-level backward/forward slices on top, at basic-block granularity.
It exists so the ``slice`` typed producer can answer for
javascript/typescript/java/go without re-deriving structure the indexer
already proved.

Provenance / honesty rules (same discipline as cfg_analysis.py):

- Persisted rows are ground truth for STRUCTURE: ``cfg_blocks`` +
  ``cfg_edges`` are the real block graph and ``cfg_defs`` rows are real def
  sites (declarations, assignments, updates, loop/catch/param bindings).
  The dominator/control-dependence/reaching-definition algorithms are
  reused verbatim from ``cfg_analysis`` — they are graph-generic.
- There is NO persisted use table.  Per-statement uses are approximated by
  scanning the statement's source lines for identifier tokens — string
  literals and comments stripped, keywords and common builtins excluded,
  member selectors folded into dotted chains, and each persisted def's own
  occurrence subtracted (an ``x =`` LHS is not a use; an augmented/update
  statement's target still reads, so ``+=``/``++`` and friends keep it).
  Every result therefore carries ``approximate_use_detection``.
- Def strength is not persisted either: ``cfg_defs`` names containing ``.``
  or ``[`` are treated as weak (mutation, not rebinding), and subscript
  targets additionally weak-define their base, mirroring the Python
  analyzer — ``approximate_def_strength``.
- Slices are intraprocedural only.  Calls are detected by the same text
  scan (identifier immediately before ``(``), reported under
  ``call_sites``, never inlined — ``call_sites_not_inlined``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .cfg_analysis import (
    CFGAnalysisError,
    DominatorTree,
    ReachingDefinitions,
    UseDefChains,
    control_dependence,
    dominators,
    post_dominators,
    reaching_definitions,
    use_def_chains,
)


# Item = one anchored statement site: (block_index, statement start line).
_Item = tuple[int, int]


@dataclass(frozen=True, slots=True)
class _Def:
    """One persisted def site. ``weak`` mirrors cfg_analysis: mutation targets
    (``a.b``, ``a[i]``) do not kill prior definitions of the same name."""

    name: str
    line: int
    weak: bool = False


@dataclass(frozen=True, slots=True)
class _Use:
    name: str
    line: int


@dataclass(slots=True)
class StoredBlock:
    """One persisted basic block.  Field names mirror
    ``cfg_analysis.BasicBlock`` so the dominator/dataflow algorithms run
    unchanged: ``id`` is the persisted ``block_index``, ``statements`` is the
    block's item list — ``(block_index, statement_line)`` tuples in source
    order."""

    id: int
    kind: str = "block"
    statement_lines: tuple[int, ...] = ()
    start_line: int = 0
    end_line: int = 0
    successors: list[int] = field(default_factory=list)
    predecessors: list[int] = field(default_factory=list)
    statements: list[_Item] = field(default_factory=list)


@dataclass(slots=True)
class StoredCFG:
    """Control-flow graph over one function, rebuilt from cfg_* rows.

    Duck-type compatible with ``cfg_analysis.CFG`` for everything the
    ported algorithms touch: ``blocks``, ``entry_id``, ``exit_id``,
    ``edges``, ``item_block``, and ``effects(item)``."""

    blocks: dict[int, StoredBlock]
    entry_id: int
    exit_id: int
    edges: list[tuple[int, int, str]]
    item_block: dict[_Item, int]
    item_effects: dict[_Item, tuple[list[_Def], list[_Use]]]
    item_span: dict[_Item, tuple[int, int]]
    item_scan_lines: dict[_Item, tuple[int, ...]]
    function_name: str
    def_line: int
    params: tuple[str, ...] = ()
    limitations: list[str] = field(default_factory=list)

    def effects(self, item: _Item) -> tuple[list[_Def], list[_Use]]:
        return self.item_effects.get(item, ([], []))

    def statements_at_line(self, line: int) -> list[_Item]:
        """Items whose source span covers ``line``.  Deterministic order:
        block id, then item position.  Entry-block items (the signature
        pseudo-item) never seed — same rule as cfg_analysis, where the
        function node item is skipped."""
        out: list[_Item] = []
        for bid in sorted(self.blocks):
            blk = self.blocks[bid]
            if blk.kind == "entry":
                continue
            for item in blk.statements:
                lo, hi = self.item_span.get(item, (item[1], item[1]))
                if lo <= line <= hi:
                    out.append(item)
        return out


# ---------------------------------------------------------------------------
# Loading: cfg_* rows -> StoredCFG.
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def has_persisted_cfg(conn: sqlite3.Connection, node_id: int) -> bool:
    """True when cfg_blocks carries at least one row for ``node_id``.

    A graph.db that predates the cfg_* sidecar (tables absent) or that has
    none for this function reads the same way: no persisted CFG."""
    try:
        if not _table_exists(conn, "cfg_blocks"):
            return False
        row = conn.execute(
            "SELECT 1 FROM cfg_blocks WHERE node_id = ? LIMIT 1",
            (int(node_id),),
        ).fetchone()
    except (sqlite3.Error, TypeError, ValueError):
        return False
    return row is not None


def _parse_statement_lines(raw: object, node_id: int, block_index: int) -> tuple[int, ...]:
    if raw is None:
        return ()
    try:
        data = json.loads(str(raw))
    except (ValueError, TypeError) as exc:
        raise CFGAnalysisError(
            f"cfg_blocks.statement_lines is not JSON for node {node_id}"
            f" block {block_index}: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise CFGAnalysisError(
            f"cfg_blocks.statement_lines is not a JSON array for node {node_id}"
            f" block {block_index}"
        )
    out: list[int] = []
    for v in data:
        try:
            iv = int(v)
        except (TypeError, ValueError) as exc:
            raise CFGAnalysisError(
                f"cfg_blocks.statement_lines has a non-integer entry for node"
                f" {node_id} block {block_index}"
            ) from exc
        if iv > 0:
            out.append(iv)
    return tuple(sorted(set(out)))


def load_stored_cfg(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    source_lines: Sequence[str] = (),
    function_name: str = "",
    language: str = "",
) -> StoredCFG:
    """Rebuild one function's CFG from its persisted cfg_* rows.

    ``source_lines`` is the owning file's text split into lines — required
    for approximate use detection (there is no persisted use table).
    Raises ``CFGAnalysisError`` when the function has no persisted CFG or
    the rows are malformed; callers treat that as "no evidence".
    """
    try:
        block_rows = conn.execute(
            "SELECT block_index, kind, COALESCE(start_line, 0),"
            " COALESCE(end_line, 0), statement_lines"
            " FROM cfg_blocks WHERE node_id = ? ORDER BY block_index",
            (int(node_id),),
        ).fetchall()
        edge_rows = conn.execute(
            "SELECT from_block, to_block, COALESCE(label, '')"
            " FROM cfg_edges WHERE node_id = ?"
            " ORDER BY from_block, to_block, label",
            (int(node_id),),
        ).fetchall()
        def_rows = conn.execute(
            "SELECT block_index, var_name, COALESCE(line, 0)"
            " FROM cfg_defs WHERE node_id = ?"
            " ORDER BY block_index, line, var_name",
            (int(node_id),),
        ).fetchall()
    except sqlite3.Error as exc:
        raise CFGAnalysisError(f"cfg tables unavailable for node {node_id}: {exc}") from exc
    if not block_rows:
        raise CFGAnalysisError(f"no persisted CFG for node {node_id}")

    limitations: list[str] = []

    def _lim(key: str) -> None:
        if key not in limitations:
            limitations.append(key)

    blocks: dict[int, StoredBlock] = {}
    for block_index, kind, start_line, end_line, raw_lines in block_rows:
        bid = int(block_index)
        if bid in blocks:
            raise CFGAnalysisError(
                f"duplicate cfg_blocks block_index {bid} for node {node_id}"
            )
        blocks[bid] = StoredBlock(
            id=bid,
            kind=str(kind or "block"),
            statement_lines=_parse_statement_lines(raw_lines, node_id, bid),
            start_line=int(start_line or 0),
            end_line=int(end_line or 0),
        )

    entries = [b for b in blocks.values() if b.kind == "entry"]
    exits = [b for b in blocks.values() if b.kind == "exit"]
    if len(entries) != 1 or len(exits) != 1:
        raise CFGAnalysisError(
            f"malformed persisted CFG for node {node_id}:"
            f" {len(entries)} entry / {len(exits)} exit blocks"
        )
    entry_id = entries[0].id
    exit_id = exits[0].id

    edges: list[tuple[int, int, str]] = []
    for from_block, to_block, label in edge_rows:
        a, b, lab = int(from_block), int(to_block), str(label or "")
        if a not in blocks or b not in blocks:
            _lim("dangling_cfg_edge")
            continue
        edges.append((a, b, lab))
        if b not in blocks[a].successors:
            blocks[a].successors.append(b)
        if a not in blocks[b].predecessors:
            blocks[b].predecessors.append(a)

    # Persisted defs per block; compound names (a.b / a[i]) are weak —
    # mutation, not rebinding.  Subscript targets additionally weak-define
    # their base, mirroring cfg_analysis's Subscript handling.
    block_defs: dict[int, list[_Def]] = {bid: [] for bid in blocks}
    any_weak = False
    for block_index, var_name, line in def_rows:
        bid = int(block_index)
        name = str(var_name or "")
        if not name or bid not in blocks:
            continue
        dline = int(line or 0)
        weak = "." in name or "[" in name
        if weak:
            any_weak = True
        block_defs[bid].append(_Def(name, dline, weak=weak))
        if "[" in name:
            base = name.split("[", 1)[0]
            if base and base != name:
                block_defs[bid].append(_Def(base, dline, weak=True))
    if any_weak:
        _lim("approximate_def_strength")

    # -- items: (block_index, statement start line), spans, and scan lines ---
    item_block: dict[_Item, int] = {}
    item_span: dict[_Item, tuple[int, int]] = {}
    for bid in sorted(blocks):
        blk = blocks[bid]
        starts = sorted(set(blk.statement_lines))
        blk.statements = [(bid, ln) for ln in starts]
        for i, ln in enumerate(starts):
            item = (bid, ln)
            item_block[item] = bid
            if i + 1 < len(starts):
                hi = starts[i + 1] - 1
            else:
                hi = blk.end_line or ln
            if hi < ln:
                hi = ln
            item_span[item] = (ln, hi)

    # A statement's scan lines are its covered span minus every OTHER item's
    # start line: a compound header's span covers its nested body (faithful
    # for seeding, like the Python If/While nodes), but those body lines
    # belong to other blocks and must not be scanned as the header's uses.
    all_starts = sorted(item_block)
    all_start_lines = {ln for _bid, ln in all_starts}

    item_scan_lines: dict[_Item, tuple[int, ...]] = {}
    for item in all_starts:
        bid, ln = item
        lo, hi = item_span[item]
        if blocks[bid].kind == "entry":
            # The signature pseudo-item: persisted end_line is the def line
            # itself, but multi-line signatures run until the first body
            # statement.  Widen the scan to the earliest statement anchored
            # in any successor block.
            succ_starts = [
                min(blocks[s].statement_lines)
                for s in blocks[bid].successors
                if blocks[s].statement_lines
            ]
            if succ_starts:
                hi = max(hi, min(succ_starts) - 1)
        scan = [
            ln2
            for ln2 in range(lo, hi + 1)
            if ln2 == ln or ln2 not in all_start_lines
        ]
        item_scan_lines[item] = tuple(scan)

    clean = _clean_source(source_lines)
    non_use = _non_use_tokens(language)

    # -- attribute each persisted def to the item whose span covers it ------
    item_defs: dict[_Item, list[_Def]] = {item: [] for item in all_starts}
    for bid in sorted(blocks):
        items = blocks[bid].statements
        if not items:
            if block_defs[bid]:
                _lim("defs_without_statement")
            continue
        spans = [(item, item_span[item]) for item in items]
        for d in sorted(block_defs[bid], key=lambda dd: (dd.line, dd.name)):
            target = next(
                (item for item, (lo, hi) in spans if lo <= d.line <= hi),
                None,
            )
            if target is None:
                earlier = [item for item in items if item[1] <= d.line]
                target = earlier[-1] if earlier else items[0]
            item_defs[target].append(d)

    # Parser-exact persisted uses (schema v15.3+): the producer emits one
    # cfg_uses row per identifier read, anchored to (block, line).  When the
    # table exists it REPLACES the lexical scan — reads are compiler-adjacent
    # evidence, not regex guesses.  Attribution to items uses the same span
    # rule as defs.  Absent table (older graph) -> lexical fallback below.
    persisted_uses: list[tuple[int, str, int]] | None = None
    try:
        persisted_uses = [
            (int(b), str(n), int(l or 0))
            for b, n, l in conn.execute(
                "SELECT block_index, var_name, COALESCE(line, 0)"
                " FROM cfg_uses WHERE node_id = ?"
                " ORDER BY block_index, line, var_name",
                (int(node_id),),
            ).fetchall()
        ]
    except sqlite3.Error:
        persisted_uses = None

    item_effects: dict[_Item, tuple[list[_Def], list[_Use]]] = {}
    any_call = False
    if persisted_uses is not None:
        for item in all_starts:
            item_effects[item] = (
                sorted(item_defs[item], key=lambda d: (d.line, d.name)),
                [],
            )
        # Persisted uses attribute to the item whose span covers their line;
        # multi-line statements map to their start item (same rule as defs).
        call_re = re.compile(r"[A-Za-z_][\w.$]*\s*\(")
        for _bid, uname, uline in persisted_uses:
            if not uname or uline <= 0:
                continue
            target = next(
                (item for item in all_starts
                 if item_span[item][0] <= uline <= item_span[item][1]),
                None,
            )
            if target is None:
                earlier = [
                    item for item in all_starts if item[1] <= uline
                ]
                target = earlier[-1] if earlier else None
            if target is None:
                continue
            defs_here, uses = item_effects[target]
            uses.append(_Use(uname, uline))
            item_effects[target] = (defs_here, uses)
        # Call detection still comes from the source text — a `f(` shape on
        # any scan line marks the call-site limitation exactly as before.
        for item in all_starts:
            for ln2 in item_scan_lines[item]:
                text_line = (
                    clean[ln2 - 1] if 0 < ln2 <= len(clean) else ""
                )
                if call_re.search(text_line):
                    any_call = True
                    break
            if any_call:
                break
    else:
        for item in all_starts:
            defs_here = item_defs[item]
            uses, call_found = _scan_uses(
                item, defs_here, item_scan_lines[item], clean, non_use,
                function_name=function_name,
            )
            if call_found:
                any_call = True
            item_effects[item] = (
                sorted(defs_here, key=lambda d: (d.line, d.name)),
                uses,
            )

    # Parser-exact supplemental uses: the producer persists flow/access
    # evidence as properties — ``data_flow`` (``var -> expr``), field
    # reads (``reads: <recv>.<field>``) and mutations
    # (``mutates: <recv>.<field>``).  The left/receiver variable is
    # provably read at the property's line; merge it where the lexical
    # scan missed it.  Coverage is partial, so the flag stays — this
    # only sharpens the chains that exist.
    try:
        sup_rows = conn.execute(
            "SELECT kind, value, line FROM properties"
            " WHERE node_id = ?"
            " AND kind IN ('data_flow','field_read','side_effect')",
            (int(node_id),),
        ).fetchall()
    except sqlite3.Error:
        sup_rows = []
    line_items: dict[int, list[_Item]] = {}
    for item in all_starts:
        for ln in item_scan_lines.get(item, ()):
            line_items.setdefault(ln, []).append(item)
    for kind, value, sline in sup_rows:
        line = int(sline or 0)
        names: list[str] = []
        v = str(value).strip()
        if kind == "data_flow":
            head = v.split("->", 1)[0].strip()
            m = _CHAIN_RE.match(head)
            if m is not None:
                names.extend(_prefixes(m.group(0)))
        else:
            m = re.match(
                r"^(reads|mutates):\s*([A-Za-z_][\w.]*)", v
            )
            if m is not None:
                # reads: whole receiver chain is loaded.  mutates: the
                # field is WRITTEN — only the receiver head is a use.
                if m.group(1) == "reads":
                    names.extend(_prefixes(m.group(2)))
                else:
                    names.append(m.group(2).split(".", 1)[0])
        for name in names:
            if name in non_use:
                continue
            for item in line_items.get(line, ()):
                defs_here, uses = item_effects[item]
                if (name, line) not in {(u.name, u.line) for u in uses}:
                    uses.append(_Use(name, line))
                item_effects[item] = (defs_here, uses)

    if any_call:
        _lim("call_sites_not_inlined")

    if persisted_uses is not None:
        # Reads are parser-exact where emitted; coverage of exotic positions
        # (await exprs, decorators, nested-function bodies) is the residual.
        _lim("approximate_use_coverage")
    else:
        _lim("approximate_use_detection")

    # Unreachable blocks still get dominator sets (the ported algorithm
    # handles them), but they are named so callers can mark the evidence.
    reachable = _reachable(blocks, entry_id)
    if any(bid not in reachable for bid in blocks):
        _lim("unreachable_code")

    def_line = blocks[entry_id].start_line or (
        min(blocks[entry_id].statement_lines)
        if blocks[entry_id].statement_lines
        else 0
    )
    params = tuple(sorted({d.name for d in block_defs[entry_id]}))

    return StoredCFG(
        blocks=blocks,
        entry_id=entry_id,
        exit_id=exit_id,
        edges=edges,
        item_block=item_block,
        item_effects=item_effects,
        item_span=item_span,
        item_scan_lines=item_scan_lines,
        function_name=function_name,
        def_line=def_line,
        params=params,
        limitations=limitations,
    )


def _reachable(blocks: dict[int, StoredBlock], root: int) -> set[int]:
    seen = {root}
    stack = [root]
    while stack:
        n = stack.pop()
        for s in blocks[n].successors:
            if s not in seen:
                seen.add(s)
                stack.append(s)
    return seen


# ---------------------------------------------------------------------------
# Approximate use detection: scan the statement's own source lines.
# ---------------------------------------------------------------------------

_CHAIN_RE = re.compile(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*")
_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
# Augmented-assignment / update operators for js/ts/java/go.  `=` alone is
# not here (plain assignment is not a read); `:=` is not here (Go declare is
# not a read either).  `==`/`!=`/`<=`/`>=`/`=>` cannot match because the
# character before `=` is not in the operator class.
_AUG_RE = re.compile(
    r"\+\+|--|>>>=|<<=|>>=|&\^=|\*\*=|&&=|\|\|=|\?\?=|[-+*/%&^|~]="
)

_COMMON_KEYWORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "default",
        "break", "continue", "return", "throw", "throws", "try", "catch",
        "finally", "new", "delete", "typeof", "instanceof", "in", "of",
        "as", "is", "class", "extends", "implements", "interface", "struct",
        "enum", "import", "from", "export", "package", "const", "let", "var",
        "function", "func", "async", "await", "yield", "static", "public",
        "private", "protected", "final", "abstract", "void", "null", "nil",
        "true", "false", "goto", "defer", "go", "select", "range", "chan",
        "map", "type", "iota", "fallthrough", "synchronized", "volatile",
        "transient", "native", "strictfp", "record", "sealed", "permits",
        "assert", "readonly", "declare", "namespace", "keyof", "infer",
        "satisfies", "override", "with", "debugger", "get", "set",
    }
)

_GO_BUILTINS = frozenset(
    {
        "append", "cap", "clear", "close", "complex", "copy", "delete",
        "imag", "len", "make", "max", "min", "new", "panic", "print",
        "println", "real", "recover", "error", "string", "bool", "byte",
        "rune", "int", "int8", "int16", "int32", "int64", "uint", "uint8",
        "uint16", "uint32", "uint64", "uintptr", "float32", "float64",
        "complex64", "complex128", "any", "comparable",
    }
)

_JAVA_BUILTINS = frozenset(
    {
        "String", "Object", "Class", "System", "Math", "Thread", "Integer",
        "Long", "Double", "Float", "Boolean", "Byte", "Character", "Short",
        "Void", "Number", "StringBuilder", "StringBuffer", "Exception",
        "RuntimeException", "Throwable", "Error", "Iterable", "Iterator",
        "Collection", "List", "Map", "Set", "Queue", "Deque", "ArrayList",
        "LinkedList", "HashMap", "LinkedHashMap", "TreeMap", "HashSet",
        "TreeSet", "Optional", "Stream", "Arrays", "Collections", "Objects",
        "Override", "Deprecated", "SuppressWarnings", "FunctionalInterface",
        "SafeVarargs", "AutoCloseable", "Enum", "Comparable", "Comparator",
        "Runnable", "Callable", "StringJoiner",
    }
)

_JS_BUILTINS = frozenset(
    {
        "console", "window", "document", "globalThis", "process", "module",
        "exports", "require", "JSON", "Math", "Object", "Array", "String",
        "Number", "Boolean", "BigInt", "Symbol", "Promise", "Error",
        "TypeError", "RangeError", "SyntaxError", "ReferenceError",
        "EvalError", "AggregateError", "RegExp", "Date", "Map", "Set",
        "WeakMap", "WeakSet", "WeakRef", "Proxy", "Reflect", "Intl",
        "ArrayBuffer", "SharedArrayBuffer", "DataView", "Uint8Array",
        "Uint8ClampedArray", "Int8Array", "Uint16Array", "Int16Array",
        "Uint32Array", "Int32Array", "Float32Array", "Float64Array",
        "BigInt64Array", "BigUint64Array", "parseInt", "parseFloat",
        "isNaN", "isFinite", "undefined", "NaN", "Infinity", "fetch",
        "alert", "setTimeout", "setInterval", "setImmediate", "clearTimeout",
        "clearInterval", "queueMicrotask", "encodeURIComponent",
        "decodeURIComponent", "encodeURI", "decodeURI", "eval",
    }
)

_TS_BUILTINS = _JS_BUILTINS | frozenset(
    {"any", "unknown", "never", "object", "string", "number", "boolean",
     "bigint", "symbol", "undefined"}
)

# Per-language keyword extras on top of _COMMON_KEYWORDS — identifiers that
# are keywords in one language but usable names in another (Java primitive
# types are the main family: ``int``/``long``/``boolean`` … are keywords in
# Java but legal variable names in JS/Go).
_LANG_KEYWORDS: dict[str, frozenset[str]] = {
    "javascript": frozenset(),
    "typescript": frozenset(),
    "java": frozenset(
        {"int", "long", "double", "float", "boolean", "byte", "char", "short"}
    ),
    "go": frozenset(),
}

_NON_USE: dict[str, frozenset[str]] = {
    "javascript": _COMMON_KEYWORDS | _JS_BUILTINS,
    "typescript": _COMMON_KEYWORDS | _TS_BUILTINS,
    "java": _COMMON_KEYWORDS | _LANG_KEYWORDS["java"] | _JAVA_BUILTINS,
    "go": _COMMON_KEYWORDS | _GO_BUILTINS,
}
_NON_USE_FALLBACK = frozenset.union(*_NON_USE.values())

# Call-site exclusion is keyword-only: builtins are genuine calls.
_CALL_STOP = _COMMON_KEYWORDS | frozenset.union(*_LANG_KEYWORDS.values())


def _non_use_tokens(language: str) -> frozenset[str]:
    return _NON_USE.get(str(language or "").strip().lower(), _NON_USE_FALLBACK)


def _clean_source(lines: Sequence[str]) -> list[str]:
    """Blank string literals and strip comments, preserving line count.

    All four supported languages share ``//`` line comments and ``/* */``
    block comments (tracked across lines); quotes ``'`` ``"`` and `` ` ``
    are blanked.  JS template-literal ``${}`` interpolations are blanked
    too — a may-lose folded into ``approximate_use_detection``.
    """
    out: list[str] = []
    in_block = False
    for raw in lines:
        buf: list[str] = []
        i, n = 0, len(raw)
        in_str: str | None = None
        while i < n:
            ch = raw[i]
            if in_block:
                if ch == "*" and i + 1 < n and raw[i + 1] == "/":
                    in_block = False
                    i += 2
                else:
                    i += 1
                continue
            if in_str is not None:
                if ch == "\\":
                    i += 2
                    continue
                if ch == in_str:
                    in_str = None
                i += 1
                continue
            if ch == "/" and i + 1 < n:
                if raw[i + 1] == "/":
                    break
                if raw[i + 1] == "*":
                    in_block = True
                    i += 2
                    continue
            if ch in "'\"`":
                in_str = ch
                i += 1
                continue
            buf.append(ch)
            i += 1
        out.append("".join(buf))
    return out


def _prefixes(chain: str) -> list[str]:
    """All dotted prefixes of ``a.b.c`` — ``a``, ``a.b``, ``a.b.c`` —
    matching cfg_analysis's attribute cascade (a load of ``a.b.c`` is a use
    of ``a.b.c``, ``a.b``, and ``a``)."""
    segs = chain.split(".")
    return [".".join(segs[: i + 1]) for i in range(len(segs))]


def _scan_uses(
    item: _Item,
    defs_here: Sequence[_Def],
    scan_lines: Sequence[int],
    clean: Sequence[str],
    non_use: frozenset[str],
    function_name: str = "",
) -> tuple[list[_Use], bool]:
    """Approximate ``(uses, call_site_found)`` for one statement item.

    Identifier chains are collected in source order; each persisted def
    consumes its first matching occurrence (defining position is not a
    use).  A consumed member/subscript target still emits its base
    prefixes (the object is read to store into it), and when the
    statement's text carries an augmented/update operator the consumed
    target is a full use too (``x += y`` and ``x++`` read ``x``)."""
    texts: list[tuple[int, str]] = []
    for ln in scan_lines:
        if 0 < ln <= len(clean):
            texts.append((ln, clean[ln - 1]))
    joined = "\n".join(t for _ln, t in texts)
    aug = bool(_AUG_RE.search(joined))
    # Calls are detected against keywords only — builtins (``println``,
    # ``len``, ``String``) are real call sites even though they are not
    # variable uses.
    call_found = any(
        m.group(1).split(".")[0] not in _CALL_STOP
        for _ln, t in texts
        for m in _CALL_RE.finditer(t)
    )

    occurrences: list[tuple[str, int]] = []  # (chain, line), source order
    for ln, text in texts:
        for m in _CHAIN_RE.finditer(text):
            occurrences.append((m.group(0), ln))

    consumed: set[int] = set()
    for d in sorted(defs_here, key=lambda dd: (dd.line, dd.name)):
        base = d.name.split(".", 1)[0].split("[", 1)[0]
        pick = next(
            (i for i, (ch, _l) in enumerate(occurrences)
             if i not in consumed and ch == d.name),
            None,
        )
        if pick is None:
            pick = next(
                (i for i, (ch, _l) in enumerate(occurrences)
                 if i not in consumed and ch.split(".", 1)[0] == base),
                None,
            )
        if pick is not None:
            consumed.add(pick)

    uses: list[_Use] = []
    seen: set[tuple[str, int]] = set()

    def _add(name: str, ln: int) -> None:
        if (name, ln) not in seen:
            seen.add((name, ln))
            uses.append(_Use(name, ln))

    for i, (chain, ln) in enumerate(occurrences):
        first = chain.split(".", 1)[0]
        if first in non_use or chain == function_name:
            # Keywords/builtins are never uses; neither is the function's
            # own declared name (a bare `f` identifier — `obj.f` stays).
            continue
        if i in consumed:
            # Base-object read: every proper prefix of the stored target.
            for pref in _prefixes(chain)[:-1]:
                _add(pref, ln)
            if aug:
                _add(chain, ln)
            continue
        for pref in _prefixes(chain):
            _add(pref, ln)
    return uses, call_found


# ---------------------------------------------------------------------------
# Slicing (block-granular ports of cfg_analysis's worklists).
# ---------------------------------------------------------------------------


def _items_by_def_key(cfg: StoredCFG) -> dict[tuple[str, int], list[_Item]]:
    out: dict[tuple[str, int], list[_Item]] = {}
    for bid in sorted(cfg.blocks):
        for item in cfg.blocks[bid].statements:
            for d in cfg.effects(item)[0]:
                out.setdefault((d.name, d.line), []).append(item)
    return out


def _items_by_use_key(cfg: StoredCFG) -> dict[tuple[str, int], list[_Item]]:
    out: dict[tuple[str, int], list[_Item]] = {}
    for bid in sorted(cfg.blocks):
        for item in cfg.blocks[bid].statements:
            for u in cfg.effects(item)[1]:
                out.setdefault((u.name, u.line), []).append(item)
    return out


def _backward_included(
    cfg: StoredCFG,
    ud: UseDefChains,
    seed_items: Iterable[_Item],
    control_deps: set[tuple[int, int, str]],
    seed_vars: set[str] | None = None,
) -> set[_Item]:
    """Included items for a backward slice — same closure as
    ``cfg_analysis._backward_included`` at item granularity: data deps via
    reaching defs of the item's uses, control deps via the predicate items
    of every block the item's block is control-dependent on, recursively."""
    def_items = _items_by_def_key(cfg)
    block_items = {b.id: list(b.statements) for b in cfg.blocks.values()}

    included: set[_Item] = set()
    seeds = list(seed_items)
    worklist: list[_Item] = list(seeds)
    seed_ids = set(seeds)

    while worklist:
        item = worklist.pop()
        if item in included:
            continue
        included.add(item)
        defs, uses = cfg.effects(item)
        for u in uses:
            if item in seed_ids and seed_vars is not None and u.name not in seed_vars:
                continue
            for dk in ud.use_to_defs.get((u.name, u.line), ()):
                worklist.extend(def_items.get(dk, ()))
        blk = cfg.item_block.get(item)
        if blk is None:
            continue
        for dep, ctrl, _label in control_deps:
            if dep == blk:
                worklist.extend(block_items.get(ctrl, ()))
    return included


def _forward_included(
    cfg: StoredCFG,
    ud: UseDefChains,
    seed_items: Iterable[_Item],
    control_deps: set[tuple[int, int, str]],
    seed_vars: set[str] | None = None,
) -> set[_Item]:
    """Included items for a forward slice — def -> uses via the chains, plus
    everything control-dependent on included predicate blocks."""
    use_items = _items_by_use_key(cfg)
    block_items = {b.id: list(b.statements) for b in cfg.blocks.values()}

    included: set[_Item] = set()
    seeds = list(seed_items)
    worklist: list[_Item] = list(seeds)
    seed_ids = set(seeds)

    while worklist:
        item = worklist.pop()
        if item in included:
            continue
        included.add(item)
        defs, _uses = cfg.effects(item)
        for d in defs:
            if item in seed_ids and seed_vars is not None and d.name not in seed_vars:
                continue
            for uk in ud.def_to_uses.get((d.name, d.line), ()):
                worklist.extend(use_items.get(uk, ()))
        blk = cfg.item_block.get(item)
        if blk is None:
            continue
        for dep, ctrl, _label in control_deps:
            if ctrl == blk:
                worklist.extend(block_items.get(dep, ()))
    return included


def backward_slice(
    cfg: StoredCFG,
    ud: UseDefChains,
    criterion: tuple[int, Iterable[str] | None],
    control_deps: set[tuple[int, int, str]] | None = None,
) -> set[int]:
    """Statement lines feeding ``criterion = (line, vars)`` — mirrors
    ``cfg_analysis.backward_slice``."""
    line, vars_ = criterion
    if control_deps is None:
        control_deps = control_dependence(cfg)
    seeds = cfg.statements_at_line(line)
    if not seeds:
        raise CFGAnalysisError(f"no statement covers line {line}")
    included = _backward_included(
        cfg, ud, seeds, control_deps, set(vars_) if vars_ else None
    )
    return {item[1] for item in included}


def forward_slice(
    cfg: StoredCFG,
    ud: UseDefChains,
    criterion: tuple[int, Iterable[str] | None],
    control_deps: set[tuple[int, int, str]] | None = None,
) -> set[int]:
    """Statement lines affected by ``criterion`` — symmetric to backward."""
    line, vars_ = criterion
    if control_deps is None:
        control_deps = control_dependence(cfg)
    seeds = cfg.statements_at_line(line)
    if not seeds:
        raise CFGAnalysisError(f"no statement covers line {line}")
    included = _forward_included(
        cfg, ud, seeds, control_deps, set(vars_) if vars_ else None
    )
    return {item[1] for item in included}


def _included_items(
    cfg: StoredCFG, ud: UseDefChains, line: int, direction: str,
    control_deps: set[tuple[int, int, str]], variables: Iterable[str] | None,
) -> set[_Item]:
    seeds = cfg.statements_at_line(line)
    if not seeds:
        raise CFGAnalysisError(f"no statement covers line {line}")
    vars_set = set(variables) if variables else None
    if direction == "backward":
        return _backward_included(cfg, ud, seeds, control_deps, vars_set)
    return _forward_included(cfg, ud, seeds, control_deps, vars_set)


# ---------------------------------------------------------------------------
# Top-level driver.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StoredAnalysis:
    """Everything the module computes for one persisted function CFG, plus
    slice methods — the StoredCFG analogue of
    ``cfg_analysis.FunctionAnalysis``."""

    function_name: str
    node_id: int
    def_line: int
    cfg: StoredCFG
    dominators: DominatorTree
    post_dominators: DominatorTree
    control_dependence: set[tuple[int, int, str]]
    reaching: ReachingDefinitions
    chains: UseDefChains
    limitations: list[str]

    def backward_slice(
        self, line: int, variables: Iterable[str] | None = None
    ) -> set[int]:
        return backward_slice(
            self.cfg, self.chains, (line, variables), self.control_dependence
        )

    def forward_slice(
        self, line: int, variables: Iterable[str] | None = None
    ) -> set[int]:
        return forward_slice(
            self.cfg, self.chains, (line, variables), self.control_dependence
        )


def analyze_stored(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    source: str,
    function_name: str = "",
    language: str = "",
) -> StoredAnalysis:
    """Full analysis pipeline over a persisted CFG: load the cfg_* rows,
    then run the same dominator / control-dependence / reaching-definition
    / use-def composition as ``cfg_analysis``."""
    cfg = load_stored_cfg(
        conn,
        node_id,
        source_lines=source.splitlines(),
        function_name=function_name,
        language=language,
    )
    dom = dominators(cfg)
    pdom = post_dominators(cfg)
    cd = control_dependence(cfg)
    rdefs = reaching_definitions(cfg)
    chains = use_def_chains(cfg, rdefs)
    return StoredAnalysis(
        function_name=function_name,
        node_id=int(node_id),
        def_line=cfg.def_line,
        cfg=cfg,
        dominators=dom,
        post_dominators=pdom,
        control_dependence=cd,
        reaching=rdefs,
        chains=chains,
        limitations=list(cfg.limitations),
    )


def _call_sites(
    cfg: StoredCFG,
    included: set[_Item],
    lines: set[int],
    source_lines: Sequence[str],
) -> list[dict[str, object]]:
    """Call sites on slice lines — reported, never inlined.  Approximate:
    an identifier chain immediately before ``(`` on a slice line."""
    clean = _clean_source(source_lines)
    out: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item in sorted(included):
        if cfg.blocks[item[0]].kind == "entry":
            continue  # signature pseudo-item — cfg_analysis skips func_node too
        for ln in cfg.item_scan_lines.get(item, ()):
            if ln not in lines or not (0 < ln <= len(clean)):
                continue
            for m in _CALL_RE.finditer(clean[ln - 1]):
                name = m.group(1)
                if name.split(".", 1)[0] in _CALL_STOP:
                    continue
                if (ln, name) not in seen:
                    seen.add((ln, name))
                    out.append({"line": ln, "name": name})
    out.sort(key=lambda c: (c["line"], c["name"]))  # type: ignore[arg-type]
    return out


# ---------------------------------------------------------------------------
# Interprocedural composition over persisted CFGs.
# ---------------------------------------------------------------------------

_RETURN_RE = re.compile(r"^\s*return\b")
_SUPPORTED_INTERPROC = frozenset(
    {"javascript", "typescript", "java", "go"}
)


def _return_items(
    cfg: StoredCFG, clean: Sequence[str]
) -> list[_Item]:
    """Statement items whose text is a ``return`` — the stored analogue of
    ``cfg_analysis._return_lines`` (AST Return nodes)."""
    out: list[_Item] = []
    for bid in sorted(cfg.blocks):
        blk = cfg.blocks[bid]
        if blk.kind == "entry":
            continue
        for item in blk.statements:
            for ln in cfg.item_scan_lines.get(item, ()):
                if 0 < ln <= len(clean) and _RETURN_RE.search(clean[ln - 1]):
                    out.append(item)
                    break
    return out


def _last_item_line(cfg: StoredCFG) -> int:
    lines = [
        item[1]
        for bid in sorted(cfg.blocks)
        if cfg.blocks[bid].kind != "entry"
        for item in cfg.blocks[bid].statements
    ]
    return max(lines) if lines else cfg.def_line


def _split_actuals(
    call_name: str,
    call_line: int,
    clean: Sequence[str],
    *,
    prefer_names: frozenset[str] = frozenset(),
) -> list[str] | None:
    """Actual argument expressions of ``call_name(...)`` on ``call_line``.

    Locates the first ``call_name(`` occurrence on the line, paren-matches
    the argument list, and splits top-level commas — nested parens,
    brackets, braces, and string literals are depth/string aware.  This is
    textual, not AST: ``approximate_actual_extraction`` always applies.
    Returns ``None`` when the call site cannot be located on the line.

    ``prefer_names`` ranks fallback candidates when the textual callee
    differs from the resolved target (callable-value flow: ``cb()``
    resolved to ``goHelper``).  Passing the caller's formal names picks
    the parameter callback over an unrelated call on a multi-call line.
    """
    if not (0 < call_line <= len(clean)):
        return None
    text = clean[call_line - 1]
    tail = call_name.rsplit(".", 1)[-1]
    m = re.search(r"(?<![\w$])" + re.escape(tail) + r"\s*\(", text)
    if m is None:
        # Callable-value flow: the textual callee differs from the resolved
        # target (``cb()`` bound to ``goHelper``).  The graph edge already
        # proved this line's call resolves to the callee — prefer a
        # call-shaped caller formal, else the first candidate.
        candidates = [
            mm
            for mm in _CALL_RE.finditer(text)
            if mm.group(1).split(".", 1)[0] not in _CALL_STOP
        ]
        preferred = [mm for mm in candidates if mm.group(1) in prefer_names]
        m = preferred[0] if preferred else (candidates[0] if candidates else None)
        if m is None:
            return None
    i = text.index("(", m.start())
    depth = 0
    in_str: str | None = None
    arg_start = i + 1
    args: list[str] = []
    j = i
    while j < len(text):
        ch = text[j]
        if in_str is not None:
            if ch == "\\":
                j += 1
            elif ch == in_str:
                in_str = None
        elif ch in "\"'`":
            in_str = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                args.append(text[arg_start:j].strip())
                return [a for a in args if a]
        elif ch == "," and depth == 1:
            args.append(text[arg_start:j].strip())
            arg_start = j + 1
        j += 1
    return None


def _actual_vars(expr: str, non_use: frozenset[str]) -> list[str]:
    """Identifier names read by one actual expression — the stored analogue
    of ``cfg_analysis._loads`` (AST Name/Attribute loads)."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _CHAIN_RE.finditer(expr):
        chain = m.group(0)
        for pref in _prefixes(chain):
            if pref not in non_use and pref not in seen:
                seen.add(pref)
                out.append(pref)
    return out


_PARAM_VALUE_RE = re.compile(
    r"^(?P<name>(?:\*{1,2}|\.{3})?[A-Za-z_][\w$]*)"
    r"(?::+(?P<type>.*?))?"
    r"(?:\s+\[required\]|\s+opt(?:=(?P<default>.*))?)?$"
)


def _param_properties(
    conn: sqlite3.Connection, node_id: int
) -> list[dict[str, object]]:
    """Signature-proven formals from persisted ``param`` properties —
    declaration order (rowid), with type annotations and optionality
    markers (``name[:type] [required]`` / ``name[:type] opt[=default]``).
    Variadics surface as ``*args`` / ``**kw`` / ``...rest`` name prefixes
    or a ``...``-prefixed type (Go ``args ...int``); older graphs emit
    them unmarked and they bind as ordinary formals.  Returns [] when
    the producer emitted no param rows for the node."""
    try:
        rows = conn.execute(
            "SELECT value, line FROM properties"
            " WHERE kind = 'param' AND node_id = ? ORDER BY id",
            (node_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict[str, object]] = []
    for value, line in rows:
        m = _PARAM_VALUE_RE.match(str(value).strip())
        if m is None:
            continue
        raw_name = m.group("name")
        ptype = (m.group("type") or "").strip()
        kind = (
            "required"
            if m.group("default") is None and " opt" not in str(value)
            else "optional"
        )
        if raw_name.startswith("**"):
            kind = "kw_variadic"
        elif raw_name.startswith(("*", "...")) or ptype.startswith("..."):
            kind = "variadic"
        out.append(
            {
                "name": raw_name.lstrip("*."),
                "type": ptype,
                "kind": kind,
                "default": m.group("default"),
                "line": int(line or 0),
            }
        )
    return out


def _formal_defs(conn: sqlite3.Connection, node_id: int) -> list[tuple[str, int]]:
    """Parameter-like defs of a persisted function, in declaration order.

    Prefers signature-proven ``param`` properties (exact names + declared
    positions); falls back to cfg_defs rows in the entry block
    (block_index 0) when no param rows exist.  ``cfg.params`` is sorted
    for the public API; hops need source order."""
    props = _param_properties(conn, node_id)
    if props:
        return [(str(p["name"]), int(p["line"])) for p in props]
    rows = conn.execute(
        "SELECT var_name, line FROM cfg_defs"
        " WHERE node_id = ? AND block_index = 0 ORDER BY id",
        (node_id,),
    ).fetchall()
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, ln in rows:
        name = str(name)
        if name not in seen:
            seen.add(name)
            out.append((name, int(ln)))
    return out


_KW_ACTUAL_RE = re.compile(r"^(?P<kw>[A-Za-z_][\w$]*)\s*=(?!=)")


def _bind_actuals(
    formals: list[dict[str, object]],
    actuals: list[str],
) -> tuple[dict[str, str], list[str]]:
    """Bind actual expressions to signature-proven formals.

    Keyword actuals (``name=expr``) bind by formal name; positional
    actuals bind to the remaining required-then-optional formals in
    declaration order; a variadic formal absorbs the rest.  Returns
    ``(formal_name -> actual_expr, unbound_reasons)`` — reasons name the
    binding failures honestly (``arity_mismatch``, ``unknown_kw``)."""
    bound: dict[str, str] = {}
    reasons: list[str] = []
    kw_actuals: dict[str, str] = {}
    pos_actuals: list[str] = []
    formal_names = {str(f["name"]) for f in formals}
    kw_variadic = next((f for f in formals if f["kind"] == "kw_variadic"), None)
    variadic = next((f for f in formals if f["kind"] == "variadic"), None)
    for a in actuals:
        if a.startswith("**"):
            # ``f(**opts)`` spreads into the keyword formals — it can only
            # bind to ``**kw``; elsewhere the spread is unresolvable.
            if kw_variadic is not None:
                bound[str(kw_variadic["name"])] = a
            else:
                reasons.append(f"spread_actual:{a[:16]}")
            continue
        if a.startswith("*"):
            # ``f(*seq)`` spreads an unknown count — binds only to a
            # positional variadic.
            if variadic is not None:
                bound[str(variadic["name"])] = a
            else:
                reasons.append(f"spread_actual:{a[:16]}")
            continue
        km = _KW_ACTUAL_RE.match(a)
        if km is not None:
            kw = km.group("kw")
            if kw in formal_names:
                kw_actuals[kw] = a[km.end():].strip()
            elif kw_variadic is not None:
                kv = str(kw_variadic["name"])
                extra = f"{kw}={a[km.end():].strip()}"
                kw_actuals[kv] = (
                    f"{kw_actuals[kv]},{extra}" if kv in kw_actuals else extra
                )
            else:
                reasons.append(f"unknown_kw:{kw}")
        else:
            pos_actuals.append(a)
    pos_formals = [
        f for f in formals if f["kind"] not in ("variadic", "kw_variadic")
    ]
    remaining = [f for f in pos_formals if str(f["name"]) not in kw_actuals]
    for f in formals:
        name = str(f["name"])
        if name in kw_actuals:
            bound[name] = kw_actuals[name]
    for i, f in enumerate(remaining):
        if i < len(pos_actuals):
            bound[str(f["name"])] = pos_actuals[i]
    if len(pos_actuals) > len(remaining):
        if variadic is not None:
            vn = str(variadic["name"])
            extra = ",".join(pos_actuals[len(remaining):])
            bound[vn] = f"{bound[vn]},{extra}" if vn in bound else extra
        else:
            reasons.append("arity_mismatch")
    return bound, reasons


def _formal_reached_stored(
    analysis: StoredAnalysis,
    formal_key: tuple[str, int],
    covered: set[_Item],
) -> bool:
    """True when any use of the formal (def key ``(name, line)``) lands on a
    covered item — the stored analogue of ``_formal_reached``."""
    use_items = _items_by_use_key(analysis.cfg)
    for uk in analysis.chains.def_to_uses.get(formal_key, ()):
        if any(it in covered for it in use_items.get(uk, ())):
            return True
    return False


def interprocedural_slice_stored(
    conn: sqlite3.Connection,
    source_texts: object,
    entry_node_id: int,
    line: int,
    direction: str = "backward",
    *,
    source: str,
    function_name: str,
    language: str,
    max_depth: int = 3,
    max_hops: int | None = None,
) -> dict[str, object]:
    """Bounded interprocedural slice composition over persisted CFGs —
    the cfg_store analogue of ``cfg_analysis.interprocedural_slice``.

    Hops compose through graph.db CALLS edges (``source_id`` +
    ``source_line`` → target node), which are exactly the resolved call
    sites — no text re-scan for target resolution.  Argument mapping is
    positional against the callee's persisted parameter defs; actual
    expressions and uses are text-extracted
    (``approximate_actual_extraction`` on top of the substrate's
    ``approximate_use_detection``).  Only callees that also carry a
    persisted CFG and a supported language are composed; everything else is
    a named limitation, never silently inlined.

    Same bounds as the AST version: ``max_depth`` (``max_depth_cut``),
    a ``(caller, call_line, callee)`` visited set (``recursion_cut``), and
    ``max_hops`` (``hop_budget`` + ``truncated``).
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

    _clean_cache: dict[str, list[str]] = {}

    def _clean(file: str) -> list[str]:
        if file not in _clean_cache:
            _clean_cache[file] = _clean_source((_text(file) or "").splitlines())
        return _clean_cache[file]

    _nodes: dict[int, tuple[str, str, str, int, int] | None] = {}

    def _node(node_id: int) -> tuple[str, str, str, int, int] | None:
        """(file, name, language, start_line, end_line) for a node id."""
        if node_id not in _nodes:
            row = conn.execute(
                "SELECT file_path, name, language, start_line, end_line"
                " FROM nodes WHERE id = ?",
                (node_id,),
            ).fetchone()
            _nodes[node_id] = (
                (
                    str(row[0]),
                    str(row[1]),
                    str(row[2] or "").strip().lower(),
                    int(row[3] or 0),
                    int(row[4] or 0),
                )
                if row
                else None
            )
        return _nodes[node_id]

    _analyses: dict[int, StoredAnalysis | None] = {}

    def _analysis(node_id: int) -> StoredAnalysis | None:
        if node_id not in _analyses:
            info = _node(node_id)
            text = _text(info[0]) if info is not None else None
            if info is None or text is None:
                _analyses[node_id] = None
            else:
                try:
                    _analyses[node_id] = analyze_stored(
                        conn,
                        node_id,
                        source=text,
                        function_name=info[1],
                        language=info[2],
                    )
                except CFGAnalysisError:
                    _analyses[node_id] = None
        return _analyses[node_id]

    edge_cols = {
        str(r[1]) for r in conn.execute("PRAGMA table_info(edges)")
    }
    has_actual_args = "actual_args" in edge_cols

    def _callees(
        caller_node_id: int, call_line: int
    ) -> list[tuple[int, list[str] | None]]:
        """Resolved CALLS targets at ``call_line`` — ``(node_id, actuals)``.

        ``actuals`` is the parser-exact argument text list persisted on the
        edge (schema v15.4+) or ``None`` when the graph predates it — the
        caller falls back to source-text splitting, flagged approximate.
        """
        if has_actual_args:
            rows = conn.execute(
                "SELECT e.target_id, e.actual_args FROM edges e"
                " WHERE e.source_id = ? AND e.type = 'CALLS'"
                " AND e.source_line = ? ORDER BY e.target_id",
                (caller_node_id, int(call_line)),
            ).fetchall()
        else:
            rows = [
                (r[0], None)
                for r in conn.execute(
                    "SELECT DISTINCT e.target_id FROM edges e"
                    " WHERE e.source_id = ? AND e.type = 'CALLS'"
                    " AND e.source_line = ? ORDER BY e.target_id",
                    (caller_node_id, int(call_line)),
                ).fetchall()
            ]
        out: list[tuple[int, list[str] | None]] = []
        seen_t: set[int] = set()
        for target_id, raw_args in rows:
            tid = int(target_id)
            if tid in seen_t:
                continue
            seen_t.add(tid)
            actuals: list[str] | None = None
            if raw_args:
                try:
                    parsed = json.loads(str(raw_args))
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    actuals = [str(a) for a in parsed]
            out.append((tid, actuals))
        return out

    def _call_lines_in(analysis: StoredAnalysis, covered: set[_Item]) -> list[int]:
        covered_lines = {it[1] for it in covered}
        return [
            int(r[0])
            for r in conn.execute(
                "SELECT DISTINCT source_line FROM edges"
                " WHERE source_id = ? AND type = 'CALLS'"
                " AND source_line IS NOT NULL ORDER BY source_line",
                (analysis.node_id,),
            )
            if int(r[0]) in covered_lines
        ]

    entry_info = _node(int(entry_node_id))
    if entry_info is None:
        raise CFGAnalysisError(f"entry node {entry_node_id} not found")
    entry_analysis = _analysis(int(entry_node_id))
    if entry_analysis is None:
        raise CFGAnalysisError(
            f"entry function {function_name!r} has no analyzable persisted CFG"
        )

    per_file: dict[str, set[int]] = {}
    cross: list[dict[str, object]] = []
    visited: set[tuple[int, int, int]] = set()
    truncated = False

    # Worklist: (node_id, kind, payload, depth, hop).
    #   "entry"  -> payload = criterion line
    #   "callee" -> payload = (mapping formal_key->actual expr,
    #               (caller_node_id, call_line))
    worklist: list[tuple[int, str, object, int, dict | None]] = [
        (int(entry_node_id), "entry", int(line), 0, None)
    ]
    while worklist:
        node_id, kind, payload, depth, hop = worklist.pop()
        info = _node(node_id)
        analysis = _analysis(node_id)
        if info is None or analysis is None:
            _lim(f"callee_analysis_failed:{node_id}")
            if hop is not None:
                hop["callee_slice_lines"] = []
            continue
        file, fn, lang = info[0], info[1], info[2]
        for lim in analysis.limitations:
            _lim(lim)
        clean = _clean(file)
        non_use = _LANG_KEYWORDS.get(lang, _COMMON_KEYWORDS)

        forward_reached: list[str] = []
        if kind == "entry":
            seeds = analysis.cfg.statements_at_line(int(payload))  # type: ignore[arg-type]
            if not seeds:
                raise CFGAnalysisError(f"no statement covers line {payload}")
            if direction == "backward":
                included = _backward_included(
                    analysis.cfg, analysis.chains, seeds,
                    analysis.control_dependence,
                )
            else:
                included = _forward_included(
                    analysis.cfg, analysis.chains, seeds,
                    analysis.control_dependence,
                )
        elif direction == "backward":
            ret = _return_items(analysis.cfg, clean)
            if not ret:
                ret = analysis.cfg.statements_at_line(
                    _last_item_line(analysis.cfg)
                )
            included = _backward_included(
                analysis.cfg, analysis.chains, ret, analysis.control_dependence
            )
        else:
            mapping = payload[0]  # type: ignore[index]
            ret_set = {
                it[1] for it in _return_items(analysis.cfg, clean)
            } or {_last_item_line(analysis.cfg)}
            use_items = _items_by_use_key(analysis.cfg)
            included = set()
            for formal_key in sorted(mapping):
                f_seeds: list[_Item] = []
                for uk in analysis.chains.def_to_uses.get(formal_key, ()):
                    f_seeds.extend(use_items.get(uk, ()))
                f_inc = _forward_included(
                    analysis.cfg,
                    analysis.chains,
                    f_seeds,
                    analysis.control_dependence,
                )
                if {it[1] for it in f_inc} & ret_set:
                    forward_reached.append(formal_key[0])
                included |= f_inc

        covered = set(included)
        lines = {it[1] for it in included}
        per_file.setdefault(file, set()).update(lines)

        if kind == "callee" and hop is not None:
            mapping, caller = payload  # type: ignore[misc]
            caller_node_id, call_line = caller
            if direction == "backward":
                reached = sorted(
                    key[0]
                    for key in mapping
                    if _formal_reached_stored(analysis, key, covered)
                )
                hop["reached_formals"] = reached
                caller_analysis = _analysis(caller_node_id)
                if caller_analysis is not None:
                    caller_info = _node(caller_node_id)
                    def_items = _items_by_def_key(caller_analysis.cfg)
                    for key, actual in mapping.items():
                        if key[0] not in reached:
                            continue
                        for v in _actual_vars(actual, non_use):
                            # Uses of the actual's vars at/near the call
                            # line resolve to caller defs through chains.
                            for (uname, uline), dk in list(
                                caller_analysis.chains.use_to_defs.items()
                            ):
                                if uname != v:
                                    continue
                                for dkey in dk:
                                    for it in def_items.get(dkey, ()):
                                        per_file.setdefault(
                                            caller_info[0], set()
                                        ).add(it[1])
            else:
                hop["reached_formals"] = forward_reached
                hop["reaches_return"] = bool(forward_reached)
            hop["callee_slice_lines"] = sorted(lines)

        call_lines = _call_lines_in(analysis, covered)
        if not call_lines:
            continue
        if depth >= max_depth:
            _lim("max_depth_cut")
            continue
        for cline in call_lines:
            targets = _callees(node_id, cline)
            if not targets:
                _lim(f"callee_unresolved:{cline}")
                continue
            for c_id, persisted_actuals in targets:
                c_info = _node(c_id)
                if c_info is None:
                    _lim("callee_target_malformed")
                    continue
                c_file, c_fn, c_lang, c_def = (
                    c_info[0],
                    c_info[1],
                    c_info[2],
                    c_info[3],
                )
                if c_lang not in _SUPPORTED_INTERPROC:
                    _lim(f"callee_language_unsupported:{c_lang or 'unknown'}")
                    continue
                if not has_persisted_cfg(conn, c_id):
                    _lim(f"callee_cfg_absent:{c_fn}")
                    continue
                if max_hops is not None and len(cross) >= max_hops:
                    truncated = True
                    _lim("hop_budget")
                    break
                hkey = (node_id, cline, c_id)
                if hkey in visited:
                    _lim("recursion_cut")
                    continue
                visited.add(hkey)
                if _text(c_file) is None:
                    _lim(f"callee_source_unavailable:{c_file}")
                    continue
                # Signature-proven actual->formal binding: persisted
                # ``param`` properties give declared order/types/kinds;
                # kw actuals bind by name, positionals fill the rest.
                if persisted_actuals is not None:
                    # Parser-exact arg texts persisted on the edge — no
                    # text re-splitting, no extraction approximation.
                    actuals = persisted_actuals
                    actuals_exact = True
                else:
                    caller_formals = frozenset(
                        str(p["name"])
                        for p in _param_properties(conn, node_id)
                    )
                    actuals = _split_actuals(
                        c_fn, cline, _clean(file),
                        prefer_names=caller_formals,
                    )
                    actuals_exact = False
                    if actuals is None:
                        _lim(f"actuals_unmapped:{c_fn}")
                        actuals = []
                formals_meta = _param_properties(conn, c_id)
                if formals_meta:
                    bound, reasons = _bind_actuals(formals_meta, actuals)
                    for r in reasons:
                        _lim(r if ":" in r else f"binding_{r}:{c_fn}")
                    # Chain lookups key on cfg_defs rows — resolve each bound
                    # formal name to its persisted def key.
                    key_by_name = {
                        str(r[0]): (str(r[0]), int(r[1]))
                        for r in conn.execute(
                            "SELECT var_name, line FROM cfg_defs"
                            " WHERE node_id = ? AND block_index = 0"
                            " ORDER BY id",
                            (c_id,),
                        )
                    }
                    mapping: dict[tuple[str, int], str] = {}
                    for fname, aexpr in sorted(bound.items()):
                        k = key_by_name.get(fname)
                        if k is None:
                            _lim(f"formal_def_absent:{fname}")
                            continue
                        mapping[k] = aexpr
                else:
                    formals = _formal_defs(conn, c_id)
                    if len(actuals) > len(formals):
                        _lim("actuals_unmapped")
                    mapping = {
                        formals[i]: actuals[i]
                        for i in range(min(len(formals), len(actuals)))
                    }
                _lim("interprocedural_summary")
                if not actuals_exact:
                    _lim("approximate_actual_extraction")
                call_items = analysis.cfg.statements_at_line(cline)
                call_defs: list[str] = []
                for it in call_items:
                    call_defs.extend(d.name for d in analysis.cfg.effects(it)[0])
                hop2: dict[str, object] = {
                    "caller_file": file,
                    "caller_fn": fn,
                    "call_line": cline,
                    "call_name": c_fn,
                    "callee_file": c_file,
                    "callee_fn": c_fn,
                    "callee_def_line": c_def,
                    "mapped_vars": {
                        key[0]: actual for key, actual in sorted(mapping.items())
                    },
                    "result_vars": sorted(set(call_defs)),
                    "callee_slice_lines": [],
                }
                cross.append(hop2)
                worklist.append(
                    (
                        c_id,
                        "callee",
                        (mapping, (node_id, cline)),
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
            "file": entry_info[0],
            "function": entry_info[1],
            "line": int(line),
            "direction": direction,
            "max_depth": max_depth,
            "substrate": "persisted_cfg",
        },
        "per_file": {p: sorted(ls) for p, ls in sorted(per_file.items())},
        "cross_function": cross,
        "limitations": limitations,
        "truncated": truncated,
    }


def slice_stored(
    conn: sqlite3.Connection,
    node_id: int,
    line: int,
    direction: str = "backward",
    *,
    source: str,
    function_name: str = "",
    language: str = "",
    variables: Iterable[str] | None = None,
) -> dict[str, object]:
    """Persisted-CFG analogue of ``cfg_analysis.slice_at_line``.

    Returns ``{"lines", "variables", "blocks", "limitations",
    "call_sites"}`` — all sorted/deterministic.  ``limitations`` always
    contains ``approximate_use_detection``: uses are text-scanned, defs are
    persisted, and nothing here is interprocedural.
    """
    if direction not in {"backward", "forward"}:
        raise CFGAnalysisError(
            f"direction must be 'backward' or 'forward', got {direction!r}"
        )
    analysis = analyze_stored(
        conn, node_id, source=source, function_name=function_name,
        language=language,
    )
    included = _included_items(
        analysis.cfg, analysis.chains, int(line), direction,
        analysis.control_dependence, variables,
    )
    lines = {item[1] for item in included}
    var_names: set[str] = set()
    for item in included:
        defs, uses = analysis.cfg.effects(item)
        var_names.update(d.name for d in defs)
        var_names.update(u.name for u in uses)

    return {
        "lines": sorted(lines),
        "variables": sorted(var_names),
        "blocks": sorted({item[0] for item in included}),
        "limitations": list(analysis.limitations),
        "call_sites": _call_sites(
            analysis.cfg,
            included,
            lines,
            source.splitlines(),
        ),
    }


__all__ = [
    "StoredAnalysis",
    "StoredBlock",
    "StoredCFG",
    "analyze_stored",
    "backward_slice",
    "forward_slice",
    "has_persisted_cfg",
    "interprocedural_slice_stored",
    "load_stored_cfg",
    "slice_stored",
]
