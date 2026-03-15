"""MCP tool definitions and handlers."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

from groundtruth.ai.briefing import BriefingEngine
from groundtruth.ai.task_parser import TaskParser
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.graph import ImportGraph
from groundtruth.index.store import SymbolStore
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.logger import get_logger
from groundtruth.utils.platform import paths_equal, validate_path
from groundtruth.utils.result import Err, Ok
from groundtruth.grounding.record import build_grounding_record
from groundtruth.validators.orchestrator import ValidationOrchestrator

log = get_logger("mcp.tools")


def _check_path(file_path: str, root_path: str | None) -> dict[str, Any] | None:
    """Validate file_path against root. Returns error dict if invalid, None if ok."""
    if root_path is None:
        return None
    ok, msg = validate_path(file_path, root_path)
    if not ok:
        return {"error": msg}
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_dependency_chain(
    store: SymbolStore, graph: ImportGraph, files: list[str], max_nodes: int = 4
) -> str:
    """Build a readable dependency chain from the highest-usage symbol across files."""
    best_sym = None
    best_usage = -1
    for fp in files[:3]:
        syms_result = store.get_symbols_in_file(fp)
        if isinstance(syms_result, Ok):
            for s in syms_result.value:
                if s.usage_count > best_usage:
                    best_usage = s.usage_count
                    best_sym = s

    if best_sym is None:
        return ""

    chain_parts: list[str] = []

    # Callers → symbol
    callers_result = graph.find_callers(best_sym.name)
    if isinstance(callers_result, Ok) and callers_result.value:
        caller_file = callers_result.value[0].file_path
        chain_parts.append(os.path.basename(caller_file))

    chain_parts.append(best_sym.name)

    # Symbol → callees
    callees_result = graph.find_callees(best_sym.name, best_sym.file_path)
    if isinstance(callees_result, Ok):
        for ref in callees_result.value[: max_nodes - len(chain_parts)]:
            chain_parts.append(os.path.basename(ref.file_path))

    return " → ".join(chain_parts[:max_nodes])


def _read_source_lines(root_path: str, file_path: str) -> list[str] | None:
    """Read file lines from disk. Returns None on failure."""
    try:
        full = Path(root_path) / file_path
        if full.exists():
            return full.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        log.debug("read_source_failed", file_path=file_path, error=str(exc))
    return None


def _extract_function_source(
    lines: list[str], start_line: int, end_line: int | None, max_lines: int = 50
) -> tuple[str, int]:
    """Extract function source code from file lines.

    Returns (source_text, total_lines).
    """
    if start_line < 1 or start_line > len(lines):
        return "", 0

    idx = start_line - 1
    if end_line is not None and end_line <= len(lines):
        total = end_line - start_line + 1
        extracted = lines[idx : idx + total]
    else:
        # Fallback: read 30 lines
        total = min(30, len(lines) - idx)
        extracted = lines[idx : idx + total]

    if len(extracted) > max_lines:
        source = "\n".join(extracted[:max_lines])
        return source + f"\n# (truncated — full function is {total} lines)", total
    return "\n".join(extracted), total


# ---------------------------------------------------------------------------
# Existing handlers (with reasoning_guidance added)
# ---------------------------------------------------------------------------


async def handle_find_relevant(
    description: str,
    store: SymbolStore,
    graph: ImportGraph,
    task_parser: TaskParser,
    tracker: InterventionTracker,
    entry_points: list[str] | None = None,
    max_files: int = 5,
) -> dict[str, Any]:
    """Handle groundtruth_find_relevant tool call."""
    start = time.monotonic_ns()

    # Parse task description into symbol names
    parse_result = await task_parser.parse(description)
    if isinstance(parse_result, Err):
        return {"error": parse_result.error.message}
    symbol_names = parse_result.value

    # Look up symbols → entry files
    entry_files: list[str] = []
    entry_symbols: list[str] = []
    for name in symbol_names:
        find_result = store.find_symbol_by_name(name)
        if isinstance(find_result, Ok):
            for sym in find_result.value:
                if sym.file_path not in entry_files:
                    entry_files.append(sym.file_path)
                if name not in entry_symbols:
                    entry_symbols.append(name)

    # Merge with explicit entry_points
    if entry_points:
        for ep in entry_points:
            if ep not in entry_files:
                entry_files.append(ep)

    if not entry_files:
        return {
            "files": [],
            "entry_symbols": entry_symbols,
            "graph_depth": 0,
            "reasoning_guidance": (
                "No files matched the task description. "
                "Try calling groundtruth_find_relevant with explicit entry_points, "
                "or use groundtruth_symbols to explore specific files."
            ),
        }

    # BFS over import graph
    graph_result = graph.find_connected_files(entry_files, max_depth=3)
    if isinstance(graph_result, Err):
        return {"error": graph_result.error.message}

    nodes = graph_result.value

    # Build a set of files that contain entry symbols for boosting
    entry_symbol_files: set[str] = set()
    for name in entry_symbols:
        find_result = store.find_symbol_by_name(name)
        if isinstance(find_result, Ok):
            for sym in find_result.value:
                entry_symbol_files.add(sym.file_path)

    # Score and filter nodes by relevance
    _RELEVANCE_THRESHOLD = 0.2
    scored_nodes: list[tuple[Any, float]] = []
    for node in nodes:
        # Distance-based decay: 1.0, 0.5, 0.25, 0.125
        score = 1.0 / (2**node.distance)

        # Boost files that directly contain entry symbols
        if node.path in entry_symbol_files:
            score *= 1.5

        # Check symbol overlap with entry symbols
        has_overlap = any(sym in entry_symbols for sym in (node.symbols or []))

        # Filter out files at distance >= 1 with no symbol overlap
        if node.distance >= 1 and node.path not in entry_symbol_files and not has_overlap:
            continue

        if score >= _RELEVANCE_THRESHOLD:
            scored_nodes.append((node, score))

    # Sort by score descending
    scored_nodes.sort(key=lambda x: x[1], reverse=True)

    # Map to response format
    files: list[dict[str, Any]] = []
    for node, score in scored_nodes[:max_files]:
        if node.distance == 0:
            relevance = "high"
        elif node.distance == 1:
            relevance = "medium"
        else:
            relevance = "low"
        files.append(
            {
                "path": node.path,
                "relevance": relevance,
                "reason": f"distance {node.distance} from entry",
                "symbols_involved": node.symbols,
                "distance": node.distance,
                "score": round(score, 3),
            }
        )

    max_depth = max((n.distance for n in nodes), default=0)

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_find_relevant",
        phase="find_relevant",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    # Build dependency chain from top files
    dep_chain = _build_dependency_chain(store, graph, [f["path"] for f in files])

    top_paths = [f["path"] for f in files if f["relevance"] == "high"]
    guidance_parts = [
        f"Found {len(files)} relevant files.",
        f"Read the top-ranked files first: {', '.join(top_paths[:3])}." if top_paths else "",
        "Call groundtruth_explain on key functions before editing.",
        "Call groundtruth_impact before modifying high-usage symbols.",
    ]

    return {
        "files": files,
        "entry_symbols": entry_symbols,
        "graph_depth": max_depth,
        "dependency_chain": dep_chain,
        "reasoning_guidance": " ".join(p for p in guidance_parts if p),
    }


async def handle_brief(
    intent: str,
    briefing_engine: BriefingEngine,
    tracker: InterventionTracker,
    store: SymbolStore,
    graph: ImportGraph | None = None,
    target_file: str | None = None,
    adaptive: AdaptiveBriefing | None = None,
) -> dict[str, Any]:
    """Handle groundtruth_brief tool call."""
    start = time.monotonic_ns()

    result = await briefing_engine.generate_briefing(intent, target_file)
    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    if isinstance(result, Err):
        tracker.record(
            tool="groundtruth_brief",
            phase="brief",
            outcome="unfixable",
            latency_ms=elapsed_ms,
        )
        return {"error": result.error.message}

    br = result.value

    # Apply adaptive briefing enhancements if available
    if adaptive is not None and target_file is not None:
        enhanced = adaptive.enhance_briefing(br, target_file)
        if isinstance(enhanced, Ok):
            br = enhanced.value

    tracker.record(
        tool="groundtruth_brief",
        phase="brief",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    # Log briefing for grounding gap analysis
    symbol_names = [s.get("name", "") for s in br.relevant_symbols if s.get("name")]
    log_result = store.insert_briefing_log(
        timestamp=int(time.time()),
        intent=intent,
        briefing_text=br.briefing,
        briefing_symbols=symbol_names,
        target_file=target_file,
    )
    briefing_log_id: int | None = None
    if isinstance(log_result, Ok):
        briefing_log_id = log_result.value

    # Build key_symbols with impact labels
    key_symbols: list[dict[str, Any]] = []
    for s in br.relevant_symbols:
        sym_name = s.get("name", "")
        sym_info: dict[str, Any] = dict(s)
        if sym_name:
            find_result = store.find_symbol_by_name(sym_name)
            if isinstance(find_result, Ok) and find_result.value:
                uc = find_result.value[0].usage_count
                if uc >= 5:
                    sym_info["impact"] = "HIGH IMPACT"
                elif uc >= 1:
                    sym_info["impact"] = "MODERATE IMPACT"
                else:
                    sym_info["impact"] = "LOW IMPACT"
                sym_info["usage_count"] = uc
        key_symbols.append(sym_info)

    # Build dependency chain
    dep_chain = ""
    if graph is not None and target_file is not None:
        dep_chain = _build_dependency_chain(store, graph, [target_file])

    target_name = os.path.basename(target_file) if target_file else "the target file"
    guidance_parts = [
        f"Briefing for {target_name}.",
        "Check callers with groundtruth_impact before modifying high-impact symbols.",
        "Match existing patterns shown in the briefing.",
        "Call groundtruth_validate after editing to verify correctness.",
    ]

    response: dict[str, Any] = {
        "briefing": br.briefing,
        "relevant_symbols": br.relevant_symbols,
        "key_symbols": key_symbols,
        "warnings": br.warnings,
        "dependency_chain": dep_chain,
        "reasoning_guidance": " ".join(guidance_parts),
    }
    if briefing_log_id is not None:
        response["briefing_log_id"] = briefing_log_id

    return response


async def handle_validate(
    proposed_code: str,
    file_path: str,
    orchestrator: ValidationOrchestrator,
    tracker: InterventionTracker,
    store: SymbolStore,
    language: str | None = None,
    grounding_analyzer: GroundingGapAnalyzer | None = None,
    root_path: str | None = None,
    graph: ImportGraph | None = None,
) -> dict[str, Any]:
    """Handle groundtruth_validate tool call."""
    path_err = _check_path(file_path, root_path)
    if path_err is not None:
        return path_err
    start = time.monotonic_ns()

    # Use deterministic validation only (agents ARE the AI)
    result = await orchestrator.validate(proposed_code, file_path, language)
    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    if isinstance(result, Err):
        tracker.record(
            tool="groundtruth_validate",
            phase="validate",
            outcome="unfixable",
            latency_ms=elapsed_ms,
        )
        return {"error": result.error.message}

    vr = result.value

    # Determine outcome
    if vr.valid:
        outcome = "valid"
    elif vr.ai_used:
        outcome = "fixed_ai"
    else:
        outcome = "fixed_deterministic"

    error_types_list = [e.get("type", "unknown") for e in vr.errors]
    tracker.record(
        tool="groundtruth_validate",
        phase="validate",
        outcome=outcome,
        file_path=file_path,
        errors_found=len(vr.errors),
        error_types=error_types_list if error_types_list else None,
        ai_called=vr.ai_used,
        latency_ms=elapsed_ms,
    )

    # Auto-link to most recent briefing for this file and compute grounding gap
    if grounding_analyzer is not None:
        logs_result = store.get_briefing_logs_for_file(file_path)
        if isinstance(logs_result, Ok) and logs_result.value:
            recent_log = logs_result.value[0]  # most recent first
            if recent_log.subsequent_validation_id is None:
                # Get the validation intervention ID (latest intervention)
                try:
                    cursor = store.connection.execute(
                        """SELECT id FROM interventions
                           WHERE phase = 'validate' AND file_path = ?
                           ORDER BY timestamp DESC LIMIT 1""",
                        (file_path,),
                    )
                    row = cursor.fetchone()
                    if row:
                        val_id: int = row["id"]
                        store.link_briefing_to_validation(recent_log.id, val_id)
                        grounding_analyzer.compare_briefing_to_output(
                            recent_log, vr.errors, proposed_code
                        )
                except Exception as exc:
                    log.debug("grounding_gap_failed", error=str(exc))

    # Build reasoning_guidance
    if vr.errors:
        error_lines: list[str] = []
        for e in vr.errors:
            msg = e.get("message", "unknown error")
            suggestion = e.get("suggestion")
            if suggestion:
                fix = suggestion.get("fix", "")
                error_lines.append(f"- {msg} → {fix}")
            else:
                error_lines.append(f"- {msg}")
        guidance = (
            f"Found {len(vr.errors)} error(s):\n"
            + "\n".join(error_lines)
            + "\nFix all errors then re-validate with groundtruth_validate."
        )
    else:
        guidance = (
            "No structural errors found. "
            "Structural validity does not guarantee logical correctness. "
            "Check calling patterns with groundtruth_patterns, "
            "verify callers with groundtruth_impact, "
            "and confirm error handling is consistent."
        )

    # Build grounding record
    grounding = build_grounding_record(
        proposed_code, file_path, store, graph=graph, language=language
    )

    return {
        "valid": vr.valid,
        "errors": vr.errors,
        "ai_used": vr.ai_used,
        "latency_ms": vr.latency_ms,
        "reasoning_guidance": guidance,
        "grounding_record": grounding.to_dict(),
    }


async def handle_trace(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    direction: str = "both",
    max_depth: int = 3,
) -> dict[str, Any]:
    """Handle groundtruth_trace tool call."""
    start = time.monotonic_ns()
    _ = max_depth  # reserved for future deeper traversal

    # Look up symbol info
    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        return {"error": find_result.error.message}

    symbols = find_result.value
    if not symbols:
        elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
        tracker.record(
            tool="groundtruth_trace",
            phase="trace",
            outcome="valid",
            latency_ms=elapsed_ms,
        )
        return {"error": f"Symbol '{symbol}' not found in index"}

    sym = symbols[0]
    symbol_info: dict[str, Any] = {
        "name": sym.name,
        "file": sym.file_path,
        "signature": sym.signature,
    }

    callers: list[dict[str, Any]] = []
    callees: list[dict[str, Any]] = []

    if direction in ("callers", "both"):
        callers_result = graph.find_callers(symbol)
        if isinstance(callers_result, Ok):
            callers = [
                {"file": r.file_path, "line": r.line, "context": r.context}
                for r in callers_result.value
            ]

    if direction in ("callees", "both"):
        callees_result = graph.find_callees(symbol, sym.file_path)
        if isinstance(callees_result, Ok):
            callees = [{"symbol": "", "file": r.file_path} for r in callees_result.value]

    impact_result = graph.get_impact_radius(symbol)
    dependency_chain: list[str] = []
    impact_radius = 0
    if isinstance(impact_result, Ok):
        dependency_chain = impact_result.value.impacted_files
        impact_radius = impact_result.value.impact_radius

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_trace",
        phase="trace",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    guidance = (
        f"Symbol '{symbol}' has {len(callers)} caller(s) and "
        f"{len(callees)} callee(s). Impact radius: {impact_radius} file(s). "
        "Review callers before changing this symbol's signature or behavior."
    )

    return {
        "symbol": symbol_info,
        "callers": callers,
        "callees": callees,
        "dependency_chain": dependency_chain,
        "impact_radius": impact_radius,
        "reasoning_guidance": guidance,
    }


async def handle_status(
    store: SymbolStore,
    tracker: InterventionTracker,
) -> dict[str, Any]:
    """Handle groundtruth_status tool call."""
    store_stats_result = store.get_stats()
    if isinstance(store_stats_result, Err):
        return {"error": store_stats_result.error.message}

    raw = store_stats_result.value

    def _int(val: object) -> int:
        if isinstance(val, int):
            return val
        return int(str(val)) if val is not None else 0

    tracker_stats_result = tracker.get_stats()
    interventions: dict[str, Any] = {}
    if isinstance(tracker_stats_result, Ok):
        ts = tracker_stats_result.value
        interventions = {
            "total": ts.total,
            "hallucinations_caught": ts.hallucinations_caught,
            "ai_calls": ts.ai_calls,
            "tokens_used": ts.tokens_used,
        }

    # Get distinct languages
    try:
        cursor = store.connection.execute("SELECT DISTINCT language FROM symbols")
        languages: list[str] = [row["language"] for row in cursor.fetchall()]
    except Exception as exc:
        log.debug("languages_query_failed", error=str(exc))
        languages = []

    symbols_count = _int(raw.get("symbols_count", 0))
    return {
        "indexed": symbols_count > 0,
        "languages": languages,
        "symbols_count": symbols_count,
        "files_count": _int(raw.get("files_count", 0)),
        "refs_count": _int(raw.get("refs_count", 0)),
        "interventions": interventions,
        "reasoning_guidance": (
            f"Index contains {symbols_count} symbols across "
            f"{_int(raw.get('files_count', 0))} files. "
            "Use groundtruth_orient for full project overview."
        ),
    }


async def handle_dead_code(
    store: SymbolStore,
    tracker: InterventionTracker,
) -> dict[str, Any]:
    """Handle groundtruth_dead_code tool call."""
    start = time.monotonic_ns()

    result = store.get_dead_code()
    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    if isinstance(result, Err):
        return {"error": result.error.message}

    dead = result.value
    tracker.record(
        tool="groundtruth_dead_code",
        phase="dead_code",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    return {
        "dead_symbols": [
            {
                "name": s.name,
                "file": s.file_path,
                "kind": s.kind,
                "last_indexed": s.last_indexed_at,
            }
            for s in dead
        ],
        "total": len(dead),
        "note": "These exported symbols have zero references anywhere in the codebase.",
        "reasoning_guidance": (
            f"Found {len(dead)} dead symbol(s). "
            "Verify each is truly unused before removing — "
            "some may be used via dynamic imports or external consumers."
        ),
    }


async def handle_unused_packages(
    store: SymbolStore,
    tracker: InterventionTracker,
) -> dict[str, Any]:
    """Handle groundtruth_unused_packages tool call."""
    start = time.monotonic_ns()

    result = store.get_unused_packages()
    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    if isinstance(result, Err):
        return {"error": result.error.message}

    unused = result.value
    tracker.record(
        tool="groundtruth_unused_packages",
        phase="unused_packages",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    return {
        "unused_packages": [
            {
                "name": p.name,
                "version": p.version,
                "package_manager": p.package_manager,
            }
            for p in unused
        ],
        "total": len(unused),
        "reasoning_guidance": (
            f"Found {len(unused)} unused package(s). "
            "Some may be used as plugins or CLI tools — "
            "verify before removing from dependencies."
        ),
    }


async def handle_hotspots(
    store: SymbolStore,
    tracker: InterventionTracker,
    limit: int = 20,
) -> dict[str, Any]:
    """Handle groundtruth_hotspots tool call."""
    start = time.monotonic_ns()

    result = store.get_hotspots(limit)
    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    if isinstance(result, Err):
        return {"error": result.error.message}

    hotspots = result.value
    tracker.record(
        tool="groundtruth_hotspots",
        phase="hotspots",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    return {
        "hotspots": [
            {
                "name": s.name,
                "file": s.file_path,
                "usage_count": s.usage_count,
                "kind": s.kind,
            }
            for s in hotspots
        ],
        "note": "High-usage symbols have the biggest blast radius if hallucinated.",
        "reasoning_guidance": (
            f"Showing top {len(hotspots)} hotspot(s). "
            "Call groundtruth_impact before modifying any of these symbols."
        ),
    }


async def handle_orient(
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    risk_scorer: RiskScorer,
    root_path: str,
) -> dict[str, Any]:
    """Handle groundtruth_orient tool call — codebase orientation."""
    start = time.monotonic_ns()

    # Stats
    stats_result = store.get_stats()
    stats: dict[str, object] = {}
    if isinstance(stats_result, Ok):
        stats = stats_result.value

    def _int(val: object) -> int:
        if isinstance(val, int):
            return val
        return int(str(val)) if val is not None else 0

    # Top-level structure
    top_level_dirs: list[str] = []
    config_files: list[str] = []
    test_dirs: list[str] = []
    config_names = {
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Makefile",
        "tox.ini",
        "setup.cfg",
    }
    test_dir_names = {"test", "tests", "__tests__", "spec"}

    try:
        for entry in os.listdir(root_path):
            full = os.path.join(root_path, entry)
            if os.path.isdir(full):
                top_level_dirs.append(entry)
                if entry.lower() in test_dir_names:
                    test_dirs.append(entry)
            elif entry in config_names:
                config_files.append(entry)
    except OSError as exc:
        log.debug("listdir_failed", root_path=root_path, error=str(exc))

    # Build/test commands from manifests
    build_commands: dict[str, str] = {}
    test_command = ""
    pyproject_path = os.path.join(root_path, "pyproject.toml")
    if os.path.exists(pyproject_path):
        try:
            content = Path(pyproject_path).read_text(encoding="utf-8")
            if "[project.scripts]" in content:
                build_commands["run"] = "See [project.scripts] in pyproject.toml"
            test_command = "pytest"
        except OSError as exc:
            log.debug("pyproject_read_failed", error=str(exc))

    pkg_json_path = os.path.join(root_path, "package.json")
    if os.path.exists(pkg_json_path):
        try:
            import json

            pkg = json.loads(Path(pkg_json_path).read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                build_commands["test"] = scripts["test"]
                test_command = scripts["test"]
            if "build" in scripts:
                build_commands["build"] = scripts["build"]
        except (OSError, ValueError) as exc:
            log.debug("package_json_read_failed", error=str(exc))

    # Entry points
    entry_result = store.get_entry_point_files(5)
    entry_points: list[str] = []
    if isinstance(entry_result, Ok):
        entry_points = entry_result.value

    # Top modules
    top_dirs_result = store.get_top_directories(5)
    top_modules: list[dict[str, Any]] = []
    if isinstance(top_dirs_result, Ok):
        top_modules = top_dirs_result.value

    # Risk summary
    risk_summary: list[dict[str, Any]] = []
    risk_result = risk_scorer.score_codebase(limit=5)
    if isinstance(risk_result, Ok):
        for rs in risk_result.value:
            top_factor = ""
            if rs.factors:
                top_factor = max(rs.factors, key=lambda k: rs.factors[k])
            risk_summary.append(
                {
                    "file": rs.file_path,
                    "risk": round(rs.overall_risk, 3),
                    "top_factor": top_factor,
                }
            )

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_orient",
        phase="orient",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    guidance_parts = [
        "Next: call groundtruth_find_relevant with your task description.",
        "Do not edit files until you understand connections.",
    ]
    if test_command:
        guidance_parts.append(f"Test command: {test_command}")

    return {
        "project": {
            "symbols_count": _int(stats.get("symbols_count", 0)),
            "files_count": _int(stats.get("files_count", 0)),
            "refs_count": _int(stats.get("refs_count", 0)),
        },
        "structure": {
            "top_level_dirs": sorted(top_level_dirs),
            "config_files": sorted(config_files),
            "test_dirs": test_dirs,
        },
        "build_commands": build_commands,
        "entry_points": entry_points,
        "top_modules": top_modules,
        "risk_summary": risk_summary,
        "reasoning_guidance": " ".join(guidance_parts),
    }


async def handle_checkpoint(
    store: SymbolStore,
    tracker: InterventionTracker,
    risk_scorer: RiskScorer,
) -> dict[str, Any]:
    """Handle groundtruth_checkpoint tool call — session progress summary."""
    start = time.monotonic_ns()

    summary = tracker.get_session_summary()

    # Risk assessment for touched files
    file_risks: list[dict[str, Any]] = []
    for fp in summary.files_referenced:
        risk_result = risk_scorer.score_file(fp)
        if isinstance(risk_result, Ok):
            rs = risk_result.value
            file_risks.append(
                {
                    "file": rs.file_path,
                    "risk": round(rs.overall_risk, 3),
                }
            )

    # Generate recommendations
    recommendations: list[str] = []
    has_unresolved = any(
        entry.get("outcome") not in ("valid",) and entry.get("phase") == "validate"
        for entry in tracker._session_log
    )
    if has_unresolved:
        recommendations.append("Run groundtruth_validate on files with unresolved errors.")

    high_risk_files = [fr for fr in file_risks if fr["risk"] >= 0.45]
    if high_risk_files:
        top_risk = high_risk_files[0]["file"]
        recommendations.append(
            f"Consider groundtruth_brief before modifying {top_risk} (high risk)."
        )

    has_briefings = summary.tools_called.get("groundtruth_brief", 0) > 0
    if not has_briefings and summary.total_calls > 0:
        recommendations.append("Use groundtruth_brief for proactive context before code changes.")

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_checkpoint",
        phase="checkpoint",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    unresolved_count = sum(
        1
        for e in tracker._session_log
        if e.get("phase") == "validate" and e.get("outcome") != "valid"
    )

    guidance = (
        f"Session: {summary.total_calls} tool calls, "
        f"{summary.errors_found} error(s) found, "
        f"{unresolved_count} unresolved. "
        "Run tests to verify changes."
    )

    return {
        "session": {
            "total_calls": summary.total_calls,
            "tools_called": summary.tools_called,
            "files_referenced": summary.files_referenced,
            "validations_run": summary.validations_run,
            "errors_found": summary.errors_found,
            "errors_fixed": summary.errors_fixed,
        },
        "file_risks": file_risks,
        "recommendations": recommendations,
        "reasoning_guidance": guidance,
    }


async def handle_symbols(
    file_path: str,
    store: SymbolStore,
    tracker: InterventionTracker,
    root_path: str | None = None,
) -> dict[str, Any]:
    """Handle groundtruth_symbols tool call — file symbol listing."""
    path_err = _check_path(file_path, root_path)
    if path_err is not None:
        return path_err
    start = time.monotonic_ns()

    symbols_result = store.get_symbols_in_file(file_path)
    if isinstance(symbols_result, Err):
        return {"error": symbols_result.error.message}

    file_symbols = symbols_result.value
    symbol_list = sorted(
        [
            {
                "name": s.name,
                "kind": s.kind,
                "signature": s.signature,
                "is_exported": s.is_exported,
                "line_number": s.line_number,
                "usage_count": s.usage_count,
            }
            for s in file_symbols
        ],
        key=lambda x: x.get("line_number") or 0,
    )

    # imports_from: what does this file import?
    imports_from: list[str] = []
    refs_result = store.get_imports_for_file(file_path)
    if isinstance(refs_result, Ok):
        seen_files: set[str] = set()
        for ref in refs_result.value:
            sym_result = store.get_symbol_by_id(ref.symbol_id)
            if isinstance(sym_result, Ok) and sym_result.value is not None:
                dep = sym_result.value.file_path
                if dep != file_path and dep not in seen_files:
                    seen_files.add(dep)
                    imports_from.append(dep)

    # imported_by: who imports from this file?
    importers_result = store.get_importers_of_file(file_path)
    imported_by: list[str] = []
    if isinstance(importers_result, Ok):
        imported_by = [p for p in importers_result.value if p != file_path]

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_symbols",
        phase="symbols",
        outcome="valid",
        file_path=file_path,
        latency_ms=elapsed_ms,
    )

    guidance = (
        f"File has {len(symbol_list)} symbol(s). "
        "Use exact names and match signatures when importing. "
        "Call groundtruth_explain for details on specific functions."
    )

    return {
        "file_path": file_path,
        "symbols": symbol_list,
        "symbol_count": len(symbol_list),
        "imports_from": sorted(imports_from),
        "imported_by": sorted(imported_by),
        "reasoning_guidance": guidance,
    }


async def handle_context(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Handle groundtruth_context tool call — symbol usage context."""
    start = time.monotonic_ns()

    # Find symbol info
    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        return {"error": find_result.error.message}

    symbols = find_result.value
    if not symbols:
        elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
        tracker.record(
            tool="groundtruth_context",
            phase="context",
            outcome="valid",
            latency_ms=elapsed_ms,
        )
        return {"error": f"Symbol '{symbol}' not found in index"}

    sym = symbols[0]
    symbol_info: dict[str, Any] = {
        "name": sym.name,
        "file": sym.file_path,
        "kind": sym.kind,
        "signature": sym.signature,
        "line_number": sym.line_number,
    }

    # Find callers
    callers_result = graph.find_callers(symbol)
    usages: list[dict[str, Any]] = []
    if isinstance(callers_result, Ok):
        for ref in callers_result.value[:limit]:
            usage: dict[str, Any] = {
                "file": ref.file_path,
                "line": ref.line,
            }

            # Try to read context from disk
            if ref.line is not None:
                try:
                    full_path = Path(root_path) / ref.file_path
                    if full_path.exists():
                        lines = full_path.read_text(encoding="utf-8").splitlines()
                        line_idx = ref.line - 1
                        snippet_lines: list[str] = []
                        if 0 <= line_idx - 1 < len(lines):
                            snippet_lines.append(lines[line_idx - 1])
                        if 0 <= line_idx < len(lines):
                            snippet_lines.append(">>> " + lines[line_idx])
                        if 0 <= line_idx + 1 < len(lines):
                            snippet_lines.append(lines[line_idx + 1])
                        usage["context"] = "\n".join(snippet_lines)
                except (OSError, UnicodeDecodeError) as exc:
                    log.debug("context_snippet_failed", file=ref.file_path, error=str(exc))

            usages.append(usage)

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_context",
        phase="context",
        outcome="valid",
        latency_ms=elapsed_ms,
    )

    guidance = (
        f"Symbol '{symbol}' is used in {len(usages)} location(s). "
        "Follow calling patterns shown — match keyword/positional style."
    )

    return {
        "symbol": symbol_info,
        "usages": usages,
        "total_usages": len(usages),
        "reasoning_guidance": guidance,
    }


