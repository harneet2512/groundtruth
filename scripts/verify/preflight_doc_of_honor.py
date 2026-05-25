#!/usr/bin/env python3
"""Pre-flight validator for DOC_OF_HONOR.md WORKING claims.

Validates every claim tagged Status: WORKING in DOC_OF_HONOR.md against
a real graph.db produced by gt-index. Designed to run in < 5 seconds
as part of the GHA canary pre-flight step.

Usage:
    python3 scripts/verify/preflight_doc_of_honor.py /tmp/preflight/test.db
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

_results: list[tuple[str, str, bool, str]] = []  # (section, claim, passed, detail)


def _record(section: str, claim: str, passed: bool, detail: str = "") -> None:
    tag = "PASS" if passed else "FAIL"
    msg = f"  [{tag}] {section}: {claim}"
    if detail:
        msg += f" -- {detail}"
    print(msg)
    _results.append((section, claim, passed, detail))


# ---------------------------------------------------------------------------
# Layer 0: Schema + Indexing
# ---------------------------------------------------------------------------

def check_layer0_schema(db: str) -> None:
    """0.2 Schema: 7 tables with correct column counts."""
    conn = sqlite3.connect(db)

    # --- 7 tables exist ---
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected_tables = {
        "nodes", "edges", "properties", "assertions",
        "cochanges", "file_hashes", "project_meta",
    }
    missing = expected_tables - tables
    _record(
        "0.2", "7 tables exist",
        not missing,
        f"missing={missing}" if missing else f"tables={sorted(tables & expected_tables)}",
    )

    # --- nodes has 13 columns ---
    node_cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
    _record(
        "0.2", "nodes has 13 columns",
        len(node_cols) == 13,
        f"got {len(node_cols)}: {node_cols}",
    )

    # --- edges has 12 columns ---
    edge_cols = [r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()]
    _record(
        "0.2", "edges has 12 columns",
        len(edge_cols) == 12,
        f"got {len(edge_cols)}: {edge_cols}",
    )

    # --- properties has 6 columns ---
    prop_cols = [r[1] for r in conn.execute("PRAGMA table_info(properties)").fetchall()]
    _record(
        "0.2", "properties has 6 columns",
        len(prop_cols) == 6,
        f"got {len(prop_cols)}: {prop_cols}",
    )

    # --- assertions has 7 columns ---
    assert_cols = [r[1] for r in conn.execute("PRAGMA table_info(assertions)").fetchall()]
    _record(
        "0.2", "assertions has 7 columns",
        len(assert_cols) == 7,
        f"got {len(assert_cols)}: {assert_cols}",
    )

    # --- cochanges has 3 columns ---
    cochange_cols = [r[1] for r in conn.execute("PRAGMA table_info(cochanges)").fetchall()]
    _record(
        "0.2", "cochanges has 3 columns",
        len(cochange_cols) == 3,
        f"got {len(cochange_cols)}: {cochange_cols}",
    )

    # --- cochanges composite PK (file_a, file_b) ---
    # SQLite PRAGMA table_info pk field > 0 means column is part of PK
    cochange_pks = [r[1] for r in conn.execute("PRAGMA table_info(cochanges)").fetchall() if r[5] > 0]
    _record(
        "0.2", "cochanges has composite PK",
        set(cochange_pks) == {"file_a", "file_b"},
        f"pk_cols={cochange_pks}",
    )

    # --- schema_version = v15.1-trust-tier ---
    meta = dict(conn.execute("SELECT key, value FROM project_meta").fetchall())
    sv = meta.get("schema_version", "<missing>")
    _record(
        "0.1/0.2", "schema_version = v15.1-trust-tier",
        sv == "v15.1-trust-tier",
        f"got {sv!r}",
    )

    conn.close()


def check_layer0_properties(db: str) -> None:
    """0.4 Property kinds: at least 1 property from simple function extractors."""
    conn = sqlite3.connect(db)

    prop_count = conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
    _record(
        "0.4", "properties table has >= 1 row",
        prop_count >= 1,
        f"count={prop_count}",
    )

    # Check for at least one of the extractors that fire on simple functions
    if prop_count > 0:
        kinds = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT kind FROM properties"
            ).fetchall()
        }
        expected_any = {"guard_clause", "return_shape", "docstring", "param", "fingerprint"}
        found = kinds & expected_any
        _record(
            "0.4", "at least 1 property from basic extractors",
            len(found) >= 1,
            f"found_kinds={sorted(found)}, all_kinds={sorted(kinds)}",
        )
    else:
        _record(
            "0.4", "at least 1 property from basic extractors",
            False,
            "no properties at all",
        )

    conn.close()


def check_layer0_resolution_pipeline(db: str) -> None:
    """0.5 Resolution pipeline: edges table has trust-tier columns."""
    conn = sqlite3.connect(db)

    edge_cols = {r[1] for r in conn.execute("PRAGMA table_info(edges)").fetchall()}
    required = {
        "confidence", "resolution_method", "trust_tier",
        "candidate_count", "evidence_type", "verification_status",
    }
    missing = required - edge_cols
    _record(
        "0.5", "edges has resolution pipeline columns",
        not missing,
        f"missing={missing}" if missing else "all 6 resolution columns present",
    )

    conn.close()


# ---------------------------------------------------------------------------
# Layer 1: Path Resolution
# ---------------------------------------------------------------------------

def check_layer1_path_resolution(db: str) -> None:
    """1.1 Path resolution: exact match and progressive prefix stripping."""
    conn = sqlite3.connect(db)

    # Get a stored file_path
    row = conn.execute("SELECT file_path FROM nodes LIMIT 1").fetchone()
    if not row:
        _record("1.1", "stored file_path exists", False, "no nodes in db")
        conn.close()
        return

    stored_path = row[0]

    # Exact match
    exact = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE file_path = ?", (stored_path,)
    ).fetchone()[0]
    _record(
        "1.1", "exact path match works",
        exact > 0,
        f"stored={stored_path!r}, found={exact}",
    )

    # Progressive prefix stripping via LIKE suffix
    # Prepend a container prefix and verify LIKE '%<stored>' still works
    container_path = f"/workspace/test/{stored_path}"
    like_count = conn.execute(
        "SELECT COUNT(*) FROM nodes WHERE ? LIKE '%' || file_path",
        (container_path,),
    ).fetchone()[0]
    _record(
        "1.1", "progressive prefix stripping (LIKE suffix) works",
        like_count > 0,
        f"query={container_path!r}, matches={like_count}",
    )

    conn.close()


# ---------------------------------------------------------------------------
# Layer 2: Passive Delivery (Python imports)
# ---------------------------------------------------------------------------

def check_layer2_imports() -> None:
    """2.x Passive delivery: key Python modules import successfully."""
    import_checks = [
        ("2.2", "post_edit.generate_improved_evidence",
         "groundtruth.hooks.post_edit", "generate_improved_evidence"),
        ("2.3", "post_view.graph_navigation",
         "groundtruth.hooks.post_view", "graph_navigation"),
        ("2.1", "graph_map.build_graph_map",
         "groundtruth.brief.graph_map", "build_graph_map"),
        ("2.x", "graph_store.GraphStore + is_graph_db",
         "groundtruth.index.graph_store", "GraphStore"),
        ("2.x", "graph_store.is_graph_db",
         "groundtruth.index.graph_store", "is_graph_db"),
        ("2.x", "evidence.change.CoChangeCache",
         "groundtruth.evidence.change", "CoChangeCache"),
        ("0.2", "schema_version.verify_graph_db_schema",
         "groundtruth.index.schema_version", "verify_graph_db_schema"),
        ("3.1", "router.CollaborationRouter",
         "groundtruth.router", "CollaborationRouter"),
    ]

    for section, claim, module_path, attr_name in import_checks:
        try:
            mod = importlib.import_module(module_path)
            obj = getattr(mod, attr_name, None)
            _record(section, claim, obj is not None,
                    f"imported {module_path}.{attr_name}")
        except Exception as exc:
            _record(section, claim, False, f"import error: {exc}")


# ---------------------------------------------------------------------------
# Layer 4: MCP Tools
# ---------------------------------------------------------------------------

def check_layer4_mcp_tools() -> None:
    """4.1 MCP tools: exactly 7 @app.tool() decorators in server.py."""
    try:
        import groundtruth.mcp.server as server_mod
        server_path = Path(server_mod.__file__)
        source = server_path.read_text(encoding="utf-8")

        # Count uncommented @app.tool() lines
        active_count = 0
        for line in source.splitlines():
            stripped = line.strip()
            if stripped == "@app.tool()" and not stripped.startswith("#"):
                active_count += 1

        _record(
            "4.1", "7 active @app.tool() decorators in server.py",
            active_count == 7,
            f"found {active_count}",
        )
    except Exception as exc:
        _record("4.1", "7 active @app.tool() decorators in server.py",
                False, f"error: {exc}")


# ---------------------------------------------------------------------------
# Layer 5: Supporting Infrastructure
# ---------------------------------------------------------------------------

def check_layer5_supporting() -> None:
    """5.x Supporting: evidence markers, _classify_return_usage, _open_graph_db."""

    # L3B_MARKERS importable
    try:
        from groundtruth.config.evidence_markers import L3B_MARKERS
        _record(
            "5.x", "L3B_MARKERS importable",
            isinstance(L3B_MARKERS, (list, tuple)) and len(L3B_MARKERS) > 0,
            f"type={type(L3B_MARKERS).__name__}, len={len(L3B_MARKERS)}",
        )
    except Exception as exc:
        _record("5.x", "L3B_MARKERS importable", False, f"error: {exc}")

    # _classify_return_usage callable
    try:
        from groundtruth.hooks.post_edit import _classify_return_usage
        _record(
            "5.x", "_classify_return_usage callable",
            callable(_classify_return_usage),
            "callable=True",
        )
    except Exception as exc:
        _record("5.x", "_classify_return_usage callable", False, f"error: {exc}")

    # _open_graph_db callable
    try:
        from groundtruth.hooks.post_edit import _open_graph_db
        _record(
            "5.x", "_open_graph_db callable",
            callable(_open_graph_db),
            "callable=True",
        )
    except Exception as exc:
        _record("5.x", "_open_graph_db callable", False, f"error: {exc}")


# ---------------------------------------------------------------------------
# Meta: caching_prompt
# ---------------------------------------------------------------------------

def check_meta_caching_prompt() -> None:
    """Meta: caching_prompt = false in config.toml (if present in CWD)."""
    config_path = Path("config.toml")
    if not config_path.exists():
        _record("Meta", "caching_prompt = false in config.toml",
                True, "config.toml not in CWD (skipped -- checked separately in GHA)")
        return

    content = config_path.read_text(encoding="utf-8")
    has_true = bool(re.search(r"caching_prompt\s*=\s*true", content))
    has_false = bool(re.search(r"caching_prompt\s*=\s*false", content))

    if has_true:
        _record("Meta", "caching_prompt = false in config.toml",
                False, "DANGEROUS: caching_prompt = true found")
    elif has_false:
        _record("Meta", "caching_prompt = false in config.toml",
                True, "caching_prompt = false confirmed")
    else:
        _record("Meta", "caching_prompt = false in config.toml",
                False, "caching_prompt not explicitly set (OH defaults to true)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: preflight_doc_of_honor.py <graph.db path>", file=sys.stderr)
        return 2

    db_path = sys.argv[1]
    if not Path(db_path).exists():
        print(f"FATAL: graph.db not found at {db_path}", file=sys.stderr)
        return 2

    print("=" * 60)
    print("DOC_OF_HONOR Pre-Flight Validation")
    print("=" * 60)
    print(f"  graph.db: {db_path}")
    print()

    # --- Layer 0: Schema + Indexing ---
    print("--- Layer 0: Schema + Indexing ---")
    check_layer0_schema(db_path)
    check_layer0_properties(db_path)
    check_layer0_resolution_pipeline(db_path)
    print()

    # --- Layer 1: Path Resolution ---
    print("--- Layer 1: Path Resolution ---")
    check_layer1_path_resolution(db_path)
    print()

    # --- Layer 2: Passive Delivery (Python imports) ---
    print("--- Layer 2: Passive Delivery (imports) ---")
    check_layer2_imports()
    print()

    # --- Layer 4: MCP Tools ---
    print("--- Layer 4: MCP Tools ---")
    check_layer4_mcp_tools()
    print()

    # --- Layer 5: Supporting ---
    print("--- Layer 5: Supporting Infrastructure ---")
    check_layer5_supporting()
    print()

    # --- Meta ---
    print("--- Meta ---")
    check_meta_caching_prompt()
    print()

    # --- Summary ---
    total = len(_results)
    passed = sum(1 for _, _, p, _ in _results if p)
    failed = total - passed

    print("=" * 60)
    print(f"SUMMARY: {passed}/{total} passed, {failed} failed")
    print("=" * 60)

    if failed:
        print()
        print("FAILED claims:")
        for section, claim, p, detail in _results:
            if not p:
                print(f"  [{section}] {claim}: {detail}")
        print()
        print("PRE-FLIGHT FAILED")
        return 1

    print()
    print("PRE-FLIGHT PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
