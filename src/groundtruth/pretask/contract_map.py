"""Deterministic CONTRACT evidence: signature + raises + guards + return shape.

The CONTRACT pillar of GT Context Philosophy (CLAUDE.md): the interface facts an
agent must preserve when editing a function — the valid value-domains (the full
typed ``signature``, e.g. ``split_by: Literal["word","sentence","page","line"]``),
the exceptions it raises, the preconditions/early-returns (guards), and the return
shape. These are read from the ``properties`` table + ``nodes`` — facts the parser
already extracted, that the first-turn brief currently throws away.

Always-available: a function's OWN contract needs NO graph edges (it is node-local),
so it fires even on isolated functions where the agent is most blind. The 1-hop
CALLEE contract (what the functions this one calls can raise — e.g. a wrapped
``os.utime`` raising ``OSError``) is gated on VERIFIED edges only, so a name_match
guess never launders as "this callee raises X".

Correct-or-quiet: emit only what the parser actually extracted; abstain (empty
string) when nothing is known rather than guess. Tier A kinds (exception_type,
guard_clause, return_shape) are present in every indexed db; Tier B kinds
(boundary_condition, conditional_return, exception_flow) appear when the repo was
indexed by the current binary.

Research: The Distracting Effect (arXiv:2505.06914, 2025) — plausible-but-wrong
context drops accuracy 6-11pp, so never render an unverified callee edge as a fact.
Lost in the Middle (NeurIPS 2024) — signature first (primacy). LLM-free, $0, pure SQL.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _has_columns,
    _neighbors,
    _node_ids,
    _open_ro,
    build_function_map,
    verified_caller_count,
)
from groundtruth.runtime.sanitizer import (
    clip_balanced,
    valid_exception_spec,
    valid_guard_clause,
    valid_return_shape,
)

# Per-kind SEMANTIC validators (C1b — B3 semantic-nonsense contract). clip_balanced
# is STRUCTURAL only: it passes ``raise,exc_info[1].with_traceback`` (brackets
# balanced) and an empty guard. The indexer occasionally stores a parsed statement
# fragment as a "raises"/"guard"/"return" value (e.g. a re-raise of an exc_info
# tuple mined as an exception_type), so a mined value must clear a per-kind shape
# check before it can render. Correct-or-quiet: a value that is not a well-formed
# exception NAME / guard expression / return shape is DROPPED, never rendered.
# Research: The Distracting Effect (arXiv:2505.06914, 2025) — structurally-balanced-
# but-wrong context degrades agents, so suppress, don't render. Language-agnostic
# (the validators reason about identifiers/brackets, not Python AST).
def _valid_data_flow(v: str) -> bool:
    """A data_flow value is ``<param> -> <use> | <use>``. Require the arrow to have
    survived clip_balanced (a value reduced to just the param name or to a dangling
    fragment carries no provenance) and a non-empty right-hand side. Correct-or-quiet.
    """
    if " -> " not in v:
        return False
    rhs = v.split(" -> ", 1)[1].strip()
    return bool(rhs)


_CONTRACT_VALUE_VALIDATORS = {
    "exception_type": valid_exception_spec,
    "guard_clause": valid_guard_clause,
    "return_shape": valid_return_shape,
    "data_flow": _valid_data_flow,
}

# Tier A — populated in every indexed db (verified empirically 2026-05-29).
_TIER_A_KINDS = ("exception_type", "guard_clause", "return_shape")
# Tier B — richer kinds the current binary extracts (older task dbs lack them).
# data_flow = per-parameter forward slice (def-use): where each input VALUE flows
# inside the body (the dimension the call graph lacks — the off-by-one "where is
# count checked vs incremented" question). Heuristic (indexer confidence 0.8), so
# it renders but is gated by the per-kind value validator below (correct-or-quiet).
_TIER_B_KINDS = ("boundary_condition", "conditional_return", "exception_flow", "data_flow")
_CONTRACT_KINDS = _TIER_A_KINDS + _TIER_B_KINDS

# Cap each kind so the rendered block stays inside the token budget.
_MAX_PER_KIND = 3
# 1-hop for callee contracts (RepoGraph ICLR 2025: 1-hop > 2-hop).
_MAX_CALLEES = 3


@dataclass(frozen=True)
class ContractEvidence:
    """The deterministic contract of a single function."""

    file: str
    function: str
    signature: str = ""
    return_type: str = ""
    raises: tuple[str, ...] = ()  # exception_type values
    guards: tuple[str, ...] = ()  # guard_clause values
    return_shape: str = ""  # most-common return_shape
    boundaries: tuple[str, ...] = ()  # boundary_condition (Tier B)
    conditionals: tuple[str, ...] = ()  # conditional_return (Tier B)
    exc_flows: tuple[str, ...] = ()  # exception_flow (Tier B)
    flows: tuple[str, ...] = ()  # data_flow: per-param forward slice (Tier B)
    is_callee: bool = False  # True when this is a verified 1-hop callee's contract

    @property
    def has_signal(self) -> bool:
        return bool(
            self.raises
            or self.guards
            or self.return_shape
            or self.boundaries
            or self.conditionals
            or self.exc_flows
            or self.flows
            # A callee with a known signature IS signal — the deciding interface
            # fact the edit-target must call correctly (the set_parse(self, key,
            # string: str) the agent otherwise greps for). The is_callee guard was
            # dropping exactly that. (Task #48, 2026-05-30)
            or self.signature
        )


# Pyright/LSP hover markers that leak into nodes.signature during the LSP resolve
# pass. Pyright hover `contents` is markdown — ```python\n(method) def f(...) -> T\n```
# — and the raw markdown was rendering verbatim into the brief (defect D-2, observed
# 8/10 briefs on the 2026-06-04 ramp). Strip it to the bare signature line.
_HOVER_KIND_RE = re.compile(
    r"^\((?:method|function|property|variable|class|parameter|field|constant|module|overload)\)\s*"
)


def _sanitize_signature(sig: str) -> str:
    """Strip leaked LSP/Pyright hover markdown from a stored signature.

    Reduces ```python\\n(method) def wait(self, ...) -> None\\n``` to ``def wait(self,
    ...) -> None``. No-op on already-clean ``def ...`` / ``name(...)`` signatures
    (fast path). Correct-or-quiet: a structurally-balanced-but-wrong fence is exactly
    the "plausible-but-wrong context" that drops agent accuracy 6-11pp (The Distracting
    Effect, arXiv:2505.06914, 2025) — so it is removed, not rendered. Language-agnostic
    (operates on fences/markers, not Python AST).
    """
    if not sig:
        return sig
    s = sig.strip()
    if "```" not in s and not s.startswith("("):
        return s  # already clean — no hover markdown
    s = s.replace("```python", " ").replace("```", " ")
    cleaned: list[str] = []
    for ln in s.splitlines():
        ln = _HOVER_KIND_RE.sub("", ln.strip()).strip()
        if ln:
            cleaned.append(ln)
    if not cleaned:
        return ""
    # Prefer the first line that looks like a signature (has an arg list).
    for ln in cleaned:
        if "(" in ln:
            return ln
    return cleaned[0]


def _node_meta(conn: sqlite3.Connection, node_ids: list[int]) -> tuple[str, str]:
    """Return (signature, return_type) for the lowest-line node in ``node_ids``."""
    if not node_ids:
        return ("", "")
    placeholders = ",".join("?" for _ in node_ids)
    try:
        row = conn.execute(
            f"SELECT signature, return_type FROM nodes WHERE id IN ({placeholders}) "
            f"ORDER BY start_line LIMIT 1",
            node_ids,
        ).fetchone()
    except sqlite3.Error:
        return ("", "")
    if not row:
        return ("", "")
    return (_sanitize_signature(row[0] or ""), row[1] or "")


def _read_props(conn: sqlite3.Connection, node_ids: list[int]) -> dict[str, list[str]]:
    """Return {kind: [value, ...]} for contract kinds, deduped, capped per kind.

    Order within a kind preserved by line (the source order the agent will see).
    """
    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    kind_ph = ",".join("?" for _ in _CONTRACT_KINDS)
    try:
        rows = conn.execute(
            f"SELECT kind, value FROM properties "
            f"WHERE node_id IN ({placeholders}) AND kind IN ({kind_ph}) "
            f"ORDER BY line",
            (*node_ids, *_CONTRACT_KINDS),
        ).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for kind, value in rows:
        if not value:
            continue
        # Repair any value the indexer stored mid-expression (blind byte cap on
        # an older binary build) so the brief never emits an unterminated literal
        # or a dangling operator. No-op on already-balanced short values
        # (e.g. "TypeError"); drops the value entirely if unrepairable.
        v = clip_balanced(str(value).strip())
        if not v:
            continue
        # SEMANTIC gate (C1b): a mined value must be a well-formed instance of its
        # kind, else it is a parsed statement fragment laundering as a contract
        # fact (the ``raises raise,exc_info[1].with_traceback`` beets garbage).
        # Drop it (correct-or-quiet). No-op for kinds without a validator.
        _validate = _CONTRACT_VALUE_VALIDATORS.get(kind)
        if _validate is not None and not _validate(v):
            continue
        bucket = out.setdefault(kind, [])
        seenset = seen.setdefault(kind, set())
        if v in seenset or len(bucket) >= _MAX_PER_KIND:
            continue
        seenset.add(v)
        bucket.append(v)
    return out


def _evidence_for(
    conn: sqlite3.Connection,
    file_path: str,
    name: str,
    *,
    is_callee: bool = False,
    ids: list[int] | None = None,
) -> ContractEvidence | None:
    """Build the ContractEvidence for one (file, function), or None if no node.

    ``ids`` may be passed pre-resolved so the caller (build_contract) does not
    run a second identical _node_ids query when it reuses them for callees.
    """
    if ids is None:
        ids = _node_ids(conn, file_path, name)
    if not ids:
        return None
    sig, ret = _node_meta(conn, ids)
    props = _read_props(conn, ids)
    shapes = props.get("return_shape", [])
    return ContractEvidence(
        file=file_path,
        function=name,
        signature=sig,
        return_type=ret,
        raises=tuple(props.get("exception_type", [])),
        guards=tuple(props.get("guard_clause", [])),
        return_shape=shapes[0] if shapes else "",
        boundaries=tuple(props.get("boundary_condition", [])),
        conditionals=tuple(props.get("conditional_return", [])),
        exc_flows=tuple(props.get("exception_flow", [])),
        flows=tuple(props.get("data_flow", [])),
        is_callee=is_callee,
    )


def build_contract(
    graph_db_path: str,
    focus: list[tuple[str, str]],
    *,
    include_callees: bool = True,
    max_callees: int = _MAX_CALLEES,
) -> list[ContractEvidence]:
    """Build the deterministic contract for each (file, function) in ``focus``.

    For each focus function: its own contract (signature + raises + guards + return
    shape, node-local, always-available). When ``include_callees`` and the function
    has VERIFIED 1-hop callees that themselves raise exceptions, append those callee
    contracts (the "what the thing I call can raise" lever) — verified edges only,
    so a name_match callee is never claimed. Pure read; never raises.
    """
    if not focus:
        return []
    conn = _open_ro(graph_db_path)
    if conn is None:
        return []
    try:
        # _has_columns drives only the callee edge query; skip the PRAGMA on the
        # inline brief path (include_callees=False).
        has_conf = has_method = False
        if include_callees:
            has_conf, has_method = _has_columns(conn)
        out: list[ContractEvidence] = []
        seen_funcs: set[tuple[str, str]] = set()
        for fpath, fname in focus:
            if not fpath or not fname or (fpath, fname) in seen_funcs:
                continue
            seen_funcs.add((fpath, fname))
            # Resolve node ids ONCE and reuse for both the contract and the
            # callee expansion (was two identical _node_ids queries per focus).
            ids = _node_ids(conn, fpath, fname)
            if not ids:
                continue
            ev = _evidence_for(conn, fpath, fname, ids=ids)
            if ev is None:
                continue
            out.append(ev)

            if not include_callees:
                continue
            # Verified 1-hop callees that ADD a raise the focus doesn't already
            # surface — the deciding "callee raises X" detail. Verified only.
            callees = _neighbors(
                conn,
                ids,
                direction="callees",
                has_conf=has_conf,
                has_method=has_method,
                max_neighbors=max_callees * 3,
            )
            added = 0
            for edge in callees:
                if added >= max_callees:
                    break
                # Verified-edge gate: never surface a name_match callee as a fact.
                if (edge.resolution_method or "").strip().lower() not in _DETERMINISTIC_METHODS:
                    continue
                if (edge.file, edge.name) in seen_funcs:
                    continue
                # Read the callee contract from the EXACT node the verified edge
                # resolved to (its edges.target_id), so sig AND props come from the
                # SAME node — not sig-over-lowest-line + props-over-the-same-name
                # union (item #17). Abstain if the resolved id can't be recovered.
                callee_id = _resolved_callee_node_id(
                    conn, ids, edge.name, edge.file, has_method=has_method
                )
                if callee_id is None:
                    continue
                cev = _evidence_for(
                    conn, edge.file, edge.name, is_callee=True, ids=[callee_id]
                )
                # Worth showing a callee if it raises/guards OR exposes a
                # signature — the signature is the deciding "call it correctly"
                # interface fact the agent otherwise greps for (Task #48).
                if cev is None or not (cev.raises or cev.guards or cev.signature):
                    continue
                seen_funcs.add((edge.file, edge.name))
                out.append(cev)
                added += 1
        return out
    finally:
        conn.close()


def _fmt_one(ev: ContractEvidence) -> str:
    """Render one function's contract as compact lines (signature first, primacy)."""
    head = f"{ev.file} :: {ev.function}"
    if ev.is_callee:
        head = f"  → calls {ev.function} ({ev.file})"
    lines = [head]
    # Render the signature for BOTH the edit-target AND its verified callees: a
    # callee's signature is the deciding "call it with these args" fact (Task #48).
    if ev.signature:
        lines.append(f"  sig: {ev.signature}")
    if ev.raises:
        lines.append(f"  raises: {', '.join(ev.raises)}")
    if ev.exc_flows:
        lines.append(f"  raises-when: {' | '.join(ev.exc_flows)}")
    if ev.guards:
        lines.append(f"  preserve: {' | '.join(ev.guards)}")
    if ev.boundaries:
        lines.append(f"  bounds: {' | '.join(ev.boundaries)}")
    if ev.flows:
        # def-use: where each input value flows (calls/comparisons/returns it feeds)
        lines.append(f"  flows: {' | '.join(ev.flows)}")
    if ev.return_shape:
        rt = f" ({ev.return_type})" if ev.return_type else ""
        lines.append(f"  returns: {ev.return_shape}{rt}")
    elif ev.return_type and ev.return_type != "None" and not ev.is_callee:
        lines.append(f"  returns: {ev.return_type}")
    return "\n".join(lines)