# ---------------------------------------------------------------------------
# New handlers: explain, impact, patterns
# ---------------------------------------------------------------------------


async def handle_explain(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Handle groundtruth_explain — deep dive into a single symbol."""
    start = time.monotonic_ns()

    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        return {"error": find_result.error.message}

    symbols = find_result.value
    if not symbols:
        return {"error": f"Symbol '{symbol}' not found in index"}

    # If file_path given, prefer symbol from that file
    sym = symbols[0]
    if file_path:
        for s in symbols:
            if paths_equal(s.file_path, file_path):
                sym = s
                break

    symbol_info: dict[str, Any] = {
        "name": sym.name,
        "file": sym.file_path,
        "kind": sym.kind,
        "signature": sym.signature,
        "documentation": sym.documentation,
        "line_range": f"{sym.line_number}-{sym.end_line}" if sym.line_number else None,
    }

    # Read source code from disk
    source_code = ""
    total_lines = 0
    file_lines = _read_source_lines(root_path, sym.file_path)
    if file_lines and sym.line_number:
        source_code, total_lines = _extract_function_source(
            file_lines, sym.line_number, sym.end_line
        )

    # Build dependency chain
    dep_chain = _build_dependency_chain(store, graph, [sym.file_path])

    # Calls out to (callees)
    calls_out: list[dict[str, Any]] = []
    callees_result = graph.find_callees(sym.name, sym.file_path)
    if isinstance(callees_result, Ok):
        for ref in callees_result.value:
            calls_out.append({"file": ref.file_path, "line": ref.line})

    # Called by (callers) with impact labels
    called_by: list[dict[str, Any]] = []
    callers_result = graph.find_callers(sym.name)
    if isinstance(callers_result, Ok):
        for ref in callers_result.value:
            caller_info: dict[str, Any] = {"file": ref.file_path, "line": ref.line}
            # Determine impact from usage count of the calling file's symbols
            file_syms = store.get_symbols_in_file(ref.file_path)
            if isinstance(file_syms, Ok) and file_syms.value:
                max_usage = max(s.usage_count for s in file_syms.value)
                if max_usage >= 5:
                    caller_info["impact"] = "HIGH IMPACT"
                elif max_usage >= 1:
                    caller_info["impact"] = "MODERATE IMPACT"
                else:
                    caller_info["impact"] = "LOW IMPACT"
            called_by.append(caller_info)

    # Side effects detection
    side_effects: list[str] = []
    if source_code:
        write_ops = [
            ".save(",
            ".delete(",
            ".insert(",
            ".update(",
            ".execute(",
            ".commit(",
            ".send(",
            ".emit(",
            ".publish(",
            ".setex(",
            ".put(",
            ".post(",
            ".patch(",
        ]
        for op in write_ops:
            if op in source_code:
                side_effects.append(f"write operation: {op.strip('(.')}")
        if re.search(r"self\.\w+\s*=", source_code) or re.search(r"this\.\w+\s*=", source_code):
            side_effects.append("state mutation")
        if re.search(r"open\(|os\.write|fs\.write", source_code):
            side_effects.append("file I/O")

    # Error handling detection
    error_handling: dict[str, bool] = {
        "has_try_catch": bool(re.search(r"\btry\b|\bexcept\b|\bcatch\b|\brecover\b", source_code))
        if source_code
        else False,
        "raises_errors": bool(re.search(r"\braise\b|\bthrow\b", source_code))
        if source_code
        else False,
    }

    complexity: dict[str, int] = {
        "lines": total_lines,
        "external_calls": len(calls_out),
        "side_effects": len(side_effects),
        "callers": len(called_by),
    }

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_explain",
        phase="explain",
        outcome="valid",
        file_path=sym.file_path,
        latency_ms=elapsed_ms,
    )

    guidance_parts = [
        f"Symbol '{sym.name}' has {len(side_effects)} side effect(s).",
    ]
    if not error_handling["has_try_catch"]:
        guidance_parts.append("No error handling detected — consider adding try/except.")
    if error_handling["raises_errors"]:
        guidance_parts.append("This function raises errors — callers must handle them.")
    guidance_parts.append(f"{len(called_by)} caller(s) depend on this symbol.")

    return {
        "symbol": symbol_info,
        "source_code": source_code,
        "dependency_chain": dep_chain,
        "calls_out_to": calls_out,
        "called_by": called_by,
        "side_effects_detected": side_effects,
        "error_handling": error_handling,
        "complexity": complexity,
        "reasoning_guidance": " ".join(guidance_parts),
    }


async def handle_impact(
    symbol: str,
    store: SymbolStore,
    graph: ImportGraph,
    tracker: InterventionTracker,
    root_path: str,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Handle groundtruth_impact — assess blast radius of modifying a symbol."""
    start = time.monotonic_ns()

    find_result = store.find_symbol_by_name(symbol)
    if isinstance(find_result, Err):
        return {"error": find_result.error.message}

    symbols = find_result.value
    if not symbols:
        return {"error": f"Symbol '{symbol}' not found in index"}

    sym = symbols[0]
    symbol_info: dict[str, Any] = {
        "name": sym.name,
        "file": sym.file_path,
        "signature": sym.signature,
    }

    # Direct callers with break_risk and call_style
    direct_callers: list[dict[str, Any]] = []
    callers_result = graph.find_callers(sym.name)
    direct_caller_files: set[str] = set()
    if isinstance(callers_result, Ok):
        for ref in callers_result.value:
            direct_caller_files.add(ref.file_path)
            caller_info: dict[str, Any] = {
                "file": ref.file_path,
                "line": ref.line,
            }

            # Read usage line from disk for call_style detection
            usage_snippet = ""
            if ref.line is not None:
                file_lines = _read_source_lines(root_path, ref.file_path)
                if file_lines and 0 < ref.line <= len(file_lines):
                    usage_snippet = file_lines[ref.line - 1]
                    caller_info["usage"] = usage_snippet.strip()

            # Call style detection
            if f"{sym.name}(" in usage_snippet:
                if "=" in usage_snippet.split(f"{sym.name}(", 1)[-1].split(")", 1)[0]:
                    caller_info["call_style"] = "keyword"
                    caller_info["break_risk"] = "MODERATE"
                else:
                    caller_info["call_style"] = "positional"
                    caller_info["break_risk"] = "HIGH"
            elif sym.name in usage_snippet:
                caller_info["call_style"] = "reference"
                caller_info["break_risk"] = "LOW"
            else:
                caller_info["call_style"] = "unknown"
                caller_info["break_risk"] = "MODERATE"

            direct_callers.append(caller_info)

    # Indirect dependents via impact radius
    impact_result = graph.get_impact_radius(sym.name)
    indirect_files: list[str] = []
    if isinstance(impact_result, Ok):
        indirect_files = [
            f for f in impact_result.value.impacted_files if f not in direct_caller_files
        ]

    total_at_risk = len(direct_caller_files) + len(indirect_files)
    if total_at_risk >= 5:
        impact_level = "HIGH"
    elif total_at_risk >= 2:
        impact_level = "MODERATE"
    else:
        impact_level = "LOW"

    impact_summary: dict[str, Any] = {
        "direct_files": len(direct_caller_files),
        "indirect_files": len(indirect_files),
        "total_files_at_risk": total_at_risk,
        "impact_level": impact_level,
    }

    safe_changes = [
        "Add optional parameter with default value",
        "Change internal logic without altering inputs/outputs",
        "Add error handling or logging",
        "Improve performance without API change",
    ]
    unsafe_changes = [
        "Add required parameter",
        "Remove parameter",
        "Change return type",
        "Rename the symbol",
    ]

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_impact",
        phase="impact",
        outcome="valid",
        file_path=sym.file_path,
        latency_ms=elapsed_ms,
    )

    high_risk_callers = [c for c in direct_callers if c.get("break_risk") == "HIGH"]
    guidance_parts = [
        f"Impact level: {impact_level}.",
        f"{len(direct_callers)} direct caller(s), {len(indirect_files)} indirect dependent(s).",
    ]
    if high_risk_callers:
        guidance_parts.append(
            f"{len(high_risk_callers)} caller(s) use positional args — "
            "adding/reordering params will break them."
        )
    guidance_parts.append(
        "1) Can you make this change without altering the signature? "
        "2) If not, can you add an optional param instead? "
        "3) Update all callers if signature must change."
    )

    return {
        "symbol": symbol_info,
        "direct_callers": direct_callers,
        "indirect_dependents": indirect_files,
        "impact_summary": impact_summary,
        "safe_changes": safe_changes,
        "unsafe_changes": unsafe_changes,
        "reasoning_guidance": " ".join(guidance_parts),
    }


