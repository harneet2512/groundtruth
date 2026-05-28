"""MCP server using stdio transport."""

from __future__ import annotations

import json
import os
import time as _time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from groundtruth.ai.briefing import BriefingEngine
from groundtruth.ai.task_parser import TaskParser
from groundtruth.analysis.adaptive_briefing import AdaptiveBriefing
from groundtruth.analysis.grounding_gap import GroundingGapAnalyzer
from groundtruth.analysis.risk_scorer import RiskScorer
from groundtruth.index.graph import ImportGraph
from groundtruth.index.graph_store import GraphStore, is_graph_db
from groundtruth.index.store import SymbolStore
from groundtruth.lsp.manager import LSPManager
from groundtruth.mcp.tools import (
    handle_brief,
    handle_checkpoint,
    handle_context,
    handle_dead_code,
    handle_do,
    handle_explain,
    handle_find_relevant,
    handle_hotspots,
    handle_impact,
    handle_orient,
    handle_patterns,
    handle_status,
    handle_symbols,
    handle_trace,
    handle_unused_packages,
    handle_validate,
)
from groundtruth.stats.token_tracker import TokenTracker
from groundtruth.stats.tracker import InterventionTracker
from groundtruth.utils.logger import get_logger
from groundtruth.utils.result import Err
from groundtruth.validators.orchestrator import ValidationOrchestrator

log = get_logger("mcp.server")


async def _safe_call(tool_name: str, coro: Any) -> dict[str, Any]:
    """Wrap a tool handler so unhandled exceptions return structured errors."""
    try:
        result: dict[str, Any] = await coro
        return result
    except Exception:
        log.error("tool_error", tool=tool_name, exc_info=True)
        return {"error": f"Internal error in {tool_name}"}