def render_contract(items: list[ContractEvidence]) -> str:
    """Render the contract block, or "" when nothing has signal (correct-or-quiet)."""
    blocks = [_fmt_one(ev) for ev in items if ev.has_signal]
    if not blocks:
        return ""
    return "<gt-contract>\n" + "\n".join(blocks) + "\n</gt-contract>"


def contract_line(graph_db_path: str, file_path: str, func_names: list[str]) -> str:
    """Compact single-function inline contract for the per-file brief entry.

    Returns one line like ``raises ValueError,TypeError | preserve not user→raise |
    returns Optional[User]`` for the FIRST function that has any contract signal, or
    "" when none. Used to add a ``Contract:`` line per brief file without the full
    block. No callee expansion (the inline form is the edit-target's own contract).
    """
    if not func_names:
        return ""
    items = build_contract(
        graph_db_path,
        [(file_path, fn) for fn in func_names[:3]],
        include_callees=False,
    )
    for ev in items:
        parts: list[str] = []
        if ev.raises:
            parts.append("raises " + ",".join(ev.raises))
        if ev.guards:
            parts.append("preserve " + "; ".join(ev.guards[:2]))
        if ev.return_shape:
            parts.append("returns " + ev.return_shape)
        elif ev.return_type and ev.return_type != "None":
            parts.append("returns " + ev.return_type)
        if ev.flows:
            # one def-use flow (the edit-target's first param) — compact, single line
            parts.append("flows " + ev.flows[0])
        if parts:
            return " | ".join(parts)
    return ""