async def handle_patterns(
    file_path: str,
    store: SymbolStore,
    tracker: InterventionTracker,
    root_path: str,
) -> dict[str, Any]:
    """Handle groundtruth_patterns — detect conventions in sibling files."""
    path_err = _check_path(file_path, root_path)
    if path_err is not None:
        return path_err
    start = time.monotonic_ns()

    directory = os.path.dirname(file_path)

    siblings_result = store.get_sibling_files(file_path)
    if isinstance(siblings_result, Err):
        return {"error": siblings_result.error.message}

    sibling_files = siblings_result.value

    # Sort by usage_count desc, take top 5
    sibling_scores: list[tuple[str, int]] = []
    for sf in sibling_files:
        syms_result = store.get_symbols_in_file(sf)
        total_usage = 0
        if isinstance(syms_result, Ok):
            total_usage = sum(s.usage_count for s in syms_result.value)
        sibling_scores.append((sf, total_usage))
    sibling_scores.sort(key=lambda x: -x[1])
    top_siblings = [s[0] for s in sibling_scores[:5]]

    # Read sibling files (up to 100 lines each)
    sibling_contents: dict[str, list[str]] = {}
    for sf in top_siblings:
        lines = _read_source_lines(root_path, sf)
        if lines:
            sibling_contents[sf] = lines[:100]

    total_siblings = len(sibling_contents)
    threshold = 0.6

    patterns_detected: list[dict[str, Any]] = []

    if total_siblings > 0:
        # Error handling pattern
        error_count = sum(
            1
            for lines in sibling_contents.values()
            if any(re.search(r"\btry\b", ln) for ln in lines)
            and any(re.search(r"\bexcept\b|\bcatch\b", ln) for ln in lines)
        )
        if error_count / total_siblings > threshold:
            example = _find_pattern_example(sibling_contents, r"\btry\b")
            patterns_detected.append(
                {
                    "pattern_name": "error_handling",
                    "description": "try/except or try/catch blocks",
                    "frequency": f"{error_count}/{total_siblings} files",
                    "example": example,
                }
            )

        # Logging pattern
        log_count = sum(
            1
            for lines in sibling_contents.values()
            if any(re.search(r"logger\.|logging\.|log\.|console\.log", ln) for ln in lines)
        )
        if log_count / total_siblings > threshold:
            example = _find_pattern_example(sibling_contents, r"logger\.|logging\.|log\.")
            patterns_detected.append(
                {
                    "pattern_name": "logging",
                    "description": "Structured logging calls",
                    "frequency": f"{log_count}/{total_siblings} files",
                    "example": example,
                }
            )

        # Decorator pattern
        decorator_count = sum(
            1
            for lines in sibling_contents.values()
            if any(re.match(r"\s*@\w+", ln) for ln in lines)
        )
        if decorator_count / total_siblings > threshold:
            example = _find_pattern_example(sibling_contents, r"^\s*@\w+")
            patterns_detected.append(
                {
                    "pattern_name": "decorators",
                    "description": "Decorator usage on functions/classes",
                    "frequency": f"{decorator_count}/{total_siblings} files",
                    "example": example,
                }
            )

        # Input validation pattern
        validation_count = sum(
            1
            for lines in sibling_contents.values()
            if any(re.search(r"\.validate\(|\.is_valid\(\)|\.parse\(", ln) for ln in lines)
        )
        if validation_count / total_siblings > threshold:
            example = _find_pattern_example(
                sibling_contents, r"\.validate\(|\.is_valid\(\)|\.parse\("
            )
            patterns_detected.append(
                {
                    "pattern_name": "input_validation",
                    "description": "Input validation calls",
                    "frequency": f"{validation_count}/{total_siblings} files",
                    "example": example,
                }
            )

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
    tracker.record(
        tool="groundtruth_patterns",
        phase="patterns",
        outcome="valid",
        file_path=file_path,
        latency_ms=elapsed_ms,
    )

    if patterns_detected:
        conventions = [
            f"{i + 1}. {p['pattern_name']} ({p['frequency']})"
            for i, p in enumerate(patterns_detected)
        ]
        guidance = (
            "Detected conventions in this directory:\n"
            + "\n".join(conventions)
            + "\nFollow these patterns in your code."
        )
    else:
        guidance = (
            f"No strong conventions detected among {total_siblings} sibling file(s). "
            "Follow project-wide conventions from groundtruth_orient."
        )

    return {
        "file": file_path,
        "directory": directory,
        "sibling_files_analyzed": total_siblings,
        "patterns_detected": patterns_detected,
        "reasoning_guidance": guidance,
    }


