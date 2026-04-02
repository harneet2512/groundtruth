"""gt-resolve: Show ambiguous edges in graph.db that could benefit from LSP resolution.

In v1.0.0 this is a diagnostic tool (--dry-run is the default).
Future versions will add live LSP resolution via textDocument/definition.

Usage:
    groundtruth resolve --db graph.db                    # show ambiguous edges
    groundtruth resolve --db graph.db --min-confidence 0.5  # custom threshold
    groundtruth resolve --db graph.db --lang python      # filter by language
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys


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
) -> list[dict]:
    """Get edges below confidence threshold."""
    conn.row_factory = sqlite3.Row

    # Check if confidence column exists
    try:
        conn.execute("SELECT confidence FROM edges LIMIT 0")
    except sqlite3.OperationalError:
        print("ERROR: graph.db has no confidence column (indexed with old gt-index).", file=sys.stderr)
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

    print(f"\n{'='*60}")
    print(f"Ambiguous edges (confidence < {min_confidence}): {len(edges)}")
    print(f"{'='*60}\n")

    for bucket_name, bucket_edges in buckets.items():
        if bucket_edges:
            print(f"  [{bucket_name}] {len(bucket_edges)} edges")

    # Group by language
    by_lang: dict[str, int] = {}
    for e in edges:
        lang = e.get("language", "unknown")
        by_lang[lang] = by_lang.get(lang, 0) + 1

    print(f"\nBy language:")
    for lang, count in sorted(by_lang.items(), key=lambda x: -x[1]):
        server_status = "installed" if servers.get(lang) else "NOT INSTALLED"
        print(f"  {lang}: {count} edges (LSP server: {server_status})")

    # Show sample edges
    print(f"\nSample ambiguous edges (top 20):")
    print(f"{'Confidence':>10}  {'Caller':30s}  {'Target':30s}  {'Method'}")
    print(f"{'-'*10}  {'-'*30}  {'-'*30}  {'-'*12}")
    for e in edges[:20]:
        caller = f"{e['caller_name']}() @ {os.path.basename(e.get('source_file', '?'))}"
        target = f"{e['target_name']}() @ {os.path.basename(e.get('target_file', '?'))}"
        print(f"{e['confidence']:>10.2f}  {caller:30s}  {target:30s}  {e['resolution_method']}")

    if len(edges) > 20:
        print(f"  ... and {len(edges) - 20} more")

    # Resolution recommendation
    resolvable = sum(1 for e in edges if servers.get(e.get("language", ""), False))
    print(f"\n{'='*60}")
    print(f"Resolvable with installed LSP servers: {resolvable}/{len(edges)} edges")
    if resolvable < len(edges):
        missing_langs = {e.get("language") for e in edges if not servers.get(e.get("language", ""))}
        print(f"Install LSP servers for: {', '.join(sorted(missing_langs))}")
        for lang in sorted(missing_langs):
            cmd = _KNOWN_SERVERS.get(lang, "?")
            print(f"  {lang}: install '{cmd}'")
    print(f"{'='*60}")


def resolve_main() -> None:
    """CLI entry point for gt-resolve."""
    parser = argparse.ArgumentParser(
        prog="groundtruth resolve",
        description="Show ambiguous edges in graph.db that could benefit from LSP resolution",
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
    args = parser.parse_args(sys.argv[sys.argv.index("resolve") + 1 :] if "resolve" in sys.argv else [])

    if not os.path.exists(args.db):
        print(f"ERROR: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    servers = _detect_servers()
    print(f"Available LSP servers: {', '.join(l for l, v in servers.items() if v) or 'none'}")

    conn = sqlite3.connect(args.db)
    edges = _get_ambiguous_edges(conn, args.min_confidence, args.lang)
    conn.close()

    _print_summary(edges, servers, args.min_confidence)


if __name__ == "__main__":
    resolve_main()
