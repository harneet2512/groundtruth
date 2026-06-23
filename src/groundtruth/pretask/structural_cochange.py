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
    # prefix family sharing a >=4 char stem: load <-> load_many (NOT get/set/add — 3-char,
    # too common -> would spam). One name must be a prefix of the other.
    if len(la) >= 4 and len(lb) >= 4 and (la.startswith(lb) or lb.startswith(la)):
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
    pairs: list[dict] = []
    for f in files or []:
        fp = _norm(f)
        if not fp or fp in seen_files:
            continue
        seen_files.add(fp)
        try:
            rows = conn.execute(
                "SELECT name, parent_id FROM nodes "
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
                rows = [(r[0], r[1]) for r in rows]
        except sqlite3.Error:
            continue
        names = [(r[0], r[1]) for r in rows if r[0] and not r[0].startswith("__")]
        n = len(names)
        for i in range(n):
            for j in range(i + 1, n):
                a, pa = names[i]
                b, pb = names[j]
                if not _stem_twin(a, b):
                    continue
                same_class = pa is not None and pa == pb
                pairs.append({"a": a, "b": b, "file": fp, "same_class": same_class})

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
