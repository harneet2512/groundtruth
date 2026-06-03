#!/usr/bin/env python3
"""gt_query — L4 GroundTruth query tool.

Reads graph.db (path from GT_GRAPH_DB env), looks up a symbol by name (or
qualified name), and emits a family-tagged briefing block:

  [LABEL] one-line evidence    [VERIFIED|POSSIBLE]

Families covered (from gt_intel.TAXONOMY_LABELS):
  CALLER    -> CALLER-BLIND-EDIT
  IMPORT    -> HALLUCINATED-IMPORT
  SIBLING   -> PATTERN-DIVERGENCE
  TEST      -> UNVERIFIED-EDIT
  IMPACT    -> BLAST-RADIUS
  TYPE      -> CONTRACT-BREAK
  PRECEDENT -> STYLE-DIVERGENCE

Confidence tiering (per gt_intel.MIN_CONFIDENCE = 0.7):
  edges with confidence >= 0.7 AND resolution_method in {same_file, import}
    -> [VERIFIED]
  edges with confidence >= 0.7 AND resolution_method == name_match
    -> [POSSIBLE]
  edges below 0.7 are dropped (admissibility gate).

Output is hard-capped at ~30 lines. The header line shows the resolved target
location; subsequent lines are evidence rows.

Telemetry:
  Appends one JSON line to $GT_INSTANCE_LOG_DIR/gt_query_calls.jsonl with
  {symbol, returned_lines, ts}. Track D's verifier reads the line count to
  fill the `[GT_LAYERS] L4=<count>` cell.

Exit codes:
  0  success (output may be "no results" — that's still a successful query)
  2  bad usage / missing GT_GRAPH_DB
  3  graph.db missing or unreadable
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

# ── Constants (mirror gt_intel.py for behavioural parity) ────────────────────
VERIFIED_RESOLUTIONS = frozenset({"same_file", "import", "name_match"})
# RC-04: legacy compile-time fallback. The runtime value is loaded from the
# graph.db `project_meta.min_confidence` row written by gt-index, falling back
# to live P50 of edges.confidence, falling back to 0.5 (brief-layer parity).
# Read via _resolve_min_confidence(conn) — DO NOT use this constant directly
# in SQL; call _conf_clause(has_conf, threshold=...) instead.
MIN_CONFIDENCE = 0.5
VERIFIED_TAG = "[VERIFIED]"
POSSIBLE_TAG = "[POSSIBLE]"

TAXONOMY_LABELS: dict[str, str] = {
    "CALLER":    "CALLER-BLIND-EDIT",
    "IMPORT":    "HALLUCINATED-IMPORT",
    "SIBLING":   "PATTERN-DIVERGENCE",
    "TEST":      "UNVERIFIED-EDIT",
    "IMPACT":    "BLAST-RADIUS",
    "TYPE":      "CONTRACT-BREAK",
    "CONTRACT":  "BEHAVIORAL-CONTRACT",
    "ROUTE":     "ROUTE-HANDLER",
    "PRECEDENT": "STYLE-DIVERGENCE",
}

MAX_LINES = 30
MAX_CALLERS = 5
MAX_CALLEES = 5
MAX_SIBLINGS = 3
MAX_TESTS = 3
MAX_IMPORTS = 3
MAX_PROPERTIES = 6
MAX_ASSERTIONS = 3

# Behavioral-contract property kinds (graph.db `properties` table), in priority
# order = "what the function does inside, that the agent must not break." This is
# the semantic DEPTH gt-index extracts; the skeleton (nodes+edges) carries none of
# it. Surfaced confidence-gated + budget-capped so the node IS its contract, not
# just its signature.
_CONTRACT_KINDS: tuple[str, ...] = (
    "return_shape", "conditional_return", "guard_clause", "boundary_condition",
    "exception_type", "exception_flow", "param", "field_read", "side_effect",
    "resource_pattern", "call_order", "structural_twin",
)


# ── Telemetry ────────────────────────────────────────────────────────────────
def _emit_telemetry(symbol: str, returned_lines: int) -> None:
    log_dir = os.environ.get("GT_INSTANCE_LOG_DIR")
    if not log_dir:
        return
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        rec = {"symbol": symbol, "returned_lines": returned_lines, "ts": time.time()}
        with open(Path(log_dir) / "gt_query_calls.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
    except OSError:
        # Telemetry failure is non-fatal.
        pass


# ── DB helpers ───────────────────────────────────────────────────────────────
def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
        return True
    except sqlite3.OperationalError:
        return False


def _conf_clause(has_conf: bool, alias: str = "e", threshold: float | None = None) -> str:
    if not has_conf:
        return ""
    t = MIN_CONFIDENCE if threshold is None else threshold
    return f" AND {alias}.confidence >= {t}"


_CONF_CACHE: dict[int, float] = {}


def _conf_for(conn: sqlite3.Connection) -> float:
    """Return the cached per-repo MIN_CONFIDENCE for this conn (RC-04)."""
    key = id(conn)
    cached = _CONF_CACHE.get(key)
    if cached is None:
        cached = _resolve_min_confidence(conn)
        _CONF_CACHE[key] = cached
    return cached


def _resolve_min_confidence(conn: sqlite3.Connection) -> float:
    """RC-04: read per-repo min_confidence from project_meta; fall back to
    0.5 (brief-layer parity) when the meta row is missing.

    The directive's contract: gt-index writes
    ``project_meta.min_confidence`` per-repo at index time (median of
    resolved edge confidences). Readers honour that value; if absent, fall
    back to a fixed 0.5 floor that matches the brief layer
    (gt_intel.MIN_CONFIDENCE) — never to a live P50 that on tiny / mostly-
    same_file/import graphs collapses to 1.0 and over-filters legitimate
    name_match (singleton, conf=0.9) edges.
    """
    try:
        row = conn.execute(
            "SELECT value FROM project_meta WHERE key = 'min_confidence'"
        ).fetchone()
        if row and row[0] is not None:
            try:
                v = float(row[0])
                # Clamp to a sane band so a degenerate index can't push the
                # threshold to 1.0 (over-filter) or 0.0 (no-op).
                if 0.0 < v <= 0.9:
                    return v
            except (TypeError, ValueError):
                pass
    except sqlite3.Error:
        pass
    return 0.5


def _resolution_in_clause() -> tuple[str, tuple[str, ...]]:
    methods = tuple(sorted(VERIFIED_RESOLUTIONS))
    return ",".join("?" * len(methods)), methods


def _tier_for(resolution_method: str | None) -> str:
    if resolution_method in ("same_file", "import"):
        return VERIFIED_TAG
    return POSSIBLE_TAG  # name_match or unknown


def _shorten(s: str, n: int = 110) -> str:
    s = (s or "").replace("\n", " ").replace("\r", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


# ── Symbol resolution ────────────────────────────────────────────────────────
LABELS_FILTER = "label IN ('Function','Method','Class','Interface')"


def _resolve_qualified(
    conn: sqlite3.Connection, symbol: str
) -> sqlite3.Row | None:
    """Resolve a dotted symbol like `Class.method` or `mod.Class.method`.

    The Go indexer (gt-index/internal/store/sqlite.go) stores parent_id as
    a self-reference: methods have parent_id pointing at the enclosing
    class. qualified_name is frequently empty on Python rows, so the only
    reliable way to find `Class.method` is to walk parent_id by name.

    Algorithm: split on '.', look up the leaf by name; if any candidate's
    parent (by parent_id chain) has a name matching the right-to-left tail
    of the qualifier path, accept that row.
    """
    parts = [p for p in symbol.split(".") if p]
    if len(parts) < 2:
        return None
    leaf = parts[-1]
    qualifiers = parts[:-1]  # e.g. ['mod', 'Class'] for mod.Class.method

    cur = conn.cursor()
    candidates = cur.execute(
        f"SELECT * FROM nodes WHERE name = ? AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC tie-breaker — was hiding tests on TDD repos
        # and on tasks where the fix touches a test. Sort by id ASC only.
        "ORDER BY id ASC",
        (leaf,),
    ).fetchall()
    if not candidates:
        return None

    # Build a parent_id -> Row index for the parents of every candidate
    # so we can chase the chain without N round-trips.
    parent_ids = {c["parent_id"] for c in candidates if c["parent_id"]}
    parents: dict[int, sqlite3.Row] = {}
    if parent_ids:
        rows = cur.execute(
            f"SELECT * FROM nodes WHERE id IN ({','.join('?' * len(parent_ids))})",
            tuple(parent_ids),
        ).fetchall()
        parents = {r["id"]: r for r in rows}

    # Score each candidate by how many qualifier segments match the
    # parent chain right-to-left. Highest score wins; ties go to the
    # non-test, lowest-id row (already pre-sorted by ORDER BY).
    best: tuple[int, sqlite3.Row] | None = None
    for cand in candidates:
        chain: list[str] = []
        node = cand
        seen: set[int] = set()
        while node["parent_id"] and node["parent_id"] not in seen:
            seen.add(node["parent_id"])
            parent = parents.get(node["parent_id"])
            if parent is None:
                break
            chain.append(parent["name"])
            node = parent
        # Match qualifiers right-to-left against chain (closest parent first)
        matched = 0
        for q, c_name in zip(reversed(qualifiers), chain):
            if q == c_name:
                matched += 1
            else:
                break
        if matched == len(qualifiers):
            return cand  # full match — accept immediately
        if best is None or matched > best[0]:
            best = (matched, cand)

    # Require at least one qualifier match before returning a partial
    # candidate; otherwise the caller's fallback path handles it.
    if best and best[0] >= 1:
        return best[1]
    return None


def _did_you_mean(matches: list[sqlite3.Row], limit: int = 5) -> str:
    names: list[str] = []
    seen: set[str] = set()
    for r in matches[:limit]:
        qn = r["qualified_name"] or r["name"]
        loc = f"{qn} ({r['file_path']}:{r['start_line']})" if r["start_line"] else qn
        if loc not in seen:
            seen.add(loc)
            names.append(loc)
    return ", ".join(names)


def resolve_symbol(
    conn: sqlite3.Connection, symbol: str
) -> tuple[sqlite3.Row | None, str | None]:
    """Find the best node matching `symbol`.

    Returns (row, hint). `hint` is non-None when the resolver couldn't pick
    a single match but has did-you-mean candidates (case-insensitive).

    Priority:
      1. Exact qualified_name match
      2. Suffix on qualified_name (e.g. `MyClass.foo`)
      3. Dotted form: split on '.' and walk parent_id chain (Class.method)
      4. Exact name match preferring non-test
      5. Case-insensitive fallback with did-you-mean hint
    """
    cur = conn.cursor()
    # 1. Exact qualified name
    row = cur.execute(
        f"SELECT * FROM nodes WHERE qualified_name = ? AND {LABELS_FILTER} LIMIT 1",
        (symbol,),
    ).fetchone()
    if row:
        return row, None

    # 2. Suffix on qualified_name
    row = cur.execute(
        f"SELECT * FROM nodes WHERE qualified_name LIKE ? AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC.
        "ORDER BY id ASC LIMIT 1",
        (f"%{symbol}",),
    ).fetchone()
    if row:
        return row, None

    # 3. Dotted form: parent_id chain walk
    if "." in symbol:
        row = _resolve_qualified(conn, symbol)
        if row:
            return row, None

    # 4. Exact name match, prefer non-test
    row = cur.execute(
        f"SELECT * FROM nodes WHERE name = ? AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC.
        "ORDER BY id ASC LIMIT 1",
        (symbol,),
    ).fetchone()
    if row:
        return row, None

    # 5. Case-insensitive fallback: collect candidates for did-you-mean.
    #    For dotted queries, fall back to the leaf name for the suggestion list.
    needle = symbol.split(".")[-1] if "." in symbol else symbol
    ci_rows = cur.execute(
        f"SELECT * FROM nodes WHERE LOWER(name) = LOWER(?) AND {LABELS_FILTER} "
        # RC-06: drop is_test ASC.
        "ORDER BY id ASC LIMIT 10",
        (needle,),
    ).fetchall()
    if not ci_rows:
        return None, None
    if len(ci_rows) == 1:
        # Unambiguous case-insensitive match — return it directly.
        return ci_rows[0], None
    # Multiple matches — return None with hint so caller can render
    # "did you mean: ..." instead of returning an arbitrary row.
    return None, _did_you_mean(ci_rows)


# ── Evidence queries ─────────────────────────────────────────────────────────
def get_callers(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence_column(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT s.name AS caller_name, s.qualified_name AS caller_qn,
               s.file_path AS caller_file, e.source_line AS line,
               e.resolution_method AS rm
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        WHERE e.target_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY (e.resolution_method = 'same_file') DESC,
                 (e.resolution_method = 'import') DESC,
                 -- RC-06: drop s.is_test ASC tie-breaker. Tests are
                 -- legitimate callers; suppressing them was hiding
                 -- evidence on TDD repos.
                 e.source_line ASC
        LIMIT ?
    """
    return list(conn.execute(sql, (target_id, *methods, MAX_CALLERS)))