@dataclass(frozen=True)
class CalleeContract:
    """One verified callee of an edit-target function: the signature + location
    the agent must call correctly. Built ONLY from deterministic edges (Task #48).
    """

    caller: str  # the edit-target function name
    callee: str  # the called function/method name
    signature: str  # the callee's full typed signature (the deciding fact)
    file: str  # callee definition file
    line: int  # callee definition start_line (1-based; 0 if unknown)


# NOTE: the legacy ``_node_sig_line(conn, file, name)`` (lowest-line over the
# same-name union) is intentionally GONE — it sourced sig+line from an arbitrary
# overload, not the node the verified edge resolved to (item #17). Callee sig/line
# are now read ONLY by the resolver's actual target id via the two helpers below.
def _node_sig_line_by_id(conn: sqlite3.Connection, node_id: int) -> tuple[str, int]:
    """(signature, start_line) for the EXACT node ``node_id`` the edge resolved to.

    The verified edge already picked a specific target node structurally (type_flow /
    impl_method / import / same_file). Read sig+line from THAT node so an overloaded /
    same-name-across-classes callee never gets the wrong overload's signature/line
    (item #17). Generalized — pure id lookup, any language.
    """
    try:
        row = conn.execute(
            "SELECT signature, start_line FROM nodes WHERE id = ?",
            (node_id,),
        ).fetchone()
    except sqlite3.Error:
        return ("", 0)
    if not row:
        return ("", 0)
    return (_sanitize_signature(str(row[0] or "")), int(row[1]) if row[1] is not None else 0)


