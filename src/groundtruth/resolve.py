"""gt-resolve: Diagnose and resolve ambiguous edges in graph.db using LSP.

Two modes:
  - Diagnostic (default): show ambiguous edges and which LSP servers could resolve them
  - Resolution (--resolve): use installed LSP servers to verify/fix ambiguous edges

Usage:
    groundtruth resolve --db graph.db                        # diagnostic mode
    groundtruth resolve --db graph.db --resolve              # live LSP resolution
    groundtruth resolve --db graph.db --resolve --lang python  # resolve Python only
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
import time


# Language server commands for auto-detection (used for reporting)
_KNOWN_SERVERS: dict[str, str] = {
    "python": "pyright-langserver",
    "javascript": "typescript-language-server",
    "typescript": "typescript-language-server",
    "go": "gopls",
    "rust": "rust-analyzer",
    "java": "jdtls",
    "c": "clangd",
    "cpp": "clangd",
    "ruby": "solargraph",
    "kotlin": "kotlin-language-server",
}


def _detect_servers() -> dict[str, bool]:
    """Detect which language servers are installed."""
    return {lang: shutil.which(cmd) is not None for lang, cmd in _KNOWN_SERVERS.items()}


def _get_ambiguous_edges(
    conn: sqlite3.Connection,
    min_confidence: float = 0.9,
    language: str | None = None,
    source_files: list[str] | None = None,
) -> list[dict]:
    """Get edges below confidence threshold.

    Args:
        source_files: If provided, only return edges whose source_file
            matches one of these paths (scoped promotion).
    """
    conn.row_factory = sqlite3.Row

    # Check if confidence column exists
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        print(
            "ERROR: graph.db has no confidence column (indexed with old gt-index).", file=sys.stderr
        )
        print("Re-index with gt-index v14+ to add confidence scoring.", file=sys.stderr)
        return []

    query = """
        SELECT e.id, e.source_id, e.target_id, e.resolution_method,
               e.confidence, e.source_file, e.source_line,
               src.name as caller_name, src.language,
               tgt.name as target_name, tgt.file_path as target_file
        FROM edges e
        JOIN nodes src ON e.source_id = src.id
        JOIN nodes tgt ON e.target_id = tgt.id
        WHERE e.confidence < ? AND e.type = 'CALLS'
    """
    params: list = [min_confidence]

    if language:
        query += " AND src.language = ?"
        params.append(language)

    if source_files:
        placeholders = ",".join("?" for _ in source_files)
        query += f" AND e.source_file IN ({placeholders})"
        params.extend(source_files)

    query += " ORDER BY e.confidence ASC LIMIT 500"

    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _print_summary(
    edges: list[dict],
    servers: dict[str, bool],
    min_confidence: float,
) -> None:
    """Print human-readable summary of ambiguous edges."""
    if not edges:
        print("No ambiguous edges found below confidence threshold.")
        return

    # Group by confidence bucket
    buckets: dict[str, list] = {"0.0-0.2": [], "0.2-0.4": [], "0.4-0.6": [], "0.6-0.9": []}
    for e in edges:
        c = e["confidence"]
        if c < 0.2:
            buckets["0.0-0.2"].append(e)
        elif c < 0.4:
            buckets["0.2-0.4"].append(e)
        elif c < 0.6:
            buckets["0.4-0.6"].append(e)
        else:
            buckets["0.6-0.9"].append(e)

    print(f"\n{'=' * 60}")
    print(f"Ambiguous edges (confidence < {min_confidence}): {len(edges)}")
    print(f"{'=' * 60}\n")

    for bucket_name, bucket_edges in buckets.items():
        if bucket_edges:
            print(f"  [{bucket_name}] {len(bucket_edges)} edges")

    # Group by language
    by_lang: dict[str, int] = {}
    for e in edges:
        lang = e.get("language", "unknown")
        by_lang[lang] = by_lang.get(lang, 0) + 1

    print("\nBy language:")
    for lang, count in sorted(by_lang.items(), key=lambda x: -x[1]):
        server_status = "installed" if servers.get(lang) else "NOT INSTALLED"
        print(f"  {lang}: {count} edges (LSP server: {server_status})")

    # Show sample edges
    print("\nSample ambiguous edges (top 20):")
    print(f"{'Confidence':>10}  {'Caller':30s}  {'Target':30s}  {'Method'}")
    print(f"{'-' * 10}  {'-' * 30}  {'-' * 30}  {'-' * 12}")
    for e in edges[:20]:
        caller = f"{e['caller_name']}() @ {os.path.basename(e.get('source_file', '?'))}"
        target = f"{e['target_name']}() @ {os.path.basename(e.get('target_file', '?'))}"
        print(f"{e['confidence']:>10.2f}  {caller:30s}  {target:30s}  {e['resolution_method']}")

    if len(edges) > 20:
        print(f"  ... and {len(edges) - 20} more")

    # Resolution recommendation
    resolvable = sum(1 for e in edges if servers.get(e.get("language", ""), False))
    print(f"\n{'=' * 60}")
    print(f"Resolvable with installed LSP servers: {resolvable}/{len(edges)} edges")
    if resolvable < len(edges):
        missing_langs = {e.get("language") for e in edges if not servers.get(e.get("language", ""))}
        print(f"Install LSP servers for: {', '.join(sorted(missing_langs))}")
        for lang in sorted(missing_langs):
            cmd = _KNOWN_SERVERS.get(lang, "?")
            print(f"  {lang}: install '{cmd}'")
    print(f"{'=' * 60}")


async def _resolve_edges(
    db_path: str,
    root: str,
    edges: list[dict],
    language: str,
) -> dict[str, int]:
    """Resolve ambiguous edges using LSP textDocument/definition.

    For each ambiguous edge:
    1. Open the source file in the LSP server
    2. Ask textDocument/definition at the call site
    3. If LSP returns a target:
       - If it matches the current edge target → upgrade confidence to 1.0
       - If it differs → update edge target + confidence to 1.0
       - If no target in graph → delete the edge (false positive)
    """
    try:
        from groundtruth.lsp.client import LSPClient
        from groundtruth.lsp.config import get_server_config
        from groundtruth.utils.result import Err as LspErr
    except ImportError:
        print(
            "ERROR: LSP client not available. Install with: pip install -e '.[dev]'",
            file=sys.stderr,
        )
        return {"error": 1}

    stats = {"verified": 0, "corrected": 0, "deleted": 0, "failed": 0, "skipped": 0}

    ext = f".{language}" if not language.startswith(".") else language
    config_result = get_server_config(ext)
    if isinstance(config_result, LspErr):
        print(f"  No LSP server configured for {language}", file=sys.stderr)
        stats["skipped"] = len(edges)
        return stats

    config = config_result.value

    # Start LSP server
    abs_root = os.path.abspath(root)
    root_uri = f"file:///{abs_root.replace(os.sep, '/')}"
    print(f"  Starting {config.command[0]} for {language}...")
    client = LSPClient(config.command, root_uri)

    try:
        start_result = await client.start()
        if isinstance(start_result, LspErr):
            print(f"  LSP start failed: {start_result.error.message}", file=sys.stderr)
            stats["failed"] = len(edges)
            return stats
    except Exception as e:
        print(f"  Failed to start LSP: {e}", file=sys.stderr)
        stats["failed"] = len(edges)
        return stats

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    opened_files: set[str] = set()

    for i, edge in enumerate(edges):
        source_file = edge.get("source_file", "")
        source_line = edge.get("source_line", 0) or 0
        target_name = edge.get("target_name", "")

        if not source_file or not target_name:
            stats["skipped"] += 1
            continue

        abs_source = os.path.join(abs_root, source_file)
        if not os.path.exists(abs_source):
            stats["skipped"] += 1
            continue

        # Open the file in LSP if not already opened
        uri = f"file:///{abs_source.replace(os.sep, '/')}"
        if uri not in opened_files:
            try:
                with open(abs_source, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                await client.did_open(uri, language, 1, text)
                opened_files.add(uri)
            except Exception:
                stats["failed"] += 1
                continue

        # Find column of the call on the source line
        try:
            with open(abs_source, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if source_line <= 0 or source_line > len(lines):
                stats["skipped"] += 1
                continue
            line_text = lines[source_line - 1]  # 1-indexed
            col = line_text.find(target_name)
            if col == -1:
                col = 0
        except Exception:
            stats["failed"] += 1
            continue

        # Ask LSP for definition
        try:
            def_result = await client.definition(uri, source_line - 1, col)
            if isinstance(def_result, LspErr):
                stats["failed"] += 1
                continue

            locations = def_result.value
            if not locations:
                # LSP couldn't resolve — mark as checked
                stats["failed"] += 1
                continue

            # Got a definition location
            target_uri = locations[0].uri
            target_line = locations[0].range.start.line + 1  # 0-indexed → 1-indexed

            # Convert URI to relative path
            target_path = target_uri.replace("file:///", "").replace("file://", "")
            if os.name == "nt":
                target_path = target_path.lstrip("/")
            try:
                target_rel = os.path.relpath(target_path, abs_root).replace("\\", "/")
            except ValueError:
                target_rel = target_path

            # Find matching node in graph.db
            row = conn.execute(
                """SELECT id FROM nodes
                   WHERE file_path LIKE ? AND name = ?
                   AND start_line <= ? AND (end_line >= ? OR end_line IS NULL)
                   ORDER BY start_line DESC LIMIT 1""",
                (f"%{os.path.basename(target_rel)}", target_name, target_line, target_line),
            ).fetchone()

            if row:
                lsp_target_id = row["id"]
                current_target_id = edge["target_id"]

                if lsp_target_id == current_target_id:
                    # Tree-sitter was right — upgrade confidence
                    conn.execute(
                        "UPDATE edges SET confidence = 1.0, resolution_method = 'lsp' WHERE id = ?",
                        (edge["id"],),
                    )
                    stats["verified"] += 1
                else:
                    # Tree-sitter pointed to wrong target — fix it
                    conn.execute(
                        "UPDATE edges SET target_id = ?, confidence = 1.0, resolution_method = 'lsp' WHERE id = ?",
                        (lsp_target_id, edge["id"]),
                    )
                    stats["corrected"] += 1
            else:
                # LSP found a definition not in our graph — edge is false positive
                conn.execute("DELETE FROM edges WHERE id = ?", (edge["id"],))
                stats["deleted"] += 1

        except Exception:
            stats["failed"] += 1
            continue

        # Progress every 100 edges
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(edges)} edges processed", file=sys.stderr)

    conn.commit()
    conn.close()

    # Shutdown LSP
    try:
        await client.shutdown()
    except Exception:
        pass

    return stats


def resolve_main() -> None:
    """CLI entry point for gt-resolve."""
    parser = argparse.ArgumentParser(
        prog="groundtruth resolve",
        description="Diagnose and resolve ambiguous edges in graph.db using LSP",
    )
    parser.add_argument("--db", required=True, help="Path to graph.db")
    parser.add_argument("--root", default=".", help="Project root directory")
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=0.9,
        help="Show edges below this confidence (default: 0.9)",
    )
    parser.add_argument("--lang", default=None, help="Filter by language")
    parser.add_argument(
        "--resolve",
        action="store_true",
        help="Actually resolve edges via LSP (not just diagnose)",
    )
    parser.add_argument(
        "--max-edges",
        type=int,
        default=500,
        help="Maximum edges to resolve (default: 500)",
    )
    args = parser.parse_args(
        sys.argv[sys.argv.index("resolve") + 1 :] if "resolve" in sys.argv else []
    )

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    servers = _detect_servers()
    print(f"Available LSP servers: {', '.join(lang for lang, v in servers.items() if v) or 'none'}")

    conn = sqlite3.connect(args.db)
    edges = _get_ambiguous_edges(conn, args.min_confidence, args.lang)
    conn.close()

    if args.resolve:
        # Live resolution mode
        if not args.lang:
            print("ERROR: --resolve requires --lang (e.g., --lang python)", file=sys.stderr)
            sys.exit(1)

        if not servers.get(args.lang):
            print(f"ERROR: No LSP server installed for {args.lang}", file=sys.stderr)
            sys.exit(1)

        lang_edges = [e for e in edges if e.get("language") == args.lang][: args.max_edges]
        if not lang_edges:
            print(f"No ambiguous {args.lang} edges to resolve.")
            return

        print(f"\nResolving {len(lang_edges)} {args.lang} edges via LSP...")
        start = time.time()
        stats = asyncio.run(_resolve_edges(args.db, args.root, lang_edges, args.lang))
        elapsed = time.time() - start

        print(f"\nResults ({elapsed:.1f}s):")
        print(f"  Verified (tree-sitter was correct): {stats.get('verified', 0)}")
        print(f"  Corrected (pointed to wrong target): {stats.get('corrected', 0)}")
        print(f"  Deleted (false positive): {stats.get('deleted', 0)}")
        print(f"  Failed (LSP couldn't resolve): {stats.get('failed', 0)}")
        print(f"  Skipped: {stats.get('skipped', 0)}")
    else:
        # Diagnostic mode (default)
        _print_summary(edges, servers, args.min_confidence)


if __name__ == "__main__":
    resolve_main()
