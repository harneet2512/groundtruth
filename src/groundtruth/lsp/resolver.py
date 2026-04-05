"""
LSP edge resolution pipeline for GT.

Reads unresolved call sites from graph.db's call_sites table,
resolves them via LSP textDocument/definition, and writes verified
edges back to the edges table.

Runs AFTER gt-index completes, before ego-graph queries.

Usage:
    python -m groundtruth.lsp.resolver --db graph.db --root /path/to/repo
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from collections import defaultdict

from .servers import LSP_SERVERS, detect_installed_servers
from .sync_client import LSPClient


def resolve_edges_with_lsp(
    graph_db_path: str,
    workspace_root: str,
    verbose: bool = True,
) -> dict[str, object]:
    """
    Resolve unresolved call sites via LSP.

    Pipeline:
      1. Read unresolved call_sites from graph.db
      2. Group by file extension
      3. For each extension with an installed LSP server:
         a. Start server
         b. Open files, send goto_definition for each call site
         c. Match LSP target to graph.db node
         d. Write lsp-verified edge (confidence=1.0)
      4. Downgrade remaining name-match edges for LSP-covered languages

    Returns:
        Stats dict: {resolved, unresolved, languages: {ext: {total, resolved}}}
    """
    conn = sqlite3.connect(graph_db_path)
    conn.row_factory = sqlite3.Row

    # 1. Read unresolved call sites grouped by extension
    call_sites = conn.execute(
        "SELECT id, caller_node_id, callee_name, line, col, file_path "
        "FROM call_sites WHERE resolved = 0"
    ).fetchall()

    if not call_sites:
        if verbose:
            print("No unresolved call sites found.", file=sys.stderr)
        conn.close()
        return {"resolved": 0, "unresolved": 0, "languages": {}}

    # Group by extension
    by_ext: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for cs in call_sites:
        ext = os.path.splitext(cs["file_path"])[1].lower()
        by_ext[ext].append(cs)

    # 2. Detect installed servers
    extensions_in_repo = set(by_ext.keys())
    available = detect_installed_servers(extensions_in_repo)

    if verbose:
        print(f"Call sites: {len(call_sites)} across {len(by_ext)} extensions", file=sys.stderr)
        print(f"LSP servers available: {', '.join(sorted(available.keys())) or 'none'}", file=sys.stderr)
        missing = extensions_in_repo - set(available.keys())
        if missing:
            for ext in sorted(missing):
                config = LSP_SERVERS.get(ext)
                if config:
                    print(f"  Missing server for {ext}: install with: {config['install']}", file=sys.stderr)

    stats: dict[str, object] = {"resolved": 0, "unresolved": 0, "languages": {}}

    # 3. Resolve each extension
    for ext, sites in sorted(by_ext.items()):
        server_config = available.get(ext)
        if server_config is None:
            # No server → sites stay unresolved
            stats["unresolved"] = int(stats["unresolved"]) + len(sites)  # type: ignore[arg-type]
            continue

        lang_stats = {"total": len(sites), "resolved": 0}

        cmd = str(server_config["cmd"])
        args = list(server_config.get("args", []))  # type: ignore[arg-type]

        if verbose:
            print(f"\nResolving {len(sites)} {ext} call sites via {cmd}...", file=sys.stderr)

        client = LSPClient(cmd, args, workspace_root)
        try:
            client.start()

            # Open all unique files
            unique_files: set[str] = set()
            for cs in sites:
                fp = cs["file_path"]
                if fp not in unique_files:
                    try:
                        client.open_file(fp)
                        unique_files.add(fp)
                    except Exception:
                        pass

            # Give server time to index after opening files
            time.sleep(1.0)

            # Resolve each call site
            for cs in sites:
                try:
                    # call_sites.line is 1-indexed from tree-sitter,
                    # LSP expects 0-indexed
                    result = client.goto_definition(
                        cs["file_path"],
                        cs["line"] - 1,
                        cs["col"],
                    )

                    if result is None:
                        continue

                    target_file = result["file"]
                    target_line = result["line"]

                    # Match target to a node in graph.db
                    target_node = conn.execute(
                        "SELECT id FROM nodes "
                        "WHERE file_path = ? "
                        "AND start_line <= ? AND end_line >= ? "
                        "AND label IN ('Function', 'Method', 'Class') "
                        "ORDER BY (end_line - start_line) ASC LIMIT 1",
                        (target_file, target_line + 1, target_line + 1),
                    ).fetchone()

                    if target_node is None:
                        # Try with just the file path (target line might be off)
                        target_node = conn.execute(
                            "SELECT id FROM nodes "
                            "WHERE file_path = ? AND name = ? "
                            "LIMIT 1",
                            (target_file, cs["callee_name"]),
                        ).fetchone()

                    if target_node is not None:
                        # Insert LSP-verified edge
                        conn.execute(
                            "INSERT INTO edges "
                            "(source_id, target_id, type, source_line, source_file, "
                            "resolution_method, confidence) "
                            "VALUES (?, ?, 'CALLS', ?, ?, 'lsp', 1.0)",
                            (
                                cs["caller_node_id"],
                                target_node["id"],
                                cs["line"],
                                cs["file_path"],
                            ),
                        )
                        # Mark call site as resolved
                        conn.execute(
                            "UPDATE call_sites SET resolved = 1 WHERE id = ?",
                            (cs["id"],),
                        )
                        lang_stats["resolved"] += 1

                except Exception:
                    continue

            conn.commit()

        except Exception as e:
            if verbose:
                print(f"  Error with {cmd}: {e}", file=sys.stderr)
        finally:
            client.stop()

        if verbose:
            print(
                f"  Resolved {lang_stats['resolved']}/{lang_stats['total']} "
                f"{ext} call sites",
                file=sys.stderr,
            )

        stats["resolved"] = int(stats["resolved"]) + lang_stats["resolved"]  # type: ignore[arg-type]
        stats["languages"][ext] = lang_stats  # type: ignore[index]

    # 4. Downgrade name-match edges for LSP-covered languages
    for ext in available:
        conn.execute(
            "UPDATE edges SET confidence = 0.2 "
            "WHERE resolution_method = 'name_match' "
            "AND source_id IN ("
            "  SELECT id FROM nodes WHERE file_path LIKE ?"
            ")",
            (f"%{ext}",),
        )
    conn.commit()

    # Final stats
    total_resolved = int(stats["resolved"])  # type: ignore[arg-type]
    total_unresolved = len(call_sites) - total_resolved
    stats["unresolved"] = total_unresolved

    if verbose:
        print(f"\nTotal: {total_resolved}/{len(call_sites)} resolved", file=sys.stderr)

        # Show edge quality summary
        rows = conn.execute(
            "SELECT resolution_method, COUNT(*), "
            "ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM edges), 1) as pct "
            "FROM edges GROUP BY resolution_method ORDER BY COUNT(*) DESC"
        ).fetchall()
        print("\nEdge quality:", file=sys.stderr)
        for row in rows:
            print(f"  {row[0] or 'unknown'}: {row[1]} ({row[2]}%)", file=sys.stderr)

    conn.close()
    return stats


def main() -> None:
    """CLI entry point for LSP edge resolution."""
    parser = argparse.ArgumentParser(
        description="Resolve graph.db edges via LSP servers"
    )
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--root", required=True, help="Workspace root directory")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: {args.db} not found", file=sys.stderr)
        sys.exit(1)

    stats = resolve_edges_with_lsp(args.db, args.root, verbose=not args.quiet)

    # Print JSON stats to stdout
    import json
    print(json.dumps(stats))


if __name__ == "__main__":
    main()