def _resolved_callee_node_id(
    conn: sqlite3.Connection,
    source_ids: list[int],
    callee_name: str,
    callee_file: str,
    *,
    has_method: bool,
) -> int | None:
    """The EXACT target node id the verified CALLS edge resolved to.

    ``_neighbors`` returns only (name, file, confidence, resolution_method) — it
    discards the resolved ``edges.target_id``, so the callee's sig/line/props get
    re-derived by lowest ``start_line`` over the same-name union (an arbitrary
    overload). Recover the resolver's actual target id from the SAME edges join
    ``_neighbors`` used, gated on the SAME verified-method predicate, so sig/line/
    props are read from the node the edge truly points at (item #17).

    Correct-or-quiet: if the verified edge resolved to MULTIPLE distinct target ids
    with this (name, file) — genuinely two overloads both called over verified edges
    — prefer the lowest-line one AMONG ONLY those resolved ids (still pinned to the
    resolver's targets, never the whole file-wide union). Returns None when no
    verified edge to (name, file) exists (then the caller abstains). Generalized:
    pure SQL over edges/nodes, no language- or benchmark-specific logic.
    """
    if not source_ids:
        return None
    placeholders = ",".join("?" for _ in source_ids)
    sql = (
        "SELECT n.id, n.start_line FROM edges e JOIN nodes n ON e.target_id = n.id "
        f"WHERE e.source_id IN ({placeholders}) AND e.type = 'CALLS' "
        "AND n.is_test = 0 AND n.name = ? AND n.file_path = ?"
    )
    params: list[object] = [*source_ids, callee_name, callee_file]
    # Same verified-provenance gate _neighbors applies (only when the column exists);
    # without it the conf=0.0 sentinel path can't prove provenance, so don't filter.
    if has_method:
        _det_in = ",".join("'" + str(m).lower() + "'" for m in sorted(_DETERMINISTIC_METHODS))
        sql += f" AND LOWER(TRIM(e.resolution_method)) IN ({_det_in})"
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return None
    if not rows:
        return None
    # Pin to the resolver's target id(s); among those, lowest known line wins
    # (NULL line last) for a deterministic, edge-faithful pick.
    rows.sort(key=lambda r: (r[1] is None, r[1] if r[1] is not None else 0))
    return int(rows[0][0])


