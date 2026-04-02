"""Entry point for GroundTruth."""

from __future__ import annotations

import argparse
import os
import sys
import warnings

# M9: Only suppress Windows asyncio noise in production (not when GT_DEBUG is set).
# These are spurious ResourceWarning/unraisable errors from ProactorEventLoop
# on Windows — transports get GC'd after the loop closes.
if not os.environ.get("GT_DEBUG"):
    warnings.filterwarnings("ignore", category=ResourceWarning)

    _original_unraisablehook = sys.unraisablehook

    def _quiet_unraisablehook(unraisable: sys.UnraisableHookArgs) -> None:
        exc = unraisable.exc_value
        if exc is not None:
            msg = str(exc)
            if "Event loop is closed" in msg or "I/O operation on closed pipe" in msg:
                return
        _original_unraisablehook(unraisable)

    sys.unraisablehook = _quiet_unraisablehook

from groundtruth import __version__  # noqa: E402


def cli() -> None:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="groundtruth",
        description="MCP server — compiler-grade codebase intelligence for AI coding agents",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    serve_parser = subparsers.add_parser("serve", help="Start the MCP server (stdio)")
    serve_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")
    serve_parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (default: <root>/.groundtruth/index.db)",
    )
    serve_parser.add_argument(
        "--no-auto-index",
        action="store_true",
        help="Don't auto-index if no index exists",
    )
    serve_parser.add_argument(
        "--lsp-trace",
        default=None,
        help="Directory for LSP trace files (JSONL)",
    )

    index_parser = subparsers.add_parser("index", help="Index a project")
    index_parser.add_argument("path", nargs="?", default=os.getcwd(), help="Project root directory")
    index_parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (default: <root>/.groundtruth/index.db)",
    )
    index_parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Max seconds for indexing (default: 600)",
    )
    index_parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Directory names to skip (can be repeated)",
    )
    index_parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing index and rebuild from scratch",
    )
    index_parser.add_argument(
        "--lsp-trace",
        default=None,
        help="Directory for LSP trace files (JSONL)",
    )
    index_parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max concurrent file indexing (default: 10)",
    )
    index_parser.add_argument(
        "--max-file-size",
        type=int,
        default=1048576,
        help="Skip files larger than this (bytes, default: 1MB)",
    )

    status_parser = subparsers.add_parser("status", help="Show index status")
    status_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")
    status_parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite database (default: <root>/.groundtruth/index.db)",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output status as JSON",
    )

    stats_parser = subparsers.add_parser("stats", help="Show intervention stats")
    stats_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")

    validate_parser = subparsers.add_parser("validate", help="Validate a file against the index")
    validate_parser.add_argument("file", help="File to validate")
    validate_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")

    dead_code_parser = subparsers.add_parser("dead-code", help="Find unused exported symbols")
    dead_code_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")

    risk_map_parser = subparsers.add_parser("risk-map", help="Show hallucination risk scores")
    risk_map_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")
    risk_map_parser.add_argument(
        "--limit", type=int, default=20, help="Max files to show (default: 20)"
    )

    viz_parser = subparsers.add_parser("viz", help="Generate 3D Code City risk map")
    viz_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")
    viz_parser.add_argument(
        "--db", default=None, help="Path to SQLite database (default: <root>/.groundtruth/index.db)"
    )
    viz_parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output HTML file path (default: <root>/.groundtruth/risk_map.html)",
    )
    viz_parser.add_argument(
        "--limit", type=int, default=200, help="Max files to include (default: 200)"
    )
    viz_parser.add_argument(
        "--theme",
        choices=["dark", "light"],
        default="dark",
        help="Color theme (default: dark)",
    )
    viz_parser.add_argument(
        "--no-bloom",
        action="store_true",
        default=False,
        help="Disable bloom post-processing effect",
    )
    viz_parser.add_argument(
        "--filter",
        choices=["low", "moderate", "high", "critical"],
        default=None,
        help="Filter nodes by risk level",
    )

    setup_parser = subparsers.add_parser("setup", help="Check LSP server availability")
    setup_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")

    resolve_parser = subparsers.add_parser(
        "resolve", help="Show ambiguous edges that could benefit from LSP resolution"
    )
    resolve_parser.add_argument("--db", required=True, help="Path to graph.db")
    resolve_parser.add_argument("--root", default=os.getcwd(), help="Project root directory")
    resolve_parser.add_argument(
        "--min-confidence", type=float, default=0.9, help="Threshold (default: 0.9)"
    )
    resolve_parser.add_argument("--lang", default=None, help="Filter by language")

    verify_parser = subparsers.add_parser("verify", help="Run pre-benchmark verification")
    verify_parser.add_argument("--repo", required=True, help="Path to repo to verify against")
    verify_parser.add_argument("--output", "-o", default=None, help="Output directory for results")
    verify_parser.add_argument("--checks", default=None, help="Run specific checks (e.g. 1,5,9)")
    verify_parser.add_argument("--verbose", action="store_true", help="Print full tool responses")
    verify_parser.add_argument("--timeout", type=int, default=600, help="Index timeout seconds")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


