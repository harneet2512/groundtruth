"""CLI command implementations."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from groundtruth.index.store import SymbolStore
from groundtruth.utils.result import Err

if TYPE_CHECKING:
    from groundtruth.analysis.risk_scorer import RiskScore


def _load_store(root: str, db_path: str | None = None) -> SymbolStore:
    """Load an existing SymbolStore or exit with an error."""

    resolved = db_path or os.path.join(root, ".groundtruth", "index.db")
    if not os.path.isfile(resolved):
        print(f"No index found at {resolved}. Run 'groundtruth index <path>' first.")
        sys.exit(1)

    store = SymbolStore(db_path=resolved)
    result = store.initialize()
    if isinstance(result, Err):
        print(f"Error initializing store: {result.error.message}")
        sys.exit(1)
    return store


def _gather_risk_data(
    store: SymbolStore,
) -> tuple[list[RiskScore], int, int, int]:
    """Gather risk scores, dead code count, unused packages count, packages count."""
    from groundtruth.analysis.risk_scorer import RiskScorer

    scorer = RiskScorer(store)
    risk_result = scorer.score_codebase(limit=500)
    risk_scores = risk_result.value if not isinstance(risk_result, Err) else []

    dead_result = store.get_dead_code()
    dead_code_count = len(dead_result.value) if not isinstance(dead_result, Err) else 0

    unused_result = store.get_unused_packages()
    unused_packages_count = len(unused_result.value) if not isinstance(unused_result, Err) else 0

    pkgs_result = store.get_all_packages()
    packages_count = len(pkgs_result.value) if not isinstance(pkgs_result, Err) else 0

    return risk_scores, dead_code_count, unused_packages_count, packages_count


def index_cmd(
    root: str,
    *,
    db_path: str | None = None,
    timeout: int = 300,
    exclude_patterns: list[str] | None = None,
    force: bool = False,
    lsp_trace: str | None = None,
    concurrency: int = 10,
    max_file_size: int = 1_048_576,
) -> None:
    """Index the current project."""
    from groundtruth.cli.output import render_risk_summary
    from groundtruth.index.indexer import Indexer
    from groundtruth.index.store import SymbolStore
    from groundtruth.lsp.manager import LSPManager

    gt_dir = os.path.join(root, ".groundtruth")
    os.makedirs(gt_dir, exist_ok=True)

    resolved_db = db_path or os.path.join(gt_dir, "index.db")

    if os.path.isfile(resolved_db) and not force:
        print(f"Index already exists at {resolved_db}. Use --force to rebuild.")
        sys.exit(0)

    if force and os.path.isfile(resolved_db):
        os.remove(resolved_db)

    store = SymbolStore(db_path=resolved_db)
    init_result = store.initialize()
    if isinstance(init_result, Err):
        print(f"Error initializing store: {init_result.error.message}")
        sys.exit(1)

    trace_dir = Path(lsp_trace) if lsp_trace else None
    lsp_manager = LSPManager(root, trace_dir=trace_dir)
    exclude_dirs = set(exclude_patterns) if exclude_patterns else None
    indexer = Indexer(store, lsp_manager, exclude_dirs=exclude_dirs)
    start_time = time.monotonic()

    async def _run() -> int:
        try:
            result = await asyncio.wait_for(
                indexer.index_project(
                    root,
                    concurrency=concurrency,
                    max_file_size=max_file_size,
                ),
                timeout=float(timeout),
            )
            if isinstance(result, Err):
                print(f"Indexing error: {result.error.message}")
                if result.error.details:
                    for key, val in result.error.details.items():
                        print(f"  {key}: {val}")
                sys.exit(1)
            return result.value
        except asyncio.TimeoutError:
            print(f"Indexing timed out after {timeout}s.")
            sys.exit(1)
        finally:
            # Force-kill all LSP processes before shutdown to prevent hangs
            for client in list(lsp_manager._clients.values()):
                proc = getattr(client, "_process", None)
                if proc is not None and proc.returncode is None:
                    try:
                        proc.kill()
                    except (OSError, ProcessLookupError):
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3.0)
                    except (asyncio.TimeoutError, OSError, ProcessLookupError):
                        pass
                client._closed = True
                client._process = None
                client._started = False
            await lsp_manager.shutdown_all()

    try:
        symbol_count = asyncio.run(_run())
        elapsed = time.monotonic() - start_time

        stats_result = store.get_stats()
        if isinstance(stats_result, Err):
            print(f"Indexed {symbol_count} symbols in {elapsed:.1f}s.")
        else:
            risk_scores, dead_code_count, unused_packages_count, packages_count = _gather_risk_data(
                store
            )
            summary = render_risk_summary(
                project_name=os.path.basename(root),
                stats=stats_result.value,
                risk_scores=risk_scores,
                dead_code_count=dead_code_count,
                unused_packages_count=unused_packages_count,
                packages_count=packages_count,
                elapsed_seconds=elapsed,
                command="index",
            )
            print(summary)
    finally:
        store.close()


def status_cmd(
    root: str,
    *,
    db_path: str | None = None,
    json_output: bool = False,
) -> None:
    """Show GroundTruth status."""
    from groundtruth.cli.output import render_risk_summary, render_status_json

    store = _load_store(root, db_path=db_path)
    try:
        stats_result = store.get_stats()
        if isinstance(stats_result, Err):
            print(f"Error reading stats: {stats_result.error.message}")
            sys.exit(1)

        risk_scores, dead_code_count, unused_packages_count, packages_count = _gather_risk_data(
            store
        )
        project_name = os.path.basename(root)

        if json_output:
            print(
                render_status_json(
                    project_name=project_name,
                    stats=stats_result.value,
                    risk_scores=risk_scores,
                    dead_code_count=dead_code_count,
                    unused_packages_count=unused_packages_count,
                    packages_count=packages_count,
                )
            )
        else:
            print(
                render_risk_summary(
                    project_name=project_name,
                    stats=stats_result.value,
                    risk_scores=risk_scores,
                    dead_code_count=dead_code_count,
                    unused_packages_count=unused_packages_count,
                    packages_count=packages_count,
                    command="status",
                )
            )
    finally:
        store.close()


def serve_cmd(
    root: str,
    *,
    db_path: str | None = None,
    no_auto_index: bool = False,
    lsp_trace: str | None = None,
) -> None:
    """Start the MCP server."""
    try:
        # Activate serve-safe logging (WARNING+, no ANSI, stderr only)
        # BEFORE importing server/tools which call get_logger() at module level.
        from groundtruth.utils.logger import configure_serve_logging

        configure_serve_logging()

        from groundtruth.mcp.server import create_server

        resolved_db = db_path or os.path.join(root, ".groundtruth", "index.db")

        if not os.path.isfile(resolved_db):
            if no_auto_index:
                print(
                    f"No index found at {resolved_db} and --no-auto-index is set. "
                    "Run 'groundtruth index' first.",
                    file=sys.stderr,
                )
                sys.exit(1)
            # Auto-index — redirect stdout to stderr so MCP stdio transport stays clean
            print("No index found. Auto-indexing...", file=sys.stderr)
            _saved_stdout = sys.stdout
            sys.stdout = sys.stderr
            try:
                index_cmd(root, db_path=db_path, lsp_trace=lsp_trace)
            finally:
                sys.stdout = _saved_stdout

        trace_dir = Path(lsp_trace) if lsp_trace else None
        app = create_server(root, db_path=resolved_db, lsp_trace_dir=trace_dir)
        app.run(transport="stdio")
    except BrokenPipeError:
        sys.exit(0)


def stats_cmd(root: str) -> None:
    """Show intervention statistics."""
    from groundtruth.stats.reporter import StatsReporter
    from groundtruth.stats.tracker import InterventionTracker

    store = _load_store(root)
    try:
        tracker = InterventionTracker(store)
        reporter = StatsReporter(tracker)
        result = reporter.generate_report()
        if isinstance(result, Err):
            print(f"Error generating report: {result.error.message}")
            sys.exit(1)
        print(result.value)
    finally:
        store.close()


def validate_cmd(file_path: str, root: str) -> None:
    """Validate code against the index."""
    from groundtruth.validators.orchestrator import ValidationOrchestrator

    store = _load_store(root)
    try:
        abs_path = os.path.abspath(file_path)
        if not os.path.isfile(abs_path):
            print(f"File not found: {abs_path}")
            sys.exit(1)

        code = Path(abs_path).read_text(encoding="utf-8")
        orchestrator = ValidationOrchestrator(store, api_key=os.environ.get("ANTHROPIC_API_KEY"))
        result = asyncio.run(orchestrator.validate(code, abs_path))
        if isinstance(result, Err):
            print(f"Validation error: {result.error.message}")
            sys.exit(1)

        vr = result.value
        if vr.valid:
            print("No issues found.")
        else:
            print(f"Found {len(vr.errors)} issue(s):\n")
            for err in vr.errors:
                print(f"  [{err.get('type', 'unknown')}] {err.get('message', '')}")
                suggestion = err.get("suggestion")
                if isinstance(suggestion, dict):
                    fix = suggestion.get("fix")
                    if fix:
                        print(f"    Suggestion: {fix}")
                    reason = suggestion.get("reason")
                    if reason:
                        print(f"    Reason: {reason}")
                print()
    finally:
        store.close()


def dead_code_cmd(root: str) -> None:
    """Find exported symbols with zero references."""
    store = _load_store(root)
    try:
        result = store.get_dead_code()
        if isinstance(result, Err):
            print(f"Error: {result.error.message}")
            sys.exit(1)

        symbols = result.value
        if not symbols:
            print("No dead code found.")
            return

        print(f"{'Name':<40} {'Kind':<12} {'File'}")
        print("-" * 90)
        for sym in symbols:
            print(f"{sym.name:<40} {sym.kind:<12} {sym.file_path}")
    finally:
        store.close()


def verify_cmd(
    repo: str,
    *,
    output: str | None = None,
    checks: str | None = None,
    verbose: bool = False,
    timeout: int = 120,
) -> None:
    """Run pre-benchmark verification against a real repo."""
    # Add benchmarks dir to sys.path so we can import verify module
    benchmarks_root = Path(__file__).resolve().parent.parent.parent.parent
    bench_path = str(benchmarks_root)
    if bench_path not in sys.path:
        sys.path.insert(0, bench_path)

    from benchmarks.verify.verify import run_verification

    output_dir = output or str(benchmarks_root / "benchmarks" / "verify" / "results")

    report = asyncio.run(
        run_verification(
            repo_path=repo,
            output_dir=output_dir,
            checks_filter=checks,
            verbose=verbose,
            timeout=timeout,
        )
    )
    sys.exit(0 if report.failed == 0 else 1)


def setup_cmd(root: str) -> None:
    """Check LSP server availability for detected languages."""
    import shutil

    from groundtruth.lsp.config import LSP_SERVERS

    # Detect languages
    supported_exts: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        # Quick scan: skip hidden dirs and common noise
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d
            not in {
                "node_modules",
                "__pycache__",
                "venv",
                ".venv",
                "dist",
                "build",
                "target",
                "vendor",
            }
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            if ext in LSP_SERVERS:
                supported_exts.add(ext)

    if not supported_exts:
        print("No supported language files found.")
        return

    install_hints: dict[str, str] = {
        ".py": "pip install pyright  OR  npm install -g pyright",
        ".ts": "npm install -g typescript-language-server typescript",
        ".tsx": "npm install -g typescript-language-server typescript",
        ".js": "npm install -g typescript-language-server typescript",
        ".go": "go install golang.org/x/tools/gopls@latest",
        ".rs": "rustup component add rust-analyzer",
        ".java": "install jdtls (eclipse.jdt.ls)",
    }

    print(f"{'Ext':<8} {'LSP Server':<40} {'Status':<12} {'Install'}")
    print("-" * 100)
    for ext in sorted(supported_exts):
        config = LSP_SERVERS.get(ext)
        if config is None:
            continue
        cmd = config.command[0]
        found = shutil.which(cmd) is not None
        status = "OK" if found else "MISSING"
        hint = "" if found else install_hints.get(ext, "")
        print(f"{ext:<8} {cmd:<40} {status:<12} {hint}")


def check_diff_cmd(
    root: str,
    *,
    db_path: str | None = None,
    diff_file: str | None = None,
    verbose: bool = False,
    output_format: str = "text",
    strict: bool = False,
    install_hook: bool = False,
) -> None:
    """Check change obligations from a unified diff.

    Reads a unified diff (from --diff-file or stdin), extracts changed symbols,
    and reports obligations — things that MUST also change.

    Limitations:
      - Symbol extraction is Python-oriented (recognises ``def``/``class`` patterns).
        JS/TS/Go/Rust function definitions are not yet detected.
      - Requires a pre-built GroundTruth index (``groundtruth index`` first).
      - Results are capped at 10 obligations, sorted by confidence.
    """
    import json
    import stat
    from collections import defaultdict

    from groundtruth.index.graph import ImportGraph
    from groundtruth.validators.obligations import ObligationEngine

    # Handle --install-hook: create/append pre-commit hook and exit
    if install_hook:
        git_hooks_dir = os.path.join(root, ".git", "hooks")
        hook_path = os.path.join(git_hooks_dir, "pre-commit")
        hook_line = "git diff --cached | groundtruth check-diff --terse --strict"

        os.makedirs(git_hooks_dir, exist_ok=True)

        existing_content = ""
        if os.path.isfile(hook_path):
            existing_content = Path(hook_path).read_text(encoding="utf-8")

        if hook_line in existing_content:
            print(f"Hook already installed in {hook_path}")
        else:
            with open(hook_path, "a", encoding="utf-8") as f:
                if not existing_content:
                    f.write("#!/bin/sh\n")
                elif not existing_content.endswith("\n"):
                    f.write("\n")
                f.write(hook_line + "\n")

            # Make executable
            current_mode = os.stat(hook_path).st_mode
            os.chmod(hook_path, current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            print(f"Installed pre-commit hook in {hook_path}")

        sys.exit(0)

    # Read diff text
    if diff_file is not None:
        diff_path = Path(diff_file)
        if not diff_path.is_file():
            print(f"Diff file not found: {diff_file}", file=sys.stderr)
            sys.exit(1)
        diff_text = diff_path.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("No diff provided. Pipe a diff to stdin or use --diff-file.", file=sys.stderr)
            sys.exit(1)
        diff_text = sys.stdin.read()

    if not diff_text.strip():
        if output_format == "json":
            print(json.dumps({"obligations": [], "total": 0, "missed": 0, "exit_code": 0}, indent=2))
        else:
            print("Empty diff — nothing to check.")
        sys.exit(0)

    store = _load_store(root, db_path=db_path)
    try:
        graph = ImportGraph(store)
        engine = ObligationEngine(store, graph)
        obligations = engine.infer_from_patch(diff_text)

        total = len(obligations)
        exit_code = 1 if (strict and total > 0) else (1 if total > 0 else 0)

        if not obligations:
            if output_format == "json":
                print(json.dumps({"obligations": [], "total": 0, "missed": 0, "exit_code": 0}, indent=2))
            else:
                print("No obligations found.")
            sys.exit(0)

        if output_format == "json":
            ob_list = []
            for ob in obligations:
                ob_list.append({
                    "kind": ob.kind,
                    "source": ob.source,
                    "target": ob.target,
                    "target_file": ob.target_file,
                    "confidence": ob.confidence,
                    "reason": ob.reason,
                })
            result = {
                "obligations": ob_list,
                "total": total,
                "missed": total,
                "exit_code": exit_code,
            }

            # Incubator enrichment (Phase 5) — same path as MCP
            from groundtruth.incubator.runtime import IncubatorRuntime, any_phase5_flag_on
            if any_phase5_flag_on():
                cli_runtime = IncubatorRuntime(store, root)
                result = cli_runtime.enrich("check", result)
                cli_runtime.log_interaction("check", result)

            print(json.dumps(result, indent=2))
            sys.exit(exit_code)

        if output_format == "terse":
            for ob in obligations:
                loc = f"{ob.target_file}:{ob.target_line}" if ob.target_line else ob.target_file
                print(f"{ob.kind}: {ob.target} — {ob.reason} ({loc})")
            sys.exit(exit_code)

        # Default text format
        # Group by kind
        by_kind: dict[str, list] = defaultdict(list)
        for ob in obligations:
            by_kind[ob.kind].append(ob)

        print(f"{total} obligation{'s' if total != 1 else ''} found\n")

        for kind, obs in by_kind.items():
            print(f"{kind}:")
            for ob in obs:
                if verbose:
                    loc = f"{ob.target_file}:{ob.target_line}" if ob.target_line else ob.target_file
                    print(f"  {ob.target}")
                    print(f"    reason:     {ob.reason}")
                    print(f"    source:     {ob.source}")
                    print(f"    file:       {loc}")
                    print(f"    confidence: {ob.confidence}")
                else:
                    print(f"  {ob.target} — {ob.reason}")
            print()

        sys.exit(exit_code)
    finally:
        store.close()


def risk_map_cmd(root: str, limit: int = 20) -> None:
    """Show hallucination risk scores for files."""
    from groundtruth.analysis.risk_scorer import RiskScorer

    store = _load_store(root)
    try:
        scorer = RiskScorer(store)
        result = scorer.score_codebase(limit=limit)
        if isinstance(result, Err):
            print(f"Error: {result.error.message}")
            sys.exit(1)

        scores = result.value
        if not scores:
            print("No files scored.")
            return

        print(f"{'Risk':<8} {'Top Factor':<25} {'File'}")
        print("-" * 80)
        for score in scores:
            top_factor = ""
            if score.factors:
                top_factor = max(score.factors, key=score.factors.get)  # type: ignore[arg-type]
            print(f"{score.overall_risk:<8.3f} {top_factor:<25} {score.file_path}")
    finally:
        store.close()
