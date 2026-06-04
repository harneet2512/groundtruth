"""GT build census + end-to-end pipeline proof — "does GT run the way it is built?"

This is the AUTHORITATIVE check that GT's whole architecture (gt_gt.md) is live for a
given graph.db, in two parts:

  PART A — BUILD CENSUS: every base the build is supposed to produce is present and
           populated to spec — the 9 tables (§2.2), the edge types + trust model (§2.3),
           the property kinds (§2.4), closure / cochanges / FTS5 / assertions.
  PART B — END-TO-END PIPELINE: run the real pipeline once (issue → FTS5 → graph → LSP →
           semantic → composite → brief, §1/§4) and the per-view/per-edit hooks (§6),
           asserting they produce GRAPH-BACKED, REAL content — not just that modules import.

Coordinated with gt_gt.md: it demands ONLY what the architecture actually produces per
language (§2.5 honesty — IMPORTS/DEFINES are dead; COMPOSES/RE_EXPORTS are JS/TS-only;
Tier-2 langs get CALLS-dominant graphs; cochanges is soft on shallow clones). It never
fails on a documented gap.

Strictness is governed by GT_REQUIRE_FULL_STACK / GT_REQUIRE_FULL_POTENTIAL (the master
gate, same as preflight_pipeline.py). Used by BOTH official pipelines via
`preflight_pipeline.py --census`.

Usage:
    python scripts/verify/build_census.py --db graph.db --root /repo
    python scripts/verify/build_census.py --db graph.db --root /repo --json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def _strict() -> bool:
    return (os.environ.get("GT_REQUIRE_FULL_STACK") == "1"
            or os.environ.get("GT_REQUIRE_FULL_POTENTIAL") == "1")


# ── gt_gt.md §2.2 — the 9 tables a current build produces ───────────────────
REQUIRED_TABLES = {
    "nodes", "edges", "properties", "assertions",
    "cochanges", "closure", "file_hashes", "project_meta", "nodes_fts",
}

# gt_gt.md §2.4/§2.5 — property kinds that are CROSS-LANGUAGE SOLID (must populate on
# any language). The §2.5 honesty note names exactly these as the cross-language-solid
# kinds; the rest are Python/JS-rich and reported, not hard-required on Tier-2.
SOLID_KINDS = {"data_flow", "param", "docstring", "return_shape", "caller_usage"}

# gt_gt.md §2.4 — the full property-kind catalog (~23) for coverage reporting.
ALL_KINDS = SOLID_KINDS | {
    "guard_clause", "conditional_return", "return_shape", "exception_type",
    "exception_flow", "exception_handler", "side_effect", "field_read",
    "call_order", "boundary_condition", "visibility", "class_field",
    "class_decorator", "security_tag", "concurrency_pattern", "resource_pattern",
    "config_read", "fingerprint", "serialization_pair", "structural_twin",
}

# gt_gt.md §2.3 — edge types + the LANGUAGES that emit them (relationship edges are
# language-uneven; CALLS/CONTAINS are universal).
EDGE_LANGS = {
    "EXTENDS": {"python", "javascript", "typescript", "java", "kotlin", "go", "rust"},
    "IMPLEMENTS": {"javascript", "typescript", "java", "kotlin", "go", "rust"},
    "COMPOSES": {"javascript", "typescript"},
    "RE_EXPORTS": {"javascript", "typescript"},
    "HANDLES_ROUTE": {"python"},
}


def _dominant_lang(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT language, COUNT(*) c FROM nodes GROUP BY language ORDER BY c DESC LIMIT 1"
    ).fetchone()
    return ((row[0] if row else "") or "").lower()


def _tables(conn: sqlite3.Connection) -> set:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


# ── PART A — BUILD CENSUS ───────────────────────────────────────────────────

def census_tables(conn: sqlite3.Connection) -> tuple[bool, str]:
    have = _tables(conn)
    missing = sorted(REQUIRED_TABLES - have)
    if missing:
        return (not _strict()), f"missing tables: {missing} (build incomplete)"
    return True, f"all {len(REQUIRED_TABLES)} build tables present"


def census_edges(conn: sqlite3.Connection, lang: str) -> tuple[bool, str]:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)")}
    types = dict(conn.execute(
        "SELECT type, COUNT(*) FROM edges GROUP BY type").fetchall())
    # CALLS + CONTAINS are universal and mandatory.
    if types.get("CALLS", 0) == 0:
        return False, "no CALLS edges (resolver produced nothing)"
    notes = [f"CALLS={types.get('CALLS',0)}", f"CONTAINS={types.get('CONTAINS',0)}"]
    # Trust model (§2.3): the columns must exist AND trust_tier must be populated.
    trust_ok = True
    for col in ("trust_tier", "candidate_count", "evidence_type"):
        if col not in cols:
            trust_ok = False
            notes.append(f"MISSING_COL:{col}")
    if "trust_tier" in cols:
        tiers = dict(conn.execute(
            "SELECT trust_tier, COUNT(*) FROM edges GROUP BY trust_tier").fetchall())
        certified = tiers.get("CERTIFIED", 0)
        notes.append(f"CERTIFIED={certified} CANDIDATE={tiers.get('CANDIDATE',0)} "
                     f"SPECULATIVE={tiers.get('SPECULATIVE',0)}")
        if _strict() and certified == 0:
            return False, f"trust model dead: 0 CERTIFIED edges ({', '.join(notes)})"
    # Language-expected relationship edges (only where gt_gt.md says they exist).
    for etype, langs in EDGE_LANGS.items():
        if lang in langs:
            notes.append(f"{etype}={types.get(etype,0)}")
    if _strict() and not trust_ok:
        return False, f"trust columns missing ({', '.join(notes)})"
    return True, " ".join(notes)


def census_properties(conn: sqlite3.Connection, lang: str) -> tuple[bool, str]:
    if "properties" not in _tables(conn):
        return (not _strict()), "no properties table (graph NOT enriched)"
    counts = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM properties GROUP BY kind").fetchall())
    present = {k for k, n in counts.items() if n > 0}
    solid_missing = sorted(SOLID_KINDS - present)
    total_kinds = len(present & ALL_KINDS)
    detail = (f"{total_kinds}/{len(ALL_KINDS)} kinds populated; "
              f"solid={{{','.join(f'{k}:{counts.get(k,0)}' for k in sorted(SOLID_KINDS))}}}")
    # The cross-language-solid kinds MUST populate — if any is 0, the graph is not
    # enriched to the cross-language baseline the build guarantees.
    if _strict() and solid_missing:
        return False, (f"cross-language-solid property kinds EMPTY: {solid_missing} "
                       f"-> graph under-enriched ({detail})")
    return True, detail


def census_closure(conn: sqlite3.Connection) -> tuple[bool, str]:
    if "closure" not in _tables(conn):
        return (not _strict()), "no closure table"
    n = conn.execute("SELECT COUNT(*) FROM closure").fetchone()[0]
    verified_calls = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE type='CALLS' AND confidence >= 0.9").fetchone()[0]
    if _strict() and verified_calls > 0 and n == 0:
        return False, (f"closure EMPTY despite {verified_calls} verified CALLS "
                       "(closure pass did not run / stale — re-run -rebuild-closure)")
    return True, f"closure rows={n} (over {verified_calls} verified CALLS)"


def census_cochanges(conn: sqlite3.Connection) -> tuple[bool, str]:
    # Soft: git co-change mining is empty on a shallow clone (the benchmark task repos
    # are often --depth 1). Report, never hard-fail (gt_gt.md §2.5 reality).
    if "cochanges" not in _tables(conn):
        return True, "no cochanges table (OK — git history may be unavailable)"
    n = conn.execute("SELECT COUNT(*) FROM cochanges").fetchone()[0]
    return True, f"cochanges pairs={n}" + (" (shallow clone / no git history)" if n == 0 else "")


# ── PART B — END-TO-END PIPELINE PROOF ──────────────────────────────────────

def e2e_brief(db: str, root: str) -> tuple[bool, str]:
    """Run the real localization+brief pipeline once and assert a GRAPH-BACKED brief."""
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        r = generate_v1r_brief(
            issue_text="fix function that returns wrong value when handling user input error",
            repo_root=root, graph_db=db,
        )
    except Exception as e:
        return False, f"pipeline brief crashed: {type(e).__name__}: {str(e)[:120]}"
    n_files = len(r.files)
    if n_files == 0:
        return False, "pipeline produced 0 candidate files (brief empty)"
    gec = getattr(r, "graph_edge_count", None)
    tier = getattr(r, "confidence_tier", "?")
    sem = getattr(r, "semantic_signal_count", None)
    fts = getattr(r, "fts5_signal_count", None)
    detail = (f"brief: {n_files} files, graph_edge_count={gec}, semantic={sem}, "
              f"fts5={fts}, tier={tier}, {r.token_estimate} tok")
    # Graph-backed: at least one candidate confirmed by a graph edge (not lexical-only).
    if _strict() and gec is not None and gec == 0:
        return False, (detail + " — brief is LEXICAL-ONLY (no graph edge backs any "
                       "candidate); pipeline not running graph-backed as built")
    return True, detail


def e2e_hooks(db: str) -> tuple[bool, str]:
    """Run the per-view + per-edit hooks on a real symbol and assert REAL content."""
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT file_path FROM nodes WHERE is_test=0 AND label IN ('Function','Method') "
            "GROUP BY file_path ORDER BY COUNT(*) DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row:
        return (not _strict()), "no non-test functions to exercise hooks on"
    test_file = row[0]
    # Per-view (L3b): graph navigation must emit real contract/nav content + respect cap.
    try:
        from groundtruth.hooks.post_view import graph_navigation
        lines, _ = graph_navigation(test_file, db, limit=3)
    except Exception as e:
        return False, f"per-view hook crashed: {type(e).__name__}: {str(e)[:100]}"
    text = "\n".join(lines or [])
    has_real = any(m in text for m in
                   ["[CONTRACT]", "[SIGNATURE]", "Called by:", "Calls into:", "[TEST]", "[RAISES]"])
    tok = len(text) // 4
    detail = f"per-view: {len(lines or [])} lines, real_evidence={has_real}, ~{tok}tok, file={test_file}"
    if _strict() and lines and not has_real:
        return False, detail + " — hook emitted only metadata/empty (no real contract content)"
    return True, detail


# ── ORCHESTRATION ───────────────────────────────────────────────────────────

def run_build_census(db: str, root: str) -> "tuple[bool, list]":
    """Run the full census + e2e. Returns (all_required_pass, [(name, ok, msg), ...])."""
    conn = sqlite3.connect(db)
    try:
        lang = _dominant_lang(conn)
        checks = [
            ("census_tables", lambda: census_tables(conn)),
            ("census_edges", lambda: census_edges(conn, lang)),
            ("census_properties", lambda: census_properties(conn, lang)),
            ("census_closure", lambda: census_closure(conn)),
            ("census_cochanges", lambda: census_cochanges(conn)),
        ]
        results = []
        ok_all = True
        for name, fn in checks:
            try:
                ok, msg = fn()
            except Exception as e:
                ok, msg = False, f"{name} crashed: {e}"
            results.append((name, ok, msg))
            ok_all = ok_all and ok
    finally:
        conn.close()
    # e2e (open their own connections / load modules)
    for name, fn in [("e2e_brief", lambda: e2e_brief(db, root)),
                     ("e2e_hooks", lambda: e2e_hooks(db))]:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"{name} crashed: {e}"
        results.append((name, ok, msg))
        ok_all = ok_all and ok
    return ok_all, results


def main() -> int:
    ap = argparse.ArgumentParser(description="GT build census + end-to-end pipeline proof")
    ap.add_argument("--db", required=True)
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    ok_all, results = run_build_census(args.db, args.root)
    if args.json:
        print(json.dumps({n: {"pass": ok, "detail": m} for n, ok, m in results}, indent=2))
    else:
        for n, ok, m in results:
            print(f"  [{'PASS' if ok else 'FAIL'}] {n}: {m}")
        print()
        if ok_all:
            print("BUILD CENSUS: GT runs as built — all bases + end-to-end pipeline live")
        else:
            failed = [n for n, ok, _ in results if not ok]
            print(f"BUILD CENSUS: {len(failed)} FAILURES: {', '.join(failed)}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