def _find_pattern_example(sibling_contents: dict[str, list[str]], pattern: str) -> str:
    """Find a 4-6 line example of a pattern from sibling files."""
    for lines in sibling_contents.values():
        for i, line in enumerate(lines):
            if re.search(pattern, line):
                start_idx = max(0, i - 1)
                end_idx = min(len(lines), i + 5)
                return "\n".join(lines[start_idx:end_idx])
    return ""


# ---------------------------------------------------------------------------
# Meta-tool: groundtruth_do
# ---------------------------------------------------------------------------

_OPERATION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("explain", re.compile(r"\b(how|what|explain|why|describe|understand)\b", re.IGNORECASE)),
    ("validate", re.compile(r"\b(validate|check|lint|verify|correct)\b", re.IGNORECASE)),
    (
        "trace",
        re.compile(r"\b(trace|who\s+calls|callers|callees|usage|references)\b", re.IGNORECASE),
    ),
    ("find", re.compile(r"\b(find|where|locate|search|which\s+files?)\b", re.IGNORECASE)),
]


def _detect_operation(query: str) -> str:
    """Detect operation from query keywords. Returns operation name."""
    for op, pattern in _OPERATION_PATTERNS:
        if pattern.search(query):
            return op
    # Default: find → brief
    return "explain"


