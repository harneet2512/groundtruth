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

    item_effects: dict[_Item, tuple[list[_Def], list[_Use]]] = {}
    any_call = False
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
    if any_call:
        _lim("call_sites_not_inlined")

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
    "load_stored_cfg",
    "slice_stored",
]