def edit_target_callee_contracts(
    graph_db_path: str,
    file_path: str,
    func_names: list[str],
    *,
    max_funcs: int = 3,
    max_callees_per_func: int = 3,
) -> list[CalleeContract]:
    """Verified callees (with signatures + locations) of the edit-target functions.

    For each focus function in ``func_names`` (capped), return its 1-hop CALLS
    callees resolved over a VERIFIED edge (resolution_method in same_file / import
    / lsp_verified / …) whose definition node carries a non-empty signature. This
    is the "what does the method I'm editing CALL, and how do I call it" fact — the
    deciding ``set_parse(self, key, string: str)`` the agent otherwise greps for.

    Correct-or-quiet: name_match edges are NEVER included (they would launder a
    guessed call target as fact). Returns [] when nothing verified has a signature.
    Pure read; never raises. Generalized — any file / language indexed by gt-index.
    """
    if not func_names:
        return []
    conn = _open_ro(graph_db_path)
    if conn is None:
        return []
    try:
        has_conf, has_method = _has_columns(conn)
        out: list[CalleeContract] = []
        seen: set[tuple[str, str, str]] = set()  # (caller, callee, file) dedup
        for fname in func_names[:max_funcs]:
            if not fname:
                continue
            ids = _node_ids(conn, file_path, fname)
            if not ids:
                continue
            callees = _neighbors(
                conn,
                ids,
                direction="callees",
                has_conf=has_conf,
                has_method=has_method,
                max_neighbors=max_callees_per_func * 3,
            )
            added = 0
            for edge in callees:
                if added >= max_callees_per_func:
                    break
                # Verified-edge gate: never surface a name_match callee as a fact.
                if (edge.resolution_method or "").strip().lower() not in _DETERMINISTIC_METHODS:
                    continue
                # Don't list the function calling itself or a same-name homonym in
                # the same file as a "callee contract" — it adds no interface fact.
                if edge.name == fname and edge.file == file_path:
                    continue
                # Read sig+line from the EXACT node the verified edge resolved to
                # (its edges.target_id), NOT the lowest-line node over the same-name
                # union — else an overload/same-name-method gets the wrong overload's
                # signature+line (item #17). Abstain if the resolved id can't be
                # recovered (correct-or-quiet) rather than emit an arbitrary overload.
                callee_id = _resolved_callee_node_id(
                    conn, ids, edge.name, edge.file, has_method=has_method
                )
                if callee_id is None:
                    continue
                sig, line = _node_sig_line_by_id(conn, callee_id)
                if not sig:
                    continue  # correct-or-quiet: no signature, no fact to send
                key = (fname, edge.name, edge.file)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    CalleeContract(
                        caller=fname,
                        callee=edge.name,
                        signature=sig,
                        file=edge.file,
                        line=line,
                    )
                )
                added += 1
        return out
    finally:
        conn.close()


