#!/usr/bin/env python3
"""Stage 2 — graph-base depth + resolved-graph handoff certificate.

Emits ``graph_certificate.json`` proving two things on the FINAL path:

  (a) the graph is DEEP — FTS5 (with a real MATCH probe), CALLS + trust tiers +
      properties/data_flow + closure rebuilt AFTER LSP;
  (b) the SAME post-LSP graph is used by build, LSP, the gates, and the OH hooks —
      i.e. the edge-content hash is identical across stages.

No embedder, container-movement, or image-cache logic lives here (Stage 3/4/5).
The hash is the canonical ``proof.graph_edges_hash`` / ``resolve._graph_edges_hash`` formula
so a residual==0 LSP pass and the agent's hooks are pinned to one graph.
"""
from __future__ import annotations

import json
import os
import sqlite3

_DET_METHODS = (
    "same_file", "import", "import_type", "type_flow", "verified_unique",
    "impl_method", "inherited", "unique_method", "return_type", "lsp", "lsp_verified",
)


def graph_edges_hash(db: str) -> str:
    """Canonical edge fingerprint — MUST match proof.graph_edges_hash /
    resolve._graph_edges_hash (a drift test guards this)."""
    import hashlib
    h = hashlib.sha256()
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for row in c.execute(
                "SELECT source_id, target_id, type, resolution_method, confidence "
                "FROM edges ORDER BY id"
            ):
                h.update(repr(tuple(row)).encode("utf-8"))
        finally:
            c.close()
    except Exception:
        return ""
    return h.hexdigest()


def fts5_match_probe(db: str):
    """Return (exists, row_count, match_ok). match_ok = a real MATCH query executed without
    error against the nodes_fts vtable — the Go-built-FTS5 proof. A regular table named
    nodes_fts (no FTS5) makes MATCH raise -> match_ok=False (fail-closed)."""
    exists = False
    rows = 0
    match_ok = False
    try:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except Exception:
        return (False, 0, False)
    try:
        r = c.execute(
            "SELECT count(*) FROM sqlite_master WHERE type IN ('table','view') AND name='nodes_fts'"
        ).fetchone()
        exists = bool(r and r[0])
        if exists:
            try:
                rows = int(c.execute("SELECT count(*) FROM nodes_fts").fetchone()[0])
            except Exception:
                rows = 0
            # A real MATCH must run without raising; this is the FTS5-native proof.
            c.execute("SELECT count(*) FROM nodes_fts WHERE nodes_fts MATCH ?", ("a*",)).fetchone()
            match_ok = True
    except Exception:
        match_ok = False
    finally:
        c.close()
    return (exists, rows, match_ok)


def _scalar(c, sql, params=()):
    try:
        r = c.execute(sql, params).fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except Exception:
        return 0


def _dist(c, sql):
    out: dict = {}
    try:
        for k, v in c.execute(sql).fetchall():
            out[str(k)] = int(v)
    except Exception:
        pass
    return out


