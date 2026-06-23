"""Structural (non-git) co-change: the completeness signal derived from the GRAPH.

WHY: the historical `cochanges` table is git-mined and empty on the eval surface (no
`.git` at the indexed root, gt_run_proof.py:176), so the completeness loss it caught is
dark. That loss is real and language-agnostic: the agent fixes the primary site and ships
a partial patch that misses a SIBLING method the hidden tests exercise — the cfn-3764
`values()`/`value()` shape (both in one class, same empty-list bug, two hunks).

v2 design (precision-first, targeting-correct):
  - Scan the ISSUE-RELEVANT files (gold/issue-symbol files) and find name-TWIN method PAIRS
    *within* them (value<->values) — keyed on the FILE, not on a guessed edit_target (v1's
    bug: it keyed on the composite-top candidate `transform`/`context`, never the gold func).
  - TWIN = singular/plural or a >=4-char prefix family; dunders excluded; same-class ranked
    above same-file. Anchor-gated (an issue-term member ranks first) + capped to bound noise.
  - The v1 `import_mirror` signal is REMOVED: its query didn't verify the IMPORTS edge
    connected the two files (it surfaced any same-named method in any importing file =
    name_match noise, e.g. add()/add() across unrelated files). The case it targeted
    (aiogram get_value -> SceneWizard, a *new* method on a delegating wrapper) is not a
    name-twin or an existing mirror and needs a separate delegation signal.

Correct-or-quiet: returns [] when no twin pair is found.
"""
from __future__ import annotations

import sqlite3

_GENERIC = {"main", "run", "setup", "teardown", "wrapper", "inner", "decorator"}


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/").lstrip("./").lstrip("/")


def _stem_twin(a: str, b: str) -> bool:
    """True if a and b are plausible method twins by name shape (not identical)."""
    if not a or not b or a == b:
        return False
    la, lb = a.lower(), b.lower()
    if la.startswith("__") or lb.startswith("__"):
        return False
    if la in _GENERIC or lb in _GENERIC:
        return False
    # singular/plural: value <-> values, key <-> keys (any length)
    if la + "s" == lb or lb + "s" == la:
        return True
    # snake_case family: load <-> load_many, value <-> value_set. The longer name must
    # extend the shorter at an UNDERSCORE boundary (shorter + "_" + suffix). A bare shared
    # prefix (parse/parser, list/listen, read/reader, handle/handler, connect/connection)
    # is a COINCIDENTAL stem, not a co-change twin — emitting it misdirects the agent
    # (worse than silence, per correct-or-quiet), so reject it.
    short, long = (la, lb) if len(la) <= len(lb) else (lb, la)
    if len(short) >= 4 and long.startswith(short + "_"):
        return True
    return False


def twin_pairs_in_files(
    conn: sqlite3.Connection,
    files,
    anchor_terms=None,
    limit: int = 4,
) -> list[dict]:
    """Find name-twin method PAIRS within each of `files`.

    Each hit: {"a","b","file","same_class"}. Ranked: an anchor-term member first, then
    same-class pairs first. Deduped, capped at `limit`. [] when none (correct-or-quiet).
    """
    anchor = {str(a).lower() for a in (anchor_terms or set())}
    seen_files: set[str] = set()
    # group methods by their REAL file_path (the suffix fallback can return rows from
    # several real files; pair only WITHIN one file and report that file, never the query
    # stem — v2 dropped the real path and rendered a wrong location).
    by_file: dict[str, list] = {}
    for f in files or []:
        fp = _norm(f)
        if not fp or fp in seen_files:
            continue
        seen_files.add(fp)
        try:
            rows = conn.execute(
                "SELECT name, parent_id, file_path FROM nodes "
                "WHERE file_path = ? AND label IN ('Function','Method') "
                "AND COALESCE(is_test,0)=0",
                (fp,),
            ).fetchall()
            if not rows:  # path stored with a different prefix; fall back to suffix match
                rows = conn.execute(
                    "SELECT name, parent_id, file_path FROM nodes "
                    "WHERE file_path LIKE ? AND label IN ('Function','Method') "
                    "AND COALESCE(is_test,0)=0",
                    (f"%/{fp}",),
                ).fetchall()
        except sqlite3.Error:
            continue
        for r in rows:
            nm, par, real = r[0], r[1], _norm(r[2])
            if not nm or nm.startswith("__"):
                continue
            by_file.setdefault(real, []).append((nm, par))

    pairs: list[dict] = []
    for real, names in by_file.items():
        n = len(names)
        for i in range(n):
            for j in range(i + 1, n):
                a, pa = names[i]
                b, pb = names[j]
                if not _stem_twin(a, b):
                    continue
                # require SAME CLASS: a cross-method same-file twin that is not in the same
                # class is mostly coincidence (correct-or-quiet). The cfn-3764 target
                # (value/values, both methods of one class) is same-class.
                if pa is None or pa != pb:
                    continue
                pairs.append({"a": a, "b": b, "file": real, "same_class": True})

    def _rank(h: dict) -> tuple:
        amatch = h["a"].lower() in anchor or h["b"].lower() in anchor
        return (0 if amatch else 1, 0 if h["same_class"] else 1, h["a"])

    pairs.sort(key=_rank)
    out: list[dict] = []
    seen: set[tuple] = set()
    for h in pairs:
        k = (min(h["a"], h["b"]), max(h["a"], h["b"]), h["file"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out[:limit]