# ---------------------------------------------------------------------------
# Pipeline infrastructure for groundtruth_do
# ---------------------------------------------------------------------------

# Depth → default pipeline mapping
_DEPTH_PIPELINES: dict[str, list[str]] = {
    "quick": ["find"],
    "standard": ["find", "brief"],
    "deep": ["find", "brief", "validate", "trace"],
}

# Intent can override depth defaults
_INTENT_PIPELINE_OVERRIDES: dict[str, list[str]] = {
    "validate": ["find", "validate"],
    "trace": ["find", "trace"],
    "find": ["find"],
    "explain": ["find", "brief"],
    "brief": ["find", "brief"],
}

# All valid step names
_VALID_STEPS: set[str] = {
    "find",
    "brief",
    "validate",
    "trace",
    "deps",
    "stats",
    "hotspots",
    "dead_code",
    "unused_packages",
    "explain",
    "impact",
    "symbols",
    "context",
    "patterns",
}


def _resolve_pipeline(
    query: str | None,
    steps: list[dict[str, Any]] | None,
    depth: str,
    operation: str | None,
) -> tuple[list[dict[str, Any]], str]:
    """Resolve the pipeline to execute.

    Returns (step_list, intent) where each step is {"tool": name, ...extra_args}.
    """
    # Explicit steps mode
    if steps is not None:
        intent = "explicit"
        return steps, intent

    # Operation backward compat → treat as query mode
    effective_query = query or ""
    if operation:
        intent = operation
        pipeline_names = _INTENT_PIPELINE_OVERRIDES.get(
            operation, _DEPTH_PIPELINES.get(depth, ["find", "brief"])
        )
    else:
        intent = _detect_operation(effective_query)
        # Intent overrides depth defaults, unless depth is explicitly "deep"
        if depth == "deep":
            pipeline_names = _DEPTH_PIPELINES["deep"]
        elif intent in _INTENT_PIPELINE_OVERRIDES:
            pipeline_names = _INTENT_PIPELINE_OVERRIDES[intent]
        else:
            pipeline_names = _DEPTH_PIPELINES.get(depth, ["find", "brief"])

    return [{"tool": name} for name in pipeline_names], intent


