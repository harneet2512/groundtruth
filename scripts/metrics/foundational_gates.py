#!/usr/bin/env python3
"""FOUNDATIONAL GATES — the 3 things that predict whether a run can exceed ~10% success.
Run FIRST, before any downstream layer audit. If any gate is OFF, the success ceiling is low and
every downstream 'misfire/fabrication' traces back here (shallow contracts = no LSP; laundered
callers = name_match graph; dead localization = zero-model embedder).

  GATE 1 GRAPH-BASED   : graph.db built + queried (nodes/edges) and edges aren't ALL name_match.
  GATE 2 LSP ENRICHMENT: nodes carry LSP-resolved return_type/signature (concrete contracts), not
                         tree-sitter-only. Shallow contracts downstream come from FAIL here.
  GATE 3 EMBEDDER      : the REAL ONNX embedder (related>unrelated cosine), not _ZeroEmbeddingModel.

Usage: python foundational_gates.py <graph_db> <repo_root> [issue_file]
"""
import sys, os, sqlite3, math


def _q1(con, sql):
    try:
        return con.execute(sql).fetchone()[0]
    except Exception as e:
        return f"ERR({e})"


def gate_graph(db):
    if not os.path.exists(db):
        print(f"[GATE 1 GRAPH-BASED] FAIL — graph.db missing: {db}")
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = _q1(con, "SELECT count(*) FROM nodes")
    e = _q1(con, "SELECT count(*) FROM edges")
    methods = con.execute(
        "SELECT resolution_method, count(*) FROM edges GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    con.close()
    nm = dict((m or "null", c) for m, c in methods)
    name_match = sum(c for m, c in nm.items() if "name_match" in (m or ""))
    deterministic = (e - name_match) if isinstance(e, int) else 0
    det_pct = (100.0 * deterministic / e) if isinstance(e, int) and e else 0.0
    ok = isinstance(n, int) and n > 0 and isinstance(e, int) and e > 0
    print(f"[GATE 1 GRAPH-BASED] {'PASS' if ok else 'FAIL'} nodes={n} edges={e} "
          f"deterministic_edges={deterministic} ({det_pct:.1f}%)")
    print(f"  resolution_methods: {methods}")
    if isinstance(e, int) and e and det_pct < 20:
        print("  WARNING: <20% deterministic edges -> callers are mostly name_match (laundering risk)")
    return ok


def gate_lsp(db):
    if not os.path.exists(db):
        print(f"[GATE 2 LSP ENRICHMENT] FAIL — graph.db missing: {db}")
        return False
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    total = _q1(con, "SELECT count(*) FROM nodes")
    rt = _q1(con, "SELECT count(*) FROM nodes WHERE return_type IS NOT NULL AND trim(return_type)!=''")
    sig = _q1(con, "SELECT count(*) FROM nodes WHERE signature IS NOT NULL AND trim(signature)!=''")
    lsp_edges = _q1(con, "SELECT count(*) FROM edges WHERE resolution_method LIKE '%lsp%'")
    con.close()
    ok = isinstance(rt, int) and rt > 0
    pct = (100.0 * rt / total) if isinstance(total, int) and total and isinstance(rt, int) else 0.0
    print(f"[GATE 2 LSP ENRICHMENT] {'PASS' if ok else 'FAIL'} "
          f"nodes_with_return_type={rt}/{total} ({pct:.1f}%) signatures={sig} lsp_resolved_edges={lsp_edges}")
    if not ok:
        print("  WARNING: NO LSP-resolved return types -> graph is tree-sitter-only; "
              "L3/L3b contracts will be SHALLOW (this is the root of 'wrong function'/'fabricated' downstream)")
    return ok


def gate_embedder():
    try:
        from groundtruth.memory.enrich.embed import get_embedding_model
        m = get_embedding_model()
        cls = type(m).__name__

        def emb(t, q):
            v = m.embed_batch([t], is_query=q)[0]
            return list(v)

        def cos(x, y):
            d = sum(i * j for i, j in zip(x, y))
            nx = math.sqrt(sum(i * i for i in x))
            ny = math.sqrt(sum(i * i for i in y))
            return d / (nx * ny) if nx and ny else 0.0

        a = emb("read configuration from a file", True)
        rel = emb("parse config settings from disk", False)
        unrel = emb("compute the determinant of a matrix", False)
        sim, dis = cos(a, rel), cos(a, unrel)
        is_zero = "Zero" in cls
        ok = (not is_zero) and sim > 0.30 and sim > dis
        print(f"[GATE 3 EMBEDDER] {'PASS' if ok else 'FAIL'} class={cls} "
              f"cos(related)={sim:.4f} cos(unrelated)={dis:.4f}")
        if is_zero:
            print("  WARNING: _ZeroEmbeddingModel fallback -> SEMANTIC IS DEAD (sem signal = 0 everywhere)")
        elif not ok:
            print("  WARNING: embedder loads but does not separate related/unrelated -> semantic is NOISE")
        return ok
    except Exception as e:
        print(f"[GATE 3 EMBEDDER] FAIL — exception: {e}")
        return False


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt_prebuilt.db"
    repo = sys.argv[2] if len(sys.argv) > 2 else "/tmp/testbed_src"
    issue_file = sys.argv[3] if len(sys.argv) > 3 else "/tmp/issue.txt"
    print("=" * 64)
    print("FOUNDATIONAL GATES — predict >10% ceiling BEFORE any downstream audit")
    print("=" * 64)
    g1 = gate_graph(db)
    g2 = gate_lsp(db)
    g3 = gate_embedder()
    print(f"\nVERDICT: graph={'ON' if g1 else 'OFF'}  lsp_enrichment={'ON' if g2 else 'OFF'}  "
          f"embedder={'ON' if g3 else 'OFF'}")
    if g1 and g2 and g3:
        print("  -> all 3 ON: downstream audit is meaningful.")
    else:
        print("  -> a GATE is OFF: success ceiling is LOW; fix this BEFORE auditing downstream layers.")
    # Downstream (only meaningful if gates pass): the real run_v74 component scores.
    if os.path.exists(issue_file):
        issue = open(issue_file, encoding="utf-8").read()[:2500]
        print("\n--- run_v74 components (downstream — read only if gates ON) ---")
        try:
            from groundtruth.pretask.v1r_brief import generate_v1r_brief
            r = generate_v1r_brief(issue_text=issue, repo_root=repo, graph_db=db)
            v = r.v74_result
            for rec in (v.ranked_full[:8] if v and v.ranked_full else []):
                print("  ", rec.get("path"), {k: round(float(x), 3) for k, x in (rec.get("components") or {}).items()})
            print("L1 counts: sem=%s struct=%s fts5=%s" % (r.semantic_signal_count, r.structural_signal_count, r.fts5_signal_count))
        except Exception:
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