def _dispatch(args: argparse.Namespace) -> None:
    """Dispatch to the appropriate command handler."""
    if args.command == "serve":
        from groundtruth.cli.commands import serve_cmd

        serve_cmd(
            os.path.abspath(args.root),
            db_path=args.db,
            no_auto_index=args.no_auto_index,
            lsp_trace=args.lsp_trace,
        )
    elif args.command == "index":
        from groundtruth.cli.commands import index_cmd

        index_cmd(
            os.path.abspath(args.path),
            db_path=args.db,
            timeout=args.timeout,
            exclude_patterns=args.exclude or None,
            force=args.force,
            lsp_trace=args.lsp_trace,
            concurrency=args.concurrency,
            max_file_size=args.max_file_size,
        )
    elif args.command == "status":
        from groundtruth.cli.commands import status_cmd

        status_cmd(
            os.path.abspath(args.root),
            db_path=args.db,
            json_output=args.json_output,
        )
    elif args.command == "stats":
        from groundtruth.cli.commands import stats_cmd

        stats_cmd(os.path.abspath(args.root))
    elif args.command == "validate":
        from groundtruth.cli.commands import validate_cmd

        validate_cmd(args.file, os.path.abspath(args.root))
    elif args.command == "dead-code":
        from groundtruth.cli.commands import dead_code_cmd

        dead_code_cmd(os.path.abspath(args.root))
    elif args.command == "risk-map":
        from groundtruth.cli.commands import risk_map_cmd

        risk_map_cmd(os.path.abspath(args.root), limit=args.limit)
    elif args.command == "setup":
        from groundtruth.cli.commands import setup_cmd

        setup_cmd(os.path.abspath(args.root))
    elif args.command == "resolve":
        from groundtruth.resolve import resolve_main

        resolve_main()
    elif args.command == "verify":
        from groundtruth.cli.commands import verify_cmd

        verify_cmd(
            repo=os.path.abspath(args.repo),
            output=args.output,
            checks=args.checks,
            verbose=args.verbose,
            timeout=args.timeout,
        )
    elif args.command == "viz":
        _run_viz(args)
    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)


def _run_viz(args: argparse.Namespace) -> None:
    """Generate the 3D Code City risk map."""
    import webbrowser
    from pathlib import Path

    from groundtruth.analysis.risk_scorer import RiskScorer
    from groundtruth.index.store import SymbolStore
    from groundtruth.utils.result import Err
    from groundtruth.viz import generate_graph_data, render_risk_map

    root = os.path.abspath(args.root)
    db_path = args.db or os.path.join(root, ".groundtruth", "index.db")
    output = args.output or os.path.join(root, ".groundtruth", "risk_map.html")

    if not os.path.exists(db_path):
        print(f"Error: database not found at {db_path}")
        print("Run 'groundtruth index' first to build the symbol index.")
        sys.exit(1)

    store = SymbolStore(db_path=db_path)
    init_result = store.initialize()
    if isinstance(init_result, Err):
        print(f"Error initializing store: {init_result.error.message}")
        sys.exit(1)

    scorer = RiskScorer(store)
    data_result = generate_graph_data(store, scorer, limit=args.limit)
    if isinstance(data_result, Err):
        print(f"Error generating graph data: {data_result.error.message}")
        sys.exit(1)

    graph_data = data_result.value

    # Filter by risk level if specified
    filter_level: str | None = args.filter
    if filter_level is not None:
        thresholds: dict[str, tuple[float, float]] = {
            "critical": (0.7, 1.1),
            "high": (0.45, 0.7),
            "moderate": (0.25, 0.45),
            "low": (0.0, 0.25),
        }
        lo, hi = thresholds[filter_level]
        graph_data.nodes = [n for n in graph_data.nodes if lo <= n.risk_score < hi]

    render_result = render_risk_map(
        graph_data,
        output,
        theme=args.theme,
        bloom=not args.no_bloom,
    )
    if isinstance(render_result, Err):
        print(f"Error writing risk map: {render_result.error.message}")
        sys.exit(1)

    # Print risk summary before opening browser
    from groundtruth.cli.output import render_risk_summary

    all_scores = scorer.score_codebase(limit=500)
    if not isinstance(all_scores, Err):
        dead_result = store.get_dead_code()
        unused_result = store.get_unused_packages()
        pkgs_result = store.get_all_packages()
        stats_result = store.get_stats()
        if not isinstance(stats_result, Err):
            summary = render_risk_summary(
                project_name=os.path.basename(root),
                stats=stats_result.value,
                risk_scores=all_scores.value,
                dead_code_count=len(dead_result.value) if not isinstance(dead_result, Err) else 0,
                unused_packages_count=len(unused_result.value)
                if not isinstance(unused_result, Err)
                else 0,
                packages_count=len(pkgs_result.value) if not isinstance(pkgs_result, Err) else 0,
                command="viz",
            )
            print(summary)

    print(f"Risk map generated: {render_result.value}")
    print(f"  {len(data_result.value.nodes)} files, {len(data_result.value.edges)} edges")

    webbrowser.open(Path(render_result.value).as_uri())
    print("Opened risk map in browser.")


if __name__ == "__main__":
    cli()