def build_graph_certificate(graph_db: str, source_root: str = "", lsp_cert=None,
                            built_inside_container=None, hook_graph_hash=None,
                            prebuilt_active=None) -> dict:
    """Assemble the graph certificate dict (depth metrics + handoff fields)."""
    exists, fts_rows, match_ok = fts5_match_probe(graph_db)
    cert: dict = {
        "schema": "gt.graph_certificate.v1",
        "graph_db_path": graph_db,
        "source_root": source_root,
        "graph_hash": graph_edges_hash(graph_db),
        "built_inside_container": built_inside_container,
        "fts5_exists": exists,
        "fts5_row_count": fts_rows,
        "fts5_match_probe_ok": match_ok,
        "host_resolved_graph_db": os.environ.get("GT_HOST_GRAPH_DB", ""),
        "runtime_context_id": "",
        "hook_graph_hash": hook_graph_hash,
        "prebuilt_active": prebuilt_active,
        "nodes_count": 0, "edges_count": 0, "calls_edges_count": 0, "contains_edges_count": 0,
        "deterministic_edge_count": 0, "name_match_edge_count": 0,
        "resolution_method_distribution": {}, "trust_tier_distribution": {},
        "properties_count": 0, "data_flow_count": 0, "assertions_count": 0, "closure_count": 0,
        "project_meta_present": False,
    }
    try:
        from groundtruth.runtime import proof as _proof
        cert["runtime_context_id"] = _proof.context_id()
    except Exception:
        cert["runtime_context_id"] = os.environ.get("GT_CONTEXT_ID", "")

    try:
        c = sqlite3.connect(f"file:{graph_db}?mode=ro", uri=True)
    except Exception:
        c = None
    if c is not None:
        try:
            cert["nodes_count"] = _scalar(c, "SELECT count(*) FROM nodes")
            cert["edges_count"] = _scalar(c, "SELECT count(*) FROM edges")
            cert["calls_edges_count"] = _scalar(c, "SELECT count(*) FROM edges WHERE type='CALLS'")
            cert["contains_edges_count"] = _scalar(c, "SELECT count(*) FROM edges WHERE type='CONTAINS'")
            cert["deterministic_edge_count"] = _scalar(
                c, "SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method IN (%s)"
                   % ",".join("'%s'" % m for m in _DET_METHODS))
            cert["name_match_edge_count"] = _scalar(
                c, "SELECT count(*) FROM edges WHERE type='CALLS' AND resolution_method LIKE 'name_match%'")
            cert["resolution_method_distribution"] = _dist(
                c, "SELECT resolution_method, count(*) FROM edges WHERE type='CALLS' "
                   "GROUP BY resolution_method")
            cert["trust_tier_distribution"] = _dist(
                c, "SELECT trust_tier, count(*) FROM edges GROUP BY trust_tier")
            cert["properties_count"] = _scalar(c, "SELECT count(*) FROM properties")
            cert["data_flow_count"] = _scalar(
                c, "SELECT count(*) FROM properties WHERE kind='data_flow'")
            cert["assertions_count"] = _scalar(c, "SELECT count(*) FROM assertions")
            cert["closure_count"] = _scalar(c, "SELECT count(*) FROM closure")
            cert["project_meta_present"] = _scalar(
                c, "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='project_meta'") > 0
        finally:
            c.close()

    if lsp_cert:
        cert["graph_hash_after_lsp"] = lsp_cert.get("graph_hash_after_lsp", "")
        cert["closure_rebuilt_after_lsp"] = bool(lsp_cert.get("closure_rebuilt_after_lsp", False))
        cert["lsp_warm_from_same_graph"] = bool(
            lsp_cert.get("graph_db") and lsp_cert.get("graph_db") == graph_db)
    else:
        cert["graph_hash_after_lsp"] = ""
        cert["closure_rebuilt_after_lsp"] = None
        cert["lsp_warm_from_same_graph"] = None
    return cert


