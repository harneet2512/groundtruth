"""Structural edit-risk — time verification by the BLAST RADIUS of what was edited.

GT's deterministic, graph-derived answer to "which unverified edit is risky enough
to warrant a verification nudge" — the verified caller fan-in of an edited-but-
untested symbol, scored RELATIVE to the repo's own fan-in distribution. This is the
signal the 2026 verification-timing literature converged toward but never built on a
STRUCTURAL axis: the field targets where the MODEL is uncertain (perplexity / PRM /
entropy); GT targets where the CODE is structurally risky, because GT carries the
call graph and is LLM-free + deterministic.

Research basis:
  - risk-TARGETED (not uniform) verification: RisCoSet (arXiv 2605.12201, 2026);
    Anytime Verified Agents (TMLR, 2026); verification is ~8x compute so target the
    risky tail (When To Solve, When To Verify, COLM 2025).
  - prospective edit-risk gating: PreFlect (arXiv 2602.07187, 2026).
  - the failure it prevents — editing past a correct state unverified:
    Coherence Collapse (arXiv 2603.24631, 2026).
  - external deterministic control > agent self-assessment: Mirror (arXiv 2604.19809,
    2026 — an external constraint cut confident failures 76% vs self-calibration ~0%).

Properties (CLAUDE.md pillars):
  - Generalized: pure nodes/edges schema; CALLS fan-in works for EVERY language the
    indexer supports; no `ast`, no `.py` assumption.
  - Dynamic: risk is RELATIVE to the repo's own fan-in distribution (a hub in a small
    repo and a hub in a huge repo are each flagged against their OWN baseline), never
    a hardcoded caller threshold.
  - Correct-or-quiet: 0.0 + no reasons when the graph is absent or no edited symbol
    has VERIFIED dependents. A name_match (guessed) caller is NEVER blast radius.
  - LLM-free, deterministic, $0.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from typing import Iterable

# The ONE canonical fact-set every consumer surface gates on (curation_map). Imported
# with a fallback so edit_risk stays import-safe behind the gt_mini_patch bulkhead and
# in any harness where pretask is not importable; the literal mirrors curation_map and
# is kept in sync by test_edit_risk + the curation_map tests.
try:  # pragma: no cover - import shape varies by harness
    from groundtruth.pretask.curation_map import (
        DETERMINISTIC_RESOLUTION_METHODS as _DETERMINISTIC_METHODS,
    )
except Exception:  # pragma: no cover
    _DETERMINISTIC_METHODS = frozenset(
        {
            "same_file",
            "import",
            "import_type",
            "type_flow",
            "verified_unique",
            "inherited",
            "return_type",
            "lsp",
            "lsp_verified",
        }
    )

# The fact floor consumers already use: a CALLS edge below this is a name_match
# guess, not a verified dependent, so it must NOT count as blast radius.
_MIN_CONFIDENCE = 0.5
# The "notable dependents" quantile that defines the risky tail, computed per-repo
# (dynamic — not a caller-count magic number). A symbol at this fan-in scores ~0.5;
# above it scores higher. The top quartile is the conventional tail boundary.
_TAIL_Q = 0.75

# Depth trickle-down: blast radius counts incoming DEPENDENCY edges of ANY kind the
# promote pass produces (a symbol read/written/data-flowed by many is risky too, not
# only called-by-many). CONTAINS/IMPORTS/PRECEDES/CO_SERIALIZES are structural/ordering,
# not dependencies, so they are excluded. Absent edge types (old graphs) contribute 0
# -> degrades to CALLS-only, correct-or-quiet.
_DEPENDENCY_EDGE_TYPES = ("CALLS", "READS", "WRITES", "DATA_FLOW")

# Per-graph reference cache (the fan-in distribution is repo-invariant within a run;
# the verify producer fires repeatedly — recompute once, not every fire).
_REF_CACHE: dict = {}


@dataclass(frozen=True)
class SymbolRisk:
    name: str
    dependents: int  # DISTINCT verified incoming-dependency edges (the blast radius)
    risk: float  # 0..1, saturating, relative to the repo fan-in reference


@dataclass(frozen=True)
class EditRisk:
    score: float  # 0..1 — the MAX symbol risk (the riskiest edited symbol)
    reasons: tuple  # tuple[SymbolRisk, ...] high -> low
    reference_fanin: float  # the repo fan-in reference used (telemetry)

    def is_quiet(self) -> bool:
        return self.score <= 0.0 or not self.reasons

    def top_reason(self):
        return self.reasons[0] if self.reasons else None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))
    except sqlite3.Error:
        return False


def _normalize_path(f) -> str:
    """Repo-relative suffix form of one path: strip git ``a/``/``b/``/``./`` and any
    leading slash. Empty string for an empty/blank input."""
    s = str(f).strip().replace("\\", "/")
    for pre in ("a/", "b/", "./"):
        if s.startswith(pre):
            s = s[len(pre) :]
    return s.lstrip("/")


def _normalize_edited_files(edited_files) -> list[str]:
    """Repo-relative suffix forms of the agent's edited files (strip git a//b/ and
    leading ./), deduped. Used to scope risk to symbols the agent actually DEFINED in
    a file it edited — a same-named symbol defined in an UN-edited file (e.g. a callee
    hub like ``List.push``) is not the agent's change and must not be flagged."""
    out: list[str] = []
    for f in edited_files or []:
        s = _normalize_path(f)
        if s:
            out.append(s)
    return list(dict.fromkeys(out))


