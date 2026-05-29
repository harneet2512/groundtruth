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

import sqlite3
from dataclasses import dataclass

from groundtruth.pretask.curation_map import (
    _DETERMINISTIC_METHODS,
    _has_columns,
    _neighbors,
    _node_ids,
    _open_ro,
)
from groundtruth.runtime.sanitizer import clip_balanced

# Tier A — populated in every indexed db (verified empirically 2026-05-29).
_TIER_A_KINDS = ("exception_type", "guard_clause", "return_shape")
# Tier B — richer kinds the current binary extracts (older task dbs lack them).
_TIER_B_KINDS = ("boundary_condition", "conditional_return", "exception_flow")
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
            or (self.signature and not self.is_callee)
        )


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
    return (row[0] or "", row[1] or "")


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
                cev = _evidence_for(conn, edge.file, edge.name, is_callee=True)
                # Only worth showing a callee if it raises or guards.
                if cev is None or not (cev.raises or cev.guards):
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
    if ev.signature and not ev.is_callee:
        lines.append(f"  sig: {ev.signature}")
    if ev.raises:
        lines.append(f"  raises: {', '.join(ev.raises)}")
    if ev.exc_flows:
        lines.append(f"  raises-when: {' | '.join(ev.exc_flows)}")
    if ev.guards:
        lines.append(f"  preserve: {' | '.join(ev.guards)}")
    if ev.boundaries:
        lines.append(f"  bounds: {' | '.join(ev.boundaries)}")
    if ev.return_shape:
        rt = f" ({ev.return_type})" if ev.return_type else ""
        lines.append(f"  returns: {ev.return_shape}{rt}")
    elif ev.return_type and not ev.is_callee:
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
        elif ev.return_type:
            parts.append("returns " + ev.return_type)
        if parts:
            return " | ".join(parts)
    return ""