def classify_graph(cert, *, proof_mode: bool = False):
    """Hard gates over the graph certificate -> (verdict, ok).

    PASS: GRAPH_VALID.
    FAIL: GRAPH_FAIL_EMPTY, GRAPH_FAIL_FTS5, GRAPH_FAIL_BUILT_ON_HOST,
          GRAPH_FAIL_MISSING_HANDOFF, GRAPH_FAIL_HANDOFF_INACTIVE, GRAPH_FAIL_STALE_CLOSURE,
          GRAPH_FAIL_HASH_MISMATCH, GRAPH_FAIL_HOOK_MISMATCH.
    """
    if not cert:
        return ("GRAPH_FAIL_EMPTY", False)
    if cert.get("edges_count", 0) <= 0 or cert.get("calls_edges_count", 0) <= 0:
        return ("GRAPH_FAIL_EMPTY", False)
    if (not cert.get("fts5_exists") or cert.get("fts5_row_count", 0) <= 0
            or not cert.get("fts5_match_probe_ok")):
        return ("GRAPH_FAIL_FTS5", False)
    if proof_mode and cert.get("built_inside_container") is False:
        return ("GRAPH_FAIL_BUILT_ON_HOST", False)
    if proof_mode and not cert.get("host_resolved_graph_db"):
        return ("GRAPH_FAIL_MISSING_HANDOFF", False)
    if (proof_mode and cert.get("host_resolved_graph_db")
            and cert.get("prebuilt_active") is False):
        return ("GRAPH_FAIL_HANDOFF_INACTIVE", False)
    if cert.get("closure_rebuilt_after_lsp") is False:
        return ("GRAPH_FAIL_STALE_CLOSURE", False)
    _gh = cert.get("graph_hash")
    _lsp = cert.get("graph_hash_after_lsp")
    if _gh and _lsp and _gh != _lsp:
        return ("GRAPH_FAIL_HASH_MISMATCH", False)
    _hook = cert.get("hook_graph_hash")
    if _gh and _hook and _hook != _gh:
        return ("GRAPH_FAIL_HOOK_MISMATCH", False)
    return ("GRAPH_VALID", True)


def format_graph_witness(host_resolved_graph_db: str, hook_graph_db: str,
                         hook_graph_hash: str, prebuilt_active) -> str:
    """The [GT_META] line the OH wrapper emits so a run/test can prove the agent's hooks read
    the SAME resolved graph the gates measured (hook_graph_hash must equal the post-LSP hash)."""
    return (f"[GT_META] graph_witness host_resolved_graph_db={host_resolved_graph_db} "
            f"hook_graph_db={hook_graph_db} hook_graph_hash={hook_graph_hash} "
            f"_gt_prebuilt_active={bool(prebuilt_active)}")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Emit + classify the Stage-2 graph certificate.")
    ap.add_argument("graph_db")
    ap.add_argument("--source-root", default="")
    ap.add_argument("--lsp-cert", default=os.environ.get("GT_LSP_CERT", "/tmp/gt/lsp_certificate.json"))
    ap.add_argument("--out", default=os.environ.get("GT_GRAPH_CERT", "/tmp/gt/graph_certificate.json"))
    ap.add_argument("--proof-mode", action="store_true",
                    default=os.environ.get("GT_PROOF_MODE") == "1")
    ap.add_argument("--built-inside-container", default=None)
    a = ap.parse_args()
    lsp_cert = None
    try:
        with open(a.lsp_cert, encoding="utf-8") as f:
            lsp_cert = json.load(f)
    except Exception:
        lsp_cert = None
    bic = None
    if a.built_inside_container is not None:
        bic = str(a.built_inside_container).lower() in ("1", "true", "yes")
    cert = build_graph_certificate(a.graph_db, a.source_root, lsp_cert, built_inside_container=bic)
    verdict, ok = classify_graph(cert, proof_mode=a.proof_mode)
    cert["verdict"] = verdict
    try:
        _d = os.path.dirname(a.out)
        if _d:
            os.makedirs(_d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(cert, f, indent=2)
    except Exception as e:
        print(f"WARN: could not write graph certificate to {a.out}: {e}")
    _hash_match = (cert.get("graph_hash") == cert.get("graph_hash_after_lsp")
                   if cert.get("graph_hash_after_lsp") else "n/a")
    print(f"[GRAPH CERTIFICATE] {verdict} {'PASS' if ok else 'FAIL'} "
          f"fts5(exists={cert.get('fts5_exists')},rows={cert.get('fts5_row_count')},"
          f"match={cert.get('fts5_match_probe_ok')}) edges={cert.get('edges_count')} "
          f"calls={cert.get('calls_edges_count')} det={cert.get('deterministic_edge_count')} "
          f"name_match={cert.get('name_match_edge_count')} "
          f"closure_after_lsp={cert.get('closure_rebuilt_after_lsp')} hash_match_lsp={_hash_match}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
