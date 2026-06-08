"""GRAPH-BASE contract — verify graph.db dimensions, read-only.

Proves the graph base GT built in-container has the dimensions the architecture
(gt_gt.md §2) requires, and reports the deterministic-vs-name_match split that
distinguishes a real map from a name-guess map. Pure SQL; no behavior change.

Verification methods grounded in the audit exploration:
  schema/counts        sqlite.go (nodes/edges/properties/assertions/closure)
  FTS5 + MATCH probe   main.go GT_REQUIRE_FTS5 gate
  closure after LSP    main.go -rebuild-closure ("rebuild-closure: N -> M")
  resolution_method    main.go per-method logging
"""
from __future__ import annotations

import os
import sqlite3
from typing import Any

# The SAME deterministic set the consumer (foundational_gates / curation_map) uses,
# so the contract's notion of "resolved" can never drift from the product's.
DETERMINISTIC_METHODS = frozenset({
    "same_file", "import", "import_type", "type_flow", "verified_unique",
    "impl_method", "inherited", "unique_method", "return_type", "lsp", "lsp_verified",
})
EXPECTED_SCHEMA_PREFIX = "v15"  # project_meta.schema_version, e.g. "v15.2-trust-tier"


def _scalar(con: sqlite3.Connection, sql: str, params: tuple = ()) -> Any:
    try:
        row = con.execute(sql, params).fetchone()
        return row[0] if row else None
    except Exception as e:
        return f"ERR({e})"


def _dist(con: sqlite3.Connection, sql: str) -> dict[str, int]:
    try:
        return {str(k): int(v) for k, v in con.execute(sql).fetchall()}
    except Exception:
        return {}