def create_server(
    root_path: str,
    db_path: str | None = None,
    lsp_trace_dir: Path | None = None,
) -> FastMCP:
    """Create and configure the MCP server."""
    app = FastMCP(name="groundtruth")

    # Initialize shared state — auto-detect Go indexer (graph.db) vs Python indexer
    resolved_db = db_path or os.path.join(root_path, ".groundtruth", "index.db")
    # Also check for graph.db from gt-index (Go binary)
    graph_db = os.path.join(root_path, ".groundtruth", "graph.db")
    if db_path is None and os.path.exists(graph_db) and is_graph_db(graph_db):
        resolved_db = graph_db
        log.info("using_graph_db", path=graph_db)

    os.makedirs(os.path.dirname(resolved_db), exist_ok=True)

    if is_graph_db(resolved_db):
        store: SymbolStore = GraphStore(resolved_db)
    else:
        store = SymbolStore(resolved_db)

    init_result = store.initialize()
    if isinstance(init_result, Err):
        raise RuntimeError(f"Failed to initialize store: {init_result.error.message}")

    graph = ImportGraph(store)
    tracker = InterventionTracker(store)
    token_tracker = TokenTracker()

    # AI components get api_key=None — agents ARE the AI
    task_parser = TaskParser(store, api_key=None)
    briefing_engine = BriefingEngine(store, api_key=None)
    lsp_manager = LSPManager(root_path, trace_dir=lsp_trace_dir)
    orchestrator = ValidationOrchestrator(store, lsp_manager, api_key=None)
    risk_scorer = RiskScorer(store)
    adaptive = AdaptiveBriefing(store, risk_scorer)
    grounding_analyzer = GroundingGapAnalyzer(store)

    def _finalize(tool_name: str, result: dict) -> str:  # type: ignore[type-arg]
        """Serialize result as <gt-evidence> text for the model.

        The model sees compact, imperative text inside XML tags.
        Full JSON is logged for analytics but NOT sent to the model.
        """
        # Track tokens on full JSON (for analytics)
        response_json = json.dumps(result)
        call_tokens = token_tracker.track(tool_name, response_json)

        # Log full JSON to structured log for analytics
        log.debug(
            "mcp_response",
            tool=tool_name,
            tokens=call_tokens,
            footprint=token_tracker.get_footprint(tool_name, call_tokens),
            result_json=response_json,
        )

        # Build model-facing text response
        if "error" in result:
            return f"<gt-evidence>\n[SKIP] {result['error']}\n</gt-evidence>"

        lines: list[str] = []

        # Extract reasoning_guidance as the primary content
        guidance = result.get("reasoning_guidance", "")

        # For validate: show errors as imperative items
        if "errors" in result and result.get("errors"):
            for err in result["errors"]:
                msg = err.get("message", "unknown")
                suggestion = err.get("suggestion", {})
                fix = suggestion.get("fix", "")
                conf = suggestion.get("confidence", 0.85)
                tier = "VERIFIED" if conf >= 0.85 else "WARNING" if conf >= 0.6 else "INFO"
                if fix:
                    lines.append(f"[{tier}] {msg} — FIX: {fix} ({conf:.2f})")
                else:
                    lines.append(f"[{tier}] {msg} ({conf:.2f})")
        elif result.get("valid") is True:
            lines.append("[OK] No structural errors found.")

        # For find_relevant: show file list compactly
        if "files" in result and isinstance(result["files"], list):
            for f in result["files"][:10]:
                if isinstance(f, dict):
                    path = f.get("path", "?")
                    reason = f.get("reason", "")
                    rel = f.get("relevance", "")
                    lines.append(f"[{rel.upper()}] {path} — {reason}")
                else:
                    lines.append(f"  {f}")

        # For brief: show briefing text
        if "briefing" in result and result["briefing"]:
            lines.append(result["briefing"])

        # For trace: show callers/callees
        if "callers" in result and isinstance(result["callers"], list):
            for c in result["callers"][:5]:
                if isinstance(c, dict):
                    lines.append(
                        f"  caller: {c.get('file', '?')}:{c.get('line', '?')} — {c.get('context', '')}"
                    )

        # Fallback if no structured content was extracted
        if not lines:
            if guidance:
                lines.append(guidance)
            else:
                return "<gt-evidence>\n[OK] Completed — no structured findings.\n</gt-evidence>"

        return "<gt-evidence>\n" + "\n".join(lines) + "\n</gt-evidence>"

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_find_relevant(
        description: str,
        entry_points: list[str] | None = None,
        entry_symbols: list[str] | None = None,
        max_files: int = 10,
    ) -> str:
        """Find relevant files for a task. Given a task description, returns ranked files."""
        result = await _safe_call(
            "groundtruth_find_relevant",
            handle_find_relevant(
                description=description,
                store=store,
                graph=graph,
                task_parser=task_parser,
                tracker=tracker,
                entry_points=entry_points,
                entry_symbols=entry_symbols,
                max_files=max_files,
            ),
        )
        return _finalize("groundtruth_find_relevant", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_brief(
        intent: str,
        target_file: str | None = None,
    ) -> str:
        """Proactive briefing before code generation. Tell me what I need to know."""
        result = await _safe_call(
            "groundtruth_brief",
            handle_brief(
                intent=intent,
                briefing_engine=briefing_engine,
                tracker=tracker,
                store=store,
                graph=graph,
                target_file=target_file,
                adaptive=adaptive,
            ),
        )
        return _finalize("groundtruth_brief", result)

    # @app.tool()  # Deprecated: use groundtruth_check_v2 instead
    async def groundtruth_validate(
        proposed_code: str,
        file_path: str,
        language: str | None = None,
    ) -> str:
        """Validate proposed code against the codebase index."""
        result = await _safe_call(
            "groundtruth_validate",
            handle_validate(
                proposed_code=proposed_code,
                file_path=file_path,
                orchestrator=orchestrator,
                tracker=tracker,
                store=store,
                language=language,
                grounding_analyzer=grounding_analyzer,
                root_path=root_path,
                graph=graph,
            ),
        )
        return _finalize("groundtruth_validate", result)

    # @app.tool()  # Deprecated: use groundtruth_investigate instead
    async def groundtruth_trace(
        symbol: str,
        direction: str = "both",
        max_depth: int = 3,
    ) -> str:
        """Trace a symbol through the codebase. Zero AI. Pure graph."""
        result = await _safe_call(
            "groundtruth_trace",
            handle_trace(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                direction=direction,
                max_depth=max_depth,
            ),
        )
        return _finalize("groundtruth_trace", result)

    # @app.tool()  # Deprecated: use groundtruth_status_v2 instead
    async def groundtruth_status() -> str:
        """Health check and stats."""
        result = await _safe_call("groundtruth_status", handle_status(store=store, tracker=tracker))
        return _finalize("groundtruth_status", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_dead_code() -> str:
        """Find exported symbols with zero references. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_dead_code", handle_dead_code(store=store, tracker=tracker)
        )
        return _finalize("groundtruth_dead_code", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_unused_packages() -> str:
        """Find installed packages that no file imports. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_unused_packages", handle_unused_packages(store=store, tracker=tracker)
        )
        return _finalize("groundtruth_unused_packages", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_hotspots(limit: int = 20) -> str:
        """Most referenced symbols in the codebase. Pure SQL. Zero AI."""
        result = await _safe_call(
            "groundtruth_hotspots", handle_hotspots(store=store, tracker=tracker, limit=limit)
        )
        return _finalize("groundtruth_hotspots", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2 instead
    async def groundtruth_orient() -> str:
        """Codebase orientation — structure, entry points, risk summary."""
        result = await _safe_call(
            "groundtruth_orient",
            handle_orient(
                store=store,
                graph=graph,
                tracker=tracker,
                risk_scorer=risk_scorer,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_orient", result)

    # @app.tool()  # Deprecated: use groundtruth_status_v2 instead
    async def groundtruth_checkpoint() -> str:
        """Session progress summary with recommendations."""
        result = await _safe_call(
            "groundtruth_checkpoint",
            handle_checkpoint(
                store=store,
                tracker=tracker,
                risk_scorer=risk_scorer,
            ),
        )
        return _finalize("groundtruth_checkpoint", result)

    # @app.tool()  # Deprecated: use groundtruth_orient_v2(file_path=...) instead
    async def groundtruth_symbols(file_path: str) -> str:
        """List all symbols in a file with imports and importers."""
        result = await _safe_call(
            "groundtruth_symbols",
            handle_symbols(
                file_path=file_path,
                store=store,
                tracker=tracker,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_symbols", result)

    # @app.tool()  # Deprecated: use groundtruth_investigate instead
    async def groundtruth_context(symbol: str, limit: int = 20) -> str:
        """Show symbol usage context with code snippets."""
        result = await _safe_call(
            "groundtruth_context",
            handle_context(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                limit=limit,
            ),
        )
        return _finalize("groundtruth_context", result)

    # @app.tool()  # Deprecated: use groundtruth_investigate instead
    async def groundtruth_explain(
        symbol: str,
        file_path: str | None = None,
    ) -> str:
        """Deep dive into a symbol — source, callers, callees, side effects, complexity."""
        result = await _safe_call(
            "groundtruth_explain",
            handle_explain(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                file_path=file_path,
            ),
        )
        return _finalize("groundtruth_explain", result)

    # @app.tool()  # Deprecated: use groundtruth_investigate instead
    async def groundtruth_impact(
        symbol: str,
        max_depth: int = 3,
    ) -> str:
        """Assess blast radius of modifying a symbol — callers, break risk, safe/unsafe changes."""
        result = await _safe_call(
            "groundtruth_impact",
            handle_impact(
                symbol=symbol,
                store=store,
                graph=graph,
                tracker=tracker,
                root_path=root_path,
                max_depth=max_depth,
            ),
        )
        return _finalize("groundtruth_impact", result)

    # @app.tool()  # Deprecated: use groundtruth_check_v2 instead
    async def groundtruth_patterns(file_path: str) -> str:
        """Detect coding conventions in sibling files of the same directory."""
        result = await _safe_call(
            "groundtruth_patterns",
            handle_patterns(
                file_path=file_path,
                store=store,
                tracker=tracker,
                root_path=root_path,
            ),
        )
        return _finalize("groundtruth_patterns", result)

    # @app.tool()  # Deprecated: use the 4 primary endpoints instead
    async def groundtruth_do(
        query: str | None = None,
        steps: list[dict[str, Any]] | None = None,
        scope: str | None = None,
        depth: str = "standard",
        file_path: str | None = None,
        code: str | None = None,
        symbol: str | None = None,
        operation: str | None = None,
    ) -> str:
        """Single entry point for all GroundTruth operations.

        Two modes:
        - Smart auto: provide ``query`` → pipeline inferred from intent + depth.
        - Explicit steps: provide ``steps`` list → caller owns the pipeline.

        ``query`` and ``steps`` are mutually exclusive.
        ``scope`` filters results to files matching the given path prefix.
        """
        result = await _safe_call(
            "groundtruth_do",
            handle_do(
                query=query,
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
                operation=operation,
                file_path=file_path,
                code=code,
                symbol=symbol,
                depth=depth,
                steps=steps,
                scope=scope,
            ),
        )
        return _finalize("groundtruth_do", result)

    @app.tool()
    async def gt_plan(plan_path: str | None = None, full: bool = False) -> str:
        """Return the current v7 edit plan JSON. Compact, deterministic."""
        await _ensure_lsp_promotion()
        _tool_start = _time.monotonic()
        from groundtruth.cli.commands import _load_plan_json
        from groundtruth.runtime.plan_surface import compact_plan, served_plan_record
        from groundtruth.runtime.telemetry import append_block

        plan = _load_plan_json(plan_path)
        result = plan if full else compact_plan(plan)
        append_block(
            "gt_plan_served",
            served_plan_record(plan, full=full, surface="mcp"),
            task_id=str(plan.get("task_id", "unknown")),
        )
        text = "<gt-evidence>\n" + json.dumps(result, sort_keys=True) + "\n</gt-evidence>"
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="gt_plan",
                 params={"plan_path": plan_path, "full": full},
                 result_len=len(text),
                 latency_ms=_tool_elapsed)
        return text

    # @app.tool()  # Deprecated: use groundtruth_check_v2 instead
    async def gt_patch_check(plan_path: str | None = None) -> str:
        """Run the canonical patch-shape auditor against the current diff."""
        from groundtruth.runtime.patch_auditor import audit_patch

        result = audit_patch(root_path, plan_path=plan_path)
        return "<gt-evidence>\n" + json.dumps(result, sort_keys=True) + "\n</gt-evidence>"

    @app.tool()
    async def gt_run_tests(
        mode: str = "contract",
        plan_path: str | None = None,
        execute: bool = False,
        timeout: int = 120,
    ) -> str:
        """Select repo-native tests for cluster, changed, or contract scope."""
        _tool_start = _time.monotonic()
        from groundtruth.cli.commands import _load_plan_json
        from groundtruth.runtime.patch_auditor import audit_patch
        from groundtruth.runtime.test_runner import execute_test_command, select_test_command

        plan = _load_plan_json(plan_path)
        patch = audit_patch(root_path, plan=plan)
        changed = patch["source_files_touched"] + patch["test_files_touched"]
        result: dict[str, object] = {
            "selection": select_test_command(root_path, mode=mode, plan=plan, changed_files=changed)
        }
        if execute:
            selection = result["selection"]
            assert isinstance(selection, dict)
            result["execution"] = execute_test_command(
                root_path,
                list(selection.get("command", []) or []),
                timeout_seconds=timeout,
                mode=mode,
                selected_contract_files=list(selection.get("selected_contract_files", []) or []),
            )
        text = "<gt-evidence>\n" + json.dumps(result, sort_keys=True) + "\n</gt-evidence>"
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="gt_run_tests",
                 params={"mode": mode, "execute": execute, "timeout": timeout},
                 result_len=len(text),
                 latency_ms=_tool_elapsed)
        return text

    # @app.tool()  # Deprecated: use groundtruth_investigate instead
    async def gt_why(file_path: str, plan_path: str | None = None) -> str:
        """Explain why a file is in the current candidate cluster."""
        from groundtruth.cli.commands import _load_plan_json

        plan = _load_plan_json(plan_path)
        norm = file_path.replace("\\", "/").lstrip("./")
        cluster = plan.get("cluster_files", [])
        pattern = plan.get("implementation_pattern", [])
        if norm in cluster:
            text = f"{norm} is in the v7 candidate cluster. " + " ".join(map(str, pattern[:2]))
        else:
            text = f"{norm} is not in the current v7 candidate cluster."
        return f"<gt-evidence>\n{text}\n</gt-evidence>"

    @app.tool()
    async def gt_contract(file_or_symbol: str | None = None, plan_path: str | None = None) -> str:
        """Return contract lines from the current v7 plan."""
        _tool_start = _time.monotonic()
        # file_or_symbol is accepted for API compatibility but not used in this implementation.
        _ = file_or_symbol
        from groundtruth.cli.commands import _load_plan_json

        plan = _load_plan_json(plan_path)
        lines = plan.get("contract_lines", [])
        text = "\n".join(f"- {line}" for line in lines) if lines else "No contract lines in plan."
        result_text = f"<gt-evidence>\n{text}\n</gt-evidence>"
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="gt_contract",
                 params={"file_or_symbol": file_or_symbol, "plan_path": plan_path},
                 result_len=len(result_text),
                 latency_ms=_tool_elapsed)
        return result_text

    # @app.tool()  # Deprecated: use gt_plan instead
    async def gt_replan(plan_path: str | None = None) -> str:
        """Evaluate deterministic replan triggers from the current diff."""
        from groundtruth.cli.commands import _load_plan_json
        from groundtruth.runtime.patch_auditor import audit_patch
        from groundtruth.runtime.replan import evaluate_replan_triggers

        plan = _load_plan_json(plan_path)
        patch = audit_patch(root_path, plan=plan)
        edited = patch["source_files_touched"] + patch["test_files_touched"] + patch[
            "outside_cluster_files"
        ]
        result = evaluate_replan_triggers(
            edited_files=edited,
            plan=plan,
            warning_history=patch["warnings"],
            patch_shape=patch,
        )
        return "<gt-evidence>\n" + json.dumps(result, sort_keys=True) + "\n</gt-evidence>"

    # --- vNext Decision Interface surfaces ---
    from groundtruth.mcp.endpoints.task_map import handle_task_map
    from groundtruth.mcp.endpoints.event_brief import handle_event_brief
    from groundtruth.mcp.endpoints.review_patch import handle_review_patch
    from groundtruth.schema.novelty import NoveltyFilter

    _novelty = NoveltyFilter()

    # @app.tool()  # Deprecated: SWE-bench specific
    async def groundtruth_task_map(
        issue_text: str,
        entry_files: list[str] | None = None,
    ) -> str:
        """Pre-task: localization, repo shape, caller/test constraints for mentioned symbols. Call ONCE at task start."""
        result = await _safe_call(
            "groundtruth_task_map",
            handle_task_map(
                issue_text=issue_text,
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=_novelty,
                entry_files=entry_files,
            ),
        )
        return result.get("error") or result.get("text", "")

    # @app.tool()  # Deprecated: SWE-bench specific
    async def groundtruth_event_brief(
        file_path: str,
    ) -> str:
        """Post-edit: only new deterministic findings for the just-modified file. Silent when nothing to say."""
        result = await _safe_call(
            "groundtruth_event_brief",
            handle_event_brief(
                file_path=file_path,
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=_novelty,
            ),
        )
        return result.get("error") or result.get("text", "")

    # @app.tool()  # Deprecated: use groundtruth_check_v2 instead
    async def groundtruth_review_patch(
        file_path: str | None = None,
    ) -> str:
        """Pre-submit: full deterministic diff review. Obligations, contradictions, call-site voting. Call ONCE before submitting."""
        result = await _safe_call(
            "groundtruth_review_patch",
            handle_review_patch(
                store=store,
                graph=graph,
                root_path=root_path,
                novelty_filter=_novelty,
                file_path=file_path,
            ),
        )
        return result.get("error") or result.get("text", "")

    # --- Consolidated endpoints (16→4) ---
    from groundtruth.mcp.endpoints.investigate import (
        handle_investigate as _handle_investigate,
    )
    from groundtruth.mcp.endpoints.orient import handle_orient as _handle_orient
    from groundtruth.mcp.endpoints.consolidated_check import (
        handle_check as _handle_check,
    )
    from groundtruth.mcp.endpoints.consolidated_status import (
        handle_status as _handle_status_consolidated,
    )

    _session_calls = 0
    _session_findings = 0
    _session_fix_required = 0

    @app.tool()
    async def groundtruth_investigate(
        symbol: str,
        file_path: str | None = None,
    ) -> str:
        """Deep-dive on a symbol — callers, callees, impact, obligations. High-confidence only."""
        await _ensure_lsp_promotion()
        _tool_start = _time.monotonic()
        nonlocal _session_calls, _session_findings
        _session_calls += 1
        try:
            text = await _handle_investigate(
                symbol=symbol,
                file_path=file_path,
                store=store,
                graph=graph,
                novelty=_novelty,
            )
        except Exception:
            log.error("tool_error", tool="groundtruth_investigate", exc_info=True)
            return "[GT] Internal error in groundtruth_investigate"
        if text:
            _session_findings += max(0, text.count("\n") - 2)
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="groundtruth_investigate",
                 params={"symbol": symbol, "file_path": file_path},
                 result_len=len(text) if text else 0,
                 latency_ms=_tool_elapsed)
        return text

    @app.tool()
    async def groundtruth_orient_v2(
        task: str | None = None,
        file_path: str | None = None,
    ) -> str:
        """What's relevant to this task or file — localization, hotspots, imports. High-confidence only."""
        _tool_start = _time.monotonic()
        nonlocal _session_calls, _session_findings
        _session_calls += 1
        try:
            text = await _handle_orient(
                task=task,
                file_path=file_path,
                store=store,
                graph=graph,
                novelty=_novelty,
            )
        except Exception:
            log.error("tool_error", tool="groundtruth_orient_v2", exc_info=True)
            return "[GT] Internal error in groundtruth_orient_v2"
        if text:
            _session_findings += max(0, text.count("\n") - 2)
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="groundtruth_orient_v2",
                 params={"task": task, "file_path": file_path},
                 result_len=len(text) if text else 0,
                 latency_ms=_tool_elapsed)
        return text

    @app.tool()
    async def groundtruth_check_v2(
        file_path: str | None = None,
        proposed_code: str | None = None,
    ) -> str:
        """Validate your edit — contradictions, obligations, structural issues. Silent when nothing to say."""
        _tool_start = _time.monotonic()
        nonlocal _session_calls, _session_findings, _session_fix_required
        _session_calls += 1
        try:
            text = await _handle_check(
                file_path=file_path,
                proposed_code=proposed_code,
                store=store,
                novelty=_novelty,
            )
        except Exception:
            log.error("tool_error", tool="groundtruth_check_v2", exc_info=True)
            return "[GT] Internal error in groundtruth_check_v2"
        if text:
            _session_findings += max(0, text.count("\n") - 2)
            if "FIX REQUIRED" in text:
                _session_fix_required += text.count("FIX REQUIRED")
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="groundtruth_check_v2",
                 params={"file_path": file_path, "proposed_code_len": len(proposed_code) if proposed_code else 0},
                 result_len=len(text) if text else 0,
                 latency_ms=_tool_elapsed)
        return text

    @app.tool()
    async def groundtruth_status_v2() -> str:
        """Index health and session summary."""
        await _ensure_lsp_promotion()
        _tool_start = _time.monotonic()
        try:
            text = await _handle_status_consolidated(
                store=store,
                session_calls=_session_calls,
                session_findings=_session_findings,
                session_fix_required=_session_fix_required,
            )
        except Exception:
            log.error("tool_error", tool="groundtruth_status_v2", exc_info=True)
            return "[GT] Internal error in groundtruth_status_v2"
        _tool_elapsed = int((_time.monotonic() - _tool_start) * 1000)
        log.info("tool_call", tool="groundtruth_status_v2",
                 params={},
                 result_len=len(text) if text else 0,
                 latency_ms=_tool_elapsed)
        return text

    # Background LSP promotion — starts on first tool call.
    # Detects installed language servers, promotes name_match edges progressively.
    _lsp_started = False

    async def _ensure_lsp_promotion() -> None:
        nonlocal _lsp_started
        if _lsp_started:
            return
        _lsp_started = True
        if not is_graph_db(resolved_db):
            return
        try:
            from groundtruth.lsp.background_promotion import start_background_promotion
            start_background_promotion(resolved_db, root_path)
        except Exception:
            pass

    return app
