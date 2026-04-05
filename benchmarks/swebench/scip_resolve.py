#!/usr/bin/env python3
"""SCIP-based edge resolution for graph.db.

Runs scip-python once per repo, parses the complete symbol index via Protobuf,
and upgrades name-match edges to scip-verified (confidence 1.0).

Usage:
    python3 scip_resolve.py --db /tmp/gt_graph.db --root /testbed

Research backing:
- SCIP: 330x faster than LSP for batch resolution (blarify benchmark)
- Same Pyright type checker, batch Protobuf output protocol
- Used by Sourcegraph, Meta/Glean, GitLab, Uber in production
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict
from typing import Any


def normalize_path(p: str) -> str:
    """Normalize path to relative form, stripping container prefixes."""
    for prefix in ("/testbed/", "/home/", "/tmp/", "/app/"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    return p.lstrip("/")


def extract_simple_name(symbol: str) -> str:
    """Extract simple function/class name from SCIP symbol string.

    SCIP symbols look like:
        scip-python python django 0.1 django/urls/resolvers.py/URLResolver#resolve().
    The simple name is the last segment after / or #, stripped of () and .
    """
    s = symbol.strip().rstrip(".")
    for sep in ("#", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    return s.rstrip("().#")


def run_scip_index(root: str) -> str | None:
    """Run scip-python on the repo root, return path to index.scip."""
    index_path = os.path.join(root, "index.scip")
    try:
        result = subprocess.run(
            ["scip-python", "index", ".", "--project-name=repo"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if os.path.exists(index_path) and os.path.getsize(index_path) > 0:
            return index_path
        # Try alternate output location
        alt = os.path.join(os.getcwd(), "index.scip")
        if os.path.exists(alt) and os.path.getsize(alt) > 0:
            return alt
        print(f"scip-python stderr: {result.stderr[:500]}", file=sys.stderr)
        return None
    except FileNotFoundError:
        print("scip-python not found in PATH", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("scip-python timed out (300s)", file=sys.stderr)
        return None


def parse_scip_index(index_path: str) -> dict[str, Any] | None:
    """Parse SCIP Protobuf index into lookup dictionaries.

    Returns:
        {
            "definitions": {scip_symbol: [(file, line_0indexed, col)]},
            "references": {(file, line_0indexed): [(scip_symbol, col)]},
            "stats": {"documents": N, "definitions": N, "references": N},
        }
    """
    try:
        # Try importing scip_pb2 from same directory
        scip_dir = os.path.dirname(os.path.abspath(__file__))
        if scip_dir not in sys.path:
            sys.path.insert(0, scip_dir)
        # Also try /tmp (Docker injection location)
        if "/tmp" not in sys.path:
            sys.path.insert(0, "/tmp")
        import scip_pb2
    except ImportError:
        print("scip_pb2.py not found — run protoc on scip.proto first", file=sys.stderr)
        return None

    idx = scip_pb2.Index()
    with open(index_path, "rb") as f:
        idx.ParseFromString(f.read())

    definitions: dict[str, list[tuple[str, int, int]]] = defaultdict(list)
    references: dict[tuple[str, int], list[tuple[str, int]]] = defaultdict(list)
    n_defs = 0
    n_refs = 0

    for doc in idx.documents:
        fpath = doc.relative_path
        for occ in doc.occurrences:
            if not occ.symbol or occ.symbol.startswith("local "):
                continue
            # SCIP range: [line, col, end_line, end_col] or [line, col, end_col]
            if len(occ.range) < 2:
                continue
            line_0 = occ.range[0]
            col = occ.range[1]

            is_def = bool(occ.symbol_roles & 0x1)
            if is_def:
                definitions[occ.symbol].append((fpath, line_0, col))
                n_defs += 1
            else:
                references[(fpath, line_0)].append((occ.symbol, col))
                n_refs += 1

    return {
        "definitions": dict(definitions),
        "references": dict(references),
        "stats": {
            "documents": len(idx.documents),
            "definitions": n_defs,
            "references": n_refs,
        },
    }


def _detect_resolution_column(conn: sqlite3.Connection) -> str:
    """Detect whether edges table uses 'resolution_method' or 'resolution'."""
    cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    col_names = {row[1] for row in cols}
    if "resolution_method" in col_names:
        return "resolution_method"
    if "resolution" in col_names:
        return "resolution"
    return "resolution_method"  # default


def _has_confidence_column(conn: sqlite3.Connection) -> bool:
    """Check if edges table has a confidence column."""
    cols = conn.execute("PRAGMA table_info(edges)").fetchall()
    return any(row[1] == "confidence" for row in cols)


def upgrade_edges(
    db_path: str, scip_data: dict[str, Any], root: str,
) -> dict[str, int]:
    """Upgrade name-match edges to SCIP-verified using parsed SCIP data.

    Returns stats dict with counts of upgraded, corrected, deleted, unchanged edges.
    """
    definitions = scip_data["definitions"]
    references = scip_data["references"]

    # Build reverse lookup: simple_name → [(scip_symbol, file, line_0)]
    name_to_defs: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    for scip_sym, locs in definitions.items():
        simple = extract_simple_name(scip_sym)
        if simple:
            for fpath, line_0, _col in locs:
                name_to_defs[simple].append((scip_sym, fpath, line_0))

    conn = sqlite3.connect(db_path, isolation_level=None)
    res_col = _detect_resolution_column(conn)
    has_conf = _has_confidence_column(conn)

    stats = {"upgraded": 0, "corrected": 0, "deleted": 0, "unchanged": 0, "total": 0}

    # Get all name-match CALLS edges
    edges = conn.execute(
        f"SELECT e.id, e.source_id, e.target_id, e.source_line, e.source_file, "
        f"s.name AS src_name, s.file_path AS src_file, s.start_line AS src_start, s.end_line AS src_end, "
        f"t.name AS tgt_name, t.file_path AS tgt_file, t.start_line AS tgt_start "
        f"FROM edges e "
        f"JOIN nodes s ON e.source_id = s.id "
        f"JOIN nodes t ON e.target_id = t.id "
        f"WHERE e.{res_col} = 'name_match' AND e.type = 'CALLS'"
    ).fetchall()

    stats["total"] = len(edges)

    updates: list[tuple[str, float, int]] = []  # (method, confidence, edge_id)
    corrections: list[tuple[int, int]] = []  # (correct_node_id, edge_id)
    deletes: list[int] = []

    for (edge_id, src_id, tgt_id, src_line, src_file_raw,
         src_name, src_file, src_start, src_end,
         tgt_name, tgt_file, tgt_start) in edges:

        # Normalize paths for comparison
        norm_src_file = normalize_path(src_file or "")
        norm_tgt_file = normalize_path(tgt_file or "")

        # Search SCIP references within source function's line range
        if src_start is None or src_end is None:
            stats["unchanged"] += 1
            continue

        found_match = False
        for line_0 in range(max(0, src_start - 2), (src_end or src_start) + 2):
            # Check both normalized and raw paths
            ref_key = (norm_src_file, line_0)
            refs = references.get(ref_key, [])
            if not refs:
                # Try with raw path
                ref_key_raw = (src_file or "", line_0)
                refs = references.get(ref_key_raw, [])

            for scip_sym, _col in refs:
                ref_simple = extract_simple_name(scip_sym)
                if ref_simple != tgt_name:
                    continue

                # Found a SCIP reference to tgt_name within source function range.
                # Now verify: does SCIP's definition for this symbol match the target?
                scip_defs = definitions.get(scip_sym, [])
                for def_file, def_line_0, _def_col in scip_defs:
                    norm_def_file = normalize_path(def_file)
                    # Compare: SCIP definition location vs graph.db target location
                    # SCIP is 0-indexed, graph.db is 1-indexed: def_line_0 + 1 ≈ tgt_start
                    if (norm_def_file == norm_tgt_file and
                            tgt_start is not None and
                            abs((def_line_0 + 1) - tgt_start) <= 3):
                        # CONFIRMED: SCIP agrees with the edge target
                        if has_conf:
                            updates.append(("scip", 1.0, edge_id))
                        else:
                            updates.append(("scip", -1, edge_id))
                        stats["upgraded"] += 1
                        found_match = True
                        break
                    elif norm_def_file != norm_tgt_file:
                        # SCIP says different file — edge target is WRONG
                        # Check if SCIP's target exists in graph.db
                        correct_node = conn.execute(
                            "SELECT id FROM nodes WHERE file_path LIKE ? AND name = ? LIMIT 1",
                            (f"%{norm_def_file}", tgt_name),
                        ).fetchone()
                        if correct_node:
                            corrections.append((correct_node[0], edge_id))
                            stats["corrected"] += 1
                        else:
                            # Target not in graph.db (stdlib/external) — delete edge
                            deletes.append(edge_id)
                            stats["deleted"] += 1
                        found_match = True
                        break
                if found_match:
                    break
            if found_match:
                break

        if not found_match:
            stats["unchanged"] += 1

    # Batch apply all changes in one transaction
    conn.execute("BEGIN")

    for method, conf, eid in updates:
        if has_conf:
            conn.execute(
                f"UPDATE edges SET {res_col} = ?, confidence = ? WHERE id = ?",
                (method, conf, eid),
            )
        else:
            conn.execute(
                f"UPDATE edges SET {res_col} = ? WHERE id = ?",
                (method, eid),
            )

    for correct_node_id, eid in corrections:
        conn.execute(
            f"UPDATE edges SET target_id = ?, {res_col} = 'scip'"
            + (", confidence = 1.0" if has_conf else "")
            + " WHERE id = ?",
            (correct_node_id, eid),
        )

    for eid in deletes:
        conn.execute("DELETE FROM edges WHERE id = ?", (eid,))

    # Downgrade remaining name-match edges
    if has_conf:
        conn.execute(
            f"UPDATE edges SET confidence = 0.2 "
            f"WHERE {res_col} = 'name_match' AND type = 'CALLS' AND confidence > 0.2",
        )

    conn.execute("COMMIT")
    conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="SCIP-based edge resolution for graph.db")
    parser.add_argument("--root", required=True, help="Repository root directory")
    parser.add_argument("--db", required=True, help="Path to graph.db")
    args = parser.parse_args()

    t0 = time.time()

    # Step 1: Run SCIP indexer
    print(f"SCIP: indexing {args.root}...", file=sys.stderr)
    index_path = run_scip_index(args.root)
    if not index_path:
        print("SCIP: indexing failed — edges unchanged", file=sys.stderr)
        sys.exit(0)  # Non-fatal: ego-graph works with name-match edges

    t1 = time.time()
    print(f"SCIP: indexed in {t1 - t0:.1f}s", file=sys.stderr)

    # Step 2: Parse SCIP index
    scip_data = parse_scip_index(index_path)
    if not scip_data:
        print("SCIP: parse failed — edges unchanged", file=sys.stderr)
        sys.exit(0)

    t2 = time.time()
    s = scip_data["stats"]
    print(
        f"SCIP: {s['documents']} docs, {s['definitions']} defs, {s['references']} refs "
        f"(parsed in {t2 - t1:.1f}s)",
        file=sys.stderr,
    )

    # Step 3: Upgrade edges
    stats = upgrade_edges(args.db, scip_data, args.root)
    t3 = time.time()

    total = stats["total"]
    upgraded_pct = (stats["upgraded"] / total * 100) if total else 0
    print(
        f"SCIP: {stats['upgraded']}/{total} edges upgraded ({upgraded_pct:.1f}%), "
        f"{stats['corrected']} corrected, {stats['deleted']} deleted, "
        f"{stats['unchanged']} unchanged — total {t3 - t0:.1f}s",
    )

    # Clean up index.scip (can be large)
    try:
        os.remove(index_path)
    except OSError:
        pass


if __name__ == "__main__":
    main()