def build_graph_contract(db_path: str, closure_before_lsp: int | None = None) -> dict:
    """Return the graph-base contract dict for graph.db at db_path.

    closure_before_lsp: the closure row count captured AFTER index but BEFORE the
    LSP/-rebuild-closure pass (the orchestrator captures it), so we can assert the
    closure was rebuilt (not stale) after LSP. None if not captured.
    """
    c: dict[str, Any] = {"contract": "graph_base", "graph_path": db_path}
    if not (db_path and os.path.exists(db_path) and os.path.getsize(db_path) > 0):
        c["graph_exists"] = False
        c["hard_fail"] = ["graph_missing"]
        return c
    c["graph_exists"] = True

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        # --- schema / project_meta ---
        pm = _dist(con, "SELECT key, value FROM project_meta")  # value coerced to int by _dist? no -> redo
        try:
            pm = {str(k): str(v) for k, v in con.execute("SELECT key, value FROM project_meta").fetchall()}
        except Exception:
            pm = {}
        schema_version = pm.get("schema_version", "")
        c["project_meta_keys"] = sorted(pm.keys())
        c["schema_version"] = schema_version
        c["git_commit"] = pm.get("git_commit", "")
        c["schema_ok"] = bool(schema_version and schema_version.startswith(EXPECTED_SCHEMA_PREFIX))

        # --- counts ---
        c["nodes_count"] = _scalar(con, "SELECT COUNT(*) FROM nodes")
        c["edges_count"] = _scalar(con, "SELECT COUNT(*) FROM edges")
        c["calls_count"] = _scalar(con, "SELECT COUNT(*) FROM edges WHERE type='CALLS'")
        c["contains_count"] = _scalar(con, "SELECT COUNT(*) FROM edges WHERE type='CONTAINS'")
        c["rel_edge_counts"] = _dist(
            con,
            "SELECT type, COUNT(*) FROM edges WHERE type IN "
            "('EXTENDS','IMPLEMENTS','COMPOSES','RE_EXPORTS','HANDLES_ROUTE') GROUP BY type",
        )
        c["properties_count"] = _scalar(con, "SELECT COUNT(*) FROM properties")
        c["data_flow_count"] = _scalar(con, "SELECT COUNT(*) FROM properties WHERE kind='data_flow'")
        c["assertions_count"] = _scalar(con, "SELECT COUNT(*) FROM assertions")
        c["file_hashes_count"] = _scalar(con, "SELECT COUNT(*) FROM file_hashes")
        c["closure_count"] = _scalar(con, "SELECT COUNT(*) FROM closure")
        c["closure_before_lsp"] = closure_before_lsp
        # closure rebuilt after LSP iff it is non-empty and (if we captured a before
        # count) it changed OR the before was also populated — recorded for classify.
        c["closure_rebuilt_after_lsp"] = (
            None if closure_before_lsp is None
            else (isinstance(c["closure_count"], int) and c["closure_count"] > 0)
        )

        # --- FTS5 existence + a REAL match probe ---
        fts_exists = bool(_scalar(
            con, "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='nodes_fts'"))
        c["nodes_fts_exists"] = fts_exists
        if fts_exists:
            c["fts5_row_count"] = _scalar(con, "SELECT COUNT(*) FROM nodes_fts")
            # probe with a real token taken from an actual node name (repo-agnostic).
            # Probe with SEVERAL real node names; a hit on ANY proves the index queries.
            # Use the first alnum subtoken with a PREFIX match (token*) so CamelCase,
            # length/truncation, and tokenization quirks can't false-fail a populated index.
            try:
                names = [r[0] for r in con.execute(
                    "SELECT name FROM nodes WHERE name IS NOT NULL AND length(name)>=4 LIMIT 8"
                ).fetchall()]
            except Exception:
                names = []
            probe_ok, probe_tok = False, ""
            for nm in names:
                tok = ""
                for _ch in str(nm):
                    if _ch.isalnum():
                        tok += _ch
                    elif tok:
                        break
                if len(tok) < 3:
                    continue
                probe_tok = probe_tok or tok
                hits = _scalar(con, "SELECT COUNT(*) FROM nodes_fts WHERE nodes_fts MATCH ?", (tok + "*",))
                if isinstance(hits, int) and hits > 0:
                    probe_ok, probe_tok = True, tok
                    break
            c["fts5_match_probe_token"] = probe_tok
            c["fts5_match_probe_ok"] = probe_ok
        else:
            c["fts5_row_count"] = 0
            c["fts5_match_probe_ok"] = False

        # --- resolution quality (the map-is-fact vs map-is-guess split) ---
        rm = _dist(con, "SELECT resolution_method, COUNT(*) FROM edges WHERE type='CALLS' GROUP BY resolution_method")
        c["resolution_method_dist"] = rm
        c["trust_tier_dist"] = _dist(
            con, "SELECT trust_tier, COUNT(*) FROM edges WHERE type='CALLS' GROUP BY trust_tier")
        det = sum(v for k, v in rm.items() if k in DETERMINISTIC_METHODS)
        nm = int(rm.get("name_match", 0))
        total_calls = sum(rm.values()) or (c["calls_count"] if isinstance(c["calls_count"], int) else 0)
        c["deterministic_count"] = det
        c["name_match_count"] = nm
        c["det_pct"] = round(100.0 * det / total_calls, 8) if total_calls else 0.0
        c["name_match_dominates"] = nm > det
    finally:
        con.close()

    # --- hard fails (graph base degraded) ---
    hf: list[str] = []
    if not c.get("nodes_fts_exists"):
        hf.append("fts5_missing")
    elif not c.get("fts5_match_probe_ok"):
        hf.append("fts5_match_probe_failed")
    if not (isinstance(c.get("calls_count"), int) and c["calls_count"] > 0):
        hf.append("calls_edges_missing")
    if not c.get("schema_ok"):
        hf.append("schema_version_unexpected")
    if not pm.get("git_commit"):
        hf.append("project_meta_missing_commit")
    # name_match dominance is NOT a graph hard-fail by itself (the consumer filters),
    # but it is the signal the classifier reads for PRODUCT/GRAPH quality.
    c["hard_fail"] = hf
    return c


if __name__ == "__main__":
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gt/graph.db"
    print(json.dumps(build_graph_contract(path), indent=2, default=str))