def _callee_sig_args(signature: str, callee: str) -> str:
    """Render a callee signature compactly as ``name(args)``.

    Strips a leading ``def `` / ``async def `` and any ``-> ReturnType`` tail and a
    trailing colon so the brief shows ``set_parse(self, key, string: str)`` rather
    than ``def set_parse(self, key, string: str) -> None:``. Falls back to the raw
    signature when it does not parse as ``def name(...)``.
    """
    sig = signature.strip()
    for prefix in ("async def ", "def "):
        if sig.startswith(prefix):
            sig = sig[len(prefix):].strip()
            break
    # Cut a return annotation / trailing colon after the balanced arg list.
    depth = 0
    end = -1
    for i, ch in enumerate(sig):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end != -1:
        sig = sig[: end + 1]
    if "(" in sig and sig.endswith(")"):
        return sig
    # Unparseable — return name(args?) best-effort, never a malformed fact.
    return signature.strip().rstrip(":")


# ──────────────────────────────────────────────────────────────────────────
# Contract DRIFT — the NEW capability (not the already-shipped contract context).
#
# After the agent edits a file and GT re-indexes it (gt-index -file), diff the
# edit-target's OWN behavioral contract pre vs post and surface only MATERIAL
# drift: the interface facts a patch broke that the rest of the code depends on
# ("return shape: list -> None; N callers depend on this", "dropped raise:
# KeyError"). This is the deciding "you broke it" signal the agent self-certifies
# away — distinct from re-delivering the contract as context (a proven null).
#
# Keying is by (file, name), NEVER node_id: an incremental reindex is DELETE+
# INSERT so node ids change. Caller counts come from VERIFIED edges only (is_fact
# / _DETERMINISTIC_METHODS), is_test=0 by construction of the call graph — zero
# test contact (the assertions table is never read). Correct-or-quiet: empty
# string when nothing material changed. LLM-free, $0, pure SQL.
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContractDrift:
    """Material contract change in one edited function, with caller exposure."""

    file: str
    function: str
    changes: tuple[str, ...]  # rendered change lines
    caller_count: int = 0  # verified (non-test) callers that depend on it
    removed: bool = False  # node gone after reindex (renamed/removed)