def get_callees(conn: sqlite3.Connection, source_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence_column(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT t.name AS callee_name, t.qualified_name AS callee_qn,
               t.file_path AS callee_file, e.source_line AS line,
               e.resolution_method AS rm
        FROM edges e
        JOIN nodes t ON e.target_id = t.id
        WHERE e.source_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        ORDER BY (e.resolution_method = 'same_file') DESC,
                 (e.resolution_method = 'import') DESC
        LIMIT ?
    """
    return list(conn.execute(sql, (source_id, *methods, MAX_CALLEES)))


def get_tests(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    has_conf = _has_confidence_column(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT DISTINCT s.name AS test_name, s.file_path AS test_file,
               s.qualified_name AS test_qn, e.resolution_method AS rm
        FROM edges e
        JOIN nodes s ON e.source_id = s.id
        WHERE e.target_id = ? AND e.type = 'CALLS'
          AND s.is_test = 1
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        LIMIT ?
    """
    return list(conn.execute(sql, (target_id, *methods, MAX_TESTS)))


def get_siblings(conn: sqlite3.Connection, target: sqlite3.Row) -> list[sqlite3.Row]:
    """Functions in the same file (or same parent class) — pattern reference."""
    cur = conn.cursor()
    if target["parent_id"]:
        rows = cur.execute(
            "SELECT name, qualified_name, signature, start_line "
            "FROM nodes WHERE parent_id = ? AND id != ? "
            "AND label IN ('Function','Method') ORDER BY start_line LIMIT ?",
            (target["parent_id"], target["id"], MAX_SIBLINGS),
        ).fetchall()
        if rows:
            return rows
    return cur.execute(
        "SELECT name, qualified_name, signature, start_line "
        "FROM nodes WHERE file_path = ? AND id != ? "
        "AND label IN ('Function','Method') ORDER BY start_line LIMIT ?",
        (target["file_path"], target["id"], MAX_SIBLINGS),
    ).fetchall()


def get_imports_for_target(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """Files that import this symbol — gives canonical import path."""
    has_conf = _has_confidence_column(conn)
    sql = f"""
        SELECT DISTINCT e.source_file AS importer_file, e.metadata AS meta
        FROM edges e
        WHERE e.target_id = ? AND e.type = 'IMPORTS'
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
        LIMIT ?
    """
    try:
        return list(conn.execute(sql, (target_id, MAX_IMPORTS)))
    except sqlite3.OperationalError:
        return []


def caller_count(conn: sqlite3.Connection, target_id: int) -> int:
    has_conf = _has_confidence_column(conn)
    placeholders, methods = _resolution_in_clause()
    sql = f"""
        SELECT COUNT(*) FROM edges e
        WHERE e.target_id = ? AND e.type = 'CALLS'
          AND e.resolution_method IN ({placeholders})
          {_conf_clause(has_conf, threshold=_conf_for(conn))}
    """
    return int(conn.execute(sql, (target_id, *methods)).fetchone()[0])


# ── Output rendering ─────────────────────────────────────────────────────────
def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    try:
        conn.execute(f"SELECT 1 FROM {name} LIMIT 0")
        return True
    except sqlite3.Error:
        return False


def get_contract_properties(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    """Behavioral-contract properties for a node — the DEPTH the skeleton drops:
    guards, return shape, boundary conditions, exception flow, params, field reads,
    structural twin. Confidence-gated (>=0.5), prioritised by _CONTRACT_KINDS, capped
    to MAX_PROPERTIES. Empty when the graph has no `properties` table (correct-or-quiet)."""
    if not _table_exists(conn, "properties"):
        return []
    conf_clause = ""
    try:
        conn.execute("SELECT confidence FROM properties LIMIT 0")
        conf_clause = "AND COALESCE(confidence, 1.0) >= 0.5"
    except sqlite3.Error:
        pass
    placeholders = ",".join("?" * len(_CONTRACT_KINDS))
    rows = conn.execute(
        f"SELECT kind, value, line FROM properties "
        f"WHERE node_id = ? AND kind IN ({placeholders}) {conf_clause} ORDER BY line",
        (node_id, *_CONTRACT_KINDS),
    ).fetchall()
    order = {k: i for i, k in enumerate(_CONTRACT_KINDS)}
    rows = sorted(rows, key=lambda r: (order.get(r["kind"], 99), r["line"] or 0))
    return rows[:MAX_PROPERTIES]


def get_supertypes(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    """Inheritance / interface contract: the superclass(es) this node EXTENDS and
    interface(s) it IMPLEMENTS — the contract the agent must satisfy. Real edge
    names are EXTENDS / IMPLEMENTS (NOT the phantom 'INHERITS'/'DEFINES'). Empty
    when none / older graph (correct-or-quiet; language-conditional: EXTENDS on
    py/ts, IMPLEMENTS on go/rust/java)."""
    conf = ""
    if _has_confidence_column(conn):
        conf = "AND COALESCE(e.confidence, 0.5) >= 0.5"
    try:
        return conn.execute(
            f"SELECT e.type AS rel, t.name AS super_name, t.qualified_name AS super_qn, "
            f"t.file_path AS super_file "
            f"FROM edges e JOIN nodes t ON e.target_id = t.id "
            f"WHERE e.source_id = ? AND e.type IN ('EXTENDS','IMPLEMENTS') {conf} "
            f"LIMIT 5",
            (node_id,),
        ).fetchall()
    except sqlite3.Error:
        return []


def get_relationships(conn: sqlite3.Connection, node_id: int) -> list[sqlite3.Row]:
    """The remaining real relationship edges the call+import skeleton drops:
    COMPOSES (has-a), HANDLES_ROUTE (serves an API route), RE_EXPORTS (public
    re-export path). Real edge names only — never the phantom INHERITS/DEFINES.
    Correct-or-quiet; language/repo-conditional (HANDLES_ROUTE web repos, COMPOSES
    ts, RE_EXPORTS barrel/__init__ packages)."""
    conf = "AND COALESCE(e.confidence, 0.5) >= 0.5" if _has_confidence_column(conn) else ""
    try:
        return conn.execute(
            f"SELECT e.type AS rel, t.name AS tname, t.qualified_name AS tqn, "
            f"t.file_path AS tfile "
            f"FROM edges e JOIN nodes t ON e.target_id = t.id "
            f"WHERE e.source_id = ? AND e.type IN ('COMPOSES','HANDLES_ROUTE','RE_EXPORTS') {conf} "
            f"LIMIT 6",
            (node_id,),
        ).fetchall()
    except sqlite3.Error:
        return []


def get_assertions_content(conn: sqlite3.Connection, target_id: int) -> list[sqlite3.Row]:
    """The actual assertions covering this node (expression + expected) — so the
    agent sees WHAT the tests require, not just the test name. Empty when no
    `assertions` table / no linked assertions (correct-or-quiet)."""
    if not _table_exists(conn, "assertions"):
        return []
    return conn.execute(
        "SELECT expression, expected FROM assertions "
        "WHERE target_node_id = ? AND expression IS NOT NULL AND expression != '' "
        "LIMIT ?",
        (target_id, MAX_ASSERTIONS),
    ).fetchall()


def render(conn: sqlite3.Connection, target: sqlite3.Row) -> list[str]:
    out: list[str] = []
    qn = target["qualified_name"] or target["name"]
    loc = f"{target['file_path']}:{target['start_line']}" if target["start_line"] else target["file_path"]
    out.append(f"# gt_query: {qn} @ {loc}")

    sig = target["signature"] or ""
    rt = target["return_type"] or ""
    if sig:
        out.append(f"[{TAXONOMY_LABELS['TYPE']}] signature: {_shorten(sig, 140)} {VERIFIED_TAG}")
    if rt:
        out.append(f"[{TAXONOMY_LABELS['TYPE']}] returns: {_shorten(rt, 80)} {VERIFIED_TAG}")

    # BEHAVIORAL CONTRACT (the depth: what the function does inside — guards, return
    # shape, boundaries, exceptions, params, field reads, structural twin). Beyond the
    # signature, this is what the agent must preserve to not break the function.
    for p in get_contract_properties(conn, target["id"]):
        kind = str(p["kind"]).replace("_", " ")
        line_s = f" [L{p['line']}]" if p["line"] else ""
        out.append(
            f"[{TAXONOMY_LABELS['CONTRACT']}] {kind}: "
            f"{_shorten(p['value'], 110)}{line_s} {VERIFIED_TAG}"
        )

    # SUPERTYPE CONTRACT — inheritance / interface (real edges EXTENDS / IMPLEMENTS;
    # inheritance lives HERE, not under the phantom 'INHERITS'). An override must
    # keep the base/interface contract — this is the relationship the skeleton drops.
    for s in get_supertypes(conn, target["id"]):
        rel = "extends" if s["rel"] == "EXTENDS" else "implements"
        out.append(
            f"[{TAXONOMY_LABELS['CONTRACT']}] {rel} {s['super_qn'] or s['super_name']} "
            f"({s['super_file']}) - base contract applies {VERIFIED_TAG}"
        )
    # OTHER RELATIONSHIPS the call/import skeleton drops: composition, API route,
    # public re-export path.
    for r in get_relationships(conn, target["id"]):
        tgt = r["tqn"] or r["tname"]
        if r["rel"] == "HANDLES_ROUTE":
            out.append(f"[{TAXONOMY_LABELS['ROUTE']}] handles route {tgt} {VERIFIED_TAG}")
        elif r["rel"] == "RE_EXPORTS":
            out.append(f"[{TAXONOMY_LABELS['IMPORT']}] re-exported as {tgt} {VERIFIED_TAG}")
        else:  # COMPOSES
            out.append(
                f"[{TAXONOMY_LABELS['CONTRACT']}] composes {tgt} "
                f"({r['tfile']}) {VERIFIED_TAG}"
            )

    # CALLER + IMPACT
    n_callers = caller_count(conn, target["id"])
    if n_callers >= 3:
        out.append(
            f"[{TAXONOMY_LABELS['IMPACT']}] {n_callers} callers — "
            f"changes here propagate broadly. {VERIFIED_TAG if n_callers >= 5 else POSSIBLE_TAG}"
        )
    callers = get_callers(conn, target["id"])
    for c in callers:
        tag = _tier_for(c["rm"])
        line = f":{c['line']}" if c["line"] else ""
        out.append(
            f"[{TAXONOMY_LABELS['CALLER']}] called by "
            f"{c['caller_qn'] or c['caller_name']} at {c['caller_file']}{line} {tag}"
        )

    # CALLEES (under SIBLING-ish framing — what this fn relies on)
    callees = get_callees(conn, target["id"])
    for c in callees[:3]:
        tag = _tier_for(c["rm"])
        out.append(
            f"[{TAXONOMY_LABELS['SIBLING']}] calls "
            f"{c['callee_qn'] or c['callee_name']} ({c['callee_file']}) {tag}"
        )

    # IMPORTS
    imports = get_imports_for_target(conn, target["id"])
    for imp in imports:
        out.append(
            f"[{TAXONOMY_LABELS['IMPORT']}] importable from "
            f"{imp['importer_file']} {VERIFIED_TAG}"
        )

    # SIBLINGS
    siblings = get_siblings(conn, target)
    for s in siblings:
        ssig = _shorten(s["signature"] or s["name"], 90)
        out.append(
            f"[{TAXONOMY_LABELS['SIBLING']}] sibling: {s['qualified_name'] or s['name']} "
            f"-- {ssig} {VERIFIED_TAG}"
        )

    # TESTS
    tests = get_tests(conn, target["id"])
    for t in tests:
        tag = _tier_for(t["rm"])
        out.append(
            f"[{TAXONOMY_LABELS['TEST']}] tested by {t['test_qn'] or t['test_name']} "
            f"({t['test_file']}) {tag}"
        )
    # what the tests actually ASSERT (expression + expected) — the invariant the
    # edit must keep true, not just the name of the covering test.
    for a in get_assertions_content(conn, target["id"]):
        exp = _shorten(a["expression"], 100)
        want = f" -> {_shorten(a['expected'], 40)}" if a["expected"] else ""
        out.append(f"[{TAXONOMY_LABELS['TEST']}] asserts: {exp}{want} {VERIFIED_TAG}")

    # PRECEDENT (best-effort: nodes table has no commit field, use file path
    # mtime hint). We omit unless metadata is available.
    if target["end_line"] and target["start_line"]:
        span = target["end_line"] - target["start_line"]
        out.append(
            f"[{TAXONOMY_LABELS['PRECEDENT']}] body spans {span} lines "
            f"({target['file_path']}:{target['start_line']}-{target['end_line']})"
        )

    if len(out) > MAX_LINES:
        out = out[:MAX_LINES] + [f"# (truncated to {MAX_LINES} lines)"]
    return out


# ── Main ─────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> int:
    if len(argv) < 2 or not argv[1].strip():
        print("usage: gt_query <symbol>", file=sys.stderr)
        _emit_telemetry(argv[1] if len(argv) > 1 else "", 0)
        return 2
    symbol = argv[1].strip()

    db_path = os.environ.get("GT_GRAPH_DB")
    if not db_path:
        print("gt_query: GT_GRAPH_DB environment variable not set", file=sys.stderr)
        _emit_telemetry(symbol, 0)
        return 2
    if not Path(db_path).exists():
        print(f"gt_query: graph.db not found at {db_path}", file=sys.stderr)
        _emit_telemetry(symbol, 0)
        return 3

    try:
        # RC-04: dropped `immutable=1` — that flag is a CALLER PROMISE that the
        # file is unchanging, but the gt-index writer can run concurrently and
        # incremental reindexes mutate graph.db mid-loop. Keep mode=ro only;
        # accept the locking cost (with busy_timeout) instead of risking
        # torn-read 0-row returns. Run PRAGMA integrity_check on first connect
        # so a corrupt DB surfaces as `db_corrupt` (exit 4) instead of silent
        # empty results.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
        conn.execute("PRAGMA busy_timeout = 5000")
        ic = conn.execute("PRAGMA integrity_check").fetchone()
        if ic is None or ic[0] != "ok":
            print(f"gt_query: db_corrupt: {ic[0] if ic else 'unknown'}", file=sys.stderr)
            _emit_telemetry(symbol, 0)
            return 4
    except sqlite3.Error as e:
        print(f"gt_query: cannot open graph.db: {e}", file=sys.stderr)
        _emit_telemetry(symbol, 0)
        return 3
    conn.row_factory = sqlite3.Row
    # RC-04: warm the per-repo MIN_CONFIDENCE cache for this conn.
    _conf_for(conn)

    try:
        target, hint = resolve_symbol(conn, symbol)
        if target is None:
            if hint:
                print(
                    f"# gt_query: no exact symbol '{symbol}' — did you mean: {hint}"
                )
                _emit_telemetry(symbol, 1)
                return 0
            msg = f"# gt_query: no symbol named '{symbol}' in graph.db"
            print(msg)
            _emit_telemetry(symbol, 1)
            return 0
        lines = render(conn, target)
        out = "\n".join(lines)
        print(out)
        _emit_telemetry(symbol, len(lines))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