def _normalize_edited_ranges(edited_ranges) -> dict[str, list[tuple[int, int]]]:
    """Repo-rel-keyed map of edited (start,end) 1-based line hunks. Keys normalized
    like ``_normalize_edited_files``; each value coerced to a list of ordered
    (lo,hi) int pairs (lo<=hi, both >0). Malformed entries are dropped, never raised
    (correct-or-quiet substrate). Empty/None -> {}."""
    out: dict[str, list[tuple[int, int]]] = {}
    for f, ranges in (edited_ranges or {}).items():
        key = _normalize_path(f)
        if not key:
            continue
        clean: list[tuple[int, int]] = []
        for r in ranges or []:
            try:
                a, b = int(r[0]), int(r[1])
            except (TypeError, ValueError, IndexError):
                continue
            lo, hi = (a, b) if a <= b else (b, a)
            if lo > 0:
                clean.append((lo, hi))
        if clean:
            out.setdefault(key, []).extend(clean)
    return out


def _line_in_edited_ranges(file_path: str, start_line, ranges_by_file: dict) -> bool:
    """True iff ``start_line`` (a node's definition line) falls inside an edited hunk
    of the file ``file_path`` matches. The node's ``file_path`` is matched against the
    edited-file keys SUFFIX-tolerantly (repo-root vs git a//b/ prefixes), mirroring the
    file-scope match. A node with no usable ``start_line`` fails closed (excluded) when
    ranges are enforced — an unlocatable definition is not provably the agent's edit."""
    try:
        sl = int(start_line)
    except (TypeError, ValueError):
        return False
    if sl <= 0:
        return False
    npath = _normalize_path(file_path)
    for key, ranges in ranges_by_file.items():
        # suffix-tolerant file identity: the node path and the edited key name the
        # same file when either is a path-suffix of the other.
        if npath == key or npath.endswith("/" + key) or key.endswith("/" + npath):
            for lo, hi in ranges:
                if lo <= sl <= hi:
                    return True
    return False


def _percentile(sorted_vals: list, q: float) -> float:
    """Nearest-rank percentile of a NON-EMPTY ascending list; 0.0 if empty."""
    if not sorted_vals:
        return 0.0
    if q <= 0:
        return float(sorted_vals[0])
    if q >= 1:
        return float(sorted_vals[-1])
    k = max(1, int(round(q * len(sorted_vals))))
    return float(sorted_vals[min(k, len(sorted_vals)) - 1])


def _repo_fanin_reference(conn, conf_clause: str, method_clause: str, conf_params) -> float:
    """75th-percentile of NON-ZERO verified dependency fan-in across the repo — the
    'notable dependents' baseline that defines the risky tail (dynamic, per-repo)."""
    types_in = ",".join("?" for _ in _DEPENDENCY_EDGE_TYPES)
    try:
        rows = conn.execute(
            f"""SELECT COUNT(DISTINCT e.source_id) AS c
                  FROM edges e
                 WHERE e.type IN ({types_in}) {conf_clause} {method_clause}
                 GROUP BY e.target_id
                HAVING c > 0""",
            (*_DEPENDENCY_EDGE_TYPES, *conf_params),
        ).fetchall()
    except sqlite3.Error:
        return 0.0
    vals = sorted(int(r[0]) for r in rows if r and r[0])
    return _percentile(vals, _TAIL_Q)