def snapshot_contract(
    graph_db_path: str, file_path: str, func_names: list[str]
) -> dict[str, dict]:
    """Capture the edit-target's OWN contract before an edit, JSON-serializable,
    keyed by function name.

    The pre-edit touchpoint persists this (e.g. to /tmp) so ``build_drift`` can
    diff against it after the agent edits + GT re-indexes the file. Own-contract
    only (``include_callees=False``): drift is about what the EDITED function's
    interface became, not its callees'. Pure read; never raises.
    """
    if not func_names:
        return {}
    items = build_contract(
        graph_db_path,
        [(file_path, fn) for fn in func_names],
        include_callees=False,
    )
    snap: dict[str, dict] = {}
    for ev in items:
        snap[ev.function] = {
            "signature": ev.signature,
            "return_type": ev.return_type,
            "return_shape": ev.return_shape,
            "raises": list(ev.raises),
            "guards": list(ev.guards),
        }
    return snap


def _norm_shape(shape: str) -> str:
    """Normalize a return_shape for comparison: keep the category + the structural skeleton (call /
    constructor / method heads, brackets), but BLANK bare variable identifiers. NON-HARM: a local-
    variable rename (`return list(data)` -> `return list(result)`) changes the captured expression
    TEXT but not the contract, and must NOT read as drift — else drift fires noise on every refactor.
    A real structural change (list->dict, .foo()->.bar(), value->None) still differs."""
    if "|" not in shape:
        return shape
    cat, expr = shape.split("|", 1)

    def _repl(m: "re.Match[str]") -> str:
        rest = expr[m.end():].lstrip()
        return m.group(0) if rest.startswith("(") else "_"  # keep call/constructor/method heads

    return cat + "|" + re.sub(r"[A-Za-z_]\w*", _repl, expr)