def _forward_results(
    pipeline_results: dict[str, dict[str, Any]],
    query: str | None,
) -> dict[str, Any]:
    """Extract forwarding values from previous step results."""
    forwarded: dict[str, Any] = {}

    # Forward query to brief.intent and find.description
    if query:
        forwarded["intent"] = query
        forwarded["description"] = query

    # Forward from find results
    find_data = pipeline_results.get("find")
    if find_data and "error" not in find_data:
        files = find_data.get("files", [])
        if files:
            first_file = files[0]
            if isinstance(first_file, dict):
                forwarded["target_file"] = first_file.get("path")
        entry_syms = find_data.get("entry_symbols", [])
        if entry_syms:
            forwarded["symbol"] = entry_syms[0]

    return forwarded


def _apply_scope(result: dict[str, Any], scope: str, step_name: str) -> dict[str, Any]:
    """Filter file paths in results by scope prefix."""
    if not scope:
        return result

    norm_scope = scope.replace("\\", "/")

    def _matches(path: str) -> bool:
        return path.replace("\\", "/").startswith(norm_scope)

    # Clone to avoid mutation
    filtered = dict(result)

    if step_name == "find":
        files = filtered.get("files", [])
        filtered["files"] = [
            f for f in files if isinstance(f, dict) and _matches(f.get("path", ""))
        ]
    elif step_name == "trace":
        for key in ("callers", "callees"):
            items = filtered.get(key, [])
            filtered[key] = [
                c for c in items if isinstance(c, dict) and _matches(c.get("file", ""))
            ]
    elif step_name in ("symbols", "dead_code"):
        for key in ("symbols", "dead_symbols"):
            items = filtered.get(key, [])
            if items:
                filtered[key] = [
                    s
                    for s in items
                    if isinstance(s, dict) and _matches(str(s.get("file", s.get("file_path", ""))))
                ]
    elif step_name == "hotspots":
        items = filtered.get("hotspots", [])
        filtered["hotspots"] = [
            h for h in items if isinstance(h, dict) and _matches(h.get("file", ""))
        ]

    return filtered