def _ranged_dependents(
    conn,
    nm,
    types_in,
    conf_clause,
    method_clause,
    file_clause,
    conf_params,
    file_params,
    ranges_by_file,
) -> int:
    """Distinct verified incoming-dependency sources of the nodes named ``nm`` whose
    DEFINITION line is inside an edited hunk (R3). Two passes: (1) the candidate target
    nodes (name + file-scope), filtered to those whose ``start_line`` is in their file's
    edited ranges; (2) the distinct verified dependency fan-in over only those survivors.
    Returns 0 when no node's definition falls in an edited range (correct-or-quiet)."""
    try:
        cand = conn.execute(
            f"""SELECT nt.id, nt.file_path, nt.start_line
                  FROM nodes nt
                 WHERE LOWER(nt.name) = LOWER(?) {file_clause}""",
            (nm, *file_params),
        ).fetchall()
    except sqlite3.Error:
        return 0
    target_ids = [
        int(r[0]) for r in cand if r and _line_in_edited_ranges(r[1], r[2], ranges_by_file)
    ]
    if not target_ids:
        return 0
    ids_in = ",".join("?" for _ in target_ids)
    try:
        row = conn.execute(
            f"""SELECT COUNT(DISTINCT e.source_id)
                  FROM edges e
                 WHERE e.type IN ({types_in})
                   AND e.target_id IN ({ids_in})
                   {conf_clause} {method_clause}""",
            (*_DEPENDENCY_EDGE_TYPES, *target_ids, *conf_params),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row and row[0] else 0


def structural_edit_risk(
    graph_db,
    symbols: Iterable[str],
    *,
    min_confidence: float = _MIN_CONFIDENCE,
    max_reasons: int = 3,
    edited_files: Iterable[str] | None = None,
    edited_ranges: dict | None = None,
) -> EditRisk:
    """Structural risk of the edited-but-untested ``symbols``, by VERIFIED caller
    fan-in scored relative to the repo. Correct-or-quiet on any absence.

    ``symbols`` are matched case-insensitively against node names; only CALLS edges
    at/above ``min_confidence`` count (a name_match guess is never blast radius).

    ``edited_files`` (when non-empty) SCOPES the match to symbols DEFINED in a file the
    agent edited: a node whose ``file_path`` is not one of these is excluded, because a
    same-named symbol defined elsewhere (a callee hub like ``List.push`` reached via a
    ``x.push(...)`` line in the diff body) is not the agent's change and must never be
    flagged as its blast radius. Empty/None -> no file constraint (back-compat; the
    repo fan-in REFERENCE is always whole-repo, only the per-symbol match is scoped).

    ``edited_ranges`` (when non-empty) TIGHTENS the file scope to the agent's edited
    LINE HUNKS: a per-repo-rel-file map of edited ``[(start,end), ...]`` 1-based ranges.
    A matched node counts ONLY when its ``start_line`` (its DEFINITION line) falls inside
    one of its file's edited ranges — i.e. the agent edited the symbol's DEFINITION, not
    merely REFERENCED its name on an edited line. This closes R3: a hub (``fastapi`` /
    ``push`` / ``run``) defined high in an edited file but only *called* in the agent's
    actual hunk (def-line outside the hunk) no longer wins on degree. Enforced only for
    files present in the map AND only when the graph carries a ``start_line`` column;
    None/empty (or an old graph without ``start_line``) -> unchanged file-scope (back-
    compat). Fail-closed: a node with no usable ``start_line`` is excluded when ranges
    are enforced for its file (an unlocatable definition is not provably the edit)."""
    names = {str(s).strip() for s in (symbols or []) if str(s).strip()}
    if not graph_db or not names or not os.path.isfile(str(graph_db)):
        return EditRisk(0.0, (), 0.0)
    try:
        conn = sqlite3.connect(str(graph_db))
    except sqlite3.Error:
        return EditRisk(0.0, (), 0.0)
    reasons: list = []
    try:
        has_conf = _column_exists(conn, "edges", "confidence")
        conf_clause = "AND e.confidence >= ?" if has_conf else ""
        conf_params = (float(min_confidence),) if has_conf else ()
        # Blast radius counts ONLY VERIFIED dependents (I3 / curation_map): the canonical
        # fact-set DETERMINISTIC_RESOLUTION_METHODS, PLUS the promote-pass dependency-DEPTH
        # edges (READS/WRITES/DATA_FLOW carry 'promote_%' provenance — non-inventing, both
        # endpoints real). A bare NOT-LIKE-name_match blacklist is NOT enough: it admits
        # impl_method/unique_method, which resolve obj.method() by GLOBAL name-uniqueness
        # with NO receiver-type check (curation_map:98-110) — a cross-receiver/external
        # collision (Array.push -> List.push), never a real dependent. Gating to
        # (fact-set ∪ promote_%) makes edit_risk count the SAME facts every other surface
        # does (generalized, not a per-repo patch); the per-symbol count AND the repo
        # reference distribution use this same clause, so the baseline stays consistent.
        has_method = _column_exists(conn, "edges", "resolution_method")
        if has_method:
            _det_in = ",".join(f"'{m}'" for m in sorted(_DETERMINISTIC_METHODS))
            method_clause = (
                f"AND (LOWER(TRIM(e.resolution_method)) IN ({_det_in}) "
                f"OR LOWER(COALESCE(e.resolution_method,'')) LIKE 'promote_%')"
            )
        elif has_conf:
            # No provenance column -> cannot prove a dependent is verified. Fail closed to
            # a CERTIFIED-grade floor so a 0.6 name_match/impl_method guess cannot launder
            # into blast radius (parity with gt_mini_patch._scope_fact_clause).
            method_clause = "AND COALESCE(e.confidence, 0) >= 0.9"
        else:
            # Neither provenance nor confidence -> cannot prove anything verified -> quiet.
            method_clause = "AND 1=0"
        types_in = ",".join("?" for _ in _DEPENDENCY_EDGE_TYPES)
        # File-scope the per-symbol match to the agent's EDITED files (when known):
        # only a node DEFINED in an edited file is the agent's change. Suffix match
        # tolerates prefix differences (repo-root vs git a//b/). The repo fan-in
        # REFERENCE stays whole-repo; only this match is scoped.
        edited_norm = _normalize_edited_files(edited_files)
        if edited_norm:
            _file_or = " OR ".join("nt.file_path = ? OR nt.file_path LIKE ?" for _ in edited_norm)
            file_clause = f"AND ({_file_or})"
            file_params: tuple = tuple(p for f in edited_norm for p in (f, f"%/{f}"))
        else:
            file_clause = ""
            file_params = ()
        # Line-range tightening (R3): enforce only when ranges are given AND the graph
        # carries start_line. Absence of either -> degrade to file-scope (back-compat).
        ranges_by_file = _normalize_edited_ranges(edited_ranges)
        has_start_line = _column_exists(conn, "nodes", "start_line")
        enforce_ranges = bool(ranges_by_file) and has_start_line
        cache_key = (str(graph_db), float(min_confidence))
        ref = _REF_CACHE.get(cache_key)
        if ref is None:
            ref = _repo_fanin_reference(conn, conf_clause, method_clause, conf_params)
            _REF_CACHE[cache_key] = ref
        for nm in names:
            if enforce_ranges:
                dependents = _ranged_dependents(
                    conn,
                    nm,
                    types_in,
                    conf_clause,
                    method_clause,
                    file_clause,
                    conf_params,
                    file_params,
                    ranges_by_file,
                )
            else:
                try:
                    row = conn.execute(
                        f"""SELECT COUNT(DISTINCT e.source_id)
                              FROM edges e
                              JOIN nodes nt ON e.target_id = nt.id
                             WHERE e.type IN ({types_in})
                               AND LOWER(nt.name) = LOWER(?)
                               {conf_clause} {method_clause} {file_clause}""",
                        (*_DEPENDENCY_EDGE_TYPES, nm, *conf_params, *file_params),
                    ).fetchone()
                except sqlite3.Error:
                    continue
                dependents = int(row[0]) if row and row[0] else 0
            if dependents <= 0:
                continue
            # Saturating, relative to the repo's notable-dependents baseline (floored
            # at 1 = the minimal non-trivial fan-in, so the score is always bounded).
            denom = dependents + max(ref, 1.0)
            risk = dependents / denom if denom > 0 else 0.0
            reasons.append(SymbolRisk(nm, dependents, risk))
    finally:
        conn.close()
    reasons.sort(key=lambda r: (-r.risk, -r.dependents, r.name))
    reasons = reasons[:max_reasons]
    score = reasons[0].risk if reasons else 0.0
    return EditRisk(score, tuple(reasons), float(ref))


def reset_reference_cache() -> None:
    """Clear the per-graph fan-in reference cache (tests / a fresh index)."""
    _REF_CACHE.clear()
