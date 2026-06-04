"""Preflight pipeline verification — runs BEFORE the agent starts.

Tests every GT layer with the actual graph.db built for this task.
If any check fails, the failure is a GT infrastructure bug, not a
task failure. Run after gt-index + LSP enrichment, before agent launch.

Usage:
    python scripts/verify/preflight_pipeline.py --db /tmp/gt_prebuilt.db --root /tmp/testbed_src

Exit code 0 = all checks pass. Non-zero = broken layer (see output).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


# --- Strictness master gate -------------------------------------------------
# A paid/leaderboard run arms GT_REQUIRE_FULL_STACK=1. Historically several
# checks gated only on GT_REQUIRE_FULL_POTENTIAL, so a workflow that set
# FULL_STACK/REQUIRE_EMBEDDER but not FULL_POTENTIAL would let a degraded graph
# through. FULL_STACK is now the master: it implies every sub-requirement. The
# older FULL_POTENTIAL flag is still honored for back-compat.
def _strict() -> bool:
    return (
        os.environ.get("GT_REQUIRE_FULL_STACK") == "1"
        or os.environ.get("GT_REQUIRE_FULL_POTENTIAL") == "1"
    )


def _require_embedder() -> bool:
    return _strict() or os.environ.get("GT_REQUIRE_EMBEDDER") == "1"


def _require_lsp() -> bool:
    return _strict() or os.environ.get("GT_REQUIRE_LSP") == "1"


def _require_fts5() -> bool:
    return _strict() or os.environ.get("GT_REQUIRE_FTS5") == "1"


# Minimum verified-edge ratio for a real run. Below this the graph is
# name_match-guess-dominated and ranking on it is unsound (gt_gt.md §2.3 trust
# model: name_match is a guess, not a fact). 0.30 = at least ~a third of edges
# are CERTIFIED/import/same_file/lsp facts. Repos with <20 edges are exempt
# (too small for the ratio to be meaningful — the count gate already covers them).
MIN_VERIFIED_EDGE_RATIO = 0.30
MIN_EDGES_FOR_RATIO_GATE = 20


def check_graph_exists(db: str) -> tuple[bool, str]:
    if not os.path.exists(db):
        return False, f"graph.db not found at {db}"
    conn = sqlite3.connect(db)
    nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    conn.close()
    if nodes == 0:
        return False, f"graph.db has 0 nodes (indexer failed)"
    if edges == 0:
        return False, f"graph.db has 0 edges (resolver failed)"
    return True, f"nodes={nodes} edges={edges}"


def check_schema_version(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "project_meta" not in tables:
            return False, "project_meta table missing"
        row = conn.execute(
            "SELECT value FROM project_meta WHERE key='schema_version'"
        ).fetchone()
        if not row:
            return False, "schema_version not stamped (L3b will crash)"
        return True, f"schema_version={row[0]}"
    finally:
        conn.close()


def check_fts5(db: str) -> tuple[bool, str]:
    # STRICT under any require flag: a real paid run must have FTS5 GO-BUILT at index
    # time (the -tags sqlite_fts5 binary), NOT a runtime Python rebuild — the rebuild
    # is slower, can tokenize differently, and (30-task run) returned empty. Reject it.
    require_gobuilt = (
        os.environ.get("GT_REQUIRE_FULL_POTENTIAL") == "1"
        or os.environ.get("GT_REQUIRE_FULL_STACK") == "1"
        or os.environ.get("GT_REQUIRE_FTS5") == "1"
    )
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "nodes_fts" in tables:
            count = conn.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            if count <= 0:
                return False, "nodes_fts present but EMPTY (Go FTS5 population failed)"
            return True, f"nodes_fts exists ({count} entries, Go-built)"
        if require_gobuilt:
            return False, ("nodes_fts ABSENT — gt-index built WITHOUT -tags sqlite_fts5. "
                           "Refusing the runtime Python rebuild under strict gate.")
        # Lenient (non-strict) fallback only: Python-side creation
        try:
            from groundtruth.pretask.graph_localizer import _FTS5_CREATE, _FTS5_POPULATE
            conn2 = sqlite3.connect(db)
            conn2.execute(_FTS5_CREATE)
            conn2.execute(_FTS5_POPULATE)
            conn2.commit()
            count = conn2.execute("SELECT COUNT(*) FROM nodes_fts").fetchone()[0]
            conn2.close()
            return True, f"nodes_fts created Python-side ({count} entries)"
        except sqlite3.Error as e:
            return False, f"FTS5 unavailable: {e}"
    finally:
        conn.close()


def check_edge_quality(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
        if "confidence" not in cols:
            return False, "no confidence column (old schema)"
        total = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        verified = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE confidence >= 0.9"
        ).fetchone()[0]
        pct = (verified / total * 100) if total > 0 else 0
        if "resolution_method" in cols:
            methods = conn.execute(
                "SELECT resolution_method, COUNT(*) FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC LIMIT 5"
            ).fetchall()
            method_str = ", ".join(f"{m}:{c}" for m, c in methods)
        else:
            method_str = "no resolution_method column"
        ratio = (verified / total) if total > 0 else 0.0
        detail = f"verified(>=0.9)={verified}/{total} ({pct:.0f}%) methods=[{method_str}]"
        # Fail-closed: a name_match-guess-dominated graph is not a fact graph.
        if _strict() and total >= MIN_EDGES_FOR_RATIO_GATE and ratio < MIN_VERIFIED_EDGE_RATIO:
            return False, (
                f"DEGRADED graph: verified-edge ratio {ratio:.2f} < {MIN_VERIFIED_EDGE_RATIO} "
                f"({detail}) — graph is name_match-guess-dominated, refusing to rank on it")
        return True, detail
    finally:
        conn.close()


def check_data_flow(db: str) -> tuple[bool, str]:
    """Contract-ENRICHMENT dimension: the per-parameter forward-slice 'data_flow'
    property must populate. 0 rows = the graph indexed (nodes/edges) but is NOT
    enriched — the stale/broken-binary regression where every consumer (brief /
    post_edit / post_view / gt_query) reads nothing. STRICT under require flags
    (mirrors the deepswe_preindex aggregate guard, now also per-task on every path)."""
    require = (os.environ.get("GT_REQUIRE_FULL_POTENTIAL") == "1"
               or os.environ.get("GT_REQUIRE_FULL_STACK") == "1")
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "properties" not in tables:
            return (not require), "no properties table (graph NOT enriched)"
        n = conn.execute(
            "SELECT COUNT(*) FROM properties WHERE kind='data_flow'"
        ).fetchone()[0]
        if n > 0:
            return True, f"data_flow rows={n} (enriched)"
        return (not require), ("0 data_flow rows — graph indexed but NOT enriched "
                               "(stale/broken binary; consumers read nothing)")
    finally:
        conn.close()


def check_assertions(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "assertions" not in tables:
            return True, "no assertions table (OK for non-test repos)"
        count = conn.execute("SELECT COUNT(*) FROM assertions").fetchone()[0]
        linked = conn.execute(
            "SELECT COUNT(*) FROM assertions WHERE target_node_id > 0"
        ).fetchone()[0]
        return True, f"assertions={count} linked_to_target={linked}"
    finally:
        conn.close()


def check_lsp_enrichment(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        if "return_type" not in cols:
            return True, "no return_type column (old schema, LSP enrichment N/A)"
        enriched = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL AND return_type != ''"
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE label IN ('Function','Method')"
        ).fetchone()[0]
        return True, f"return_type populated: {enriched}/{total} functions"
    finally:
        conn.close()


def check_lsp_edges(db: str) -> tuple[bool, str]:
    """Delivery check (not schema): catches F2/F3 — an installed LSP server but
    ZERO lsp-resolved edges means the precision pass did not actually run (POSIX
    file:// URI bug, handshake failure, or the server is absent). The existing
    lsp_enrichment check only verifies the return_type COLUMN, which passes even
    with 0 lsp edges. Hard-FAIL when a server IS installed yet wrote nothing.
    """
    import shutil
    _SERVERS = {
        "python": "pyright-langserver", "py": "pyright-langserver",
        "go": "gopls", "rust": "rust-analyzer", "rs": "rust-analyzer",
        "typescript": "typescript-language-server", "ts": "typescript-language-server",
        "javascript": "typescript-language-server", "js": "typescript-language-server",
    }
    conn = sqlite3.connect(db)
    try:
        lsp = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE resolution_method='lsp'").fetchone()[0]
        row = conn.execute(
            "SELECT language, COUNT(*) c FROM nodes GROUP BY language ORDER BY c DESC LIMIT 1"
        ).fetchone()
        lang = ((row[0] if row else "") or "").lower()
        node_cols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()}
        return_types = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE return_type IS NOT NULL AND return_type != ''"
        ).fetchone()[0] if "return_type" in node_cols else 0
    finally:
        conn.close()
    server = _SERVERS.get(lang)
    require = _require_lsp()
    if lsp > 0:
        return True, f"lsp edges={lsp} (lang={lang}, server={server})"
    if not server:
        return True, f"0 lsp edges; no known LSP server for lang='{lang}' (expected)"
    if not shutil.which(server):
        return (not require), (
            f"0 lsp edges; LSP server '{server}' NOT installed for lang={lang} "
            f"-> enrichment skipped (install for full potential)")
    # Server installed but 0 promoted edges. Some languages legitimately yield no
    # edge corrections (already-resolved CALLS) — accept ONLY if return_type/
    # signature enrichment proves the pass ran and wrote SOMETHING (spec A:
    # "populated return_type/signature plus a documented reason if edges are
    # impossible"). Zero edges AND zero enrichment under a server = real bug.
    if return_types > 0:
        return True, (
            f"0 lsp edges but return_type enrichment={return_types} populated "
            f"(lang={lang}, server={server}) — pass ran; no edge corrections needed")
    return False, (
        f"0 lsp edges AND 0 return_type enrichment but '{server}' IS installed for "
        f"lang={lang} -> enrichment RAN BUT WROTE NOTHING (URI/handshake bug — real GT bug)")


def check_semantic_embedder(root: str) -> tuple[bool, str]:
    """Catches F1 fail-closed: the semantic ranker needs a real embedder. This does
    NOT just check imports — it LOADS the local ONNX model and EMBEDS a probe, then
    asserts the vector is finite, non-zero, and the expected dimension. Under
    GT_REQUIRE_EMBEDDER / GT_REQUIRE_FULL_STACK a missing/zero/NaN embedder is a HARD
    fail (no silent collapse to 2-signal consensus). GT_FORCE_ONNX_EMBEDDER=1 means
    BOTH halves (run_v74 + localize) must resolve to the same ONNX surface — proven
    here by loading via the shared groundtruth.memory.enrich.embed loader (the one
    _OnnxEmbedderAdapter wraps in both halves).

    gt_gt.md §5/§7: GT_REQUIRE_EMBEDDER makes both halves raise instead of zeroing
    W_SEM; GT_FORCE_ONNX_EMBEDDER puts both on the identical container ONNX surface.
    """
    require = _require_embedder()
    force_onnx = os.environ.get("GT_FORCE_ONNX_EMBEDDER") == "1"

    # Real load + embed via the shared loader (offline ONNX only — no HF at runtime).
    try:
        from groundtruth.memory.enrich.embed import embed_query, get_embedding_model
        model = get_embedding_model()
        vec = embed_query("function returns user id error handling")
    except Exception as e:  # FileNotFoundError (no baked model), import error, etc.
        msg = (f"embedder did NOT load: {type(e).__name__}: {str(e)[:160]} "
               f"-> semantic ranker OFF (GT_FORCE_ONNX_EMBEDDER={force_onnx})")
        return (not require), (("DEGRADED: " + msg) if not require else msg)

    # Validate the actual output: finite, non-zero, consistent dimension.
    try:
        import math
        n = len(vec)
        if n == 0:
            return False, "embedder returned an EMPTY vector (dim=0)"
        if model.dim and n != model.dim:
            return False, f"embedder dim mismatch: got {n}, expected {model.dim}"
        if not all(math.isfinite(x) for x in vec):
            return False, f"embedder returned non-finite values (NaN/Inf) in {n}-d vector"
        norm = math.sqrt(sum(x * x for x in vec))
        if norm <= 1e-9:
            return False, (f"embedder returned a ZERO vector (norm={norm:.2e}) — this is the "
                           "silent-zero-fallback that degrades W_SEM to 0; refusing it")
    except Exception as e:
        return False, f"embedder output validation crashed: {e}"

    onnx_dir = getattr(model, "model_dir", "?")
    return True, (f"embedder OK: real {n}-d ONNX vector, norm={norm:.3f}, finite, "
                  f"path={onnx_dir} (force_onnx={force_onnx})")


def check_fts5_query(db: str) -> tuple[bool, str]:
    """Test that FTS5 actually works (not just table exists) by running a BM25 query.

    Uses a separate writable connection for FTS5 table creation (if needed)
    to avoid mutating state through the read connection.
    """
    conn = sqlite3.connect(db)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        if "nodes_fts" not in tables:
            # Attempt Python-side creation via a SEPARATE writable connection
            # (same pattern as check_fts5 and _fts5_candidates).
            try:
                from groundtruth.pretask.graph_localizer import _FTS5_CREATE, _FTS5_POPULATE
                conn2 = sqlite3.connect(db)
                try:
                    conn2.execute(_FTS5_CREATE)
                    conn2.execute(_FTS5_POPULATE)
                    conn2.commit()
                finally:
                    conn2.close()
                # Reopen the read connection to see the new table
                conn.close()
                conn = sqlite3.connect(db)
            except sqlite3.Error as e:
                return False, f"FTS5 unavailable and Python-side creation failed: {e}"

        # Pick a common token from the nodes table for testing
        sample_row = conn.execute(
            "SELECT name FROM nodes WHERE is_test = 0 AND length(name) >= 4 LIMIT 1"
        ).fetchone()
        if not sample_row:
            return True, "fts5_query: no non-test nodes to test with (empty graph)"

        test_token = sample_row[0]
        try:
            results = conn.execute(
                """SELECT rowid, name, bm25(nodes_fts, 1.0, 2.0, 0.5, 0.5) as score
                   FROM nodes_fts
                   WHERE nodes_fts MATCH ?
                   ORDER BY score LIMIT 5""",
                (f'"{test_token}"',),
            ).fetchall()
            return True, f"fts5_query OK: query for '{test_token}' returned {len(results)} hits"
        except sqlite3.Error as e:
            return False, f"fts5_query: BM25 query failed: {e}"
    finally:
        conn.close()


def check_grep_available() -> tuple[bool, str]:
    """Check that rg (ripgrep) is in PATH for grep-to-seed."""
    import shutil
    rg_path = shutil.which("rg")
    if rg_path:
        return True, f"rg available at {rg_path}"
    # Not a hard failure — Python walk fallback exists — but worth noting
    return True, "rg NOT in PATH (will use Python os.walk fallback — slower)"


def check_path_seeds(db: str) -> tuple[bool, str]:
    """Verify that file_path column has values matching expected patterns.

    Path-to-seed requires file_path entries with directory separators so
    path-component matching works. A flat graph (all file_path = basename only)
    would make path-to-seed ineffective.
    """
    conn = sqlite3.connect(db)
    try:
        total = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM nodes WHERE is_test = 0"
        ).fetchone()[0]
        if total == 0:
            return False, "path_seeds: no non-test files in graph"

        # Count files with at least one path separator
        with_sep = conn.execute(
            "SELECT COUNT(DISTINCT file_path) FROM nodes "
            "WHERE is_test = 0 AND (file_path LIKE '%/%' OR file_path LIKE '%\\%')"
        ).fetchone()[0]
        pct = (with_sep / total * 100) if total > 0 else 0

        # Sample a file to show the format
        sample = conn.execute(
            "SELECT file_path FROM nodes WHERE is_test = 0 LIMIT 1"
        ).fetchone()
        sample_str = sample[0] if sample else "N/A"

        if with_sep == 0:
            return False, (
                f"path_seeds: 0/{total} files have path separators "
                f"(path-to-seed will be ineffective). Sample: {sample_str}"
            )
        return True, (
            f"path_seeds OK: {with_sep}/{total} ({pct:.0f}%) files have "
            f"directory structure. Sample: {sample_str}"
        )
    finally:
        conn.close()


def check_brief_generation(db: str, root: str) -> tuple[bool, str]:
    try:
        from groundtruth.pretask.v1r_brief import generate_v1r_brief
        result = generate_v1r_brief(
            issue_text="test issue with some keywords like function error bug",
            repo_root=root,
            graph_db=db,
        )
        n_files = len(result.files)
        tok = result.token_estimate
        if n_files == 0:
            return False, "brief generated 0 candidates"
        return True, f"brief OK: {n_files} files, {tok} tokens"
    except Exception as e:
        return False, f"brief generation failed: {e}"


def check_l3b_delivery(db: str) -> tuple[bool, str]:
    conn = sqlite3.connect(db)
    try:
        # Pick a non-test file with functions
        row = conn.execute(
            "SELECT file_path FROM nodes WHERE is_test = 0 AND label IN ('Function','Method') "
            "GROUP BY file_path ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        if not row:
            return False, "no non-test functions in graph"
        test_file = row[0]
        conn.close()

        from groundtruth.hooks.post_view import graph_navigation
        lines, _ = graph_navigation(test_file, db, limit=3)
        if not lines:
            return True, f"graph_navigation returned empty for {test_file} (may be OK)"
        has_evidence = any(
            m in "\n".join(lines)
            for m in ["[CONTRACT]", "[SIGNATURE]", "Called by:", "Calls into:", "[TEST]"]
        )
        return True, f"L3b OK: {len(lines)} lines, evidence={has_evidence}, file={test_file}"
    except Exception as e:
        return False, f"L3b failed: {e}"


def check_prebuilt_graph(db: str) -> tuple[bool, str]:
    """Under GT_FORBID_PREBUILT_GRAPH=1 (leaderboard legitimacy), refuse a graph.db
    that was handed in via a prebuilt/cross-task path. A legitimate run builds the
    graph fresh in the current task job; pointing GT at GT_PREBUILT_GRAPH_DB is the
    illegitimate path. Deep provenance (creating-job id, fresh_index_built) is
    verified by scripts/verify/legitimacy.py; this is the env-level tripwire."""
    if os.environ.get("GT_FORBID_PREBUILT_GRAPH") != "1":
        return True, "prebuilt-graph guard not armed (GT_FORBID_PREBUILT_GRAPH!=1)"
    prebuilt = os.environ.get("GT_PREBUILT_GRAPH_DB", "").strip()
    if prebuilt:
        return False, (
            f"illegitimate_prebuilt_artifact_detected: GT_PREBUILT_GRAPH_DB={prebuilt} is set "
            f"under GT_FORBID_PREBUILT_GRAPH=1 — a fresh per-task index is required")
    return True, "no prebuilt graph path in env (fresh per-task index required)"


def _brief_candidate_files(db: str, root: str) -> "list[str]":
    from groundtruth.pretask.v1r_brief import generate_v1r_brief
    result = generate_v1r_brief(
        issue_text="fix function error when handling user request returns wrong value",
        repo_root=root, graph_db=db,
    )
    out = []
    for f in result.files:
        p = getattr(f, "path", None) or getattr(f, "file_path", None)
        if p:
            out.append(p)
    return out


def check_l1_graph_backed(db: str, root: str) -> tuple[bool, str]:
    """Spec A/F: the brief must produce GRAPH-BACKED candidates, not lexical/BM25 only.
    Prior runs showed L1 graph_edge_count=0 (became a pure FTS5 list). Generate the
    brief, then for its candidate files count how many participate in any graph edge
    (CALLS/CONTAINS/EXTENDS...). Under strict mode, ZERO graph-backed candidates is a
    HARD fail — the brief degraded to lexical-only."""
    try:
        files = _brief_candidate_files(db, root)
    except Exception as e:
        return False, f"L1 brief generation crashed: {e}"
    if not files:
        return False, "L1 brief produced 0 candidate files"
    conn = sqlite3.connect(db)
    try:
        graph_backed = 0
        for fp in files:
            base = os.path.basename(fp)
            n = conn.execute(
                "SELECT COUNT(*) FROM edges e "
                "WHERE e.source_file = ? OR e.source_file LIKE ? "
                "OR e.source_id IN (SELECT id FROM nodes WHERE file_path = ? OR file_path LIKE ?) "
                "OR e.target_id IN (SELECT id FROM nodes WHERE file_path = ? OR file_path LIKE ?)",
                (fp, f"%{base}", fp, f"%/{base}", fp, f"%/{base}"),
            ).fetchone()[0]
            if n > 0:
                graph_backed += 1
    finally:
        conn.close()
    detail = f"L1 graph_edge_count: {graph_backed}/{len(files)} candidate files are graph-backed"
    if _strict() and graph_backed == 0:
        return False, (detail + " — brief is LEXICAL-ONLY (no graph edge confirms any "
                       "candidate); refusing under full-stack mode")
    return True, detail


# Cheap, db-only graph-base DIMENSION checks — safe to run PER-TASK on every path.
# Excludes semantic_embedder / brief_generation / l3b_delivery (those load the
# embedder / generate a brief and are gated ONCE per shard at init). These are pure
# SQL on graph.db = milliseconds, so per-task gating adds no meaningful wall-time.
PER_TASK_DB_CHECKS = [
    "prebuilt_graph", "graph_exists", "schema_version", "fts5", "edge_quality",
    "data_flow", "assertions", "lsp_enrichment", "lsp_edges",
]


def run_db_dimension_gate(db: str) -> "tuple[bool, list]":
    """Run the cheap graph-base dimension checks against one graph.db.
    Returns (all_required_pass, [(name, ok, msg), ...]). The OH wrapper calls this
    per-task so the OH path gates the SAME dimensions as DeepSWE's preflight, from
    the SAME source (no drift). Strictness is governed by the GT_REQUIRE_* env the
    individual checks read (FTS5 Go-built, data_flow>0, lsp edges when server present)."""
    fns = {
        "prebuilt_graph": lambda d: check_prebuilt_graph(d),
        "graph_exists": check_graph_exists, "schema_version": check_schema_version,
        "fts5": check_fts5, "edge_quality": check_edge_quality,
        "data_flow": check_data_flow, "assertions": check_assertions,
        "lsp_enrichment": check_lsp_enrichment, "lsp_edges": check_lsp_edges,
    }
    results = []
    ok_all = True
    for name in PER_TASK_DB_CHECKS:
        try:
            ok, msg = fns[name](db)
        except Exception as e:  # a crashing check is a failure under the gate
            ok, msg = False, f"{name} crashed: {e}"
        results.append((name, ok, msg))
        ok_all = ok_all and ok
    return ok_all, results


def main():
    parser = argparse.ArgumentParser(description="Preflight pipeline verification")
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    checks = [
        ("prebuilt_graph", lambda: check_prebuilt_graph(args.db)),
        ("graph_exists", lambda: check_graph_exists(args.db)),
        ("schema_version", lambda: check_schema_version(args.db)),
        ("fts5", lambda: check_fts5(args.db)),
        ("fts5_query", lambda: check_fts5_query(args.db)),
        ("grep_available", lambda: check_grep_available()),
        ("path_seeds", lambda: check_path_seeds(args.db)),
        ("edge_quality", lambda: check_edge_quality(args.db)),
        ("data_flow", lambda: check_data_flow(args.db)),
        ("assertions", lambda: check_assertions(args.db)),
        ("lsp_enrichment", lambda: check_lsp_enrichment(args.db)),
        ("lsp_edges", lambda: check_lsp_edges(args.db)),
        ("semantic_embedder", lambda: check_semantic_embedder(args.root)),
        ("brief_generation", lambda: check_brief_generation(args.db, args.root)),
        ("l1_graph_backed", lambda: check_l1_graph_backed(args.db, args.root)),
        ("l3b_delivery", lambda: check_l3b_delivery(args.db)),
    ]

    results = {}
    all_pass = True
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"EXCEPTION: {e}"
        results[name] = {"pass": ok, "detail": detail}
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        if not args.json:
            print(f"  [{status}] {name}: {detail}")

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print()
        if all_pass:
            print("PREFLIGHT: ALL CHECKS PASS")
        else:
            failed = [k for k, v in results.items() if not v["pass"]]
            print(f"PREFLIGHT: {len(failed)} FAILURES: {', '.join(failed)}")

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