def _diff_contract(pre: dict, post: dict) -> list[str]:
    """The material changes pre->post. Order-insensitive for set-valued kinds
    (a reorder is not drift). Correct-or-quiet: only well-formed deltas."""
    changes: list[str] = []
    pre_shape = (pre.get("return_shape") or "").strip()
    post_shape = (post.get("return_shape") or "").strip()
    # Compare STRUCTURE (variable-rename-invariant), not literal text -> no false drift on a rename.
    if _norm_shape(pre_shape) != _norm_shape(post_shape) and (pre_shape or post_shape):
        changes.append(f"return shape: {pre_shape or 'none'} -> {post_shape or 'none'}")
    # LSP-sharpened type-level drift (List[X] -> Optional[X]) when types are known.
    pre_rt = (pre.get("return_type") or "").strip()
    post_rt = (post.get("return_type") or "").strip()
    if pre_rt != post_rt and (pre_rt or post_rt):
        changes.append(f"return type: {pre_rt or 'none'} -> {post_rt or 'none'}")
    pre_raises = set(pre.get("raises") or [])
    post_raises = set(post.get("raises") or [])
    for dropped in sorted(pre_raises - post_raises):
        changes.append(f"dropped raise: {dropped}")
    for added in sorted(post_raises - pre_raises):
        changes.append(f"new raise: {added}")
    pre_guards = set(pre.get("guards") or [])
    post_guards = set(post.get("guards") or [])
    for dropped in sorted(pre_guards - post_guards):
        # CORRECT-OR-QUIET (offline-proof finding, arviz add-guard FP): the indexer captures only a
        # LIMITED set of guard_clauses per function, so ADDING a guard can displace the captured one
        # and look like a "drop". A guard is only really dropped if its exception ALSO disappeared
        # from raises. If the guard's exception is still raised post-edit, this is a capture artifact
        # (not a real drop) -> suppress, so drift never falsely tells the agent it broke a guard.
        _excs = re.findall(r"raise\s+([A-Za-z_][A-Za-z0-9_]*)", dropped)
        if _excs and any(e in post_raises for e in _excs):
            continue
        changes.append(f"dropped guard: {dropped}")
    return changes


def _verified_caller_count(graph_db_path: str, file_path: str, name: str) -> int:
    """Number of VERIFIED (fact) callers of (file, name). name_match callers are
    never counted — a guessed caller must not inflate the consequence.

    Item #14: use the dedicated UNCAPPED ``COUNT(DISTINCT)`` (curation_map.
    verified_caller_count) instead of counting the callers of a
    ``build_function_map(dynamic=False)`` result — that path truncated at
    ``max_neighbors=5``, so a hub with 30 verified callers reported 5 and the
    drift block understated blast radius 6x. A COUNT must never be subject to a
    presentation cap."""
    return verified_caller_count(graph_db_path, file_path, name)


def render_drift(drifts: list[ContractDrift]) -> str:
    """Render the drift block with verification framing folded in, or "" when no
    material drift (correct-or-quiet). Identical payload across scaffolds."""
    if not drifts:
        return ""
    blocks: list[str] = []
    for d in drifts:
        head = f"{d.file} :: {d.function}"
        if d.caller_count > 0:
            s = "s" if d.caller_count != 1 else ""
            head += f"  ({d.caller_count} verified caller{s} depend on this)"
        lines = [head] + [f"  {c}" for c in d.changes]
        blocks.append("\n".join(lines))
    framing = (
        "Your edit changed the behavioral contract below. Confirm each change is "
        "intended - callers depend on the prior contract:"
    )
    return "<gt-drift>\n" + framing + "\n" + "\n".join(blocks) + "\n</gt-drift>"


def build_drift(
    graph_db_path: str,
    file_path: str,
    func_names: list[str],
    *,
    pre_snapshot: dict[str, dict],
) -> str:
    """Diff the post-edit contract (read live from the re-indexed graph) against
    ``pre_snapshot`` and render material drift.

    Call AFTER the agent edits the file AND GT has re-indexed it (so the graph's
    properties reflect the post-edit body). ``pre_snapshot`` is the dict produced
    by ``snapshot_contract`` before the edit. A function present pre but absent
    post = renamed/removed (callers will break). Pure read; never raises; "" when
    nothing material changed.
    """
    if not func_names or not pre_snapshot:
        return ""
    post = snapshot_contract(graph_db_path, file_path, func_names)
    drifts: list[ContractDrift] = []
    for name in func_names:
        pre = pre_snapshot.get(name)
        if pre is None:
            continue  # no baseline for this name -> nothing to diff
        cur = post.get(name)
        if cur is None:
            drifts.append(
                ContractDrift(file_path, name, ("function removed or renamed",), 0, True)
            )
            continue
        changes = _diff_contract(pre, cur)
        if not changes:
            continue
        cnt = _verified_caller_count(graph_db_path, file_path, name)
        drifts.append(ContractDrift(file_path, name, tuple(changes), cnt))
    return render_drift(drifts)
