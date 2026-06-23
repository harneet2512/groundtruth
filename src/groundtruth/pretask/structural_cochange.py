"""Structural (non-git) co-change: the completeness signal derived from the GRAPH,
not from git history.

WHY this exists: the historical co-change table (`cochanges`) is git-mined, and on the
SWE-bench eval surface the indexed repo has no `.git` (gt_run_proof.py:176), so that table
is empty on every task. But the completeness loss it was meant to catch is real and
PYTHON-relevant: the agent fixes the primary edit target and ships a partial patch that
misses a sibling site the hidden tests exercise (e.g. `values()` fixed, sibling `value()`
missed; `FSMContext.get_value` added, mirror `SceneWizard` missed).

This module recovers that signal from facts already in graph.db — no git, language-agnostic:
  S1 same-class siblings   : nodes sharing the target's parent_id (the class). High precision.
  S2 name-stem twins       : a sibling whose name is a singular/plural or prefix variant of
                             the target (value<->values, load<->loads, get<->get_all).
  S3 import-mirror         : a same-named method in a DIFFERENT file that is graph-connected
                             to the target's file by an IMPORTS edge (the wrapper/mirror layer).

Correct-or-quiet: returns [] when no structural sibling is found; never guesses.
"""
from __future__ import annotations

import sqlite3


def _stem_twin(a: str, b: str) -> bool:
    """True if a and b are plausible method twins by name shape (not identical)."""
    if a == b or not a or not b:
        return False
    la, lb = a.lower(), b.lower()
    # singular/plural: value <-> values, key <-> keys
    if la + "s" == lb or lb + "s" == la:
        return True
    # prefix family sharing a 4+ char stem: get <-> get_all, load <-> load_many
    if len(la) >= 4 and len(lb) >= 4 and (la.startswith(lb) or lb.startswith(la)):
        return True
    return False


def structural_cochange(
    conn: sqlite3.Connection,
    func_name: str,
    file_path: str,
    limit: int = 4,
) -> list[dict]:
    """Return structural co-change partners of (func_name, file_path) from graph.db.

    Each hit: {"name", "file", "reason"} where reason in
    {"same_class_sibling", "name_twin", "import_mirror"}. Highest-precision first,
    deduped, capped at `limit`. Returns [] when nothing structural is found.
    """
    if not func_name or not file_path:
        return []
    fp = file_path.replace("\\", "/").lstrip("./").lstrip("/")
    try:
        rows = conn.execute(
            "SELECT id, parent_id, file_path FROM nodes "
            "WHERE name = ? AND file_path LIKE ? AND COALESCE(is_test,0)=0 LIMIT 1",
            (func_name, f"%{fp}"),
        ).fetchall()
    except sqlite3.Error:
        return []
    if not rows:
        return []
    tgt_id, tgt_parent, tgt_file = rows[0][0], rows[0][1], rows[0][2]

    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(name: str, f: str, reason: str) -> None:
        if not name or not f:
            return
        key = (name, f)
        if key in seen or (name == func_name and f == tgt_file):
            return
        seen.add(key)
        hits.append({"name": name, "file": f, "reason": reason})

    # S1 + S2: same-class siblings (same parent_id), name-twins ranked above bare siblings.
    if tgt_parent is not None:
        try:
            sibs = conn.execute(
                "SELECT name, file_path FROM nodes "
                "WHERE parent_id = ? AND id != ? AND COALESCE(is_test,0)=0 "
                "AND label IN ('Function','Method')",
                (tgt_parent, tgt_id),
            ).fetchall()
        except sqlite3.Error:
            sibs = []
        for nm, f in sibs:
            if _stem_twin(func_name, nm or ""):
                _add(nm, f, "name_twin")
        # bare same-class siblings only if we still have room and found a twin or nothing
        for nm, f in sibs:
            if _stem_twin(func_name, nm or ""):
                continue
            _add(nm, f, "same_class_sibling")

    # S3: import-mirror — same-named method in a file IMPORTS-connected to the target file.
    try:
        mirrors = conn.execute(
            "SELECT n.name, n.file_path FROM nodes n "
            "JOIN edges e ON (e.type='IMPORTS') "
            "WHERE n.name = ? AND n.file_path != ? AND COALESCE(n.is_test,0)=0 "
            "AND n.label IN ('Function','Method') "
            "AND ( (e.source_file LIKE ? AND n.file_path = e.source_file) "
            "   OR (e.source_file = n.file_path) ) "
            "LIMIT 8",
            (func_name, tgt_file, f"%{fp}"),
        ).fetchall()
    except sqlite3.Error:
        mirrors = []
    for nm, f in mirrors:
        _add(nm, f, "import_mirror")

    # name_twin first, then import_mirror, then bare sibling
    order = {"name_twin": 0, "import_mirror": 1, "same_class_sibling": 2}
    hits.sort(key=lambda h: order.get(h["reason"], 9))
    return hits[:limit]