async def _handle_deps_combined(
    store: SymbolStore,
    tracker: InterventionTracker,
) -> dict[str, Any]:
    """Run dead_code + unused_packages and merge results."""
    dead_result = await handle_dead_code(store=store, tracker=tracker)
    unused_result = await handle_unused_packages(store=store, tracker=tracker)
    return {
        "dead_code": dead_result,
        "unused_packages": unused_result,
    }


def _summarize_step(step_name: str, data: dict[str, Any]) -> str | None:
    """Generate a summary fragment for a single step result."""
    if "error" in data:
        return f"{step_name} failed"

    if step_name == "find":
        files = data.get("files", [])
        return f"found {len(files)} relevant file(s)"
    elif step_name == "brief":
        return "briefing generated"
    elif step_name == "validate":
        errs = data.get("errors", [])
        if errs:
            return f"{len(errs)} validation issue(s)"
        return "code validated OK"
    elif step_name == "trace":
        callers = data.get("callers", [])
        return f"traced ({len(callers)} caller(s))"
    elif step_name == "deps":
        dead = data.get("dead_code", {}).get("dead_symbols", [])
        unused = data.get("unused_packages", {}).get("unused_packages", [])
        return f"{len(dead)} dead symbol(s), {len(unused)} unused package(s)"
    elif step_name == "stats":
        return "status retrieved"
    elif step_name == "hotspots":
        spots = data.get("hotspots", [])
        return f"{len(spots)} hotspot(s)"
    elif step_name == "explain":
        return "explanation generated"
    elif step_name == "impact":
        radius = data.get("impact_radius", "?")
        return f"impact radius: {radius}"
    elif step_name == "symbols":
        syms = data.get("symbols", [])
        return f"{len(syms)} symbol(s)"
    elif step_name == "dead_code":
        dead = data.get("dead_symbols", [])
        return f"{len(dead)} dead symbol(s)"
    elif step_name == "unused_packages":
        unused = data.get("unused_packages", [])
        return f"{len(unused)} unused package(s)"
    elif step_name == "context":
        usages = data.get("usages", [])
        return f"{len(usages)} usage(s)"
    elif step_name == "patterns":
        return "patterns detected"
    return None


def _synthesize_summary(
    pipeline: list[str],
    results: dict[str, dict[str, Any]],
) -> str:
    """Build a summary string from per-step templates."""
    parts: list[str] = []
    for step_name in pipeline:
        data = results.get(step_name, {})
        fragment = _summarize_step(step_name, data)
        if fragment:
            parts.append(fragment)
    if not parts:
        return "Pipeline completed with no results."
    return "; ".join(parts) + "."


async def handle_do(
    query: str | None,
    store: SymbolStore,
    graph: ImportGraph,
    task_parser: TaskParser,
    briefing_engine: BriefingEngine,
    orchestrator: ValidationOrchestrator,
    tracker: InterventionTracker,
    risk_scorer: RiskScorer,
    adaptive: AdaptiveBriefing | None = None,
    grounding_analyzer: GroundingGapAnalyzer | None = None,
    root_path: str | None = None,
    operation: str | None = None,
    file_path: str | None = None,
    code: str | None = None,
    symbol: str | None = None,
    depth: str = "standard",
    steps: list[dict[str, Any]] | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """Single entry point that routes to find/brief/validate/trace/explain pipelines.

    Supports two modes:
    - Smart auto: provide ``query`` (natural language) → pipeline inferred from intent + depth.
    - Explicit steps: provide ``steps`` (list of step dicts) → caller owns the pipeline.

    ``query`` and ``steps`` are mutually exclusive.
    """
    start = time.monotonic_ns()

    # --- Input validation ---
    if query and steps:
        return {"error": "Provide either 'query' or 'steps', not both."}
    if not query and not steps and not operation:
        return {"error": "Provide 'query', 'steps', or 'operation'."}

    # --- Resolve pipeline ---
    step_list, intent = _resolve_pipeline(query, steps, depth, operation)
    pipeline_names: list[str] = []
    step_args_by_name: dict[str, dict[str, Any]] = {}

    for step_def in step_list:
        if isinstance(step_def, dict):
            tool_name = step_def.get("tool", "")
        else:
            tool_name = str(step_def)
            step_def = {"tool": tool_name}
        if tool_name not in _VALID_STEPS:
            return {"error": f"Unknown step: {tool_name!r}. Valid: {sorted(_VALID_STEPS)}"}
        pipeline_names.append(tool_name)
        # Extra args from the step definition (override forwarded values)
        extra = {k: v for k, v in step_def.items() if k != "tool"}
        if extra:
            step_args_by_name[tool_name] = extra

    results: dict[str, dict[str, Any]] = {}
    forwarded: dict[str, Any] = {}

    # --- Execute pipeline ---
    for step_name in pipeline_names:
        # Build forwarded values from prior results
        forwarded = _forward_results(results, query)

        # Merge: forwarded < top-level params < explicit step args
        step_extra = step_args_by_name.get(step_name, {})

        try:
            step_result = await _execute_step(
                step_name=step_name,
                forwarded=forwarded,
                top_level={
                    "file_path": file_path,
                    "code": code,
                    "symbol": symbol,
                    "query": query,
                },
                step_extra=step_extra,
                store=store,
                graph=graph,
                task_parser=task_parser,
                briefing_engine=briefing_engine,
                orchestrator=orchestrator,
                tracker=tracker,
                risk_scorer=risk_scorer,
                adaptive=adaptive,
                grounding_analyzer=grounding_analyzer,
                root_path=root_path,
            )
        except Exception as exc:
            step_result = {"error": f"{step_name} failed: {exc}"}

        # Apply scope filter
        if scope:
            step_result = _apply_scope(step_result, scope, step_name)

        results[step_name] = step_result

        # Short-circuit: if find returned no files, stop pipeline
        if step_name == "find" and "error" not in step_result:
            files_found = step_result.get("files", [])
            if not files_found:
                elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)
                return {
                    "intent": intent,
                    "pipeline": pipeline_names,
                    "scope": scope,
                    "results": results,
                    "summary": "No relevant files found for the given query.",
                    "next_steps": [
                        "Try a more specific query or check that the project is indexed."
                    ],
                    "latency_ms": elapsed_ms,
                }

    # --- Build summary + next_steps ---
    summary = _synthesize_summary(pipeline_names, results)
    next_steps = _build_next_steps(pipeline_names, file_path)

    elapsed_ms = max(1, (time.monotonic_ns() - start) // 1_000_000)

    return {
        "intent": intent,
        "pipeline": pipeline_names,
        "scope": scope,
        "results": results,
        "summary": summary,
        "next_steps": next_steps,
        "latency_ms": elapsed_ms,
    }


def _build_next_steps(pipeline: list[str], file_path: str | None) -> list[str]:
    """Suggest next steps based on what wasn't run."""
    suggestions: list[str] = []
    if "brief" not in pipeline:
        suggestions.append("Run groundtruth_brief for a proactive briefing before writing code.")
    if "validate" not in pipeline and file_path:
        suggestions.append("Run groundtruth_validate to check proposed code against the index.")
    if "trace" not in pipeline:
        suggestions.append("Run groundtruth_trace to see callers/callees of key symbols.")
    return suggestions


async def _execute_step(
    step_name: str,
    forwarded: dict[str, Any],
    top_level: dict[str, Any],
    step_extra: dict[str, Any],
    store: SymbolStore,
    graph: ImportGraph,
    task_parser: TaskParser,
    briefing_engine: BriefingEngine,
    orchestrator: ValidationOrchestrator,
    tracker: InterventionTracker,
    risk_scorer: RiskScorer,
    adaptive: AdaptiveBriefing | None,
    grounding_analyzer: GroundingGapAnalyzer | None,
    root_path: str | None,
) -> dict[str, Any]:
    """Execute a single pipeline step, resolving args from forwarded/top-level/step_extra."""

    def _arg(name: str, default: Any = None) -> Any:
        """Resolve arg: step_extra > top_level > forwarded > default."""
        if name in step_extra:
            return step_extra[name]
        if name in top_level and top_level[name] is not None:
            return top_level[name]
        if name in forwarded and forwarded[name] is not None:
            return forwarded[name]
        return default

    if step_name == "find":
        return await handle_find_relevant(
            description=_arg("description", _arg("query", "")),
            store=store,
            graph=graph,
            task_parser=task_parser,
            tracker=tracker,
            max_files=_arg("max_files", 10),
            entry_points=_arg("entry_points"),
        )

    elif step_name == "brief":
        return await handle_brief(
            intent=_arg("intent", _arg("query", "")),
            briefing_engine=briefing_engine,
            tracker=tracker,
            store=store,
            graph=graph,
            target_file=_arg("target_file", _arg("file_path")),
            adaptive=adaptive,
        )

    elif step_name == "validate":
        fp = _arg("file_path")
        proposed = _arg("code", _arg("proposed_code"))
        if not fp or not proposed:
            return {"error": "validate requires file_path and code"}
        return await handle_validate(
            proposed_code=proposed,
            file_path=fp,
            orchestrator=orchestrator,
            tracker=tracker,
            store=store,
            grounding_analyzer=grounding_analyzer,
            root_path=root_path,
        )

    elif step_name == "trace":
        sym = _arg("symbol")
        if not sym:
            return {"error": "trace requires a symbol"}
        return await handle_trace(
            symbol=sym,
            store=store,
            graph=graph,
            tracker=tracker,
            direction=_arg("direction", "both"),
            max_depth=_arg("max_depth", 3),
        )

    elif step_name == "deps":
        return await _handle_deps_combined(store=store, tracker=tracker)

    elif step_name == "stats":
        return await handle_status(store=store, tracker=tracker)

    elif step_name == "hotspots":
        return await handle_hotspots(
            store=store,
            tracker=tracker,
            limit=_arg("limit", 20),
        )

    elif step_name == "dead_code":
        return await handle_dead_code(store=store, tracker=tracker)

    elif step_name == "unused_packages":
        return await handle_unused_packages(store=store, tracker=tracker)

    elif step_name == "explain":
        sym = _arg("symbol")
        if not sym:
            return {"error": "explain requires a symbol"}
        return await handle_explain(
            symbol=sym,
            store=store,
            graph=graph,
            tracker=tracker,
            root_path=root_path or "",
            file_path=_arg("file_path"),
        )

    elif step_name == "impact":
        sym = _arg("symbol")
        if not sym:
            return {"error": "impact requires a symbol"}
        return await handle_impact(
            symbol=sym,
            store=store,
            graph=graph,
            tracker=tracker,
            root_path=root_path or "",
            max_depth=_arg("max_depth", 3),
        )

    elif step_name == "symbols":
        fp = _arg("file_path")
        if not fp:
            return {"error": "symbols requires a file_path"}
        return await handle_symbols(
            file_path=fp,
            store=store,
            tracker=tracker,
            root_path=root_path,
        )

    elif step_name == "context":
        sym = _arg("symbol")
        if not sym:
            return {"error": "context requires a symbol"}
        return await handle_context(
            symbol=sym,
            store=store,
            graph=graph,
            tracker=tracker,
            root_path=root_path or "",
            limit=_arg("limit", 20),
        )

    elif step_name == "patterns":
        fp = _arg("file_path")
        if not fp:
            return {"error": "patterns requires a file_path"}
        return await handle_patterns(
            file_path=fp,
            store=store,
            tracker=tracker,
            root_path=root_path or "",
        )

    return {"error": f"Unknown step: {step_name}"}
